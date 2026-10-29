# -*- coding: utf-8 -*-
"""时间轴补全：把「整行消失」的 ASR 段补成「…」占位。

## 为什么需要（R155 实测）

一个 cue 里塞了两句时，Sakura 是纯翻译模型，**只翻前半句、后半句直接丢**：
    `なんでこんな感じなの` + `フフフフ ママゴジがガギャギャ…`
    → 译文只有「为什么会变成这样啊？」
写盘时 `if not zh: continue` 又不写空行，于是**那一秒完全空白**。
`empty_check` 是硬门禁（每个 ASR 段起点 ±0.35s 内必须有行），
批跑 15 部里有 5 部各缺 1~3 行（0.07%~0.15%）就是它抓出来的。

## 为什么放在 05 之后、而不是 03 写盘时

03 的 draft 是**显示行之前的中间态**，行数比成品少；按它判会误补
（实测 RJ1561476 会补 29 行、RJ377265 补 41 行，而真实缺失只有 1~3 行）。
05_term_lock 才是交付形态，按它判才准。

## 口径

与 `empty_check.py` 完全一致（`--tol 0.35`），所以补完必然过那道门禁。
补的是「…」不是猜的译文：**有时间轴占位 > 整行消失**；
导出表里会标 `masked`，审校时一眼能看到。

用法：
    python fill_timeline.py --run <run_id>            # 只报告
    python fill_timeline.py --run <run_id> --apply    # 写回 05_term_lock
"""
import argparse
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
# ⚠️ 必须在 import cues **之前**定口径：`cues.ENABLED` 在 import 时读一次。
#    交付口径是 cue 模式；不设会静默退成「显示行」，起点对不上（假缺失几百行）。
os.environ.setdefault("KIKOERU_CUE_PIPELINE", "1")
TS = re.compile(r"^\[(\d+):([\d.]+)\]")
TOL = 0.35
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_lrc(p):
    rows = []
    for ln in open(p, encoding="utf-8"):
        m = TS.match(ln)
        if m:
            rows.append((round(int(m.group(1)) * 60 + float(m.group(2)), 2),
                         ln.rstrip("\n")[m.end():]))
    return rows


def missing(segments, rows):
    """按**重建的 cue** 起点判缺（不是 ASR 段起点）。

    ⚠️ 第一版用 ASR 段起点，5 部报出 96~466 行假缺失——因为 05 的行是
    「显示行」，起点是 cue 起点/中文切点，和段起点本来就对不上。
    `empty_check.py` 用的是重建 cue 的起点，必须同一套口径，
    否则补出来的「…」会把正常行挤掉。
    """
    import cues
    cus = cues.build_semantic_cues(cues.flatten_for_cues(segments))
    have = [t for t, _ in rows]
    out = []
    for c in cus:
        tx = (c.get("text") or "").strip()
        if not tx:
            continue
        # 纯符号 / 纯拉丁的 cue（`3・2・1`、`Okay, now…`）没有可译内容，不补
        if not re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", tx):
            continue
        t = round(float(c["start"]), 2)
        if any(abs(t - h) <= TOL for h in have):
            continue
        out.append({"t": t, "ja": tx})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    run = a.run if os.path.isdir(a.run) else os.path.join(ROOT, "work", "runs", a.run)
    man = json.load(open(os.path.join(run, "manifest.json"), encoding="utf-8"))
    rj = man["project"]
    lrc_dir = os.path.join(run, "05_term_lock", rj)
    asr_dir = os.path.join(run, "02_asr_fix", rj, "asr_fix")
    if not os.path.isdir(asr_dir):
        asr_dir = os.path.join(run, "01_asr", rj, "asr")

    total = 0
    for p in sorted(glob.glob(os.path.join(lrc_dir, "*.lrc"))):
        track = os.path.basename(p)[:-4]
        jf = os.path.join(asr_dir, track + ".json")
        if not os.path.exists(jf):
            continue
        segs = (json.load(open(jf, encoding="utf-8")).get("segments") or [])
        rows = load_lrc(p)
        gaps = missing(segs, rows)
        if not gaps:
            continue
        total += len(gaps)
        print("%-46s 缺 %d 行" % (track[:44], len(gaps)))
        for g in gaps:
            print("      %7.2f  %s" % (g["t"], g["ja"][:58]))
        if a.apply:
            rows.extend([(g["t"], "…") for g in gaps])
            rows.sort(key=lambda x: x[0])
            head = []
            for ln in open(p, encoding="utf-8"):
                if TS.match(ln):
                    break
                head.append(ln.rstrip("\n"))
            with open(p, "w", encoding="utf-8") as f:
                f.write("\n".join(head) + "\n\n")
                for t, zh in rows:
                    f.write("[%02d:%05.2f]%s\n" % (int(t // 60), t % 60, zh))
    print("\n合计缺 %d 行%s" % (total, "（已补「…」占位）" if a.apply else "（只报告，加 --apply 才写）"))


if __name__ == "__main__":
    main()
