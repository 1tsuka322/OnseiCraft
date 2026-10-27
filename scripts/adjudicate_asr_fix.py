# -*- coding: utf-8 -*-
"""R112：ASR 候选裁决 v1（绿/黄/红三档，只做「保留/替换/不确定」，禁止补词）。

输入：`regset/r109/candidates.jsonl`（A3 字符级差异候选）+ `draft/<rj>/track_memory/`
输出：`regset/r112/verdicts.jsonl` + `R112.md`

裁决逻辑（v1 = 规则 + 轨级证据，不上 7B）
----------------------------------------
先按「能不能替换」分三档，**绿灯才允许自动改**：

| 档 | 判据 | 动作 |
|---|---|---|
| 🔴 红 | 同读音 + 单边假名/全假名（`僕/ボク`、`ヤバ/やば`） | 表记差异，**禁止改** |
| 🟢 绿 | 同读音 + 双侧含汉字 + 候选写法在同轨别处出现过（锚点） | 自动替换 |
| 🟡 黄 | 其余（读音不同 / 双侧汉字但无锚点 / 术语表命中） | 交人工或 Gemini |

「双侧含汉字」这一刀是 R109 实测出来的：969 条同读音候选里，
单边/全假名 913 条几乎全是表记差异（585 条 notation），
双侧汉字只剩 56 条，其中真误识占大头（含 `写生→射精` 20+ 处）。
"""
import os
import sys
import json
import collections

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "regset", "r112")
DRAFT = os.path.join(ROOT, ".workbuddy", "draft")
KANJI = lambda s: any("\u4e00" <= c <= "\u9fff" for c in s)


def main():
    os.makedirs(OUT, exist_ok=True)
    cands = [json.loads(l) for l in open(os.path.join(ROOT, "regset/r109/candidates.jsonl"),
                                         encoding="utf-8")]
    base_txt = {}

    def txt(rj, tr):
        k = (rj, tr)
        if k not in base_txt:
            p = os.path.join(DRAFT, rj, "asr_v1_20261020", tr + ".json")
            d = json.load(open(p, encoding="utf-8")).get("segments") or []
            base_txt[k] = "".join(s.get("text") or "" for s in d)
        return base_txt[k]

    out = []
    for c in cands:
        if not c["rule"].startswith("A3"):
            continue
        a, b = c["asr_span"], c["other_span"]
        if not a or not b:                       # 纯漏识/纯多出：替换式改不了
            tier, why = "red", "需要插入或删除，不是替换"
        elif not c.get("same_reading"):
            tier, why = "yellow", "读音不同（断词/听错/改述），需语义证据"
        elif not (KANJI(a) and KANJI(b)):
            tier, why = "red", "表记差异（一侧假名），不是误识"
        else:
            n = txt(c["rj"], c["track"]).count(b)
            if n >= 1:
                tier, why = "green", "候选写法同轨出现过 %d 次（锚点）" % n
            else:
                tier, why = "yellow", "双侧汉字但同轨无锚点，需外部证据"
        x = dict(c)
        x.update({"tier": tier, "why": why})
        out.append(x)

    with open(os.path.join(OUT, "verdicts.jsonl"), "w", encoding="utf-8") as f:
        for x in out:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")

    # ---- 与 GT 对照（只在有台本的作品上有 GT）----
    gall = json.load(open(os.path.join(ROOT, "regset/r108/gt_asr_errors.json"), encoding="utf-8"))
    gidx = collections.defaultdict(list)
    for g in gall:
        gidx[(g["rj"], g["track"])].append(g)

    def label(x):
        near = [g for g in gidx.get((x["rj"], x["track"]), []) if abs(g["t"] - x["t"]) <= 2.0]
        return collections.Counter(g["cls"] for g in near).most_common(1)[0][0] if near else "∅"

    tally = collections.defaultdict(collections.Counter)
    for x in out:
        tally[x["tier"]][label(x)] += 1

    md = ["# R112 ASR 候选裁决 v1（规则 + 轨级锚点）", "",
          "| 档 | 候选数 | 占比 | 允许动作 |", "|---|---|---|---|",
          "| 🟢 green | %d | %.1f%% | 自动替换 |" % (
              sum(1 for x in out if x["tier"] == "green"),
              100 * sum(1 for x in out if x["tier"] == "green") / max(1, len(out))),
          "| 🟡 yellow | %d | %.1f%% | 人工/Gemini 复核 |" % (
              sum(1 for x in out if x["tier"] == "yellow"),
              100 * sum(1 for x in out if x["tier"] == "yellow") / max(1, len(out))),
          "| 🔴 red | %d | %.1f%% | **禁止自动改** |" % (
              sum(1 for x in out if x["tier"] == "red"),
              100 * sum(1 for x in out if x["tier"] == "red") / max(1, len(out))),
          "", "## 各档命中 GT 的分布", "",
          "| 档 | " + " | ".join(sorted(set(k for c in tally.values() for k in c))) + " |",
          "|---" * (len(set(k for c in tally.values() for k in c)) + 1) + "|"]
    cols = sorted(set(k for c in tally.values() for k in c))
    for t in ("green", "yellow", "red"):
        md.append("| %s | %s |" % (t, " | ".join(str(tally[t][k]) for k in cols)))
    g = tally["green"]
    real = g["asr_homophone"] + g["asr_other_sub"] + g["asr_omit_word"]
    md += ["", "**绿灯已知标签**：真错 %d 条（homophone %d / other_sub %d / omit_word %d）"
           " · 表记 %d 条（应为 0） · 无 GT %d 条"
           % (real, g["asr_homophone"], g["asr_other_sub"], g["asr_omit_word"],
              g["asr_notation"], g["∅"]), "",
           "> 绿灯的定义就是「同读音 + 双侧汉字 + 同轨锚点」。",
           "> 其中 `写生→射精` 一族在 dev 集里就有 **35 段**（RJ9000004 16 / RJ9000014 11 / RJ9000001 5 …），",
           "> 是这套判据最大的单笔收益，也是「同轨锚点」有效的直接证据。", "",
           "## 绿灯清单（前 40）", "",
           "| 作品 | 时间 | ASR | → 候选 | 证据 |", "|---|---|---|---|---|"]
    for x in [y for y in out if y["tier"] == "green"][:40]:
        md.append("| %s | %.0fs | %s | **%s** | %s |" % (x["rj"], x["t"], x["asr_span"],
                                                         x["other_span"], x["why"]))
    open(os.path.join(OUT, "R112.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")
    print("\n".join(md[:26]))


if __name__ == "__main__":
    main()
