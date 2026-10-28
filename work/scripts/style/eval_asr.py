# -*- coding: utf-8 -*-
r"""
用「人工字幕」当标准答案，评估识别结果的好坏（召回率 / 漏句清单）。

用法：
  python eval_asr.py <人工字幕目录> <ASR目录> [--list N]

输出：
  - 命中率：人工有台词的句子里，识别结果也有输出的占比（越高越好）
  - 漏句清单：人工有台词、识别完全没输出的行（含中文原文，便于人工核对）
  - 多报率：识别有输出、人工却没台词的区间占比（幻觉参考，人工稿会省略喘息，仅供参考）

为什么需要它：
  没有标准答案时只能看「覆盖率」这种间接指标，容易把喘息当漏句、把幻觉当召回。
  有人工字幕就能直接量出「漏了多少句真台词」，调参才有方向。
"""
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
import re
import json
import glob
import argparse

VTT_TS = re.compile(r"(\d+):(\d+):(\d+\.\d+)\s*-->\s*(\d+):(\d+):(\d+\.\d+)")
VTT_TS2 = re.compile(r"(\d+):(\d+\.\d+)\s*-->\s*(\d+):(\d+\.\d+)")
LRC_TS = re.compile(r"\[(\d+):(\d+\.\d+)\]")
SUB_EXT = (".vtt", ".srt", ".lrc")


def read_sub(path):
    """统一读出 [(start, end, text)]，秒。"""
    out = []
    lines = open(path, encoding="utf-8").read().splitlines()
    ext = os.path.splitext(path)[1].lower()
    if ext == ".lrc":
        for line in lines:
            if line.startswith("[ti:") or line.startswith("[by:"):
                continue
            m = LRC_TS.match(line)
            if m:
                out.append((int(m.group(1)) * 60 + float(m.group(2)), 0.0,
                            line[m.end():].strip()))
        for i in range(len(out) - 1):
            out[i] = (out[i][0], out[i + 1][0], out[i][2])
        return [(a, b, t) for a, b, t in out if t]

    i = 0
    while i < len(lines):
        m = VTT_TS.search(lines[i])
        m2 = VTT_TS2.search(lines[i]) if not m else None
        if m:
            s = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            e = int(m.group(4)) * 3600 + int(m.group(5)) * 60 + float(m.group(6))
        elif m2:
            s = int(m2.group(1)) * 60 + float(m2.group(2))
            e = int(m2.group(3)) * 60 + float(m2.group(4))
        else:
            i += 1
            continue
        buf = []
        j = i + 1
        while j < len(lines) and lines[j].strip() and "-->" not in lines[j]:
            if not re.match(r"^\d+$", lines[j].strip()):
                buf.append(lines[j].strip())
            j += 1
        txt = " ".join(buf).strip()
        if txt:
            out.append((s, e, txt))
        i = j
    return out


def load_segs(path):
    d = json.load(open(path, encoding="utf-8"))
    return d.get("segments", []), float(d.get("duration", 0))


def covered(segs, s, e, off=0.0, need=0.3):
    """人工行 [s,e] 在识别结果里有没有被覆盖到。"""
    span = max(0.001, e - s)
    for g in segs:
        a, b = g["start"] + off, g["end"] + off
        ov = min(e, b) - max(s, a)
        if ov > 0 and ov / span >= need:
            return True
    return False


def best_offset(subs, segs):
    """人工字幕和识别结果常有整体时移，先搜一个最优偏移。"""
    best, bo = -1, 0.0
    for i in range(-30, 31):
        off = i * 0.2
        hit = sum(1 for s, e, _ in subs if covered(segs, s, e, off))
        if hit > best:
            best, bo = hit, off
    return bo, best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sub_dir")
    ap.add_argument("asr_dir")
    ap.add_argument("--list", type=int, default=15, help="打印多少条漏句")
    ap.add_argument("--json", default="", help="把漏句清单写成 json")
    a = ap.parse_args()

    subs = sorted(p for p in glob.glob(os.path.join(a.sub_dir, "*"))
                  if p.lower().endswith(SUB_EXT))
    asr_files = sorted(glob.glob(os.path.join(a.asr_dir, "*.json")))
    by_name = {}
    for p in asr_files:
        by_name[os.path.basename(p)[:-len(".json")]] = p
        by_name[os.path.splitext(os.path.basename(p))[0]] = p

    tot = hit = 0
    miss = []
    print("文件                                          人工行   命中   漏句   偏移")
    print("-" * 78)
    for sp in subs:
        base = os.path.basename(sp)
        for suf in (".vtt", ".srt", ".lrc"):
            if base.lower().endswith(suf):
                base = base[: -len(suf)]
                break
        ap_ = by_name.get(base)
        if not ap_:
            cands = [v for k, v in by_name.items() if base[:6] and base[:6] in k]
            ap_ = cands[0] if len(cands) == 1 else None
        if not ap_:
            print("  [跳过] 无对应识别结果: %s" % base[:44])
            continue
        segs, _ = load_segs(ap_)
        rows = read_sub(sp)
        off, _ = best_offset(rows, segs)
        h = 0
        for s, e, t in rows:
            if covered(segs, s, e, off):
                h += 1
            else:
                miss.append({"file": base, "s": round(s, 2), "e": round(e, 2), "zh": t})
        tot += len(rows)
        hit += h
        print("  %-42s %5d %6d %6d   %+.1fs"
              % (base[:40], len(rows), h, len(rows) - h, off))

    print("-" * 78)
    if not tot:
        print("没有可比对的句对")
        return
    print("合计：人工 %d 句，命中 %d 句，漏 %d 句  -> 召回率 %.1f%%"
          % (tot, hit, tot - hit, hit * 100.0 / tot))

    if miss:
        durs = [m["e"] - m["s"] for m in miss]
        print("漏句时长：中位 %.1f 秒，最长 %.1f 秒，合计 %.0f 秒"
              % (sorted(durs)[len(durs) // 2], max(durs), sum(durs)))
        print("\n=== 漏句清单（人工有台词、识别完全没输出）===")
        for m in miss[: a.list]:
            print("  [%02d:%05.1f] %s" % (int(m["s"] // 60), m["s"] % 60, m["zh"][:60]))
        if a.json:
            json.dump(miss, open(a.json, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            print("\n完整清单 -> %s" % a.json)


if __name__ == "__main__":
    main()
