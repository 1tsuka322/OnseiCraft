# -*- coding: utf-8 -*-
"""给 E1 锁补 `ja` / `ja_norm`（一次性迁移，2026-10-26）。

背景：锁只按「文件|时间戳」存，重跑 ASR 后时间戳变了就认不回来。
补上「这句日文是什么」之后，`lock_match.lookup` 才能在时间戳漂移时按日文认回
（**只在该文件内唯一时**，重复句一律报失配）。

用法：
  python lock_migrate.py            # 干跑：只报能补多少
  python lock_migrate.py --apply    # 写盘（先备份到 locks/_backup_<日期>/）
"""
import glob
import json
import os
import re
import shutil
import sys
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
from lock_match import norm_ja          # noqa: E402

ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCKS = os.path.join(ROOT, ".workbuddy", "locks")
DRAFT = os.path.join(ROOT, ".workbuddy", "draft")
TS = re.compile(r"^\[(\d+):([\d.]+)\]$")


def sec(ts):
    m = TS.match(ts.strip())
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


def segs_of(rj, fn):
    """按优先级找该轨的 ASR 段：asr_v1（成品多对齐它）→ asr。"""
    stem = fn[:-4] if fn.lower().endswith(".lrc") else fn
    for sub in ("asr_v1_20261020", "asr"):
        for name in (stem + ".json", stem + ".mp3.json"):
            p = os.path.join(DRAFT, rj, sub, name)
            if os.path.exists(p):
                try:
                    return (json.load(open(p, encoding="utf-8")) or {}).get("segments") or []
                except Exception:
                    return []
    return []


def ja_at(segs, t, tol=2.0):
    """t 落在哪个段里；都不落在就取 2 秒内最近的段。"""
    best, bd = None, 1e9
    for s in segs:
        a = float(s.get("start") or 0)
        b = float(s.get("end") or a + 0.01)
        if a - 0.05 <= t < b:
            return s.get("text") or ""
        d = min(abs(a - t), abs(b - t))
        if d < bd:
            best, bd = s, d
    return (best.get("text") or "") if (best is not None and bd <= tol) else ""


def main():
    apply_ = "--apply" in sys.argv
    files = sorted(glob.glob(os.path.join(LOCKS, "*.json")))
    files = [f for f in files if not os.path.basename(f).startswith("_")]
    tot = filled = already = 0
    per = []
    for p in files:
        rj = os.path.basename(p)[:-5]
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            print("跳过 %s：%s" % (rj, e))
            continue
        n_new = n_have = n_miss = 0
        for k, v in d.items():
            if "|" not in k or not isinstance(v, dict):
                continue
            fn, ts = k.split("|", 1)
            tot += 1
            if (v.get("ja") or "").strip():
                already += 1
                n_have += 1
                continue
            t = sec(ts)
            ja = ja_at(segs_of(rj, fn), t) if t is not None else ""
            if ja.strip():
                v["ja"] = ja.strip()
                v["ja_norm"] = norm_ja(ja)
                filled += 1
                n_new += 1
            else:
                n_miss += 1
        per.append((rj, len(d), n_new, n_have, n_miss))
        if apply_ and (n_new or n_have):
            os.makedirs(os.path.join(LOCKS, "_backup_20261026"), exist_ok=True)
            shutil.copy2(p, os.path.join(LOCKS, "_backup_20261026",
                                         os.path.basename(p)))
            json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("%-11s %6s %6s %6s %6s" % ("作品", "锁数", "新补", "已有", "补不到"))
    for r in per:
        print("%-11s %6d %6d %6d %6d" % r)
    print("\n合计 %d 条锁：新补 %d / 已有 %d / 补不到 %d%s"
          % (tot, filled, already, tot - filled - already,
             "" if apply_ else "（干跑，加 --apply 写盘）"))
    if not apply_:
        return 0
    print("备份目录：%s" % os.path.join(LOCKS, "_backup_20261026"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
