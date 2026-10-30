# -*- coding: utf-8 -*-
"""扫「写死的项目内绝对路径」——三层目录改造后最容易残留的声明≠实际。

之前两个工具栽过：`lock_from_plan.py` 写死 `<repo>/data/locks`（真实是 `work/data/locks`）、
`fix_lines_batch.py` 写死 `<repo>/scripts`（真实是 `work/scripts`）。
两者都「跑得动但不生效」或「直接报错」，属于最难发现的一类。
"""
import glob
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

PAT = re.compile(r"""["']([A-Za-z]:[\\/][^"']{3,90})["']""")

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

files = (glob.glob("scripts/*.py")
         + glob.glob("pipeline/**/*.py", recursive=True)
         + glob.glob("tmp/daily/*.py")
         + glob.glob("tmp/*.py"))

bad = {}
for f in files:
    try:
        s = open(f, encoding="utf-8").read()
    except Exception:                                   # noqa: BLE001
        continue
    for m in PAT.finditer(s):
        p = m.group(1)
        if "kikoeru" not in p.lower():
            continue                                    # 只关心本项目
        q = p.replace("\\", "/")
        if not os.path.exists(q):
            bad.setdefault(f, []).append(q)

for f in sorted(bad):
    print("❌", f)
    for p in sorted(set(bad[f]))[:5]:
        print("     ", p)
print("\n有问题的文件 %d 个（扫了 %d 个）" % (len(bad), len(files)))
