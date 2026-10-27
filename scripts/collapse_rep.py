# -*- coding: utf-8 -*-
r"""修既有 ASR JSON 里的「重复循环」伪影。

Whisper 在「一段够长但没有明确语音」的音频上会陷入字符级重复循环。
实测：RJ9000011 Track04 有 27 秒全是「あ」、RJ9000002 トラック2 有 28 秒
全是「ぎゅー」、RJ9000002 トラック4 有 11 秒「カチャカチャ」。
这种段原样进翻译只会产出成串雷同的垃圾行。

`asr.py` 从 2026-10-13 起已在识别时折叠；本脚本用来**返工已存在的 JSON**。

判定刻意保守（要求既「长」又「高度重复」），因为 ぴゅぴゅぴゅ／好き好き好き／
シコシコ 这类 2-7 秒的重复是音声作品里**真实的连续喘息**，必须原样保留。

用法：
  python collapse_rep.py <ASR目录> [--dry]
"""
import os
import re
import sys
import json
import glob
import argparse

REP_MAX_SEC = 10.0     # 段长 >= 此值才考虑是循环伪影
REP_MIN_TIMES = 6      # 同一最短单元重复次数 >= 此值
REP_KEEP = 3           # 折叠后保留几遍
REP_LONG = 30          # 归一化文本长度 >= 此值且字符种类极少 -> 也判为循环伪影
REP_DISTINCT = 3       # 允许的不同字符数
REP_RUN_KEEP = 4       # 每个 run 最多保留几个字
# 与 asr.py 的 collapse_repetition 保持一致（规则改了要同步改两边）
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "asr_rep", os.path.join(os.path.dirname(os.path.abspath(__file__)), "asr.py"))
_asr = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_asr)
collapse_repetition = _asr.collapse_repetition


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("asr_dir")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(a.asr_dir, "*.json")))
    n_segs = n_files = 0
    for jf in files:
        d = json.load(open(jf, encoding="utf-8"))
        segs = d.get("segments", [])
        changed = []
        for i, s in enumerate(segs):
            txt, was = collapse_repetition(s.get("text", ""), s.get("start"), s.get("end"))
            if was:
                changed.append((i, s["start"], s["end"], s["text"]))
                if not a.dry:
                    s["text"] = txt
                    s["words"] = []
                    s["collapsed"] = True
                    d["rep_collapsed"] = d.get("rep_collapsed", 0) + 1
        if changed and not a.dry:
            json.dump(d, open(jf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        for i, st, en, t in changed:
            n_segs += 1
            print("  %-46s seg%3d [%7.2f-%7.2f] %3.0fs %r -> %r"
                  % (os.path.basename(jf)[:46], i, st, en, en - st, t[:22], None))
        if changed:
            n_files += 1
    print("\n%s：%d 个文件 / %d 段被折叠"
          % ("（--dry）" if a.dry else "完成", n_files, n_segs))


if __name__ == "__main__":
    main()
