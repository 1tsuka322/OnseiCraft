# -*- coding: utf-8 -*-
r"""
角色判明：把声纹聚出来的 SPEAKER_XX 对应到具体角色。

**为什么需要这一步**：diarize.py 聚的是音色。同一声优分饰多角时（姐妹、双子、
一人饰两角）会被并成同一类，光看声纹分不出来。只有结合自称、被称呼的叫法、
口吻和剧情才能判明"这句到底是谁说的"。

做法：把每个 SPEAKER 的代表句（日文 + 就近的人工中文）+ 整轨人工字幕的剧情上下文
一起交给 Qwen，让它给出角色名、身份和判断依据。

用法：
  python identify.py <ASR目录> <人工字幕目录> [文件名关键字]

产出：改写 json，写 "roles": {"SPEAKER_00": {"name": "铃", "role": "姐姐",
      "note": "自称「姐姐」，被对方叫「铃」", "lines": 210}, ...}
"""
import os
import re
import sys
import json
import glob
import time
import argparse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "http://127.0.0.1:8080/v1/chat/completions"
N_SAMPLE = 12      # 每个说话人取几句代表作
CTX_LINES = 120    # 人工字幕里取多少行当剧情背景
WORKERS = 2

SYSTEM = """你是日本音声作品（ASMR 广播剧）的考据与字幕编辑。
下面给出某一轨对白，已经按"音色"聚类成若干说话人类别（SPEAKER_00/01…）。
请你判断每个类别对应剧本里的哪个角色。

关键难点：同一声优常常分饰多个角色，音色完全相同，只靠声音分不出来。

**先分清「自称」和「称呼」，这是最容易判反的地方**（曾把父女作品判成母子、
把「お兄さん」当成女主的自称）：
- 自称 = 说话人提到**自己**用的词：私／あたし／俺／僕／うち／自分の名前。
- 称呼对方 = **お兄さん／お兄ちゃん／お父さん／お母さん／先生／先輩／君／あなた／旦那様**。
  这些词出现在某段台词里，**几乎一定是那个人在跟对方说话**，不能拿来当自称。
  典型误判：女主对哥哥说「お兄さん、見て」→ 若据此判定 SPEAKER_00 自称「お兄さん」、
  角色是"哥哥"，就完全判反了。正确读法：SPEAKER_00 在叫别人「お兄さん」，所以她不是哥哥。
- 被称呼 = 对方在对话里怎么叫他，出现在**另一个 SPEAKER 的句子里**。
- 判据要能自洽：A 自称「私」且被 B 叫「娘」（女儿），那 A 是女儿、B 是父亲。
  反过来若判定 A 是父亲，就必须解释为什么别人叫他「娘」。

判断线索：
1. **自称**：说话人怎么称呼自己（あたし／私／俺／お姉ちゃん／妹 等）
2. **被称呼**：**别人**（别的 SPEAKER 的台词里）怎么叫他（「お姉ちゃん」「響」「先生」「お前」等）
3. **口吻**：敬语／タメ口／傲娇／年上感／孩子气 等
4. **剧情位置**：谁在跟谁说话、场景里应该有几个人
5. **作品标题**：标题常直接写出人物关系（"父亲和女儿""姐妹""JK"），是强线索

注意：
- 台词里大量出现器官/性行为的词汇，那是**剧情内容，不是身份线索**，别据此判断角色。
- 某一类只有 0 句或极少几句，说明它是聚类噪声，role 写「（次要/不明）」即可。
- 两个类别其实是同一人（口吻与自称一致），就都标同一角色名并在 note 里注明"与 XX 同一人"。

note 一句话说完，**控制在 30 字以内，禁止重复罗列同一句台词**
（曾出现 note 里刷几百个「くぅ」把输出撑爆、JSON 被截断）。

只输出 JSON，不要解释。格式：
{"SPEAKER_00": {"name": "角色名或通称", "role": "身份（姐姐/妹妹/主角/路人等）",
                "note": "判断依据，引用原文里的自称或称呼"},
 "SPEAKER_01": {...}}
名字不确定就写最通用的称呼（如「女主」「少年」），不要留空。"""


def read_lrc(path):
    out = []
    for raw in open(path, "rb").read().decode("utf-8", errors="ignore").splitlines():
        m = re.match(r"\s*\[(\d+):([\d.]+)\]\s*(.*)$", raw)
        if m:
            out.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
    return out


def call(messages, temperature=0.2, max_tokens=900):
    payload = {"model": "qwen", "messages": messages, "temperature": temperature,
               "max_tokens": max_tokens, "top_p": 0.9, "repetition_penalty": 1.05,
               "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(API, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    for k in range(3):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]
        except Exception:
            if k == 2:
                raise
            time.sleep(2 * (k + 1))
    return ""


def nearest_zh(zh_lines, t):
    best, bd = "", 9e9
    for ts, s in zh_lines:
        d = abs(ts - t)
        if d < bd:
            bd, best = d, s
        if ts > t + 5:
            break
    return best if bd < 6 else ""


def _balanced(s, i):
    """从 s[i] == '{' 起做括号配平扫描，返回配平的结束下标；未配平返回 -1。
    会跳过字符串字面量里的花括号，所以 note 里出现 { } 也不会算错。"""
    depth, j, n = 0, i, len(s)
    in_str = False
    while j < n:
        c = s[j]
        if in_str:
            if c == "\\":
                j += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def parse_roles(reply):
    """模型常在 note 里陷入重复（几百个「くぅ」），输出被 max_tokens 截断，
    JSON 缺尾巴。这里三级降级：整段解析 → 配平子串 → 砍掉最后一个残缺条目再补 }。"""
    start = reply.find("{")
    if start < 0:
        return None
    end = _balanced(reply, start)
    if end > 0:
        try:
            d = json.loads(reply[start:end + 1])
        except Exception:
            d = None
        if isinstance(d, dict):
            r = {k: v for k, v in d.items()
                 if isinstance(v, dict) and k.startswith("SPEAKER")}
            if r:
                return r
    # 截断修复：从后往前找 "}" 当条目边界，逐个尝试补尾巴再解析。
    # 尾巴不只是 "}"——模型还常漏写 note 的**结束引号**（实测 27 个引号这种奇数），
    # 所以要把 '"}'、'"}}' 之类组合都试一遍。
    frag = reply[start:]
    tails = ("}", '"}', '"}}', "}}", '"}}', '"\n}')
    tried = 0
    for cut in range(len(frag) - 1, 0, -1):
        if frag[cut] != "}":
            continue
        for tail in tails:
            try:
                d = json.loads(frag[:cut + 1] + tail)
            except Exception:
                continue
            if isinstance(d, dict):
                r = {k: v for k, v in d.items()
                     if isinstance(v, dict) and k.startswith("SPEAKER")}
                if r:
                    return r
        tried += 1
        if tried >= 8:      # 试够 8 个边界就收手，别在长文本上死循环
            break
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("asr_dir")
    ap.add_argument("lrc_dir")
    ap.add_argument("--corpus", action="store_true",
                    help="语料库模式：asr_dir 下每个子目录是一个作品的 asr/，"
                         "lrc_dir 下同名的子目录放人工字幕")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--force", action="store_true", help="已判明角色的轨也重算（默认跳过）")
    ap.add_argument("key", nargs="?", default="")
    a = ap.parse_args()

    try:
        urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=10).read()
    except Exception as e:
        print("llama-server 未就绪: %s\n先跑 serve.py start" % e)
        return

    if a.corpus:
        tasks = []
        for w in sorted(os.listdir(a.asr_dir)):
            ad = os.path.join(a.asr_dir, w, "asr")
            ld = os.path.join(a.lrc_dir, w)
            if not os.path.isdir(ad) or not os.path.isdir(ld):
                continue
            if a.key and a.key not in w:
                continue
            for jf in sorted(glob.glob(os.path.join(glob.escape(ad), "*.json"))):
                tasks.append((jf, ld, w))
        print("语料库模式：%d 个轨" % len(tasks), flush=True)
    else:
        files = sorted(glob.glob(os.path.join(glob.escape(a.asr_dir), "*.json")))
        if a.key:
            files = [f for f in files if a.key in os.path.basename(f)]
        tasks = [(f, a.lrc_dir, os.path.basename(os.path.dirname(a.lrc_dir.rstrip("\\/")))) for f in files]
    if not tasks:
        print("没有 ASR json")
        return

    def run_one(jf, lrc_dir, title=""):
        d = json.load(open(jf, encoding="utf-8"))
        segs = d.get("segments", [])
        if not segs or not d.get("speakers"):
            print("跳过（还没做声纹聚类）%s" % d["source"][:40], flush=True)
            return
        # 已判明角色的轨默认跳过：语料是分批追加的，重跑不该把已完成的全部重算一遍。
        # 要重算用 --force（例如改了判据/prompt 之后）。
        if d.get("roles") and not a.force:
            return
        src = d["source"]
        base = os.path.splitext(src)[0]
        lrc = os.path.join(lrc_dir, base + ".lrc")
        zh_lines = read_lrc(lrc) if os.path.exists(lrc) else []
        if not zh_lines:
            cand = [p for p in glob.glob(os.path.join(glob.escape(a.lrc_dir), "*.lrc"))
                    if base[:12] in os.path.basename(p)]
            if cand:
                zh_lines = read_lrc(cand[0])

        by_spk = {}
        for s in segs:
            by_spk.setdefault(s.get("speaker", "SPEAKER_00"), []).append(s)
        # 剧情背景：人工字幕开头一段 + 结尾一段
        ctx = "\n".join(t for _, t in zh_lines[:CTX_LINES] if t)
        if len(zh_lines) > CTX_LINES:
            ctx += "\n…\n" + "\n".join(t for _, t in zh_lines[-40:] if t)

        blocks = []
        for spk in sorted(by_spk):
            ss = sorted(by_spk[spk], key=lambda x: -(x["end"] - x["start"]))[:N_SAMPLE]
            ss = sorted(ss, key=lambda x: x["start"])
            rows = []
            for s in ss:
                zh = nearest_zh(zh_lines, s["start"])
                rows.append("日：%s%s" % (s["text"], ("\n中：%s" % zh) if zh else ""))
            blocks.append("【%s】共 %d 句，以下是对白片段：\n%s"
                          % (spk, len(by_spk[spk]), "\n".join(rows)))

        # 标题常直接写明人物关系（父女/姐妹/JK），是对抗"自称/称呼判反"的最强线索
        head = "作品：%s\n轨名：%s\n" % (title[:70], base[:60]) if title else "作品/轨名：%s\n" % base[:60]
        # 明确列出真实存在的编号：模型常凭空多编一个 SPEAKER_0N（实测占 28%）
        keys = ", ".join(sorted(by_spk))
        user = (head + "\n【人工中文字幕（剧情背景，可能含角色名与称呼）】\n%s\n\n"
                "【按音色分类的对白】\n%s\n\n"
                "本轨实际存在的类别只有：%s（共 %d 个）。"
                "只输出这些编号，不要新增别的 SPEAKER 编号，也不要少输出。"
                % (ctx[:3000], "\n\n".join(blocks), keys, len(by_spk)))
        reply = call([{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": user}])
        roles = parse_roles(reply)
        if not roles:
            print("  解析失败 %s" % base[:40], flush=True)
            print("  " + reply[:200].replace("\n", " "), flush=True)
            return
        # 防御：模型偶尔会编造不存在的 SPEAKER_0N，写回前按 segments 里是否真有句过滤
        real = {}
        for spk, info in roles.items():
            n = len(by_spk.get(spk, []))
            if n:
                info["lines"] = n
                real[spk] = info
        if not real:
            print("  全部为编造编号，放弃写入 %s" % base[:40], flush=True)
            return
        roles = real
        d["roles"] = roles
        json.dump(d, open(jf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("%s" % base[:44])
        for spk in sorted(roles):
            r = roles[spk]
            print("   %s = %s（%s）  %d句  依据：%s"
                  % (spk, r.get("name", "?"), r.get("role", "?"),
                     r.get("lines", 0), str(r.get("note", ""))[:60]), flush=True)

    # 逐轨判明是独立任务，llama-server 用 -np 3 开的，这里并行 3 路正好吃满
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(lambda t: run_one(*t), tasks))
    print("\n完成 ->", a.asr_dir)


if __name__ == "__main__":
    main()
