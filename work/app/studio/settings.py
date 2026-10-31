# -*- coding: utf-8 -*-
"""OnseiCraft 的配置层。

两件事：
1. **在 import pipeline_lib 之前**把 `KIKOERU_PROD` 等环境变量注入好
   （`pipeline_lib/paths.py` 是在 import 时求值常量的，晚一步就改不动）。
2. 读写 `work/app/app.json`（用户偏好）与 `work/pipeline/pipeline.local.json`（模型路径）。

⚠️ 模型路径只写 `pipeline.local.json`，不写 `pipeline.json`：
前者是本机覆盖、不进版本库；后者是公共配置，里面一个绝对路径都不许有。
"""
import json
import os

# 打包后 ROOT 由 main.py 通过 KIKOERU_ROOT 注入（exe 同级 work/）；
# 开发模式按本文件位置反推：studio → app → work
ROOT = os.path.abspath(os.environ.get("KIKOERU_ROOT")
                       or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
BASE = os.path.dirname(ROOT)

APP_DIR = os.path.join(ROOT, "app")
APP_CFG = os.path.join(APP_DIR, "app.json")
LOCAL_CFG = os.path.join(ROOT, "pipeline", "pipeline.local.json")

DEFAULT_PROD = os.path.join(BASE, "kikoerusubtitle")
DEFAULT_IMPORT = os.path.join(ROOT, "data", "import")
DEFAULT_CACHE = os.path.join(ROOT, "data", "asr_cache")
DEFAULT_FEEDBACK = os.path.join(ROOT, "data", "feedback")

AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".aac", ".wma")

DEFAULTS = {
    "prod_dir": DEFAULT_PROD,
    "import_dir": DEFAULT_IMPORT,
    "asr_cache_dir": DEFAULT_CACHE,
    "feedback_dir": DEFAULT_FEEDBACK,
    # 是否在跑完后自动暂存 ASR（用户可在完成页改主意）
    "auto_keep_asr": True,
    # 上次用的项目号，方便连续处理同一部
    "last_project": "",
    # 实测倍率（秒/每秒音频），跑完自动更新，用于 ETA
    "rate_01": 0.18,
    "rate_03": 0.12,
    # 02/04/05/06 的固定开销合计（秒），同样自适应
    "fixed_tail_sec": 54.0,
    "rate_samples": 0,
}


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_app():
    cfg = dict(DEFAULTS)
    cfg.update(_read_json(APP_CFG, {}))
    return cfg


def save_app(cfg):
    data = {k: v for k, v in cfg.items() if k in DEFAULTS or k.startswith("_")}
    _write_json(APP_CFG, data)


def apply_env(cfg=None):
    """必须在 `import pipeline_lib` 之前调用（main.py 第一件事）。"""
    cfg = cfg or load_app()
    os.environ["KIKOERU_PROD"] = cfg["prod_dir"]
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.makedirs(cfg["prod_dir"], exist_ok=True)
    for d in (cfg["import_dir"], cfg["asr_cache_dir"], cfg["feedback_dir"]):
        os.makedirs(d, exist_ok=True)
    return cfg


# ---------------------------------------------------------------- 模型配置
MODEL_KEYS = [
    ("model_sakura", "初译模型（Sakura GGUF）", "file",
     "03_translate 用的日→中翻译模型，懂 ACGN 词汇"),
    ("model_q5", "润色模型（Qwen2.5-7B Q5，仅旧路线）", "file",
     "只有把 polish_cleanup_only 改成 false 才会用到"),
    ("model_hymt", "HY-MT 模型（旧路线，可留空）", "file",
     "回退路线 engine=hymt 时使用"),
    ("asr_model", "语音识别模型（faster-whisper）", "dir",
     "large-v3 目录，内含 model.bin / config.json"),
    ("llama_dir", "llama.cpp 目录", "dir",
     "需要其中的 llama-server.exe"),
]


def load_local():
    return _read_json(LOCAL_CFG, {})


def save_local(data):
    _write_json(LOCAL_CFG, data)


def set_model(key, value):
    d = load_local()
    d.setdefault("models", {})
    if key == "asr_model":
        d["asr_model"] = value
    else:
        d.setdefault("models", {})[key] = value
    save_local(d)


def get_model(key):
    d = load_local()
    if key == "asr_model":
        return d.get("asr_model", "")
    return (d.get("models") or {}).get(key, "")


def check_models():
    """返回 [(key, label, path, ok, hint)]，供设置页/首页体检用。"""
    out = []
    for key, label, kind, hint in MODEL_KEYS:
        p = get_model(key) or ""
        ok = bool(p) and (os.path.isdir(p) if kind == "dir" else os.path.isfile(p))
        out.append((key, label, p, ok, hint))
    return out


def missing_models():
    """真正会挡住流程的模型。旧路线的 q5 / HY-MT 不算——默认路线用不到它们。"""
    return [m for m in check_models()
            if not m[3] and m[0] not in ("model_hymt", "model_q5")]
