#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v2/v3 在标准样品上的对比分析。

只比 01 产物（json），不跑管线。

指标分两类：
  A. 客观结构量 —— 段数 / 均段长 / 覆盖率 / 幻觉剔除数 / 空段数
     （这些不需要人工判断，直接可比）
  B. 内容正确性 —— 关键词命中（每个样品单独定义，见 samples/_meta/asr_keywords.json）
     关键词是**人工确认过在该轨真实出现**的，命中率高 = 听对了。
     不设「相似度」指标：两个模型都错时相似度会骗人。

⚠️ 不用「段数多 = 好」：段数多可能只是切得碎。要看**与关键词的对应**。
"""
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
BASE = os.path.join(ROOT, "tmp", "asr_v2v3_samples")
KW = os.path.join(ROOT, "samples", "_meta", "asr_keywords.json")
ERRATA = os.path.join(ROOT, "data", "style", "asr_errata.json")


def load(out_dir):
    segs = []
    for p in sorted(glob.glob(os.path.join(out_dir, "*.json"))):
        try:
            d = json.load(io.open(p, encoding="utf-8"))
        except Exception:
            continue
        if d.get("error"):
            return None, d.get("error")
        segs += d.get("segments") or []
    return segs, None


def stats(segs):
    if not segs:
        return {}
    dur = [s["end"] - s["start"] for s in segs]
    txt = "".join(s.get("text", "") for s in segs)
    cov = sum(dur) / max((segs[-1]["end"] - segs[0]["start"]), 1) * 100
    # 时间空洞：相邻段之间的空白。**这是「漏听」最硬的证据** ——
    # 2026-11-08 RJ1458584 实测：v2 在 247.1→277.1 有 30 秒空白，
    # 原文那段是角色在念 `A B C D E F G H`（数罩杯等级），v2 全丢，
    # 而 v3 完整保留。覆盖率（73% vs 66%）反而看不出这件事 ——
    # 因为 v2 段长（大段吞静音），v3 段碎（静音单独留空）。
    gaps = [segs[i + 1]["start"] - segs[i]["end"] for i in range(len(segs) - 1)]
    return {
        "n": len(segs),
        "avg_dur": round(sum(dur) / len(dur), 2),
        "chars": len(txt),
        "cov": round(cov, 1),
        "max_gap": round(max(gaps), 1) if gaps else 0,
        "gap5": sum(1 for g in gaps if g > 5),
        "digits": len(re.findall(r"\d", txt)),
        "upper": len(re.findall(r"[A-Z]", txt)),
        "text": txt,
    }


def errata_hits(text):
    """两边命中了哪些已知空耳形式（errata 表里的 `ja`）。

    ⚠️ **只列不判**：errata 里有 `謝罪`/`悪夢`/`まず` 这类**本身是正常日语词**的
    条目，出现在正常语境里不算错。所以这里只输出命中清单供人工核，
    不做「命中多 = 差」的自动判断。
    2026-11-08 RJ1260535 实例：
      v2 命中 謝罪（原文是 しゃせい = 射精）
      v3 命中 写生（同一处，也是 しゃせい）
      —— 两版都错，只是错法不同，自动计分会判成「打平」，人工才看得出。
    """
    try:
        e = json.load(io.open(ERRATA, encoding="utf-8"))["entries"]
    except Exception:
        return []
    out = []
    for x in e:
        ja = (x.get("ja") or "").strip()
        if len(ja) < 2 or not x.get("fix"):
            continue
        if ja in text:
            out.append("%s→%s" % (ja, x["fix"]))
    return out


def main():
    rjs = sys.argv[1:]
    if not rjs:
        rjs = sorted(os.path.basename(d) for d in glob.glob(os.path.join(BASE, "v2", "*")))
    kw = {}
    if os.path.isfile(KW):
        kw = json.load(io.open(KW, encoding="utf-8"))

    for rj in rjs:
        a, ea = load(os.path.join(BASE, "v2", rj))
        b, eb = load(os.path.join(BASE, "v3", rj))
        print("=" * 74)
        print("## %s  %s" % (rj, kw.get(rj, {}).get("scene", "")))
        if ea or eb:
            print("  v2: %s / v3: %s" % (ea or "ok", eb or "ok"))
            continue
        sa, sb = stats(a), stats(b)
        print("  %-12s %8s %8s %8s" % ("", "v2", "v3", "差"))
        for k, name in (("n", "段数"), ("avg_dur", "均段长"), ("chars", "字数"),
                        ("cov", "覆盖率%"), ("max_gap", "最大空洞s"),
                        ("gap5", ">5s空洞数"), ("digits", "数字数"), ("upper", "大写字母")):
            va, vb = sa.get(k, 0), sb.get(k, 0)
            print("  %-12s %8s %8s %8s" % (name, va, vb, vb - va))

        kws = (kw.get(rj) or {}).get("keywords") or []
        if kws:
            print("\n  关键词命中（人工确认该轨真实出现）:")
            print("  %-18s %6s %6s" % ("词", "v2", "v3"))
            win = {"v2": 0, "v3": 0}
            for w in kws:
                ca, cb = sa["text"].count(w), sb["text"].count(w)
                mark = ""
                if ca != cb:
                    if cb > ca:
                        win["v3"] += 1; mark = "  ← v3"
                    else:
                        win["v2"] += 1; mark = "  ← v2"
                print("  %-18s %6d %6d%s" % (w[:16], ca, cb, mark))
            print("  → 胜出: v2 %d / v3 %d / 平 %d"
                  % (win["v2"], win["v3"], len(kws) - win["v2"] - win["v3"]))

        ea_hits, eb_hits = errata_hits(sa["text"]), errata_hits(sb["text"])
        if ea_hits or eb_hits:
            print("\n  已知空耳命中（只列不判，需人工核语境）:")
            print("    v2: %s" % (" ".join(ea_hits[:8]) or "—"))
            print("    v3: %s" % (" ".join(eb_hits[:8]) or "—"))
        print()


if __name__ == "__main__":
    main()
