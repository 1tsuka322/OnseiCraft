# -*- coding: utf-8 -*-
"""知识表 / 风格库位置解析（唯一出口）。

公开仓库不含数据，所以这里有一层**回落**：

1. `KIKOERU_STYLE` 环境变量（最高优先，管线会显式传）
2. `<repo>/data/style` —— 维护者的真实风格库（含平行语料，不入库）
3. `<repo>/presets` —— **公开预设**：clone 下来就有，含规则/词典/合成样例

这样同一份代码在「有私有语料」和「刚 clone 下来」两种环境下都能跑，
区别只是效果强弱（有语料 → few-shot 更贴目标译者；只有预设 → 走规则与词典）。
"""
import os

ROOT = os.environ.get(
    "KIKOERU_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def style_dir():
    env = os.environ.get("KIKOERU_STYLE")
    if env:
        return env
    real = os.path.join(ROOT, "data", "style")
    if os.path.isdir(real):
        return real
    return os.path.join(ROOT, "presets")


def table(name):
    """风格库里某张表的完整路径（不保证存在）。"""
    return os.path.join(style_dir(), name)


def has(name):
    return os.path.exists(table(name))
