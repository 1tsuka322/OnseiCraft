# -*- coding: utf-8 -*-
r"""对回归集打分：读人工标注（`review.csv`）+ 自动交付层指标，输出分层报告。

## 五层（对齐 GPT 建议的分层回归）
| 层 | 来源 | 说明 |
|---|---|---|
| ASR | 人工 `asr_ok` | 日文识别对不对（关键词/角色名/首分钟） |
| 忠实度 | 人工 `faith_ok`/`faith_err` | 否定、疑问、施受、数量、称谓、关键动作 |
| 连贯性 | 人工 `cohere_ok` | 主语连续性、指代、口吻、相邻 cue 承接 |
| 风格 | 人工 `style_ok`/`onomat_ok` | 标点/空格/句长/拟声处理 |
| 交付 | **自动** | 时间重叠、空 cue、日文残留、重复幻觉（不依赖标注） |

`Y` 记通过、`N` 记不通过、`?`/空 记「未判/跳过」，比率只按 (Y+N) 计。

## 用法
  python score_regset.py [--review FILE] [--out report.md]
  python score_regset.py --review A.csv --compare B.csv     # A/B 对照
"""
import os
import re
import sys
import csv
import glob
import json
import argparse

sys.stdout.reconfigure(encoding="utf-8")

import rootloc  # noqa: E402  仓库三层结构：work/ 里，成品/音频在上一级
OUT_ROOT = os.environ.get("KIKOERU_LRC_ROOT") or rootloc.PROD
DEF_REVIEW = os.path.join(os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "regset", "review.csv")
PUNCT = "。、，,.!！?？…♪~〜ー♡ \u3000「」『』（）()"
RE_JA = re.compile(r"[぀-ヿ]")


def norm(s):
    return "".join(c for c in (s or "") if c not in PUNCT)


def rate(rows, col):
    y = sum(1 for r in rows if (r.get(col) or "").strip().upper() == "Y")
    n = sum(1 for r in rows if (r.get(col) or "").strip().upper() == "N")
    tot = y + n
    return (y / tot * 100 if tot else None), y, n


def delivery_metrics():
    """自动交付层：扫全部成品 lrc。"""
    n_lines = n_empty = n_ja = n_dup = n_overlap = 0
    for f in sorted(glob.glob(os.path.join(OUT_ROOT, "RJ*", "*.lrc"))):
        rows = []
        for ln in open(f, encoding="utf-8"):
            m = re.match(r"\[(\d+):(\d+\.\d+)\](.*)", ln.strip())
            if m:
                rows.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
        for i, (t, zh) in enumerate(rows):
            n_lines += 1
            if not zh:
                n_empty += 1
            if RE_JA.search(zh):
                n_ja += 1
            if i + 1 < len(rows):
                a, b = norm(zh), norm(rows[i + 1][1])
                if len(b) >= 4 and b in a and len(a) > len(b):
                    n_dup += 1
                if rows[i + 1][0] <= t:
                    n_overlap += 1
    return {"lines": n_lines, "empty": n_empty, "ja_residue": n_ja,
            "suffix_dup": n_dup, "time_overlap": n_overlap}


def report(name, rows, out):
    out.append("## %s（n=%d）\n" % (name, len(rows)))
    out.append("| 层 | 指标 | 通过率 | Y / N |")
    out.append("|---|---|---|---|")
    for label, col in [("ASR", "asr_ok"), ("忠实度", "faith_ok"),
                       ("连贯性", "cohere_ok"), ("风格", "style_ok"),
                       ("拟声", "onomat_ok")]:
        r, y, n = rate(rows, col)
        out.append("| %s | %s | %s | %d / %d |" % (
            label, col, ("%.1f%%" % r) if r is not None else "—", y, n))
    out.append("")
    # 忠实度错误类型
    from collections import Counter
    err = Counter()
    for r in rows:
        e = (r.get("faith_err") or "").strip()
        if e:
            for k in re.split(r"[;,、/|]", e):
                if k.strip():
                    err[k.strip()] += 1
    if err:
        out.append("**忠实度错误类型分布**：" + "、".join("%s×%d" % (k, v) for k, v in err.most_common()))
        out.append("")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", default=DEF_REVIEW)
    ap.add_argument("--compare", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    out = ["# 回归集评分报告\n"]
    if os.path.exists(a.review):
        rows = list(csv.DictReader(open(a.review, encoding="utf-8-sig")))
        report("主集（%s）" % os.path.basename(a.review), rows, out)
    else:
        out.append("> 未找到 `%s`（还没标注？）。仅输出交付层自动指标。\n" % a.review)

    if a.compare and os.path.exists(a.compare):
        rows2 = list(csv.DictReader(open(a.compare, encoding="utf-8-sig")))
        report("对照（%s）" % os.path.basename(a.compare), rows2, out)

    dm = delivery_metrics()
    out.append("## 交付层（自动，全部 15 部成品）\n")
    out.append("| 指标 | 值 |")
    out.append("|---|---|")
    out.append("| 总行数 | %d |" % dm["lines"])
    out.append("| 空 cue | %d |" % dm["empty"])
    out.append("| 日文残留 | %d |" % dm["ja_residue"])
    out.append("| 后缀重复（后行⊂前行） | %d |" % dm["suffix_dup"])
    out.append("| 时间轴非递增 | %d |" % dm["time_overlap"])

    text = "\n".join(out)
    print(text)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text + "\n")
        print("\n已写入：%s" % a.out)


if __name__ == "__main__":
    main()
