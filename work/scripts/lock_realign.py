#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把**丢失的**行级锁按日文锚点重新对齐到新 ASR（换模型后的一次性迁移）。

## 背景（2026-11-08）

换 large-v3 后 RJ1058739 的 22 条锁只落地 16 条。逐条查清：
**4 条的中文在成品里完全不存在**（不是错位，是真丢）。
锚点日文在新 ASR 里仍有（2-gram 覆盖 ≥50%），但 v3 把长句切碎了 ——
锁的完整日文横跨 2~4 个 cue，单 cue 的 `lcs_ratio` 掉到 0.42，被 `MIN_SIM=0.55` 拦掉。

## 为什么不在运行时改 `assign_cues` 做跨 cue 拼接

那会把整段中文盖到**第一个** cue 上，而那个 cue 可能只有 2 秒 ——
**错位比丢失更坏**（MEMORY 明确记过：丢失会被覆盖率门禁报出来，错位看起来一切正常）。

所以改成一次性迁移：把锁的 `ts` 和 `ja` 更新到新 ASR 的对应位置，之后正常匹配。

## 安全约束

- 只处理「`ja` 非空 **且** `zh` 不在成品任何位置」的锁（真丢失的）
- 新位置相似度必须 ≥ 0.6
- 新 `ts` 与旧 `ts` 的距离必须 ≤ 120 秒（防跑到别处去）
- 写盘前自动备份到 `locks/_backup_<日期>/`

用法：
  python scripts/lock_realign.py --rj RJ1058739          # 干跑，只列建议
  python scripts/lock_realign.py --rj RJ1058739 --apply  # 写盘
  python scripts/lock_realign.py --all                   # 全部作品干跑
"""
import argparse
import glob
import io
import json
import os
import re
import shutil
import sys
import time

if hasattr(sys, "stdout") and hasattr(sys, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
from lock_match import norm_ja, sim_ratio  # noqa: E402

ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(SCRIPTS))
LOCKS = os.path.join(ROOT, "data", "locks")
RUNS = os.path.join(ROOT, "runs")
TS = re.compile(r"^\[(\d+):([\d.]+)\]$")
MAX_MOVE = 120.0     # 新 ts 与旧 ts 最大允许距离（秒）
MIN_SIM = 0.60       # 新位置最低相似度


def sec(ts):
    m = TS.match(ts.strip())
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


def fmt(t):
    return "[%02d:%05.2f]" % (int(t) // 60, t - 60 * (int(t) // 60))


def latest_run(rj):
    cands = sorted(glob.glob(os.path.join(RUNS, "*_%s_production" % rj)))
    return cands[-1] if cands else None


def load_segs(run, rj, fn):
    stem = fn[:-4] if fn.lower().endswith(".lrc") else fn
    for name in (stem + ".json", stem + ".mp3.json"):
        p = os.path.join(run, "02_asr_fix", rj, "asr_fix", name)
        if os.path.exists(p):
            try:
                return (json.load(io.open(p, encoding="utf-8")) or {}).get("segments") or []
            except Exception:
                return []
    return []


def lrc_text(run, rj):
    """成品全文（去时间戳），用来判断锁文是否已落地。"""
    buf = []
    for p in glob.glob(os.path.join(run, "05_term_lock", rj, "*.lrc")):
        for line in io.open(p, encoding="utf-8-sig"):
            m = re.match(r"\[(\d+):(\d+\.\d+)\](.*)", line.strip())
            if m and m.group(3).strip():
                buf.append(m.group(3))
    return "\n".join(buf)


def squash(s):
    return re.sub(r"[\s\u3000。、，．\.…‥・!?！？「」『』（）()\[\]【】〈〉《》〜～ー―—\-－~♪♡♥☆★\"'“”‘’]",
                  "", s or "")


def find_best(lock_ja, segs, old_t=None):
    """在新 ASR 段里找锁 ja 的最佳落点。允许 1~3 段拼接。

    返回 (start, sim, text) 或 None。

    ⚠️ 2026-11-08：加了**距离惩罚**。第一版只取 `sim_ratio` 最高的位置，
    结果 18 条丢失锁里有 10 条被匹配到 400~900 秒之外 —— 全是同一个高频片段
    （比如 `見たことのないもの` 在别处也出现）。同分时应该取**离原位置最近**的。
    惩罚上限 0.15，不会把真正的高分匹配挤掉（sim 1.00 vs 0.85 的差距仍保留）。
    """
    best = None
    n = len(segs)
    for i in range(n):
        acc = norm_ja(segs[i].get("text", ""))
        for k in range(0, 3):
            if k > 0:
                if i + k >= n:
                    break
                acc += norm_ja(segs[i + k].get("text", ""))
            if len(acc) > len(lock_ja) * 2.5:
                break
            s = sim_ratio(lock_ja, acc)
            st = float(segs[i].get("start") or 0)
            if old_t is not None:
                s -= min(abs(st - old_t) / 1000.0, 0.15)
            if best is None or s > best[1]:
                best = (st, s, acc)
    return best


def process(p, apply_):
    rj = os.path.basename(p)[:-5]
    run = latest_run(rj)
    if not run:
        print("%-11s 无 run，跳过" % rj)
        return 0, 0
    d = json.load(io.open(p, encoding="utf-8"))
    zh_all = squash(lrc_text(run, rj))
    if not zh_all:
        print("%-11s 成品为空，跳过" % rj)
        return 0, 0
    segs_cache, changes, n_missing = {}, [], 0
    for key, v in list(d.items()):
        if not isinstance(v, dict):
            continue
        zh = (v.get("zh") or "").strip()
        ja = norm_ja(v.get("ja") or "")
        if not zh or not ja or len(ja) < 6:
            continue
        if squash(zh) and squash(zh) in zh_all:
            continue                       # 已落地，不动
        n_missing += 1
        fn, ts = key.split("|", 1) if "|" in key else (key, "")
        old = sec(ts)
        if old is None:
            continue
        if fn not in segs_cache:
            segs_cache[fn] = load_segs(run, rj, fn)
        best = find_best(ja, segs_cache[fn], old)
        if not best or best[1] < MIN_SIM:
            changes.append({"key": key, "zh": zh, "action": "skip",
                            "why": "新 ASR 里找不到（sim=%s）" % (round(best[1], 2) if best else "—")})
            continue
        new_t, sim, new_ja = best
        if abs(new_t - old) > MAX_MOVE:
            changes.append({"key": key, "zh": zh, "action": "skip",
                            "why": "位移 %.0fs > %.0fs" % (abs(new_t - old), MAX_MOVE)})
            continue
        changes.append({"key": key, "zh": zh, "action": "move",
                        "new_key": "%s|%s" % (fn, fmt(new_t)),
                        "old_t": old, "new_t": new_t, "sim": round(sim, 2),
                        "ja_old": v.get("ja", ""), "ja_new": new_ja})

    moves = [c for c in changes if c["action"] == "move"]
    print("%-11s 锁 %3d / 真丢失 %2d / 可迁移 %2d / 放弃 %2d"
          % (rj, len(d), n_missing, len(moves), n_missing - len(moves)))
    for c in changes:
        if c["action"] == "move":
            print("   ✓ %s → %s (sim %.2f, %.1fs→%.1fs)"
                  % (c["key"].split("|")[1], c["new_key"].split("|")[1],
                     c["sim"], c["old_t"], c["new_t"]))
            print("       %s" % c["zh"][:44])
        else:
            print("   ✗ %s  %s" % (c["key"].split("|")[-1], c["why"]))

    if apply_ and moves:
        bak = os.path.join(LOCKS, "_backup_%s" % time.strftime("%Y%m%d"))
        os.makedirs(bak, exist_ok=True)
        shutil.copy2(p, os.path.join(bak, os.path.basename(p)))
        for c in moves:
            v = d.pop(c["key"])
            v["ja"] = c["ja_new"]
            v["ja_norm"] = c["ja_new"]
            v["ja_src"] = "lock_realign:v3"
            v["ts_orig"] = c["key"].split("|")[-1]
            d[c["new_key"]] = v
        json.dump(d, io.open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("   → 已写盘（备份 %s）" % bak)
    return n_missing, len(moves)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rj", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    files = sorted(glob.glob(os.path.join(LOCKS, "RJ*.json")))
    if a.rj:
        files = [f for f in files if os.path.basename(f) == a.rj + ".json"]
    elif not a.all:
        raise SystemExit("要 --rj 或 --all")
    tm = tn = 0
    for p in files:
        m, n = process(p, a.apply)
        tm += m
        tn += n
    print("\n合计：真丢失 %d 条，可迁移 %d 条%s"
          % (tm, tn, "" if a.apply else "（干跑，加 --apply 写盘）"))


if __name__ == "__main__":
    main()
