# -*- coding: utf-8 -*-
"""统计每个作品的人工字幕行数 vs 对齐成功行数（召回率）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_library as B  # noqa: E402
import align_style as A  # noqa: E402

CORPUS = r"$KIKOERU_ROOT\learning"
ASR_ROOT = r"$KIKOERU_ROOT\data\draft\learning"

rows = []
for w in sorted(os.listdir(CORPUS)):
    wd = os.path.join(CORPUS, w)
    if not os.path.isdir(wd):
        continue
    asr = os.path.join(ASR_ROOT, w, "asr")
    if not os.path.isdir(asr):
        rows.append((w, 0, 0, 0.0))
        continue
    # 直接复用 build_library 的配对逻辑（含编号前缀回退匹配：
    # 有的作品音频是日文名、字幕是中文名，只有「m19_01_」这类序号相同）
    pairs, human, raw = B.pairs_of_work(wd, asr, w, verbose=False)
    rows.append((w, human, raw, 100.0 * raw / human if human else 0.0))

rows.sort(key=lambda x: x[3])
print("%-50s %6s %6s %7s" % ("作品", "人工", "对齐", "召回率"))
for w, h, r, p in rows:
    flag = "  <<< 低" if p < 80 else ""
    print("%-50s %6d %6d %6.1f%%%s" % (w[:48], h, r, p, flag))
th = sum(x[1] for x in rows)
tr = sum(x[2] for x in rows)
print()
print("合计 人工 %d / 对齐 %d  全局召回率 %.1f%%" % (th, tr, 100.0 * tr / th))
