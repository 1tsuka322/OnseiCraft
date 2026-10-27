# -*- coding: utf-8 -*-
"""阶段指纹的「隐式输入」补全（2026-10-26，按外部评审 T10）。

原来的指纹只有 `params + inputs + knowledge + code`，漏了三类会改变输出、
却不会改变这四个东西的输入：

1. **解析后的配置**（profile/engine/cue_pipeline/slots/allow_loss/spk_prefix/track_memory…）；
2. **业务子进程真正读到的环境变量**（`KIKOERU_*`）；
3. **模型文件**（whisper / GGUF：路径、大小、mtime）。

原则是「影响该阶段输出的最小闭包」：放多了会造成无关的大面积 cache miss，
放少了会产生「产物没重跑、口径已经变了」的静默错产物。所以：
- 主机名、绝对路径、日志、时间、队列状态 —— **不进**；
- 该阶段不读的知识表 —— **不进**；
- 模型只进真正用模型的阶段（01/03/04）。

模型**只 stat 不读内容**：5GB 读一次几十秒，而 size+mtime 足以发现替换。
"""
import hashlib
import json
import os

# 影响输出的配置键（其余如 python 解释器路径、_gates、_note 一律不进）
CONFIG_KEYS = ("engine", "cue_pipeline", "slots", "system_mode", "allow_loss",
               "spk_prefix", "track_memory", "device", "compute", "ext",
               "diarize", "diarize_thr")

# 模型路径常量（抄自 serve.py；不 import 它，避免副作用）
GGUF = {
    "gguf_hymt": r"D:\fasterwhisperGUI-0.8.5.sfx\models\hy-mt1.5-7b\HY-MT1.5-7B-Q4_K_M.gguf",
    "gguf_qwen_q5": (r"D:\fasterwhisperGUI-0.8.5.sfx\models\qwen2.5-7b-instruct-gguf"
                     r"\qwen2.5-7b-instruct-q5_k_m-00001-of-00002.gguf"),
    "gguf_qwen_q4": (r"D:\fasterwhisperGUI-0.8.5.sfx\models\qwen2.5-7b-instruct-gguf"
                     r"\qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf"),
}
WHISPER_NAME = "faster-whisper-large-v3"

# 每个阶段实际用到的模型（最小闭包）
MODELS_BY_STAGE = {
    "01_asr": ["whisper"],
    "03_translate": ["gguf_hymt", "gguf_qwen_q5", "gguf_qwen_q4"],
    "04_polish": ["gguf_hymt", "gguf_qwen_q5", "gguf_qwen_q4"],
}


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def resolved_config(cfg):
    """只保留影响输出的键；profile 从 _profile 取。"""
    out = {}
    for k in CONFIG_KEYS:
        if k in cfg and cfg[k] is not None:
            out[k] = cfg[k]
    if cfg.get("_profile"):
        out["profile"] = cfg["_profile"]
    return out


def config_hash(cfg):
    return hashlib.sha256(_canon(resolved_config(cfg)).encode("utf-8")).hexdigest()


def env_whitelist(cfg):
    """业务子进程实际会读到的 KIKOERU_*（与 stages.Ctx.__init__ 的注入一致）。"""
    env = {}
    if cfg.get("engine"):
        env["KIKOERU_ENGINE"] = str(cfg["engine"])
    if cfg.get("cue_pipeline") is not None:
        env["KIKOERU_CUE_PIPELINE"] = str(int(cfg.get("cue_pipeline", 1)))
    if cfg.get("slots"):
        env["KIKOERU_SLOTS"] = str(int(cfg["slots"]))
    if cfg.get("spk_prefix"):
        env["KIKOERU_SPK_PREFIX"] = "1"
    if cfg.get("track_memory"):
        env["KIKOERU_TRACK_MEMORY"] = "1"
    return env


def _stat(path):
    rec = {"path": path, "exists": False, "bytes": None, "mtime": None}
    try:
        st = os.stat(path)
        rec.update(exists=True, bytes=st.st_size, mtime=int(st.st_mtime))
    except OSError:
        pass
    return rec


def model_records(cfg, stage=None):
    """模型指纹：{逻辑名: {path, exists, bytes, mtime}}。stage 给了就只取该阶段用到的。"""
    names = MODELS_BY_STAGE.get(stage) if stage else None
    out = {}
    if names is None or "whisper" in names:
        out["whisper"] = {"name": WHISPER_NAME, "path": None, "exists": True,
                          "bytes": None, "mtime": None}
    for k, p in GGUF.items():
        if names is None or k in names:
            out[k] = _stat(p)
    return out


def _stable_inputs(inputs):
    """inputs 是 list[dict]，按规范 JSON 排序，保证「顺序变了但内容一样」指纹不变。"""
    if not isinstance(inputs, (list, tuple)):
        return inputs
    return sorted(list(inputs), key=lambda x: _canon(x))


def stage_fingerprint(stage, params, inputs, knowledge, code, cfg, models=None):
    payload = {
        "stage": stage,
        "params": params,
        "config": resolved_config(cfg),
        "config_hash": config_hash(cfg),
        "env": env_whitelist(cfg),
        "inputs": _stable_inputs(inputs),
        "knowledge": knowledge,
        "code": code,
        "models": models if models is not None else model_records(cfg, stage),
    }
    return hashlib.sha256(_canon(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 自检
def _selftest():
    ok = []
    base = {"engine": "hymt", "cue_pipeline": 1, "slots": 2, "system_mode": "strong",
            "device": "cuda", "compute": "float16", "_profile": "production",
            "python": r"C:/a/python.exe", "_gates": ["check"]}
    h1, h2 = config_hash(base), config_hash(dict(base))
    ok.append(("同一配置哈希稳定", h1 == h2))
    ok.append(("cue_pipeline 变化 → 哈希变",
               config_hash(dict(base, cue_pipeline=0)) != h1))
    ok.append(("解释器路径不进哈希",
               config_hash(dict(base, python=r"D:/other/python.exe")) == h1))

    fp1 = stage_fingerprint("04_polish", {"slots": 2},
                            [{"path": "b"}, {"path": "a"}], {}, {"polish.py": "x"}, base)
    fp2 = stage_fingerprint("04_polish", {"slots": 3},
                            [{"path": "a"}, {"path": "b"}], {}, {"polish.py": "x"}, base)
    fp3 = stage_fingerprint("04_polish", {"slots": 2},
                            [{"path": "a"}, {"path": "b"}], {}, {"polish.py": "x"}, base)
    ok.append(("params 变化 → 指纹变", fp1 != fp2))
    ok.append(("inputs 顺序不影响指纹", fp1 == fp3))

    for name, good in ok:
        print("  %s %s" % ("✅" if good else "❌", name))
    n = sum(1 for _, g in ok if g)
    print("%s fingerprint selftest %d/%d" % ("✅" if n == len(ok) else "❌", n, len(ok)))
    return 0 if n == len(ok) else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
