# -*- coding: utf-8 -*-
r"""
清洗已产出的 ASR JSON：剔除 Whisper 套用 YouTube 套话编造的幻觉句。

背景：整轨（关 VAD）识别时，静音/音乐/呼吸段常被模型填进
「次の動画でお会いしましょう」「この動画の字幕は視聴者によって作成されました」
「ご視聴ありがとうございました」这类训练语料套话，音声作品里不可能出现。
asr.py 从今往后会自动剔除；本脚本用来清洗更早产出的 JSON。

用法：
  python clean_hallu.py <ASR目录> [--dry]
  --dry  只报告不写回
"""
import os
import sys
import json
import glob
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from asr import is_hallucination, is_freetalk  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("asr_dir")
    ap.add_argument("--corpus", action="store_true",
                    help="语料库模式：遍历 asr_dir 下每个子目录的 asr/")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    if a.corpus:
        files = []
        for w in sorted(os.listdir(a.asr_dir)):
            files += sorted(glob.glob(os.path.join(a.asr_dir, w, "asr", "*.json")))
    else:
        files = sorted(glob.glob(os.path.join(a.asr_dir, "*.json")))
    if not files:
        print("没有 JSON：%s" % a.asr_dir)
        return
    total = 0
    for jf in files:
        d = json.load(open(jf, encoding="utf-8"))
        segs = d.get("segments", [])
        keep, dropped = [], []
        # free talk 里「ご視聴ありがとうございました」是真实台词，不能当幻觉删
        ft = is_freetalk(d.get("source", os.path.basename(jf)))
        for s in segs:
            if is_hallucination(s.get("text", ""), ft):
                dropped.append((round(float(s.get("start", 0)), 1), s.get("text", "")[:44]))
            else:
                keep.append(s)
        if not dropped:
            continue
        print("%s : 剔除 %d 段" % (os.path.basename(jf)[:44], len(dropped)))
        for st, t in dropped:
            print("    %.1fs  %s" % (st, t))
        total += len(dropped)
        if not a.dry:
            d["segments"] = keep
            with open(jf, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
    print("\n共剔除 %d 段%s" % (total, "（--dry 未写回）" if a.dry else ""))


if __name__ == "__main__":
    main()
