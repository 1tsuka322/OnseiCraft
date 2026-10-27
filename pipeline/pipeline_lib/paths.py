# -*- coding: utf-8 -*-
"""路径与全局配置（单一出口，别在别处硬编码 ROOT）。"""
import json
import os
import sys

ROOT = os.path.abspath(os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

SCRIPTS = os.path.join(ROOT, "scripts")
# 风格库/知识表：环境变量 → 本地真实库（含平行语料，不入库）→ 公开预设 presets/
# 这层回落是给「刚 clone 下来、还没有自己语料」的人用的：能跑，效果走规则与词典。
_PRESETS = os.path.join(ROOT, "presets")
_LOCAL_STYLE = os.path.join(ROOT, ".workbuddy", "style")
STYLE = (os.environ.get("KIKOERU_STYLE")
         or (_LOCAL_STYLE if os.path.isdir(_LOCAL_STYLE) else _PRESETS))
DRAFT = os.path.join(ROOT, ".workbuddy", "draft")
PROD = os.environ.get("KIKOERU_PROD", os.path.join(ROOT, "kikoerusubtitle"))
LOCKS = os.path.join(ROOT, ".workbuddy", "locks")
OVERLAY = os.path.join(ROOT, ".workbuddy", "repair_overlay")

RUNS = os.environ.get("KIKOERU_RUNS", os.path.join(ROOT, "runs"))
KNOW = os.environ.get("KIKOERU_KNOWLEDGE", os.path.join(ROOT, "knowledge"))
KNOW_VERSIONS = os.path.join(KNOW, "versions")
KNOW_PROPOSALS = os.path.join(KNOW, "proposals")
KNOW_TESTS = os.path.join(KNOW, "tests")
KNOW_BACKUPS = os.path.join(KNOW, "backups")
KNOW_REGISTRY = os.path.join(KNOW, "registry.json")

TMP = os.path.join(ROOT, ".workbuddy", "tmp")
QUEUE = os.path.join(TMP, "pipeline_queue")
CONFIG = os.path.join(ROOT, "pipeline.json")

# 生产知识表：**唯一真相**在 .workbuddy/style/，这里只登记「参与版本化的是哪些文件」。
# role 用于「这条产物依赖哪类知识」的追溯与失效判断。
KNOWLEDGE_FILES = [
    ("asr_errata.json", "asr_fix", "已核实 ASR 错字表"),
    ("do_not_change.json", "asr_fix", "表记差异白名单（不得当错字改）"),
    ("glossary.json", "translate", "术语表（进 prompt 的那张）"),
    ("pin_sets.json", "term_lock", "允许集（同一词的可接受译法）"),
    ("user_rules.txt", "translate", "用户拍板的统一规范（优先级最高）"),
    ("hard_rules.txt", "polish", "硬约束（带真实百分比，进 prompt）"),
    ("parallel.json", "polish", "日→中平行句对（few-shot 例句来源，17MB 只记哈希）"),
    ("terms_db.json", "term_lock", "术语发现库（jieba 实现）"),
    ("stats.json", "checks", "人工风格统计（check 的参照）"),
]

DEFAULTS = {
    "python": r"C:/Users/zyk/.workbuddy/binaries/python/envs/default/Scripts/python.exe",
    "device": "cuda",
    "compute": "float16",
    "engine": "hymt",
    # 交付的 16 部是 cue 模式做的（spacer 只在 is_cue 分支产生，成品有 304 个 spacer）。
    # 0=显示行 / 1=语义 cue —— 默认必须与交付口径一致，否则行数/清屏行全对不上。
    "cue_pipeline": 1,
    "spk_prefix": False,
    "track_memory": False,
    "diarize": False,
    "diarize_thr": 0.30,
    # 空字幕门禁（R142/R143，2026-10-27）：成品里「应有字幕的行」缺字的比例上限。
    # 0 = 一行都不许缺。RJ9000013 曾因 ASR 前缀污染（`９接`）出现 19 行空字幕，
    # check/scan/trunc 三道全放过 —— 空文本行被当成清屏 spacer 了。
    "empty_max_miss": 0.0,
    "profiles": {
        "production": {
            "stages": ["01_asr", "02_asr_fix", "03_translate", "04_polish",
                       "05_term_lock", "06_checks"],
            "gates": ["check", "empty", "scan", "errata_audit", "test_regressions",
                      "overlay", "trunc", "slot_guard"],
        },
        "fast": {
            "stages": ["01_asr", "02_asr_fix", "03_translate", "04_polish",
                       "05_term_lock", "06_checks"],
            "gates": ["check", "empty", "scan", "errata_audit"],
        },
        "checks": {
            "stages": ["06_checks"],
            "gates": ["check", "empty", "scan", "errata_audit", "test_regressions",
                      "overlay", "trunc", "slot_guard"],
        },
    },
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG):
        try:
            user = json.load(open(CONFIG, encoding="utf-8"))
            for k, v in user.items():
                if k == "profiles":
                    for pk, pv in v.items():
                        cfg["profiles"].setdefault(pk, {}).update(pv)
                else:
                    cfg[k] = v
        except Exception as e:
            print("⚠️ 读 %s 失败，用默认配置：%s" % (CONFIG, e))
    return cfg


def python_exe(cfg=None):
    """必须用装了依赖的那个 venv；找不到就退回当前解释器并提示。"""
    p = (cfg or load_config()).get("python") or ""
    if p and os.path.exists(p):
        return p
    return sys.executable


def ensure_dirs():
    for d in (RUNS, KNOW, KNOW_VERSIONS, KNOW_PROPOSALS, KNOW_TESTS, KNOW_BACKUPS, QUEUE):
        os.makedirs(d, exist_ok=True)
