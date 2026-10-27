# -*- coding: utf-8 -*-
"""成品截断审计（2026-10-25，GPT 第七轮 Q4 的可确定性部分）。

GPT 的结论：**句内语义截断不能仅凭长度确定**；能确定性判的只有「机械完整性」。
本脚本就只做机械完整性，三条判据，都可解释、零模型：

R1 `dangling`  行尾是悬空虚词/介词（「的」「在」「把」「被」「和」「跟」「从」「对」
               「就」「都」「很」「而且」「因为」「所以」「但是」…）→ 中文语法上不成立；
R2 `unclosed`  引号/括号/书名号未闭合（「『』」计一对，允许跨行 → 只在**行内**开而不闭才算）；
R3 `no_punct`  行内 ≥12 字却没有任何句末标点（。！？…）——长句无收束，疑似被砍掉后半。

⚠️ 不判「长度比」：那是语义判断，需要初译对照，且 GPT 明确说只能当高召回筛子。

用法：
  python trunc_audit.py [--dir kikoerusubtitle] [--json out.json] [--md out.md]
"""
import argparse
import collections
import glob
import json
import os
import re

ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DANGLING = ("的", "在", "把", "被", "和", "跟", "从", "对", "就", "都", "很",
            "而且", "因为", "所以", "但是", "如果", "虽然", "然后", "还是",
            "一边", "一边儿", "地", "得", "着", "了", "吗", "呢")
# 「了/吗/呢」结尾在口语里合法，单独降级为 weak
WEAK = {"了", "吗", "呢", "地", "得", "着"}
PAIRS = [("「", "」"), ("『", "』"), ("（", "）"), ("(", ")"), ("《", "》"), ("【", "】")]
TS = re.compile(r"^\[(\d+):(\d+\.\d+)\](.*)$")


def scan_line(t):
    hits = []
    s = t.strip()
    if not s:
        return hits
    # R1 悬空虚词
    for w in sorted(DANGLING, key=len, reverse=True):
        if s.endswith(w):
            hits.append(("weak_dangling" if w in WEAK else "dangling", w))
            break
    # R2 行内未闭合
    for o, c in PAIRS:
        if s.count(o) > s.count(c):
            hits.append(("unclosed", o))
    # R3 长句无收束
    body = re.sub(r"[「」『』（）()《》【】\s]", "", s)
    if len(body) >= 12 and not re.search(r"[。！？…!?]$", s):
        hits.append(("no_punct", str(len(body))))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(ROOT, "kikoerusubtitle"))
    ap.add_argument("--json", default=os.path.join(ROOT, "regset/r127/trunc_audit.json"))
    ap.add_argument("--md", default=os.path.join(ROOT, "docs/terms/TRUNC_AUDIT_20261025.md"))
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(a.dir, "**", "*.lrc"), recursive=True))
    rows, by_rule, by_work = [], collections.Counter(), collections.Counter()
    total = 0
    for f in files:
        rj = os.path.basename(os.path.dirname(f))
        lines = [l.rstrip("\n") for l in open(f, encoding="utf-8")]
        for i, ln in enumerate(lines):
            m = TS.match(ln)
            if not m:
                continue
            t = m.group(3)
            if not t.strip():
                continue                      # 清屏 spacer 不算
            total += 1
            hs = scan_line(t)
            if not hs:
                continue
            for rule, val in hs:
                by_rule[rule] += 1
                by_work[rj] += 1
            prev = TS.match(lines[i - 1]).group(3).strip() if i and TS.match(lines[i - 1]) else ""
            nxt = ""
            for j in range(i + 1, min(i + 4, len(lines))):
                m2 = TS.match(lines[j])
                if m2 and m2.group(3).strip():
                    nxt = m2.group(3).strip()
                    break
            rows.append(dict(rj=rj, file=os.path.basename(f), ts=m.group(1) + ":" + m.group(2),
                             zh=t.strip(), prev=prev, next=nxt,
                             rules=[r for r, _ in hs], vals=[v for _, v in hs]))

    print("扫描 %d 个 LRC / %d 条内容行" % (len(files), total))
    print("\n=== 命中（按规则）===")
    for k, v in by_rule.most_common():
        print("  %-14s %d  (%.2f%% of 行)" % (k, v, 100.0 * v / max(total, 1)))
    print("\n=== 按作品（前 10）===")
    for k, v in by_work.most_common(10):
        print("  %-12s %d" % (k, v))
    print("\n=== 样例（每规则 5 条）===")
    shown = collections.Counter()
    for r in rows:
        for rule in r["rules"]:
            if shown[rule] >= 5:
                continue
            shown[rule] += 1
            print("  [%s] %s %s" % (rule, r["rj"], r["ts"]))
            print("      前：%s" % r["prev"][:50])
            print("      本：%s" % r["zh"][:70])
            print("      后：%s" % r["next"][:50])

    os.makedirs(os.path.dirname(a.json), exist_ok=True)
    json.dump(dict(total=total, by_rule=dict(by_rule), by_work=dict(by_work), rows=rows),
              open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\njson →", a.json)

    L = ["# 成品截断审计（机械完整性，2026-10-25）", "",
         "> 脚本 `scripts/trunc_audit.py`（只判机械完整性，**不判语义**）。",
         "> 扫描 %d 个 LRC / %d 条内容行（清屏 spacer 已排除）。" % (len(files), total), "",
         "## 命中统计", "", "| 规则 | 命中 | 占内容行 | 含义 |", "|---|---|---|---|",
         "| dangling | %d | %.2f%% | 行尾悬空虚词/介词（语法上不成立） |" % (by_rule["dangling"], 100.0 * by_rule["dangling"] / max(total, 1)),
         "| weak_dangling | %d | %.2f%% | 行尾「了/吗/呢/地/得/着」——口语里常合法，仅供参考 |" % (by_rule["weak_dangling"], 100.0 * by_rule["weak_dangling"] / max(total, 1)),
         "| unclosed | %d | %.2f%% | 行内引号/括号未闭合 |" % (by_rule["unclosed"], 100.0 * by_rule["unclosed"] / max(total, 1)),
         "| no_punct | %d | %.2f%% | ≥12 字却无句末标点（疑似被砍掉后半） |" % (by_rule["no_punct"], 100.0 * by_rule["no_punct"] / max(total, 1)),
         "", "## 按作品", "", "| 作品 | 命中 |", "|---|---|"]
    for k, v in by_work.most_common():
        L.append("| %s | %d |" % (k, v))
    L += ["", "## 明细（前 60 条，带前后文，供 fixline 用）", "",
          "| 作品 | 时间 | 规则 | 本行 | 前一行 |", "|---|---|---|---|---|"]
    for r in rows[:60]:
        L.append("| %s | %s | %s | %s | %s |" %
                 (r["rj"], r["ts"], ",".join(r["rules"]), r["zh"][:40], r["prev"][:30]))
    open(a.md, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("md →", a.md)


if __name__ == "__main__":
    main()
