#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""标准样品自检：跑 samples/ 这一组，输出可对比的指标表。

为什么不用全库重跑（2026-11-07 用户定）：
  17 部 / 147 轨一轮要 3 小时，而大多数改动（译法、判据、切分）在 12 条短轨上
  就能暴露。samples/ 共 62 分钟音频 —— 首次 ASR 约 31 分钟，
  **之后只跑 02-06 约 6 分钟**（01 有 asr_meta，参数一致就复用）。

用法：
  python scripts/selfcheck.py --asr        # 首次 / 改了 01：全流程
  python scripts/selfcheck.py              # 常规：只跑 02-06
  python scripts/selfcheck.py --rj RJ1331975   # 只看一部
  python scripts/selfcheck.py --baseline   # 把本次结果存为基线

指标（都能直接反映代码层/语料层问题）：
  · 行数 / 平均句长       → 切分是否过碎
  · 「…」占比             → 占位符与漏译
  · 含「她/他」行数        → 人称漂移（代理指标，不是正确率）
  · person_audit 风险数   → 高风险候选
  · lexical 各项           → 脱落/省略/音译/拉丁
  · 锁落地率               → 人工修复是否被冲掉
  · 门禁红项（按 kind）    → 可交付 vs 仅审计
"""
import argparse
import glob
import io
import json
import os
import re
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SAMPLES = os.path.join(ROOT, "samples")
RUNS = os.path.join(ROOT, "runs")
PY = sys.executable

KIND = {"empty": "contract", "errata_audit": "contract", "test_regressions": "contract",
        "check": "output", "lexical": "output",
        "scan": "audit", "overlay": "audit", "trunc": "audit", "slot_guard": "audit",
        "locks": "audit", "semantic": "audit", "person_audit": "audit"}


def sample_rjs():
    out = []
    for d in sorted(os.listdir(SAMPLES)):
        p = os.path.join(SAMPLES, d)
        if not os.path.isdir(p) or not re.match(r"^RJ\d+$", d):
            continue
        if glob.glob(os.path.join(p, "*.mp3")) or glob.glob(os.path.join(p, "*.wav")):
            out.append(d)
    return out


# ---------------------------------------------------------------- 价值追踪
# 2026-11-07 用户要求：样品集要能**动态更新**，也能**限定某次只跑指定几个**。
# 价值 = 这个样品每次自检能报出多少问题（报不出问题的样品跑它纯浪费 GPU）。
VALUE_PATH = os.path.join(SAMPLES, "_meta", "value.json")


def load_value():
    if os.path.isfile(VALUE_PATH):
        try:
            return json.load(io.open(VALUE_PATH, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_value(v):
    os.makedirs(os.path.dirname(VALUE_PATH), exist_ok=True)
    json.dump(v, io.open(VALUE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def sample_meta():
    """samples/_meta/samples.json → {rj: [条目, ...]}（同一 RJ 可能有多条轨）。"""
    p = os.path.join(SAMPLES, "_meta", "samples.json")
    out = {}
    if os.path.isfile(p):
        for it in json.load(io.open(p, encoding="utf-8")):
            out.setdefault(it["rj"], []).append(it)
    return out


def hits_of(m):
    """本次自检在这个样品上报出多少「值得看」的东西。"""
    lx = m.get("lex") or {}
    n = sum(lx.get(k, 0) for k in ("content_drop", "ellipsis_only", "translit", "latin2"))
    n += m.get("person_risk", 0)
    n += len(m.get("bad") or [])
    return n


def tier_of(rec):
    """hot / normal / cold。cold = 连续 3 次零问题。"""
    runs = rec.get("runs") or 0
    if rec.get("cold_streak", 0) >= 3:
        return "cold"
    if runs and (rec.get("hits") or 0) / runs >= 1.0:
        return "hot"
    return "normal"


def pick_samples(args):
    """按 --only / --tag / --all / 默认(hot+normal) 决定跑哪些。"""
    allr = sample_rjs()
    if args.only:
        miss = [r for r in args.only if r not in allr]
        if miss:
            print("⚠️ 不在样品集里：%s" % " ".join(miss))
        return [r for r in args.only if r in allr]
    if args.tag:
        meta = sample_meta()
        sel = [rj for rj in allr
               if any(args.tag in (it.get("genre") or "") or
                      any(args.tag in t for t in (it.get("tags") or []))
                      for it in meta.get(rj, []))]
        return sel or allr
    if args.all:
        return allr
    # 默认：跳过 cold
    v = load_value()
    sel = [rj for rj in allr if tier_of(v.get(rj, {})) != "cold"]
    skipped = [rj for rj in allr if rj not in sel]
    if skipped:
        print("（跳过 cold 样品：%s；要跑用 --all）" % " ".join(skipped))
    return sel


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
            best = (key, os.path.basename(p), m)
    return best


def clear_stale_lock():
    """跑每部前清掉 pid 已死的 GPU 锁。

    ⚠️ 2026-11-07 实测：串行跑 12 个样品时，第 3 个起全部 `退出码 2`，
    日志只有一行「[lock] 被占用：pid=29172」，而那个 pid 早已不存在。
    `_pid_alive()` 用 `tasklist` 判活有误判窗口（进程正在退出时仍返回存在），
    锁一旦残留，后面每一部都秒失败 —— **批量自检必须自己兜这一层**。
    """
    lp = os.path.join(ROOT, "data", "tmp", "pipeline_queue", "gpu.lock")
    if not os.path.exists(lp):
        return
    try:
        d = json.load(io.open(lp, encoding="utf-8"))
    except Exception:
        d = {}
    pid = d.get("pid")
    alive = False
    if pid:
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % int(pid)],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=15).stdout
            alive = str(pid) in out
        except Exception:
            alive = False
    if not alive:
        import uuid
        try:
            os.replace(lp, "%s.stale.%s" % (lp, uuid.uuid4().hex))
            print("  [selfcheck] 归档 stale 锁（pid=%s 已不存在）" % pid)
        except OSError:
            pass


def run_stage(rj, rid, asr):
    """跑管线。rid 为空 = 新建 run。"""
    clear_stale_lock()
    if rid:
        cmd = [PY, "-u", os.path.join(ROOT, "pipeline", "pipeline.py"),
               "run", "--run", rid]
        if not asr:
            cmd += ["--from", "02_asr_fix"]
    else:
        cmd = [PY, "-u", os.path.join(ROOT, "pipeline", "pipeline.py"),
               "run", "--project", rj, "--profile", "production",
               "--source", os.path.join(SAMPLES, rj)]
    print("  $ %s" % " ".join(cmd[-6:]), flush=True)
    r = subprocess.run(cmd, cwd=ROOT)
    return r.returncode


def metrics(rid):
    d6 = os.path.join(RUNS, rid, "06_checks")
    out = {"rid": rid}
    # 成品行数/字数/省略号/她
    n = ch = ell = ta = 0
    by_tags = set()
    for p in glob.glob(os.path.join(RUNS, rid, "05_term_lock", "*", "*.lrc")):
        for line in io.open(p, encoding="utf-8-sig"):
            s0 = line.strip()
            if s0.startswith("[by:"):
                by_tags.add(s0)
            m = re.match(r"\[(\d+):(\d+\.\d+)\](.*)", s0)
            if not m or not m.group(3).strip():
                continue
            t = m.group(3)
            n += 1
            ch += len(t)
            if "…" in t:
                ell += 1
            if "她" in t or "他" in t:
                ta += 1
    out.update({"rows": n, "chars": ch, "avg_len": round(ch / n, 2) if n else 0,
                "ellipsis_pct": round(100.0 * ell / n, 1) if n else 0, "pronoun": ta})

    # `[by:]` 标签是否与实际 ASR 模型一致（2026-11-08）
    # 2026-11-06 踩过：`translate.py`/`polish.py` 把 `faster-whisper large-v3` 写死在
    # 标签里，换模型后**每一份成品的 [by:] 都在撒谎**。收敛到 `knobs.asr_tag()` 之后
    # 仍然要有独立检查 —— 「声明 == 实际」不能只靠代码自觉。
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import knobs as _K
        want = _K.asr_tag()
        got = " ".join(sorted(by_tags))
        out["by_ok"] = bool(by_tags) and (want in got)
        out["by_tag"] = (sorted(by_tags)[0][:46] if by_tags else "（无 [by:]）")
        out["by_want"] = want
    except Exception as e:
        out["by_ok"] = None
        out["by_tag"] = "检查失败：%s" % str(e)[:30]

    # ASR 时间空洞（2026-11-08 新增）
    # RJ1458584 实测：v2 在 247.1→277.1 有 **30 秒空白**，而原文那段是角色
    # 在念 `A B C D E F G H`（数罩杯等级）—— v2 全丢，v3 完整保留。
    # 「最大空洞」比「覆盖率」更能反映漏听：覆盖率会被「大段吞静音」掩盖
    # （v2 覆盖率 73% 反而高于 v3 的 66%，但 v2 漏了 30 秒内容）。
    segs = []
    for jp in glob.glob(os.path.join(RUNS, rid, "01_asr", "*", "asr", "*.json")):
        try:
            d = json.load(io.open(jp, encoding="utf-8"))
        except Exception:
            continue
        segs += [(float(s["start"]), float(s["end"])) for s in (d.get("segments") or [])]
    segs.sort()
    if len(segs) > 1:
        gaps = [segs[i + 1][0] - segs[i][1] for i in range(len(segs) - 1)]
        out["asr_max_gap"] = round(max(gaps), 1)
        out["asr_gap5"] = sum(1 for g in gaps if g > 5)

    # 译文 / 日文字数比率（2026-11-08）
    # 日译中的字数比通常在 0.7~1.1（日语有假名和助词，中文更短）。
    # 比率掉到 0.6 以下 → 整体性漏译或翻译被截断；超过 1.3 → 啰嗦/加戏。
    # 这条能抓住「逐行看不出问题、但整部少了一大截」的情况。
    ja_chars = 0
    for jp in glob.glob(os.path.join(RUNS, rid, "02_asr_fix", "*", "asr_fix", "*.json")):
        try:
            d = json.load(io.open(jp, encoding="utf-8"))
        except Exception:
            continue
        ja_chars += sum(len(s.get("text", "")) for s in (d.get("segments") or []))
    if ja_chars and ch:
        out["ja_chars"] = ja_chars
        out["zh_ja_ratio"] = round(float(ch) / ja_chars, 2)
    # lexical
    lp = os.path.join(d6, "lexical_audit.json")
    if os.path.isfile(lp):
        s = json.load(io.open(lp, encoding="utf-8")).get("summary") or {}
        out["lex"] = {k: v for k, v in s.items() if k != "rows"}
    # person
    pp = os.path.join(d6, "person_audit.json")
    if os.path.isfile(pp):
        pj = json.load(io.open(pp, encoding="utf-8"))
        out["person_risk"] = pj.get("risk", 0)
    # gates
    gp = os.path.join(d6, "gates.json")
    if os.path.isfile(gp):
        g = json.load(io.open(gp, encoding="utf-8"))
        bad = [(k, KIND.get(k, "?")) for k, v in g.items() if not v.get("ok")]
        out["deliver_ok"] = not any(kd in ("contract", "output") for _, kd in bad)
        out["bad"] = bad
        lk = g.get("locks", {}).get("summary")
        out["locks"] = str(lk)[:28] if lk else ""
    # 锁落地率（2026-11-08）：130 条人工修复是靠行级锁保住的，
    # 换 ASR 模型 / 改切分都会让锁的时间戳漂移 → 必须每次都看落地率。
    lp = os.path.join(d6, "lock_coverage.json")
    if os.path.isfile(lp):
        lc = json.load(io.open(lp, encoding="utf-8"))
        rows = lc.get("rows") or []
        t = sum(r.get("total", 0) for r in rows)
        land = sum(r.get("landed", 0) for r in rows)
        out["lock_total"] = t
        out["lock_landed"] = land
        out["lock_misplaced"] = sum(r.get("misplaced", 0) for r in rows)
        out["lock_rate"] = round(100.0 * land / t, 1) if t else None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asr", action="store_true", help="跑 01（首次或改了 ASR）")
    ap.add_argument("--rj", nargs="*", default=None, help="（旧参数，等同 --only）")
    ap.add_argument("--only", nargs="*", default=None, help="只跑指定 RJ")
    ap.add_argument("--tag", default="", help="按题材筛选（匹配 genre 或 tags）")
    ap.add_argument("--all", action="store_true", help="含 cold 样品，全跑")
    ap.add_argument("--baseline", action="store_true", help="把本次结果存为基线")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if a.rj and not a.only:
        a.only = a.rj

    rjs = pick_samples(a)
    print("样品 %d 部：%s\n" % (len(rjs), " ".join(rjs)))

    results = []
    for rj in rjs:
        print("=== %s ===" % rj, flush=True)
        lr = latest_run(rj)
        rid = lr[1] if lr else ""
        has01 = bool(rid and glob.glob(os.path.join(RUNS, rid, "01_asr", rj, "asr", "*.json")))
        rc = run_stage(rj, rid if has01 else "", a.asr)
        lr = latest_run(rj)
        if not lr:
            print("  ⚠️ 没有 run")
            continue
        m = metrics(lr[1])
        m["rj"] = rj
        m["rc"] = rc
        m["hits"] = hits_of(m)
        # 价值表：本次之前的历史（用于显示 tier）
        _v = load_value().get(rj, {})
        _runs = (_v.get("runs") or 0) + 1
        _hits = (_v.get("hits") or 0) + m["hits"]
        m["tier"] = tier_of({"runs": _runs, "hits": _hits,
                             "cold_streak": (_v.get("cold_streak") or 0) + 1
                             if m["hits"] == 0 else 0})
        m["avg_hits"] = round(_hits / _runs, 2)
        results.append(m)

    # ---- 输出表 ----
    print("\n%-11s %6s %6s %6s %6s %6s %6s %-18s %-6s %s"
          % ("RJ", "行", "均字", "…%", "代词", "风险", "空洞s", "lexical", "tier", "可交付"))
    for m in results:
        lx = m.get("lex") or {}
        lxs = "脱%d/省%d/音%d" % (lx.get("content_drop", 0), lx.get("ellipsis_only", 0),
                                  lx.get("translit", 0))
        gap = m.get("asr_max_gap", 0)
        gs = "%.1f%s" % (gap, "!" if gap >= 15 else "")
        print("%-11s %6d %6.1f %6.1f %6d %6d %6s %-18s %-6s %s"
              % (m["rj"], m["rows"], m["avg_len"], m["ellipsis_pct"],
                 m["pronoun"], m.get("person_risk", 0), gs, lxs, m.get("tier", ""),
                 "✅" if m.get("deliver_ok") else "❌" + ",".join(k for k, _ in m.get("bad") or [])))
    tot = sum(m["rows"] for m in results)
    print("\n合计 %d 行；可交付 %d / %d"
          % (tot, sum(1 for m in results if m.get("deliver_ok")), len(results)))
    print("「空洞s」= ASR 相邻段最大间隔；≥15s 标 ! —— 原文可能有整段被漏听"
          "（2026-11-08 RJ1458584：v2 有 30s 空洞，丢掉了 A~H 十四个罩杯字母）")

    # `[by:]` 标签撒谎单独报（不占表宽，但绝不能被忽略）
    bad_by = [m for m in results if m.get("by_ok") is False]
    if bad_by:
        print("\n⚠️ `[by:]` 标签与实际 ASR 模型不符（声明≠实际）：")
        for m in bad_by:
            print("  %-11s 实际 %s / 标签 %s" % (m["rj"], m.get("by_want"), m.get("by_tag")))

    # 行级锁落地率：130 条人工修复全靠它，落地率掉了等于修复被冲掉
    bad_lock = [m for m in results
                if (m.get("lock_total") or 0) > 0 and (m.get("lock_rate") or 0) < 85]
    if bad_lock:
        print("\n⚠️ 行级锁落地率 <85%（人工修复可能被上游重跑冲掉）：")
        for m in bad_lock:
            print("  %-11s %d/%d 落地（%.1f%%）错位 %d"
                  % (m["rj"], m.get("lock_landed"), m.get("lock_total"),
                     m.get("lock_rate") or 0, m.get("lock_misplaced")))

    # 译文/日文字数比率异常：整部漏译或啰嗦
    bad_ratio = [m for m in results
                 if m.get("zh_ja_ratio") is not None
                 and not (0.6 <= m["zh_ja_ratio"] <= 1.3)]
    if bad_ratio:
        print("\n⚠️ 译文/日文字数比率越界（正常 0.6~1.3）：")
        for m in bad_ratio:
            print("  %-11s 日文 %d 字 → 中文 %d 字（比率 %.2f）%s"
                  % (m["rj"], m["ja_chars"], m["chars"], m["zh_ja_ratio"],
                     " ← 疑似整体漏译/截断" if m["zh_ja_ratio"] < 0.6 else " ← 疑似啰嗦/加戏"))

    # ---- 更新价值表（样品集动态进化）----
    import datetime
    day = datetime.date.today().strftime("%Y%m%d")
    v = load_value()
    for m in results:
        rj = m["rj"]
        rec = v.setdefault(rj, {"runs": 0, "hits": 0, "cold_streak": 0})
        h = m.get("hits", 0)
        rec["runs"] = (rec.get("runs") or 0) + 1
        rec["hits"] = (rec.get("hits") or 0) + h
        rec["last_hits"] = h
        rec["last"] = day
        rec["cold_streak"] = 0 if h else (rec.get("cold_streak") or 0) + 1
        rec["avg_hits"] = round(rec["hits"] / rec["runs"], 2)
        rec["tier"] = tier_of(rec)
    save_value(v)
    hot = [k for k, x in v.items() if x.get("tier") == "hot"]
    cold = [k for k, x in v.items() if x.get("tier") == "cold"]
    print("价值表：hot %d / cold %d（cold 连续 3 次零问题，默认跳过；要跑用 --all）"
          % (len(hot), len(cold)))

    # ---- 落盘 ----
    out = a.out or os.path.join(ROOT, "internal", "docs", "experiments",
                                "SELFCHECK_%s.md" % day)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    L = ["# 标准样自检 %s" % day, "",
         "| RJ | 行 | 均字 | …%% | 代词 | 风险 | 空洞s | lexical | 可交付 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for m in results:
        lx = m.get("lex") or {}
        gap = m.get("asr_max_gap", 0)
        L.append("| %s | %d | %.1f | %.1f | %d | %d | %.1f%s | 脱%d/省%d/音%d | %s |"
                 % (m["rj"], m["rows"], m["avg_len"], m["ellipsis_pct"], m["pronoun"],
                    m.get("person_risk", 0), gap, "!" if gap >= 15 else "",
                    lx.get("content_drop", 0),
                    lx.get("ellipsis_only", 0), lx.get("translit", 0),
                    "✅" if m.get("deliver_ok") else "❌"))
    L.append("")
    L.append("合计 %d 行；可交付 %d / %d" % (tot, sum(1 for m in results if m.get("deliver_ok")),
                                        len(results)))
    L.append("")
    L.append("> **空洞s** = ASR 相邻段最大间隔，≥15s 标 `!`。原文可能有整段被漏听——"
             "2026-11-08 实测 RJ1458584 的 v2 有 30s 空洞，丢掉了 A~H 十四个罩杯字母。")
    bad_by = [m for m in results if m.get("by_ok") is False]
    L.append("")
    if bad_by:
        L.append("## ⚠️ `[by:]` 标签与实际 ASR 模型不符")
        L.append("")
        for m in bad_by:
            L.append("- **%s**：实际 `%s`，标签 `%s`" % (m["rj"], m.get("by_want"), m.get("by_tag")))
    else:
        L.append("`[by:]` 标签检查：全部与实际 ASR 模型一致。")
    io.open(out, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("报表 → %s" % out)

    if a.baseline:
        bp = os.path.join(SAMPLES, "_meta", "baseline.json")
        json.dump({m["rj"]: m for m in results}, io.open(bp, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("基线 → %s" % bp)


if __name__ == "__main__":
    main()
