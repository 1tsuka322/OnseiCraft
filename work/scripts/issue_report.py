#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""问题池：把自验证暴露的问题按「归谁解决」汇总成一份 md。

为什么需要它（2026-11-07 用户要求）：
  自己一直改会有盲区。每次全库重跑/自验证后，应当把问题**分类整理**，
  语料类的交外部 AI（Gemini）、代码类的交外部 AI（GPT）、产品决策类的交用户。
  否则结论散在几十条日志里，下次重复踩。

⚠️ 本脚本**只读**：不写任何 run 产物，不进任何阶段的 code 闭包。
   用法：
     python scripts/issue_report.py                      # 每部取最新 run
     python scripts/issue_report.py --out xxx.md
     python scripts/issue_report.py --rj RJ01654115 ...  # 只看指定部

输出三段：
  A 语料/译法  —— 角色名、拟声、人称、真错译（交 Gemini）
  B 代码/判据  —— 门禁误报、指纹、缓存（交 GPT）
  C 需拍板    —— 白名单增删、切分阈值、产品交互（交用户）
"""
import argparse
import glob
import io
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNS = os.path.join(ROOT, "runs")

STAGES6 = ("01_asr", "02_asr_fix", "03_translate", "04_polish", "05_term_lock", "06_checks")

# 门禁分类（与 stages.GATE_KIND 同口径；这里复制一份是为了脚本能独立跑）
KIND = {
    "empty": "contract", "errata_audit": "contract", "test_regressions": "contract",
    "check": "output", "lexical": "output",
    "scan": "audit", "overlay": "audit", "trunc": "audit", "slot_guard": "audit",
    "locks": "audit", "semantic": "audit", "person_audit": "audit",
}


def latest_runs():
    """每个 project 取 created_at 最新的 run。"""
    best = {}
    for p in glob.glob(os.path.join(RUNS, "*_production")):
        mp = os.path.join(p, "manifest.json")
        if not os.path.isfile(mp):
            continue
        try:
            m = json.load(io.open(mp, encoding="utf-8"))
        except Exception:
            continue
        rj = m.get("project") or os.path.basename(p).split("_")[1]
        key = m.get("created_at") or ""
        if rj not in best or key > best[rj][0]:
            best[rj] = (key, os.path.basename(p), m)
    return best


def parse_lexical_md(path):
    """解析 LEXICAL_AUDIT.md 的明细表行。返回 [dict]。"""
    out = []
    if not os.path.isfile(path):
        return out
    for line in io.open(path, encoding="utf-8"):
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 5:
            continue
        if not re.match(r"^\d+(\.\d+)?$", cells[0]):
            continue
        out.append({
            "t": float(cells[0]),
            "track": cells[1],
            "kind": cells[2],
            "ja": cells[3],
            "zh": cells[4],
            "extra": cells[5] if len(cells) > 5 else "",
        })
    return out


def collect(rj_filter=None, since=""):
    data = {}
    for rj, (created, rid, m) in sorted(latest_runs().items()):
        if rj_filter and rj not in rj_filter:
            continue
        d6 = os.path.join(RUNS, rid, "06_checks")
        gates = {}
        gp = os.path.join(d6, "gates.json")
        if os.path.isfile(gp):
            try:
                gates = json.load(io.open(gp, encoding="utf-8"))
            except Exception:
                gates = {}
        lex = parse_lexical_md(os.path.join(d6, "LEXICAL_AUDIT.md"))
        per = []
        pp = os.path.join(d6, "person_audit.json")
        if os.path.isfile(pp):
            try:
                pj = json.load(io.open(pp, encoding="utf-8"))
                per = [x for x in (pj.get("items") or []) if x.get("risk")]
            except Exception:
                per = []
        reps = (m.get("repairs") or [])
        st, note = freshness(m.get("stages") or {}, since)
        data[rj] = {
            "run": rid, "created": created, "gates": gates,
            "lexical": lex, "person": per, "repairs": reps,
            "has06": bool(gates), "fresh": st, "fresh_note": note,
        }
    return data


def freshness(stages, since=""):
    """06 的结果是不是**当前产物**的。

    ⚠️ 2026-11-07 踩到：批次跑到一半时跑本脚本，17 部全显示 ✅ —— 但其中 9 部的
    06 是前一天留下的旧结果（`01_asr` 甚至还是 running）。gates.json 存在且 ok，
    不代表它是当前产物的 gates。这正是「假绿」的又一种形状，必须在汇总层就标出来。

    两道判据：
      1. 有阶段 status=running → incomplete
      2. 06.ended_at 早于 `since`（本次批次起点）→ stale
         只比 06 vs 05 不够：旧 run 里 06 本来也晚于 05，照样「顺序正常」。
    返回 (状态, 说明)：fresh / stale / incomplete
    """
    s06 = stages.get("06_checks") or {}
    running = [n for n in STAGES6
               if (stages.get(n) or {}).get("status") == "running"]
    if running:
        return "incomplete", "未完成：%s" % "/".join(x.split("_")[0] for x in running)
    e06 = s06.get("ended_at") or ""
    if not e06:
        return "incomplete", "06 无 ended_at"
    if since and e06 < since:
        return "stale", "06 是 %s 的旧结果" % e06[:10]
    return "fresh", e06


def gate_line(gates):
    """返回 (可交付?, 红项列表[(name,kind,summary)])"""
    bad = []
    for k, v in (gates or {}).items():
        if not v.get("ok"):
            bad.append((k, KIND.get(k, v.get("kind") or "?"), v.get("summary")))
    deliver_ok = all(kd != "contract" and kd != "output" for _, kd, _ in bad)
    return deliver_ok, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--rj", nargs="*", default=None)
    ap.add_argument("--date", default="")
    ap.add_argument("--since", default="",
                    help="本次批次起点（ISO，如 2026-11-07T00:00）；早于它的 06 判陈旧")
    a = ap.parse_args()

    import datetime
    day = a.date or datetime.date.today().strftime("%Y%m%d")
    out = a.out or os.path.join(ROOT, "internal", "docs", "experiments", "ISSUE_POOL_%s.md" % day)

    since = a.since
    if not since:
        since = datetime.date.today().strftime("%Y-%m-%dT00:00:00")
    data = collect(a.rj, since)
    L = []
    L.append("# 问题池 %s" % day)
    L.append("")
    L.append("> 自验证暴露的问题，按**归谁解决**分组。")
    L.append("> A 语料/译法 → 交 Gemini；B 代码/判据 → 交 GPT；C 需拍板 → 交用户。")
    L.append("> 由 `scripts/issue_report.py` 生成（只读），分类说明为人工补写。")
    L.append("")

    # ---- 汇总 ----
    L.append("## 0. 汇总")
    L.append("")
    L.append("| 部 | 结果新鲜度 | 可交付 | 门禁红项（kind） | lexical | person风险 | repairs |")
    L.append("|---|---|---|---|---|---|---|")
    n_ok = n_fresh = 0
    for rj, d in data.items():
        if not d["has06"]:
            L.append("| %s | — | — | 06 未跑完 | — | — | — |" % rj)
            continue
        st = d["fresh"]
        ok, bad = gate_line(d["gates"])
        fresh = (st == "fresh")
        if fresh:
            n_fresh += 1
            if ok:
                n_ok += 1
        mark = {"fresh": "✅", "stale": "⚠️旧", "incomplete": "⚠️未完成"}[st]
        if st != "fresh":
            mark += " %s" % d["fresh_note"][:18]
        bads = "、".join("%s(%s)" % (k, kd) for k, kd, _ in bad) or "—"
        lx = d["gates"].get("lexical", {}).get("summary") or "—"
        L.append("| %s | %s | %s | %s | %s | %d | %d |" % (
            rj, mark, ("✅" if ok else "❌") if fresh else "（不计）",
            bads, lx, len(d["person"]), len(d["repairs"])))
    L.append("")
    L.append("**新鲜结果：可交付 %d / %d**（陈旧/未完成的部不计入，避免旧 gates 冒充新结论）"
             % (n_ok, n_fresh))
    L.append("")

    # ---- A 语料/译法 ----
    L.append("## A. 语料 / 译法（交 Gemini）")
    L.append("")
    L.append("> 判据：译文本身有争议（人名、拟声、人称、性词汇、语气），换个译法就能好。")
    L.append("")
    a_items = []
    for rj, d in data.items():
        for it in d["lexical"]:
            if it["kind"] in ("translit", "content_drop", "ellipsis_only"):
                a_items.append((rj, it))
    if a_items:
        L.append("| 部 | 时间 | 类型 | 日文 | 现译 |")
        L.append("|---|---|---|---|---|")
        for rj, it in a_items:
            L.append("| %s | %.2f | %s | %s | %s |" % (
                rj, it["t"], it["kind"],
                it["ja"].replace("|", "\\|")[:50], it["zh"].replace("|", "\\|")[:50]))
    else:
        L.append("（无）")
    L.append("")
    # 人称风险
    per_items = [(rj, it) for rj, d in data.items() for it in d["person"]]
    L.append("### 人称风险（person_audit，只报不改）")
    L.append("")
    if per_items:
        L.append("| 部 | 时间 | 译文 | 日文 |")
        L.append("|---|---|---|---|")
        for rj, it in per_items[:40]:
            L.append("| %s | %.2f | %s | %s |" % (
                rj, it.get("t", 0), str(it.get("zh", ""))[:36], str(it.get("ja", ""))[:36]))
        if len(per_items) > 40:
            L.append("| … | | 共 %d 条，只列前 40 | |" % len(per_items))
    else:
        L.append("（无）")
    L.append("")
    # repairs
    L.append("### 已人工修正（repairs，可作为「模型会错在哪」的样本）")
    L.append("")
    reps = [(rj, r) for rj, d in data.items() for r in d["repairs"]]
    if reps:
        L.append("| 部 | 时间 | 修正后 | 原因 |")
        L.append("|---|---|---|---|")
        for rj, r in reps[:40]:
            items = r.get("items") or []
            for it in items[:4]:
                L.append("| %s | %s | %s | %s |" % (
                    rj, it.get("start", ""), str(it.get("zh", ""))[:30],
                    str(it.get("why", ""))[:50]))
        if len(reps) > 40:
            L.append("| … | | 共 %d 批 | |" % len(reps))
    else:
        L.append("（无）")
    L.append("")

    # ---- B 代码/判据 ----
    L.append("## B. 代码 / 判据（交 GPT）")
    L.append("")
    L.append("> 判据：门禁报的东西**不是译文错**，而是检测逻辑/工程机制的问题。")
    L.append("")
    L.append("### B1 门禁红项（按 kind 归类）")
    L.append("")
    by_gate = {}
    for rj, d in data.items():
        _, bad = gate_line(d["gates"])
        for k, kd, sm in bad:
            by_gate.setdefault((k, kd), []).append((rj, sm))
    if by_gate:
        L.append("| 门禁 | kind | 命中部数 | 摘要 |")
        L.append("|---|---|---|---|")
        for (k, kd), v in sorted(by_gate.items(), key=lambda x: -len(x[1])):
            L.append("| %s | %s | %d | %s |" % (
                k, kd, len(v), str(v[0][1])[:46]))
    else:
        L.append("（无）")
    L.append("")

    # ---- C 需拍板 ----
    L.append("## C. 需拍板（交用户）")
    L.append("")
    L.append("> 判据：改不改会影响观感或产品行为，不是对错问题。")
    L.append("")
    L.append("（人工补写：白名单增删、切分阈值、软件交互、要不要重跑全库…）")
    L.append("")

    txt = "\n".join(L) + "\n"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    io.open(out, "w", encoding="utf-8").write(txt)
    print("已生成 %s（%d 部）" % (out, len(data)))
    print(txt[:1200])


if __name__ == "__main__":
    main()
