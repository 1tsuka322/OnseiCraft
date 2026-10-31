# -*- coding: utf-8 -*-
"""E1 锁覆盖度门禁：登记的锁，有多少条真的出现在成品里。

## 为什么需要这个（2026-10-31 P0）

锁是「7B 模型天花板下守住质量的最后防线」—— 78 条人工修复、4187 条句级锁，
都是人一句一句听出来改出来的。但**没有任何门禁检查它们有没有落地**：
`polish.apply_lock` 匹配不上就返回原文，日志只写「覆写 N 行」，**miss 不报错**。

实测（2026-10-31，17 部新路线作品）：**1032 条锁只有 547 条落地（53%）**，
另一半静默丢失，而且是在 beta.15「全库重跑 + 9 条人工修正」之后。

## 判据（为什么用「子串」而不是「整行相等」）

`polish` 的显示层会把一句长译文按逗号拆成多个显示行，所以整句锁文天然对不上任何**单行**。
把整轨文本去掉标点拼成一串再判子串，是唯一不会误报的判法。
（2026-10-30 用「整行相等」判过一次，报 45.5% 丢失，全是假阳性。）

## 三态（2026-10-31 起分两档「丢失」，因为处置方式不同）

- `landed`：锁文（去标点）是整轨文本的子串 → 落地了
- `missing`：**有日文锚点**却没落地 → **真丢失**，匹配器本该找到它，计入门禁失败
- `no_anchor`：没有日文锚点的锁没落地 → **待人工**，匹配器没有证据可用，
  不能按时间硬套（错位比丢失更坏），只能用 `lock_migrate.py` 补锚或人工核
- `unreadable`：锁文为空 / 该轨 LRC 不存在 → 不算丢失，单独计数

`--max-miss` 只管 `missing`；`no_anchor` 单独报数，不拦门禁。

## 用法

    python lock_coverage.py --lrc <含 RJ 子目录的父目录> --rj RJ1228193
    python lock_coverage.py --all --lrc ../kikoerusubtitle      # 全库
    python lock_coverage.py --all --max-miss 0                  # 有真丢失就退出码 1

退出码：`missing > max_miss` → 1。默认 `max_miss=0`（有锚点的一条都不许丢）。
"""
import argparse
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rootloc  # noqa: E402

LOCKS = os.path.join(rootloc.DATA, "locks")
TS = re.compile(r"^\[(\d+):([\d.]+)\](.*)$")
PUNCT = re.compile(r"[\s、。，,．\.！!？\?…~～ー—\-·♡♪☆★「」『』（）()\[\]【】\"'“”‘’]")


def squash(s):
    return PUNCT.sub("", s or "")


def track_text(lrc_dir, fn):
    """该轨成品的全文（去标点）。fn 可带或不带 .lrc。"""
    p = os.path.join(lrc_dir, fn if fn.endswith(".lrc") else fn + ".lrc")
    if not os.path.exists(p):
        return None
    buf = []
    for line in open(p, encoding="utf-8"):
        m = TS.match(line.strip())
        if m:
            buf.append(m.group(3))
    return squash("".join(buf))


def audit_one(rj, lrc_root):
    lp = os.path.join(LOCKS, rj + ".json")
    if not os.path.exists(lp):
        return None
    try:
        d = json.load(open(lp, encoding="utf-8"))
    except Exception as e:
        return {"rj": rj, "error": str(e)}
    lrc_dir = os.path.join(lrc_root, rj)
    cache = {}
    landed, missing, no_anchor, unreadable = [], [], [], []
    for k, v in (d or {}).items():
        if "|" not in k:
            continue
        fn, ts = k.split("|", 1)
        if fn.lower().endswith(".lrc"):
            fn = fn[:-4]
        zh = (v.get("zh") if isinstance(v, dict) else str(v)) or ""
        ja = (v.get("ja") or "") if isinstance(v, dict) else ""
        z = squash(zh)
        if not z:
            unreadable.append({"key": k, "why": "锁文为空"})
            continue
        if fn not in cache:
            cache[fn] = track_text(lrc_dir, fn)
        txt = cache[fn]
        if txt is None:
            unreadable.append({"key": k, "why": "成品里没有这一轨"})
            continue
        # ⚠️ 不做「去说话人前缀」的宽容匹配：`甘：`/`白：` 是**台本的角色归属**，
        # 属于内容而不是格式。锁里有、成品里没有 = 真的丢了，必须如实报出来。
        # （2026-10-31 一度按「格式开关」去剥，被用户当场纠正——那会静默删掉
        #  人工锁里的说话人标签。）
        if z in txt:
            landed.append(k)
        else:
            rec = {"key": k, "zh": zh[:60], "ja": ja[:60]}
            (missing if ja.strip() else no_anchor).append(rec)
    return {"rj": rj, "total": len(landed) + len(missing) + len(no_anchor) + len(unreadable),
            "landed": len(landed), "missing": len(missing),
            "no_anchor": len(no_anchor),
            "unreadable": len(unreadable),
            "miss_detail": missing[:200], "no_anchor_detail": no_anchor[:200],
            "unreadable_detail": unreadable[:40]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lrc", default=rootloc.PROD, help="含 RJ 子目录的父目录")
    ap.add_argument("--rj", action="append", help="只查这些作品（可多次）")
    ap.add_argument("--all", action="store_true", help="全库")
    ap.add_argument("--max-miss", type=int, default=0, help="允许的丢失条数上限（默认 0）")
    ap.add_argument("--json", help="把明细写成 json")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if a.rj:
        rjs = a.rj
    elif a.all:
        # 只认 RJ 号：`data/locks/` 里还躺着 out.json 之类的临时产物
        rjs = sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(LOCKS, "*.json"))
                     if re.match(r"^RJ\d+$", os.path.basename(p)[:-5]))
    else:
        ap.error("要给 --rj 或 --all")

    rows, tot, miss, noanc = [], 0, 0, 0
    for rj in rjs:
        r = audit_one(rj, a.lrc)
        if not r:
            continue
        if r.get("error"):
            print("⚠️ %s 锁文件读不了：%s" % (rj, r["error"]))
            continue
        rows.append(r)
        tot += r["total"]
        miss += r["missing"]
        noanc += r["no_anchor"]

    if not a.quiet:
        print("%-11s %6s %6s %6s %8s %6s %7s"
              % ("作品", "锁数", "落地", "丢失", "无锚点", "不可判", "落地率"))
        for r in rows:
            rate = (100.0 * r["landed"] / r["total"]) if r["total"] else 0.0
            print("%-11s %6d %6d %6d %8d %6d %6.1f%%"
                  % (r["rj"], r["total"], r["landed"], r["missing"],
                     r["no_anchor"], r["unreadable"], rate))
        print("\n合计 %d 条锁：落地 %d / 丢失 %d / 无锚点待人工 %d / 不可判 %d"
              % (tot, tot - miss - noanc - sum(r["unreadable"] for r in rows),
                 miss, noanc, sum(r["unreadable"] for r in rows)))
        bad = [r for r in rows if r["missing"]]
        if bad:
            print("\n丢失明细（有锚点却没落地，每部最多列 5 条，看 json 拿全量）：")
            for r in bad[:8]:
                print("  === %s（%d 条）===" % (r["rj"], r["missing"]))
                for m in r["miss_detail"][:5]:
                    print("    %s" % m["key"][:70])
                    print("      锁: %s" % m["zh"])
                    print("      日: %s" % m["ja"])
        na = [r for r in rows if r["no_anchor"]]
        if na:
            print("\n无锚点待人工（没有日文，匹配器无证据可用；见 lock_migrate.py）：")
            for r in na[:8]:
                print("  %s：%d 条" % (r["rj"], r["no_anchor"]))

    if a.json:
        json.dump({"lrc_root": a.lrc, "max_miss": a.max_miss,
                   "total": tot, "missing": miss, "no_anchor": noanc, "rows": rows},
                  open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    if miss > a.max_miss:
        print("\n❌ 锁覆盖度不足：%d 条有锚点的锁没落地（上限 %d）" % (miss, a.max_miss))
        return 1
    print("\n✅ 锁覆盖度：%d 条锁落地，有锚点的一条不丢（无锚点 %d 条待人工）" % (tot, noanc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
