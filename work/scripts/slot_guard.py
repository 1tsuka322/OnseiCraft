# -*- coding: utf-8 -*-
"""R113：语义槽守卫（slot guard）——防止润色层把初译的语义槽洗掉。

来自 ChatGPT 第四轮 Q4：
> 第二段输入必须带第一段的「不可变语义槽」：主体、动作、肯否、因果、术语；
> 程序后验比对槽位：发生变化则回退第一段或标黄。

本工具不改进模型，只做**后验校验 + 回退**（确定性、可离线在现有成品上跑）。

槽位定义（只取能自动判的五个）
------------------------------
| 槽 | ja 侧判据 | zh 侧判据 |
|---|---|---|
| 肯否 | ない/ません/ぬ/なく/な/禁止 | 不/没/别/勿/未/无/非 |
| 主体 | 私/僕/俺/あたし/わたし ↔ あなた/君/お前/あんた | 我 ↔ 你/您 |
| 数量 | 数字（含汉字数字） | 数字（含汉字数字） |
| 因果 | から/ので/だから/ため/せいで | 因为/所以/由于/因此 |
| 术语 | glossary 的 ja 命中 | 对应 zh 是否出现 |

用法
----
  python slot_guard.py <asr_dir> <init_dir> <polished_dir> \
      [--glossary ...] [--report out.json] [--revert-to reverted_dir]

输出：每个 track 的槽位变化清单 + 汇总（变化率、按槽分布、压缩率）。
`--revert-to`：把发生变化的行**回退成初译**，写一个新目录（不动原成品）。
"""
import os
import re
import sys
import json
import glob
import argparse

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NEG_JA = re.compile(r"(ない|ません|なかった|なく|ぬ|無い|じゃね|じゃな|やめ|禁止|ダメ|だめ)")
NEG_ZH = re.compile(r"(不|没|别|勿|莫|未|无|非|禁止|不要|不能)")
SUBJ_1_JA = re.compile(r"(私|僕|俺|あたし|わたし|わたくし)")
SUBJ_2_JA = re.compile(r"(あなた|君|お前|あんた|貴様)")
SUBJ_1_ZH = re.compile(r"我")
SUBJ_2_ZH = re.compile(r"(你|您)")
CAUSAL_JA = re.compile(r"(だから|から|ので|ため|せいで|おかげで)")
CAUSAL_ZH = re.compile(r"(因为|所以|由于|因此|结果)")
NUM = re.compile(r"(\d+|[一二三四五六七八九十百千万]+)")

CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
      "十": 10, "百": 100, "千": 1000, "万": 10000}


def cn2int(s):
    """汉字数字 → int（够用即可，只处理 ≤ 万 的常见写法）。"""
    if s.isdigit():
        return int(s)
    total, unit = 0, 1
    for ch in reversed(s):
        if ch in CN:
            v = CN[ch]
            if v >= 10:
                unit = v
                total = total or 0
                if total == 0:
                    total = v
                    unit = 1
            else:
                total += v * unit
    return total or None



def lrc_path(d, tr):
    """轨名 → lrc 路径。成品/初译常带音频扩展名（`<track>.mp3.lrc`），别死拼 `<track>.lrc`。"""
    p = os.path.join(d, tr + ".lrc")
    if os.path.exists(p):
        return p
    cands = glob.glob(os.path.join(d, tr + ".*.lrc"))
    return cands[0] if cands else p


def read_lrc(path):
    out = []
    for ln in open(path, encoding="utf-8-sig"):
        m = re.match(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$", ln.rstrip("\n"))
        if m:
            t = int(m.group(1)) * 60 + float(m.group(2))
            out.append((round(t, 2), m.group(3).strip()))
    return out


def load_asr(asr_dir, track):
    p = os.path.join(asr_dir, track + ".json")
    if not os.path.exists(p):
        for cand in (track.replace(".mp3", "") + ".json",):
            q = os.path.join(asr_dir, cand)
            if os.path.exists(q):
                p = q
                break
    if not os.path.exists(p):
        # 轨名带音频扩展名（`<track>.mp3.json`）而列表里的 tr 已经剥掉了 → glob 兜底
        cands = glob.glob(os.path.join(asr_dir, track + ".*.json"))
        p = cands[0] if cands else None
    if not p:
        return []
    # 2026-10-26：有的 asr_fix/*.json 是**裸列表**（不是 {"segments": [...]}），
    # 直接 .get() 会 AttributeError —— 实测 4 部接管作品的 slot_guard 因此整体失败。
    d = json.load(open(p, encoding="utf-8"))
    segs = d if isinstance(d, list) else (d.get("segments") or [])
    return [(round(float(s["start"]), 2), s.get("text") or "") for s in segs
            if isinstance(s, dict) and "start" in s]


def ja_at(asr, t, tol=0.6):
    best, bt = "", 9e9
    for st, tx in asr:
        d = abs(st - t)
        if d < bt:
            best, bt = tx, d
    return best if bt <= tol else ""


def slots(ja, zh, gloss):
    s = {"zh": zh}
    # 肯否：只比较「ja 有否定」时 zh 是否也否定；反向（中文多否定）单独记 add_neg
    s["neg_ja"] = bool(NEG_JA.search(ja))
    s["neg_zh"] = len(NEG_ZH.findall(zh))
    # 主体
    s["s1_ja"] = bool(SUBJ_1_JA.search(ja))
    s["s2_ja"] = bool(SUBJ_2_JA.search(ja))
    s["s1_zh"] = bool(SUBJ_1_ZH.search(zh))
    s["s2_zh"] = bool(SUBJ_2_ZH.search(zh))
    # 数量
    s["num_ja"] = sorted({cn2int(x) for x in NUM.findall(ja)} - {None})
    s["num_zh"] = sorted({cn2int(x) for x in NUM.findall(zh)} - {None})
    # 因果
    s["caus_ja"] = bool(CAUSAL_JA.search(ja))
    s["caus_zh"] = bool(CAUSAL_ZH.search(zh))
    # 术语
    s["terms"] = [g["zh"] for g in gloss if g["ja"] in ja and g["zh"]]
    return s


def diff_slots(a, b, len_ratio=1.0, neighbors=""):
    """返回被润色改坏的槽（只报「应该保持却没保持」的方向）。

    ⚠️ 实测校准（R113 冒烟 291 cue）：
    - `subj1_dropped`（丢「我」）**几乎全是误报** —— 中文主语省略是常态
      （`我不想要勉强你。`→`不想勉强你` 更好）。**不再作为缺陷**，只计数。
    - `subj2_dropped`（丢「你」）在**行被大幅缩短**时才是真缺陷（截断），
      否则也可能是省略 → 只在 len_ratio ≥ 0.8 时单独报，缩短时归 `truncated`。
    - 真正的硬缺陷是 **截断**（len_ratio < 0.65 且初译够长）和 否定/因果/数量/术语 丢失。
    """
    d = []
    if len_ratio < 0.65 and len(a.get("zh", "")) >= 8:
        # 先看「丢掉的内容是不是挪到邻行去了」——译文提前/滞后一行是本工程的老现象
        # （cue 级翻译 → 显示行拆分），不查邻行会把「移位」全算成「截断」（实测虚报 ~50%）。
        tail = a["zh"][-6:]
        if tail and tail not in b.get("zh", "") and tail not in (neighbors or ""):
            d.append("truncated")
        else:
            d.append("shifted")
    if a["neg_ja"] and a["neg_zh"] > 0 and b["neg_zh"] == 0:
        d.append("neg_dropped")                  # 初译有否定，润色后没了
    if a["neg_ja"] and a["neg_zh"] == 0 and b["neg_zh"] > 0:
        d.append("neg_added")                    # 初译没否定，润色加了
    if a["s2_zh"] and not b["s2_zh"] and len_ratio >= 0.8:
        d.append("subj2_dropped")
    if a["num_ja"] and a["num_zh"] and set(a["num_zh"]) - set(b["num_zh"]):
        d.append("num_changed")
    if a["caus_ja"] and a["caus_zh"] and not b["caus_zh"] and len_ratio >= 0.65:
        d.append("causal_dropped")
    for t in a["terms"]:
        if t not in b["terms"]:
            d.append("term_lost:%s" % t)
    return d


def scan_dir(asr_dir, init_dir, polished_dir, gloss=None):
    """扫描三个目录，返回可疑行（供 run_all 复用；main 只是它的 CLI 外壳）。"""
    if gloss is None:
        gloss = []
        gp = os.path.join(ROOT, "data/style/glossary.json")
        if os.path.exists(gp):
            g = json.load(open(gp, encoding="utf-8"))
            for k, v in g.items():
                if isinstance(v, dict) and v.get("zh"):
                    gloss.append({"ja": k, "zh": v["zh"]})
    tracks = sorted(os.path.splitext(os.path.basename(p))[0]
                    for p in glob.glob(os.path.join(polished_dir, "*.lrc")))
    tracks = [re.sub(r"\.(mp3|wav|flac|m4a)$", "", t) for t in tracks]
    rows = []
    for tr in tracks:
        init = dict(read_lrc(lrc_path(init_dir, tr)))
        pol_lines = read_lrc(lrc_path(polished_dir, tr))
        asr = load_asr(asr_dir, tr)
        bounds = [(t, asr[i + 1][0] if i + 1 < len(asr) else t + 99)
                  for i, (t, _) in enumerate(asr)]
        pol = []
        for t0, t1 in bounds:
            seg = [x for tt, x in pol_lines if t0 - 0.05 <= tt < t1 - 0.05]
            if seg:
                pol.append((t0, " ".join(seg)))
        for t, zh_p in pol:
            zh_i = init.get(t)
            if zh_i is None:
                cand = [(abs(k - t), v) for k, v in init.items()]
                if cand and min(cand)[0] <= 1.0:
                    zh_i = min(cand)[1]
            if not zh_i:
                continue
            ja = ja_at(asr, t)
            if not ja:
                continue
            ratio = len(zh_p) / max(1, len(zh_i))
            _i = next((k for k, (tt, _) in enumerate(pol) if abs(tt - t) < 0.05), -1)
            nb = " ".join(pol[k][1] for k in (_i - 1, _i + 1) if 0 <= k < len(pol))
            d = diff_slots(slots(ja, zh_i, gloss), slots(ja, zh_p, gloss), ratio, nb)
            if d:
                rows.append({"track": tr, "t": t, "ja": ja[:60], "init": zh_i,
                             "polished": zh_p, "ratio": round(ratio, 2), "changed": d})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("asr_dir")
    ap.add_argument("init_dir")
    ap.add_argument("polished_dir")
    ap.add_argument("--glossary", default=os.path.join(ROOT, "data/style/glossary.json"))
    ap.add_argument("--report", default=None)
    ap.add_argument("--revert-to", default=None, help="把槽位变化的行回退成初译，写这个目录")
    a = ap.parse_args()

    gloss = []
    if os.path.exists(a.glossary):
        g = json.load(open(a.glossary, encoding="utf-8"))
        for k, v in g.items():
            if isinstance(v, dict) and v.get("zh"):
                gloss.append({"ja": k, "zh": v["zh"]})

    tracks = sorted(os.path.splitext(os.path.basename(p))[0]
                    for p in glob.glob(os.path.join(a.polished_dir, "*.lrc")))
    tracks = [re.sub(r"\.(mp3|wav|flac|m4a)$", "", t) for t in tracks]
    rows, tot = [], {"cue": 0, "changed": 0, "init_chars": 0, "pol_chars": 0}
    by_kind = {}
    for tr in tracks:
        init = dict(read_lrc(lrc_path(a.init_dir, tr)))
        pol_lines = read_lrc(lrc_path(a.polished_dir, tr))
        asr = load_asr(a.asr_dir, tr)
        # ⚠️ 关键口径：polish 写的是**显示行**（一个 cue 可能拆成 2–3 行），
        # 初译写的是**cue 行**。直接按时间戳一对一比会把「拆行」误判成「截断」
        # （第一版就这样虚报 66 条 truncated）。必须把显示行**按 cue 窗口聚合**再比。
        bounds = [(t, asr[i + 1][0] if i + 1 < len(asr) else t + 99)
                  for i, (t, _) in enumerate(asr)]
        pol = []
        for t0, t1 in bounds:
            seg = [x for tt, x in pol_lines if t0 - 0.05 <= tt < t1 - 0.05]
            if seg:
                pol.append((t0, " ".join(seg)))
        rev = []
        for t, zh_p in pol:
            zh_i = init.get(t)
            if zh_i is None:
                # 时间戳可能被 cleanup 微调 → 找最近的初译行
                cand = [(abs(k - t), v) for k, v in init.items()]
                if cand and min(cand)[0] <= 1.0:
                    zh_i = min(cand)[1]
            if not zh_i:
                continue
            ja = ja_at(asr, t)
            if not ja:
                continue
            tot["cue"] += 1
            tot["init_chars"] += len(zh_i)
            tot["pol_chars"] += len(zh_p)
            ratio = len(zh_p) / max(1, len(zh_i))
            _i = next((k for k, (tt, _) in enumerate(pol) if abs(tt - t) < 0.05), -1)
            nb = " ".join(pol[k][1] for k in (_i - 1, _i + 1) if 0 <= k < len(pol))
            d = diff_slots(slots(ja, zh_i, gloss), slots(ja, zh_p, gloss), ratio, nb)
            if d:
                tot["changed"] += 1
                for k in d:
                    by_kind[k.split(":")[0]] = by_kind.get(k.split(":")[0], 0) + 1
                rows.append({"track": tr, "t": t, "ja": ja[:50], "init": zh_i,
                             "polished": zh_p, "ratio": round(ratio, 2), "changed": d})
                rev.append((t, zh_i))
        if a.revert_to:
            os.makedirs(a.revert_to, exist_ok=True)
            revmap = dict(rev)
            lines = []
            for t, zh in pol:
                lines.append("[%02d:%05.2f]%s" % (int(t // 60), t % 60, revmap.get(t, zh)))
            open(os.path.join(a.revert_to, tr + ".lrc"), "w", encoding="utf-8").write("\n".join(lines) + "\n")

    comp = 1 - tot["pol_chars"] / max(1, tot["init_chars"])
    md = ["# R113 语义槽守卫报告", "",
          "| 指标 | 值 |", "|---|---|",
          "| 可比 cue | %d |" % tot["cue"],
          "| 槽位发生变化 | **%d（%.2f%%）** |" % (tot["changed"], 100 * tot["changed"] / max(1, tot["cue"])),
          "| 润色压缩率 | %.2f%% |" % (100 * comp), "",
          "| 变化类型 | n |", "|---|---|"]
    for k, v in sorted(by_kind.items(), key=lambda x: -x[1]):
        md.append("| %s | %d |" % (k, v))
    md += ["", "## 明细（前 30）", "", "| 轨 | 时间 | 日文 | 初译 | 润色后 | 变化 |", "|---|---|---|---|---|---|"]
    for r in rows[:30]:
        md.append("| %s | %.1f | %s | %s | %s | %s |" % (
            r["track"][:14], r["t"], r["ja"][:26], r["init"][:22], r["polished"][:22],
            ",".join(r["changed"])))
    print("\n".join(md))
    if a.report:
        json.dump({"summary": tot, "by_kind": by_kind, "rows": rows},
                  open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("\n→ %s" % a.report)


if __name__ == "__main__":
    main()
