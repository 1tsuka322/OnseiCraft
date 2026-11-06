# -*- coding: utf-8 -*-
"""管线旋钮的**单一真相**（默认值 + 环境变量覆盖）。

为什么单独一个文件：`SEG_FLUSH_CHARS` / `CHUNK` 这类旋钮以前各写一份默认值——
`cues.py` 写 10、`translate.py` 写 3，而 `pipeline_lib/stages.py`（进指纹的 params）
和 `pipeline_lib/fingerprint.py`（进指纹的 env）又各抄一份。
只要有人改了脚本里的默认值，指纹那边就还在报旧值 → 又是「声明 ≠ 实际」
（R156 的教训：改了 cue 门槛，03 指纹不变，run 误判缓存命中）。

现在四处都从这里取，改默认值只改这一处。

**入口顺序（2026-10-30 收敛）**：`pipeline.json` 的 `seg_flush_chars` / `chunk`
→ `Ctx` 注入 `KIKOERU_*` 环境变量 → 本模块读取。
也就是说**正常跑管线时改 `pipeline.json` 就够了**；环境变量仍可临时覆盖（做 A/B 用），
本文件的默认值只在「不经过管线、直接跑脚本」时生效。

⚠️ 本文件必须**零副作用、零第三方依赖**：`pipeline_lib` 会用
`spec_from_file_location` 直接加载它（不 import scripts 包、不动 sys.path）。
"""
import os

# 段边界处当前 cue 字数 ≥ 此值 → 切（越小切得越细）
SEG_FLUSH_CHARS = int(os.environ.get("KIKOERU_SEG_FLUSH_CHARS", "10"))

# 每块多少行一起送翻译（越小越不容易跨行重排，但调用次数变多）
CHUNK = int(os.environ.get("KIKOERU_CHUNK", "3"))

# 空译文占位符（03 写盘用；06 的 empty 门禁也认它）
FALLBACK = "…"


def snapshot():
    """给指纹用：当前实际生效的旋钮值。"""
    return {"seg_flush_chars": SEG_FLUSH_CHARS, "chunk": CHUNK}


# 写进 LRC `[by:]` 的引擎显示名。**必须与实际加载的 gguf 一致**——
# R153 事故（声明 hymt 实际跑 Qwen）就是靠读这个标签发现的，
# 所以标签本身不能是写死的常量。`translate.py`（初译草稿）与 `polish.py`（成品）
# 共用这一张表，避免两处各写一份、跑 Sakura 却标 Qwen。
ENGINE_TAGS = {
    "sakura": "Sakura-7B-Qwen2.5-v1.0",
    "hymt": "HY-MT1.5-7B",
    "qwen3": "Qwen3-8B",
}
_ENGINE_DEFAULT = "Qwen2.5-7B"


def engine_tag(engine=None):
    """引擎名 → LRC 标签用的显示名。未知引擎回落到 Qwen2.5-7B（旧行为）。"""
    e = (engine if engine is not None else os.environ.get("KIKOERU_ENGINE", "qwen")) or "qwen"
    return ENGINE_TAGS.get(str(e).strip().lower(), _ENGINE_DEFAULT)


_ASR_DEFAULT = "faster-whisper-large-v2"


def asr_tag():
    """LRC `[by:]` 里的 ASR 显示名：**取实际生效的模型**，不写死。

    2026-11-06 换成 large-v2 后发现 `translate.py` / `polish.py` 都把
    `"faster-whisper large-v3"` 写死在标签里 —— 换模型后**每一份成品的 [by:] 都在撒谎**。
    管线里 `Ctx.env` 已经注入 FW_MODEL / KIKOERU_ASR_MODEL（与 01 命令行的 --model 同源），
    直接跑脚本时退回到读 pipeline.local.json，最后才用默认目录名。
    """
    for key in ("FW_MODEL", "KIKOERU_ASR_MODEL"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return os.path.basename(v.replace("\\", "/").rstrip("/")) or v
    try:
        import json
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "pipeline", "pipeline.local.json")
        with open(p, encoding="utf-8") as f:
            v = ((json.load(f) or {}).get("asr_model") or "").strip()
        if v:
            return os.path.basename(v.replace("\\", "/").rstrip("/")) or v
    except Exception:
        pass
    return _ASR_DEFAULT
