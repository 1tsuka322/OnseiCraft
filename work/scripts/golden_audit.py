#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""语料探针评分（Golden Audit）：跑探针库，出四维分数 + PASS/WARN/FAIL。

方法论来自 Gemini 2026-11-07 的建议，但按本项目实测做了修正：
  · 不预设「基线 94 分」——先测真实基线
  · 权重 PFI .35 / DLR .30 / EHR .20 / SA .15
  · **负向禁绝优先**（不扼杀意译多样性）
  · 探针三阶：active 必跑 / staged 观察 / archived 抽样

与 selfcheck.py 的分工：
  selfcheck  = 跑管线 + 出原始指标（行数/lexical/风险）
  golden_audit = 读成品 + 执行断言 + 打分（不跑管线，秒级）

用法：
  python scripts/golden_audit.py                 # 用当前成品打分
  python scripts/golden_audit.py --json out.json # 另存明细
"""
import argparse
import glob
import io
import json
import os
import re
import sys

if hasattr(sys, "stdout") and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SAMPLES = os.path.join(ROOT, "samples")
RUNS = os.path.join(ROOT, "runs")
PROBES = os.path.join(SAMPLES, "_meta", "probes.json")


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


def lrc_lines(rid, rj):
    """成品所有非空行（去掉时间戳）。"""
    out = []
    for p in glob.glob(os.path.join(RUNS, rid, "05_term_lock", rj, "*.lrc")):
        for line in io.open(p, encoding="utf-8-sig"):
            m = re.match(r"\[(\d+):(\d+\.\d+)\](.*)", line.strip())
            if m and m.group(3).strip():
                out.append((os.path.basename(p), m.group(3)))
    return out


def ja_text(rid, rj):
    """该 run 的日文（02_asr_fix 全轨拼起来），供「原文有什么 → 译文必须有什么」类判据用。"""
    buf = []
    for jp in glob.glob(os.path.join(RUNS, rid, "02_asr_fix", rj, "asr_fix", "*.json")):
        try:
            d = json.load(io.open(jp, encoding="utf-8"))
        except Exception:
            continue
        buf += [s.get("text", "") for s in (d.get("segments") or [])]
    return "".join(buf)


def sample_rjs():
    out = []
    for d in sorted(os.listdir(SAMPLES)):
        p = os.path.join(SAMPLES, d)
        if os.path.isdir(p) and re.match(r"^RJ\d+$", d):
            if glob.glob(os.path.join(p, "*.mp3")) or glob.glob(os.path.join(p, "*.wav")):
                out.append(d)
    return out


def run_probe(pr, all_rjs, cache):
    """返回 (得分 0-100, 明细)。"""
    scope = pr.get("scope") or all_rjs
    targets = [rj for rj in scope if rj in all_rjs]
    if not targets:
        return None, {"skip": "scope 里的 RJ 不在样品集"}

    viol = []
    for rj in targets:
        rid = latest_run(rj)
        if not rid:
            continue
        if rj not in cache:
            cache[rj] = lrc_lines(rid, rj)
        lines = cache[rj]

        # 1) 负向词表
        for n in pr.get("negative") or []:
            for fn, t in lines:
                if n in t:
                    viol.append({"rj": rj, "hit": n, "text": t[:40], "file": fn})

        # 2) 负向正则
        rx = pr.get("negative_regex")
        if rx:
            pat = re.compile(rx)
            for fn, t in lines:
                if pat.search(t):
                    viol.append({"rj": rj, "hit": "regex:" + rx, "text": t[:40], "file": fn})

        # 3) 正向要求（至少出现一次）
        pos = pr.get("positive") or []
        if pos:
            got = any(p in t for _, t in lines for p in pos)
            if not got:
                viol.append({"rj": rj, "hit": "缺少正向锚定 %s" % "/".join(pos[:3]),
                             "text": "", "file": ""})

        # 4) 数字/字母保留
        # ⚠️ 2026-11-07：原判据是「含字母数字的行占比 ≥3%」，太脆 ——
        # RJ1458584 只有 2.8%（罩杯只在 5 行里出现）就被判违规，
        # 但实际 `L…M…` `M罩杯` 全都保留了，是**阈值误报**。
        # 改成条件式（和 DLR-02 同思路）：原文出现的字母/数字 token，
        # 译文里必须找得到。只抓**大写字母与数字** —— 小写字母大概率是
        # 网络用语 `w`/`ww`，04 已经确定性剔除，原文有译文没有才是对的。
        if pr.get("require_alnum"):
            ja_all = ja_text(rid, rj)
            toks = sorted(set(re.findall(r"\d+|[A-Z]{1,3}", ja_all)))
            zh_all = "".join(t for _, t in lines)
            miss = [t for t in toks if t not in zh_all]
            if miss:
                viol.append({"rj": rj, "hit": "原文有 %s，译文未保留" % "/".join(miss[:5]),
                             "text": "", "file": ""})
        elif pr.get("require_digit"):
            ja_all = ja_text(rid, rj)
            toks = sorted(set(re.findall(r"\d+", ja_all)))
            zh_all = "".join(t for _, t in lines)
            miss = [t for t in toks if t not in zh_all]
            if miss:
                viol.append({"rj": rj, "hit": "原文有数字 %s，译文未保留" % "/".join(miss[:5]),
                             "text": "", "file": ""})

        # 4b) 原文有生殖词 → 译文必须有对应词（降级检测）
        jaw = pr.get("require_positive_when_ja")
        if jaw:
            ja_txt = []
            for jp in glob.glob(os.path.join(RUNS, rid, "02_asr_fix", rj, "asr_fix", "*.json")):
                try:
                    d = json.load(io.open(jp, encoding="utf-8"))
                except Exception:
                    continue
                ja_txt += [s.get("text", "") for s in (d.get("segments") or [])]
            ja_all = "".join(ja_txt)
            if any(w in ja_all for w in jaw):
                ZH_OK = ("肉棒", "鸡鸡", "小鸡鸡", "小穴", "阴茎", "精液", "精子", "肉刃", "凶器")
                if not any(w in t for _, t in lines for w in ZH_OK):
                    viol.append({"rj": rj, "hit": "原文有生殖词，译文全篇无对应词（疑似降级）",
                                 "text": "", "file": ""})

        # 5) 纯标点碎行（lim=0 时只抓「去掉标点后为空」）
        # ⚠️ 2026-11-07：**纯省略号行不算碎行** —— 那是 `fill_timeline` 补时间轴的
        # 占位符（MEMORY 有记：补完「…」后不能再用 --from 01/03/04 重跑）。
        # 只抓「？/！」这类真·标点孤片，它们才是切分或漏译的信号。
        # 实测：RJ1260535 121.78 原文是 17.8 秒的 `お?あ、いけいけ、出せ出せ…`，
        # 译文只有「？」——整段漏译，探针抓到是对的。
        if pr.get("max_len_for_punct_only") is not None:
            lim = pr["max_len_for_punct_only"]
            for fn, t in lines:
                core = re.sub(r"[\s，。！？…~♡♪、,.!?ー～]", "", t)
                if len(core) > lim:
                    continue
                if not re.search(r"[？！?!]", t):
                    continue        # 纯省略号占位符，跳过
                viol.append({"rj": rj, "hit": "纯标点行", "text": t[:12], "file": fn})

    # 打分：无违规 100；每处扣 20，最低 0
    score = max(0, 100 - 20 * len(viol))
    return score, {"viol": viol[:20], "n_viol": len(viol), "targets": targets}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    spec = json.load(io.open(PROBES, encoding="utf-8"))
    all_rjs = sample_rjs()
    cache = {}

    dims = {k: {"scores": [], "viol": 0, "probes": 0} for k in spec["weights"]}
    detail = []
    p0_fail = []

    print("样品 %d 部：%s\n" % (len(all_rjs), " ".join(all_rjs)))
    print("%-9s %-5s %-8s %6s %s" % ("探针", "维度", "严重度", "得分", "说明"))
    for pr in spec["probes"]:
        if pr.get("status") == "archived":
            continue
        sc, info = run_probe(pr, all_rjs, cache)
        if sc is None:
            print("%-9s %-5s %-8s %6s %s" % (pr["id"], pr["dim"], pr["severity"], "—",
                                             info.get("skip", "")))
            continue
        dims[pr["dim"]]["scores"].append(sc)
        dims[pr["dim"]]["viol"] += info["n_viol"]
        dims[pr["dim"]]["probes"] += 1
        mark = "✅" if sc == 100 else ("⚠️" if sc >= 60 else "❌")
        note = pr["scene"] if sc == 100 else "%d 处：%s" % (
            info["n_viol"], "、".join(sorted(set(v["hit"] for v in info["viol"]))[:3]))
        print("%-9s %-5s %-8s %6d %s %s" % (pr["id"], pr["dim"], pr["severity"], sc, mark, note))
        detail.append({"id": pr["id"], "dim": pr["dim"], "severity": pr["severity"],
                       "score": sc, **info})
        if pr["severity"] == "P0" and info["n_viol"] > 0:
            p0_fail.append(pr["id"])

    # ---- 四维 + 总分 ----
    print("\n=== 维度得分 ===")
    total = 0.0
    dim_score = {}
    for k, w in spec["weights"].items():
        s = sum(dims[k]["scores"]) / len(dims[k]["scores"]) if dims[k]["scores"] else 100.0
        dim_score[k] = round(s, 1)
        total += s * w
        print("  %-4s 权重 %.2f  得分 %6.1f  探针 %d 个，违规 %d 处"
              % (k, w, s, dims[k]["probes"], dims[k]["viol"]))
    total = round(total, 1)
    print("\n总分 %.1f" % total)

    # ---- 判定 ----
    if p0_fail:
        verdict = "FAIL（P0 未过：%s）" % ",".join(p0_fail)
    elif total >= 93:
        verdict = "PASS"
    elif total >= 85:
        verdict = "WARN（需人工复核）"
    else:
        verdict = "FAIL（总分 < 85）"
    print("判定：%s" % verdict)

    if a.json:
        json.dump({"version": spec["version"], "dims": dim_score, "total": total,
                   "verdict": verdict, "p0_fail": p0_fail, "detail": detail},
                  io.open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("明细 → %s" % a.json)


if __name__ == "__main__":
    main()
