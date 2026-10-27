# -*- coding: utf-8 -*-
r"""记忆遵守率报表（2026-10-25，T5-A：轨级记忆注入的**确定性**评估指标）。

它回答什么
----------
轨级记忆（`draft/<rj>/track_memory/<轨>.json`）里写了「`ja→zh`」，实际成品 LRC 里
这些行到底有没有用 `zh`？以及：这个术语是不是 **glossary 已覆盖**——覆盖了就不算
记忆的功劳（术语锁/术语表本来就会统一它），只有 `memory_only` 才是记忆的净信号。

为什么不用模型、纯读文件算数：A/B 差异被采样噪声淹没（同配置两遍 33% 行不同，
见 T4），任何「再跑一遍模型打分」的评估都不可信；而「记忆写了 X、成品有没有 X」
是确定性事实，逐字节可复现。

口径（与 scan_violations 一致，坑 90）
------------------------------------
1. 逐轨读成品 LRC；空文本行（清屏 spacer）不参与术语检查，但**保留在时间轴里**——
   行区间 = [本行 start, 下一行 start)，下一行含 spacer 才是真实显示区间。
2. ASR 版本**不写死**：成品与 `draft/asr`（v2 重跑版）混存，16 部里 12 部时间戳
   对齐 `asr_v1_20261020`。直接复用 `scan_violations.pick_asr()`（词级时间戳匹配率
   选版本、平分优先 v1）；选中的版本名打进输出，便于人工核对。
3. 行日文 = `scan_violations.ja_in_range()`：该区间内 ASR 词级文本**聚合拼接**
   （一个 LRC 行常跨 2–3 段，只取首条会漏 ja）。
4. 术语命中：行日文含 `ja` 记一次命中；行中文含 `zh` 记一次「遵守」。
5. 分栏（术语级、按 (ja,zh) 去重后判定）：
   · covered      glossary 里有同一 ja 且 zh 相同 → 遵守率是术语表基线，非记忆贡献；
   · memory_only  glossary 里没有该 ja → **记忆的净信号**；
   · conflict     glossary 有该 ja 但 zh 不同 → 理论上 load_mem() 已丢弃，应恒为 0。
   ⚠️ 已知口径限制：一个 cue 的中文可能被显示层拆成两行（render_display_lines），
   zh 若恰好跨行会被算「未遵守」；把 zh 当整体子串查本行，比按 cue 聚合更保守。

用法：
  python mem_conformance.py --out-dir <成品LRC目录> --mem-dir <draft/<rj>/track_memory> \
                            --asr-dir <draft/<rj>/asr> [--glossary style/glossary.json] \
                            [--json 输出.json]
"""
import os
import sys
import json
import glob
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan_violations as sv   # 复用 pick_asr / parse_lrc / ja_in_range（坑 90 同口径）

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")   # die() 走 stderr，Windows 管道下防乱码
ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def die(msg):
    """参数/输入问题必须响亮地失败（任务约束：不许静默返回 0）。"""
    print("错误：%s" % msg, file=sys.stderr)
    sys.exit(2)


def load_glossary(path):
    """glossary.json → {ja: zh}。值是 {"zh": ...} dict 或纯字符串（load_style 同口径）。"""
    g = json.load(open(path, encoding="utf-8"))
    out = {}
    for ja, v in g.items():
        out[ja] = (v.get("zh") or "") if isinstance(v, dict) else (v or "")
    return out


def term_class(ja, zh, gloss):
    """covered / memory_only / conflict（分栏判据见模块 docstring 第 5 条）。"""
    if ja not in gloss:
        return "memory_only"
    return "covered" if gloss.get(ja) == zh else "conflict"


def main():
    ap = argparse.ArgumentParser(description="轨级记忆遵守率报表（确定性，不调模型）")
    ap.add_argument("--out-dir", required=True, help="成品 LRC 目录（kikoerusubtitle/<rj>）")
    ap.add_argument("--mem-dir", required=True, help="draft/<rj>/track_memory")
    ap.add_argument("--asr-dir", required=True, help="draft/<rj>/asr（版本选择见 pick_asr）")
    ap.add_argument("--glossary", default="", help="style/glossary.json（不给则全部按 memory_only）")
    ap.add_argument("--json", default="", help="结果另存 JSON（可选）")
    a = ap.parse_args()

    # 参数校验：路径不存在要报清晰错误，不要静默当空集跑出一份全 0 的报表。
    for flag, d in (("--out-dir", a.out_dir), ("--mem-dir", a.mem_dir), ("--asr-dir", a.asr_dir)):
        if not os.path.isdir(d):
            die("%s 不是目录：%s" % (flag, d))
    if a.glossary and not os.path.exists(a.glossary):
        die("--glossary 文件不存在：%s" % a.glossary)
    lrcs = sorted(glob.glob(os.path.join(a.out_dir, "*.lrc")))
    if not lrcs:
        die("--out-dir 下没有任何 .lrc：%s" % a.out_dir)

    gloss = load_glossary(a.glossary) if a.glossary else {}

    # pick_asr/load_asr 以 <draft根>/<rj>/<版本目录> 解析；从 --asr-dir 反推，
    # 非标准布局（如 tmp 下的实验目录）也能选版本。默认根相同时不动模块全局。
    rj = os.path.basename(os.path.dirname(os.path.abspath(a.asr_dir)))
    draft_root = os.path.dirname(os.path.dirname(os.path.abspath(a.asr_dir)))
    if os.path.normcase(sv.ASR_ROOT) != os.path.normcase(draft_root):
        sv.ASR_ROOT = draft_root

    # 术语分栏是**术语级**的（同一 (ja,zh) 在多轨出现只判一次），命中/遵守按轨累计。
    cls_of = {}
    stat = {}          # (ja, zh) -> {"hit": n, "conf": n, "tracks": n}
    tracks_out = []
    n_tracks = 0

    for lf in lrcs:
        stem = os.path.basename(lf)[:-4]          # 去掉 .lrc，如 "00.xxx.mp3"
        mem_p = os.path.join(a.mem_dir, stem + ".json")
        if not os.path.exists(mem_p):
            # 记忆文件名与轨名不同步时按 source 前缀兜底找一次（load_mem 同思路）
            cands = [p for p in glob.glob(os.path.join(a.mem_dir, "*.json"))
                     if os.path.basename(p).startswith(stem[:8])]
            mem_p = cands[0] if cands else None
        if not mem_p or not os.path.exists(mem_p):
            print("轨 %s：无记忆文件，跳过" % stem[:44])
            continue
        try:
            mem = json.load(open(mem_p, encoding="utf-8"))
        except Exception as e:
            print("轨 %s：记忆文件损坏（%s），跳过" % (stem[:44], e))
            continue
        terms = [t for t in (mem.get("terms") or []) if (t.get("ja") or "").strip()
                 and (t.get("zh") or "").strip()]
        if not terms:
            print("轨 %s：记忆无 terms，跳过" % stem[:44])
            continue

        rows = sv.parse_lrc(lf)
        data, ver = sv.pick_asr(rj, stem, rows)
        if data is None:
            print("轨 %s：asr 与 asr_v1_20261020 都没有该轨 JSON，跳过" % stem[:44])
            continue
        segs = data.get("segments") or []

        t_hit = {c: 0 for c in ("covered", "memory_only", "conflict")}
        per_term = {}
        for t in terms:
            ja, zh = t["ja"], t["zh"]
            key = (ja, zh)
            if key not in cls_of:
                cls_of[key] = term_class(ja, zh, gloss)
                stat[key] = {"hit": 0, "conf": 0, "tracks": 0}
            rec = stat[key]

        n_tracks += 1
        hit_here = set()     # 本轨命中的术语（「出现在几轨」用，一次行扫描顺带收齐）
        # 逐内容行：区间 = [本行 start, 下一行 start)（含 spacer 的下一行才是区间终点）
        for i, (t0, zh_line) in enumerate(rows):
            if not zh_line.strip():
                continue                      # 清屏 spacer：不在内容行上查术语
            t1 = rows[i + 1][0] if i + 1 < len(rows) else float("inf")
            ja_line = sv.ja_in_range(segs, t0, t1)
            if not ja_line:
                continue
            for t in terms:
                ja, zh = t["ja"], t["zh"]
                if ja not in ja_line:
                    continue
                rec = stat[(ja, zh)]
                rec["hit"] += 1
                hit_here.add((ja, zh))
                t_hit[cls_of[(ja, zh)]] += 1
                if zh in zh_line:
                    rec["conf"] += 1
        for key in hit_here:
            stat[key]["tracks"] += 1

        print("轨 %s [asr=%s]：命中 %d / covered %d / memory_only %d / conflict %d"
              % (stem[:44], ver, sum(t_hit.values()), t_hit["covered"],
                 t_hit["memory_only"], t_hit["conflict"]))
        seen = set()
        for t in terms:
            key = (t["ja"], t["zh"])
            if key in seen:
                continue
            seen.add(key)
            rec = stat[key]
            if rec["hit"]:
                print("  %s→%s  命中 %d/%d  (%s)" % (t["ja"], t["zh"], rec["conf"],
                                                     rec["hit"], cls_of[key]))
        tracks_out.append({"track": stem, "asr_version": ver,
                           "hits_by_class": t_hit,
                           "terms": [{"ja": t["ja"], "zh": t["zh"],
                                      "cls": cls_of[(t["ja"], t["zh"])],
                                      "hit": stat[(t["ja"], t["zh"])]["hit"],
                                      "conf": stat[(t["ja"], t["zh"])]["conf"],
                                      "tracks": stat[(t["ja"], t["zh"])]["tracks"]}
                                     for t in terms]})

    # ---- 汇总 ----
    agg = {c: {"hit": 0, "conf": 0} for c in ("covered", "memory_only", "conflict")}
    n_conflict_terms = 0
    for (ja, zh), c in cls_of.items():
        if c == "conflict":
            n_conflict_terms += 1
        agg[c]["hit"] += stat[(ja, zh)]["hit"]
        agg[c]["conf"] += stat[(ja, zh)]["conf"]
    total_hit = sum(v["hit"] for v in agg.values())
    print("=== 汇总（%d 轨 / %d 条命中）===" % (n_tracks, total_hit))
    for c in ("covered", "memory_only"):
        h = agg[c]["hit"]
        print("%-12s 遵守率 %s  (%s)"
              % (c, ("%.1f%%" % (100.0 * agg[c]["conf"] / h)) if h else "n/a",
                 "基线，参考用" if c == "covered" else "记忆净信号"))
    print("%-12s %d 条（应恒为 0）" % ("conflict", n_conflict_terms))
    for (ja, zh), c in sorted(cls_of.items()):
        if c == "conflict":
            print("  conflict: %s→%s（glossary=%s）" % (ja, zh, gloss.get(ja)))

    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        summary = {c: agg[c] for c in agg}
        summary.update({"n_tracks": n_tracks, "n_conflict_terms": n_conflict_terms})
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"out_dir": a.out_dir, "glossary": a.glossary,
                       "tracks": tracks_out, "summary": summary},
                      f, ensure_ascii=False, indent=1)
        print("已写入：%s" % a.json)


if __name__ == "__main__":
    main()
