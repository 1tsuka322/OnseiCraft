# -*- coding: utf-8 -*-
"""风险分层（GPT 第五轮 Q1-e）：把 cue 分成可单独报告的子集。

为什么要分层
------------
GPT 第五轮：建高风险挑战集并分层报告——术语、否定/因果、长句、说话人切换、记忆命中各单列，
**同时保留随机普通样本**，避免只在「容易改善」的子集报喜；总体率按各层在全库的真实占比加权。

分层口径（全部确定性规则，可复现）
------------------------------
| 层 | 判据 |
|---|---|
| `term` | 命中术语表（glossary） |
| `neg` | 日文含否定（ない/ません/なく/ぬ/無い/禁止/ダメ） |
| `causal` | 含 から/ので/ため/せいで |
| `long` | 日文 ≥25 字或时长 ≥5s |
| `person` | 人称词出现（私/僕/俺/あなた/君/お前…）——说话人切换的代理（原始 asr 无 spk 标签） |
| `mem` | 该句内容出现在本轨 track_memory 的术语/锚点/实体里（记忆可能起作用的子集） |
| `errata` | 命中已知错字表 |
| `normal` | 以上都不中 |

一条 cue 可属多层；报告时按层给数，并单独给 `normal` 的随机样本。
"""
import os
import re
import sys
import json
import glob

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NEG = re.compile(r"(ない|ません|なかった|なく|ぬ|無い|じゃね|じゃな|禁止|ダメ|だめ)")
CAUSAL = re.compile(r"(だから|から|ので|ため|せいで|おかげで)")
PERSON = re.compile(r"(私|僕|俺|あたし|わたし|わたくし|あなた|君|お前|あんた|貴様|姉さん|お姉|兄|お兄)")


def load_glossary():
    p = os.path.join(ROOT, "data/style/glossary.json")
    out = []
    if os.path.exists(p):
        g = json.load(open(p, encoding="utf-8"))
        for k, v in g.items():
            if isinstance(v, dict) and v.get("zh"):
                out.append(k)
    return out


def load_errata():
    p = os.path.join(ROOT, "data/style/asr_errata.json")
    return [e["ja"] for e in json.load(open(p, encoding="utf-8"))["entries"]] \
        if os.path.exists(p) else []


def mem_terms(draft_root, track):
    """本轨 track_memory 里的 ja 词表（术语 + 锚点 + 实体）。"""
    p = os.path.join(draft_root, "track_memory", track + ".json")
    if not os.path.exists(p):
        return []
    m = json.load(open(p, encoding="utf-8"))
    ws = [t["ja"] for t in m.get("terms") or []]
    ws += [a["w"] for a in m.get("anchors") or []]
    ws += [e["w"] for e in m.get("entities") or []]
    return [w for w in ws if w and len(w) >= 2]


def strata_of(ja, dur, gloss, errata, memw):
    s = []
    if any(g in ja for g in gloss):
        s.append("term")
    if NEG.search(ja):
        s.append("neg")
    if CAUSAL.search(ja):
        s.append("causal")
    if len(ja) >= 25 or dur >= 5.0:
        s.append("long")
    if PERSON.search(ja):
        s.append("person")
    if any(w in ja for w in memw):
        s.append("mem")
    if any(e in ja for e in errata):
        s.append("errata")
    return s or ["normal"]


def scan(asr_dir, draft_root=None):
    gloss, errata = load_glossary(), load_errata()
    rows = []
    for p in sorted(glob.glob(os.path.join(asr_dir, "*.json"))):
        track = os.path.splitext(os.path.basename(p))[0]
        memw = mem_terms(draft_root, track) if draft_root else []
        d = json.load(open(p, encoding="utf-8"))
        for s in d.get("segments") or []:
            ja = s.get("text") or ""
            rows.append({"track": track, "t": round(float(s["start"]), 1),
                         "ja": ja, "dur": round(float(s["end"]) - float(s["start"]), 1),
                         "strata": strata_of(ja, float(s["end"]) - float(s["start"]),
                                             gloss, errata, memw)})
    return rows


def main():
    import collections
    asr_dir = sys.argv[1]
    draft_root = sys.argv[2] if len(sys.argv) > 2 else None
    rows = scan(asr_dir, draft_root)
    c = collections.Counter(s for r in rows for s in r["strata"])
    n = len(rows)
    print("cue 总数 %d" % n)
    for k, v in c.most_common():
        print("  %-8s %5d（%.1f%%）" % (k, v, 100 * v / max(1, n)))
    out = sys.argv[3] if len(sys.argv) > 3 else None
    if out:
        json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("→ %s" % out)


if __name__ == "__main__":
    main()
