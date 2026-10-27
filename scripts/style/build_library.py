# -*- coding: utf-8 -*-
r"""
批量建库：把整个学习语料库（多个作品，每个作品 = 音频 + 人工字幕 + ASR 结果）
一次性对齐、汇总成平行句对，并生成统计 / 术语表 / 硬性约束 / 风格指南。

与 align_style.py 的区别：align_style 一次只处理一个目录，
本脚本遍历整个语料库，逐作品对齐后**合并统计**。
单个作品的句对太少，统计出来的比例会失真（一个译者的习惯不该被另一译者稀释，
但同一译者的多个作品必须合起来看），所以先逐个对齐、最后统一汇总。

用法：
  python build_library.py                       # 默认跑 $KIKOERU_ROOT/learning
  python build_library.py --reset               # 清空旧库重建（默认是在旧库上累积）
  python build_library.py --key 柚木            # 只跑名字里含关键字的作品
  python build_library.py --no-llm              # 不调大模型归纳风格指南

产出（默认 $KIKOERU_ROOT/.workbuddy/style）：
  parallel.json / stats.json / glossary.json / hard_rules.txt / style_guide.txt
"""
import os
import re
import sys
import glob
import json
import random
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import align_style as A  # noqa: E402

CORPUS = r"$KIKOERU_ROOT\learning"
ASR_ROOT = r"$KIKOERU_ROOT\.workbuddy\draft\learning"


def pairs_of_work(work_dir, asr_dir, tag, verbose=True):
    """对齐一个作品，返回 (句对, 人工行数, 对齐数)。

    人工行数 vs 对齐数 = 这一轨的召回率：人工写了但 ASR 没识别出来的台词，
    会表现为"对不上"，比例低就说明识别有缺漏，得回头查 ASR 参数。
    """
    subs = sorted([p for p in glob.glob(os.path.join(glob.escape(work_dir), "*"))
                   if p.lower().endswith(A.SUB_EXT)])
    if not subs:
        return [], 0, 0
    asr_files = sorted(glob.glob(os.path.join(glob.escape(asr_dir), "*.json")))
    asr_by = {}
    for p in asr_files:
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            print("    [坏 json] %s %s" % (os.path.basename(p)[:30], str(e)[:40]))
            continue
        asr_by[A.key_of(p)] = d

    out = []
    n_human = 0
    n_hit = 0
    for sp in subs:
        base = A.key_of(sp)
        d = asr_by.get(base)
        if d is None:
            # 字幕名可能和中文字幕名不同（如「m19_01_『想要…』」vs「m19_01_『私と…』」），
            # 按前 8 个字符（序号部分）回退匹配
            hits = [v for k, v in asr_by.items() if base[:8] and base[:8] in k]
            d = hits[0] if len(hits) == 1 else None
        if d is None:
            if verbose:
                print("    [跳过] 无识别结果: %s" % base[:46])
            continue
        rows = A.read_sub(sp)
        n_wm = sum(1 for _, _, t in rows if A.is_watermark(t))
        rows = [(s, e, t) for s, e, t in rows if not A.is_watermark(t)]
        pr = A.align(rows, d.get("segments", []))
        n_human += len(rows)
        n_hit += len(pr)
        is_ft = bool(A.FT_NAME.search(base))
        for p in pr:
            p["src"] = tag
            if is_ft:
                p["type"] = "freetalk"
            spk = A.dominant_speaker(d.get("segments", []), p["s"], p["e"])
            if spk:
                p["spk"] = spk
                role = (d.get("roles") or {}).get(spk)
                if role:
                    p["role"] = role.get("name") or role.get("role") or ""
        if verbose:
            print("    %-42s 人工 %3d 行(剔水印%2d) -> 对齐 %3d 对%s"
                  % (base[:40], len(rows) + n_wm, n_wm, len(pr),
                     "  [free talk]" if is_ft else ""))
        out.extend(pr)
    return out, n_human, n_hit


def filter_pairs(pairs):
    """质量过滤：时间窗对错、长度不合理、含标记符号的都要丢掉，
    否则统计出来的句长/语气词比例全是噪声。"""
    good, seen = [], set()
    for p in pairs:
        ja, zh = p["ja"], A.to_zh_cn(p["zh"])
        if not ja or not zh or ja in seen:
            continue
        if not (3 <= len(zh) <= 45) or not (3 <= len(ja) <= 80):
            continue
        if re.search(r"[〈<\[\(]", zh):
            continue
        r = len(zh) / max(1, len(ja))
        if r < 0.25 or r > 1.6:
            continue
        p["zh"] = zh
        seen.add(ja)
        good.append(p)
    return good


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=CORPUS)
    ap.add_argument("--asr", default=ASR_ROOT)
    ap.add_argument("--style", default=A.DEFAULT_STYLE)
    ap.add_argument("--key", default="")
    ap.add_argument("--reset", action="store_true", help="清空旧库重建")
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()

    os.makedirs(a.style, exist_ok=True)
    P_PARALLEL = os.path.join(a.style, "parallel.json")
    P_STATS = os.path.join(a.style, "stats.json")
    P_GUIDE = os.path.join(a.style, "style_guide.txt")
    P_HARD = os.path.join(a.style, "hard_rules.txt")

    works = [w for w in sorted(os.listdir(a.corpus))
             if os.path.isdir(os.path.join(a.corpus, w))]
    if a.key:
        works = [w for w in works if a.key in w]
    print("语料库 %d 个作品\n" % len(works), flush=True)

    all_raw = []
    tot_human = tot_hit = 0
    low_recall = []
    for i, w in enumerate(works, 1):
        asr_dir = os.path.join(a.asr, w, "asr")
        if not os.path.isdir(asr_dir):
            print("[%2d/%d] %s —— 无 ASR 结果，跳过" % (i, len(works), w[:46]), flush=True)
            continue
        print("[%2d/%d] %s" % (i, len(works), w[:56]), flush=True)
        pr, nh, nk = pairs_of_work(os.path.join(a.corpus, w), asr_dir, w)
        tot_human += nh
        tot_hit += nk
        if nh:
            r = 100.0 * nk / nh
            print("    小计 %d 对 | 召回率 %.1f%%（人工 %d 行 / 对齐 %d）"
                  % (len(pr), r, nh, nk), flush=True)
            if r < 80:
                low_recall.append((w, r, nh, nk))
        all_raw.extend(pr)

    good = filter_pairs(all_raw)
    print("\n对齐 %d 对 -> 质量过滤后 %d 对" % (len(all_raw), len(good)), flush=True)
    if tot_human:
        print("全局召回率 %.1f%%（人工 %d 行 / 对齐 %d 行）"
              % (100.0 * tot_hit / tot_human, tot_human, tot_hit), flush=True)
    if low_recall:
        print("\n[召回率偏低的作品] 人工写了但 ASR 没识别出来，优先复查这些：", flush=True)
        for w, r, nh, nk in sorted(low_recall, key=lambda x: x[1]):
            print("  %.0f%%  %s  (%d/%d)" % (r, w[:56], nk, nh), flush=True)

    if a.reset:
        merged = good
        print("--reset：清空旧库，全新 %d 组" % len(merged))
    else:
        old = []
        if os.path.exists(P_PARALLEL):
            old = json.load(open(P_PARALLEL, encoding="utf-8")).get("pairs", [])
        have = set(p["ja"] for p in old)
        merged = old + [p for p in good if p["ja"] not in have]
        print("累积语料 %d 组（新增 %d）" % (len(merged), len(merged) - len(old)))

    stats = A.compute_stats(merged)
    json.dump({"pairs": merged, "stats": stats}, open(P_PARALLEL, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(stats, open(P_STATS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\n=== 客观统计（%d 组）===" % stats["total"])
    print("  平均句长 %.1f 字 | 句末句号 %.1f%% | 省略号 %.1f%% | 空格分隔 %.1f%%"
          % (stats["avg_zh_len"], stats["pct_period"], stats["pct_ellipsis"],
             stats.get("pct_space", 0)))
    print("  疑问句 %.1f%% | 「呢」%.1f%% | 顿号 %.1f%% | 叹号 %.1f%%"
          % (stats.get("pct_question", 0), stats["pct_ne"], stats["pct_dunhao"],
             stats.get("pct_excl", 0)))
    n_ft = sum(1 for p in merged if p.get("type") == "freetalk")
    n_role = sum(1 for p in merged if p.get("role"))
    print("  free talk 句对 %d | 已判明角色 %d" % (n_ft, n_role))
    print("  高频词:", stats["top_terms"])

    gloss = A.build_glossary(merged)
    json.dump(gloss, open(os.path.join(a.style, "glossary.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n=== 术语表 %d 条 ===" % len(gloss))
    for k, v in list(gloss.items())[:25]:
        print("  %-12s -> %-8s (%d 次, %.0f%%)" % (k, v["zh"], v["hit"], v["rate"] * 100))

    hard = A.gen_hard_rules(stats)
    p_user = os.path.join(a.style, "user_rules.txt")
    if os.path.exists(p_user):
        ur = open(p_user, encoding="utf-8").read().strip()
        if ur:
            hard = ("# 用户指定的统一规范，优先级最高，与下面统计结论冲突时以本节为准\n"
                    + ur + "\n\n" + hard)
    if gloss:
        lines = ["- **术语必须统一**（真实译文共现统计，同一日文词全篇只能这样译）："]
        for k, v in list(gloss.items())[:40]:
            lines.append("  %s -> %s" % (k, v["zh"]))
        hard = hard + "\n" + "\n".join(lines)
    open(P_HARD, "w", encoding="utf-8").write(hard + "\n")
    print("\n=== 硬性约束 ===\n" + hard)

    if a.no_llm:
        return
    try:
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=8).read()
    except Exception as e:
        print("\nllama-server 未就绪，跳过风格指南：%s" % e)
        return

    random.seed(11)
    sample = random.sample(merged, min(100, len(merged)))
    sample_txt = "\n".join("%d|%s\n  译：%s" % (i + 1, p["ja"], p["zh"])
                           for i, p in enumerate(sample))
    print("\n归纳风格指南中 ...", flush=True)
    guide = A.call_llm([{"role": "user",
                         "content": A.GUIDE_PROMPT % A.guide_prompt_args(sample, sample_txt, stats)}])
    guide = A.sanitize_guide(guide, stats)
    with open(P_GUIDE, "w", encoding="utf-8") as f:
        f.write("# 人工翻译风格指南\n# 来源：%d 个作品 / %d 组平行句对\n\n"
                % (len(works), stats["total"]))
        f.write(guide.strip() + "\n")
    print("\n===== 风格指南 =====\n" + guide)
    print("\n已保存 -> %s" % a.style)


if __name__ == "__main__":
    main()
