# -*- coding: utf-8 -*-
r"""
kikoeru ASR：把音频识别为日文，产出含词级时间戳的 JSON。

用法：
  python asr.py <音频目录> <输出目录> [扩展名|auto] [--recursive] [--device cuda] [--compute float16]

例：
  python asr.py "$KIKOERU_ROOT/RJ1401520/xxx/02_mp3" "./asr_out" mp3
  python asr.py "$KIKOERU_ROOT/RJ9000005/wav" "./asr_out" wav --recursive

输出：<输出目录>\<音频文件名>.json
  {"source":..., "language":"ja", "duration":..., "segments":[{start,end,text,words:[{s,e,w}]}]}
已存在的 json 会跳过（断点续跑）。
"""
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json
import time
import glob
import re
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr_halluc  # noqa: E402  三门槛幻觉判据（词级时间戳），与 asr.py 同目录

# 默认 ASR 模型目录名（2026-11-06 起 = large-v2，见 resolve_asr_model 的注释）
ASR_MODEL_DIRNAME = "faster-whisper-large-v2"


def resolve_asr_model(explicit=None):
    """解析 ASR 模型：返回 (值, 来源)。值可以是**本地目录**或 faster-whisper 的**规格名**。

    优先级：`--model` → FW_MODEL → KIKOERU_ASR_MODEL → pipeline.json → pipeline.local.json
            → `<root>/models/faster-whisper-large-v2` → 规格名 "large-v2"（让 faster-whisper 自己下）。

    ⚠️ 2026-11-06 起默认模型是 **large-v2**（不是 v3）。两条带官方台本的轨实测：
    表記 CER 17.4%/19.0% vs v3 的 22.5%/21.2%，读音 CER 7.4%/9.6% vs 7.9%/10.5%，
    汉字率也更高（19.6%/18.1% vs 17.0%/17.5%）。详见
    `internal/docs/experiments/ASR_AB_20261106.md`。

    ⚠️ 2026-11-01 用户报错（OnseiCraft 0.1.3）：这里原来是
    `os.environ.get("FW_MODEL", r"D:\\fasterwhisperGUI-0.8.5.sfx\\models\\...")`——
    **开发机的绝对路径当默认值**，换台机器就指向不存在的目录，faster-whisper 抛
    `ValueError: Invalid model size 'D:\\fasterwhisperGUI-...'`。
    更糟的是它**绕过了配置**：界面里 asr_model 明明填对了（诊断报告显示 OK），
    实际用的还是那个写死的路径 —— 又一次「声明 ≠ 实际」。
    现在只认配置与环境变量，并且把「值从哪来」一起返回，好写进日志。
    """
    cands = []
    if explicit:
        cands.append(("--model", explicit))
    for key in ("FW_MODEL", "KIKOERU_ASR_MODEL"):
        val = (os.environ.get(key) or "").strip()
        if val:
            cands.append((key, val))

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.environ.get("KIKOERU_ROOT", os.path.dirname(here))
    cfg_p = os.environ.get("KIKOERU_CONFIG") or os.path.join(root, "pipeline", "pipeline.json")
    for cp in (cfg_p, os.path.join(os.path.dirname(cfg_p), "pipeline.local.json")):
        if os.path.isfile(cp):
            try:
                with open(cp, encoding="utf-8") as f:
                    d = json.load(f) or {}
                m = (d.get("asr_model") or (d.get("models") or {}).get("asr_model") or "").strip()
                if m:
                    cands.append((os.path.basename(cp), m))
            except Exception:
                pass

    for base in (root, os.path.dirname(root)):
        cand = os.path.join(base, "models", ASR_MODEL_DIRNAME)
        if os.path.isdir(cand):
            cands.append(("默认相对目录", cand))

    if cands:
        return cands[0][1], cands[0][0]
    return ASR_MODEL_DIRNAME, "内置规格名（faster-whisper 自行下载）"


def _looks_like_path(s):
    return ("/" in s) or ("\\" in s) or bool(re.match(r"^[A-Za-z]:", s))


def check_asr_model(model, source):
    """路径形态的模型必须真的可用，否则**说人话**退出，别让 faster-whisper 抛那句天书。

    这条是「声明 == 实际」的检查点：配置说用 A，就必须真能用 A；
    不能用就立刻停，而不是悄悄回落到别的模型（那会产出「看着正常、其实换了模型」的成品）。
    """
    if not _looks_like_path(model):
        return                                  # 规格名（large-v3 等）交给 faster-whisper
    if not os.path.isdir(model):
        raise SystemExit(
            "ASR 模型目录不存在：%s\n"
            "  来源：%s\n"
            "  → 修法（任选一条）：\n"
            "     1) 打开 OnseiCraft → 设置 → 「语音识别模型」选到 faster-whisper-large-v2 目录\n"
            "     2) 在 work/pipeline/pipeline.local.json 里写 \"asr_model\": \"<模型目录>\"\n"
            "     3) 设环境变量 FW_MODEL=<模型目录>\n"
            "  该目录里应有 model.bin / config.json / tokenizer.json / vocabulary.json"
            % (model, source))
    if not os.path.isfile(os.path.join(model, "model.bin")):
        raise SystemExit(
            "ASR 模型目录缺 model.bin：%s\n"
            "  来源：%s\n"
            "  → 目录选对了但没下完，或选到了上一级。请重新下载/解压 faster-whisper-large-v2。"
            % (model, source))


MODEL, MODEL_SRC = resolve_asr_model()
AUDIO_EXTS = ["wav", "mp3", "m4a", "flac", "ogg", "opus", "aac", "wma"]

# 分块识别参数（见 SKILL.md：整轨识别会让低语段大面积丢失）
CHUNK_SEC = float(os.environ.get("FW_CHUNK", 60))     # 每块时长
CHUNK_OV = float(os.environ.get("FW_CHUNK_OV", 5))    # 块间重叠
# 长轨保护（2026-10-21，移植 WhisperJAV 对 #394「长音频停输出」的防御）：
# 单轨超过此秒数 → 分块识别且**每块重建一次识别器**（默认 20 分钟）。
# 我们的 35–50 分钟长轨暂未复现该故障，但用户判断会遇到 → 默认开启。
# ⚠️ 2026-10-28 R154：原来这里是 1200（20 分钟），而且它**同时被当成「分块大小」**。
#    于是一条 21 分钟的轨 → 切出一个 20 分钟的块 → 单块的激活值就把 8GB 卡打满
#    （实测显存 7859/8192、GPU 100%、45 分钟跑不完一块）。
#    「重载间隔」和「分块大小」是两件事，必须分开：
#      · RELOAD_SEC：多久重建一次识别器（防解码状态腐坏），默认 40 分钟
#      · LONG_CHUNK：长轨按多大切块（控激活值），默认 10 分钟
RELOAD_SEC = float(os.environ.get("KIKOERU_ASR_RELOAD_SEC", "2400"))
LONG_CHUNK = float(os.environ.get("KIKOERU_ASR_LONG_CHUNK", "600"))
RELOAD_OV = 2.0
NO_SPEECH = float(os.environ.get("FW_NO_SPEECH", 0.85))
# ⚠️ 2026-10-19 实测（P2-6）：**只看 nsp 的硬阈值会误杀真实台词**。
#    RJ9000002 Tr.06 一轨：93 段里有 **27 段** nsp 在 0.86–0.94（BGM 把 nsp 抬高了），
#    但 `avg_logprob` 只有 -0.19~-0.36（模型很确信）——整段开头 0–54 秒的对话被删光。
#    所以判「静音」必须**两个条件同时成立**：nsp 高 **且** avg_logprob 差（≤ -1.0）。
#    这条与 README 6.3.8 的结论一致：nsp 是弱信号，字符密度/置信度才是强信号。
NO_SPEECH_LP = float(os.environ.get("FW_NO_SPEECH_LP", -1.0))


def is_silence_seg(s):
    """这段是不是「静音上的幻觉」——nsp 高 **且** 置信度差。

    ⚠️ 别退回「只看 nsp」：ASMR 全程有 BGM/喘息，nsp 中位就有 0.23、p90 0.58，
       软语/BGM 段动辄 0.86+，但 avg_logprob 很好。只看 nsp 会成片丢真台词。
    """
    nsp = float(getattr(s, "no_speech_prob", 0) or 0)
    lp = float(getattr(s, "avg_logprob", 0) or 0)
    return nsp >= NO_SPEECH and lp <= NO_SPEECH_LP
# ⛔ 2026-10-19（P2-6 三臂 A/B 证伪）：曾以为「开头丢 30 秒」是 faster-whisper 内部
#    滑窗丢弃、要靠显式 `no_speech_threshold` 才能从源头堵住。**实测完全无效**——
#    阈值 0.85 / 默认 × 后过滤开 / 关，段数与首段起点逐字相同（93/93），
#    差异 100% 来自上面的段级后过滤（SKILL.md 坑 72）。`_ns_kw()` 仅为对照实验保留。
NS_THRESHOLD = os.environ.get("FW_NS_THRESHOLD", "").strip()


def _ns_kw():
    """给 transcribe() 的可选参数。⚠️ 实测 `no_speech_threshold` 对整轨识别
    没有任何效果（P2-6），只留着做 A/B 对照；正常流程不要设。"""
    if not NS_THRESHOLD:
        return {}
    return {"no_speech_threshold": float(NS_THRESHOLD)}


def _transcribe_with_fallback(model, audio, vad=False):
    """整轨/分块识别，带降级重试。返回 (segments_list, info, fallback_tag)。

    ⚠️ 2026-11-07 实测（RJ1667609 巫女轨，faster-whisper 1.2.1）：
      `condition_on_previous_text=False` + `word_timestamps=True` + `beam_size=5`
      **三者同时满足**时，某类音频会抛
        IndexError: boolean index did not match indexed array along axis 0
      崩在 `for s in segments` **迭代时**（不是 transcribe 返回时），所以外层
      try 能兜住，但整条轨直接作废。逐项排除：
        去掉 word_timestamps     → 正常（但丢词级时间戳，管线 5 处依赖）
        beam_size=1              → 正常（只降搜索宽度，词级时间戳保住）
      是上游对齐阶段 `all_tokens` 索引越界，我们改不了，只能降级。

    降级顺序：beam 5 → 1。**不动 word_timestamps**（它是硬依赖）。
    """
    kw = dict(language="ja", vad_filter=vad, condition_on_previous_text=False,
              word_timestamps=True, initial_prompt="以下是一段日语对话的转写。",
              **_ns_kw())
    try:
        segs, info = model.transcribe(audio, beam_size=5, **kw)
        return list(segs), info, None
    except IndexError as e:
        if "boolean index" not in str(e):
            raise
        print("    [降级重试] word_timestamps+beam5 触发上游 IndexError → beam_size=1",
              flush=True)
        segs, info = model.transcribe(audio, beam_size=1, **kw)
        return list(segs), info, "beam1"


r"""Whisper 在静音/音乐/呼吸段会套用训练语料里的 YouTube 套话编造台词。

音声作品里不可能出现"下集视频见""本片字幕由观众制作""感谢收看"这类句子，
出现即判为幻觉。实测 RJ9000006 的 7 轨里命中 6 处（全部落在开头静音区）。
只对「整句就是套话」的短句生效，避免误伤正常台词。
"""
HALLU = [
    r"(次|次の|次回の)?(動画|ビデオ)で(お会い|会い)(しましょう|ましょう|ませ)",
    r"(この)?(動画|ビデオ|映像)の?字幕は.{0,14}(作成|制作|つくっ|付け)",
    r"字幕は(視聴者|リスナー)",
    r"自動(生成)?字幕",
    r"字幕(提供|協力|は.{0,6}による)",
    r"チャンネル登録",
    r"(高評価|いいね|グッドボタン)(お願い|して|よろしく|ください)",
    r"コメント(お待ち|して|ください|よろしく)",
    r"最後まで(ご視聴|ご覧|お見届け|見て)",
    r"(次回|次の動画|次週)も(お楽しみ|見て|よろしく|お会い)",
    r"再生リスト|関連動画",
    r"^次回(予告|のお知らせ)$",
    r"^(ご|御)視聴(ありがとう|いただき)",
    r"^(お|ご)?(楽しみ|たのしみ)に(して|してい)",
    r"^(チャンネル|高評価|いいね|登録)",
]
# 这几句在 free talk 里可能是**真的**：声优收尾时常说「ご視聴ありがとうございました」
# 「お疲れ様でした」。所以单独放一组，只在非 free talk 轨上才当幻觉剔除。
HALLU_SOFT = [
    r"(ご視聴|ご清聴|ご視聴いただき|聞いて(くださ|いただ))ありがとう(ございました|ました)",
    r"お疲れ様でした",
    r"それではまた(お会い|ね|)",
]
HALLU_RE = [re.compile(p) for p in HALLU]
HALLU_SOFT_RE = [re.compile(p) for p in HALLU_SOFT]
HALLU_MAXLEN = 40
# ---- 2026-10-14 新增：非日文文字 / 纯拉丁单词 = Whisper 串语言幻觉 ----
# 全库 22073 段里 182 段（0.8%）是这类垃圾，全部出现在**喘息/噪声**处：
#   `ded` / `lin` / `Cuba` / `Damage` / `KUKENAI` / `wwwww`（噪声上幻觉出的英文单词）
#   `услわんないよー` / `всеバラメ` / `либо紹介しておきましょう`（串成西里尔/阿拉伯文）
# 这类段本身没有语义，原样进翻译会产出「ded」这种垃圾行（实测 RJ9000010 Tr.10 07:06）。
# ⚠️ 判据要**只认明显非日文**的东西，别误伤：
#   · 西里尔/谚文/阿拉伯/泰文/天城文/希腊文 → 日语 ASR 绝不可能出现，直接丢
#   · 纯拉丁字母且 ≥3 字（无数字/空格）→ 噪声上的英文幻觉；`OK`(2 字) 这类保留
HALLU_SCRIPT = re.compile(
    r"[\u0400-\u04ff\u0500-\u052f\uac00-\ud7af\u0600-\u06ff\u0750-\u077f"
    r"\u0e00-\u0e7f\u0900-\u097f\u0980-\u09ff\u0370-\u03ff\u0590-\u05ff]")
HALLU_LATIN = re.compile(r"^[A-Za-z]{3,}$")
# free talk 轨：文件名里出现这些词就按"声优谈话"处理
FT_NAME = re.compile(r"(free\s*talk|フリー\s*トーク|フリートーク|おまけ|あとがき|後記)", re.I)

# ---------- 重复循环伪影 ----------
# Whisper 在「一段够长但没有明确语音」的音频上会陷入字符级重复循环。
# 实测：RJ9000011 Track04 有 27 秒全是「あ」、RJ9000002 トラック2 有 28 秒
# 全是「ぎゅー」、RJ9000002 トラック4 有 11 秒「カチャカチャ」。
# 这种段原样进翻译只会产出成串雷同的垃圾行。
# 判定刻意保守（要求既"长"又"高度重复"），因为 ぴゅぴゅぴゅ／好き好き好き／
# シコシコ 这类 2-7 秒的重复是音声作品里**真实的连续喘息**，必须保留。
REP_MAX_SEC = 10.0     # 段长 >= 此值才考虑是循环伪影
REP_MIN_TIMES = 6      # 同一最短单元重复次数 >= 此值
REP_KEEP = 3           # 折叠后保留几遍
REP_LONG = 30          # 归一化文本长度 >= 此值且字符种类极少 -> 也判为循环伪影
REP_DISTINCT = 3       # 允许的不同字符数
REP_RUN_KEEP = 4       # 每个 run 最多保留几个字


def _min_period(t):
    """找最短重复单元长度；不是循环则返回 None。"""
    n = len(t)
    for L in range(1, n // 2 + 1):
        if all(t[i] == t[i % L] for i in range(n)):
            return L
    return None


def _runs(t):
    out = []
    for c in t:
        if out and out[-1][0] == c:
            out[-1][1] += 1
        else:
            out.append([c, 1])
    return [tuple(r) for r in out]


def collapse_repetition(text, start, end):
    r"""把超长字符循环段折叠成一小段代表性文本。

    返回 (文本, 是否折叠)。不够格就原样返回。

    覆盖两种形态：
      · 单一单元循环：`ぎゅー`×30（28 秒）、`あ`×50（27 秒）
      · 多 run 循环：  `あ`×100 + `う`×127（16.6 秒）——不是一个单元，
        但整段只有 2 种字符，仍是呻吟循环而非台词
    """
    dur = (end or 0) - (start or 0)
    t = re.sub(r"[\s。、，.!！?？…♪~〜ー♡]+", "", text or "")
    if dur < REP_MAX_SEC or len(t) < 8:
        return text, False
    L = _min_period(t)
    if L and L <= 6 and len(t) // max(L, 1) >= REP_MIN_TIMES:
        return t[:L] * REP_KEEP, True
    # 多 run：字符种类极少的超长段
    if len(t) >= REP_LONG and len(set(t)) <= REP_DISTINCT:
        rs = _runs(t)
        if len(rs) <= REP_DISTINCT and all(n >= 4 for _, n in rs):
            return "".join(c * min(n, REP_RUN_KEEP) for c, n in rs), True
    return text, False


def is_freetalk(name):
    return bool(FT_NAME.search(name or ""))


def is_hallucination(text, freetalk=False):
    t = re.sub(r"[\s。、，.!！?？…♪~〜ー]+$", "", (text or "").strip())
    if not t:
        return False
    # 非日文文字 / 纯拉丁单词（2026-10-14）：不受长度上限约束，先判
    if HALLU_SCRIPT.search(t) or HALLU_LATIN.match(t):
        return True
    if len(t) > HALLU_MAXLEN:
        return False
    if any(r.search(t) for r in HALLU_RE):
        return True
    # free talk 里感谢收听是真实台词，不能删
    return (not freetalk) and any(r.search(t) for r in HALLU_SOFT_RE)


def is_ts_hallucination(s, freetalk=False):
    """段级「词级时间戳崩塌 / 幻觉填充词」判定（2026-10-19 接入 asr_halluc 三门槛）。

    `is_hallucination()` 只看**文本**；这里补 asr_halluc.py 的判据：
      A 幻觉词（`おやすみなさい` 等）独占 ≥8s、整句 ≤15 字、**词跨 ≥10s**；
      B 词级时间戳崩塌（单字跨 ≥15s 或零宽词占比 ≥60%，且时长 ≥10s 且 <15 字）。
    多个条件缺一不可，所以不会误杀软语/长音（0.2s 气声天然零宽占比高）。

    ⚠️ 接入原因：P2-6 放松段级过滤后（nsp **且** lp），原来被「只看 nsp」顺带拦掉的
    一部分静音幻觉会溜回来，需要这条在三门槛上兜底。判据细节见 asr_halluc.py。
    ⚠️ free talk 的感谢/晚安是真实台词，A 类在 free talk 里不判（与 is_hallucination 同口径）。
    """
    seg = {"text": getattr(s, "text", "") or "",
           "start": float(getattr(s, "start", 0) or 0),
           "end": float(getattr(s, "end", 0) or 0),
           "words": [{"s": float(w.start or 0), "e": float(w.end or 0)}
                     for w in (getattr(s, "words", None) or [])
                     if getattr(w, "start", None) is not None
                     and getattr(w, "end", None) is not None]}
    r = asr_halluc.scan_one(seg)
    if not r:
        return None
    if freetalk and str(r.get("kind", "")).startswith("A_"):
        return None
    return r


def collect(src, ext, recursive):
    # 目录名里常带 []（如 "[RJ432405]作品名"），glob 会把 [] 当字符类导致匹配为空，
    # 必须用 glob.escape 转义路径部分（只转义目录，通配符部分保持原样）。
    base = glob.escape(src)
    files = []
    if ext and ext != "auto":
        pat = os.path.join(base, "**", "*." + ext) if recursive else os.path.join(base, "*." + ext)
        files = glob.glob(pat, recursive=recursive)
    else:
        for e in AUDIO_EXTS:
            pat = os.path.join(base, "**", "*." + e) if recursive else os.path.join(base, "*." + e)
            files.extend(glob.glob(pat, recursive=recursive))
    return sorted(files)


def probe_duration(path):
    r"""用 av 探测容器报告的时长（估算值）。

    注意：mp3 没有 Xing 头时 av 会用「首帧码率 × 文件大小」估算，
    所以文件被截断或填充垃圾数据时，这个值会**虚高**。
    真正能解出多少音频，要看 faster-whisper 的 info.duration。
    两者一比就能自动揪出下载不完整的文件。
    """
    try:
        import av
        with av.open(path) as c:
            return (c.duration or 0) / 1e6
    except Exception:
        return -1


def transcribe_chunked(model, path, chunk=CHUNK_SEC, ov=CHUNK_OV, name="", factory=None,
                       holder=None):
    r"""分块独立识别，再合并。**默认不使用**，保留仅作对照实验用。

    曾经以为分块更好（覆盖率 66% -> 75%），但用 RJ1267421 的人工字幕做标准答案
    评测后证明相反：整轨召回率 95.3%，60 秒块 91.5%，30 秒块 88.9%，块越小越差。
    覆盖率是个误导性指标——分块会让喘息区的段时间戳膨胀，覆盖率虚高但真召回更低。
    用 --chunked 才会走到这里。
    """
    from faster_whisper.audio import decode_audio
    import re as _re

    pcm = decode_audio(path, sampling_rate=16000)
    total = len(pcm) / 16000.0
    step = max(1.0, chunk - ov)
    ft = is_freetalk(name)
    out, pos = [], 0.0
    while pos < total - 1.0:
        if factory is not None:
            # 每块重建识别器：长轨（>RELOAD_SEC）下防止解码状态腐坏导致后半段停输出。
            # ⚠️⚠️ 2026-10-28 R154 修：原来 `model = None` 只清掉**本函数的局部引用**，
            #    调用方那个 `model` 还活着 → 旧模型根本没释放 → 每块再建一个，
            #    VRAM 逐块累积。实测 RJ1623028（21 分钟轨 → 2 块）：显存打到 7859/8192，
            #    GPU 100% 但 45 分钟跑不完一块（模型被挤到反复换入换出）。
            #    改成用 holder 传递引用，重建时把旧对象彻底丢掉。
            try:
                import gc as _gc
                if holder is not None:
                    holder[0] = None
                model = None
                _gc.collect()
                try:
                    import torch as _torch
                    _torch.cuda.empty_cache()
                except Exception:
                    pass
                model = factory()
                if holder is not None:
                    holder[0] = model
            except Exception as e:
                print("    [重载失败，沿用旧模型] %s" % str(e)[:100], flush=True)
        seg = pcm[int(pos * 16000): int(min(pos + chunk, total) * 16000)]
        if len(seg) < 16000:
            break
        try:
            segments, _, _fb = _transcribe_with_fallback(model, seg, vad=False)  # 块内降级只影响该块
        except Exception as e:
            print("    [块失败] %.0fs: %s" % (pos, str(e)[:120]), flush=True)
            pos += step
            continue
        block_end = pos + chunk
        for s in segments:
            if is_silence_seg(s):
                continue
            hr = is_ts_hallucination(s, ft)
            if hr:
                print("    [时间戳幻觉已剔除] %.1fs %r（%s）"
                      % (pos + float(s.start), (s.text or "").strip()[:28], hr.get("kind")),
                      flush=True)
                continue
            txt = (s.text or "").strip()
            if not txt or is_hallucination(txt, ft):
                continue
            st, en = pos + float(s.start), pos + float(s.end)
            # 落在重叠区里开始的句子交给下一块，避免被块尾切断
            if st >= block_end - ov and block_end < total:
                continue
            words = []
            for w in (s.words or []):
                if w.start is None or w.end is None:
                    continue
                words.append({"s": round(pos + float(w.start), 3),
                              "e": round(pos + float(w.end), 3), "w": w.word})
            out.append({"start": round(st, 3), "end": round(en, 3),
                        "text": txt, "words": words})
        pos += step

    out.sort(key=lambda d: d["start"])
    # 去掉重叠区产生的相邻重复句
    ded, last = [], ""
    for d in out:
        key = _re.sub(r"\s", "", d["text"])
        if key and key == last:
            continue
        last = key
        ded.append(d)
    return ded, total


def _can_reuse(path, model_tag, a):
    """旧 json 能不能当「当前配置的产物」复用。返回 (能否复用, 原因)。

    ⚠️ 2026-11-06：以前只看文件存不存在。换模型重跑 01 时 asr.py 6 轨全跳过，
    阶段 7 秒"成功"、manifest 被写成新模型、产物还是 9 天前的旧 json，
    而 acceptance 拿刚写进去的指纹和当前代码比 → 必然相等 → 全绿。
    现在比对 asr_meta（产出参数）；**没有 asr_meta 的旧文件一律不复用**
    —— 宁可贵一次真跑，也不要再出现"声称跑了其实没跑"。
    """
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        return False, "读不出来（%s）" % str(e)[:40]
    if d.get("error"):
        return False, "上次识别失败（%s）" % str(d["error"])[:40]
    m = d.get("asr_meta") or {}
    if not m:
        return False, "旧产物没记产出参数"
    if m.get("model") != model_tag:
        return False, "模型不同：%s ≠ %s" % (m.get("model"), model_tag)
    if m.get("compute") != a.compute:
        return False, "精度不同：%s ≠ %s" % (m.get("compute"), a.compute)
    if bool(m.get("vad")) != bool(a.vad):
        return False, "VAD 开关不同"
    if bool(m.get("chunked")) != bool(a.chunked):
        return False, "分块模式不同"
    return True, ""


def main():
    global MODEL, MODEL_SRC
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("ext", nargs="?", default="auto")
    ap.add_argument("--model", default=None,
                    help="faster-whisper 模型目录或规格名（默认按 环境变量 → pipeline 配置 解析）")
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--redo", action="store_true",
                    help="忽略已存在的 json，强制重新识别。默认是断点续跑（已有结果就跳过）；"
                         "⚠️ 不加这个参数时，`pipeline run --force` 只会绕过阶段级指纹缓存，"
                         "进到本脚本后仍会因为 json 已存在而 6 轨全跳过 —— "
                         "manifest 被改写成新模型、产物却是旧的，验收还会「全绿」（2026-11-06 踩到）")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute", default="float16")
    ap.add_argument("--vad", action="store_true",
                    help="开启 VAD（默认关闭：音声作品里连续的喘息/呻吟会被 VAD 判成一整段语音，"
                         "导致最长 300+ 秒的超长段，模型大量丢词并编造文本。实测关闭后段数 18->208）")
    ap.add_argument("--chunked", action="store_true",
                    help="改用分块识别（默认整轨：以人工字幕为标准答案实测，"
                         "整轨召回率 95.3%% 优于 60 秒块的 91.5%% 和 30 秒块的 88.9%%）")
    ap.add_argument("--chunk", type=float, default=CHUNK_SEC, help="分块模式下的块长秒数")
    ap.add_argument("--overlap", type=float, default=CHUNK_OV, help="分块模式下的重叠秒数")
    a = ap.parse_args()

    if a.model:
        MODEL, MODEL_SRC = a.model, "--model"
    # 先用后不校验 = 报错在 faster-whisper 里，用户只看到 "Invalid model size"。
    check_asr_model(MODEL, MODEL_SRC)

    os.makedirs(a.out, exist_ok=True)
    files = collect(a.src, a.ext, a.recursive)
    if not files:
        print("没有找到音频文件：%s (ext=%s)" % (a.src, a.ext))
        sys.exit(1)

    from faster_whisper import WhisperModel
    # 别写死 large-v3：换模型（moss / 微调版 / 别的规格名）时日志要能对上，
    # 否则「声明 == 实际」在日志层面就已经撒谎了。
    MODEL_TAG = (os.path.basename(os.path.normpath(MODEL))
                 if _looks_like_path(MODEL) else MODEL)
    print("加载模型 %s（来源 %s）(%s / %s) ..."
          % (MODEL_TAG, MODEL_SRC, a.device, a.compute), flush=True)
    model = WhisperModel(MODEL, device=a.device, compute_type=a.compute)
    print("就绪，共 %d 个文件\n" % len(files), flush=True)

    t_all = time.time()
    suspects = []
    for idx, path in enumerate(files, 1):
        name = os.path.basename(path)
        out_path = os.path.join(a.out, name + ".json")
        if os.path.exists(out_path) and not a.redo:
            _ok, _why = _can_reuse(out_path, MODEL_TAG, a)
            if _ok:
                print("[%d/%d] 跳过（产出参数一致，合法复用）%s" % (idx, len(files), name),
                      flush=True)
                continue
            print("[%d/%d] 旧产物不可复用（%s）→ 重跑 %s" % (idx, len(files), _why, name),
                  flush=True)
        t0 = time.time()
        print("[%d/%d] 识别中: %s" % (idx, len(files), name), flush=True)
        dropped = []
        collapsed = []
        _long = False
        _beam_fb = None      # 记录本条轨是否降级过（写进 asr_meta）
        if not a.chunked:
            _d = probe_duration(path)
            if _d > RELOAD_SEC > 0:
                _long = True
                print("    [长轨保护] %.0f 分钟 > %.0f 分钟阈值 → 分块识别 + 每块重建识别器"
                      % (_d / 60.0, RELOAD_SEC / 60.0), flush=True)
        try:
            if not a.chunked and not _long:
                segments, info, _beam_fb = _transcribe_with_fallback(model, path, vad=a.vad)
                data = []
                for s in segments:
                    if is_silence_seg(s):
                        continue
                    if not (s.text or "").strip():
                        continue
                    if is_hallucination(s.text, is_freetalk(name)):
                        dropped.append((round(float(s.start), 1), s.text.strip(), "text"))
                        continue
                    hr = is_ts_hallucination(s, is_freetalk(name))
                    if hr:
                        dropped.append((round(float(s.start), 1), s.text.strip(),
                                        str(hr.get("kind"))))
                        continue
                    txt, was_coll = collapse_repetition(
                        s.text, float(s.start), float(s.end))
                    if not txt:
                        continue
                    if was_coll:
                        collapsed.append((round(float(s.start), 1), s.text.strip()))
                    words = [{"s": round(float(w.start), 3), "e": round(float(w.end), 3), "w": w.word}
                             for w in (s.words or []) if w.start is not None] if not was_coll else []
                    # ⚠️ 保存置信度（2026-10-15 加）：`no_speech_prob` / `avg_logprob`
                    #    是判「这段到底有没有说话」的关键，之前没存 → 无法区分
                    #    「真台词但段尾拖长」和「静音上的纯幻觉」。
                    #    实测 `おやすみなさい` 在静音上被幻觉出来，段长 24.7 秒。
                    conf = {"no_speech_prob": round(float(getattr(s, "no_speech_prob", -1)), 4),
                            "avg_logprob": round(float(getattr(s, "avg_logprob", 0)), 4)}
                    data.append({"start": round(float(s.start), 3), "end": round(float(s.end), 3),
                                 "text": txt, "words": words, **conf,
                                 "collapsed": True} if was_coll else
                                {"start": round(float(s.start), 3), "end": round(float(s.end), 3),
                                 "text": txt, "words": words, **conf})
                dur = float(info.duration)
            else:
                _mk = (lambda: WhisperModel(MODEL, device=a.device, compute_type=a.compute)) \
                    if _long else None
                _holder = [model]
                data, dur = transcribe_chunked(
                    model, path,
                    (min(RELOAD_SEC, LONG_CHUNK) if _long else a.chunk),
                    RELOAD_OV if _long else a.overlap,
                    name=name, factory=_mk, holder=_holder)
        except Exception as e:
            print("    [失败] %s: %s" % (name, str(e)[:200]), flush=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({"source": name, "language": "ja", "duration": 0,
                           "segments": [], "error": str(e)}, f, ensure_ascii=False, indent=1)
            continue
        el = time.time() - t0
        # 完整性校验：容器标称时长 vs 实际解出的音频时长
        est = probe_duration(path)
        truncated = est > 0 and dur < est * 0.6
        cov = sum(d["end"] - d["start"] for d in data) / max(dur, 1) * 100
        meta = {"source": name, "language": "ja",
                "duration": round(dur, 1), "container_duration": round(est, 1),
                "truncated": truncated, "coverage": round(cov, 1), "segments": data}
        # ---- 产出参数（2026-11-06）----
        # 以前 json 里**不记模型**，于是「json 存在就跳过」无法判断旧产物是不是
        # 当前配置产生的 —— 换模型后重跑会静默复用旧文本（假绿的根因之一）。
        # 记下来之后，跳过判断就能做「参数一致才复用」，这是**合法复用**而不是漏跑。
        meta["asr_meta"] = {"model": MODEL_TAG, "model_src": MODEL_SRC,
                            "device": a.device, "compute": a.compute,
                            "vad": bool(a.vad), "chunked": bool(a.chunked),
                            "chunk_sec": a.chunk if a.chunked else None}
        if _beam_fb:
            meta["asr_meta"]["beam_fallback"] = _beam_fb
        if dropped:
            meta["dropped_halluc"] = [{"start": st, "kind": k, "text": t}
                                      for st, t, k in dropped]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        print("    完成: %d 段 / %.1f 分钟 / %.0f 秒 (%.1fx 实时) / 覆盖率 %.0f%%"
              % (len(data), dur / 60, el, dur / max(el, 0.01), cov), flush=True)
        for st, t, k in dropped:
            print("    [幻觉已剔除·%s] %.1fs %s" % (k, st, t[:40]), flush=True)
        for st, t in collapsed:
            print("    [重复循环已折叠] %.1fs %r" % (st, t[:28]), flush=True)
        if cov < 40:
            print("    [提示] 覆盖率偏低，若确认该轨有大量台词被漏，可试 --chunk 30 缩小块长",
                  flush=True)
        if truncated:
            print("    [!! 文件可能不完整] 标称 %.1f 分钟，实际只解出 %.1f 分钟，请重新下载"
                  % (est / 60, dur / 60), flush=True)
            suspects.append((name, est, dur))

    print("\n全部识别完成 -> %s   总耗时 %.1f 分钟" % (a.out, (time.time() - t_all) / 60))
    if suspects:
        print("\n=== 疑似下载不完整的文件（需重新下载）===")
        for name, est, dur in suspects:
            print("  %s   标称 %.1f 分钟 / 实际 %.1f 分钟" % (name, est / 60, dur / 60))


if __name__ == "__main__":
    main()
