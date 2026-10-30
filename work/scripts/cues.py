# -*- coding: utf-8 -*-
r"""语义 cue 层 + 中文显示行渲染（GPT 工作框架的阶段 1 / 阶段 3）。

## 为什么要它
旧链路把「日文切行」和「翻译单位」绑成同一个东西：`build_lines()` 切出的**显示行**
直接拿去逐行翻译。于是当一个 ASR 段被词级切点拆成 L1/L2 时，模型在 L1 就提前补全、
把 L2 的内容也翻进去 → **跨行翻译泄漏**（已确认 8 处）。

新链路把两者解耦：

    words[] ──build_semantic_cues()──> Cue[]（翻译单位，语义完整）
                                          │  一 cue 一条唯一中文 T
                                          ▼
                            render_display_lines() ──> 显示行（只切中文，不重译）

**核心契约：翻译/润色只读写 cue；显示行只能由成稿中文切出，不允许再生成或改写文字。**

## 开关
`KIKOERU_CUE_PIPELINE=1` 时启用（默认关，仍走旧的 `build_lines`，可随时回退）。
"""
import os

import knobs
import asr_words
import re
import math

# ---------------- 配置（GPT 框架给的初值，都集中在这里方便调）----------------
# cue 边界
HARD_PAUSE_SEC = 0.75      # 词间停顿 ≥ 此值 → 硬切
SOFT_PAUSE_SEC = 0.30      # 候选切点（保护性切分时才用）
MAX_CUE_SEC = 10.0         # cue 超此长度尝试在候选点切
MAX_CUE_JA_CHARS = 70      # 日文字符上限
MIN_CUE_SEC = 1.0          # 句末标点切分的最低 cue 时长
MIN_CUE_JA_CHARS = 16      # 句末标点切分的最低日文字数
# ⚠️ 为什么句末标点还要配字数下限：只按「句末标点」切，会把「うん。」「そう。」这类
# 短句切成独立 cue，显示行随之变短（实测中位掉到 8 字，人工参考 13.7）。
# 旧 build_lines 用的是 MIN_LINE=16（日文字），这里对齐它。

# 段边界 flush（2026-10-14 加）：Whisper 的 segment 基本是完整话语，当前 cue 已够长就切。
# 不加这条时，一个「结尾没有句末标点」的段会与下一段缝成一条 cue——
# 实测 RJ9000016 0:15 把「はい?じゃなきゃとっくにやめてるでしょ」+「それ、家庭教師じゃないですよね。」
# 缝成一条 7.5 秒、36 字的 cue（一行字幕挂太久）。
# ⚠️ 2026-10-14 消融：**只保留「字数 ≥ SEG_FLUSH_CHARS」一条**，效果最好——
#   12 轨 cue 数 1508(全关) → **1576**(≥16字) → 1670(≥16字 或 ≥3s)。
#   时长条件（≥3s）会让中位日文字从 14 掉到 13（显示行跟着变短），且对修 ⑥ 无额外贡献 → 已去掉。
SEG_FLUSH_ENABLED = True
# 段边界处当前 cue 字数 ≥ 此值 → 切。
# ⚠️ 2026-10-30 从 16 降到 **10**（可用 `KIKOERU_SEG_FLUSH_CHARS` 覆盖）。
# 动机：16 会让「拟声+实词」缝成一条 cue。实例 RJ1530549 轨07 284.08：
#   `リリアも気持ちいいよ`(10字) + `おまんこで`(5) + `上手におちんと`(8) 三段全 <16
#   → 缝成 7.8 秒一条 → Sakura 当一句话翻，整段返回空 → 03 丢行（旧成品有译文）。
# 全库实测（17 部 / 24756 段）：
#   16 → cue 22135、跨 ≥3 段 691(3.1%)、中位 14.0 字
#   12 → cue 22947、跨 ≥3 段 447(1.9%)、中位 14.0 字
#   10 → cue 23479、跨 ≥3 段 330(1.4%)、中位 13.0 字   ← 采用
#    8 → cue 24104、跨 ≥3 段 232(1.0%)、中位 12.0 字
# 取 10 是因为用户报的那条正好 10 字（12 切不开它），代价是中位字数 14→13。
SEG_FLUSH_CHARS = knobs.SEG_FLUSH_CHARS   # 单一真相在 knobs.py（默认 10）
SEG_FLUSH_SEC = None                 # None = 不用时长条件（见上）
# ⚠️ 试过但**没采用**：把段末「句末助词」（でしょ/か/ね/よ…）也算句子结束。
# 想法是「用语言信号代替字数兜底」，但实测 12 轨 cue 数 1559、中位日文字 **13**
# （比纯字数条件的 1557 / 14 更差）——助词在段末太常见，切得更多更碎。故不启用。

# 喘息/碎片串（2026-10-14 加，随后**实测证明不必要，默认关闭**）。
# 当初为修「ん…わかった… お…あ…あ…足… 被并成一条」而加，但消融发现：
# **只要把无词级时间戳的段按标点拆成伪词（synth_pseudo_words），这些呼吸组自己就分开了**
# （伪词让「句末标点切分」能正常工作）。碎片规则额外只多切 19 个 cue，却把中位日文字
# 从 14 压到 12 → 显示行变短。故设 99（=关闭）。留作可调开关。
FRAGMENT_ELLIPSIS_MIN = 99

# 无词级时间戳的段（全库 102/22073 段 ≈ 0.5%）：整段只有一句 text，没有 words[]。
# 不处理的话整段变成**一个不可切 token** → 多句被并成一条巨大 cue → 模型压缩/漏译
# （实测 RJ9000002 t2 0:15 的 34 字 3 句 cue，把「お疲れ様」整个漏掉）。
PSEUDO_MIN_SEC = 0.05

# 显示行
MAX_LINE_SEC = 10.0        # 单行最长停留
MAX_LINE_CHARS = 26        # 单行最多可读汉字
# ⚠️ 这两项是**离线扫参**定的（见 CUE_PILOT_REPORT.md / tmp 里的 sweep）：
# 初值 6.5s 切得太碎，平均字长掉到 8.0（旧管线 10.2、人工 13.7）；
# 真正的元凶其实是 cue 边界过细（句末标点没配字数下限，见 MIN_CUE_JA_CHARS），
# 修掉之后 10.0s/26 字与旧管线持平（中位 9.0）。
MIN_LINE_SEC = 0.9         # 单行最短停留
# 单行最长停留（GPT 第四轮 Q2）。动机：22.9 秒的巨 cue 只按 MAX_LINE_SEC 切 3 行，
# 首行实测挂 11 秒 —— RJ9000014 `03.鬼娘二人…` 用户明确说「换行时间不对」。
# ⚠️ 取 5.5 的依据（2026-10-16 用户盲听裁决，见 listen_hold2/）：
#   C2 听辨包 A(5.5) vs B(8.0) 二选一，用户裁决「抛开翻译问题，长文本换行成几个
#   短文本看着舒服」→ 偏好短停留多行。与人工字幕实测行停留中位 5.53s 一致
#   （learning/RJ403578 唯一存留的人工 LRC，双证据）。
#   切换前 12 轨副作用扫描（tmp/daily/hold_switch_scan.py）：
#     行数 1361→1572（+15.5%）；hold 中位 ~4.4→~3.4s、p90 ~6.4→~5.2s；
#     hold_capped 86→373 行，但被掐行中位仅 9 字、提前消失中位 1.08s（读完即走）；
#     行间空档中位 2.00→1.87s（不增反降）；unsplittable 仅 1 行。
# 扫参历史（GPT 第四轮，12 轨）：
#      阈值   超限行占比   预计新增行
#      5.5s     51%        +415（+25%）
#      6.0s     46%        +326（+20%）
#      7.0s     36%        +231（+14%）
#      8.0s     26%        +168（+10%）← 旧默认，用户盲听后弃用
#      10.0s    16%         +96（+6%）
# ⚠️ MAX_HOLD_SEC 只影响长 cue（短 cue 不受影响，行太碎问题不会回归）。
# 可用 KIKOERU_MAX_HOLD_SEC 覆盖。
MAX_HOLD_SEC = float(os.environ.get("KIKOERU_MAX_HOLD_SEC", "5.5"))
# 自适应停留上限（GPT 第五轮）：`MAX_HOLD_SEC` 是**保护上限**，不是每行的理想时长。
# 真正要避免的是「用户已读完却仍挂在屏幕上」——实测 ASR 段尾会延伸进静音：
#   `おやすみなさい`（晚安）的段跨 **24.7 秒**、`お兄ちゃんの味は酸っぱい味` 跨 **20.6 秒**，
#   切又切不动（只有几个字），于是字幕挂了 20+ 秒。
# 判据（GPT 原话）：
#   短文本 + 长停留 + 无新增语义 → 缩短显示（本函数）
#   长文本 + 连续语义 + 无安全切点 → 允许接近 MAX_HOLD_SEC 并打标
# 读得完需要多久 = 字数 / 目标语速 × 余量；再夹到 [MIN_HOLD_SEC, MAX_HOLD_SEC]。
MIN_HOLD_SEC = 2.0         # 再短的字幕也至少显示这么久
HOLD_SLACK = 2.2           # 阅读余量（ASMR 语速慢、有拖音，给宽一点）
TARGET_CPS = 4.5           # 目标阅读速度（字/秒）
MAX_CPS = 6.0              # 超过就考虑拆
MIN_CJK_CHARS = 4          # 单侧最少可读汉字（完整短应答例外）


def hold_cap(text):
    """一行中文「读得完」需要的显示时长（秒），夹在 [MIN_HOLD_SEC, MAX_HOLD_SEC]。

    与 cap_holds 同判据（2026-10-15 GPT 第五轮）：读得完就不该继续挂。
    ⚠️ 2026-10-23 R23 发现：这个 cap 从来没有真正生效过——LRC 写盘只写
    start（polish.py 输出阶段），行实际停留 = 下一行时间戳 - 本行 start，
    cap_holds 设的 end 被静音 gap 覆盖（全库实测 693 行停留 ≥20s、最严重
    123.3s）。提取成模块级函数供 writer / 修复脚本 / scan 共用。
    """
    n = cjk_len(text) or len(text or "")
    return min(MAX_HOLD_SEC, max(MIN_HOLD_SEC, n / TARGET_CPS * HOLD_SLACK))

ENABLED = os.environ.get("KIKOERU_CUE_PIPELINE", "0") not in ("0", "", "false", "False")

BREAK = "。！？!?…、"          # 与 polish.BREAK 一致
EOS = "。！？!?…"             # 句末标点（可结束 cue）

# ---- F7 候选切点：敬体句尾（2026-10-21，默认关闭，见 docs/archive/20261026/F7_SENTENCE_RULES_20261021.md）----
# 动机：ASMR 的 ASR 输出常整段无标点，我们只能等 0.75s 硬停顿/10s 上限 → 一条 cue 塞两三句。
# WhisperJAV 的做法是「敬体句尾无条件切」，但那会切出碎片；这里**只当候选点**，
# 仍受 MIN_CUE_JA_CHARS / MIN_CUE_SEC 门槛约束（复用现有纪律）。
# 开启：KIKOERU_CUE_POLITE_SPLIT=1
POLITE_SPLIT = os.environ.get("KIKOERU_CUE_POLITE_SPLIT", "0") == "1"
POLITE_ENDINGS = ("ございます", "でしょう", "ましょう", "ください", "でした",
                  "ました", "ません", "です", "ます")
# 后面紧跟这些=连接用法/附着成分，不是句尾（ですが/ますが/ですから/…に/…を…）
# ⚠️ 2026-10-21 精化：v1 只排了 が/から/けど 这类连接词，切在「です＋助词」处
#    （如「…です の」）会把短语切断 → 译文留假名（实测 3 轨 1–3 处）。
# ⚠️⚠️ 2026-10-22 v3 重大修正：whisper 的日语**子词分词**把附着在敬体后面的
#    助词/助动词切到下一个 token（「ます|ね」「です|か」「ください|ませ」
#    「でした|ら」）——v1 的 +12.2% cue 收益大部分是切在短语中间换来的，
#    这正是 e2e 测试里 3 轨日文残留的来源。v3 把终助词/附着助动词全部纳入排除，
#    切点改到「敬体+终助词」**整体之后**（见下方 POLITE_FINAL_PARTICLES 分支）。
POLITE_EXCL = ("が", "から", "けど", "けれど", "し", "ので", "のに", "なら",
               "って", "と", "ば", "なのに", "ですけど", "ますけど", "ですが",
               # 助词/继续成分（v2 新增）
               "の", "を", "に", "は", "も", "で", "へ", "や", "て",
               "ながら", "つつ", "たり", "だり", "ものの", "くせに",
               # 终助词/附着助动词（v3 新增）：它们是敬体句子的一部分，不是下一句开头
               "ね", "よ", "な", "わ", "ぞ", "ぜ", "か", "ら", "ませ", "さ",
               "かい", "かな", "かしら", "よね", "のね", "なあ", "ねえ",
               "よー", "わー", "ってば", "っけ", "ど")
# 敬体+终助词是一个整体：「…ですね｜次の文…」的切点在 ね/よ/な 之后。
# 本词是裸终助词且上一词以敬体收尾 → 允许在此切（仍受 16 字门槛约束）。
POLITE_FINAL_PARTICLES = ("ね", "よ", "な", "わ", "さ", "ぞ", "ぜ")
# 停顿门槛：默认 **0（关闭）**——whisper 词级时间戳在段内是连续的，段内 gap 恒 ≈0，
# 用它当判据会把功能整体废掉（实测 polite_end 22→2）。保留开关供「段间」场景试。
POLITE_MIN_GAP = float(os.environ.get("KIKOERU_CUE_POLITE_GAP", "0"))
# 敬体切分后：尾巴 cue 字数 < 此值 → 并回前一条（0=关闭）
POLITE_MERGE_MIN = int(os.environ.get("KIKOERU_CUE_POLITE_MERGE_MIN", "8"))
SPLIT_AFTER = "。！？…；、，"   # 允许切在中文这些字符之后
SPLIT_AFTER_SPACE = " "        # 半角空格后也可切
# 句末标点优先于逗号/顿号（2026-10-30 修「回吐」）。
# 旧写法「吸附到最近合法切点」会把下一句的首词留在上一行：
#   「晚安。不行，老师可能会来巡视。」 15 字切 2 行，理想点 7.5，
#   最近的合法切点是逗号后（6），于是切成「晚安。不行」/「老师可能会来巡视。」
# 现在给非句末切点加 CHAR 级惩罚：句末标点哪怕远 4 个字也优先。
SPLIT_SENT_END = "。！？…"
SPLIT_SENT_BONUS = 4
BAD_BEFORE = "的地得了着过"     # 切点前是这些 → 禁止
BAD_AFTER = "的地得了着过"      # 切点后是这些 → 禁止
NEG_CHARS = "不没别未否莫免无勿"
OPEN_BRACKETS = "（(「『【[《"
CLOSE_BRACKETS = "）)」』】]》"
UNITS = "次回个秒分时天年月日人点本枚杯匹只件套张岁"

CJK = re.compile(r"[\u4e00-\u9fff]")
DIGIT = re.compile(r"\d")


def cjk_len(s):
    return len(CJK.findall(s or ""))


def _join(words):
    """段边界插空格做提示（与 polish.build_lines 的 flush 一致）。"""
    buf = []
    for k, x in enumerate(words):
        if k and x.get("seg") != words[k - 1].get("seg"):
            buf.append(" ")
        buf.append(x["w"])
    return "".join(buf).strip()


def _last_soft_break(words, min_left=2):
    """从末尾往回找候选切点，返回下标 i（words[i:] 归下一段）。

    优先**段边界**（Whisper 的 segment 基本是完整话语，切在这里最安全），
    其次找「停顿 ≥ SOFT_PAUSE_SEC」的词间空隙。都找不到返回 None。
    ⚠️ 绝不按「助词/接续词」猜——日文词 token 是子词级的，必然误伤（见 polish.py 的教训）。
    """
    for i in range(len(words) - 1, min_left - 1, -1):
        if words[i].get("seg") != words[i - 1].get("seg"):
            return i
    for i in range(len(words) - 1, min_left - 1, -1):
        if words[i]["s"] - words[i - 1]["e"] >= SOFT_PAUSE_SEC:
            return i
    return None


# 伪词切分：保留句末标点在前一片（`…。` 归上一片），空片丢弃。
_PSEUDO_RE = re.compile(r"[^。！？!?…]*[。！？!?…]+|[^。！？!?…]+")


def synth_pseudo_words(seg, si):
    """段没有词级时间戳时，把整段 text 按句末标点切成**伪词**，时间按字数比例分摊。

    这样下游的「句末标点切分」和「段边界切分」都能正常工作，
    不会整段变成一个不可切的大 token。
    """
    text = (seg.get("text") or "").strip()
    if not text:
        return []
    parts = [p for p in _PSEUDO_RE.findall(text) if p.strip()]
    if not parts:
        return []
    total = sum(len(p) for p in parts) or 1
    t0 = float(seg["start"])
    span = max(0.0, float(seg["end"]) - t0)
    out, cur = [], t0
    for p in parts:
        d = span * len(p) / total
        out.append({"s": round(cur, 3), "e": round(cur + max(d, PSEUDO_MIN_SEC), 3),
                    "w": p, "spk": seg.get("speaker", ""), "seg": si})
        cur += d
    return out


def flatten_for_cues(segments):
    """cue 管线专用词流：有词级时间戳用词级，没有的按标点合成伪词。

    （不动 polish.flatten_words，是为了不扰动旧管线的对照基线。）
    """
    out = []
    for si, seg in enumerate(segments):
        ws = seg.get("words")
        # ⚠️ 安全网（2026-10-28 R154）：words 拼起来和 text 不一致 = 上游改过 text
        #    却没同步 words（01 的拉丁清理 / 02 的 errata / 人工落地）。
        #    这时 words 是**过期**的，用它翻译等于错字表完全失效。
        #    宁可丢掉词级时间戳（cue 边界略粗），也不能拿过期文本去译。
        if ws and not asr_words.consistent(seg):
            ws = None
        if ws:
            out.extend({"s": float(w["s"]), "e": float(w["e"]), "w": w["w"],
                        "spk": seg.get("speaker", ""), "seg": si} for w in ws)
        elif (seg.get("text") or "").strip():
            out.extend(synth_pseudo_words(seg, si))
    out.sort(key=lambda x: x["s"])
    return out


def _is_fragment_run(words):
    """碎片串判定：只用「…」密度（子词级 token 不能用平均长度判，见配置注释）。"""
    t = "".join(x["w"] for x in words)
    return t.count("…") >= FRAGMENT_ELLIPSIS_MIN


def build_semantic_cues(words):
    """词流 → 语义 cue。每个词恰好属于一个 cue，时间递增、不重叠。

    边界优先级（GPT 框架 + 2026-10-14 补丁）：
      1 硬切：说话人变化 / 停顿 ≥ HARD_PAUSE_SEC
      1b 段边界：当前 cue 已够长（≥ SEG_FLUSH_CHARS 或 ≥ SEG_FLUSH_SEC）→ 切
      2 优先切：句末标点（且 cue 已 ≥ MIN_CUE_SEC 与 MIN_CUE_JA_CHARS；
                碎片串豁免字数下限）
      3 候选切：停顿 ≥ SOFT_PAUSE_SEC（只在超限时用）
      4 保护性切：cue 超 MAX_CUE_SEC / MAX_CUE_JA_CHARS → 退到最近候选点
      5 兜底：整段都跨不过安全边界 → **保留完整段**，标 max_guard_unsplittable
    """
    cues, cur = [], []

    def flush(reason):
        if not cur:
            return
        t = _join(cur)
        if t:
            dur = cur[-1]["e"] - cur[0]["s"]
            r = reason
            if dur > MAX_CUE_SEC:
                r = "max_guard_unsplittable"
            cues.append({
                "start": round(cur[0]["s"], 3), "end": round(cur[-1]["e"], 3),
                "text": t, "spk": cur[-1].get("spk", ""),
                "words": [{"text": x["w"], "start": x["s"], "end": x["e"],
                           "seg": x.get("seg")} for x in cur],
                "split_reason": r,
            })
        cur.clear()

    def nchars():
        return sum(len(x["w"].strip()) for x in cur)

    for wi, w in enumerate(words):
        if cur:
            gap = w["s"] - cur[-1]["e"]
            if w.get("spk", "") != cur[-1].get("spk", ""):
                flush("speaker")
            elif gap >= HARD_PAUSE_SEC:
                flush("pause")
            elif (SEG_FLUSH_ENABLED and w.get("seg") != cur[-1].get("seg")
                  and (cur[-1]["w"].strip()[-1:] in EOS
                       or nchars() >= SEG_FLUSH_CHARS
                       or (SEG_FLUSH_SEC and cur[-1]["e"] - cur[0]["s"] >= SEG_FLUSH_SEC))):
                # 段边界：Whisper 段≈完整话语。满足其一就切：
                #   · 当前 cue 已经以句末标点收尾（最强信号，切了绝不错）
                #   · 或已够长（字数/时长）
                # 免得「结尾无句末标点的段」与下一段缝成一条（RJ9000016 0:15），
                # 或「短句+下一段」被缝成巨 cue（4 处 max_guard 残留的成因）。
                flush("segment")
        cur.append(w)
        n = nchars()
        dur = cur[-1]["e"] - cur[0]["s"]
        tail = w["w"].strip()
        frag = _is_fragment_run(cur)
        if tail and tail[-1] in EOS and dur >= MIN_CUE_SEC and (n >= MIN_CUE_JA_CHARS or frag):
            flush("fragment" if frag and n < MIN_CUE_JA_CHARS else "sentence")
        elif (POLITE_SPLIT and not frag and n >= MIN_CUE_JA_CHARS and dur >= MIN_CUE_SEC
              and (any(tail.endswith(e) for e in POLITE_ENDINGS)
                   or (tail in POLITE_FINAL_PARTICLES and wi > 0
                       and any(words[wi - 1]["w"].strip().endswith(e)
                               for e in POLITE_ENDINGS)))):
            # 敬体句尾当候选切点：① 下一词是连接/继续成分 → 不切；② 句尾后无小停顿 → 不切
            # v3：切点两种形态——
            #   a) 本词以敬体收尾（…です｜…），下一词不能是附着成分（v3 排除表）；
            #   b) 本词是裸终助词且上一词以敬体收尾（…ですね｜…），终助词归属前句。
            nxt_w = words[wi + 1] if wi + 1 < len(words) else None
            nxt = nxt_w["w"].strip() if nxt_w else ""
            gap_next = (nxt_w["s"] - w["e"]) if nxt_w else 99.0
            if not (nxt and any(nxt.startswith(x) for x in POLITE_EXCL)) \
                    and gap_next >= POLITE_MIN_GAP:
                flush("polite_end")
        elif dur >= MAX_CUE_SEC or n >= MAX_CUE_JA_CHARS:
            i = _last_soft_break(cur)
            if i is not None:
                carry = cur[i:]
                del cur[i:]
                flush("soft_pause")
                cur.extend(carry)
    flush("end")
    # 敬体切分后的短尾回并：切点留下的「尾巴 cue」字数过少 → 并回前一条
    # （否则翻译面对缺主语的碎片会留假名；WhisperJAV 的 _merge_tiny_fragments 同思路）
    if POLITE_SPLIT and POLITE_MERGE_MIN > 0 and len(cues) > 1:
        merged = [cues[0]]
        for c in cues[1:]:
            if (merged[-1]["split_reason"] == "polite_end"
                    and cjk_len(c["text"]) < POLITE_MERGE_MIN
                    and c["end"] - merged[-1]["start"] <= MAX_CUE_SEC):
                prev = merged[-1]
                prev["text"] += c["text"]
                prev["end"] = c["end"]
                prev["words"] += c["words"]
                prev["split_reason"] = "polite_end_merged"
            else:
                merged.append(c)
        cues = merged

    # 强制时间单调：ASR 偶尔有「同一段音频被两个窗各识别一次」的重叠段（实测 RJ9000007 04_ 等 3 处）。
    # 这里只把前一段的 end 收到后一段的 start，**不删内容**（内容层面的重复由扫描器另行告警）。
    for i in range(len(cues) - 1):
        if cues[i]["end"] > cues[i + 1]["start"]:
            cues[i]["end"] = cues[i + 1]["start"]
            cues[i]["split_reason"] += "+overlap_trimmed"

    for k, c in enumerate(cues):
        c["cue_id"] = "c%04d" % k
    return cues


# ---------------- 中文显示切分 ----------------
def split_points(zh, glossary=None):
    """返回**允许**切分的位置下标集合（1 <= i < len(zh)）。规则引擎优先。

    ⚠️ 判据要用切点**两侧的非空格字符**——切点常常落在空格上
    （「这个 3 次就好了」的切点在下标 5，是「3」和「次」之间的空格），
    只看 `zh[i-1]` 会漏掉「数字+单位」这类规则。
    """
    ok = []
    terms = [t for t in (glossary or {}) if t]

    def prev_ns(i):
        j = i - 1
        while j >= 0 and zh[j] == " ":
            j -= 1
        return zh[j] if j >= 0 else ""

    def next_ns(i):
        j = i
        while j < len(zh) and zh[j] == " ":
            j += 1
        return zh[j] if j < len(zh) else ""

    for i in range(1, len(zh)):
        # 允许的位置：句末标点 / 分号顿号逗号 / 半角空格之后
        if not (zh[i - 1] in SPLIT_AFTER or zh[i - 1] in SPLIT_AFTER_SPACE):
            continue
        pl, nr = prev_ns(i), next_ns(i)
        if not pl or not nr:
            continue
        # 禁止：切点前后是「的地得了着过」
        if pl in BAD_BEFORE or nr in BAD_AFTER:
            continue
        # 禁止：否定词与谓词之间
        if pl in NEG_CHARS:
            continue
        # 禁止：数字与单位之间
        if DIGIT.search(pl) and nr in UNITS:
            continue
        # 禁止：括号 / 引号内部
        seg = zh[:i]
        if sum(seg.count(c) for c in OPEN_BRACKETS) > sum(seg.count(c) for c in CLOSE_BRACKETS):
            continue
        # 禁止：术语词典项内部
        if any((zh.find(t) < i < zh.find(t) + len(t)) for t in terms if t in zh):
            continue
        # 禁止：任一侧不足 MIN_CJK_CHARS 个可读汉字
        # ⚠️ **句末标点后豁免**（2026-10-30）：`晚安。不行，老师可能会来巡视。` 的 `。` 后
        # 只有 2 个汉字，被这条挡掉 → 全串只剩一个合法切点（逗号后 6），于是切成
        # 「晚安。不行」/「老师可能会来巡视。」——下一句的首词被留在上一行（「回吐」）。
        # 完整句独立成行是可接受的（「晚安。」本来就该单独一行）；时长仍受 MIN_LINE_SEC
        # 约束，`build()` 会把停留过短的行并回下一段。
        if zh[i - 1] not in SPLIT_SENT_END:
            if cjk_len(zh[:i]) < MIN_CJK_CHARS or cjk_len(zh[i:]) < MIN_CJK_CHARS:
                continue
        # 空格切点：相邻「词」不能是单字。
        # 呻吟/结巴常写成「不 要 停 下 来」这种单字串，按空格切会切碎语义；
        # 而正常译文里空格分隔的是短语（「不要 不要这样」），两侧至少有一侧 ≥2 字。
        if zh[i - 1] in SPLIT_AFTER_SPACE:
            lt = re.search(r"[^ ]+$", zh[:i])
            rt = re.match(r"[^ ]+", zh[i:])
            if not ((lt and cjk_len(lt.group()) >= 2) or (rt and cjk_len(rt.group()) >= 2)):
                continue
        ok.append(i)
    return ok


def _split_by_points(zh, pts, k):
    """把 zh 按字符数均分成 k 段，边界吸附到最近的**合法切点**。

    ⚠️ 吸附时**句末标点优先**（2026-10-30）：给非句末切点加 `SPLIT_SENT_BONUS` 字符的
    惩罚，否则会把下一句首词留在上一行（「晚安。不行」/「老师可能会来巡视。」）。
    惩罚是字符级的，句末标点离理想点远 4 字以内都仍然胜出；远太多时照样用逗号。
    """
    total = len(zh)
    cuts = []
    for j in range(1, k):
        ideal = total * j / k
        if not pts:
            break
        def cost(p):
            return abs(p - ideal) + (0 if zh[p - 1] in SPLIT_SENT_END else SPLIT_SENT_BONUS)
        best = min(pts, key=cost)
        if best not in cuts:
            cuts.append(best)
    cuts.sort()
    parts, prev = [], 0
    for c in cuts:
        parts.append(zh[prev:c])
        prev = c
    parts.append(zh[prev:])
    return [p.strip() for p in parts if p.strip()]


def render_display_lines(cue, zh, glossary=None):
    """成稿中文 → 显示行。**只切不重写**：拼接回来必须等于 zh。

    拆行触发：显示时长 > MAX_LINE_SEC **或** 可读字符 > MAX_LINE_CHARS **或**
    单行停留会超过 MAX_HOLD_SEC。行数取三者所需的最大值。

    ⚠️ 时间分配改成**累计比例一次性算完**（GPT 第四轮 Q2）：
    旧写法「d = dur * n[j]/tot，逐段累加」会把舍入误差全部堆到最后一行
    （22.9 秒的 cue 切 3 行，末行实测挂到 7.6 秒）。
    现在是 `line_end_i = start + dur * 前 i 行累计字数 / 总字数`。
    ⚠️ **没有安全切点时不硬切**：保留整行并打 `overlong_display_unsplittable`，
    宁可一行挂久，也不切进固定搭配、不把两句塞进同一行。
    """
    zh = (zh or "").strip()
    if not zh:
        return []
    dur = max(0.01, cue["end"] - cue["start"])
    nch = cjk_len(zh) or len(zh)
    cid = cue.get("cue_id")
    k_sec = int(math.ceil(dur / MAX_LINE_SEC))
    k_chr = int(math.ceil(nch / MAX_LINE_CHARS))
    k_hold = int(math.ceil(dur / MAX_HOLD_SEC))
    k = max(k_sec, k_chr, k_hold)
    if k <= 1:
        return [{"start": cue["start"], "end": cue["end"], "text": zh, "cue_id": cid}]

    pts = split_points(zh, glossary)
    cid = cue.get("cue_id")

    def build(parts):
        """累计比例分配时间 + 合并过短行。只切不重写（拼接等于 zh）。"""
        n = [max(1, cjk_len(p) or len(p)) for p in parts]
        cum, acc = [], 0
        for x in n:
            acc += x
            cum.append(acc)
        tot = cum[-1] or 1
        ends = [cue["end"] if j == len(parts) - 1
                else round(cue["start"] + dur * cum[j] / tot, 3)
                for j in range(len(parts))]
        out, t, buf = [], cue["start"], ""
        for j, p in enumerate(parts):
            buf = p if not buf else buf + p
            if j < len(parts) - 1 and ends[j] - t < MIN_LINE_SEC:
                continue                       # 太短 → 并入下一段
            e = cue["end"] if j == len(parts) - 1 else ends[j]
            out.append({"start": round(t, 3), "end": round(e, 3),
                        "text": buf, "cue_id": cid})
            t, buf = e, ""
        return out

    # k 逐级加码，直到**每一行**都不超过 MAX_HOLD_SEC。
    # 只按 dur/MAX_HOLD_SEC 定 k 不够：累计比例会把长文本那行分到更长时间，
    # 实测 22.9s 的 cue 切 5 行，首行仍挂 7.6s。
    lines = []
    if pts:
        for kk in range(k, min(len(zh), k + 12) + 1):
            cand = _split_by_points(zh, pts, kk)
            if len(cand) <= 1:
                break
            lines = build(cand)
            if len(lines) <= 1:
                continue
            if max(x["end"] - x["start"] for x in lines) <= MAX_HOLD_SEC + 0.05:
                break
    def cap_holds(ls):
        """自适应停留上限：读得完就不该继续挂。

        判据（GPT 第五轮）：`MAX_HOLD_SEC` 是**保护上限**、不是理想时长。
        ASR 段尾常延伸进静音（实测「晚安」的段跨 24.7 秒），切又切不动
        （只有几个字），只能**缩短显示**——否则用户读完还挂 20 多秒。
        ⚠️ 2026-10-23 R23：缩短后的 end 必须在**写盘层**落实（spacer 清屏行），
        否则 LRC 语义（行显示到下一行时间戳）会把它整条吃掉——见 `spacer_rows`。
        """
        for x in ls:
            cap = hold_cap(x["text"])
            if x["end"] - x["start"] > cap + 0.05:
                x["end"] = round(x["start"] + cap, 3)
                x["flag"] = "hold_capped"
        return ls

    if len(lines) <= 1:
        ln = {"start": cue["start"], "end": cue["end"], "text": zh, "cue_id": cid}
        if dur > MAX_HOLD_SEC or k_chr > 1:
            ln["flag"] = "overlong_display_unsplittable"
        return cap_holds([ln])
    # 仍超限的行单独打标记：说明这里没有更靠前的合法切点（不是没切，是切不动）
    for x in lines:
        if x["end"] - x["start"] > MAX_HOLD_SEC + 0.05:
            x["flag"] = "display_over_hold"
    return cap_holds(lines)


def cue_stats(cues):
    from collections import Counter
    c = Counter(x["split_reason"] for x in cues)
    return {"cues": len(cues), "by_reason": dict(c),
            "max_sec": max((x["end"] - x["start"] for x in cues), default=0),
            "unsplittable": c.get("max_guard_unsplittable", 0)}


# ---- LRC 清屏 spacer（2026-10-23 R23，坑 106）----
# LRC 语义：一行显示到**下一行时间戳**为止。render_display_lines 算的 end
# （cap_holds 的「读完即走」）不写进 LRC 就失效——全库实测 693 行停留 ≥20s、
# 最严重 123.3s（用户 10-15 投诉的 A1–A7 同类）。修法：本行 end 与下一行
# start 之间有 gap 时，补一条**空文本 spacer**，播放器读空行即清屏；
# 不支持清屏的播放器行为与现在完全一致（无回退风险）。
SPACER_EPS = 0.3           # gap 小于此值不补（自然段间停顿）


def spacer_rows(lines, next_start=None, eps=SPACER_EPS):
    """display lines → LRC 行 [(time, text)]，按 end 补空文本清屏行。

    `lines`：render_display_lines 的输出（每行带 start/end/text）。
    `next_start`：下一个 cue 的起始时间（本 cue 末行的比较对象；None = 轨尾）。
    同一 cue 内的行首尾相接（build() 保证），只有**末行**可能产生 spacer。
    """
    rows = []
    for j, dl in enumerate(lines):
        rows.append((dl["start"], dl["text"]))
        nxt = lines[j + 1]["start"] if j + 1 < len(lines) else next_start
        end = dl.get("end")
        if nxt is not None and end is not None and nxt - end > eps:
            rows.append((round(end, 3), ""))
    return rows


def build_units(segments, legacy_fn):
    """按开关返回**翻译单位**：cue（新）或显示行（旧）。

    返回 (units, is_cue)。两种单位都有 start/end/text/spk 四个键，
    所以下游（translate/polish）除输出阶段外无需分叉。
    """
    if not ENABLED:
        return legacy_fn(segments), False
    return build_semantic_cues(flatten_for_cues(segments)), True
