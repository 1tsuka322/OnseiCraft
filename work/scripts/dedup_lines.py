# -*- coding: utf-8 -*-
"""删掉「和上一行一字不差」的重复显示行。

## 为什么单独做，不改 polish.py

`render_display_lines` 会把同一句中文贴到**两个不同的 cue** 上（不是 cue 内部）。
实测 RJ1623028 全库 25 处，改了 polish.py 的 cue 内去重只修掉 2 处——
剩下 23 处都是跨 cue 的。

跨 cue 在 polish 里删不了：那个 cue 会一行不剩，触发
`keep < len(lines)` 硬门禁（丢行比重复严重得多）。

## 为什么删行是安全的

LRC 的显示时长 = 到下一行时间戳为止。删掉重复的第二行，
第一行会自动延长到第三行——**内容一点没少，只是不再闪两遍**。
`empty_check` 判的是 cue 起点，删重复行不影响。

用法：
    python dedup_lines.py --run <run_id>            # 只报告
    python dedup_lines.py --run <run_id> --apply    # 写回 05_term_lock
"""
import argparse
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
TS = re.compile(r"^\[(\d+):([\d.]+)\]")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    run = a.run if os.path.isdir(a.run) else os.path.join(ROOT, "work", "runs", a.run)
    man = json.load(open(os.path.join(run, "manifest.json"), encoding="utf-8"))
    rj = man["project"]
    lrc_dir = os.path.join(run, "05_term_lock", rj)

    total = 0
    for p in sorted(glob.glob(os.path.join(lrc_dir, "*.lrc"))):
        head, rows = [], []
        for ln in open(p, encoding="utf-8"):
            m = TS.match(ln)
            if not m:
                head.append(ln.rstrip("\n"))
                continue
            t = round(int(m.group(1)) * 60 + float(m.group(2)), 2)
            rows.append((t, ln.rstrip("\n")[m.end():]))
        out, dropped = [], []
        prev = None
        for t, zh in rows:
            # ⚠️ 纯「…」不能删：那是「这段有声音但没译出」的占位。
            # 删了会让那个 cue 一行不剩，empty_check 立刻报「空字幕」
            # （实测 RJ1623028 Track05 删了 3 个连续的 …，直接挂门禁）。
            if zh and zh == prev and not re.fullmatch(r"[…\s]+", zh):
                dropped.append((t, zh))
                continue
            out.append((t, zh))
            prev = zh
        if not dropped:
            continue
        total += len(dropped)
        print("%-46s 删 %d 行" % (os.path.basename(p)[:44], len(dropped)))
        for t, zh in dropped[:3]:
            print("      %7.2f  %s" % (t, zh[:40]))
        if a.apply:
            with open(p, "w", encoding="utf-8") as f:
                f.write("\n".join(head) + "\n\n")
                for t, zh in out:
                    f.write("[%02d:%05.2f]%s\n" % (int(t // 60), t % 60, zh))
    print("\n合计 %d 行%s" % (total, "（已删）" if a.apply else "（只报告，加 --apply 才写）"))


if __name__ == "__main__":
    main()
