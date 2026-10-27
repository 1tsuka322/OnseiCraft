# -*- coding: utf-8 -*-
r"""
机译 vs 人工对照：找出"机器译得像机器"的地方。

**为什么需要这一步**：对齐只能学到"这句日文对应这句中文"，学不到
"机器常犯什么错"。同一个意思，机器会照日语语序直译，人工会按中文口语重新组织——
这种差距光看平行语料看不出来，必须让机器先翻一遍，再跟人工稿逐句比。

流程：
  1. 从 parallel.json 采样若干句对
  2. 用**当前线上翻译的提示词**（translate.py 的 SYSTEM，不带人工参考）翻一遍
  3. 逐句算与人工译文的字面差异，挑差异最大的当典型样本
  4. 交给大模型归纳"机器译法的通病"，写成可执行的修正规则

产出（$KIKOERU_ROOT/data/style）：
  mt_vs_human.txt   对照样本 + 归纳出的通病清单（给人看）
  并把结论追加进 hard_rules.txt（给模型看）

用法：
  python contrast.py [--n 400] [--style DIR] [--no-llm]
"""
import os
import re
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json
import random
import difflib
import argparse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# 上一层是 <skill>/kikoeru-style-learn，再上一层才是两个 skill 的共同父目录
SUB = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                   "kikoeru-subtitle", "scripts")
assert os.path.isdir(SUB), "找不到 subtitle 脚本目录：%s" % SUB
sys.path.insert(0, SUB)
sys.path.insert(0, HERE)
import translate as T  # noqa: E402
import align_style as A  # noqa: E402

BATCH = 12      # 每批几句（和线上翻译一致）
TOPN = 40       # 挑差异最大的多少组当典型样本


def sim(a, b):
    """字面相似度 0~1。"""
    return difflib.SequenceMatcher(None, a, b).ratio()


def pick_samples(pairs, n):
    """挑有信息量的句子：太短的（「あ」「ん」）没内容，太长的容易对错位。"""
    cand = [p for p in pairs if 6 <= len(p["ja"]) <= 45 and 3 <= len(p["zh"]) <= 40]
    random.seed(7)
    random.shuffle(cand)
    return cand[:n]


def mt_translate(samples):
    """用线上提示词翻译，返回与 samples 等长的列表（失败为 None）。"""
    out = [None] * len(samples)
    blocks = [samples[i:i + BATCH] for i in range(0, len(samples), BATCH)]
    for bi, b in enumerate(blocks, 1):
        # block 元素是 (编号, 文本) 元组，编号从 1 起（与 translate.py 内部一致）
        blk = [(k + 1, p["ja"]) for k, p in enumerate(b)]
        try:
            res, _ = T.translate_block(blk, None)
        except Exception as e:
            print("  批 %d 失败：%s" % (bi, str(e)[:80]), flush=True)
            res = None
        if res and len(res) == len(b):
            for k, t in enumerate(res):
                out[bi * BATCH - BATCH + k] = t
        if bi % 5 == 0:
            print("  已翻 %d/%d 句" % (bi * BATCH, len(samples)), flush=True)
    return out


ANALYZE = """下面是同一批日语句子的两种中文译文：
- 【机器】是用当前翻译提示词自动翻的
- 【人工】是资深译者翻的定稿

**先做筛选再归纳**：语料是靠时间戳自动对齐的，里面混有少量**错配对**
（日文原文和人工译文讲的完全不是一回事，连提到的动作、对象都搭不上）。
这类错配对**必须直接丢弃**，不要拿它归纳任何结论 —— 从它们身上只能得到
"缺乏感情""太直白"这种空话。判定方法：人工译文里是否出现了日文原文完全没提到
的动作、对象或语义；如果两者讲的不是同一件事，就是错配。

对其余**确实是一回事**的样本，归纳**机器译法相对人工的通病**，重点是"怎么改才像人话"。

只输出 Markdown 清单，每条格式：
- 问题名：说明（一句话）｜机器：例｜人工：例｜改法：…

要求：
1. 只写真实出现在下面样本里的问题，不要凭空编；样本不足 3 例支撑的不要写
2. 最多 12 条，按出现频次从高到低排
3. **每条必须能直接当规则执行**：
   - 允许："拟态词「びんびん=硬邦邦、メロメロ=晕陶陶」要按词义译，不要按字面"
   - 允许："应允语气「いいんですよ」译成「可以哦~」，不要译成「没关系」"
   - 禁止："缺乏情感波动""过于直白""不够自然" 这类无法执行的评价
4. 如果某条差异只是**可选的表达偏好**（两种译法都成立），不要写
5. 不要复述样本全文，每条例句不超过 20 字

样本：
%s"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--style", default=A.DEFAULT_STYLE)
    ap.add_argument("--n", type=int, default=400)
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()

    P = os.path.join(a.style, "parallel.json")
    if not os.path.exists(P):
        print("没有平行语料，先跑 build_library.py")
        return
    pairs = json.load(open(P, encoding="utf-8")).get("pairs", [])
    if len(pairs) < 50:
        print("语料太少（%d 组）" % len(pairs))
        return

    try:
        urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=10).read()
    except Exception as e:
        print("llama-server 未就绪：%s\n先跑 serve.py start" % e)
        return

    samples = pick_samples(pairs, a.n)
    print("采样 %d 句，用线上提示词翻译中 ..." % len(samples), flush=True)
    mt = mt_translate(samples)

    rows = []
    tot = 0.0
    for p, m in zip(samples, mt):
        if not m:
            continue
        s = sim(m, p["zh"])
        tot += s
        rows.append({"ja": p["ja"], "mt": m, "human": p["zh"], "sim": round(s, 3)})
    if not rows:
        print("翻译全部失败")
        return
    avg = tot / len(rows)
    rows.sort(key=lambda r: r["sim"])
    print("\n机译与人工稿平均字面相似度 %.1f%%（%d 句）" % (avg * 100, len(rows)))
    print("相似度分布：<30%% %d 句 | 30-60%% %d 句 | >60%% %d 句"
          % (sum(1 for r in rows if r["sim"] < 0.3),
             sum(1 for r in rows if 0.3 <= r["sim"] < 0.6),
             sum(1 for r in rows if r["sim"] >= 0.6)))

    top = rows[:TOPN]
    print("\n=== 差异最大的 %d 组（含时间戳错配噪声，仅供参考）===" % len(top))
    for r in top[:12]:
        print("  日：%s" % r["ja"][:40])
        print("    机器：%s" % r["mt"][:40])
        print("    人工：%s" % r["human"][:40])

    # ---------- 量化对比（不依赖大模型，可复现）----------
    # LLM 归纳容易把「时间戳错配的无关句对」当真问题，输出"缺乏感情""太直白"
    # 这类无法执行的空话。真正能落地的规则必须先量化出来。
    n = len(rows)
    mt_all = [r["mt"] for r in rows]
    hu_all = [r["human"] for r in rows]

    def rate(lst, pred):
        return sum(1 for t in lst if pred(t)) / max(1, len(lst))

    def suffix_rate(lst, marks):
        return rate(lst, lambda t: t.rstrip().endswith(tuple(marks)))

    METRICS = [
        ("句末句号「。」", lambda l: suffix_rate(l, ["。"])),
        ("句末问号「？」", lambda l: suffix_rate(l, ["？", "?"])),
        ("句末感叹「！」", lambda l: suffix_rate(l, ["！", "!"])),
        ("句末省略号「…」", lambda l: suffix_rate(l, ["…"])),
        ("句末拖音「~～」", lambda l: suffix_rate(l, ["~", "～", "〜"])),
        ("含音符/特殊符号「♪♡」", lambda l: rate(l, lambda t: ("♪" in t or "♡" in t))),
        ("含语气词(呢吧哦啊啦嘛呀)", lambda l: rate(l, lambda t: any(x in t for x in "呢吧哦啊啦嘛呀"))),
        ("含叠词/复沓(蹭蹭摸摸撸撸)", lambda l: rate(l, lambda t: re.search(r"(.)\1", t) is not None)),
        ("平均句长(字)", lambda l: sum(len(t) for t in l) / max(1, len(l))),
    ]
    print("\n=== 量化对比：机器 vs 人工 ===")
    print("%-24s %8s %8s %8s" % ("指标", "机器", "人工", "差值"))
    gaps = []
    for name, fn in METRICS:
        mv, hv = fn(mt_all), fn(hu_all)
        mult = 1.0 if "句长" in name else 100.0     # 比例统一换算成百分比
        mv, hv = mv * mult, hv * mult
        print("%-24s %8.1f %8.1f %+8.1f" % (name, mv, hv, hv - mv))
        gaps.append((name, mv, hv))

    # 只挑「差距够大」的指标生成规则，避免被噪声带偏
    # 用户规范优先：句末句号由 user_rules.txt 拍板，不交给统计决定
    # （人工语料是旧「无句号」时代产物，统计上 ~2%，若照统计写「机器用得过多要收敛」
    #  就会把用户 2026-10-08 的「句末用句号」规范顶掉，必须跳过）。
    pol = A.user_period_policy()
    rules = []
    for name, mv, hv in gaps:
        if "平均句长" in name:
            continue
        if "句末句号" in name and pol == "period":
            continue
        if hv - mv >= 8:      # 人工明显多于机器
            key = {"句末句号": ("。", ""), "句末问号": ("？", ""), "句末感叹": ("！", ""),
                   "句末省略号": ("…", ""), "句末拖音": ("~～", ""),
                   "音符": ("♪♡", ""), "语气词": ("呢吧哦啊啦嘛呀", ""),
                   "叠词": ("叠词", "")}
            for k, _ in key.items():
                if k in name:
                    rules.append("%s：人工 %.0f%% vs 机器 %.0f%%，机器用得太少，"
                                 "在不破坏原意的前提下补上" % (name, hv, mv))
                    break
        elif mv - hv >= 8:
            rules.append("%s：机器 %.0f%% vs 人工 %.0f%%，机器用得过多，要收敛"
                         % (name, mv, hv))
    mv_len = sum(len(t) for t in mt_all) / max(1, len(mt_all))
    hv_len = sum(len(t) for t in hu_all) / max(1, len(hu_all))
    if hv_len - mv_len >= 1.5:
        rules.append("机器译文比人工短 %.1f 字（%.1f vs %.1f），容易把语气/喘息/迟疑"
                     "整个删掉，这些不要删，要译成口语" % (hv_len - mv_len, mv_len, hv_len))
    elif mv_len - hv_len >= 1.5:
        rules.append("机器译文比人工长 %.1f 字（%.1f vs %.1f），有添油加醋，要收紧"
                     % (mv_len - hv_len, mv_len, hv_len))

    txt = ["# 机译 vs 人工对照",
           "# 采样 %d 句，平均字面相似度 %.1f%%" % (n, avg * 100), "",
           "## 量化对比", "",
           "%-24s %8s %8s" % ("指标", "机器", "人工")]
    for name, mv, hv in gaps:
        unit = "" if "句长" in name else "%"
        txt.append("%-24s %7.1f%s %7.1f%s" % (name, mv, unit, hv, unit))
    if rules:
        txt += ["", "## 由此得出的可执行规则", ""] + ["- " + r for r in rules]
        print("\n=== 可执行规则 ===")
        for r in rules:
            print("  - " + r)

    txt += ["", "## 差异最大的样本（含时间戳错配噪声，仅供参考）", ""]
    for i, r in enumerate(top, 1):
        txt.append("%d. 日：%s" % (i, r["ja"]))
        txt.append("   机器：%s" % r["mt"])
        txt.append("   人工：%s" % r["human"])
    out_md = "\n".join(txt)

    if rules:
        P_HARD = os.path.join(a.style, "hard_rules.txt")
        head = ("\n- **以下由「机译 vs 人工」量化对照实测得出，是机器译文与人工稿"
                "差距最大的地方，润色时重点自查**：\n")
        lines = ["  " + r for r in rules]
        if os.path.exists(P_HARD):
            old = open(P_HARD, encoding="utf-8").read()
            marker = "量化对照实测得出"
            if marker in old:                      # 重跑时替换，不重复堆积
                old = old.split(head.strip()[:12])[0]
                old = re.split(r"\n- \*\*以下由「机译 vs 人工」", old)[0]
            open(P_HARD, "w", encoding="utf-8").write(old.rstrip() + "\n"
                                                      + head + "\n".join(lines) + "\n")
            print("\n已写入 hard_rules.txt")

    if not a.no_llm:
        body = "\n".join("%d. 日：%s\n   机器：%s\n   人工：%s"
                         % (i, r["ja"], r["mt"], r["human"])
                         for i, r in enumerate(top, 1))
        print("\n归纳通病中 ...", flush=True)
        rep = T.call_llm([{"role": "user", "content": ANALYZE % body[:9000]}],
                         temperature=0.3, max_tokens=1600)
        out_md += "\n\n## 机器译法的通病与改法\n\n" + rep.strip() + "\n"
        print("\n" + rep)

        # 结论要进 hard_rules，否则模型看不到，白分析
        P_HARD = os.path.join(a.style, "hard_rules.txt")
        head = ("\n- **以下来自「机译 vs 人工」对照实测，是机器最容易犯的错，"
                "润色时重点自查**：\n")
        lines = [("  " + l.strip()) for l in rep.splitlines()
                 if l.strip().startswith(("-", "*", "•"))][:14]
        if lines and os.path.exists(P_HARD):
            old = open(P_HARD, encoding="utf-8").read()
            if "来自「机译 vs 人工」对照实测" not in old:
                open(P_HARD, "w", encoding="utf-8").write(old.rstrip() + "\n"
                                                          + head + "\n".join(lines) + "\n")
                print("\n已追加进 hard_rules.txt")

    open(os.path.join(a.style, "mt_vs_human.txt"), "w", encoding="utf-8").write(out_md)
    print("\n已保存 -> %s" % os.path.join(a.style, "mt_vs_human.txt"))


if __name__ == "__main__":
    main()
