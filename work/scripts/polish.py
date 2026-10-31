# -*- coding: utf-8 -*-
r"""
阶段三：参照人工译者风格，把机器初译润色成该译者的口吻，写出最终 LRC。

用法：
  python polish.py <ASR目录> <初译目录> <输出目录> [--style DIR] [文件名关键字]

例：
  python polish.py "./asr_out" "$KIKOERU_ROOT/data/draft/RJ9000005" \
                   "$KIKOERU_ROOT/kikoerusubtitle/RJ9000005"

风格资料默认读 $KIKOERU_ROOT\data\style（由 kikoeru-style-learn 生成）：
  parallel.json / style_guide.txt / hard_rules.txt / stats.json
"""
import os

import knobs
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json
import glob
import time
import re
import argparse
import random
import hashlib
import threading
import urllib.request
import difflib
import subprocess
from concurrent.futures import ThreadPoolExecutor

import cues  # 语义 cue 层（KIKOERU_CUE_PIPELINE=1 时启用，见 cues.py）
import lock_match  # E1 锁的双 key 匹配（时间戳 + 规范化日文），见 lock_match.py

# ---- R23 LRC 清屏 spacer 开关（坑 106）----
# LRC 行显示到下一行时间戳为止 → cap_holds 的「读完即走」必须在写盘层补空文本
# 清屏行才生效。⚠️ 默认**关**：全量生效 = 全库 65% 行中位提前 2.4s 清屏，
# 超出 C2 盲听验证范围（中位 1.08s），等用户听辨后再开。
# 存量极端尾（停留 > 语音+阅读+8s，1642 行）由 tmp/daily/fix_lrc_linger.py 修。
# 2026-10-23 用户盲听裁定：12 例中 11 例选「读完即走」(B)、1 例平 → **默认开**。
# 关掉用 KIKOERU_LRC_END_SPACER=0（成品已全量落盘 10180 个 spacer，
# 回退见 tmp/daily/fix_lrc_linger.py + tmp/lrc_backup_fullread_20261023/）。
SPACER_ON = os.environ.get("KIKOERU_LRC_END_SPACER", "1") == "1"

# ---- R35 说话人名前缀开关（A2 乙方案，2026-10-23 用户拍板）----
# 台本轨（RJ9000014 白/甘）把说话人名前置成「白：你好。」而不是剥掉。
# 开：`KIKOERU_SPK_PREFIX=1`。开时泄漏人名归一成前缀（不判坏格删行）；
# 关：维持 R28 行为（泄漏判坏格 → 退回初译）。
SPK_PREFIX_ON = os.environ.get("KIKOERU_SPK_PREFIX", "0") == "1"

try:
    import zhconv

    def to_zh_cn(t):
        return zhconv.convert(t, "zh-cn")
except ImportError:
    def to_zh_cn(t):
        return t

API = "http://127.0.0.1:8080/v1/chat/completions"
# ---- E1 句级锁定（2026-10-21，借鉴 ASMR-Dubber 的「锁定不被重跑覆盖」）----
# review.py 审阅页勾「锁定」的行写进 <ROOT>/data/locks/<作品名>.json
# （key = "文件|时间戳"，值 = {zh: 锁定文本, old: 锁定前原文}；旧格式纯字符串也认）。
# 重跑 polish 时按 key 覆写 → 人工修正不再被批量重跑冲掉。
# key 漂移（显示层切分变化导致 ts 移位）时回退按 old 原文认领。
# KIKOERU_NO_LOCKS=1 可关（A/B 对照用）。
LOCKS_DIR = os.environ.get("KIKOERU_LOCKS", os.path.join(os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "locks"))
LOCK_MAP = {}    # (file, ts) -> 锁定文本（主 key）
LOCK_OLD = {}    # (file, old_text) -> 锁定文本（历史兜底）
LOCK_RAW = {}    # (file, ts) -> {zh, ja}（喂给 lock_match）
LOCK_JA = {}     # (file, norm_ja) -> [ts, ...]（备 key；同句多锁时不用）
LOCK_N = [0]     # 本 run 实际覆写行数
LOCK_JA_HIT = [0]  # 其中「不是按时间戳、而是靠文本/时间锚定认回来」的行数
LOCK_AMBIG = set()  # 分数太接近、不猜的 (file, ts)
LOCK_UNMATCHED = set()  # 整轨都找不到落点的锁 (file, ts)
LOCK_CUE = {}    # {cue_idx: [rec, ...]} 当前轨的锁分配（写盘时按最近时间取用）
LOCK_STAT = {"assign": 0, "unmatched": 0, "ambiguous": 0, "tracks": 0, "detail": {},
             # 逐条落点（key → {how, cue, lock_ts, row_ts, written}）：排查「分配了却没写」
             # 这类静默丢失时，没有这张表就只能靠猜。
             "records": {}}


def assign_locks_for_track(fn, lines):
    """写盘前把该轨的锁分给 cue（v3 文本锚定，见 lock_match.assign_cues）。

    为什么在写盘**前**做：锁的 key 是**显示行**时间戳，但一条 cue 会被
    `render_display_lines` 切成 1~3 个显示行，切法依赖最终译文 —— 写盘时才拿得到行清单。
    先按 cue 分（cue 的 start/end/ja 写盘前就确定），写盘时再落到最近的显示行。
    """
    LOCK_CUE.clear()
    if not LOCK_RAW:
        return
    cues = [(i, ln["start"], ln.get("end") or ln["start"], ln.get("text", ""))
            for i, ln in enumerate(lines)]
    assign, unmatched, ambig = lock_match.assign_cues(LOCK_RAW, fn, cues)
    LOCK_CUE.update(assign)
    for k in unmatched:
        LOCK_UNMATCHED.add(k)
    for k in ambig:
        LOCK_AMBIG.add(k)
    for idx, recs in assign.items():
        for r in recs:
            LOCK_STAT["records"]["%s|%s" % r["key"]] = {
                "how": r["how"], "cue": idx, "lock_ts": r["t"],
                "row_ts": None, "sim": r["sim"], "drift": r["drift"]}
    LOCK_STAT["assign"] += sum(len(v) for v in assign.values())
    LOCK_STAT["unmatched"] += len(unmatched)
    LOCK_STAT["ambiguous"] += len(ambig)
    LOCK_STAT["tracks"] += 1
    if unmatched or ambig:
        LOCK_STAT["detail"][fn] = {
            "assigned": sum(len(v) for v in assign.values()),
            "unmatched": ["%s|%s" % k for k in unmatched],
            "ambiguous": ["%s|%s" % k for k in ambig]}


def apply_lock(fn, ts, text, ja="", cue_idx=None, row_times=None):
    """返回该行最终文本：命中锁则用锁定文本（逐字，不再过 cleanup）。

    v3（2026-10-31，P0）：**cue 级文本锚定**。锁先分给 cue（`assign_locks_for_track`），
    写盘时按「离锁时间戳最近的显示行」取用 —— 同一条 cue 切成多个显示行时，
    人工逐行锁过的几条都能各自落到对应行上。

    `cue_idx=None` 时退回单行视图（`lock_match.lookup`），仅供 `test_lock_apply.py`
    和外部调用；**生产写盘一律走 cue 级**，否则看不到整轨、没法靠时间消歧。
    """
    if cue_idx is not None:
        recs = LOCK_CUE.get(cue_idx)
        if not recs:
            return text
        t = lock_match.parse_ts(ts)
        bi, bd = -1, 1e9
        for i, r in enumerate(recs):
            d = abs(r["t"] - t) if t is not None else 1e8
            if d < bd:
                bi, bd = i, d
        if bi < 0 or bd > lock_match.WINDOW:
            return text
        # ⚠️ 2026-10-31 修：**必须让给更近的那一行**。
        # 以前是「窗口内谁先来谁拿走」（行是按时间顺序遍历的），于是锁的 ts 落在
        # cue 的第 2 个显示行时，第 1 行会以 bd=4.68 把它抢走 → 写错行 + 原行内容
        # 被覆盖成重复句（RJ1623028 00:30.38 实测：整句被搬到上一行）。
        # 现在先问一遍「同 cue 里有没有更近的行」，有就什么都不做，等那行来取。
        if row_times:
            _mine = bd
            for _ot in row_times:
                if _ot is not None and abs(recs[bi]["t"] - _ot) < _mine - 1e-6:
                    return text
        r = recs.pop(bi)          # 一条锁只落到一行，落完即消费
        _rec = LOCK_STAT["records"].get("%s|%s" % r["key"])
        if _rec is not None:
            _rec["row_ts"] = t
            _rec["written"] = (r["zh"] != text)
        if r["zh"] != text:
            LOCK_N[0] += 1
            if r["how"] != "ts":
                LOCK_JA_HIT[0] += 1
        return r["zh"]
    v, how = lock_match.lookup(LOCK_RAW, LOCK_JA, LOCK_OLD, fn, ts, text, ja)
    if how == "ambiguous":
        LOCK_AMBIG.add((fn, ts))
        return text
    if v is not None and v != text:
        LOCK_N[0] += 1
        if how != "ts":
            LOCK_JA_HIT[0] += 1
        return v
    return text


def load_locks(out_dir):
    """读 locks json → LOCK_MAP/LOCK_OLD，返回载入条数。"""
    LOCK_MAP.clear()
    LOCK_OLD.clear()
    LOCK_RAW.clear()
    LOCK_JA.clear()
    p = os.path.join(LOCKS_DIR, os.path.basename(os.path.abspath(out_dir)) + ".json")
    if not os.path.exists(p):
        return 0
    try:
        data = json.load(open(p, encoding="utf-8"))
    except Exception as e:
        print("⚠️ 句级锁定读取失败（忽略）：%s" % e)
        return 0
    n = 0
    for k, v in (data or {}).items():
        if "|" not in k:
            continue
        fn, ts = k.split("|", 1)
        if fn.lower().endswith(".lrc"):   # review.py 的 key 带 .lrc，src 不带 → 归一化
            fn = fn[:-4]
        if isinstance(v, dict):
            zh, old = (v.get("zh") or "").strip(), (v.get("old") or "").strip()
            ja = (v.get("ja") or "").strip()
        else:                      # 旧格式：纯字符串
            zh, old, ja = str(v).strip(), str(v).strip(), ""
        if not zh:
            continue
        LOCK_MAP[(fn, ts)] = zh
        LOCK_RAW[(fn, ts)] = {"zh": zh, "ja": ja}
        if old:
            LOCK_OLD.setdefault((fn, old), zh)
        n += 1
    LOCK_JA.update(lock_match.build_ja_index(LOCK_RAW))
    return n


# ---- E2 cue 级缓存签名（2026-10-21，借鉴 ASMR-Dubber 的「改一句只重做一句」）----
# 签名 = 轨名 + ja + 起止时间 + 该条初译 + 模型 + CHUNK/MASK/review/system_mode +
# system prompt 哈希。命中即跳过 LLM、直接复用旧译文——术语/风格微调后的重跑
# 从「每轨全量」降到「只跑变了的句」。缓存按作品存：data/cache/polish_sig/<作品>.json
# ⚠️ 锁行在写盘时覆写（apply_lock），与缓存正交；--cleanup-only 不过 LLM，不走缓存。
# `--force` / `KIKOERU_NO_CACHE=1` 可全量重跑（缓存仍会按新结果更新）。
CACHE_DIR = os.environ.get("KIKOERU_CACHE_DIR",
                           os.path.join(os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "cache", "polish_sig"))
_SYS_MODE = ["strong"]      # 当前 system_mode，cue_sig 用（main 里同步）


def cue_sig(src, ln, old, system, mem=""):
    """一条 cue 的缓存签名（输入全同 → 译文必同 → 可复用）。

    ⚠️ 2026-10-25 修：`mem`（轨级记忆文本）**必须进签名**。原来它不在签名里，
    于是「开关 KIKOERU_TRACK_MEMORY / 改 track_memory/*.json」后缓存照样命中，
    改动等于没生效——R117 的 A/B 全靠 `--force` 才绕开，生产上会静默复用旧译文。
    `mem` 为空时（记忆默认关）签名与旧版逐字节相同，不会让已有缓存整体失效。
    """
    h = hashlib.sha1()
    parts = [src, ln.get("text", ""), "%.2f" % (ln.get("start") or 0),
             "%.2f" % (ln.get("end") or 0), old or "", MODEL_TAG, str(CHUNK),
             str(bool(MASK)), str(bool(REVIEW)), _SYS_MODE[0],
             # 后文策略会改变 review 的结果，必须进签名（否则切模式后缓存照样命中）
             LOOKAHEAD]
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x00")
    h.update(hashlib.sha1(system.encode("utf-8")).digest())
    if mem:
        h.update(b"\x01mem")
        h.update(hashlib.sha1(mem.encode("utf-8")).digest())
    return h.hexdigest()[:20]
# 写进 lrc 的 [by:] 元信息，要如实反映实际用的翻译引擎。
# 配合 KIKOERU_ENGINE 环境变量（与 translate.py 同一套），跑 Sakura 时两条命令都要带上它。
_ENGINE_NAME = knobs.engine_tag()   # 表在 knobs.py，与 translate.py 共用
MODEL_TAG = "faster-whisper large-v3 + %s (人工风格对齐)" % _ENGINE_NAME
# 风格库位置统一由 styleloc 解析：环境变量 → data/style → presets（公开预设）
import styleloc  # noqa: E402
DEFAULT_STYLE = styleloc.style_dir()

# 批次大小。2026-10-14 从 10 降到 5（GPT 第四轮 Q1-c）：
# 批越大，7B 越容易把上一批内容搬进这批，而 `parse_reply` 只保行数不保对位，
# 于是产出「看起来合理、实则整体错位」的成品（实测 RJ9000010 08. 5:44–6:25 四句）。
# ⚠️ 别假设小批能根除——本文件同时用 ID 协议 + 块外污染检测兜住。
# 可用 KIKOERU_POLISH_BATCH 覆盖，便于在同一批数据上扫不同 N 的失败率。
CHUNK = int(os.environ.get("KIKOERU_POLISH_BATCH", "5"))
WORKERS = 2
# ---- T4（2026-10-25）：评估模式（KIKOERU_TEMP 已设）强制串行 ----
# 为什么：主润色与复核都走 ThreadPoolExecutor(WORKERS)，两个线程的 HTTP 请求
# 到达 llama-server 的先后是**竞态**。单槽（KIKOERU_SLOTS=1）下第二路请求会排队，
# 但谁先到每次不同；slot 的 KV/prompt-cache 让同一条请求的处理路径取决于它前面
# 是谁——于是「同 prompt + temp=0 + top_p=1.0」的贪心输出在近平局 token 上翻牌。
# 实测取证（RJ9000007 轨00，diag_c/diag_d 两臂）：call_0001–0004 的 prompt 与
# reply 内容全同（仅并发编号互换），call_0005 prompt 逐字节相同、reply 却不同
#（81 vs 79 tokens）——分歧只能来自 server 侧到达顺序差异，即本竞态。
# 串行 = 请求序列完全确定 → A/A 逐字节一致。只压评估模式；生产（采样）不变。
if os.environ.get("KIKOERU_TEMP"):
    WORKERS = 1
CTX_KEEP = 6        # 上下文给足：只看前 3 句容易断掉情绪和称呼的连贯
N_SAMPLE = 16
SYS_MAX = 7000    # system prompt 字符上限，超过会自动缩减例句（见 serve.py 的 -c/-np 说明）
REVIEW = True       # 润色后再过一遍「上下文连贯复核」，可用 --no-review 关掉

MIN_LEN, MAX_LEN = 14, 34
BREAK = "。！？!?…、"
COMMA_SOFT_MAX = 4  # 逗号任一侧 ≤ 这么多汉字，就判为「短促连呼」，逗号降级成空格

# ---- 跨段合并：这些参数直接决定最终字幕的行长，是本链路最影响观感的一处 ----
# 背景：关闭 VAD 后 Whisper 会把每次喘息都切成独立 segment（平均仅 13 个日文字），
# 若不跨段合并，成品字幕中位只有 5 个中文字、22% 的行不到 3 字 —— 一闪而过的碎片，
# 读起来就是「不连贯」。人工稿中位 13 字。
MERGE = True        # 允许跨 segment 累积（--no-merge 可关，退回旧行为）
MIN_LINE = 16       # 累积到这么多日文字后才允许在句读处断行
MAX_LINE = 34       # 软上限：到这附近就该在**安全断点**断行
MAX_GAP = 1.5       # 相邻词间隔超过这么多秒，认为是换句/换气，断行
MAX_LINE_SEC = 7.0  # 单行最长停留秒数的**软**上限（见下方说明）
# 硬上限：只有连安全断点都找不到、且已经长得离谱时才就地切
HARD_LINE = 52
HARD_LINE_SEC = 12.0

# 强制断行（超时长/超长度）时用来「退到最近的语义边界」，绝不切在半截词处。
# 只认两种可信边界：① 下一个词开启新 segment（Whisper 的 segment 基本是完整话语）
#                   ② 前一个词以句读收尾（。！？…、）
# **不要用「助词/接续词」当判据**——日文的词 token 是子词级的
# （親/御/さん、なん/と/なく、して/る），助词启发式必然误伤：
# 实测「して」含「て」，于是切出「…お邪魔して」+「るの…」，反而更糟。
# 找不到安全点就**不切**（宁可单行长一点），只有超过 HARD 上限才就地切。
#
# 实测教训：MAX_LINE_SEC=7 硬切时，192.22s 起累积到 199.52s 正好 7.30s，
# 于是在「御」后断行，把「親御さん」劈成「親御」+「さん」；
# 下一行同理把「なんとなく」劈成「なんと」+「なく」。
# 模型收到这种半截词乱句只能瞎猜 —— 这是「翻译诡异」最大的单一来源。


def _safe_cut_index(cur, min_keep=2):
    """在缓冲里找一个安全断点，返回下标 i：cur[:i] 归上一行、cur[i:] 归下一行。

    从**末尾往回**找（让上一行更饱满、少切几刀）。右侧至少留 1 个词。
    找不到安全点返回 None，调用方应选择「不切、继续累积」。
    """
    for i in range(len(cur) - 1, min_keep - 1, -1):
        if cur[i].get("seg") != cur[i - 1].get("seg"):
            return i
    for i in range(len(cur) - 1, min_keep - 1, -1):
        t = cur[i - 1]["w"].strip()
        if t and t[-1] in BREAK:
            return i
    return None

NUM = r"^\s*(\d+)\s*(?:[|｜]|[:：]|[.．、])\s*(.+)$"
# 模型偶尔会把 prompt 里的字段标签当成答案吐回来：prompt 用「日：…／初：…／现：…／人：…」
# 分栏，它就把「现：快进」整串当译文输出（实测 RJ9000006 第 01 轨前 8 行）。
# 这与 SPEAKER_00 泄漏是同一类问题——标签是 prompt 的结构，必须剥掉。
FIELD_LABEL = re.compile(r"^\s*(现|日|初|人|译|原文|译文|中译|中|文)\s*[:：]\s*")
# 有时模型只吐回一个模板词、连冒号都没有（实测吐过「最终中文」——
# 那是 prompt 里「每行『编号|最终中文』」的说明文字）。整格等于这些词的一律判为泄漏。
LABEL_ONLY = {"最终中文", "中文", "译文", "原文", "最终", "日文", "初译", "结果"}
JPN = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
# 假名**连续**片段：≥4 字才算「整句没译」。
# ⚠️ 2026-10-15 修正：原来 `bad_cell` 只要有**任何一个**假名就判坏格，
#    于是初译里留了个人名（`みお` 2 字）就把**整行丢掉**——
#    RJ9000010 `10.` 11:19.26 的 `みおのよだれの味がする おいしい` 在成品里完全消失。
#    「丢内容」比「译得不好」严重得多（用户永远看不到那句）。
KANA_RUN = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u30fc]{4,}")


def kana_count(s):
    return sum(1 for c in (s or "") if "\u3040" <= c <= "\u30ff" or c == "\u30fc")
# 拒译检测：只认**明确的拒绝短语**。
# ⚠️ 裸的「抱歉」不能放进来——对话译文里「抱歉」太常见，会把正常译文整行误杀。
REFUSE = ("无法翻译", "不能翻译", "作为一个AI", "作为AI", "无法提供", "无法完成",
          "无法处理", "无法协助", "不能协助", "我不能帮你")
DUP_TAIL = re.compile(r"(呢|吧|啦|嘛|呀)\s*(呢|吧|啦|嘛|呀)\s*$")
JP_TAIL_PARTICLE = re.compile(
    r"(ね|な|なぁ|なあ|の|よ|わ|ぞ|かな|かしら|でしょ|でしょう|かね|もの|ぞい)"
    r"[、。．！？!?…ー〜~♪]*$")


# ---------- 行切分 ----------
def flatten_words(segments):
    """把各 segment 的词拉平成一条带说话人标记的时间流。

    Whisper 的 segment 边界不适合直接当字幕行边界：关闭 VAD 后每次喘息都会被切成
    独立 segment，照它断行会得到大量 1-3 字的碎片。拉平后按语义/停顿重新分行。
    """
    out = []
    for si, seg in enumerate(segments):
        spk = seg.get("speaker", "")
        ws = seg.get("words")
        if ws:
            out.extend({"s": float(w["s"]), "e": float(w["e"]),
                        "w": w["w"], "spk": spk, "seg": si} for w in ws)
        elif (seg.get("text") or "").strip():
            out.append({"s": float(seg["start"]), "e": float(seg["end"]),
                        "w": seg["text"].strip(), "spk": spk, "seg": si})
    out.sort(key=lambda x: x["s"])
    return out


def build_lines(segments):
    """切行时顺便记下这句话是谁说的（ASR 做过 diarize 才有）。

    默认跨 segment 累积：说话人换了、停太久、到句读且够长、或超过长度/时长上限才断。
    """
    if not MERGE:
        return _build_lines_by_segment(segments)

    words = flatten_words(segments)
    lines, cur = [], []

    def flush():
        if not cur:
            return
        # 段边界＝话语边界，合并时插一个空格做提示。
        # 不插的话模型看到的是一坨没有边界的乱块：实测「あはは、いけないんだ」
        # 「家のこと知らないで」「お母さんがやってんじゃない?」三句被粘成一行，
        # 结果「〜んじゃない?（是我妈弄的吧）」被误译成「妈妈应该做」。
        buf = []
        for k, x in enumerate(cur):
            if k and x.get("seg") != cur[k - 1].get("seg"):
                buf.append(" ")
            buf.append(x["w"])
        t = "".join(buf).strip()
        if t:
            lines.append({"start": cur[0]["s"], "end": cur[-1]["e"],
                          "text": t, "spk": cur[-1]["spk"]})
        cur.clear()

    for w in words:
        if cur:
            gap = w["s"] - cur[-1]["e"]
            if w["spk"] != cur[-1]["spk"] or gap > MAX_GAP:
                flush()
        cur.append(w)
        tail = w["w"].strip()
        n = sum(len(x["w"].strip()) for x in cur)
        dur = cur[-1]["e"] - cur[0]["s"]
        if tail and tail[-1] in BREAK and n >= MIN_LINE:
            flush()
        elif n >= MAX_LINE or dur >= MAX_LINE_SEC:
            # 到软上限了，但**不能就地切**——先退到最近的安全断点。
            i = _safe_cut_index(cur)
            if i is not None:
                carry = cur[i:]
                del cur[i:]
                flush()
                cur.extend(carry)
            elif dur >= HARD_LINE_SEC or n >= HARD_LINE:
                flush()   # 极端长句真的无处可退，才就地切
    flush()
    return lines


def _build_lines_by_segment(segments):
    """旧行为：每个 segment 内独立分行，不跨 segment 合并。"""
    lines, cur = [], []

    def flush(spk=""):
        if cur:
            t = "".join(w["w"] for w in cur).strip()
            if t:
                lines.append({"start": cur[0]["s"], "end": cur[-1]["e"],
                              "text": t, "spk": spk})
            cur.clear()

    for seg in segments:
        spk = seg.get("speaker", "")
        ws = seg.get("words") or []
        if not ws:
            if seg["text"].strip():
                lines.append({"start": seg["start"], "end": seg["end"],
                              "text": seg["text"].strip(), "spk": spk})
            continue
        for w in ws:
            cur.append(w)
            n = sum(len(x["w"].strip()) for x in cur)
            tail = w["w"].strip()
            if (tail and tail[-1] in BREAK and n >= MIN_LEN) or n >= MAX_LEN:
                flush(spk)
        flush(spk)
    return lines


def read_lrc(path):
    out = []
    for l in open(path, encoding="utf-8"):
        m = re.match(r"\[(\d+):([\d.]+)\](.*)$", l.strip())
        if m and not l.startswith("[ti:") and not l.startswith("[by:"):
            out.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
    return out


def match_lines(lines, old):
    """按时间把机器初译对应到每一行。"""
    res = [""] * len(lines)
    old_sorted = sorted(old, key=lambda x: x[0])
    for i, ln in enumerate(lines):
        best, bd = "", 9e9
        for os_, ot in old_sorted:
            d = abs(os_ - ln["start"])
            if d < bd:
                bd, best = d, ot
            if os_ > ln["start"] + 3:
                break
        res[i] = best if bd < 2.5 else ""
    return res


# ---------- 风格资料 ----------
def load_style(style_dir):
    guide, hard, pairs, stats = "", "", [], {}
    g = os.path.join(style_dir, "style_guide.txt")
    if os.path.exists(g):
        txt = open(g, encoding="utf-8").read()
        txt = txt.split("## 客观统计")[0]
        txt = "\n".join(l for l in txt.splitlines() if not l.startswith("#"))
        guide = txt.strip()
    h = os.path.join(style_dir, "hard_rules.txt")
    if os.path.exists(h):
        hard = open(h, encoding="utf-8").read().strip()
    s = os.path.join(style_dir, "stats.json")
    if os.path.exists(s):
        stats = json.load(open(s, encoding="utf-8"))
    p = os.path.join(style_dir, "parallel.json")
    if os.path.exists(p):
        data = json.load(open(p, encoding="utf-8"))
        pool = [x for x in data.get("pairs", [])
                if 3 <= len(x["zh"]) <= 40 and 3 <= len(x["ja"]) <= 60]
        random.seed(3)
        pairs = random.sample(pool, min(N_SAMPLE, len(pool)))
        # 样例统一转简体，避免模型照抄繁体
        pairs = [dict(p, zh=to_zh_cn(p["zh"])) for p in pairs]
        if not stats:
            stats = data.get("stats", {})
    gloss = {}
    gp = os.path.join(style_dir, "glossary.json")
    if os.path.exists(gp):
        try:
            gloss = json.load(open(gp, encoding="utf-8"))
        except Exception:
            gloss = {}
    if not hard and stats:
        hard = "- 句末用句号「。」收束 - 省略号统一「……」 - 语气词不要滥用"
    return guide, hard, pairs, stats, gloss


def build_system(guide, hard, pairs, n_pairs, gloss=None):
    ex = "\n".join("%d|%s\n   译：%s" % (i + 1, p["ja"], p["zh"]) for i, p in enumerate(pairs))
    gline = ""
    if gloss:
        # R45（2026-10-23）：**按 hit 降序取前 40 条**。原来是 `list(gloss.items())[:40]`，
        # 靠 dict 插入顺序——新加的术语（如 おちんぽ/チンポ）永远排在末尾，**根本进不了
        # prompt**。70 条里 30 条被静默丢弃，且丢的是最新的那批。
        _items = sorted(gloss.items(),
                        key=lambda kv: -(kv[1].get("hit", 0) if isinstance(kv[1], dict) else 0))
        gline = ("\n\n【术语对照表】（该译者的固定译法，遇到必须这样译，不可换成别的说法）\n"
                 + "、".join("%s→%s" % (k, v["zh"] if isinstance(v, dict) else v)
                             for k, v in _items[:40]))
    return """你是日语字幕译者，现在要接手另一位译者的作品，必须严格模仿他的风格。

【硬性风格约束（来自 %d 组真实译文统计，必须遵守）】
%s
%s

【该译者的风格指南】
%s

【该译者的真实译文样例】（日文 -> 他的中文）
%s

任务：把给出的日文台词，润色成完全符合该译者口吻的简体中文。

★★ 先看这一条——它最影响「看不看得懂」：**日文的主语/宾语几乎全省略，中文必须补出来**。
   两人对话默认：说话人＝「我」，听话人＝「你」；
   **独白/内心话一律从主角的第一人称视角补，主语就是「我」**（音声作品几乎全是主角视角）。
   ·「家のこと知らないで」 ✗「不了解家里的情况」（谁不了解？读者一头雾水）
     ✓「你又不了解我们家的情况」
   ·「お母さんがやってんじゃない?」 ✗「不是我妈弄的？」 ✓「这本来是我妈弄的啦」
   ·「感謝しろよ」 ✗「感谢吧」 ✓「你得谢谢我哦」
   ·「いけないんだ」 ✗「不该这样」 ✓「（你）不能这样啦」
   · 以「〜ないで」「〜してる」「〜なんだ」这类连用形/终助词收尾的句子，
     被省略的主语默认是「你」。
   **每句写完自问「谁对谁做了什么」，答不上来就是没补主语，重写。**
   宁可补得啰嗦，也绝不能让读者猜——缺主语正是"看不懂什么意思"的头号原因。
   ▸ 以下八类句子**几乎一定要补人称**（日语靠语法形式替人称代了言）：
     ① 敬语「お待ちしましょう」→「**我**来」；「お変わりはございませんか」→「**您**一向可好」
     ② 寒暄语「お待たせしました」→「让**您**久等了」；「おいとましなければ」→「**我**该告辞了」
     ③ 授受动词（あげる/くれる/もらう/いただく）→「送给**您**」「朋友给**我**的书」
     ④ 思考感觉动词（思う/考える/感じる/気がする）→ 补「**我**」
     ⑤ 感情形容词（うれしい/悲しい/こわい/さびしい/ほしい/いたい）→ 补「**我**」
     ⑥ 希望助动词「たい」→ 补「我」；推量「う・よう」表意志/劝诱 → 补「我／我们」；
        命令句 → 补「你／您」
     ⑦ **对话中自称名字、或长辈直呼对方名字 → 一律改成人称代词**
        （「夏子一人じゃないけど」✓「不光**我**自己」；「夏子のほかに？」✓「除**你**之外？」）
        ——不改的话中文读者会以为是第三者在场
     ⑧ 职务尊称「課長さんは…」→「科长**您**…」
   ⚠️ 但**补主语 ≠ 改人称**——这是两个相反的错误。原文明确说了是谁就是谁，
      别把第三人称改成「我」：
      ·「バレー部の顧問の先生なんだ」 ✓「是排球部的顾问老师」 ✗「我是排球部的顾问老师」
        （「〜なんだ」是在向对方说明**第三方**的事，保持第三人称）
      · 孩子对母亲说的「ママ」 ✓「妈妈」 ✗「我妈」（「我妈」是向别人介绍自己母亲时才用）

规则：
1. 每行输出严格为「编号|译文」，编号与输入一致，不要空行、不要解释、不要保留日文。
2. **输出必须是简体中文**：样例与初译若出现繁体字，一律转写成简体后再输出。
3. 以机器初译为参考，但以日文原文为准；初译有误要改正。
3b. **日文原文本身会有 ASR 同音错字**（语音识别的固有噪声）：
    遇到明显不合剧情逻辑的词，先想它的同音/近音正确写法再译。
    实测案例：「男子近世」实为「男子禁制」（男生禁入）、「おはん/おもはん」
    实为「うん/ほな」这类口语、拟声词被听成无关词。
    判断标准：译文放进对话里讲不通，就回头怀疑原文错字。
4. 模仿样例的用词、语气词、标点与断句习惯（尤其是省略号与句末标点）。
5. 一句对一句，行数必须和输入完全一致，绝不合并、拆分、省略。
6. 单行译文控制在 22 字以内。

【连贯性与自然度——这是最重要的质量要求】
7. 必须参照「前文」：人称（我/你/他）、称呼、时态、情绪强度要和前文衔接，
   同一角色同一场戏里的口吻从头到尾保持一致，不要忽冷忽热。
8. **禁止机械直译**：不要按日文语序逐字搬。要按中文口语习惯重新组织，
   像真人说话那样自然流畅。日文助词（は/が/を/に/の）不需要译出。
9. 喘息、呻吟、口吃、拖音（あ、ん、え、その…）按中文口语习惯自然表达，
   不要写成「那个、那个」这种生硬重复，也不要整句删掉。
   ⚠️ **纯气声碎片**（整句只有单假名 + 省略号，如「お…あ…あ…足…」「お…お…」）**没有词义**，
   逐字译成语气词即可（あ→啊、お→哦／唔、う→呜、ん→嗯、ひ→咿），
   **绝不要因为前文出现过某个词就往那个词上靠**——实测前文有「妈妈」，
   模型就把「お…あ…あ…足…」译成了「妈…妈…妈…脚…」（应只是「哦…啊…啊…脚…」）。
10. 语气词从「呢/吧/哦/啊/啦」里挑，且只用一个，不要叠加。
    **「呢」严禁连用**：连续两句都用「呢」就是机器味。同一个「ね」人工译者
    会换着译（呢/啊/吧/喔/省略），只有真正的疑问、感叹软化才用「呢」，
    平铺直叙的叮嘱、陈述用「啊/吧/喔」或不加。
11. 每句都要像同一个人在连续说话，前后句之间逻辑要接得上。
12. **疑问句必须译成疑问句**：识别出的日文基本不带「？」，
    不能靠原文的问号判断。要看句末助词（か/かな/かしら/の/ん/っけ/よね/でしょ/でしょう/
    ですか/ますか）和上下文是否在发问、反问、确认。是疑问句句末就用「？」，
    千万别译成陈述句。
13. **标点与分隔（这条最容易出翻译腔，务必照做）**：句内停顿**优先用半角空格**。
    实测这位译者 87%% 的行根本不用逗号，20.8%% 的行用空格、只有 12.8%% 用逗号。
    · 短促的连呼、重复、感叹、呼唤之间，一律用空格：
      「主换人了呢 真可爱 你在装什么？」「加油 加油 加油」「来吧 一个人射吧」
      ——绝不能写成「主换人了呢，真可爱，你在装什么？」
    · 只有真正的从句结构（有明确的主谓/因果，如「所以」「但是」「如果」引导）才用逗号：
      「今天没戴眼镜，所以亲起来很方便」「那家伙真是任性呢，要是失控了可不好」
    · **一行最多一个逗号**；出现两个以上逗号几乎一定是把短促连呼误用了逗号。
    语气延长用三点「…」；句末用句号「。」收束（2026-10-08 用户规范）。
14. **拖音、拟声、符号是这位译者的重要特征，别丢**：
    · 拉长音撒娇用「~」收尾（好舒服喔~、来嘛~）——人工约 7%% 的句尾带 ~
    · 心动、高潮处句尾可加「♡」（在里面抖起来了♡）——零星用，别泛滥
    · 拟声词用中文真实说法：シコシコ→撸撸／套弄、ずりずり→蹭蹭、
      ぐちゅぐちゅ→咕啾咕啾、くりくり→转着揉揉，绝不音译成「叽叽」「库库」
    · ⚠️ **独立成句的短片假名尤其危险**——没有上下文时最容易按音节拼成汉字：
      キュー／チュー → 「啾」（亲吻声），绝不译「基欧」；ぴゅっ／びゅっ → 「噗／喷」。
      凡「基欧」「卡雅玛雅」「塔科斯」这种按假名逐音节拼出来的汉字，一律是错的。
      **唯一例外是角色名**（リリア→莉亚、マヤ→玛雅），那是固定音译，要全篇一致。
    · 叠词复沓是口语的魂（蹭蹭 摸摸 撸撸 好乖好乖），人工 37%% 的句子带叠词，
      机器只有 16%%——有动作重复就大胆叠。
    · **拟声/拟态词先分类再选词**，类别错了词就错：音/声（自然或物的声音）·
      情（心理状态）· 容（人的样子）· 态（事物状态）。同一个「がらがら」，
      在「車輪ががらがら」是**声**，在「人ががらがら」是**态**；
      「いそいそと」是**情**（兴冲冲），不是**容**（动作快慢）。
    · **定感情色彩（褒/贬）**：同是笑声，からから＝爽朗、にやにや＝偷偷笑、
      けたけた＝没修养的傻笑、げらげら＝无所顾忌地大笑——别一律译成「哈哈哈」。
    · **注意程度（轻/重/强/弱）**：「あかあかと」是「闪闪发亮」不是「明亮」；
      「キョトンと」是「一时间惊呆了」不是「惊奇」；单音/连续音/复音要区别对待。
15. **机械直译的标准改法**（照这个思路改，不是照抄字面）：
    ·「うちは男ばかりなので」✗「我家是男性居多，因为…」✓「家里全是男的嘛」
    ·「いつもお世話になって」✗「一直承蒙照顾」✓「平时多亏你们照顾啦」
    ·「偉いね」✗「做得不错呢」✓「好乖好乖~」
    ·「気持ちよかった」✗「感觉很好」✓「好舒服」
    · 日文语序搬过来的定语从句（「接受了…的我的…」）一律拆开重说。
16. **口语小词别按字面硬译**（这几处最容易翻出"莫名其妙"的中文）：
    ·「いや」＝「不是啦／没有啦」，**不是「不行」**；「だって」＝「可是／因为」。
      连起来「いや、だって」就是「不是啦，可是…」，不是在否定什么。
    · 句末「〜んじゃない?」「〜じゃない?」是**推测／反问**，译「…吧？」「不是…吗？」，
      **不是「应该做」**：「お母さんがやってんじゃない?」✓「这不是我妈弄的嘛？」
      ✗「妈妈应该做」。
    ·「〜んだ／〜の／〜んだけど」句末是说明缘由、委婉断定，译「…啊」「…的」，
      别套成「应该」。日文没有「应该」这个义务助动词（那是「べき／なければ」）。
    ·「お邪魔する」＝「打扰了／来叨扰」，不是「闯入」；「勝手に」＝「擅自」。
      原文里逗号很少时，请按**话语**切分意思，别把两三个短句揉成一个长句。
17. **译文里出现上下文完全不合逻辑的具体名词，先怀疑是 ASR 听错了**：
    房间对话里冒出「大象」几乎一定是把「そう（那样）」听成了「象」；
    「男子近世」其实是「男子禁制」。回头看原文的近音词再译，别照着错字硬翻。
18. **翻译腔五病——看到就改**（这是"读起来像机器翻的"最具体的来源）：
    ·「关于／对于」综合症：✗「关于那件事，我感到很抱歉」✓「那件事，真的很抱歉」
    ·「被」字滥用：✗「他被大家所讨厌着」✓「大家都讨厌他」
    · **「进行／做出／给予」等虚假动词**：✗「作出了拒绝」「进行说明」✓「拒绝了」「解释清楚」
    · 无意义的复数「们」：✗「学生们都看向了我」✓「全班都看向了我」
    · **过长的定语「的」链**：✗「带着看到什么难以置信的东西的表情的她…」
      ✓「她神情错愕，像是看到了什么不可思议的东西」
    另：动作要干脆、少抽象名词——✗「他对我进行了视线的投射」✓「他看向我」。
    修完自问一遍：「这句话我能对着画面配出音来吗？」
19. **节制语气词**：不要满篇「呢、啦、哦、呀、哟」——语气优先靠标点（！？……）
    和副词（到底、简直、果然）体现。人工译者的「呢」只占 5.7%%，你很容易用超。
    也不要把「ええと」「あの」机械译成「那个」「怎么说呢」，按语境化为
    「呃……」「我说……」或干脆留白。""" % (n_pairs, hard, gline, guide, ex)


def build_conservative(gloss=None):
    """B1 保守润色模式的 system prompt（2026-10-16）。

    与强润色（build_system）的唯一区别就是这份 prompt——其余流程（分块/解析/
    审校/cleanup/门禁）逐行相同，保证 B1 对比干净。

    设计原则：**能不改就不改**。强润色的规则 7-19（连贯性/机械直译改法/口语小词/
    翻译腔五病/语气词节制）全是「改发动机」，全部删掉；只留「只修确定错误」
    的五类白名单 + 格式硬规则。术语表照给（术语对齐是保守版的核心价值）。
    """
    gline = ""
    if gloss:
        gline = ("\n\n【术语对照表】（该译者的固定译法，遇到必须这样译，不可换成别的说法）\n"
                 + "、".join("%s→%s" % (k, v["zh"]) for k, v in list(gloss.items())[:40]))
    return """你是日语字幕的**最小改动校对员**。前面的译者（HY-MT 机器初译）已经完成了
日→中翻译，质量整体合格。你的任务**不是重译，而是只修确定的问题**。

【只允许修改以下五类】
1. **固定术语不一致**：术语对照表里的日文词，必须按表里的中文译。没进表的词，
   哪怕你更熟悉别的说法，也不许换。
2. **明显错译**：译文放进对话里完全讲不通（ASR 同音错字导致；把第三人称当成
   「我」；疑问句译成陈述句——看句末助词 か/かな/の/ん/っけ/でしょ 判断）。
3. **漏主语导致看不懂**：日文主语省略、初译没补「我/你」会让读者猜时才补。
   初译已经补了的，一个字都不要动。
4. **日文残留**：假名没译干净（人名等固定音译除外）。
5. **标点规范**（只调标点，不动文字）：
   · 句内停顿用**半角空格**，不用逗号（「加油 加油 加油」✓ 「加油，加油，加油」✗）
   · 句末用句号「。」收束；疑问用「？」；拖音用三点「…」
   · 一行最多一个逗号，两个以上一定是误用%s

【绝对不做——违反任何一条都是严重错误】
- 不换语气词：呢/吧/啊/啦/喔 一个字都不许动
- 不加叠词、不加「~」「♡」、不补任何标点装饰
- 不重写句式：哪怕你觉得有更优雅的说法，**原样保留**
- 不删「冗余修饰」：啰嗦是风格，不是错误
- 不改语序、不并句、不拆句
- 输出简体中文，不许出现日文

【输出格式】
严格逐行输出「编号|译文」，编号与输入一致，行数与输入完全一致。
**没有问题的句子必须原样照抄**——照抄是正常且预期的结果，不是偷懒。""" % gline


# ---------- 后处理 ----------
def fix_particle(zh, ja):
    """原文句末没有语气助词 -> 去掉中文句末硬加的语气词。"""
    if not zh:
        return zh
    if JP_TAIL_PARTICLE.search((ja or "").strip()):
        return zh
    t = re.sub(r"(呢|吧|哦|啦|啊)(?=[！？!?…]*$)", "", zh)
    return t.strip() if t.strip() else zh


def _zh_len(s):
    return len(re.sub(r"[^\u4e00-\u9fff]", "", s))


def soften_commas(t, thr=COMMA_SOFT_MAX):
    """把「短促连呼」型的行内逗号降级成空格，向人工风格靠拢。

    人工语料实测：87% 的行**完全没有逗号**，只有 11% 有一个、2% 有两个；
    句内停顿主要靠**半角空格**（20.8% 的行含空格）。而机器产出恰恰相反——
    逗号 40%~56%、空格仅 2%，典型的书面语翻译腔：
        「啊，啊呀，这，这里，快快被弄的话」
        「看起来好开心呢，啊，快快快，快点，快点」
    规则：逗号两侧只要有一侧是短片段（≤ thr 个汉字），说明这里是口语里
    的短促停顿/连呼，改用空格；两侧都是长片段才是真正的从句，保留逗号。
        「今天没戴眼镜，所以亲起来很方便」→ 两侧都长，保留 ✓
        「主换人了呢，真可爱，你在装什么？」→ 前面短，转空格 ✓
    """
    parts = t.split("，")
    if len(parts) < 2:
        return t
    out = parts[0]
    for i in range(1, len(parts)):
        short = _zh_len(parts[i - 1]) <= thr or _zh_len(parts[i]) <= thr
        out += (" " if short else "，") + parts[i]
    return out


_PUNCT_ONLY = re.compile(
    r"^[\s…‥・.．,，、。！？!?~～\-—–♪♡♥☆★（）()\[\]「」『』\"'“”‘’]+$")
_JA_LEX = re.compile(r"[\u3040-\u30ff\u4e00-\u9fffA-Za-z0-9]")
# 纯气声/拟声短串（R33，与 scan_violations._SOUND_KANA 同口径）：
# 整句只由气声字组成（含 ばー 呕吐声）→ 译「…」是忠实翻译，不算丢句。
_SOUND_KANA = set("ぁあぅうぇえおっはひもんばー")


def _is_pure_sound(ja):
    core = re.sub(r"[^ぁ-んァ-ヶー]", "", ja or "")
    return bool(core) and len(core) <= 4 and all(c in _SOUND_KANA for c in core)


def bad_cell(zh, ja):
    """判断一个单元格是「结构泄漏/日文残留」而不是真正的译文。

    模型有时不吐译文而吐 prompt 的结构：
      · `SPEAKER_00` —— 说话人标签被当成答案；
      · 纯数字 —— 上游解析漏剥「编号|」前缀（Qwen3 会输出「1|1|译文」，
        只剥一层就残留一个「1|」，整行于是退化成行号）；
    另外日文残留也不能要。都判为「坏格」，交给调用方退回初译。
    纯数字只在**原文本身不是数字**时才算坏，免得误伤正常的数字翻译。
    """
    if not zh:
        return True
    if zh.strip() in LABEL_ONLY:
        return True
    # ---- 纯标点丢句（2026-10-19 实测事故）----
    # 模型偶尔整句只吐「…」/「？」，把原句内容整个丢掉（全库成品 43 行，其中
    # 33 行初译是好的）。判据必须**同时**满足两侧：
    #   · 中文侧没有任何实义字符（汉字/假名/字母/数字）；
    #   · 日文侧**确有**实义字符。
    # ⚠️ 日文侧不加这条守卫就会误伤：ja 本身就是 `…`/`あ…` 这类停顿或气声时，
    #    纯标点是**忠实翻译**（还兼作长静默处的清屏行），判坏会把合法行也回退。
    # ⚠️ R33（2026-10-23）：补「纯气声/拟声短串」豁免（docstring 一直承诺但没实现）——
    #    `ばー/あぁん/おっ` 类纯声音段译「…」是忠实翻译，判坏会白走一趟回退还挂
    #    POLISH_UNVERIFIED。与 scan_violations.punct_only_loss 同口径。
    if _PUNCT_ONLY.match(zh.strip()) and _JA_LEX.search(ja or "") \
            and not _is_pure_sound(ja):
        return True
    # 日文残留：**连续 ≥4 假名**，或假名占比 ≥40%（整句没译）。
    # ⚠️ 不用「出现任何假名就判坏」——那会把「味蕾里全是みお的口水」这种
    #    只是**人名没译**的句子整行丢掉（2026-10-15 实测事故）。
    if KANA_RUN.search(zh):
        return True
    k = kana_count(zh)
    if k and k / max(1, len(zh)) >= 0.4:
        return True
    if re.fullmatch(r"[A-Z_]*SPEAKER[_\s]*\d*", zh.strip()):
        return True
    if re.fullmatch(r"\d{1,3}", zh.strip()) and not re.fullmatch(r"[\d\s.,．，]+", (ja or "").strip()):
        return True
    # A8 占位符残留：模型有时把**别句**的 `<M1>` 搬进本句（或该句本无 span 却冒出占位符），
    # restore_zh 只校验「本句应有的 span」，抓不到这种跨句漂移 → 这里兜底：
    # 任何残留的 `<M数字>` 都判为坏格，交给调用方退回初译（2026-10-16 实测 RJ9000014 Tr.10）。
    if re.search(r"<M\d+>", zh):
        return True
    # ⚠️ M2 协议（2026-10-18）：`〔…〕` 形态的占位符同样会跨句漂移，且 `check_literal`
    # 只校验本句 span、管不到别的句子。整条管线里 `〔〕` **只由遮蔽器产生**（10.6 万
    # 人工句对里出现 0 次），所以正文出现任意 `〔`/`〕` 都是残留 → 判坏格。
    # 漏了这条守卫，切到 `KIKOERU_MASK_LITERAL=1` 后 `〔哈啊…〕` 会直接漏进成品。
    if "〔" in zh or "〕" in zh:
        return True
    # ---- 元话语泄漏（2026-10-18 实测事故）----
    # P2 归属提示行（`※ c001｜中｜提示｜含解释/确认语气（んだ）…`）被模型当成**待译内容**，
    # 改写成中文正文输出：`“んだ”这个词用于表达某种语气，用来说明是谁处于某种状况中。`
    # 跑 RJ9000013 端到端时一次漏了 15 处。已把 hint 行格式从 `[cNNN]` 改成 `※ cNNN`
    # 并在块尾加终止标记，这里再加兜底检测。
    # ⚠️ **不能只查「这个词」**——正常台词也有（`对喜欢这个词有反应`，
    #    实测成品库 4 处全是误报）。必须查「解释日语语法」的模式：
    _META_DISCOURSE = re.compile(
        r"用于表达|用来表达|用于解释|用来说明|这个表达其实|注意：这句|"
        r"说话人是谁|是对听者说的|说给听者听的|是一种[^。]{0,8}句式|"
        r"在日语里|日语中说的")
    if _META_DISCOURSE.search(zh):
        return True
    return False


_HALF2FULL = {"?": "？", "!": "！", ",": "，", ";": "；", ":": "：",
              "(": "（", ")": "）"}


def normalize_halfwidth(t):
    """半角标点 → 全角（2026-10-26，R46 规范的确定性版本）。

    为什么要：ASR 原文常带半角（`次、また耳少し引っ張るよ?`），译文照抄后
    `check.py` 会判「半角标点」**硬门禁失败**（今天两次都栽在这上面，只能手工修）。
    只在**含中文**的行上动，纯英文/数字/时间保持原样；`.` 只在不夹在字母数字之间时转。
    """
    if not t or not re.search(r"[\u4e00-\u9fff]", t):
        return t
    out = []
    n = len(t)
    for i, ch in enumerate(t):
        if ch in _HALF2FULL:
            out.append(_HALF2FULL[ch])
            continue
        if ch == ".":
            prev = t[i - 1] if i else ""
            nxt = t[i + 1] if i + 1 < n else ""
            # 数字/字母之间（3.5、A.B）不动；省略号已被前面的规则收敛成「…」。
            # ⚠️ 必须用 ASCII 判断：`'啊'.isalnum()` 也是 True，会把中文后的句号漏掉。
            if not (re.match(r"[0-9A-Za-z]", prev) or re.match(r"[0-9A-Za-z]", nxt)) \
                    and prev != "." and nxt != ".":
                out.append("。")
                continue
        out.append(ch)
    return "".join(out)


# ---- 译文里的日文残留归一（2026-10-28 R154，用户拍板）----
# 两类，来源相同：初译把源语原样留在中文里，check.py 判「日文残留」→ 硬门禁失败。
#   ① 名字里的「ヶ」：`茜ヶ崎椿` → `茜崎椿`。`ヶ` 是日本人名的正规写法，但本项目
#      规范是**零假名**；夹在两个汉字之间时不可能是中文，删掉不影响语义。
#      ⚠️ 只删「漢+ヶ+漢」，不碰 `ヶ月`/`ヶ所` 这类量词（那出现在日文侧，不是这里）。
#   ② 术语的假名形：`茜ヶ崎つばき` → `茜ヶ崎椿`。**只替换 ja 键里含假名**的术语——
#      `先生` 这种纯汉字键绝不能碰，中文里「先生」是合法词。
_NAME_KANA = re.compile(r"(?<=[\u4e00-\u9fff])ヶ(?=[\u4e00-\u9fff])")
_KANA_TERMS = [None]


def kana_terms():
    """[(ja, zh)]：ja 键**含假名**的术语，按 ja 长度降序（长词先换，避免被短词截断）。"""
    if _KANA_TERMS[0] is not None:
        return _KANA_TERMS[0]
    out = []
    try:
        g = json.load(open(os.path.join(DEFAULT_STYLE, "glossary.json"), encoding="utf-8"))
        for ja, v in g.items():
            zh = (v or {}).get("zh") if isinstance(v, dict) else None
            if zh and re.search(r"[\u3040-\u30ff]", ja):
                out.append((ja, zh))
    except Exception:
        pass
    out.sort(key=lambda x: -len(x[0]))
    _KANA_TERMS[0] = out
    return out


def dejapanize(t):
    """把译文里残留的日文写法确定性改掉（规则见上）。纯字符串替换，可复现。"""
    if not t or not re.search(r"[\u3040-\u30ff]", t):
        return t
    for ja, zh in kana_terms():
        if ja in t:
            t = t.replace(ja, zh)
    t = _NAME_KANA.sub("", t)
    return t


def cleanup(t, stats):
    if not t:
        return t
    # 先剥 prompt 字段标签（「现：」「日：」「人：」…），这是模型把 prompt 结构当答案吐回来的
    t = FIELD_LABEL.sub("", t.strip())
    # 清掉行内的日文长音符「ー」(U+30FC)。
    # 模型（实测 Sakura）偶尔把拖音写成「诶ー?」，而「ー」落在假名码位区间，
    # 会被 JPN 检测当成日文残留，**导致整行被丢弃**（一次全量跑丢了 3 行）。
    # 中文里不该出现这个字符，直接删掉比丢整行好。
    t = t.replace("\u30fc", "")
    # 译文里残留的日文写法（2026-10-28 R154，用户拍板「ヶ 删掉」）。
    # 和上面的「ー」同一类问题：初译把源语原样留在中文里 → check.py 判日文残留 →
    # **硬门禁失败**（RJ01654115 复跑时 9 行全是这一个根因）。必须在 cleanup 里做，
    # 因为这是「确定性归一」，不该再赌模型下一次会不会自己改对。
    t = dejapanize(t)
    # 省略号归一：英文点串「...」/ 半角「…」/ 数量不对的「………」一律收敛，
    # 形态跟随该译者的真实习惯（三点还是六连点，见 style/stats.json 的 ellipsis_style）
    mk = (stats or {}).get("ellipsis_style", "……")
    t = re.sub(r"\.{2,}", "…", t)
    t = re.sub(r"…+", mk, t)
    # 句末句号：**保留**（用户 2026-10-08 拍板「句末句号 + 句内空格」）。
    # 之前这里是「句末不加句号」的老规范，实测 Sakura 带句号的输出更自然，
    # 句号→逗号→空格的连环转换反而是「机械感」元凶。只清理异常形态：
    t = re.sub(r"[。．]{2,}", "。", t)          # 叠句号收成一个
    t = re.sub(r"[。．]+(?=[！？!?])", "", t)   # 句号后紧跟叹号/问号时，句号多余
    # 顿号「、」：中文口语里少见，转逗号（下面统一再转空格）
    if stats.get("pct_dunhao", 0) <= 10:
        t = t.replace("、", "，")
    t = re.sub(r"，\s*(?=……)", "", t)
    t = re.sub(r"，{2,}", "，", t)
    # 句内逗号过密是「翻译腔」指纹，统一降级短促停顿为空格（句号保留，不受影响）
    t = soften_commas(t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    m = DUP_TAIL.search(t)
    if m:
        t = t[:m.start()] + m.group(2)
    # 重复字符压缩：模型把呻吟/喘息（ASR 的「あ、あ、だめだめ」）放大成无限循环，
    # 实测出过「哦哦哦…」×1728、「啊啊啊…」×1693、「三三三…」×360 的灾难行。
    # 同一字符连续重复 ≥6 次时压成 3 个，既保留"重复/呻吟"语义又不撑爆一行。
    # 不处理「好好」「乖乖」这类有意义的叠词（长度 <6），也不处理标点。
    def _dedup_rep(mo):
        ch = mo.group(1)
        return ch * 3
    # 连续重复（「哦哦哦…」）
    t = re.sub(r"([^\s，。！？…~♡♪、，])\1{5,}", _dedup_rep, t)
    # 带空格分隔的同一字符重复（「三 三 三 三…」「啊 啊 啊…」）
    def _dedup_spaced(mo):
        ch = mo.group(1)
        return (ch + " ") * 3
    t = re.sub(r"([^\s，。！？…~♡♪、，])(?:\s+\1){5,}", _dedup_spaced, t)
    t = re.sub(r"[，,]+$", "", t)
    # 日文的拖音「ー」常被模型写成半角连字符（实测「真是的-」「那我先去学习了-」），
    # 中文里没有这种写法，连同破折号/长音符一并去掉（人工用「~」表示拖音，另由 prompt 管）。
    t = re.sub(r"[\-\u2010-\u2015\u30fc]+$", "", t).strip()
    t = normalize_halfwidth(t)   # 半角标点 → 全角（ASR 原文半角会照抄进来）
    t = to_zh_cn(t)          # 兜底：繁体 -> 简体
    return t.strip()


# ---------- LLM ----------
_THINK = re.compile(r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", re.I)
_THINK_OPEN = re.compile(r"<think(?:ing)?>[\s\S]*$", re.I)


def strip_think(s):
    """剥掉推理模型的  thinking…<｜end▁of▁thinking｜> 块。

    Qwen3 默认开 thinking；正常已通过请求里的 enable_thinking:false 关掉
    （需要 llama-server 带 --jinja）。这里是兜底：万一换模型或换 llama.cpp
    版本后它漏出来，译文就会被大段思考内容污染，宁可直接砍掉。
    第二个正则是为了处理被 max_tokens 截断、只有开标签没有闭标签的情况。
    """
    if not s:
        return s
    return _THINK_OPEN.sub("", _THINK.sub("", s)).strip()


def _rep_temp(d):
    """修复路径的温度（2026-10-25）：**评估模式下修复路径也必须贪心**。

    原先 `_retry_one_cue`/`_unmasked_rewrite`/`_targeted_rewrite` 把温度硬编码成
    0.5/0.45/0.4，于是 `KIKOERU_TEMP=0` 只压住了主路径——实测约 12% 的 cue 会走
    修复路径，它们仍在采样，导致「同配置跑两遍」有 17% 的行不同，
    **和处理效应同量级，A/B 全部作废**（GPT 第五轮明确要求 A/A 必须逐字节一致）。
    """
    v = os.environ.get("KIKOERU_TEMP")
    if v is None:
        return d
    try:
        return float(v)
    except ValueError:
        return d


def call(messages, max_tokens=1400, temperature=0.35):
    # KIKOERU_TEMP（2026-10-25）：A/B 专用。默认不变；设 0 走贪心解码，
    # 否则同配置两次跑只有 **33% 的行相同**（实测），任何 prompt 改动的效果都被噪声淹没。
    _t = os.environ.get("KIKOERU_TEMP")
    if _t:
        try:
            temperature = float(_t)
        except ValueError:
            pass
    # greedy 时 top_p 必须给 1.0：llama.cpp 在 temperature=0 下多数实现会忽略 top_p，
    # 但不保证；留 0.9 有可能仍按 top_p 截断，导致「以为贪心了其实没有」。
    _top_p = 1.0 if temperature <= 0.0 else 0.9
    payload = {"model": "qwen", "messages": messages, "temperature": temperature,
               "max_tokens": max_tokens, "top_p": _top_p, "repetition_penalty": 1.05,
               "chat_template_kwargs": {"enable_thinking": False}}
    _dump_n = 0     # KIKOERU_DUMP_PROMPT 时的本次调用编号（0 = 不落盘）
    if os.environ.get("KIKOERU_DUMP_PROMPT"):
        # 记**每一次**模型调用（含修复路径与复核路径）——只记主路径的话，
        # 「主路径 prompt 相同、输出却不同」永远定位不到（实测卡了一晚上）。
        # T4（2026-10-25）补三件事：
        #   ① 编号自增加锁：WORKERS=2 两个线程并发进 call()，`_CALL_N[0] += 1`
        #      的读-改-写会撞车——两个调用拿到同一编号，一个 call_NNNN.txt 被
        #      写两遍、另一个编号凭空消失，日志本身就错位、不可 cmp。
        #   ② 头部补 top_p：A/A 排查要核对的不止 temp/max_tokens，采样参数
        #      也得留痕（贪心下它必须是 1.0）。
        #   ③ 同时落盘 call_NNNN.reply.txt（finish_reason + 原文；失败尝试追加
        #      FAILED 行）——只记 prompt 时，「哪一次调用第一次返回不同文本」
        #      只能靠下游间接猜，有了 reply 就能直接 cmp 定位。
        try:
            with _DMP_LOCK:
                _CALL_N[0] += 1
                _dump_n = _CALL_N[0]
            _d = os.path.join(_DUMP_DIR[0] or ".", "call_%04d.txt" % _dump_n)
            os.makedirs(os.path.dirname(_d), exist_ok=True)
            open(_d, "w", encoding="utf-8").write(
                "temp=%s top_p=%s max_tokens=%s\n==== SYSTEM ====\n%s\n==== USER ====\n%s"
                % (temperature, _top_p, max_tokens,
                   messages[0]["content"] if messages else "",
                   messages[-1]["content"] if messages else ""))
        except Exception:
            pass

    def _log_reply(txt):
        """reply/失败原因落盘（KIKOERU_DUMP_PROMPT 时；与 call_NNNN.txt 同号）。"""
        if not _dump_n:
            return
        try:
            open(os.path.join(_DUMP_DIR[0] or ".", "call_%04d.reply.txt" % _dump_n),
                 "a", encoding="utf-8").write(txt)
        except Exception:
            pass
    req = urllib.request.Request(API, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    last = ""
    for k in range(3):
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                body = json.loads(r.read().decode("utf-8"))
                _ch = body["choices"][0]
                # T4：reply 原文 + 结束原因落盘。finish_reason=length 说明被
                # max_tokens 截断（A/A 排查时这是「prompt 相同输出不同」的另一种
                # 解释来源），completion_tokens 顺带留痕。
                _log_reply("finish_reason=%s completion_tokens=%s attempt=%d\n"
                           "==== REPLY ====\n%s\n"
                           % (_ch.get("finish_reason") or "",
                              (body.get("usage") or {}).get("completion_tokens", ""),
                              k, _ch["message"]["content"]))
                return strip_think(_ch["message"]["content"])
        except urllib.error.HTTPError as e:
            # 4xx 的响应体里有真原因（多为 prompt 超过 llama-server 的 -c 上下文），
            # 不读出来就只能看到裸的 400，无法定位。
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:400]
            except Exception:
                pass
            last = "HTTP %s %s\n%s" % (e.code, e.reason, body)
            _log_reply("attempt=%d FAILED: %s\n" % (k, last))
            if 400 <= e.code < 500:
                raise RuntimeError("推理服务拒绝请求：%s" % last)
            if k == 2:
                raise RuntimeError(last)
            time.sleep(2 * (k + 1))
        except Exception:
            # T4：重试/异常也要留痕——「同一次请求被重发」若是 A/A 分歧来源，
            # reply.txt 里的 FAILED 行直接可见（attempt=0/1/2 各一次）。
            _log_reply("attempt=%d FAILED: %s\n" % (k, sys.exc_info()[1]))
            if k == 2:
                raise
            time.sleep(2 * (k + 1))
    return ""


# ---------- 块内身份协议（2026-10-14，GPT 第四轮 Q1-a）----------
# 旧方案按「第几行」绑定模型返回，`parse_reply` 只保证行数一致、**不保证每条对位**：
# 实测 RJ9000010 08. 5:44–6:25 连续 4 句整体错位，成品成了本轨别处句子的近似复制，
# 而**初译是对的**——肉眼完全看不出问题。
# 现在每条 cue 发一个**稳定且非连续**的 ID，按 ID 建映射，集合对不上就整块重试。
_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # 去掉易混的 I O 0 1
ID_RE = re.compile(r"\[(Q[A-Z2-9]{5})\]")
# 块外污染检测（Q1-b）阈值
POLL_MIN_RUN = 8        # 去标点后最长公共连续片段下限（汉字数）
POLL_MIN_COV = 0.70     # 且占较短一句的比例
POLL_MIN_ABS = 10       # 或绝对长度 ≥ 此值（长句只共享一个从句时，覆盖率会低到 63%）
POLL_JA_SIM = 0.60      # 两条日文相似度 ≥ 此值 → 视为原文本来就重复，不算污染
POLL_SHORT = 8          # 任一句短于此长度 → 只记录、不阻断（误报率太高）
PUNCT_ZH = "。、，,.!！?？…♪~〜ー♡ \u3000「」『』（）()"


def cue_id(ja, start, end, src=""):
    """由 (轨, 起点, 终点, 日文) 派生稳定、非连续的 6 位 ID。"""
    import hashlib
    h = hashlib.md5(("%s|%.2f|%.2f|%s" % (src, start, end, ja))
                    .encode("utf-8")).digest()
    n = int.from_bytes(h[:6], "big")
    out = []
    for _ in range(5):
        out.append(_ALPHA[n % 32])
        n //= 32
    return "Q" + "".join(out)


def ja_anchor(ja):
    """日文首尾锚点：程序提供，用于确认模型没把 ID 与输入行脱钩。"""
    t = "".join(c for c in (ja or "") if c not in PUNCT_ZH and not c.isspace())
    if len(t) <= 8:
        return t
    return t[:3] + "…" + t[-3:]


def parse_reply_id(reply, ids, anchors=None):
    """按 ID 解析。返回 (dict id→中文, None) 或 (None, 失败原因)。

    硬失败（GPT：任一不满足就整块重试）：
      · 输出 ID 集合 != 输入 ID 集合（缺、多、未知、重复）
      · 某 ID 对应空文本
      · 有文本无法唯一归属到任何输入 ID（残留）
      · 输出带了锚点但锚点与程序给的不一致
    """
    ids = list(ids)
    want = set(ids)
    seen = {}
    residue = 0
    marks = [(m.start(), m.end(), m.group(1)) for m in ID_RE.finditer(reply or "")]
    for k, (s, e, cid) in enumerate(marks):
        nxt = marks[k + 1][0] if k + 1 < len(marks) else len(reply)
        body = reply[e:nxt]
        # 形如 [Q7F2K][お兄…ぽん]|中文 —— 锚点可选
        body = body.strip()
        m_anc = re.match(r"^\s*\[[^\]]*\]\s*", body)
        if m_anc:
            if anchors and cid in anchors:
                got = m_anc.group(0).strip("[] \t")
                if got and anchors[cid] and got != anchors[cid]:
                    return None, "锚点不符 %s（期望 %s 实得 %s）" % (cid, anchors[cid], got)
            body = body[m_anc.end():]
        body = body.strip()
        if body.startswith("|") or body.startswith("|"):
            body = body[1:].strip()
        body = body.split("\n")[0].strip()          # 只要同一行
        body = FIELD_LABEL.sub("", body).strip()
        if "|" in body or "｜" in body:
            body = re.split(r"[|｜]", body)[-1].strip()
        if not body:
            return None, "ID %s 对应空文本" % cid
        if cid in seen:
            return None, "ID %s 重复出现" % cid
        seen[cid] = body
    got = set(seen)
    if got != want:
        miss = sorted(want - got)
        extra = sorted(got - want)
        return None, "ID 集合不符：缺 %s 多 %s" % (miss[:4], extra[:4])
    # 残留：**这一行有中文、却又不是以 [Q...] 开头** → 无法归属到任何 ID。
    # （模型偶尔会先输出一句"以下是译文："这类引导语，那也算没按格式来，整块重试更安全。）
    residue = 0
    for line in (reply or "").splitlines():
        t = line.strip().strip("`").strip()
        if not t or t.startswith("["):
            continue
        if re.match(r"^[|｜\s\-—…。、,.:：]+$", t):
            continue
        if re.search(r"[\u4e00-\u9fff]", t):
            residue += 1
    if residue:
        return None, "有 %d 行文本无法归属到任何 ID" % residue
    # ⚠️ 判据要和 bad_cell 一致：**连续 ≥4 假名 或 假名占比 ≥40%** 才算「整句没译」。
    # 用 `JPN.search`（任何假名即失败）会把「味蕾里全是みお的口水」这种
    # **只是人名没译**的整块打回，实测把 ID 失败率从 ~1 推到 14/26（2026-10-15）。
    for v in seen.values():
        if KANA_RUN.search(v):
            return None, "输出里有连续假名（疑似未译）"
        k = kana_count(v)
        if k and k / max(1, len(v)) >= 0.4:
            return None, "输出里假名占比过高（疑似未译）"
    return seen, None


def _lcs_run(a, b):
    """最长公共**连续**子串长度（O(n·m)，字幕长度足够快）。"""
    if not a or not b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def norm_zh(t):
    return "".join(c for c in (t or "") if c not in PUNCT_ZH)


def is_pollution(zh_new, zh_other, ja_new, ja_other):
    """判断 zh_new 是否是 zh_other 的错位复制（GPT 第四轮 Q1-b）。

    高危需同时满足：最长公共连续片段 ≥8 汉字；占较短一句 ≥70%；
    两条**日文**相似度低（原文本来就重复的不算）；
    且两边都不是拟声/叠词保护 span。任一句短于 8 字 → 不算（只记录）。
    """
    from scan_violations import is_rep, ZH_ONO
    a, b = norm_zh(zh_new), norm_zh(zh_other)
    if len(a) < POLL_SHORT or len(b) < POLL_SHORT:
        return False
    if is_rep(zh_new) or is_rep(zh_other):
        return False
    if all(c in ZH_ONO for c in a) or all(c in ZH_ONO for c in b):
        return False
    run = _lcs_run(a, b)
    if run < POLL_MIN_RUN:
        return False
    if not (run >= POLL_MIN_COV * min(len(a), len(b)) or run >= POLL_MIN_ABS):
        return False
    if difflib.SequenceMatcher(None, norm_zh(ja_new), norm_zh(ja_other)).ratio() >= POLL_JA_SIM:
        return False
    return True


def parse_reply(reply, n):
    out = {}
    for line in reply.splitlines():
        line = line.strip().strip("`").strip()
        if not line:
            continue
        m = re.match(NUM, line)
        if m:
            idx = int(m.group(1))
            if 1 <= idx <= n and idx not in out:
                val = m.group(2).strip()
                if "|" in val or "｜" in val:
                    val = re.split(r"[|｜]", val)[-1].strip()
                val = FIELD_LABEL.sub("", val)
                if JPN.search(val):
                    val = None
                if val:
                    out[idx] = val
    if len(out) == n:
        return [out[i] for i in range(1, n + 1)]
    rows = []
    for line in reply.splitlines():
        line = line.strip().strip("`").strip()
        if not line:
            continue
        line = re.sub(NUM, r"\2", line)
        if "|" in line or "｜" in line:
            line = re.split(r"[|｜]", line)[-1].strip()
        line = FIELD_LABEL.sub("", line)
        if line and not JPN.search(line):
            rows.append(line)
    return rows if len(rows) == n else None


# ---------- 策略表（A6，GPT 第七轮 Q2-B）----------
# GPT：「随机抽 4 组例句塞进 prompt」应**降级**——随机例句既不稳定也没有证据链；
# 换成「**当前 cue 命中了哪些策略表条目**」。
_POLICY = None


def load_policy():
    """加载术语表（strict 档）+ 小写假名变体 + 浊音近音变体。

    ⚠️ 2026-10-18：优先读 `glossary_v3.json`（清洗版，278 条 / strict 98）。
       v2 是**字面 n-gram 共现**产物，1491 条里 1243 条是跨界碎片
       （`マッ`/`ルフ`/`持`/`孫→小穴`），注入等于喂噪声。清洗判据见
       `refine_glossary.py`（分词边界 + 序列频次 + 包含率）。
       v3 不存在时自动回退 v2，不影响旧环境。
    """
    global _POLICY
    if _POLICY is not None:
        return _POLICY
    sty = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "style"))
    terms, small, voice = {}, set(), set()
    src = None
    for name in ("glossary_v3.json", "glossary_v2.json"):
        fp = os.path.join(sty, name)
        if not os.path.exists(fp):
            continue
        try:
            for r in json.load(open(fp, encoding="utf-8")):
                if r.get("tier") == "strict":
                    terms[r["ja"]] = (r["zh"], round(r.get("cond", 1.0), 2))
            src = name
            break
        except Exception:
            continue
    try:
        v = json.load(open(os.path.join(sty, "kana_variants.json"), encoding="utf-8"))
        small = {r["variant"] for r in v.get("small", [])}
        voice = {r["variant"] for r in v.get("voice", [])}
    except Exception:
        pass
    _POLICY = {"terms": terms, "small": small, "voice": voice, "src": src}
    return _POLICY


def policy_hits(ja_list):
    """这批 cue 的日文命中了哪些策略表条目。**只列本批出现的**（prompt 省 token）。"""
    pol = load_policy()
    blob = "\n".join(ja_list)
    out = {"terms": [], "small": [], "voice": []}
    for j, (zh, cond) in sorted(pol["terms"].items(), key=lambda x: -len(x[0])):
        if j in blob and len(out["terms"]) < 12:
            out["terms"].append((j, "%s（条件概率 %.2f）" % (zh, cond)))
    for v in sorted(pol["small"]):
        if v in blob and len(out["small"]) < 6:
            out["small"].append(v)
    for v in sorted(pol["voice"]):
        if v in blob and len(out["voice"]) < 4:
            out["voice"].append(v)
    return out


# ---------------------------------------------------------------------------
# 作品级实体账本（GPT 第八轮 Q1/Q2：源语驱动的称谓事实层，2026-10-16）
# ---------------------------------------------------------------------------
# 「先生 → 老师」是**词级规则**；「这 27 次先生都指同一位家庭教师」是**实体级事实**。
# 分块 7B 不会可靠地自行维护后者。账本从「作品名 + Tag + 轨名」推出称谓族
# （用户 2026-10-16 给的免费线索），每个 block 只注入本块命中的锁。
_LEDGER = None
_LEDGER_TRIED = False

# A8 非实义 span 遮蔽器模块（main 里按开关加载；None = 关闭）
MASK = None
# A8 计数器：[单句丢占位符回退初译] 的次数（日志汇总用）
# ⚠️ 跨轨累计、不清零——GPT 第九轮 Q2 实测复盘正是靠累计口径反推回退数。
# 逐轨真相看 MASK_AUDIT（每次回退/成功都落盘），别只看这个累计值。
stats_mask_fail = [0]
# A8 per-cue 遮蔽审计（GPT 第九轮 Q2「先修可观测性」+ 第十轮 Q5「补运行级/重试级」）：
# 每条（GPT 第十轮 Q5 给的字段表）：
#   run_id / track / cue_id / ja / expected_spans / model_output_raw / observed_spans /
#   wrapper_form / status / fail_kind / retry_count / final_method / reason
# status ∈ success / fallback
# fail_kind ∈ missing / duplicate / reordered / raw_japanese / other
# wrapper_form ∈ bracket / paren / none / other / missing（M2 协议下才有意义）
# final_method ∈ literal_restore / literal_strip / retry / unmasked_rewrite / draft_fallback
# ⚠️ 计数器改为**每轨 reset**（GPT 第十轮 Q5 明确要求），全局报表由 json 聚合生成。
TARGETED_LOG = []          # D5 定向重译的逐条记录（旧→新 + 触发原因）
DET_STATS = [0]            # D9-3 确定性直出的 cue 总数（全跑累计）
MASK_AUDIT = []
_CUR_TRACK = [""]
_RUN_ID = [""]          # 每次 polish 启动生成一次，写进每条记录
_PREV_SPK_RAW = [""]    # C1：本 block 第一条的上一句原始声纹标签（换人提示用）
# 单条重试计数（cue_id -> 已重试次数）；D10 回退阶梯用
_CUE_RETRY = {}
# D10 回退阶梯开关：① 单条重试 ② 无遮蔽定向润色（都可以单独关掉做 A/B）
MASK_RETRY = os.environ.get("KIKOERU_MASK_RETRY", "1") != "0"
MASK_UNMASK_RETRY = os.environ.get("KIKOERU_MASK_UNMASK_RETRY", "1") != "0"


# ---------------- 归属结构层（D3b/D5）与 A8 审计的模块级配置 ----------------
# ⚠️ 2026-10-18：这一节在一次批量替换中**被整段误删**（PRAG_ON / PRAG_STATS /
#    prag_block_hints / load_pragmatics / _aggregate_audit 五个符号全丢），
#    主流程一跑到 load_pragmatics() 就 NameError。已重建，教训：
#    **按行号范围替换前必须先确认起止标记唯一**（当时 `% (ja, ...))` 在
#    _retry_one_cue 里也出现过一次，导致 i1 落到了错误的行）。
PRAG_ON = os.environ.get("KIKOERU_PRAGMATICS", "1") != "0"
prag_block_hints = None
PRAG_STATS = {"blocks": 0, "cues": 0, "high": 0, "medium": 0, "dropped": 0}


def load_pragmatics():
    """加载归属 hint 模块（失败不影响主流程）。"""
    global prag_block_hints, _PH
    if not PRAG_ON:
        print("归属提示：KIKOERU_PRAGMATICS=0，已禁用")
        return
    try:
        import pragmatics_hints as _ph
        _PH = _ph                      # D5 定向重译要复用 check_pair
        prag_block_hints = _ph.format_block_hints
        _ph._selftest()
    except Exception as e:
        prag_block_hints = None
        print("⚠️ 归属提示不可用（跳过）：%s" % e)


def _aggregate_audit(records):
    """把 per-cue 审计记录聚合成报表（GPT 第十轮 Q5：**不让全局计数器当事实来源**）。

    返回 total / success / fallback / fallback_rate / fail_kind / wrapper /
    final_method / retry_total / by_track。每轨 reset 的计数器只用于实时打印，
    落盘的 track_summary / global_summary 都从这份聚合里出。
    """
    d = {"total": 0, "success": 0, "fallback": 0, "fallback_rate": 0.0,
         "fail_kind": {}, "wrapper": {}, "final_method": {}, "retry_total": 0,
         "by_track": {}}
    for c in records:
        st = c.get("status") or ""
        d["total"] += 1
        d["success" if st == "success" else "fallback"] += 1
        for key, field in (("fail_kind", "fail_kind"), ("wrapper", "wrapper_form"),
                           ("final_method", "final_method")):
            v = c.get(field) or ""
            if v:
                d[key][v] = d[key].get(v, 0) + 1
        d["retry_total"] += int(c.get("retry_count") or 0)
        trk = c.get("track") or ""
        t = d["by_track"].setdefault(trk, {"total": 0, "success": 0,
                                           "fallback": 0, "fallback_rate": 0.0})
        t["total"] += 1
        t["success" if st == "success" else "fallback"] += 1
    if d["total"]:
        d["fallback_rate"] = round(d["fallback"] / float(d["total"]), 4)
    for t in d["by_track"].values():
        if t["total"]:
            t["fallback_rate"] = round(t["fallback"] / float(t["total"]), 4)
    return d



def _looks_japanese(t):
    """重试结果里「像没译」的判据。

    ⚠️ 不能直接调 `bad_cell`：它会把 `〔〕` 残留判坏，而 M2 下重试结果本来就要
       再走一遍 `check_literal` 剥包装，那时还没剥 → 会把正确的重试结果误杀。
       这里只看日文残留（连续 ≥4 假名，或假名占比 ≥40%）。
    """
    if not t:
        return True
    if KANA_RUN.search(t):
        return True
    return kana_count(t) / max(1, len(t)) >= 0.4


def _retry_one_cue(system, ja_disp, old_zh, bad_zh, spans, lit):
    """D10 第 1 级：只重做这一句，并在提示里**点名**丢掉了哪段。

    为什么单独发而不是整块重试：整块重试会让本来正确的句子重新采样（引入新波动），
    而且贵。单句重试只影响失败那一句。
    返回 (zh, why)；失败返回 (None, why)。
    """
    if lit:
        vals = "、".join("`%s`" % ph for ph, _o in spans)
        note = ("上一次输出里，这些**已经是最终中文**的片段丢了或被改写了：%s。\n"
                "它们必须原样出现在译文的语义对应位置，不要翻译、不要改写、不要删。" % vals)
    else:
        note = ("上一次输出里，这些占位符丢了或序号错了：%s。\n"
                "必须逐字照抄，一个都不能少、不能改序号。" % " ".join(ph for ph, _o in spans))
    user = ("%s\n\n【只重做这一句】\n日文原文：%s\n机器初译：%s\n上一次的错误输出：%s\n\n"
            "【输出】严格 1 行，格式 `[句ID]|中文译文`，句ID 用 `c0`。"
            % (note, ja_disp, old_zh, bad_zh))
    try:
        reply = call([{"role": "system", "content": system},
                      {"role": "user", "content": user}], temperature=_rep_temp(0.5))
    except Exception:
        return None, "call_error"
    if not reply:
        return None, "empty"
    if any(k in reply for k in REFUSE):
        return None, "refused"
    got = None
    for ln in reply.splitlines():
        m = re.match(r"\s*\[?\s*c0\s*\]?\s*[|｜]\s*(.+)", ln)
        if m:
            got = m.group(1).strip()
            break
    if not got:
        # 模型没按格式回 → 退一步：整段回复当译文（只要不是空/不是日文）
        cand = reply.strip().splitlines()[0].strip() if reply.strip() else ""
        got = cand if cand and not _looks_japanese(cand) else None
    return (got, "ok") if got else (None, "no_parse")


_PH = None                     # pragmatics_hints 模块引用（D5 定向重译复用 check_pair）
# D5 定向重译（2026-10-18）：块润色完成后，对**后检测 high 告警**的 cue 单独重译一次。
# 为什么不在同一轮做：模型刚写完这一块，正在"顺着自己的译法"往下走；
# 单独重发一句 + 明确指出归属冲突，它才愿意改（同 `_retry_one_cue` 的经验）。
TARGETED_RETRANSLATE = os.environ.get("KIKOERU_TARGETED_RETRY", "1") != "0"
TARGETED_MAX = int(os.environ.get("KIKOERU_TARGETED_MAX", "6") or 6)

def _unmasked_rewrite(system, ja, old_zh, bad_zh):
    """D10 第 2 级：**去掉占位符**，把原日文给它，明示"这里该有拟声"。

    适用于模型怎么都守不住占位符的情况——此时占位符本身是障碍，
    与其强行保留不如让它在上下文里自己处理拟声。
    """
    user = ("【只重做这一句】\n"
            "日文原文：%s\n机器初译：%s\n上一次的错误输出：%s\n\n"
            "这句里有拟声/气声/喘息，中文要译出对应的拟声词（如「哈啊」「噗嗤」），"
            "**不要音译假名、不要跳过**。\n\n"
            "【输出】严格 1 行，格式 `[句ID]|中文译文`，句ID 用 `c0`。"
            % (ja, old_zh, bad_zh))
    try:
        reply = call([{"role": "system", "content": system},
                      {"role": "user", "content": user}], temperature=_rep_temp(0.45))
    except Exception:
        return None
    if not reply or any(k in reply for k in REFUSE):
        return None
    for ln in reply.splitlines():
        m = re.match(r"\s*\[?\s*c0\s*\]?\s*[|｜]\s*(.+)", ln)
        if m:
            t = m.group(1).strip()
            return t if t and not _looks_japanese(t) else None
    return None


def _targeted_rewrite(system, ja, old_zh, bad_zh, why):
    """D5：后检测说这句归属/极性有问题 → 只重做这一句，把冲突点讲明白。

    `why` 是 check_pair 的 detail（如「ja 含 君，中文无听者指向」）。
    返回新译文或 None（解析失败/吐日文/拒答）。
    """
    user = ("【只重做这一句】\n"
            "日文原文：%s\n机器初译：%s\n当前译文：%s\n\n"
            "【后检测发现的问题】%s\n"
            "请重新判断这句**讲的是谁的经历/状态**（说话人自己 / 听者），"
            "再按这个判断重译。日文句尾助词本身不决定人称，要看内容。\n\n"
            "【输出】严格 1 行，格式 `[句ID]|中文译文`，句ID 用 `c0`。"
            % (ja, old_zh, bad_zh, why))
    try:
        reply = call([{"role": "system", "content": system},
                      {"role": "user", "content": user}], temperature=_rep_temp(0.4))
    except Exception:
        return None
    if not reply or any(k in reply for k in REFUSE):
        return None
    for ln in reply.splitlines():
        m = re.match(r"\s*\[?\s*c0\s*\]?\s*[|｜]\s*(.+)", ln)
        if m:
            t = m.group(1).strip()
            return t if t and not _looks_japanese(t) else None
    return None


def _mask_fail_kind(probs):
    """把 restore_zh / check_literal 的 problems 归类。

    GPT 第十轮 Q5 的枚举：missing / duplicate / reordered / raw_japanese / other
    """
    s = " ".join(probs)
    if "残留原始日文" in s:
        return "raw_japanese"
    if "漏掉" in s or "消失" in s or ("数量" in s and "!=" in s):
        return "missing"
    if "重复" in s:
        return "duplicate"
    if "未知" in s or "不连续" in s or "残留占位符" in s:
        return "reordered"
    return "other"


def load_ledger(draft_dir=None):
    """加载当前作品的实体账本（一次）。失败返回 None，不影响主流程。"""
    global _LEDGER, _LEDGER_TRIED
    if _LEDGER_TRIED:
        return _LEDGER
    _LEDGER_TRIED = True
    if not draft_dir:
        return None
    try:
        import entity_ledger as el
        # draft_dir 形如 <作品>/draft_hymt 或 <作品>/draft
        parent = os.path.dirname(os.path.abspath(draft_dir))
        rj = os.path.basename(parent)
        # 作品根：draft_dir 的父目录再往上找 meta.md（kikoerusubtitle/<rj>/meta.md）
        root = os.path.dirname(parent)
        meta = el.find_meta_for(rj, [root, os.path.join(os.path.dirname(root), "kikoerusubtitle")])
        asr = os.path.join(parent, "asr")
        if rj:
            _LEDGER = el.build_ledger(rj, meta, asr)
            n_e = len(_LEDGER.get("entities") or {})
            if n_e:
                ents = "、".join("%s→%s（%s）" % (k, v["zh_family"][0], v["confidence"])
                                 for k, v in _LEDGER["entities"].items())
                print("实体账本：%s（%s）" % (rj, ents))
    except Exception as e:
        print("  ⚠️ 实体账本不可用（跳过）：%s" % e)
        _LEDGER = None
    return _LEDGER


def ledger_hits(ja_list):
    """本块日文命中的实体锁，渲染成 prompt 片段（无账本/无命中返回 ""）。"""
    led = load_ledger()
    if not led or not led.get("entities"):
        return ""
    try:
        import entity_ledger as el
        return el.format_block_hits(el.block_hits(led, ja_list))
    except Exception:
        return ""



# ---- 轨级记忆注入（2026-10-25，R113 落地）----
# 为什么：R114 实测 80.9% 的 ASR/翻译错在**同轨内没有任何第二处佐证**，
# 说明瓶颈是「不知道这场戏在讲什么」，不是上下文长度。track_memory 把整轨统计
# （场景主题链 / 本轨固定译法 / 已知错字 / 禁改写法）压成 300–500 字，逐轨注入。
# 默认关：KIKOERU_TRACK_MEMORY=1 才开（要 A/B 过再决定是否默认）。
_MEM_TXT = [""]        # 签名用：global + 全部 items（任一变化 → 缓存失效）
_MEM_GLOBAL = [""]     # 每块都注入的「全局短卡」（场景主线）
_MEM_ITEMS = [[]]      # 按块过滤注入的条目：[{"ja":..., "note":...}]
_DUMP_DIR = [""]
_CALL_N = [0]
# T4（2026-10-25）：call_NNNN 编号自增/写盘的互斥锁。WORKERS=2 时两个线程并发
# 进 call()，`_CALL_N[0] += 1` 的读-改-写不是原子的——不加锁两个调用会拿到同一
# 编号，prompt 日志直接错位，A/A 的「call 数量与内容一致」永远对不上。
_DMP_LOCK = threading.Lock()


_GLOSS_MEM = [None]


def _gloss_for_mem():
    """术语表（style/glossary.json）只读缓存，供轨级记忆做冲突判定。"""
    if _GLOSS_MEM[0] is None:
        try:
            _GLOSS_MEM[0] = json.load(open(os.path.join(DEFAULT_STYLE, "glossary.json"),
                                          encoding="utf-8"))
        except Exception:
            _GLOSS_MEM[0] = {}
    return _GLOSS_MEM[0]


def load_mem(asr_dir, src):
    """读 `draft/<rj>/track_memory/<track>.json` 并压成一段 prompt。"""
    import glob as _g
    if os.environ.get("KIKOERU_TRACK_MEMORY", "0") != "1":
        return dict(sig="", global_txt="", items=[])
    base = os.path.dirname(os.path.normpath(asr_dir))
    d = os.path.join(base, "track_memory")
    if not os.path.isdir(d):
        return dict(sig="", global_txt="", items=[])
    stem = src
    for ext in (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus"):
        if stem.lower().endswith(ext):
            stem = stem[:-len(ext)]
            break
    p = os.path.join(d, stem + ".json")
    if not os.path.exists(p):
        c = _g.glob(os.path.join(d, stem + "*.json"))
        if not c:
            return ""
        p = c[0]
    try:
        m = json.load(open(p, encoding="utf-8"))
    except Exception:
        return ""
    # ---- 全局短卡（每块都注入，极短）----
    # 2026-10-25 Step1：原来的「整轨广播」改成「全局短卡 + 按块检索」。
    # GPT 第七轮 Q3 的结论：不要纯按块检索（会丢全局感），保留一条极短全局卡，其余按块给。
    g = ["\n\n【本轨背景】（整轨统计；不要复述、不要写进译文）"]
    th = m.get("theme_chain") or []
    if th:
        g.append("场景主线：" + "；".join("%s %s" % (c["win"], "/".join(c["top"]))
                                         for c in th[:4]))
    global_txt = "\n".join(g) + "\n"

    # ---- 按块条目（只在该块日文真的出现时才注入）----
    items = []
    tm = [t for t in (m.get("terms") or []) if t.get("zh")][:10]
    if tm:
        # 与术语表冲突 → 丢记忆侧；与术语表重合 → 跳过（system 里已有，SKILL 坑 137）
        _gl = _gloss_for_mem()
        keep, drop, dup = [], [], 0
        for t in tm:
            gg = _gl.get(t["ja"]) or {}
            if gg.get("zh") and gg["zh"] != t["zh"]:
                drop.append("%s(记忆=%s/术语表=%s)" % (t["ja"], t["zh"], gg["zh"]))
            elif gg.get("zh"):
                dup += 1
            else:
                keep.append(t)
        if drop:
            print("  [轨级记忆] 丢弃 %d 条与术语表冲突的译法：%s"
                  % (len(drop), "、".join(drop)), flush=True)
        if dup:
            print("  [轨级记忆] 跳过 %d 条与术语表重复的译法（system 里已有）" % dup, flush=True)
        for t in keep:
            items.append(dict(ja=t["ja"], note="本轨固定译法 %s→%s" % (t["ja"], t["zh"])))
    # 本轨特有词：corpus==0 = 全语料没出现过（中位 7 个/轨，SKILL 坑 137）
    for e in (m.get("entities") or [])[:12]:
        if (e.get("corpus") or 0) == 0 and e.get("w"):
            items.append(dict(ja=e["w"], note="本轨特有词 %s（全库没出现过，全轨用同一种译法）" % e["w"]))
    items = items[:12]
    # 签名 = 全局卡 + 全部条目（保守：任一条目变化都让缓存失效）
    sig = global_txt + "\n".join(x["note"] for x in items)
    return dict(sig=sig, global_txt=global_txt, items=items)


def polish_block(system, block, prev_ctx, mask=None):
    """block: [(id, ja, 初译, anchor)]；按**句 ID** 解析返回，不按行号。

    为什么不用「第几行」绑定：实测 7B 会把上一批的内容搬进这批，
    行数看起来一样、内容却整体错位，肉眼查不出来（详见 parse_reply_id 注释）。

    A8 遮蔽：`mask` 非 None 时启用——把 ja 里的非实义 span 换成 `<M1>` 占位符，
    模型原样保留占位符，返回后 `restore_zh` 填回中文拟声。校验失败 → 返回 None
    触发上层重试（宁可回退初译，也不放行被字面翻译的拟声）。
    """
    # ---- A8：对每句 ja 做遮蔽（mask 为 None 时全跳过，等价旧行为）----
    ja_disp = {}       # id -> 送模型的日文（可能是遮蔽后的）
    spans_of = {}      # id -> [(占位符, 原文span)]
    if mask is not None:
        _lit = bool(getattr(mask, "LITERAL", False))
        for i, ja, _old, _anc, *_x in block:
            mj, sp = mask.mask_ja(ja, literal=_lit)
            ja_disp[i] = mj
            spans_of[i] = sp

    body = "\n\n".join("[%s]（%s）\n日：%s\n初：%s"
                       % (i, anc, ja_disp.get(i, ja) if mask is not None else ja,
                          old if old else "（无）")
                       for i, ja, old, anc, *_x in block)
    # ---- GPT 第十轮 Q6：上下文预算优先级 ----
    # 高置信归属 hint > 当前 cue 正文与上下文 > 中文字面值保护 > medium hint > 随机例句
    # 做法：high hint 超量时先缩 medium hint，再缩随机例句（正文与 high hint 不动）。
    # 这里 body / mask 说明是「必须保住」的部分，所以只裁 extra 里的低优先级项。
    # ⚠️ 2026-10-25：轨级记忆**不再放在 extra 头部**。原来 `extra = _MEM_TXT[0]`
    # 让它占住预算最前面，而尾部截断只砍后面——称谓锁/术语锁（硬约束）反而先被丢掉。
    # 现在顺序 = 称谓锁 → 术语/变体 → 轨级记忆（最软，先被砍）。
    extra = ""
    prag_meta = {"n_high": 0, "n_medium": 0, "n_low": 0, "dropped": 0}
    # ---- 归属结构层（GPT 第十轮 Q1 · P2）：本块命中的话语行为 hint ----
    # P2 要求：提示块放在「待润色 cue 列表**正上方**」，不是整个 user 内容最开头。
    # 因此这里先算、先攒，最后拼在 body 之前、mask 说明之后。
    ph_txt = ""
    if PRAG_ON:
        try:
            # C1： item 第 4 位带原始声纹标签（spk_raw），供「换人→人称重置」提示用；
            #     提示文本本身**不含编号**（防照抄污染）。prev 由 try_block 通过
            #     _PREV_SPK_RAW 传入（本块第一条的上一句标签）。
            ph_txt, prag_meta = prag_block_hints(
                [(it[0], it[1], it[2], it[4] if len(it) > 4 else "")
                 for it in block], prev_spk_raw=_PREV_SPK_RAW[0])
        except Exception:
            ph_txt, prag_meta = "", prag_meta
        if ph_txt:
            PRAG_STATS["blocks"] += 1
            PRAG_STATS["cues"] += ph_txt.count("] ")
            PRAG_STATS["high"] += prag_meta.get("n_high", 0)
            PRAG_STATS["medium"] += prag_meta.get("n_medium", 0)
            PRAG_STATS["dropped"] += prag_meta.get("dropped", 0)
    # ---- 实体账本：本块命中的称谓锁（最强约束，放最前）----
    led_txt = ledger_hits([b[1] for b in block])
    if led_txt:
        extra += led_txt + "\n"
    # ---- A6：把「本批命中的策略表条目」显式给出（替代随机例句）----
    hit = policy_hits([b[1] for b in block])
    if hit["terms"]:
        extra += "【本批命中的固定译法】（遇到必须这样译）\n"
        extra += "\n".join("  %s → %s" % (k, v) for k, v in hit["terms"]) + "\n"
    if hit["small"] or hit["voice"]:
        extra += "【本批出现的疑似 ASR 变体】\n"
        for v in hit["small"]:
            extra += ("  %s：全库仅数次，疑似小写假名变体"
                      "（**不要**按标准形改写，照原样处理）\n" % v)
        for v in hit["voice"]:
            extra += ("  %s：浊音近音变体，是发音问题、**不是**气声\n" % v)
    # ---- 轨级记忆（R110/R117）：放在最后 = 最低优先级，超预算时先被砍 ----
    # 它给的是「这场戏在讲什么」的整轨背景，属于辅助理解；术语/称谓硬约束在上面。
    if _MEM_GLOBAL[0] or _MEM_ITEMS[0]:
        _mt = _MEM_GLOBAL[0]
        if _MEM_ITEMS[0]:
            _blk_ja = "".join(b[1] for b in block)
            _hit = [x for x in _MEM_ITEMS[0] if x["ja"] in _blk_ja]
            if _hit:
                _mt += "本块相关（只列本块日文里真出现的）：" + "；".join(x["note"] for x in _hit) + "\n"
        if _mt.strip():
            extra += _mt
    # ---- M2 协议（GPT 第十轮 Q2）：字面值载体〔哈啊…〕的说明 ----
    # 与旧 `<M1>` 的区别：模型看到的是**最终中文**，所以提示语改成「原样保留」而不是
    # 「照抄占位符」——它不容易把 〔哈啊…〕 当格式噪声（这正是 73% 回退的根因）。
    if mask is not None and mask.LITERAL and any(spans_of.values()):
        user_mask_note = (
            "\n⚠️ **重要**：日文里出现的 `〔…〕` 里已经是**最终中文**"
            "（如 `〔哈啊…〕` `〔抱紧〕` 是拟声/气声的中文对应）。"
            "请**原样保留**这些片段在语义对应的位置——不要翻译、不要改写、"
            "不要删、也不要额外重复。")
    elif mask is not None and any(spans_of.values()):
        user_mask_note = (
            "\n⚠️ **重要**：日文里出现的 `<M1>` `<M2>`… 是**已抽出的拟声/气声片段**的"
            "占位符。你必须**逐字照抄**这些占位符到你的中文译文里（放在语义对应的"
            "位置），**一个都不能少、不能多、不能改序号、不能翻译、不能删**。"
            "例如日文 `で、<M1>、<M2>、お褒美です` → 中文必须是"
            " `所以 <M1> <M2> 是给我的奖励`（`<M1>` `<M2>` 原样保留）。")
    else:
        user_mask_note = ""
    # 【输出】段与正文一起下移：P2 要求提示块紧贴正文、在正文**正上方**
    tail = ("\n\n【输出】**严格 %d 行**，每行以句 ID 开头，格式 `[句ID]|中文译文`。\n"
            "⚠️ 不许改 ID、不许合并两句、不许漏行、不许把 A 句的中文写到 B 句上；"
            "中文里不要出现日文，也不要出现初译原文。"
            % len(block))
    # ---- 上下文预算：extra 超限时从尾部砍（低优先级在后）----
    # ⚠️ 2026-10-25 修：这里原来算了 `nth = len(body)//3` 却**从没用它**，
    # 注释写着「只裁 extra 里的低优先级项」实际一行裁剪都没有 —— 轨级记忆/称谓锁/策略表
    # 都会往 extra 里堆，堆爆了 llama.cpp 会**截断 prompt 开头**（system 直接受损），
    # 而且症状是「跑得动但质量莫名变差」，极难定位。现在按字符硬截断并打印。
    _MAX_EXTRA = int(os.environ.get("KIKOERU_MAX_EXTRA", "900"))
    if len(extra) > _MAX_EXTRA:
        _cut = extra[:_MAX_EXTRA]
        _cut = _cut[:_cut.rfind("\n")] if "\n" in _cut else _cut
        print("    ⚠️ extra 超 %d 字符，已截到 %d（丢掉的是低优先级：策略变体/记忆尾部）"
              % (_MAX_EXTRA, len(_cut)), flush=True)
        extra = _cut
    user = ("%s\n\n%s\n\n【待润色】共 %d 句。每句有一个**句 ID**、日文原文、机器初译。\n%s%s"
            % (ph_txt + "\n" if ph_txt else "",
               extra, len(block), body, user_mask_note + tail))
    if prev_ctx:
        user = ("【前文】（已译好的前几句，仅供保持语境与口吻连贯，不要重复翻译、不要输出）\n"
                + "\n".join(prev_ctx) + "\n\n") + user
    reply = call([{"role": "system", "content": system}, {"role": "user", "content": user}])
    res, why = parse_reply_id(reply, [b[0] for b in block],
                              {b[0]: b[3] for b in block})
    if res and not any(any(k in t for k in REFUSE) for t in res.values()):
        # ---- A8 还原：把中文里的 <M1> 填回中文拟声 ----
        # ⚠️ 单句级回退（2026-10-16 A8 接入实测）：模型偶尔会丢占位符，
        #    早期写法「任一句校验失败 → 整块 return None」代价太大（整块的
        #    好句子被拖去重试/回退）。改成：该句回退其初译，其余句照常。
        #    丢占位符的句子本就没处理拟声，回退初译不劣于放行。
        if mask is not None:
            old_of = {i: old for i, _ja, old, _anc, *_x in block}
            ja_of = {i: ja for i, ja, _old, _anc, *_x in block}
            for i in list(res.keys()):
                sp = spans_of.get(i) or []
                if not sp:
                    continue
                raw = res[i]
                _lit = bool(getattr(mask, "LITERAL", False))
                if _lit:
                    # M2 协议：中文里已经有字面值，不"还原"，只校验+剥包装
                    fixed, probs, lit_info = mask.check_literal(raw, sp)
                    _wrapper = lit_info.get("wrapper", "na")
                    _observed = lit_info.get("observed") or []
                else:
                    fixed, probs = mask.restore_zh(raw, sp)
                    _wrapper = "na"
                    _observed = []
                rec = {"run_id": _RUN_ID[0], "track": _CUR_TRACK[0], "cue_id": i,
                       "ja": ja_of.get(i, "")[:60],
                       "expected_spans": [o for _p, o in sp],
                       "model_output_raw": raw[:200],
                       "observed_spans": _observed,
                       "wrapper_form": _wrapper,
                       "retry_count": _CUE_RETRY.get((_CUR_TRACK[0], i), 0)}
                if probs:
                    stats_mask_fail[0] += 1
                    rec.update(fail_kind=_mask_fail_kind(probs),
                               reason="; ".join(probs)[:120])
                    # ---- D10 回退阶梯（2026-10-18）----
                    # 原来失败直接回退初译（73% 回退率的元凶）。改成三级：
                    #   ① 单条重试（点名丢了哪段，温度略高增加多样性）
                    #   ② 无遮蔽定向润色（把原日文给它，明示目标中文）
                    #   ③ 仍失败才回退初译
                    _ok = False
                    if MASK_RETRY:
                        _CUE_RETRY[(_CUR_TRACK[0], i)] = 1
                        # ⚠️ rec 是在重试之前建的，retry_count 记的是旧值 0
                        #    → 这里同步一次，否则报表 retry_total 永远是 0（实测踩到）
                        rec["retry_count"] = 1
                        zh2, why2 = _retry_one_cue(
                            system, ja_disp.get(i, ""), old_of.get(i) or "", raw, sp, _lit)
                        if zh2:
                            if _lit:
                                f2, p2, i2 = mask.check_literal(zh2, sp)
                                w2 = i2.get("wrapper", "na")
                                o2 = i2.get("observed") or []
                            else:
                                f2, p2 = mask.restore_zh(zh2, sp)
                                w2, o2 = "na", []
                            if not p2:
                                res[i] = f2
                                rec.update(status="success", final_method="retry",
                                           reason="重试成功（原问题：%s）"
                                                  % rec["reason"][:60],
                                           wrapper_form=w2, observed_spans=o2)
                                _ok = True
                        # ② 无遮蔽定向润色：去掉占位符，直接让模型处理拟声
                        if not _ok and MASK_UNMASK_RETRY:
                            zh3 = _unmasked_rewrite(system, ja_of.get(i, ""),
                                                    old_of.get(i) or "", raw)
                            if zh3:
                                res[i] = zh3
                                rec.update(status="success",
                                           final_method="unmasked_rewrite",
                                           reason="无遮蔽定向润色（原问题：%s）"
                                                  % rec["reason"][:60])
                                _ok = True
                    if not _ok:
                        res[i] = old_of.get(i) or res[i]   # ③ 回退该句初译
                        rec.update(status="fallback", final_method="draft_fallback")
                else:
                    res[i] = fixed
                    # ⚠️ final_method 要区分协议：M2 分支**没有"还原"这一步**，
                    # 只是「校验 + 剥包装」，中文本来就在模型输出里。
                    # 记成 literal_restore 会让报表误以为走了 restore_zh（2026-10-18 修）。
                    # observed_spans 保留 check_literal 实际观测到的核心值，
                    # **不要**覆盖成 `[o for _p, o in sp]`（那是 expected_spans，重复了）。
                    rec.update(status="success", fail_kind="",
                               reason="",
                               final_method="literal_strip" if _lit else "literal_restore")
                MASK_AUDIT.append(rec)
        # ---- D5 定向重译（2026-10-18）----
        # 块润色完成后跑后检测，**只对 high 告警的 cue** 单独重译一次。
        # high = 源语与中文明确冲突（强听者标记没译出来 / 否定极性翻反）；
        # soft 不动（GPT 第十轮 Q3：软门禁只告警不阻断）。
        # ⚠️ 不依赖 old_of/ja_of：那两个只在 `if mask is not None` 里定义。
        if TARGETED_RETRANSLATE and _PH is not None:
            _ja_map = {i2: ja for i2, ja, _o, _a, *_x in block}
            _old_map = {i2: o for i2, _ja, o, _a, *_x in block}
            _done = 0
            for _i in list(res.keys()):
                if _done >= TARGETED_MAX:
                    break
                _ja = _ja_map.get(_i, "")
                _zh = res.get(_i) or ""
                try:
                    _al = _PH.check_pair(_ja, _zh)
                except Exception:
                    continue
                _highs = [a for a in _al if a[0] == "high"]
                if not _highs:
                    continue
                _why = "; ".join(a[2] for a in _highs)[:160]
                _zh2 = _targeted_rewrite(system, _ja, _old_map.get(_i) or "", _zh, _why)
                if _zh2 and _zh2 != _zh:
                    res[_i] = _zh2
                    _done += 1
                    PRAG_STATS["targeted"] = PRAG_STATS.get("targeted", 0) + 1
                    TARGETED_LOG.append({"track": _CUR_TRACK[0], "cue_id": _i,
                                         "why": _why, "old": _zh[:80],
                                         "new": _zh2[:80]})
        return res, reply
    return None, reply or ("__ERR__:%s" % why)


REVIEW_SYS = r"""你是资深日语音声作品中文字幕的**审校**。
下面是一场戏里连续的一段对白：前面几句已经定稿，后面是刚译好的中文（附日文原文和说话人）。

你的任务不是重翻，而是**边读边检查上下文**，只在确有必要时修改：
1. **人称与称呼是否前后一致**：同一个角色不能一会儿「我」一会儿「人家」；
   对对方的称呼（老哥／哥哥／你／前辈…）全篇要统一。
2. **这句是谁说的**：注意说话人标签。同一声优常分饰多角，
   姐姐和妹妹、傲娇和温顺的口吻必须分明，别把两个人的腔调混成一个人。
3. **承接是否自然**：前一句是问句，后一句就要答得上；情绪强度、时态、逻辑要接得住。
4. **有没有机械直译**：按日文语序硬搬的句子改写成中文口语的自然说法。
   典型病灶：「是…的呢」「因为…所以…」结构、「男性居多，因为宗教原因」这类
   公文腔、把 の 从句整个嵌进定语。改成家里人聊天会说的话。
5. **「呢」浓度**：连续两句以上用「呢」结尾就是机器味，把多余的换成
   啊/吧/喔 或直接去掉（日文本来就没有语气助词的句子必须去掉）。
6. **疑问句有没有被译成陈述句**：Whisper 识别出的日文常常不带「？」，
   必须看句末助词（か／かな／の／ん／っけ／でしょ／でしょう／ですか）判断，
   是疑问句就一定要用「？」。
7. **格式**：句末用句号「。」收束；**句内停顿优先用半角空格**，短促连呼/重复一律用空格
   （「加油 加油 加油」「主换人了呢 真可爱」），只有真从句才留逗号、一行最多一个；
   语气延长用三点「…」；语气词只用一个。看到一行里两个以上逗号，基本是误用，改掉。
8. **该活泼的地方要活泼**：拟声（撸撸 蹭蹭 咕啾）、拖音（~）、♡ 符号
   在撒娇/兴奋/高潮场景该有就有，别全部抹平成书面语。
9. **ASR 同音错字**：日文原文是语音识别产物，会有错字。译文讲不通时
   回头想原文的近音正确词再改译。实测案例：「男子近世」＝「男子禁制」（禁入）、
   「ひとんて」＝「人（ひと）の」、「おはん」＝口语应答词。
10. **省略主语/生硬祈使——读不读得懂的关键**：日文主语宾语几乎全省，中文必须补。
    两人对话默认说话人＝「我」、听话人＝「你」。
    ·「家のこと知らないで」 ✗「不了解家里的情况」（谁不了解？）
      ✓「你又不了解我们家的情况」
    ·「お母さんがやってんじゃない?」 ✗「不是我妈弄的？」 ✓「这本来是我妈弄的啦」
    ·「感謝しろよ」 ✗「感谢吧」 ✓「你得谢谢我哦」
    ·「いけないんだ」 ✗「不该这样」 ✓「（你）不能这样啦」
    复核时逐句自问「谁对谁做了什么」，答不上来就补主语/宾语后重写。
    宁可补得啰嗦，也不要让读者猜。
11. **口语小词别按字面硬译**：「いや」＝「不是啦」（不是「不行」）；
   「いや、だって」＝「不是啦，可是…」；句末「〜んじゃない?」是推测／反问
   （「…吧？」「不是…吗？」），**不是「应该做」**；「お邪魔する」＝「打扰了」，不是「闯入」。
   日文没有「应该」这种义务表达——看到中文里冒出「应该」，多半是错译，回去查原文。
12. **翻译腔五病**（看到就改）：「关于／对于」综合症（✗关于那件事，我很抱歉）、
   「被」字滥用（✗他被大家所讨厌着）、「进行／做出」虚假动词（✗作出拒绝）、
   无意义复数「们」（✗学生们都看向我）、过长定语「的」链（✗…的表情的她）。
   还有：节制语气词，别满篇「呢/啦/哦/呀」；「ええと/あの」别机械译成「那个」。

13. **后文只读（P1-a，2026-10-28）**：给到【后文（只读）】时，**不要改后文、不要把后文
    的 ID 写进输出**。它的唯一用途是判断本块最后几句：
    · 后一句是**回答/承接** → 本块末句该是问句/提议，而不是陈述句（`〜の?` 必须译成「？」）；
    · 后一句用「所以/那就/但是」开头 → 本块末句大概率是原因/前提，别把它译成结论；
    · 后一句出现的人名/称呼/代词 → 本块的「他/她/那个」要指同一个人，不能换人；
    · 后一句把话接走了 → 本块末句不能译成「话已说完」的收尾（别加「了」「就是这样」）。

输出：每行以**句 ID** 开头，格式 `[句ID]|最终中文`。没问题的句子原样输出，不要改写。
"""

# ---------------------------------------------------------------- 结构性高风险（P1-b）
# 「只靠前文判不出来、必须看后一句」的句子。全库实测（23651 段 ASR，R154）：
#   连接词开头 6.4% / 句首指示词 5.8% / 助词开头碎片 3.7% → 去重后合计 **14.4%**。
# 只有这些块才把【后文（只读）】塞进 review prompt；其余块保持原样（算力基本不变）。
# 口径为什么这么窄：
#   · `そう`/`こう`/`ああ` 单独出现几乎全是「そうですね」这类应答，全量算进来会飙到 25.9%；
#   · 助词只算「几乎不会起句」的那批（の/を/は/が/ね/よ…）。`で`/`と`/`も`/`に` 起句
#     是正常日语（「で、どうする？」），算进来会多出 3.7pp 全是误报。
_RISK_CONN = re.compile(
    r"^(でも|だって|だから|ですから|だけど|けど|けれど|けれども|しかし|そして|それで|"
    r"それに|それじゃ|じゃあ|では|つまり|あの|えっと|いや|うん|はい|または|ただ)")
_RISK_DEMON = re.compile(
    r"^(これ|それ|あれ|この|その|あの|こんな|そんな|あんな|こういう|そういう|ああいう|"
    r"どれ|どの|こちら|そちら|あちら)")
_RISK_PARTICLE = re.compile(r"^(の|を|は|が|ね|よ|な|わ|ぞ|ぜ|より|まで|から|へ)")


def struct_risk(ja):
    """返回该句的结构性风险标签列表（空 = 低风险）。纯确定性，可复现。"""
    ja = (ja or "").strip()
    if not ja:
        return []
    why = []
    if _RISK_CONN.match(ja):
        why.append("conn")
    if _RISK_DEMON.match(ja):
        why.append("demon")
    # 助词开头只对短句算：长句以助词开头通常是 ASR 切在词中间，另有 slot_guard 管。
    if _RISK_PARTICLE.match(ja) and len(ja) <= 12:
        why.append("particle")
    return why


# 后文策略：auto=只对高风险块给后文（默认）/ all=全给 / off=完全不给（旧行为）
LOOKAHEAD = os.environ.get("KIKOERU_REVIEW_LOOKAHEAD", "auto").strip().lower()
if LOOKAHEAD not in ("auto", "all", "off"):
    LOOKAHEAD = "auto"
LOOKAHEAD_N = 2          # 只给后 2 句：给多了模型会开始「重写后文」
LOOKAHEAD_STATS = {"blocks": 0, "risky": 0, "why": {}}


def _drop_foreign_ids(reply, keep):
    """把回执里**不属于本块**的 ID 段整段丢掉。

    ⚠️ 必须做：`parse_reply_id` 对「多出来的 ID」是**硬失败**（整块重试）。
    加了后文之后，模型偶尔会顺手把后文也「复核」一遍输出出来 ——
    那不是错误，只是它多干了活，丢段即可，不该让整块重试。
    """
    marks = [(m.start(), m.end(), m.group(1)) for m in ID_RE.finditer(reply or "")]
    if not marks:
        return reply
    keep = set(keep)
    out = []
    # 按标记切成「一段一 ID」（段 = 本标记到下一个标记），只留 keep 里的段。
    # ⚠️ 不能用「跳到下一个 keep 标记」的写法：最后一段会把后文的尾巴一起吞进来。
    for k, (s, e, cid) in enumerate(marks):
        nxt = marks[k + 1][0] if k + 1 < len(marks) else len(reply)
        if cid in keep:
            out.append(reply[s:nxt])
    return "\n".join(out) if out else reply


def review_block(prev_zh, block, next_zh=None):
    """block: [(id, ja, 当前中文, 说话人)]，prev_zh: 前面几句定稿中文。

    next_zh（P1-a，2026-10-28）：**只读**的后文 2 句。日文的 でも/だから/これ/それ、
    反问与预设回答，只靠前文判不出来 —— 实测 131/706 段边界切在句中（R153）。
    后文绝不进输出：`_drop_foreign_ids` 兜住模型顺手多输出的情况。
    """
    body = []
    for i, ja, zh, spk in block:
        body.append("[%s]\n日：%s\n现：%s%s" % (i, ja, zh, ("\n人：%s" % spk) if spk else ""))
    tail = ""
    if next_zh:
        tail = ("\n\n【后文（只读，禁止修改、禁止输出其 ID）】\n%s"
                % "\n".join(next_zh))
    user = ("【前文定稿】\n%s\n\n【待复核】共 %d 句：\n%s%s\n\n"
            "【输出】**严格 %d 行**，每行以句 ID 开头，格式 `[句ID]|最终中文`，"
            "不许改 ID、不许合并、不许漏行、不许串到别的 ID 上%s："
            % ("\n".join(prev_zh) if prev_zh else "（开头，无前文）",
               len(block), "\n\n".join(body), tail, len(block),
               "、不许把后文的 ID 写进输出" if next_zh else ""))
    reply = call([{"role": "system", "content": REVIEW_SYS},
                  {"role": "user", "content": user}])
    want = [b[0] for b in block]
    if next_zh:
        reply = _drop_foreign_ids(reply, want)
    res, _why = parse_reply_id(reply, want, None)
    if res and not any(any(k in t for k in REFUSE) for t in res.values()):
        return res
    return None


def spk_leak(zh, spk):
    """译文是否泄漏了说话人名（R28，2026-10-23；R35 前缀感知化）。

    `spk`：该行已判明的角色名（来自台本/C1 接线；没有则空串）。
    实测 RJ9000014 台本轨 5 种泄漏格式（10-22 只防住行首，漏了 36 行）：
      ① 整行只剩人名（「白」）② 行首 `X `/`X的`（**不带冒号**才是泄漏）
      ③ 行尾 `人：X`（「…很舒服。人：白」）④ 行尾标点后裸名（「…女孩…甘」）
      ⑤ 行尾 ` X[。！？…]`（「是的 白。」）
    ⚠️ R35（2026-10-23 用户拍板乙方案）：行首 `X：` 是**规范前缀格式**
      （「白：你好。」），不再算泄漏；只有不带冒号的 `X `/`X的` 才算。
    ⚠️ 行文人名不判（「我明白。」「把脸涂白。」「白天」是合法行文）。
    """
    if not spk:
        return False
    zh = (zh or "").strip()
    if not zh:
        return False
    if zh == spk:
        return True
    if zh.startswith((spk + " ", spk + "的")):
        return True                      # 不带冒号的行首 → 仍是模型泄漏
    if re.search(r"人：\s*%s\s*$" % re.escape(spk), zh):
        return True
    if re.search(r"[。，！？…～]\s*%s$" % re.escape(spk), zh):
        return True
    if re.search(r"\s+%s[。！？…]?$" % re.escape(spk), zh):
        return True
    return False


def strip_spk_leak(zh, spk):
    """剥掉译文中任意位置的说话人名泄漏（R35，乙方案配套）。

    与 `spk_leak` 同判据但做**移除**：供「人名前缀」模式下先把泄漏清干净，
    再统一前置成 `X：译文`。行文人名不动（只在行首/行尾位置剥）。
    """
    if not spk:
        return (zh or "").strip()
    t = (zh or "").strip()
    t = re.sub(r"\s*人：\s*%s\s*$" % re.escape(spk), "", t)
    t = re.sub(r"([。，！？…～])\s*%s$" % re.escape(spk), r"\1", t)
    t = re.sub(r"\s+%s([。！？…]?)$" % re.escape(spk), r"\1", t)
    # R43（2026-10-23）：第 6 种泄漏格式——**人名前没有标点也没有空格**
    # （「甘：但是甘」）。不能无条件剥（「明白」「不甘」是合法词），所以只在
    # 「行首已经有规范前缀 X：」时才剥——同一行出现两次人名必然是泄漏。
    # ⚠️ 必须放在剥行首前缀**之前**，否则前缀被剥掉后这个判据就失效了。
    if re.match(r"^%s[：:]" % re.escape(spk), t):
        t = re.sub(r"(?<=[\u4e00-\u9fff])%s$" % re.escape(spk), "", t)
    t = re.sub(r"^%s[：:\s]+" % re.escape(spk), "", t)
    if t == spk:
        return ""
    return t.strip()


def fmt_lrc(sec):
    m, s = divmod(max(0.0, sec), 60)
    return "[%02d:%05.2f]" % (int(m), s)


def _guess_asr_dir(draft_dir):
    """从初译目录推断同作品的 ASR json 目录（供 term_lock 做日文侧校验）。

    draft 目录结构：`<作品>/draft_hymt/` 与 `<作品>/asr/` 同级。
    对不上就返回 ""，term_lock 会退化为「不做日文校验」模式。
    """
    try:
        parent = os.path.dirname(os.path.abspath(draft_dir))
        cand = os.path.join(parent, "asr")
        return cand if os.path.isdir(cand) else ""
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("asr_dir")
    ap.add_argument("draft_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--style", default=DEFAULT_STYLE)
    ap.add_argument("--no-review", dest="review", action="store_false",
                    help="关掉润色后的上下文连贯复核（默认开）")
    ap.add_argument("--cleanup-only", dest="cleanup_only", action="store_true",
                    help="完全不过 LLM，只对初译做标点/格式后处理。"
                         "初译交给翻译专用模型（如 SakuraLLM）时用这个："
                         "它的输出已是 ACGN 风格，再套为 Qwen 设计的润色反而会坏。")
    ap.add_argument("--allow-loss", dest="allow_loss", action="store_true",
                    help="放行「成品行数 < 输入 cue 数」（默认**不允许**）。"
                         "丢行 = 用户永远看不到那句，比译错严重（GPT 第六轮 Q5 硬门禁）。")
    ap.add_argument("--no-term-lock", dest="no_term_lock", action="store_true",
                    help="关掉收尾的「称谓/术语一致性锁定」（term_lock.py，默认开）。"
                         "该步骤用确定性覆写保证「先生/老师」等称谓全篇统一——"
                         "LLM 对汉语合法词二选一会摇摆，prompt 挡不住（2026-10-16）。")
    ap.add_argument("--system-mode", dest="system_mode", default="strong",
                    choices=["strong", "conservative"],
                    help="B1 实验：strong=完整润色（默认，19 条改发动机）；"
                         "conservative=最小改动校对（只修术语/错译/漏主语/"
                         "日文残留/标点五类，其余原样照抄）。")
    ap.add_argument("--force", action="store_true",
                    help="忽略 cue 级缓存全量重跑（E2；缓存仍会按新结果更新）。"
                         "改了通用机制（prompt 模板/CHUNK/检测器）后建议带此参数跑一次全量。")
    ap.add_argument("--review-lookahead", dest="review_lookahead", default=None,
                    choices=["auto", "all", "off"],
                    help="复核时给不给【后文（只读）】（P1-a/P1-b，2026-10-28）。"
                         "auto=只对结构性高风险块给（默认，实测 17.3% 的句）；"
                         "all=每块都给；off=旧行为。也可用 KIKOERU_REVIEW_LOOKAHEAD。")
    ap.add_argument("key", nargs="?", default="")
    a = ap.parse_args()

    os.makedirs(a.out_dir, exist_ok=True)
    # E1：句级锁定（人工审阅页改过并锁定的行，重跑时覆写保护）
    if os.environ.get("KIKOERU_NO_LOCKS") in ("1", "true", "True"):
        print("句级锁定：KIKOERU_NO_LOCKS=1，已禁用")
    else:
        _nlocks = load_locks(a.out_dir)
        if _nlocks:
            print("句级锁定：载入 %d 条（%s）→ 重跑将逐字覆写这些行"
                  % (_nlocks, os.path.join(LOCKS_DIR,
                                           os.path.basename(os.path.abspath(a.out_dir)) + ".json")))
    # GPT 第十轮 Q5：运行级审计标识（每次 polish 启动一个，写进每条 mask 记录）
    _RUN_ID[0] = "%s-%s" % (time.strftime("%Y-%m-%dT%H:%M:%S"),
                            os.path.basename(os.path.abspath(a.out_dir)))
    guide, hard, pairs, stats, gloss = load_style(a.style)
    n_pairs = stats.get("total", 0)
    # 作品级实体账本（称谓锁）：从作品名/Tag/轨名推「先生→老师」等称谓族，
    # 每个 block 只注入本块命中的锁（GPT 第八轮 Q1/Q2）。
    load_ledger(a.draft_dir)
    # A8：非实义 span 遮蔽器（把 ja 里的拟声/气声换成 <M1> 占位符，避免被字面翻译）。
    # KIKOERU_MASK_NONLEX=0 可关掉（做 A/B 对照用）；模块自身读该环境变量决定 ENABLED。
    global MASK
    MASK = None
    try:
        import mask_nonlexical as _mn
        if _mn.ENABLED:
            MASK = _mn
            print("A8 遮蔽器：已启用（拟声/气声 → %s）"
                  % ("〔中文字面值〕" if _mn.LITERAL else "<M1> 占位符"))
        else:
            print("A8 遮蔽器：KIKOERU_MASK_NONLEX=0，已禁用")
    except Exception as e:
        print("⚠️ A8 遮蔽器不可用（跳过）：%s" % e)
    # 归属结构层（GPT 第九轮 Q1）：话语行为 hint，按 block 注入
    load_pragmatics()
    if not pairs:
        print("警告：%s 下没有平行语料，将退化为普通翻译（先跑 kikoeru-style-learn）" % a.style)
    print("风格指南 %d 字 / 硬约束 %d 条 / 译者样例 %d 组 / 语料 %d 对 / 术语 %d 条"
          % (len(guide), len(hard.splitlines()), len(pairs), n_pairs, len(gloss)))
    # 硬规则、术语表会随每次学习不断增长，system prompt 迟早会顶到 llama-server
    # 的上下文上限（-c 会被 -np 平分，每槽实际可用远小于 -c）。超了就 400 直接崩，
    # 所以这里主动缩减例句数量，宁可少给几条例句也不要整个作品跑不动。
    if a.system_mode == "conservative":
        # B1：保守模式不给风格例句（不改风格，给了反而诱导改写）
        system = build_conservative(gloss)
        print("conservative 模式：system prompt %d 字符（最小改动校对）" % len(system))
    else:
        sample = pairs
        while len(sample) > 2:
            cand = build_system(guide, hard, sample, n_pairs, gloss)
            if len(cand) <= SYS_MAX:
                break
            if len(sample) <= 4:
                # 样例已减到下限仍超 SYS_MAX → 静态部分（模板+硬规则+指南+术语）
                # 本身就超限，再减样例没用。
                # ⚠️ 这里必须 break：旧写法 sample[:max(4, len//2)] 在 len==4 时
                # 取 sample[:4] 原样不动 → 永远 4 > 2 → **死循环烧 CPU**
                # （2026-10-16 实测卡死 1 小时才发现，症状：GPU 空闲、无输出、
                #  python 进程 100% CPU。py-spy dump 抓到 build_system 里空转。）
                # 真实约束是「token 数 ≤ 每槽上下文」，字符只是粗估；
                # 超限时 llama.cpp 会截断 prompt 开头（系统提示受损），
                # 所以配套措施：serve.py 用 -np 2（每槽 12288 tokens）。
                print("⚠️ system prompt %d 字符仍超 SYS_MAX=%d"
                      "（模板+硬规则+指南+术语本身已超限，样例减到底也没用）；"
                      "请确认 llama-server 每槽上下文 ≥ prompt token 数 + 2000"
                      % (len(cand), SYS_MAX), flush=True)
                break
            sample = sample[:max(4, len(sample) // 2)]
        system = build_system(guide, hard, sample, n_pairs, gloss)
        print("system prompt 约 %d 字符（样例 %d 组）\n" % (len(system), len(sample)))

    try:
        urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=10).read()
    except Exception as e:
        if a.cleanup_only:
            pass          # 不调 LLM，不需要服务
        else:
            print("llama-server 未就绪: %s\n先运行 serve.py start" % e)
            return

    if os.environ.get("KIKOERU_DUMP_PROMPT"):
        _DUMP_DIR[0] = os.path.join(a.out_dir, "_prompt_log")
        os.makedirs(_DUMP_DIR[0], exist_ok=True)
        print("KIKOERU_DUMP_PROMPT：prompt 落盘 → %s" % _DUMP_DIR[0], flush=True)
    files = sorted(glob.glob(os.path.join(a.asr_dir, "*.json")))
    if a.key:
        files = [f for f in files if a.key in os.path.basename(f)]
    if not files:
        print("没有待处理文件")
        return

    # ---- E2：cue 级缓存（按作品）----
    _SYS_MODE[0] = a.system_mode
    global REVIEW                      # --no-review 要进缓存签名（复核结果不同）
    REVIEW = bool(a.review)
    global LOOKAHEAD                   # 后文策略同样进缓存签名
    if a.review_lookahead:
        LOOKAHEAD = a.review_lookahead
    print("复核后文策略：%s（auto=仅结构性高风险块，实测 17.3%%）" % LOOKAHEAD)
    _sig_path = os.path.join(CACHE_DIR, os.path.basename(os.path.abspath(a.out_dir)) + ".json")
    _sig_cache = {}
    _use_cache = (not a.force and not a.cleanup_only
                  and os.environ.get("KIKOERU_NO_CACHE") not in ("1", "true", "True"))
    if _use_cache and os.path.exists(_sig_path):
        try:
            _sig_cache = json.load(open(_sig_path, encoding="utf-8"))
        except Exception:
            _sig_cache = {}
    _cache_hit_total = 0

    for jf in files:
        d = json.load(open(jf, encoding="utf-8"))
        src = d["source"]
        _m = load_mem(a.asr_dir, src)
        _MEM_TXT[0] = _m["sig"]
        _MEM_GLOBAL[0] = _m["global_txt"]
        _MEM_ITEMS[0] = _m["items"]
        if _MEM_ITEMS[0]:
            print("  [轨级记忆] 全局卡 %d 字符 + 按块条目 %d 条"
                  % (len(_MEM_GLOBAL[0]), len(_MEM_ITEMS[0])), flush=True)
        _CUR_TRACK[0] = src
        _fb_before = stats_mask_fail[0]     # 本轨回退增量（审计用，见下）
        # GPT 第十轮 Q5：计数器**每轨 reset**，全局报表由 mask_audit.json 聚合生成。
        stats_mask_fail[0] = 0
        _CUE_RETRY.clear()
        LOOKAHEAD_STATS.update({"blocks": 0, "risky": 0, "why": {}})
        lines, is_cue = cues.build_units(d["segments"], build_lines)
        # 只把「已判明的角色名」传给模型；没判明就留空。
        # 绝不能把原始声纹编号 SPEAKER_00 塞进去——那是给模型的噪音，
        # 实测它会把整行照抄成「SPEAKER_00」（RJ9000006 第 06 轨 8 行）。
        roles = d.get("roles") or {}
        spk_name = {k: (v.get("name") or "").strip()
                    for k, v in roles.items() if isinstance(v, dict)}
        for ln in lines:
            _raw = ln.get("spk", "")
            # C1（2026-10-21）：原始声纹标签另存 spk_raw ——「换人→人称重置」的
            # 结构提示要用它（pragmatics_hints.speaker_switch_notes），
            # 但**不能把 SPEAKER_xx 本身写进 prompt**（模型会照抄进成品）。
            ln["spk_raw"] = _raw
            # ⚠️ 台本（script_align）直接给的是**角色名**（`みう`/`ねね`），不是声纹编号。
            #    原来无条件走 spk_name 映射，没有 roles 时就清空了 —— 台本信息白给。
            #    只对 `SPEAKER_00` 这种编号做映射；已经是人名的直接保留。
            if _raw and not re.match(r"^SPEAKER_\d+$", _raw, re.I):
                continue
            ln["spk"] = spk_name.get(_raw, "")
        dp = os.path.join(a.draft_dir, src + ".lrc")
        old = read_lrc(dp) if os.path.exists(dp) else []
        olds = match_lines(lines, old)
        # 锁计数器按轨清零：全局累加会让每轨都报同一个数（实测过，日志误导）
        LOCK_N[0] = 0
        LOCK_JA_HIT[0] = 0
        LOCK_AMBIG.clear()
        print("=== %s : %d %s（已匹配初译 %d）===" % (src[:44], len(lines),
              "cue" if is_cue else "行", sum(1 for o in olds if o)), flush=True)

        results = [None] * len(lines)
        blocks = [list(range(i, min(i + CHUNK, len(lines)))) for i in range(0, len(lines), CHUNK)]
        _det = {}
        # ---- D9 第三路（2026-10-18）：纯非实义独立 cue **确定性直出**，不过模型 ----
        # 整句只有拟声/气声 + 标点、且每段都在 ZH_MAP 里 → 直接用表值。
        # 为什么值得：这类句子过 7B 只会引入波动（`はぁ` 有时「哈」有时「啊」），
        # 而且白花一次采样。全库 2.26%（498/22064）属这一类。
        # 2026-10-28 R154：**不再排除 --cleanup-only**。
        # 原来写成 `... and not a.cleanup_only`，等于把「基于字典/正则的确定性遮蔽」
        # 和「基于 LLM 的启发式润色」绑在一起一刀切了。走 Sakura 路线时（cleanup-only），
        # 拟声 cue 就没人管了——而 Sakura 恰恰最怕纯拟声输入（会吐英文/复读）。
        # 确定性规则无条件跑，LLM 才受开关控制。
        if MASK is not None and getattr(MASK, "DETERMINISTIC", False):
            for _i, _ln in enumerate(lines):
                _r = MASK.pure_nonlexical(_ln["text"])
                if _r:
                    _det[_i] = _r
                    results[_i] = _r
            if _det:
                blocks = [[i for i in b if i not in _det] for b in blocks]
                blocks = [b for b in blocks if b]
                DET_STATS[0] += len(_det)
                print("    [D9-3] 纯非实义 cue 确定性直出 %d 条（不过模型）" % len(_det),
                      flush=True)

        # ---- E2：缓存命中 → 不过 LLM，直接复用旧译文 ----
        # 签名含 system 哈希/CHUNK/MASK/review/system_mode，任一变化自动失效。
        # 命中行从 blocks 剔除；写盘时 locks 照常覆写（与缓存正交）。
        cached = {}
        if _use_cache:
            _trk_cache = _sig_cache.get(src) or {}
            for _i in range(len(lines)):
                _s = cue_sig(src, lines[_i], olds[_i], system, _MEM_TXT[0])
                if _s in _trk_cache and _trk_cache[_s]:
                    cached[_i] = _trk_cache[_s]
            if cached:
                for _i, _zh in cached.items():
                    if _i not in _det:
                        results[_i] = _zh
                blocks = [[i for i in b if i not in cached] for b in blocks]
                blocks = [b for b in blocks if b]
                _cache_hit_total += len(cached)
                print("    [E2 缓存] 命中 %d / %d 条（跳过 LLM）" % (len(cached), len(lines)),
                      flush=True)


        if a.cleanup_only:
            # 只做确定性后处理，不过 LLM。
            # 用途：初译交给**翻译专用模型**（如 SakuraLLM）时，它的输出已经是 ACGN 风格，
            # 再套我们为 Qwen 设计的润色 prompt 反而会坏——实测 Qwen 会把 Sakura 的结果
            # 整体错位一行（第 1 句被吞、后续全部前移）。
            results = list(olds)
            # ⚠️ 必须把 D9-3 的确定性结果放回去：`list(olds)` 会把它覆盖掉，
            # 那样上面那条「确定性遮蔽无条件跑」就白改了。
            for _i, _r in _det.items():
                results[_i] = _r
            print("    [cleanup-only] 跳过 LLM 润色，只做标点/格式归一"
                  + ("（确定性遮蔽 %d 条仍生效）" % len(_det) if _det else ""), flush=True)

        def prev_ctx(i):
            """第 i 句之前的几句：日文 + 已润色的中文（没有就用初译）。"""
            out = []
            for j in range(max(0, i - CTX_KEEP), i):
                zh = results[j] or olds[j]
                if not zh:
                    continue
                out.append("日：%s\n译：%s" % (lines[j]["text"], zh))
            return out

        # ---- 每句一个稳定、非连续的 ID（GPT 第四轮 Q1-a）----
        # 旧方案按「第几行」绑定模型返回：`parse_reply` 只保证行数一致、**不保证每条对位**。
        # 实测 RJ9000010 08. 5:44–6:25 四句整体错位，成品成了本轨别处句子的近似复制，
        # 而初译是对的——肉眼完全看不出。现在按 ID 映射，集合对不上就整块重试。
        ids = [cue_id(ln["text"], ln["start"], ln["end"], src) for ln in lines]
        anchors = [ja_anchor(ln["text"]) for ln in lines]
        _taken = set()
        for i in range(len(ids)):
            n = 2
            while ids[i] in _taken:                    # 撞 ID 概率极低，但要保唯一
                ids[i] = ids[i][:-1] + _ALPHA[n % 32]
                n += 1
            _taken.add(ids[i])

        # ---- 块外污染索引（Q1-b）----
        # 把本轨**所有初译**先建好：模型若把别处的句子搬过来，能立刻抓到。
        old_n = [norm_zh(o) for o in olds]

        def pollution_hit(i, zh):
            """zh 是否像本轨别处句子（j）的错位复制；命中返回 j。"""
            for j in range(len(lines)):
                if j == i or not old_n[j]:
                    continue
                if is_pollution(zh, olds[j], lines[i]["text"], lines[j]["text"]):
                    return j
            return None

        # ---- 状态机（Q1-c）----
        # 块失败 → 整块重试 1 次 → 仍失败拆半批递归 → size 1 单条重试 1 次
        #          → 仍失败退回**该 cue 自己的初译**并标 POLISH_UNVERIFIED 进人工清单
        stats_run = {"id_fail": 0, "pollution": 0, "unverified": 0, "retry_ok": 0}

        def commit(idxs, res):
            for i in idxs:
                v = res.get(ids[i])
                if not v:
                    continue
                bad_j = pollution_hit(i, v)
                if bad_j is not None:
                    stats_run["pollution"] += 1
                    print("    ⚠️ 污染：ID %s 的中文疑似复制第 %d 句（%.1fs）"
                          % (ids[i], bad_j + 1, lines[bad_j]["start"]), flush=True)
                    continue
                results[i] = v

        def try_block(idxs, depth=0):
            """返回 True=已提交；False=彻底失败（调用方负责回退）。"""
            # C1：本块第一条的上一句声纹标签（换人提示的对比基线）
            _PREV_SPK_RAW[0] = (lines[idxs[0] - 1].get("spk_raw", "")
                                if idxs and idxs[0] > 0 else "")
            blk = [(ids[i], lines[i]["text"], olds[i], anchors[i],
                    lines[i].get("spk_raw", "")) for i in idxs]
            for _attempt in range(2):                       # 同批重试 1 次
                try:
                    res, raw = polish_block(system, blk, prev_ctx(idxs[0]), mask=MASK)
                except Exception as e:
                    print("    ⚠️ 块异常：%s" % e, flush=True)
                    res, raw = None, None
                if res is None:
                    stats_run["id_fail"] += 1
                    if depth == 0:
                        print("    块失败（%s）→ 重试 1 次" % (raw or "解析失败")[:120],
                              flush=True)
                    continue
                before = [results[i] for i in idxs]
                commit(idxs, res)
                if any(results[i] != before[k] for k, i in enumerate(idxs)):
                    return True
                # 全被污染拦下 → 换更小的批再试
                stats_run["pollution"] += 1
                break
            if len(idxs) > 1:
                mid = (len(idxs) + 1) // 2
                ok1 = try_block(idxs[:mid], depth + 1)
                ok2 = try_block(idxs[mid:], depth + 1)
                return ok1 or ok2 or all(results[i] for i in idxs)
            return False

        t0 = time.time()
        # 按波次推进：同一波内并行，波与波之间串行，
        # 这样后一波能拿到前一波「已润色的中文」做上下文，保证前后连贯。
        for w0 in ([] if a.cleanup_only else range(0, len(blocks), WORKERS)):
            wb = list(range(w0, min(w0 + WORKERS, len(blocks))))
            jobs = []
            for bi in wb:
                idxs = blocks[bi]
                jobs.append((bi, idxs, prev_ctx(idxs[0])))

            def work(job):
                bi, idxs, ctx = job
                blk = [(ids[i], lines[i]["text"], olds[i], anchors[i]) for i in idxs]
                try:
                    res, raw = polish_block(system, blk, ctx, mask=MASK)
                except Exception as e:
                    res, raw = None, ("__EXC__:%s" % e)
                return bi, idxs, res, raw

            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                for bi, idxs, res, raw in ex.map(work, jobs):
                    if res is None:
                        stats_run["id_fail"] += 1
                        print("    块 %d ID/格式失败（%s）→ 重试/拆批"
                              % (bi + 1, raw or "解析失败"), flush=True)
                        try_block(idxs)      # 内部：同批重试1次 → 折半 → 单条重试1次
                        continue
                    commit(idxs, res)
                    # 被污染全拦的块，同样走拆批阶梯
                    if not any(results[i] for i in idxs):
                        try_block(idxs)

            # 边润色边复核：拿刚译好的这波 + 前文定稿再过一遍，
            # 专门修人称/称呼不一致、口吻串味、机械直译和疑问句被译成陈述句。
            if a.review:
                rjobs = []
                for bi in wb:
                    idxs = blocks[bi]
                    prev_zh = ["%d. %s" % (n + 1, t) for n, t in
                               enumerate(results[j] for j in range(max(0, idxs[0] - CTX_KEEP),
                                                                  idxs[0])) if t]
                    blk = [(ids[i], lines[i]["text"], results[i] or olds[i],
                            lines[i].get("spk", "")) for i in idxs]
                    if not any(x[2] for x in blk):
                        continue
                    # ---- P1-b：结构性高风险分流 ----
                    # 只有「连接词/句首指示词/助词开头碎片」的块才带后文；
                    # 其余块保持原 prompt（少两行，算力与命中率都不变）。
                    risk = []
                    for i in idxs:
                        for w in struct_risk(lines[i]["text"]):
                            risk.append(w)
                    for w in set(risk):
                        LOOKAHEAD_STATS["why"][w] = LOOKAHEAD_STATS["why"].get(w, 0) + 1
                    LOOKAHEAD_STATS["blocks"] += 1
                    want_la = (LOOKAHEAD == "all") or (LOOKAHEAD == "auto" and risk)
                    next_zh = []
                    if want_la:
                        # 后文取**初译**：本波之后的行还没润色，results 里是 None。
                        # 初译已经能表达「这是问句/这是回答/这里换人了」，够判承接。
                        for j in range(idxs[-1] + 1, min(idxs[-1] + 1 + LOOKAHEAD_N,
                                                          len(lines))):
                            t = (results[j] or olds[j] or "").strip()
                            if t:
                                next_zh.append("日：%s\n译：%s" % (lines[j]["text"], t))
                        if next_zh:
                            LOOKAHEAD_STATS["risky"] += 1
                    rjobs.append((bi, prev_zh, blk, next_zh))

                def rwork(job):
                    bi, prev_zh, blk, next_zh = job
                    return bi, review_block(prev_zh, blk, next_zh)

                with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                    for bi, res in ex.map(rwork, rjobs):
                        idxs = blocks[bi]
                        if res is None:
                            continue
                        for i in idxs:
                            # 复核回空或漏掉日文就保留润色结果
                            v = res.get(ids[i])
                            # 同 bad_cell 口径：连续假名/占比过高才算未译，
                            # 单个人名不构成「丢掉这次复核」的理由
                            if v and not KANA_RUN.search(v) and not (
                                    kana_count(v) and kana_count(v) / max(1, len(v)) >= 0.4):
                                if pollution_hit(i, v) is None:
                                    results[i] = v
        # ---- 末位兜底：退回**该 cue 自己的初译**，并标 POLISH_UNVERIFIED ----
        # ⚠️ 不要再拿「按时间匹配到的别处初译」顶替——那正是制造
        #    「看起来合理、实则错位」成品的原因（GPT 第四轮 Q1-c）。
        unverified = []
        for i, v in enumerate(results):
            if not v:
                results[i] = olds[i]
                if olds[i]:
                    unverified.append(i)
        stats_run["unverified"] = len(unverified)
        # A8 本轨回退数（GPT 第十轮 Q5：计数器每轨 reset，这里是本轨真实值）
        _fb_delta = stats_mask_fail[0]
        _mask_note = (" · A8丢占位符回退 %d（本轨）" % _fb_delta) if MASK else ""
        _prag_note = (" · 归属提示注入 %d 块" % PRAG_STATS["blocks"]) if PRAG_ON and prag_block_hints else ""
        _la_note = ""
        if LOOKAHEAD != "off" and LOOKAHEAD_STATS["blocks"]:
            _la_note = (" · 后文注入 %d/%d 块（%s）"
                        % (LOOKAHEAD_STATS["risky"], LOOKAHEAD_STATS["blocks"],
                           "/".join("%s %d" % (k, v)
                                    for k, v in sorted(LOOKAHEAD_STATS["why"].items()))))
        print("    润色完成 %.1f 秒｜ID/格式失败 %d · 污染拦下 %d · 回退初译 %d%s"
              % (time.time() - t0, stats_run["id_fail"], stats_run["pollution"],
                 stats_run["unverified"], _mask_note + _prag_note + _la_note), flush=True)
        # A8 per-cue 审计落盘（GPT 第九轮 Q2 + 第十轮 Q5 运行级/重试级）：
        # 每轨覆盖写 track 视图（本轨 + 全局聚合报表）。全局报表**由 records 聚合**，
        # 不再让 stats_mask_fail 这个全局可变变量当事实来源。
        if MASK:
            _trk = [c for c in MASK_AUDIT if c.get("track") == src]
            _agg = _aggregate_audit(MASK_AUDIT)
            with open(os.path.join(a.out_dir, "mask_audit.json"), "w",
                      encoding="utf-8") as f:
                json.dump({
                    "run_id": _RUN_ID[0],
                    "track": src,
                    "track_summary": _agg["by_track"].get(src, {}),
                    "global_summary": {k: v for k, v in _agg.items() if k != "by_track"},
                    "by_track": _agg["by_track"],
                    "track_cues": _trk,          # 本轨逐 cue（查回退原因看这里）
                    "all_cues": MASK_AUDIT,      # 全跑逐 cue（聚合源，勿手改）
                }, f, ensure_ascii=False, indent=1)
        if TARGETED_LOG:
            with open(os.path.join(a.out_dir, "targeted_retry.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"run_id": _RUN_ID[0], "records": TARGETED_LOG},
                          f, ensure_ascii=False, indent=1)
        for i in unverified:
            print("    [POLISH_UNVERIFIED] %d:%05.2f %s"
                  % (int(lines[i]["start"] // 60), lines[i]["start"] % 60,
                     (olds[i] or "")[:40]), flush=True)

        out = os.path.join(a.out_dir, src + ".lrc")
        jp_drop = []
        unver = []
        n_spacer = 0
        # E1 锁 v3（2026-10-31）：**写盘前**把本轨的锁按文本锚定分给 cue。
        # 必须在写盘前做 —— 显示行切分依赖最终译文，写盘时才拿得到行清单；
        # 而 cue 的 start/end/ja 早就确定了，所以按 cue 分配是唯一稳定的锚点。
        assign_locks_for_track(src, lines)
        with open(out, "w", encoding="utf-8") as f:
            f.write("[ti:%s]\n[by:%s]\n\n" % (src, MODEL_TAG))
            keep = 0
            fallback = 0
            for idx, (ln, zh) in enumerate(zip(lines, results)):
                zh = re.sub(r"\s+", " ", (zh or "")).strip()
                zh = cleanup(fix_particle(zh, ln["text"]), stats)
                # 单字译文兜底（2026-10-28 R154）：`はぁ…` 被模型译成单字「哈」，
                # 中文里单写一个「哈」像冷笑。日文是已知纯拟声词时，用 ZH_MAP 的
                # 确定性值（`はぁ`→「哈啊」）覆盖。只在「译文短到只剩一个字」时动手，
                # 不会误伤正常句子。
                if MASK is not None and len(zh or "") <= 1:
                    _r = MASK.pure_nonlexical(ln["text"])
                    if _r and len(_r) > len(zh):
                        zh = _r
                # 说话人名泄漏（2026-10-22 RJ9000014 实录）：prompt 每行带「人：白/甘」，
                # 短句/诗句时模型直接把人名当译文吐回来（实测 74 行整行只剩「白」「甘」），
                # 或把人名缀在译文前（28 行，如「白的思考变得漫无目的。」）。
                # ⚠️ R28（2026-10-23）补：泄漏不止行首——台本轨成品实测 5 种格式，
                # 行尾「人：白」25 行、行尾裸名「…甘」5 行、句中「 白。」3 行全部漏检。
                # 判据统一在 `spk_leak()`（可门禁测试）。
                # ⚠️ R35（2026-10-23 用户拍板乙方案）：`KIKOERU_SPK_PREFIX=1` 时
                # **不删行**——剥掉任意位置的泄漏人名，统一前置成「白：译文」。
                _spk = (ln.get("spk") or "").strip()
                if _spk and SPK_PREFIX_ON:
                    zh = strip_spk_leak(zh, _spk)
                    if zh and not zh.startswith(_spk + "："):
                        zh = _spk + "：" + zh
                elif spk_leak(zh, _spk):
                    zh = ""
                # 坏格（结构泄漏 / 日文残留）先退回该行的**初译**。
                if bad_cell(zh, ln["text"]):
                    alt = cleanup(fix_particle((olds[idx] or "").strip(), ln["text"]), stats)
                    if alt and not bad_cell(alt, ln["text"]):
                        zh = alt
                        fallback += 1
                    elif alt:
                        # ⚠️⚠️ 两个都坏 → **也绝不丢行**（2026-10-15 修正）。
                        # 原来是 `continue` 直接丢掉整行，结果用户**永远看不到那句**
                        # （实测 RJ9000010 10. 11:19.26 整行消失）。
                        # 现在取「假名更少」的那个，并标 POLISH_UNVERIFIED 进人工清单。
                        zh = alt if kana_count(alt) < kana_count(zh) else zh
                        unver.append((idx, zh))
                    elif zh:
                        unver.append((idx, zh))
                if not zh:
                    # 只有「润色和初译都为空」才真的没有内容可写
                    jp_drop.append((fmt_lrc(ln["start"]), "（润色与初译皆空）"))
                    continue
                if is_cue:
                    # 阶段 3：成稿中文 → 显示行。**只切分位置，不重写文字**。
                    # R23（坑 106）：LRC 行显示到下一行时间戳为止，渲染端算的
                    # end 不写盘就失效（实测全库 693 行停留 ≥20s、最严重 123.3s）。
                    # spacer_rows 在「本行 end 与下一行 start 有 gap」时补空文本
                    # 清屏行。⚠️ 默认关：全量生效 = 中位提前 2.4s 清屏（超出 C2
                    # 盲听验证过的范围），等用户听辨后再开；存量极端尾由
                    # tmp/daily/fix_lrc_linger.py 外科修复（只修 >语音+8s 的）。
                    _dls = cues.render_display_lines(ln, zh, gloss)
                    _next = (lines[idx + 1]["start"]
                             if idx + 1 < len(lines) else None)
                    if SPACER_ON:
                        _out = cues.spacer_rows(_dls, _next)
                    else:
                        _out = [(d["start"], d["text"]) for d in _dls]
                    # 同一 cue 内的显示行去重（R155）：`render_display_lines`
                    # 偶尔把同一句中文贴到连续两个时间戳上（实测全库 193 处，
                    # RJ377265 一部 99 处）。观众看到的是同一句话连着闪两遍。
                    # 只去 **cue 内** 的重复：跨 cue 去掉会让那个 cue 一行都不剩，
                    # 触发下面 `keep < len(lines)` 的硬门禁。
                    _prev_tx = None
                    _row_ts = [lock_match.parse_ts(fmt_lrc(x[0])) for x in _out]
                    for _t, _tx in _out:
                        if _tx:
                            # E1：句级锁定（人工修正逐字保留，不因重跑丢失）
                            _tx = apply_lock(src, fmt_lrc(_t), _tx, ln.get("text", ""),
                                             cue_idx=idx, row_times=_row_ts)
                        if _tx and _tx == _prev_tx:
                            continue
                        _prev_tx = _tx
                        f.write("%s%s\n" % (fmt_lrc(_t), _tx))
                        if _tx:
                            keep += 1
                        else:
                            n_spacer += 1
                else:
                    # E1：句级锁定（人工修正逐字保留，不因重跑丢失）
                    txt = apply_lock(src, fmt_lrc(ln["start"]), zh, ln.get("text", ""),
                                     cue_idx=idx)
                    f.write("%s%s\n" % (fmt_lrc(ln["start"]), txt))
                    keep += 1
        if unver:
            print("    ⚠️ POLISH_UNVERIFIED %d 行（两个来源都含假名残留，已保留内容）" % len(unver),
                  flush=True)
            for idx, zh in unver[:6]:
                print("       [%s] %s" % (fmt_lrc(lines[idx]["start"]), zh[:44]), flush=True)
        # ⚠️⚠️ 硬门禁（GPT 第六轮 Q5）：**成品行数必须 ≥ 输入 cue 数**。
        # 「丢行」= 用户永远看不到那句，比译错严重得多。宁可留一句带假名的人名。
        # 显示层只会**多切**不会合并，所以 keep < len(lines) 就一定是丢了内容。
        if keep < len(lines):
            lost = len(lines) - keep
            print("    ❌❌ 硬门禁失败：输入 %d 个 cue，只写出 %d 行（**丢了 %d 行**）"
                  % (len(lines), keep, lost), flush=True)
            for t, s in jp_drop[:lost or 5]:
                print("       [%s] %s" % (t, s), flush=True)
            if not a.allow_loss:
                raise SystemExit("丢行不可接受（要放行加 --allow-loss）")
        elif jp_drop:
            print("    ⚠️ jp_drop %d 行（行数未少，说明是显示层内部）" % len(jp_drop), flush=True)
        print("    已写出 %d 行 -> %s%s" % (keep, out,
              "（其中 %d 行退回初译）" % fallback if fallback else ""), flush=True)
        if n_spacer:
            print("    清屏 spacer %d 行（KIKOERU_LRC_END_SPACER=1，读完即走）"
                  % n_spacer, flush=True)
        if LOCK_N[0]:
            print("    句级锁定覆写 %d 行（人工修正已保留%s）"
                  % (LOCK_N[0],
                     "；其中 %d 行靠文本/时间锚定认回（时间戳已漂）" % LOCK_JA_HIT[0]
                     if LOCK_JA_HIT[0] else ""), flush=True)
        if LOCK_AMBIG:
            print("    ⚠️ 锁失配待人工 %d 处（候选分数太接近，不猜）" % len(LOCK_AMBIG),
                  flush=True)
            for f, t in sorted(LOCK_AMBIG)[:5]:
                print("       [歧义] %s|%s" % (f[:36], t), flush=True)
        # 本轨没落点的锁：**不是**「本来就没落地」，而是「这一版找不到它该在哪」。
        # 静默丢人工修正是本项目最贵的故障（beta.15 前 47% 的锁就这么没的），所以必须报。
        _un = sorted(k for k in LOCK_UNMATCHED if k[0] == src)
        if _un:
            print("    ⚠️ 本轨 %d 条锁找不到落点（无 cue 在锚定窗口内）" % len(_un),
                  flush=True)
            for f, t in _un[:5]:
                print("       [无落点] %s|%s" % (f[:36], t), flush=True)
        for ts, t in jp_drop:
            print("    [丢弃日文残留] %s %s" % (ts, t), flush=True)
        for ln, zh in list(zip(lines, results))[:5]:
            print("      %s %s" % (fmt_lrc(ln["start"]), (zh or "").strip()[:60]))

        # ---- E2：缓存落盘（整轨替换，天然防止陈旧条目堆积）----
        # 存 results（润色后、cleanup 前的文本）——下次命中后走同一条 cleanup，
        # 确定性后处理一致。term_repair/term_lock/locks 都在写盘后/写盘时作用，
        # 不影响缓存正确性。
        if _use_cache:
            _trk_new = {}
            for _i in range(len(lines)):
                _v = (results[_i] or "").strip()
                if _v:
                    _trk_new[cue_sig(src, lines[_i], olds[_i], system, _MEM_TXT[0])] = _v
            _sig_cache[src] = _trk_new
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                json.dump(_sig_cache, open(_sig_path, "w", encoding="utf-8"),
                          ensure_ascii=False)
            except Exception as e:
                print("    ⚠️ E2 缓存写盘失败（不影响本次结果）：%s" % e, flush=True)

    # ---- 收尾①：源语驱动的实体/称谓确定性修复（GPT 第八轮 Q4，2026-10-16）----
    # term_lock 只统一「同义变体」；这里处理**初译错译**：
    # 源语 `先生は`（无指示词）被初译成「这位先生」→ 删「这位」并改「老师」。
    # 属于「源语已知实体 → 高置信可确定性修复」的问题，不该留给润色模型猜。
    if not a.no_term_lock:
        try:
            ja_dir = _guess_asr_dir(a.draft_dir)
            if ja_dir:
                cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "term_repair.py"), a.out_dir,
                       "--ja-dir", ja_dir]
                led_path = os.path.join(a.out_dir, "_ledger.json")
                # 账本给锚点（与注入层同源）
                try:
                    import entity_ledger as el
                    led = load_ledger(a.draft_dir)
                    if led:
                        json.dump(led, open(led_path, "w", encoding="utf-8"),
                                  ensure_ascii=False, indent=1)
                        cmd += ["--ledger", led_path]
                except Exception:
                    pass
                print("\n=== 源语驱动称谓修复 ===")
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
                for ln in (r.stdout or "").splitlines():
                    if ln.strip():
                        print("  " + ln)
                if r.returncode != 0 and r.stderr:
                    print("  ⚠️ term_repair 失败：%s" % r.stderr.strip()[:200])
        except Exception as e:
            print("  ⚠️ term_repair 跳过：%s" % e)

    # ---- 收尾②：称谓/术语一致性锁定（确定性后处理，2026-10-16）----
    # 为什么放在最后、而不是靠 prompt：实测「先生→老师」这种**汉语合法词**的
    # 二选一，LLM 每次跑都在摇摆（同一场戏 3 秒内交替），prompt/术语表都挡不住。
    # 只有确定性覆写能保证全篇一致。失败不影响成品（只是少一层保险）。
    if not a.no_term_lock:
        try:
            import term_lock
            ja_dir = _guess_asr_dir(a.draft_dir)
            cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                "term_lock.py"), a.out_dir, "--apply"]
            if ja_dir:
                cmd += ["--ja-dir", ja_dir]
            print("\n=== 称谓一致性锁定 ===")
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            for ln in (r.stdout or "").splitlines():
                if ln.strip():
                    print("  " + ln)
            if r.returncode != 0 and r.stderr:
                print("  ⚠️ term_lock 失败：%s" % r.stderr.strip()[:200])
        except Exception as e:
            print("  ⚠️ term_lock 跳过：%s" % e)

    if _use_cache and _cache_hit_total:
        print("E2 缓存合计命中 %d 条（%s）" % (_cache_hit_total, _sig_path))

    # ---- E1 锁落点账本（2026-10-31 P0）----
    # 「声明 == 实际」的可核对形式：登记的锁有多少条找到了落点、多少条没有。
    # ⚠️ 这里记的是**分配**结果，不是最终成品 —— 后面 term_repair/term_lock 还会改文本。
    # 真正的覆盖度由 06_checks 的 `locks` 门禁拿成品 LRC 重新核（不信这份自述）。
    if LOCK_STAT["tracks"]:
        _doc = {
            "run_id": _RUN_ID[0],
            "window": lock_match.WINDOW,
            "tracks": LOCK_STAT["tracks"],
            "registered": len(LOCK_RAW),
            "assigned": LOCK_STAT["assign"],
            "unmatched": LOCK_STAT["unmatched"],
            "ambiguous": LOCK_STAT["ambiguous"],
            "overwritten_rows": LOCK_N[0],
            "anchored_rows": LOCK_JA_HIT[0],
            "detail": LOCK_STAT["detail"],
            "records": LOCK_STAT["records"],
        }
        json.dump(_doc, open(os.path.join(a.out_dir, "_lock_assign.json"), "w",
                             encoding="utf-8"), ensure_ascii=False, indent=1)
        print("E1 锁：登记 %d / 落点 %d / 无落点 %d / 歧义 %d（明细 _lock_assign.json）"
              % (_doc["registered"], _doc["assigned"], _doc["unmatched"],
                 _doc["ambiguous"]))

    print("\n全部完成 ->", a.out_dir)


if __name__ == "__main__":
    main()
