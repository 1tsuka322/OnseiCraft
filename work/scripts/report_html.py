#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 06 的报告渲染成**单文件 HTML**（无外部依赖，双击就能看）。

## 为什么要有这个

2026-11-07 用户提过一个真问题：

> 漏译拦截什么的只能靠 agent 工作流修正，走软件的话是修不了的是吗

查证后的现状是：软件**能检测**（06 跑 lexical 会红），但
`app/studio/runner.py:_report_gates` **只回显门禁名和红绿，不读明细** ——
用户跑到最后看到「门禁未过」，不知道是哪几行、也不知道怎么改。

这个脚本把「哪几行」变成可读的页面。软件端只需加一个「查看问题清单」按钮
打开它，不必改动管线逻辑。

## 内容

- 顶部：可交付判定（contract + output 类）+ 各门禁一览
- 分区：**可交付判定** / **仍需审查**（audit 类，不影响交付但要看）
- 明细：lexical（脱落/省略/音译/拉丁）/ person（人称风险）/ locks（锁落地）/ trunc（截断）

用法：
  python scripts/report_html.py --rj RJ1058739      # 自动找最新 run
  python scripts/report_html.py --run <run_id>
  python scripts/report_html.py --all               # 每部一份
"""
import argparse
import glob
import html
import io
import json
import os
import sys

if hasattr(sys, "stdout") and hasattr(sys, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNS = os.path.join(ROOT, "runs")

# 门禁分类（与 pipeline_lib/stages.py 的 GATE_KIND 同口径）
KIND = {
    "empty": "contract", "errata_audit": "contract", "test_regressions": "contract",
    "check": "output", "lexical": "output",
    "scan": "audit", "overlay": "audit", "trunc": "audit", "slot_guard": "audit",
    "locks": "audit", "semantic": "audit", "person_audit": "audit",
}
KIND_CN = {"contract": "执行契约", "output": "输出合法性", "audit": "质量审计"}

CSS = """
body{font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
     margin:0;padding:24px 32px;color:#1a1a1a;background:#fafafa;max-width:1200px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:26px 0 10px;
   padding-bottom:6px;border-bottom:1px solid #e0e0e0}
.sub{color:#666;font-size:13px;margin-bottom:16px}
.badge{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;
       font-weight:600;margin-right:8px}
.ok{background:#e6f4ea;color:#137333}.bad{background:#fce8e6;color:#c5221f}
.warn{background:#fef7e0;color:#b06000}
table{border-collapse:collapse;width:100%;margin:8px 0;background:#fff;
      box-shadow:0 1px 2px rgba(0,0,0,.06)}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid #eee;
      vertical-align:top;font-size:13px}
th{background:#f5f5f5;font-weight:600;font-size:12px;color:#555}
td.t{white-space:nowrap;color:#666;font-variant-numeric:tabular-nums;width:70px}
td.k{white-space:nowrap;width:110px}
details{margin:10px 0}summary{cursor:pointer;font-weight:600;padding:6px 0;
        color:#0b57d0}summary:hover{text-decoration:underline}
.empty{color:#999;font-style:italic;padding:8px 0}
.gates{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:6px}
.gate{padding:6px 10px;border-radius:6px;background:#fff;font-size:13px;
      box-shadow:0 1px 2px rgba(0,0,0,.06)}
code{background:#f0f0f0;padding:1px 5px;border-radius:3px;font-size:12px}
"""


def latest_run(rj):
    best = None
    for p in glob.glob(os.path.join(RUNS, "*_%s_*" % rj)):
        mp = os.path.join(p, "manifest.json")
        if not os.path.isfile(mp):
            continue
        try:
            m = json.load(io.open(mp, encoding="utf-8"))
        except Exception:
            continue
        key = m.get("created_at") or ""
        if best is None or key > best[0]:
            best = (key, os.path.basename(p))
    return best[1] if best else None


def load(rid, name):
    p = os.path.join(RUNS, rid, "06_checks", name)
    if not os.path.isfile(p):
        return None
    try:
        return json.load(io.open(p, encoding="utf-8"))
    except Exception:
        return None


def esc(s, n=0):
    s = "" if s is None else str(s)
    if n and len(s) > n:
        s = s[:n] + "…"
    return html.escape(s)


def table(headers, rows):
    if not rows:
        return '<div class="empty">（无）</div>'
    h = "".join("<th>%s</th>" % esc(x) for x in headers)
    body = "".join("<tr>%s</tr>" % "".join(
        '<td class="%s">%s</td>' % (c, esc(v, 60)) for c, v in r) for r in rows)
    return "<table><tr>%s</tr>%s</table>" % (h, body)


def render(rid):
    m = json.load(io.open(os.path.join(RUNS, rid, "manifest.json"), encoding="utf-8"))
    rj = m.get("project") or "?"
    gates = load(rid, "gates.json") or {}
    lex = load(rid, "lexical_audit.json") or {}
    per = load(rid, "person_audit.json") or {}
    lck = load(rid, "lock_coverage.json") or {}

    bad = [(k, KIND.get(k, "?"), v) for k, v in gates.items() if not v.get("ok")]
    deliver_bad = [k for k, kd, _ in bad if kd in ("contract", "output")]
    ok = not deliver_bad

    L = ["<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
         "<title>%s 问题清单</title><style>%s</style></head><body>" % (esc(rj), CSS),
         "<h1>%s 问题清单</h1>" % esc(rj),
         "<div class='sub'>run <code>%s</code> · 生成于 %s</div>"
         % (esc(rid), esc(__import__("time").strftime("%Y-%m-%d %H:%M"))),
         "<div><span class='badge %s'>%s</span>" % ("ok" if ok else "bad",
                                                   "可交付" if ok else "不可交付"),
         "<span class='badge warn'>仍需审查 %d 项</span></div>"
         % len([1 for k, kd, _ in bad if kd == "audit"])]

    # ---- 门禁一览 ----
    L.append("<h2>门禁一览</h2><div class='gates'>")
    for k, v in gates.items():
        st = "ok" if v.get("ok") else ("bad" if KIND.get(k) in ("contract", "output") else "warn")
        L.append("<div class='gate'><span class='badge %s'>%s</span>%s<br>"
                 "<span class='sub' style='margin:0'>%s</span></div>"
                 % (st, "通过" if v.get("ok") else "未过", esc(k),
                    esc(v.get("summary") or "", 48)))
    L.append("</div>")

    # ---- 按类别汇总（防 by_kind 误读）----
    # ⚠️ manifest 的 gate.by_kind 列的是「该类的**所有**门禁」，不管红绿。
    #    直接读会以为整类都红（RJ1228193 的日志写 output:["check","lexical"]，
    #    实际 check 是绿的）。这里从每个门禁自己的 ok 重新派生，不猜、不复用 by_kind。
    kinds = {}
    for k, v in gates.items():
        kd = KIND.get(k, "?")
        slot = kinds.setdefault(kd, {"all": [], "bad": []})
        slot["all"].append(k)
        if not v.get("ok"):
            slot["bad"].append(k)
    if kinds:
        L.append("<h2>按类别</h2>")
        L.append("<p class='sub'>「全部」是该类别参与的门禁；只有「失败」里的才会拦住交付。"
                 "manifest 的 <code>by_kind</code> 是类别成员清单，<b>不代表红绿</b>。</p>")
        L.append("<ul>")
        for kd in ("contract", "output", "audit"):
            if kd not in kinds:
                continue
            slot = kinds[kd]
            alltxt = "、".join(esc(x) for x in slot["all"])
            if slot["bad"]:
                badtxt = "、".join("<b>%s</b>" % esc(x) for x in slot["bad"])
                L.append("<li>%s：全部 %d 项（%s）→ <span class='badge bad'>失败 %d</span> %s</li>"
                         % (esc(KIND_CN.get(kd, kd)), len(slot["all"]), alltxt,
                            len(slot["bad"]), badtxt))
            else:
                L.append("<li>%s：全部 %d 项（%s）→ <span class='badge ok'>全部通过</span></li>"
                         % (esc(KIND_CN.get(kd, kd)), len(slot["all"]), alltxt))
        L.append("</ul>")

    # ---- 可交付判定失败项 ----
    if deliver_bad:
        L.append("<h2>阻断交付的问题</h2>")
        for k, kd, v in bad:
            if kd in ("contract", "output"):
                L.append("<p><span class='badge bad'>%s</span>%s：%s</p>"
                         % (esc(KIND_CN[kd]), esc(k), esc(v.get("summary") or "")))

    # ---- lexical 明细 ----
    hits = []
    for rjblk in (lex.get("rj") or []):
        for h in (rjblk.get("hits") or []):
            if h.get("kind") in ("content_drop", "ellipsis_only", "translit", "latin2"):
                hits.append(h)
    L.append("<h2>词汇层问题（%d 条）</h2>" % len(hits))
    KIND_CN2 = {"content_drop": "实词脱落", "content_drop_soft": "脱落(软)",
                "ellipsis_only": "纯省略", "translit": "音译可疑", "latin2": "拉丁残留"}
    L.append(table(["时间", "类型", "日文原文", "译文"],
                   [[("t", "%.2f" % (h.get("t") or 0)),
                     ("k", KIND_CN2.get(h.get("kind"), h.get("kind"))),
                     ("", (h.get("ja") or "")[:70]),
                     ("", (h.get("zh") or "")[:50])] for h in hits]))

    # ---- 人称风险 ----
    items = [x for x in (per.get("items") or []) if x.get("risk")]
    L.append("<h2>人称风险（%d 条候选）</h2>" % len(items))
    L.append("<div class='sub'>判据：原文前后 6 秒无他者实体、译文却出现「她/他」。"
             "**这是候选清单，不是错误清单** —— 需要人工判断是不是真错。</div>")
    L.append(table(["时间", "轨", "日文原文", "现译"],
                   [[("t", "%.2f" % (x.get("t") or 0)),
                     ("k", (x.get("track") or "")[:14]),
                     ("", (x.get("ja") or "")[:60]),
                     ("", (x.get("zh") or "")[:44])] for x in items]))

    # ---- 锁落地 ----
    rows = (lck.get("rows") or [])
    if rows:
        L.append("<h2>行级锁落地</h2>")
        L.append(table(["总锁", "落地", "错位", "丢失", "落地率"],
                       [[("t", r.get("total")), ("", r.get("landed")),
                         ("", r.get("misplaced", 0)), ("", r.get("missing")),
                         ("", "%.0f%%" % (100.0 * r.get("landed", 0) / max(r.get("total", 1), 1)))]
                        for r in rows]))
        miss = []
        for r in rows:
            for x in (r.get("miss_detail") or []):
                miss.append(x)
        if miss:
            L.append("<details><summary>丢失的锁（%d 条，人工修复没落地）</summary>" % len(miss))
            L.append(table(["锁文", "日文锚点"],
                           [[("", (x.get("zh") or "")[:46]), ("", (x.get("ja") or "")[:50])]
                            for x in miss]))
            L.append("</details>")

    # ---- 截断审计 ----
    tp = os.path.join(RUNS, rid, "06_checks", "TRUNC_AUDIT.md")
    if os.path.isfile(tp):
        txt = io.open(tp, encoding="utf-8").read()
        seg = txt.split("## 命中统计")[-1][:900] if "## 命中统计" in txt else txt[:900]
        L.append("<details><summary>截断审计（机械完整性）</summary><pre style='background:#fff;"
                 "padding:12px;overflow:auto;font-size:12px'>%s</pre></details>" % esc(seg))

    L.append("<div class='sub' style='margin-top:30px'>"
             "由 <code>scripts/report_html.py</code> 生成。"
             "「可交付」只代表执行契约和输出合法性通过，<b>不代表翻译无误</b>。</div>")
    L.append("</body></html>")
    return rj, "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rj", default="")
    ap.add_argument("--run", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    rids = []
    if a.run:
        rids = [a.run]
    elif a.rj:
        r = latest_run(a.rj)
        if not r:
            raise SystemExit("找不到 %s 的 run" % a.rj)
        rids = [r]
    elif a.all:
        seen = {}
        for p in sorted(glob.glob(os.path.join(RUNS, "*_production"))):
            try:
                m = json.load(io.open(os.path.join(p, "manifest.json"), encoding="utf-8"))
            except Exception:
                continue
            rj = m.get("project")
            if rj:
                seen[rj] = max(seen.get(rj, ""), os.path.basename(p))
        rids = sorted(seen.values())
    else:
        raise SystemExit("要 --rj / --run / --all")

    for rid in rids:
        try:
            rj, doc = render(rid)
        except Exception as e:
            print("%-40s 渲染失败：%s" % (rid, e))
            continue
        out = a.out or os.path.join(RUNS, rid, "06_checks", "report.html")
        io.open(out, "w", encoding="utf-8").write(doc)
        print("%-11s → %s" % (rj, out))


if __name__ == "__main__":
    main()
