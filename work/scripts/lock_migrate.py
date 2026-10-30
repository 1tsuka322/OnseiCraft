# -*- coding: utf-8 -*-
"""给 E1 锁补 `ja` / `ja_norm`（文本锚定的燃料）。

## 背景

锁只按「文件|时间戳」存，重跑 ASR / 改切分后时间戳漂 0.3~1.6s 就认不回来。
`lock_match.assign_cues` 靠「锁的日文 vs cue 的日文」相似度重新定位，
所以锁里必须带 ja，否则只能靠精确时间戳（漂一点就丢）。

## ⚠️ 只给「已验证对齐」的锁补 ja（2026-10-31 首次链路验证的教训）

朴素做法是「拿该时间戳的 ASR 文本填进去」。这在**锁本身已经错位**时是有害的：
RJ01654115 的锁 `[05:51.22]` 文案是「啊 不行 不能随便把任何东西交给别人…」，
而该时刻 ASR 是 `おまんこおかしくなっちゃう`（小穴要变得奇怪了）—— 两者毫无关系。
按时间填 ja 会让这对 (zh, ja) **看起来完全一致**，匹配器于是信心十足地把
一句不相干的中文盖到那一行上。**错位比丢失更坏**：丢失会被覆盖率门禁报出来。

所以补 ja 前先做一致性校验：**该锁的 zh 必须已经出现在成品 LRC 里**（去标点子串）。
zh 落地了 ⇒ ts 指的就是它自己那行 ⇒ 此刻该位置的 ASR 就是它的 ja。
不满足的一律不填，留给覆盖率门禁报出来交人工。

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
sys.stdout.reconfigure(encoding="utf-8")
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
from lock_match import norm_ja, sim_ratio   # noqa: E402

ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCKS = os.path.join(ROOT, "data", "locks")
DRAFT = os.path.join(ROOT, "data", "draft")
RUNS = os.path.join(ROOT, "runs")
TS = re.compile(r"^\[(\d+):([\d.]+)\]$")


def sec(ts):
    m = TS.match(ts.strip())
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


def segs_of(rj, fn):
    """按优先级找该轨的 ASR 段。

    ⚠️ 2026-10-31（P0）改：**先查 run 的 02_asr_fix**，再退回 data/draft。
    原来只读 `data/draft`，那是 **v1 旧 ASR**；成品的 ja 早已是 02 改错后的版本，
    用旧 ASR 补出来的 ja 和新 cue 对不上，文本锚定会直接锚空。
    （memory 里「data/draft 是旧 ASR，漏算一半」说的就是这件事。）
    """
    stem = fn[:-4] if fn.lower().endswith(".lrc") else fn
    cands = sorted(glob.glob(os.path.join(RUNS, "*_%s_production" % rj)))
    for run in reversed(cands):
        for name in (stem + ".json",):
            p = os.path.join(run, "02_asr_fix", rj, "asr_fix", name)
            if os.path.exists(p):
                try:
                    return (json.load(open(p, encoding="utf-8")) or {}).get("segments") or []
                except Exception:
                    break
    for sub in ("asr_v1_20261020", "asr"):
        for name in (stem + ".json", stem + ".mp3.json"):
            p = os.path.join(DRAFT, rj, sub, name)
            if os.path.exists(p):
                try:
                    return (json.load(open(p, encoding="utf-8")) or {}).get("segments") or []
                except Exception:
                    return []
    return []


def ja_at(segs, t, back=4.0, fwd=3.0, maxn=3):
    """取覆盖 t 的日文：起点落在 [t-back, t+fwd] 的段，最多拼 maxn 段。

    为什么要拼接：一个 cue 常跨 2~3 条 ASR 段，只取「命中那一条」会让锁的 ja
    短于 cue 的 ja；`lock_match.sim_ratio` 用 lcs/min(len)，短的一侧被包含时=1.0，
    所以**宁可比 cue 长/全，不要缺**。
    """
    if not segs or t is None:
        return ""
    near = [s for s in segs if -back <= (float(s.get("start") or 0) - t) <= fwd]
    if not near:
        # 兜底：2 秒内最近的段（时间轴整体漂移时用）
        best, bd = None, 1e9
        for s in segs:
            a = float(s.get("start") or 0)
            b = float(s.get("end") or a + 0.01)
            d = 0.0 if a <= t < b else min(abs(a - t), abs(b - t))
            if d < bd:
                best, bd = s, d
        if best is None or bd > 2.0:
            return ""
        near = [best]
    near.sort(key=lambda s: float(s.get("start") or 0))
    return "".join((s.get("text") or "") for s in near[:maxn]).strip()


PROD = os.path.join(os.path.dirname(ROOT), "kikoerusubtitle")
PUNCT = re.compile(r"[\s、。，,．\.！!？\?…~～ー—\-·♡♪☆★「」『』（）()\[\]【】\"'“”‘’]")


def prod_rows(rj, fn, cache={}):
    """成品该轨 [(秒, 文本)]。用来验证「这条锁确实还指着它自己那行」。"""
    key = (rj, fn)
    if key in cache:
        return cache[key]
    stem = fn[:-4] if fn.lower().endswith(".lrc") else fn
    p = os.path.join(PROD, rj, stem + ".lrc")
    out = None
    if os.path.exists(p):
        out = []
        for line in open(p, encoding="utf-8"):
            m = re.match(r"^\[(\d+):(\d+\.\d+)\](.*)$", line.strip())
            if m:
                out.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3)))
    cache[key] = out
    return out


def aligned(rj, fn, ts, zh, window=8.0, min_sim=0.5):
    """这条锁的时间戳还指着它自己的那句话吗？

    两道证据，任一成立即算对齐：
    1. **整轨子串**：锁文（去标点）出现在成品全文里 —— 最硬，说明它真的落地了。
    2. **邻域相似**：±`window` 秒内某一行与锁文的相似度 ≥ `min_sim`。
       这一条专治「锁落地过、但被 term_lock 按多数票改写了」的情形：
       锁写「连肉棒都不给撸一撸吗？」，成品是「连鸡鸡都不给撸一撸吗？」——
       整轨子串判不出来（差一个词），相似度 0.72 能认出来。

    为什么需要这两道：没有它们就只能按 ts 硬填 ja，而**错位的锁**（ts 早已
    漂到别的句子上）会被填成「看起来完全一致」的假配对，匹配器就会把一句
    不相干的中文盖到那一行 —— 比不补更糟。
    """
    rows = prod_rows(rj, fn)
    if rows is None:
        return False
    z = PUNCT.sub("", zh or "")
    if not z:
        return False
    if z in PUNCT.sub("", "".join(t for _t, t in rows)):
        return True
    t0 = sec(ts)
    if t0 is None:
        return False
    for t, txt in rows:
        if abs(t - t0) > window:
            continue
        if sim_ratio(z, PUNCT.sub("", txt)) >= min_sim:
            return True
    return False


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
        n_new = n_have = n_miss = n_unaligned = 0
        for k, v in d.items():
            if "|" not in k or not isinstance(v, dict):
                continue
            fn, ts = k.split("|", 1)
            tot += 1
            if (v.get("ja") or "").strip():
                already += 1
                n_have += 1
                continue
            zh = (v.get("zh") or "").strip()
            if not zh or not aligned(rj, fn, ts, zh):
                n_unaligned += 1
                continue
            t = sec(ts)
            ja = ja_at(segs_of(rj, fn), t) if t is not None else ""
            if ja.strip():
                v["ja"] = ja.strip()
                v["ja_norm"] = norm_ja(ja)
                v["ja_src"] = "lock_migrate:aligned"
                filled += 1
                n_new += 1
            else:
                n_miss += 1
        per.append((rj, len(d), n_new, n_have, n_unaligned, n_miss))
        if apply_ and n_new:
            os.makedirs(os.path.join(LOCKS, "_backup_20261031"), exist_ok=True)
            shutil.copy2(p, os.path.join(LOCKS, "_backup_20261031",
                                         os.path.basename(p)))
            json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("%-11s %6s %6s %6s %9s %7s"
          % ("作品", "锁数", "新补", "已有", "未落地跳过", "无ASR"))
    for r in per:
        print("%-11s %6d %6d %6d %9d %7d" % r)
    print("\n合计 %d 条锁：新补 %d / 已有 %d / 未落地跳过 %d / 无ASR %d%s"
          % (tot, filled, already, sum(r[4] for r in per), sum(r[5] for r in per),
             "" if apply_ else "（干跑，加 --apply 写盘）"))
    if not apply_:
        return 0
    print("备份目录：%s" % os.path.join(LOCKS, "_backup_20261031"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
