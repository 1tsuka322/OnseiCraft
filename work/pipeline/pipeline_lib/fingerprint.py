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
               "diarize", "diarize_thr", "seg_flush_chars", "chunk")

# 模型路径常量（抄自 serve.py；不 import 它，避免副作用）
# ⚠️ 下面这些是**开发机的路径**，只当兜底默认值：真正的值由 pipeline.local.json 的
#    `models.*` 给（load_config 会深合并进来）。别的机器上这些默认值一律 exists=False，
#    于是「换 Sakura 模型不触发 03 重算」——正是 R156 那类盲区。取的时候先看配置。
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GGUF = {
    # 默认引擎（2026-10-29 起 engine=sakura）。以前漏登记 → 换 Sakura 模型文件不会触发 03 重算，
    # 而 03 的 manifest 里 server_model 只记录、不进哈希（R156 链路验证的盲区之一）。
    "gguf_sakura": r"D:\fasterwhisperGUI-0.8.5.sfx\models\sakura-7b\sakura-7b-v1.0-iq4xs.gguf",
    "gguf_hymt": r"D:\fasterwhisperGUI-0.8.5.sfx\models\hy-mt1.5-7b\HY-MT1.5-7B-Q4_K_M.gguf",
    "gguf_qwen_q5": (r"D:\fasterwhisperGUI-0.8.5.sfx\models\qwen2.5-7b-instruct-gguf"
                     r"\qwen2.5-7b-instruct-q5_k_m-00001-of-00002.gguf"),
    "gguf_qwen_q4": (r"D:\fasterwhisperGUI-0.8.5.sfx\models\qwen2.5-7b-instruct-gguf"
                     r"\qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf"),
}
WHISPER_NAME = "faster-whisper-large-v2"   # 2026-11-06 起默认（见 internal/docs/experiments/ASR_AB_20261106.md）

# 每个阶段实际用到的模型（最小闭包）。**空列表 = 该阶段不调模型**：
# 02/05/06 是纯确定性阶段，以前 names=None 会把 whisper + 全部 gguf 都算进指纹，
# 于是「换一个没被用到的 gguf」也会让它们全量重跑（R154 实测的假 cache miss）。
MODELS_BY_STAGE = {
    "01_asr": ["whisper"],
    "02_asr_fix": [],
    # 03 的默认引擎是 sakura，hymt/qwen 只作回退路线；四个都登记，
    # 因为换任何一个都可能改变输出（回退时 hymt、polish 回退时 q5/q4）。
    "03_translate": ["gguf_sakura", "gguf_hymt", "gguf_qwen_q5", "gguf_qwen_q4"],
    "04_polish": ["gguf_hymt", "gguf_qwen_q5", "gguf_qwen_q4"],
    "05_term_lock": [],
    "06_checks": [],
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


def _knobs():
    """读 `scripts/knobs.py`（旋钮的单一真相）。

    用 `spec_from_file_location` 而不是 `import knobs`：不动 sys.path、不 import scripts 包
    （那里有几十个模块，副作用不可控）。knobs.py 自己保证零依赖零副作用。
    """
    if _KNOBS[0] is not None:
        return _KNOBS[0]
    import importlib.util
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "scripts", "knobs.py")
    spec = importlib.util.spec_from_file_location("_kikoeru_knobs", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _KNOBS[0] = mod
    return mod


def env_whitelist(cfg, stage=None):
    """业务子进程实际会读到的 KIKOERU_*（与 stages.Ctx.__init__ 的注入一致）。

    ⚠️ `KIKOERU_SEG_FLUSH_CHARS` / `KIKOERU_CHUNK` 是**脚本直接从 os.environ 读**的
    （`cues.py` / `translate.py` 都从 `knobs.py` 取），不走 cfg，所以以前完全不在指纹里——
    改 cue 门槛（R156 的核心改动）不会让 03 失效，普通 `run` 会误判「缓存命中」。

    这里只对**真的读它们**的 03/04 补上（04 的 `build_units()` 也走
    `build_semantic_cues`，虽然它的输入里含 03 的草稿、能间接失效，但显式登记更稳），
    避免污染 01/02/05/06（否则改门槛会让那些纯确定性阶段也假 cache miss，白跑几十分钟）。
    值直接取自 knobs，不再抄一份默认值。
    """
    env = {}
    if stage in ("03_translate", "04_polish"):
        env["KIKOERU_SEG_FLUSH_CHARS"] = str(int(cfg.get("seg_flush_chars",
                                                          _knobs().SEG_FLUSH_CHARS)))
        env["KIKOERU_CHUNK"] = str(int(cfg.get("chunk", _knobs().CHUNK)))
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
    """记录一个模型产物的存在性/大小/mtime。

    目录形态（whisper 的 ct2 模型）→ 对 `model.bin` 取大小与 mtime。
    原来直接 `os.stat(目录)`，`bytes` 恒为 0、mtime 是**目录**的，
    换权重文件（同名覆盖）时指纹可能不变 —— 等于让 01 替新模型复用旧产物。
    """
    probe = path
    if os.path.isdir(path):
        mb = os.path.join(path, "model.bin")
        probe = mb if os.path.isfile(mb) else path
    rec = {"path": path, "exists": False, "bytes": None, "mtime": None}
    try:
        st = os.stat(probe)
        rec.update(exists=True, bytes=st.st_size, mtime=int(st.st_mtime))
    except OSError:
        pass
    return rec


def _asr_model_path(cfg):
    """ASR 模型的**实际**路径（与 scripts/asr.py 的 resolve_asr_model 同序）。

    ⚠️ 原来这里写死 `{"name": "faster-whisper-large-v3", "path": None, "exists": True}`
    —— 等于宣称「whisper 一定在、且永远是同一个」，**换模型不会让 01 失效**。
    2026-11-01 用户报错暴露了同一个洞的另一半：asr.py 用的是写死的开发机路径，
    而指纹里那条 whisper 永远是「存在」，所以「配置说 A、实际跑 B」谁都没发现。
    现在按 asr.py 的解析顺序取路径，路径变了 / 大小变了 / mtime 变了 → 01 必然重算。
    """
    p = (cfg or {}).get("asr_model") or ((cfg or {}).get("models") or {}).get("asr_model")
    if not p:
        for k in ("FW_MODEL", "KIKOERU_ASR_MODEL"):
            p = (os.environ.get(k) or "").strip()
            if p:
                break
    if not p:
        cand = os.path.join(_ROOT, "models", WHISPER_NAME)
        p = cand if os.path.isdir(cand) else ""
    return str(p or "")


def model_records(cfg, stage=None):
    """模型指纹：{逻辑名: {path, exists, bytes, mtime}}。stage 给了就只取该阶段用到的。"""
    names = MODELS_BY_STAGE.get(stage) if stage else None
    out = {}
    if names == []:
        return out          # 该阶段不调模型（02/05/06）—— 指纹里不该出现任何 gguf
    if names is None or "whisper" in names:
        ap = _asr_model_path(cfg)
        if ap and (os.sep in ap or "/" in ap or "\\" in ap):
            rec = _stat(ap)
            # name 取**实际目录名**，不写死常量 —— 换模型时指纹要能看出换了哪个。
            rec["name"] = os.path.basename(ap.rstrip("\\/")) or WHISPER_NAME
            out["whisper"] = rec
        else:
            # 规格名（large-v2）：模型在 HF 缓存里，路径不可知 → 只记名字，
            # 但**必须记成「未知」而不是「存在」**，别让指纹替模型打包票。
            out["whisper"] = {"name": ap or WHISPER_NAME, "path": None,
                              "exists": None, "bytes": None, "mtime": None}
    for k, p in GGUF.items():
        if names is None or k in names:
            out[k] = _stat(((cfg or {}).get("models") or {}).get(k) or p)
    return out


def _stable_inputs(inputs):
    """inputs 是 list[dict]，按规范 JSON 排序，保证「顺序变了但内容一样」指纹不变。"""
    if not isinstance(inputs, (list, tuple)):
        return inputs
    return sorted(list(inputs), key=lambda x: _canon(x))


# ---- 管线自身的代码也要进指纹（R154）----
# 各阶段的 `code` 只登记**被该阶段调用的脚本**，`pipeline_lib/*` 不在里面。
# 于是改 stages.py（比如默认引擎、门禁清单、_ensure_server 的行为）时，
# 指纹一点不变 → 旧产物照样「缓存命中」→ 改动等于没生效。
# 这和 R153 是同一类错误：**改了执行层，产物看不出来**。
_RUNNER = [None]
_KNOBS = [None]


def runner_hash():
    """pipeline_lib/*.py 的聚合哈希（不含 __pycache__）。"""
    if _RUNNER[0]:
        return _RUNNER[0]
    d = os.path.dirname(os.path.abspath(__file__))
    parts = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".py"):
            continue
        p = os.path.join(d, fn)
        try:
            parts.append("%s:%s" % (fn, hashlib.sha256(open(p, "rb").read()).hexdigest()))
        except OSError:
            continue
    _RUNNER[0] = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return _RUNNER[0]


def stage_fingerprint(stage, params, inputs, knowledge, code, cfg, models=None):
    payload = {
        "stage": stage,
        "params": params,
        "config": resolved_config(cfg),
        "config_hash": config_hash(cfg),
        "env": env_whitelist(cfg, stage),
        "inputs": _stable_inputs(inputs),
        "knowledge": knowledge,
        "code": code,
        "runner": runner_hash(),
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

    ok.append(("runner_hash 稳定且非空", bool(runner_hash()) and runner_hash() == runner_hash()))
    for name, good in ok:
        print("  %s %s" % ("✅" if good else "❌", name))
    n = sum(1 for _, g in ok if g)
    print("%s fingerprint selftest %d/%d" % ("✅" if n == len(ok) else "❌", n, len(ok)))
    return 0 if n == len(ok) else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
