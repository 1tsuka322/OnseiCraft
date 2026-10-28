# -*- coding: utf-8 -*-
"""仓库三层结构的路径换算（给**独立运行**的 scripts/*.py 用）。

`pipeline/pipeline_lib/paths.py` 是管线内部的唯一出口；但 scripts/ 下的脚本
经常被单独调用（`python scripts/check.py ...`），它们自己算 ROOT 时算的是
`work/`，而**成品与音频在 work 外面**（仓库根那一层）。写在这里，避免每个脚本
各自 `os.path.join(ROOT, "kikoerusubtitle")` —— 2026-10-28 目录重构时，
有 8 个脚本都是这么写的，全部要改，改漏一个就是「找不到目录」的静默错。

```
<repo>/                 仓库根：README + ASMRmusic/ + kikoerusubtitle/ + work/
├── ASMRmusic/          原始音频
├── kikoerusubtitle/    交付成品 LRC
└── work/               ROOT：代码/文档/数据/运行产物/临时
```

环境变量优先：`KIKOERU_ROOT`（work）/ `KIKOERU_PROD` / `KIKOERU_AUDIO` / `KIKOERU_REC`。
"""
import os

# work/ —— 本文件在 work/scripts/rootloc.py
ROOT = os.path.abspath(os.environ.get(
    "KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
# 仓库根 —— 成品与音频挂在这一层
BASE = os.path.dirname(ROOT)

PROD = os.environ.get("KIKOERU_PROD", os.path.join(BASE, "kikoerusubtitle"))
AUDIO = os.environ.get("KIKOERU_AUDIO", os.path.join(BASE, "ASMRmusic"))
REC = os.environ.get("KIKOERU_REC", os.path.join(ROOT, "kikoerusubtitle_rec"))
DATA = os.path.join(ROOT, "data")
DRAFT = os.path.join(DATA, "draft")
TMP = os.path.join(ROOT, "tmp")
