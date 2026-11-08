#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把人工确认的 repair plan 转成行级 lock（只读 plan，写 locks）。

为什么必须转（2026-11-07）：
  `repair --apply` 改的是 05_term_lock 的产物，而 manifest 只记
  `{at, n, plan: "tmp/daily/xxx.json"}` —— **实质内容只存在 tmp 里**。
  任何一次上游重跑（改 errata / 改 glossary / 改 04 代码）都会覆盖 05，
  130 处人工修复全部消失，而 manifest 还留着一条「修过 10 行」的记录，
  看起来像修过。

  GPT 2026-11-07 B3/B4 的建议正是这条：人工确认的修复要进**行级锁**
  （05 会自动应用），并绑定原译文，内容变化时拒绝盲目重放。

lock 格式（data/style/locks/<RJ>.json）：
  "文件名.mp3.lrc|[03:51.36]": {"zh": "锁定译文", "ja": "日文锚点", "old": "原译文"}

用法：
  python tmp/daily/repair_to_lock.py --dry     # 只看会写什么
  python tmp/daily/repair_to_lock.py           # 真写（自动备份）
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
# ⚠️ 2026-11-08 修：原来多 dirname 了一层，指到 repo 根（那里没有 data/locks 也没有 tmp/daily），
# 导致「缺 plan」全报错、一条锁也没写成。与 paths.ROOT 同口径：work 才是 ROOT。
ROOT = os.path.dirname(HERE)
LOCKS = os.path.join(ROOT, "data", "locks")   # 与 lock_coverage.py 同口径（不是 data/style/locks）

# plan 文件 → run_id（人工确认过，写死比猜安全）
PLAN_RUN = {
    "RJ1058739": "20261028-201043_RJ1058739_production",
    "RJ1623028": "20261028-203424_RJ1623028_production",
    "RJ01654115": "20261029-010039_RJ01654115_production",
    "RJ426223": "20261029-015605_RJ426223_production",
    "RJ1019849": "20261029-024410_RJ1019849_production",
    "RJ416595": "20261029-040134_RJ416595_production",
    "RJ1065777": "20261029-054251_RJ1065777_production",
}


def ts_key(start):
    """231.36 → [03:51.36]"""
    m, s = divmod(float(start), 60)
    return "[%02d:%05.2f]" % (int(m), s)


def read_lrc_map(path):
    """{秒(取2位): 中文} —— 用来抓「原译文」做绑定。"""
    out = {}
    if not os.path.exists(path):
        return out
    for line in io.open(path, encoding="utf-8-sig"):
        mm = re.match(r"\[(\d+):(\d+\.\d+)\](.*)", line.strip())
        if mm:
            out[round(int(mm.group(1)) * 60 + float(mm.group(2)), 2)] = mm.group(3)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--plan", action="append", default=[],
                    help="指定 plan 文件（可多次）。不给则扫默认的 repair_person_<RJ>.json")
    a = ap.parse_args()

    # ---- 构造待处理清单 (plan_path, rj, run_id) ----
    items = []
    if a.plan:
        for pp in a.plan:
            cand = [pp, os.path.join(ROOT, pp)]
            path = next((c for c in cand if os.path.exists(c)), None)
            if not path:
                print("⚠️ 找不到 plan：%s" % pp)
                continue
            m = re.search(r"(RJ\d+)", os.path.basename(path))
            if not m:
                print("⚠️ 文件名里没有 RJ 号：%s" % os.path.basename(path))
                continue
            rj = m.group(1)
            rid = PLAN_RUN.get(rj)
            if not rid:
                found = sorted(glob.glob(os.path.join(ROOT, "runs", "*_%s_production" % rj)))
                if not found:
                    print("⚠️ 找不到 %s 的 run" % rj)
                    continue
                rid = os.path.basename(found[-1])
            items.append((path, rj, rid))
    else:
        for rj, rid in PLAN_RUN.items():
            items.append((os.path.join(ROOT, "tmp", "daily", "repair_person_%s.json" % rj), rj, rid))

    total = 0
    for pp, rj, rid in items:
        if not os.path.exists(pp):
            print("⚠️ 缺 plan：%s" % pp)
            continue
        plan = json.load(io.open(pp, encoding="utf-8"))
        lp = os.path.join(LOCKS, rj + ".json")
        locks = {}
        if os.path.exists(lp):
            locks = json.load(io.open(lp, encoding="utf-8"))
        before = len(locks)

        # 原译文：从 04_polish 读（repair 前的样子），抓不到就留空
        d04 = os.path.join(ROOT, "runs", rid, "04_polish", rj)
        added = []
        for it in plan:
            track = it["track"]
            key = "%s|%s" % (track, ts_key(it["start"]))
            old = read_lrc_map(os.path.join(d04, track)).get(round(float(it["start"]), 2), "")
            rec = {"zh": it["zh"]}
            if it.get("ja"):
                rec["ja"] = it["ja"]
            if old:
                rec["old"] = old
            locks[key] = rec
            added.append(key)

        print("%-11s plan %2d 条 → locks %d → %d 条" % (rj, len(plan), before, len(locks)))
        total += len(added)
        if a.dry:
            for k in added[:3]:
                print("    %s  →  %s" % (k[:60], locks[k]["zh"][:30]))
            continue
        os.makedirs(LOCKS, exist_ok=True)
        if os.path.exists(lp):
            shutil.copy2(lp, "%s.bak_%s" % (lp, time.strftime("%Y%m%d")))
        json.dump(locks, io.open(lp, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    print("\n合计写入 %d 条 lock%s" % (total, "（dry-run，未写盘）" if a.dry else ""))


if __name__ == "__main__":
    main()
