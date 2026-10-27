# -*- coding: utf-8 -*-
"""术语族盘点工具（2026-10-25，T3：把「我慢汁族」那次手工盘点固化成可复用工具）。

为什么
------
每次术语裁决都要手工统计「一个概念的 ja 写法分布 + zh 译法分布 + 作品分布」。
语料 69% 是重复（多变体轨），原始频次会把分布放大 2~4 倍，
所以频次一律同时报「原始 / 去重后」（见 docs/archive/20261026/TERM_AUDIT_WOMANJIRU_20261024.md §八）。

去重逻辑复用 T2 的 dedup_pairs()（import，不复制粘贴）。

用法
----
  python term_family.py --ja 我慢汁,我慢じる,ガマンチレ
  python term_family.py --ja 我慢汁,我慢じる,ガマンチレ --zh 忍耐汁,忍耐液,前列腺液
  python term_family.py --ja ... --zh ... --json out.json

输出（stdout，--json 另存同内容）：
  - ja 各写法：原始频次 / 去重后频次 / 作品数（去重后口径）
  - zh 各译法：同上 + 与哪些 ja 写法共现最多（top3）
  - 交叉表：ja 写法 × zh 译法 共现次数（原始 / 去重后，按原始取 top10）

匹配规则：子串匹配；ja 词只查 ja 字段，zh 词只查 zh 字段（分侧计数，
与 TERM_AUDIT_WOMANJIRU_20261024.md 的「侧」口径一致）。
"""
import os
import sys
import json
import argparse
import collections

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dedup_corpus import CORPUS, load_corpus, dedup_pairs   # T2 公共函数，勿复制算法


def side_hits(pairs, field, term):
    """field 含 term 的下标列表。"""
    return [i for i, p in enumerate(pairs) if term in (p.get(field) or "")]


def work_count(pairs, idxs):
    return len({pairs[i].get("src") or "" for i in idxs})


def main():
    ap = argparse.ArgumentParser(description="术语族盘点（ja 写法 × zh 译法 分布）")
    ap.add_argument("--ja", required=True, help="逗号分隔的 ja 写法列表（查 ja 字段）")
    ap.add_argument("--zh", default="", help="逗号分隔的 zh 译法列表（查 zh 字段，可选）")
    ap.add_argument("--json", default="", help="结果另存到该 JSON 文件（可选）")
    a = ap.parse_args()

    ja_terms = [t.strip() for t in a.ja.split(",") if t.strip()]
    zh_terms = [t.strip() for t in a.zh.split(",") if t.strip()]

    pairs = load_corpus()["pairs"]
    kept_idx = dedup_pairs(pairs)
    kept_set = set(kept_idx)
    print("数据     : %s（只读）" % CORPUS)
    print("语料规模 : 原始 %d 对 · 去重后 %d 对（dedup_corpus 口径）" % (len(pairs), len(kept_idx)))

    result = {"corpus": CORPUS, "total_raw": len(pairs), "total_dedup": len(kept_idx),
              "ja": [], "zh": [], "cross": []}

    # ---- ja 写法 ----
    print("\n== ja 写法（ja 字段子串计数）==")
    print("  %-14s %6s %8s %10s" % ("写法", "原始", "去重后", "作品数(去重后)"))
    for t in ja_terms:
        raw = side_hits(pairs, "ja", t)
        ded = [i for i in raw if i in kept_set]
        w = work_count(pairs, ded)
        print("  %-14s %6d %8d %10d" % (t, len(raw), len(ded), w))
        result["ja"].append({"term": t, "raw": len(raw), "dedup": len(ded), "works_dedup": w})

    # ---- zh 译法 ----
    if zh_terms:
        print("\n== zh 译法（zh 字段子串计数）==")
        print("  %-14s %6s %8s %10s  %s" % ("译法", "原始", "去重后", "作品数(去重后)", "最常共现 ja(top3)"))
        for t in zh_terms:
            raw = side_hits(pairs, "zh", t)
            ded = [i for i in raw if i in kept_set]
            w = work_count(pairs, ded)
            co = collections.Counter()
            for i in ded:
                for jt in ja_terms:
                    if jt in (pairs[i].get("ja") or ""):
                        co[jt] += 1
            co_s = " · ".join("%s×%d" % (k, v) for k, v in co.most_common(3)) or "（本列表内无共现）"
            print("  %-14s %6d %8d %10d  %s" % (t, len(raw), len(ded), w, co_s))
            result["zh"].append({"term": t, "raw": len(raw), "dedup": len(ded),
                                 "works_dedup": w, "cooc_ja": co.most_common(3)})

        # ---- 交叉表 ----
        print("\n== 交叉表 ja × zh（同一条 pair 里 ja 字段含写法且 zh 字段含译法；按原始取 top10）==")
        print("  %-14s %-14s %8s %8s" % ("ja", "zh", "原始", "去重后"))
        cross = []
        for jt in ja_terms:
            ja_raw = set(side_hits(pairs, "ja", jt))
            for zt in zh_terms:
                n_raw = len(ja_raw & set(side_hits(pairs, "zh", zt)))
                n_ded = sum(1 for i in ja_raw if i in kept_set and zt in (pairs[i].get("zh") or ""))
                cross.append({"ja": jt, "zh": zt, "raw": n_raw, "dedup": n_ded})
        cross.sort(key=lambda x: (-x["raw"], -x["dedup"]))
        for c in cross[:10]:
            print("  %-14s %-14s %8d %8d" % (c["ja"], c["zh"], c["raw"], c["dedup"]))
        result["cross"] = cross

    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        print("\n已写入   : %s" % a.json)


if __name__ == "__main__":
    main()
