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


# ================================================================ 知识包解析
# 2026-11-06（GPT 议题 3）：双轨本身合理，问题是**运行时自动选择且用户不知道**。
# 这一天踩到的：安装版 `data/style` 整个目录不存在 → 静默回落到 34 条的 presets
# （完整表 138 条）→ `ハンの子 → 班の子` 这条正好在缺的 104 条里 → 02 applied:0
# → 「汉的孩子们」。日志里一个字都没提示。
#
# 所以这里把「解析结果」变成**可核对的证据**：目录、来源类型、每张表的哈希、
# 缺哪些、哪些能力因此未启用。`identity` 是跨机可比的身份（路径只作本地诊断）。

import hashlib

# 缺了就跑不动的表
REQUIRED = ("asr_errata.json", "glossary.json")
# 缺了只是「该能力未启用」的表
OPTIONAL = ("terms_db.json", "translit_ok.txt", "ono_ja.txt",
            "do_not_change.json", "pin_sets.json", "coverage.json",
            "stats.json", "user_rules.txt", "asrfix_fewshot.md")

_CACHE = {}

# 阶段依赖身份协议版本（2026-11-10，GPT R3 Q1）。
# 改了「身份怎么算」就 +1，这样旧记录能被识别成「协议不同」而不是「内容不同」。
PROTOCOL = 1


def stage_identity(names, pkg=None):
    """按**某个阶段实际声明的表**算一个身份（GPT R3 Q1 方案 A）。

    为什么要有它：整包 `identity` 把 11 张表一把算，于是「只被 06 审计读的白名单」
    改一行也会让 02 的记录失效——2026-11-10 实测：往 `translit_ok.txt` 加 3 行，
    17 部作品的 02+ 全部判失效，而 02 根本不读这张表。

    所以拆成两层（GPT 原话「保留整包身份用于来源追溯，另算阶段有效依赖身份用于失效判断」）：
      - 整包 `identity`：跨机追溯「这份产物是用哪个知识包跑的」，**不用于判失效**
      - 本函数返回的身份：只覆盖该阶段声明的表，**用于判失效**

    身份 = 协议版本 + 每张表（名称, 存在与否, 内容哈希）。
    **路径不进身份**（跨机可比）；路径只在 `resolve()` 的 `dir/source_path` 里做诊断。

    返回 `{"identity": "v1@xxxx", "deps": {表名: {"exists": bool, "sha256": str}}}`。
    `deps` 会被写进 manifest，供 acceptance 逐表比对（GPT 建议的迁移路径）。
    """
    pkg = pkg or resolve()
    tabs = pkg.get("tables") or {}
    deps, parts = {}, []
    for n in sorted(set(names or [])):
        t = tabs.get(n)
        if t:
            deps[n] = {"exists": True, "sha256": t["sha256"]}
        else:
            deps[n] = {"exists": False, "sha256": ""}
        parts.append("%s:%s:%s" % (n, deps[n]["exists"], deps[n]["sha256"]))
    agg = hashlib.sha256(("v%d|" % PROTOCOL + "|".join(parts)).encode()).hexdigest()[:12]
    return {"identity": "v%d@%s" % (PROTOCOL, agg), "deps": deps}


def _sha(p):
    try:
        return hashlib.sha256(open(p, "rb").read()).hexdigest()
    except Exception:
        return ""


def resolve(force=False):
    """一次性解析知识包。返回 dict（见上）。同一目录结果缓存。"""
    d = style_dir()
    if not force and d in _CACHE:
        return _CACHE[d]
    if os.environ.get("KIKOERU_STYLE"):
        stype = "env"
    elif os.path.isdir(os.path.join(ROOT, "data", "style")):
        stype = "private"
    else:
        stype = "preset"
    tables, miss_req, miss_opt = {}, [], []
    for name in REQUIRED + OPTIONAL:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            tables[name] = {"sha256": _sha(p), "bytes": os.path.getsize(p)}
        elif name in REQUIRED:
            miss_req.append(name)
        else:
            miss_opt.append(name)
    agg = hashlib.sha256("".join("%s:%s" % (k, v["sha256"])
                                 for k, v in sorted(tables.items())).encode()).hexdigest()[:12]
    try:
        rel = os.path.relpath(d, ROOT).replace("\\", "/")
    except ValueError:
        rel = d
    out = {"dir": d, "source_type": stype, "source_path": rel,
           "identity": "%s@%s" % (stype, agg),
           "tables": tables,
           "missing_required": miss_req, "missing_optional": miss_opt,
           # 可选表缺失 = 对应能力**未启用**，调用方必须如实说，不能"警告后继续"
           "disabled": list(miss_opt)}
    _CACHE[d] = out
    return out


def require(what="知识包"):
    """必需表缺一个就抛错 —— 不允许静默降级到残缺包。"""
    r = resolve()
    if r["missing_required"]:
        raise RuntimeError(
            "%s不完整（来源 %s）：缺必需表 %s\n  目录：%s\n"
            "  显式指定（KIKOERU_STYLE）就补齐或去掉该变量；"
            "自动回落说明 data/style 与 presets 都不完整。"
            % (what, r["source_type"], r["missing_required"], r["dir"]))
    return r


def capability(name):
    """某张表对应能力是否可用（缺 = 未启用）。"""
    return name in resolve()["tables"]
