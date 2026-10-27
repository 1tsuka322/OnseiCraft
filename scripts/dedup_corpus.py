# -*- coding: utf-8 -*-
"""语料去重工具（2026-10-25，T2：把 R104 的一次性去重固化成可复用工具）。

为什么
------
parallel.json 约 75% 的 pair 与同作品邻近 pair 时间重叠 >50%（多变体轨：
反転/声のみ/ノイズ除去なし——同一音频不同跨度重复进语料），导致任何
per-pair 质检与术语频次被高估约 2~4 倍。
见 docs/archive/20261026/TERM_AUDIT_WOMANJIRU_20261024.md §八：88077 → 26885（压缩 69%），
「任何占比/频次类结论都不能拿 88077 当分母」。

去重口径（与 2026-10-24 人工验证一致）
------------------------------------
同作品（src 相同）内按 (s, e) 排序，贪心保留；
候选与任一已保留 pair 的时间重叠 >50% 则丢弃。
「时间重叠 >50%」= 重叠时长 / 两条中**较短者**的时长 > 0.5。
实跑校准：88077 对 → 保留 26961（人工验证值 26885 的 +0.28%，在 ±1% 内）。

用法
----
  python dedup_corpus.py --stats                          # 只报统计，不写文件（默认）
  python dedup_corpus.py --stats --term 忍耐汁,我慢汁,射精
  python dedup_corpus.py --write                          # 写 .workbuddy/tmp/parallel_dedup.json
  python dedup_corpus.py --write --term 忍耐汁

parallel.json 只读，绝不改写。dedup_pairs()/overlap_ratio() 是公共函数，
term_family.py（T3）等直接 import 复用，不要复制粘贴算法。
"""
import os
import sys
import json
import argparse
import collections

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS = os.path.join(ROOT, ".workbuddy", "style", "parallel.json")
DEDUP_OUT = os.path.join(ROOT, ".workbuddy", "tmp", "parallel_dedup.json")
THR = 0.5          # 时间重叠比例阈值（人工拍板，勿改）
MIN_WORK_PAIRS = 100   # 重叠率 top10 只列达到这个对数的作品（避免小作品噪声）


def load_corpus(path=CORPUS):
    return json.load(open(path, encoding="utf-8"))


def work_key(pair):
    return pair.get("src") or ""


def overlap_ratio(a, b):
    """重叠时长 / 两条中较短者的时长（0~1+，0=无重叠；时长非法按无重叠算）。"""
    ov = min(a["e"], b["e"]) - max(a["s"], b["s"])
    if ov <= 0:
        return 0.0
    shorter = min(a["e"] - a["s"], b["e"] - b["s"])
    return ov / shorter if shorter > 0 else 0.0


def group_by_work(pairs):
    by_work = collections.defaultdict(list)
    for i, p in enumerate(pairs):
        by_work[work_key(p)].append(i)
    return by_work


def sorted_by_time(pairs, idxs):
    """同作品内按 (s, e) 排序的下标列表（稳定：同刻按原文件顺序）。"""
    return sorted(idxs, key=lambda i: (pairs[i]["s"], pairs[i]["e"]))


def dedup_pairs(pairs, thr=THR):
    """贪心去重：同作品内按 (s,e) 排序，候选与任一已保留条重叠 >thr 则丢弃。

    返回保留下标列表（按原文件顺序升序）。O(n log n)：lo 指针利用
    「候选 s 单调不降 → kept.e <= 候选.s 的保留条永远不会再重叠」剪枝。
    """
    kept_all = []
    for src, idxs in group_by_work(pairs).items():
        kept, kept_e, lo = [], [], 0
        for i in sorted_by_time(pairs, idxs):
            b = pairs[i]
            while lo < len(kept) and kept_e[lo] <= b["s"]:
                lo += 1
            dup = False
            for j in range(lo, len(kept)):
                a = pairs[kept[j]]
                if a["s"] >= b["e"]:      # kept 按 s 升序，后面只会更大
                    break
                if overlap_ratio(a, b) > thr:
                    dup = True
                    break
            if not dup:
                kept.append(i)
                kept_e.append(b["e"])
        kept_all.extend(kept)
    return sorted(kept_all)


def adjacent_overlap_stats(pairs, thr=THR):
    """「与同作品邻近 pair（按 start 排序的前一条）重叠 >thr」的条数。

    返回 (总重叠条数, {作品: (重叠条数, 总对数)})。这是人工验证用的口径
    （88077 中约 75% 重叠），比「与任意条重叠」的口径（92%）更能代表
    「确实会被去重影响」的比例。
    """
    n_total = 0
    per_work = {}
    for src, idxs in group_by_work(pairs).items():
        segs = sorted_by_time(pairs, idxs)
        n = 0
        for k in range(1, len(segs)):
            if overlap_ratio(pairs[segs[k - 1]], pairs[segs[k]]) > thr:
                n += 1
        per_work[src] = (n, len(segs))
        n_total += n
    return n_total, per_work


def term_freq_lines(pairs, kept_idx, terms):
    """各术语在原始 / 去重后语料中的频次，分 ja / zh 两侧计数。"""
    kept_set = set(kept_idx)
    lines = []
    for t in terms:
        row = ["  %s:" % t]
        for side in ("ja", "zh"):
            raw = sum(1 for p in pairs if t in (p.get(side) or ""))
            ded = sum(1 for i in kept_set if t in (pairs[i].get(side) or ""))
            cell = " %s %5d → %-5d" % (side, raw, ded)
            if raw:
                cell += "(-%.0f%%)" % (100.0 * (raw - ded) / raw)
            row.append(cell)
        lines.append("".join(row))
    return lines


def main():
    ap = argparse.ArgumentParser(description="parallel.json 去重工具（只读语料）")
    ap.add_argument("--stats", action="store_true", help="报统计（无任何参数时的默认行为），不写文件")
    ap.add_argument("--write", action="store_true",
                    help="写 %s（同结构，只保留非重叠 pair）" % DEDUP_OUT)
    ap.add_argument("--term", default="", help="逗号分隔术语：报原始/去重后频次对比（ja/zh 分侧计数）")
    a = ap.parse_args()

    data = load_corpus()
    pairs = data["pairs"]
    n_overlap, per_work = adjacent_overlap_stats(pairs)
    kept_idx = dedup_pairs(pairs)
    n_kept, n_drop = len(kept_idx), len(pairs) - len(kept_idx)

    print("语料     : %s（只读）" % CORPUS)
    print("总对数   : %d" % len(pairs))
    print("重叠条数 : %d（%.1f%%，与同作品邻近 pair 时间重叠 >50%%，按 start 排序与前一条比）"
          % (n_overlap, 100.0 * n_overlap / len(pairs)))
    print("去重后   : 保留 %d（%.1f%%），丢弃 %d" % (n_kept, 100.0 * n_kept / len(pairs), n_drop))

    top = sorted(per_work.items(), key=lambda kv: (kv[1][0] / kv[1][1], kv[1][1]), reverse=True)
    top = [(w, v) for w, v in top if v[1] >= MIN_WORK_PAIRS][:10]
    print("按作品重叠率 top10（只列 ≥%d 对的作品；作品 | 对数 | 重叠 | 重叠率）：" % MIN_WORK_PAIRS)
    for w, (n, tot) in top:
        print("  %-40s %5d %6d  %5.1f%%" % (w[:40], tot, n, 100.0 * n / tot))

    if a.term:
        terms = [t.strip() for t in a.term.split(",") if t.strip()]
        print("术语频次（原始 → 去重后；ja=日文侧，zh=中文侧）：")
        for ln in term_freq_lines(pairs, kept_idx, terms):
            print(ln)

    if a.write:
        out = dict(data)
        out["pairs"] = [pairs[i] for i in kept_idx]
        stats = dict(data.get("stats") or {})
        stats.update({
            "total": n_kept,
            "dedup_from": len(pairs),
            "dedup_dropped": n_drop,
            "dedup_rule": "同作品(src)内按(s,e)排序贪心保留；与已保留条时间重叠>50%%"
                          "（重叠/较短者时长）即丢弃 [dedup_corpus.py %s]" % THR,
        })
        out["stats"] = stats
        os.makedirs(os.path.dirname(DEDUP_OUT), exist_ok=True)
        with open(DEDUP_OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print("已写入   : %s（%d 对，同结构）" % (DEDUP_OUT, n_kept))


if __name__ == "__main__":
    main()
