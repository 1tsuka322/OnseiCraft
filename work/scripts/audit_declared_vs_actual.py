#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""声明 ≠ 实际 核查（2026-11-11）。

## 为什么需要它

2026-11-10 发现：安装版的 `pipeline.local.json` 写死 `asr_model = ...large-v2`，
而代码默认是 v3、界面标签也写 v3 —— **实际跑了两天 v2 才发现**。
根因是同一个事实散在多处，谁也没去核对「声明」和「实际」是否一致。

这个脚本把「一个 run 里所有关于 ASR 模型的声明」列出来，逐条和**实际加载的模型**比：

| 声明来源 | 字段 |
|---|---|
| manifest 的 01 params | `stages.01_asr.params.model` |
| manifest 的模型指纹 | `stages.01_asr.models.whisper` |
| 每个 asr json 的元数据 | `asr_meta.model` |
| 成品的 `[by:]` 标签 | 第一行 `[by:...]` |
| 01 的日志 | `加载模型 X` |

**实际**取 `asr_meta.model` 的众数（没有 meta 的老文件单独报「无 meta」）。

## 用法

    python scripts/audit_declared_vs_actual.py            # 全部样品
    python scripts/audit_declared_vs_actual.py --all      # 样品 + 生产 17 部
    python scripts/audit_declared_vs_actual.py --rj RJ1019849

退出码 0 = 全部一致；1 = 有不一致（可用于门禁）。
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:                            # noqa: BLE001
        return {}


def _actual_models(run_dir, rj):
    """从 asr json 的 asr_meta 取实际模型 → (Counter, 无 meta 的文件数)。"""
    c, no_meta = collections.Counter(), 0
    for p in glob.glob(os.path.join(run_dir, "01_asr", rj, "asr", "*.json")):
        m = (_load(p).get("asr_meta") or {}).get("model") or ""
        if m:
            c[m] += 1
        else:
            no_meta += 1
    return c, no_meta


def _log_models(run_dir):
    """01 日志里最后一次「加载模型 X」。"""
    last = ""
    for f in glob.glob(os.path.join(run_dir, "01_asr", "*.log")):
        for ln in open(f, encoding="utf-8", errors="replace"):
            # ⚠️ 日志行形如 `加载模型 faster-whisper-large-v3（来源 --model）(cuda / float16)`，
            # 用 \S+ 会把「（来源」也吃进去 —— 必须在全角/半角括号处截断。
            m = re.search(r"加载模型 ([^\s（(]+)", ln)
            if m:
                last = m.group(1)
    return last


def _by_tags(lrc_dir):
    """成品里所有 `[by:]` 标签（去重）。"""
    tags = set()
    for p in glob.glob(os.path.join(lrc_dir, "*.lrc")):
        for i, ln in enumerate(open(p, encoding="utf-8", errors="replace")):
            if i > 3:
                break
            m = re.match(r"^\[by:(.*)\]$", ln.strip())
            if m:
                tags.add(m.group(1).strip())
    return tags


def check_one(rj, run_dir, prod_dir=""):
    """返回 (问题列表, 摘要行)。空列表 = 一致。"""
    bad = []
    m = _load(os.path.join(run_dir, "manifest.json"))
    st = ((m.get("stages") or {}).get("01_asr") or {})
    declared = (st.get("params") or {}).get("model") or ""
    fp_model = ((st.get("models") or {}).get("whisper") or "")
    if isinstance(fp_model, dict):
        fp_model = fp_model.get("name") or fp_model.get("path") or ""

    actual, no_meta = _actual_models(run_dir, rj)
    log_model = _log_models(run_dir)

    if not actual:
        return ["无 asr_meta（老格式，无法判定实际模型）"], "%-11s 无 meta" % rj

    # 实际 = 众数
    real = actual.most_common(1)[0][0]
    short = lambda x: (x or "").replace("\\", "/").rstrip("/").split("/")[-1]

    if declared and short(declared) != short(real):
        bad.append("params.model=%s ≠ 实际=%s" % (short(declared), short(real)))
    if fp_model and short(fp_model) != short(real):
        bad.append("指纹 models.whisper=%s ≠ 实际=%s" % (short(fp_model), short(real)))
    if log_model and short(log_model) != short(real):
        bad.append("日志加载=%s ≠ 实际=%s" % (short(log_model), short(real)))
    if no_meta:
        bad.append("%d 个 asr json 无 asr_meta" % no_meta)
    if len(actual) > 1:
        bad.append("同一 run 内混用多个模型：%s" % dict(actual))

    tags = _by_tags(prod_dir) if prod_dir and os.path.isdir(prod_dir) else set()
    for t in tags:
        # [by:] 形如 `faster-whisper-large-v3 + Sakura-7B-...`
        head = t.split("+")[0].strip()
        if head and short(head).replace(" ", "-") != short(real).replace(" ", "-"):
            # 只比 ASR 那一段；标签写法可能带空格，宽松比
            if short(real) not in t:
                bad.append("成品 [by:] 标签不含实际模型：%r" % t[:40])

    line = "%-11s 实际=%s（%d 文件）" % (rj, short(real), sum(actual.values()))
    if len(actual) > 1:
        line += "  ⚠️ 混用 %d 种" % len(actual)
    return bad, line


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rj", default="")
    ap.add_argument("--all", action="store_true", help="样品 + 生产 17 部")
    ap.add_argument("--prod", default=os.path.join(ROOT, "..", "kikoerusubtitle"))
    a = ap.parse_args()

    targets = []
    if a.rj:
        targets = [a.rj]
    else:
        for p in sorted(glob.glob(os.path.join(ROOT, "samples", "RJ*"))):
            if os.path.isdir(p):
                targets.append(os.path.basename(p))
        if a.all:
            for p in sorted(glob.glob(os.path.join(ROOT, "runs", "*_production"))):
                rj = os.path.basename(p).split("_")[1]
                if rj not in targets:
                    targets.append(rj)

    print("=== 声明 vs 实际（ASR 模型）===")
    bad_all = []
    for rj in targets:
        ds = sorted(glob.glob(os.path.join(ROOT, "runs", "*_%s_production" % rj)))
        if not ds:
            print("%-11s 无 run" % rj)
            continue
        prod = os.path.join(os.path.abspath(a.prod), rj)
        bad, line = check_one(rj, ds[-1], prod)
        print(("  ❌ " if bad else "  ✅ ") + line)
        for b in bad:
            print("       - %s" % b)
        if bad:
            bad_all.append((rj, bad))

    print("\n合计 %d 个 run，不一致 %d 个" % (len(targets), len(bad_all)))
    if bad_all:
        print("⚠️ 不一致清单：%s" % [x[0] for x in bad_all])
    return 0 if not bad_all else 1


if __name__ == "__main__":
    sys.exit(main())
