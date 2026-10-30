# -*- coding: utf-8 -*-
"""路径与全局配置（单一出口，别在别处硬编码 ROOT）。

2026-10-28 起仓库是**三层**结构，本文件是唯一换算点：

```
<repo>/                     仓库根（只放 README + 三个目录）
├── ASMRmusic/              原始音频（不进库）
├── kikoerusubtitle/        交付成品 LRC（不进库）
└── work/                   ← ROOT：全部工作文件（代码/文档/数据/运行产物/临时）
```

`ROOT` 由本文件的物理位置反推（`work/pipeline/pipeline_lib/paths.py` 上三级），
所以 `work/` 整个搬走也不会算错；只有 `PROD`/`AUDIO` 这两个**在 work 外面**的目录
需要 `BASE = dirname(ROOT)`。想彻底换地方就设 `KIKOERU_ROOT` / `KIKOERU_PROD` / `KIKOERU_AUDIO`。
"""
import json
import os
import sys

ROOT = os.path.abspath(os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
# 仓库根 = work 的上一级。ASMRmusic / kikoerusubtitle 挂在这一层。
BASE = os.path.dirname(ROOT)

SCRIPTS = os.path.join(ROOT, "scripts")
# 风格库/知识表：环境变量 → 本地真实库（含平行语料，不入库）→ 公开预设 presets/
# 这层回落是给「刚 clone 下来、还没有自己语料」的人用的：能跑，效果走规则与词典。
_PRESETS = os.path.join(ROOT, "presets")
_LOCAL_STYLE = os.path.join(ROOT, "data", "style")
STYLE = (os.environ.get("KIKOERU_STYLE")
         or (_LOCAL_STYLE if os.path.isdir(_LOCAL_STYLE) else _PRESETS))
DRAFT = os.path.join(ROOT, "data", "draft")
# 成品与音频都在 work 外面（仓库根），别用 ROOT 拼。
PROD = os.environ.get("KIKOERU_PROD", os.path.join(BASE, "kikoerusubtitle"))
AUDIO = os.environ.get("KIKOERU_AUDIO", os.path.join(BASE, "ASMRmusic"))
# 成品回收站留在 work 里（它是工作产物，不是交付物）
REC = os.environ.get("KIKOERU_REC", os.path.join(ROOT, "kikoerusubtitle_rec"))
LOCKS = os.path.join(ROOT, "data", "locks")
OVERLAY = os.path.join(ROOT, "data", "repair_overlay")

RUNS = os.environ.get("KIKOERU_RUNS", os.path.join(ROOT, "runs"))
KNOW = os.environ.get("KIKOERU_KNOWLEDGE", os.path.join(ROOT, "knowledge"))
KNOW_VERSIONS = os.path.join(KNOW, "versions")
KNOW_PROPOSALS = os.path.join(KNOW, "proposals")
KNOW_TESTS = os.path.join(KNOW, "tests")
KNOW_BACKUPS = os.path.join(KNOW, "backups")
KNOW_REGISTRY = os.path.join(KNOW, "registry.json")

TMP = os.path.join(ROOT, "data", "tmp")
QUEUE = os.path.join(TMP, "pipeline_queue")
# ⚠️ 配置在 **pipeline 包目录里**（work/pipeline/pipeline.json），不在 work/ 根。
# 2026-10-28 目录重构后这里写成 `ROOT/pipeline.json`（= work/pipeline.json，根本不存在），
# `load_config()` 于是**静默只用 DEFAULTS**——改配置文件等于没改。
# 这正是「声明 ≠ 实际」那一类：文件在库里、看着改了、实际没读。
# 现在按「包目录」定位，并在文件缺失时告警。
CONFIG = os.environ.get("KIKOERU_CONFIG") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline.json")
# 本机覆盖（不进库）：模型路径、llama.cpp 位置这些**因机器而异**的东西放这里，
# 公共的 pipeline.json 里不写任何绝对路径。两份按「先公共、后本地」深合并。
CONFIG_LOCAL = os.path.join(os.path.dirname(CONFIG), "pipeline.local.json")

# 生产知识表：**唯一真相**在 data/style/，这里只登记「参与版本化的是哪些文件」。
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
    # P2（2026-10-28）：源语短语 → 必须表达的概念族。管「原文出现就不能丢什么」，
    # 与 glossary（管「更偏好怎么说」）是两张表，别合并。
    ("coverage.json", "checks", "语义覆盖表（委婉化拦截）"),
]

DEFAULTS = {
    # 不写死解释器：默认用「跑 pipeline.py 的那个」。写死过两次路径，
    # 两次都指向已经不存在的目录，`python_exe` 静默回落 → 看起来一切正常，
    # 实际换了环境（R154 实测：pipeline.json 里那条早就没了）。
    # 要指定就设 KIKOERU_PYTHON，或在 pipeline.json 里加 "python"。
    "python": os.environ.get("KIKOERU_PYTHON", ""),
    "device": "cuda",
    "compute": "float16",
    # ⚠️ DEFAULTS 必须与**交付口径**一致（2026-10-30 修正）。
    # 以前这里写 `hymt` / `cleanup_only=False`（旧路线），而 pipeline.json 是
    # `sakura` / `True`——配置文件一旦缺失或被改名，`load_config()` 就静默退回旧路线，
    # 跑出来的成品看着正常、其实换了引擎。这正是 R153「声明 ≠ 实际」的翻版。
    "engine": "sakura",
    # 04 用的模型：只在**回退旧路线**（cleanup_only=false）时才生效。
    # 初译是纯翻译模型，润色要「20 条规则 + 术语表 + 编号协议」，只有聊天模型做得来；
    # 2026-10-28 之前两阶段共用同一个服务，实际跑的一直是 Qwen（见 R153）。
    "polish_engine": "q5",
    # 04 只做确定性后处理、不过 LLM（Sakura 路线的配套开关，R154 起为交付口径）。
    # true = 「Sakura 初译 → cleanup-only」；false = 旧路线「HY-MT → Qwen 润色」。
    "polish_cleanup_only": True,
    # 01 阶段末尾清 Whisper 的拉丁幻觉（`はぁ… Huh, Ah, no, sorry!`）。
    # 纯确定性、不需要模型；做 A/B 时置 false。
    "asr_latin_cleanup": True,
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
                      "overlay", "trunc", "slot_guard", "semantic"],
        },
        "fast": {
            "stages": ["01_asr", "02_asr_fix", "03_translate", "04_polish",
                       "05_term_lock", "06_checks"],
            "gates": ["check", "empty", "scan", "errata_audit"],
        },
        "checks": {
            "stages": ["06_checks"],
            "gates": ["check", "empty", "scan", "errata_audit", "test_regressions",
                      "overlay", "trunc", "slot_guard", "semantic"],
        },
    },
}


def _merge(cfg, user):
    for k, v in (user or {}).items():
        if k == "profiles":
            for pk, pv in v.items():
                cfg["profiles"].setdefault(pk, {}).update(pv)
        elif k == "models" and isinstance(v, dict):
            cfg.setdefault("models", {}).update(v)
        else:
            cfg[k] = v


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if not os.path.exists(CONFIG):
        # 不再静默：路径写错时配置文件存在却没被读，跑出来跟默认值一样，谁都发现不了。
        print("⚠️ 配置文件不存在：%s → 只用 DEFAULTS（+ 本地覆盖）" % CONFIG)
    for path in (CONFIG, CONFIG_LOCAL):
        if os.path.exists(path):
            try:
                _merge(cfg, json.load(open(path, encoding="utf-8")))
            except Exception as e:
                print("⚠️ 读 %s 失败，跳过：%s" % (path, e))
    return cfg


def python_exe(cfg=None):
    """必须用装了依赖的那个 venv；找不到就**明确告警**再退回当前解释器。

    ⚠️ 以前是静默回落：配了一条不存在的路径，跑起来一切正常，
    实际用的却是另一个解释器（依赖可能不同）。这违反「声明 == 实际」——
    凡是声明了一个解释器/模型，就必须有一条 assert 实际 == 声明。
    """
    p = (cfg or load_config()).get("python") or ""
    if p and os.path.exists(p):
        return p
    if p:
        print("⚠️ 配置的解释器不存在：%s → 改用当前解释器 %s"
              % (p, sys.executable))
    return sys.executable


def ensure_dirs():
    for d in (RUNS, KNOW, KNOW_VERSIONS, KNOW_PROPOSALS, KNOW_TESTS, KNOW_BACKUPS, QUEUE):
        os.makedirs(d, exist_ok=True)
