#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""导出人称风险案例，供外部 AI 逐条判定（只读）。

背景（2026-11-07）：person_audit 报 138 条「高风险」，但它是**软告警**，
不是「确认错误」——判据只是「原文前后 6s 无他者实体、译文却出现她/他」。
其中 132 条原文根本没有第二/第三人称词（省略主语），必须人工判断：
  · 真错：应译「我/你」（对听者说话、独白）
  · 合法：确实在说第三人称（前文提过别人、引用、复数他者）

导出格式按「部 × 时间」排，每条带日文原文与译文，便于外部 AI 逐条打标。

用法：
  python scripts/person_cases.py                      # 今天的 run
  python scripts/person_cases.py --since 2026-11-07T00:00:00
"""
import argparse
import glob
import io
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNS = os.path.join(ROOT, "runs")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    import datetime
    since = a.since or datetime.date.today().strftime("%Y-%m-%dT00:00:00")
    out = a.out or os.path.join(ROOT, "internal", "docs", "experiments",
                                "PERSON_CASES_%s.md" % since[:10].replace("-", ""))

    rows = []
    for p in glob.glob(os.path.join(RUNS, "*", "06_checks", "person_audit.json")):
        rid = p.split(os.sep)[-3]
        mp = os.path.join(RUNS, rid, "manifest.json")
        try:
            m = json.load(io.open(mp, encoding="utf-8"))
        except Exception:
            continue
        e6 = ((m.get("stages") or {}).get("06_checks") or {}).get("ended_at") or ""
        if e6 < since:
            continue
        try:
            d = json.load(io.open(p, encoding="utf-8"))
        except Exception:
            continue
        rj = m.get("project") or rid.split("_")[1]
        for it in (d.get("items") or []):
            if it.get("risk"):
                rows.append((rj, it))

    L = []
    L.append("# 人称风险案例（%s 之后的 run）" % since[:10])
    L.append("")
    L.append("共 **%d** 条。判据是「原文前后 6 秒无他者实体、译文却出现她/他」——" % len(rows))
    L.append("**这是候选清单，不是错误清单**。请逐条判断属于哪类：")
    L.append("")
    L.append("| 标记 | 含义 |")
    L.append("|---|---|")
    L.append("| **W** | 真错：应为「我/你」（对听者说话或独白）|")
    L.append("| **O** | 合法：确实在说第三人称（前文提过别人 / 引用 / 复数他者）|")
    L.append("| **S** | 合法：中文该省略主语，加「她/他」反而是多余 |")
    L.append("")
    L.append("---")
    L.append("")

    cur = None
    for i, (rj, it) in enumerate(rows, 1):
        if rj != cur:
            cur = rj
            L.append("## %s" % rj)
            L.append("")
            L.append("| # | 时间 | 日文原文 | 现译 | 判断 |")
            L.append("|---|---|---|---|---|")
        ja = str(it.get("ja", "")).replace("|", "\\|").replace("\n", " ")
        zh = str(it.get("zh", "")).replace("|", "\\|").replace("\n", " ")
        L.append("| %d | %.2f | %s | %s |  |" % (i, it.get("t", 0), ja[:60], zh[:40]))
    L.append("")

    os.makedirs(os.path.dirname(out), exist_ok=True)
    io.open(out, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("已生成 %s（%d 条）" % (out, len(rows)))


if __name__ == "__main__":
    main()
