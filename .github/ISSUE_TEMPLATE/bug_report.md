---
name: 缺陷报告
about: 某个阶段产出不对、门禁误报、或管线行为异常
title: "[bug] "
labels: bug
---

## 现象

<!-- 一句话说清「期望什么 / 实际什么」 -->

## 复现

```bash
# 阶段名 + run_id + 命令
```

## 证据

- 阶段：`01_asr` / `02_asr_fix` / `03_translate` / `04_polish` / `05_term_lock` / `06_checks`
- `06_checks/gates.json` 对应条目：

```
粘贴门禁输出
```

## 位置（不要贴原始台词）

<!-- 用占位描述：RJ0000001 00:12.34 第 3 句 -->

## 环境

- OS：
- Python：
- 是否 GPU / 模型：
- `pipeline.py selftest` 是否全过：
