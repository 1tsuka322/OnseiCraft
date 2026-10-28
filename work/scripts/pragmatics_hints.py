# -*- coding: utf-8 -*-
"""pragmatics_hints.py —— 参与者 / 话语行为账本（GPT 第九轮 Q1 主力方案 B）

定位：实体账本（entity_ledger.py）解决「谁叫什么」；本模块解决
「谁在说、说的是谁、是否在等听者回应」。**轻量**：只做 cue 级表层标记扫描，
产出可审计的 hint（标签 + 证据 + 置信度），不做任何语义理解。

## 设计口径（GPT 第九轮答复，逐条遵守）

- **hint 不是「事实锁」**：只有高置信、明确的词面证据才锁定；中低置信
  只提供给模型与告警器，不当硬规则。
- **句尾助词不直接映射成固定人称**：
  · `あんた / 君 / お前`：强听者侧信号（高置信）
  · `そっち`：中信号——可能是「你那边」，也可能仅是方位
  · `ってば`：强烈断言/反驳信号，但**不自行决定我或你**
  · `んだ / のだ`：解释、确认、发现的语气，不能改成「你……吧」
  · `っけ`：回忆式疑问
  · `でしょ / じゃん / ね`：寻求共识、预设听者参与，但不总是疑问句
- 反面教材（已核实）：T2「你看到了吧」正确，核心不是 `んだ`，而是
  `そっち + 見た` 指向听者经历；T3「都说了我不会唱」核心是
  `歌ってない + ってば` 结合争辩语境——**仅靠单句形态不能保证判对**，
  所以 hint 只作提示，配合上下文与后检测。

## 输出形态（GPT 给的 JSON 形状）

```json
{
  "participants": {"speaker": "CV", "listener": "user"},
  "cue_hints": {
    "c042": {"explicit_listener_marker": ["そっち"],
             "act": "listener_experience_confirmation", "confidence": "medium"},
    "c043": {"act": "speaker_denial", "evidence": ["ない", "ってば"],
             "confidence": "medium"}
  }
}
```

## 后检测（GPT 指定的两类高优先级 + 一条中优先级）

  规则 1（高）：ja 含 `あんた/君/お前`，中文却无听者指向（你/妳/对方/您）
              → 疑似「听者侧标记未渲染」
  规则 2（高）：ja 明显否定 + `ってば`，中文却变疑问/无否定表达
              → 疑似「否定极性翻反」
  规则 3（中）：ja 含 `そっち`，中文无「你/那边」等对应
              → 只告警，不做硬规则（句级共现不稳定，语料统计 38%）

⚠️ 召回有上限是设计使然：极性翻反但没有 `ってば` 的句子（如
`ふーってしてあげない`）两条规则都抓不到——那是 hint 层 + 上下文层的事，
后检测只拦「有明确词面冲突」的。
"""
import os
import re

# ---------------- 标记表（全部来自 GPT 答复 + 本项目语料统计） ----------------

# 强听者侧（高置信）：出现即几乎必然指向听者（语料：あんた→你 86%）
LISTENER_STRONG = ("あんた", "君", "お前")
# 中信号：可能「你那边」也可能纯方位（语料句级含「你」仅 38%）
LISTENER_MEDIUM = ("そっち",)

# ⚠️⚠️ 2026-10-17 全量 133 轨实测后加的**排除规则**（v1 → v2 修正）
# v1 直接把 君/お前 当初人称，实测大量误报：
#   · `チン君` / `ギクロンビ君` / `ウォーマン君` / `ドM君` / `庭の君` —— 是**给东西起的名字**
#     或「…君（くん）敬称」的复合名词，不是第二人称
#   · `僕のお前に` —— お前 同形异义，这里是「（身体）前面」
#   · `そっちがローター` / `そっちが悪い` / `のけそっちゃいます` —— そっち 是「那个/那边」，
#     或只是「のけぞる」里的字面巧合
# 所以：**只有「独立成词」的才算是人称**。判据 = 前一个字不是汉字/片假名（名词修饰）
# 且不在排除组合里。这个方向与项目一贯口径「宁可漏判不可误判」一致。
RE_KANJI_OR_KATA = re.compile(r"[\u4e00-\u9fff\u30a0-\u30ffA-Za-z0-9]")
# 名词修饰的助词/结尾（`庭の君` / `お前との` / `チン君` 的「ン」已属片假名）
MODIFIER_TAIL = "のとがはにでをやかなも"
# お前 的非人称组合（同形异义）
# ⚠️ 2026-10-17 影子模式实测修正（GPT 第十轮 Q4）：
#    原来把 "お前と" / "もお前" / "のお前" 整条排除，三条都开得太宽：
#      · `お前との時間` → 「和你」的**第二人称**（被 "お前と" 误杀）
#      · `私もお前のこと好き` → 「も」是「也」，不是同形异义（被 "もお前" 误杀）
#      · `変態のお前が` → 形容词修饰的**第二人称**（被 "のお前" 误杀）
#    真正该排除的只有「所有格 + 方位」：`僕のお前` = 我的（身体）前面。
#    判据收敛为：**前接 の 的所有格 + 后接方位/身体语义**才排除；
#    以及紧接的方位词（お前の方 / お前側）。
OWNED_ONMAE = re.compile(r"[僕俺私わたし]のお前")
POSITIONAL_AFTER_ONMAE = ("の方", "の側", "側面", "の向き", "の辺", "のあたり")
# 复合动词里的字面巧合（のけぞる / のけそる）：そっち 不是指示词
SOCCHI_FALSE_FRIENDS = ("のけそっ", "のけぞっ", "ぬけそっ")
# 君 作「敬称后缀 / 拟人名」时：前面是片假名、汉字、或非句首
KIMI_SUFFIX_CTX = ("君", "くん")
# ⚠️ 2026-10-17 影子模式（GPT 第十轮 Q4）第 2 轮发现：
#    `あんた` 的判据「前接实词 → 复合词」没做分词，遇到副词尾就误杀：
#      `多分あんたのおかげ`（多分＝大概，あんた 是独立第二人称）被静音 → 「你的功劳」漏检。
#    → 前接这些副词/接续词时，一律不算复合词。
ANTA_ADVERB_TAIL = ("多分", "本当", "全部", "絶対", "全然", "結局", "やっぱり",
                    "たぶん", "ほんと", "ぜんぜん")


def _person_occurrences(marker, ja):
    """返回 marker 在本句里的**人称用法**出现（近似）。

    非人称判据（实测误报源）：
      · `君`：前面紧跟**汉字/片假名/ASCII** → 复合名词（チン君 / ウォーマン君 / ドM君 / 庭の君）
      · `お前`：所有格方位（僕のお前）/ 后接方位词 → 同形异义
      · `あんた`：几乎无同形异义，只要求前面不是片假名（アンタ 单独出现另算）

    ⚠️⚠️ 2026-10-17 影子模式（GPT 第十轮 Q4）发现的**大范围漏判**：
      原判据写成「前一字是汉字/片假名 **或** 助词 MODIFIER_TAIL」，
      而 MODIFIER_TAIL 含 `はがをにもとでやな` —— 于是 `君は` `君の` `君を` `君に`
      `君も` `君と` `君で` `君が` **全被当成名词修饰排除了**。
      实测 34 条 s2 里有 27 条是这种误排除（`君は大丈夫なわけ?` → 「你没问题吗？」被静音）。
      → 修正：只有「前一字是实词/敬称后缀」(RE_KANJI_OR_KATA) 才算名词修饰；
        **助词前面的 君 是标准的第二人称主语/宾语**。`庭の君` 靠「の」+「庭」单独判。
    """
    out = []
    start = 0
    while True:
        k = ja.find(marker, start)
        if k < 0:
            break
        start = k + len(marker)
        prev = ja[k - 1] if k > 0 else ""
        prev2 = ja[k - 2] if k > 1 else ""
        # 名词修饰（只有这两种真会误判）：
        #  a) 前接实词（汉字/片假名/ASCII）—— チン君 / ドM君 / ウォーマン君
        #  b) `庭の君` 这类「实词 + の + 君」（の 本身是助词，要看它前面）
        noun_mod = bool(prev) and bool(RE_KANJI_OR_KATA.match(prev))
        if prev == "の" and prev2 and RE_KANJI_OR_KATA.match(prev2) \
                and marker in KIMI_SUFFIX_CTX:
            # `庭の君` = 复合名词；但 `今の君の課題` 是「**你**现在的课题」——
            # 判据：若 の 后面紧跟「君の/君は/君が…」（君 自己带助词），则是人称。
            after = ja[start:start + 1]
            if after == "の" or after in "はがをにものとでやかな":
                noun_mod = False           # 今の君の… / あの君は…
            else:
                noun_mod = True            # 庭の君みたいに
        if marker in KIMI_SUFFIX_CTX and noun_mod:
            continue                       # チン君 / ドM君 / 庭の君
        if marker == "お前" and OWNED_ONMAE.search(ja[:start] + marker):
            continue                       # 僕のお前 = 我的（身体）前面
        if marker == "お前" and any(ja[start:start + len(p)] == p
                                    for p in POSITIONAL_AFTER_ONMAE):
            continue                       # お前の方 / お前側 = 身体前面（方位）
        if marker == "あんた" and prev and RE_KANJI_OR_KATA.match(prev):
            # 前接实词 → 复合词（アンタ君 之类）；但 `多分あんたの` 是独立第二人称
            if not any(ja[:k].endswith(w) for w in ANTA_ADVERB_TAIL):
                continue
        out.append(marker)
    return out


# ⚠️ 影子样本 #3（2026-10-18，盲听抽检发现）：`そっち` 整条撤除后仍有**主语翻转**漏网——
#    `そっちもよく聞こえる` → 译成「**我这边**也听得很清楚」（そっち 指听者，被译成说话人）。
#    这类不能用「そっち 一律算人称」修（语料抽检指人仅 37%，会大面积误报），
#    只认**强模式**：そっち + 听者才做的动作（听得见/看得见/带过来/继续/站着）。
#    这是「宁漏不错」下的最小补丁：只覆盖 6 个动词，其余仍走 medium。
SOCHI_LISTENER_VERBS = ("聞こえ", "見え", "持ってき", "続け", "立って", "行く", "来て")


def _socchi_is_listener(ja):
    """`そっち` 是否指向**听者**（强模式）。"""
    k = ja.find("そっち")
    if k < 0:
        return False
    tail = ja[k + 3:k + 10]
    return any(v in tail for v in SOCHI_LISTENER_VERBS)


def _hits(markers, ja):
    """通用标记命中（不带人称排除）。"""
    return [m for m in markers if m in ja]


def _person_hits(markers, ja):
    """人称标记命中（带排除）。"""
    out = []
    for m in markers:
        out.extend(_person_occurrences(m, ja))
    return out


def _tteba_is_vocative(ja):
    """`ってば` 是呼びかけ还是断言？——v1 误报的主要来源。

    实测反例（全是**责怪式呼唤**，不是否定/反驳）：
      · `先生ってばすぐ照れちゃうんだから`（老师你呀…）
      · `ママってば恥ずかしがり屋だな`（妈妈你呀…）
      · `先生ってば全然抵抗しないね`
      · `終わるわけないって。先生ってば気が早いよー`（这里的 ってば 在从句首）
    判据：`ってば` **紧接在人称名词后面**（先生/ママ/お母さん…）时，是呼びかけ。
    反例（真断言）：`だから歌ってないってば`（前面是动词/形容词活用）。
    简化实现：`ってば` 之前 1–4 字若构成「称谓名词」→ 呼びかけ。
    """
    VOCATIVE_Nouns = ("先生", "ママ", "パパ", "お母さん", "お父さん", "お姉さん",
                      "お兄さん", "せんせい", "ママさん", "姉さん", "兄さん",
                      "あんた", "君", "お前", "ねえ")
    for n in VOCATIVE_Nouns:
        if (n + "ってば") in ja:
            return True
    return False


def _neg_before_tteba(ja):
    """否定是否**在 ってば 之前的同一断言**里（排除 `わけないって。…ってば` 那种跨句）。

    实测反例：`終わるわけないって。先生ってば気が早いよー`
      → 否定在上一句末（わけないって），ってば 在下一句做呼びかけ。
    判据：只看最后一个 `ってば` 之前 12 字内是否有否定形。
    """
    k = ja.rfind("ってば")
    if k < 0:
        return False
    seg = ja[max(0, k - 12):k]
    if "。" in seg or "、" in seg:
        seg = re.split(r"[。、]", seg)[-1]
    return bool(NEG_RE.search(seg + "ってば"))
# 断言/反驳：标记「说者在断言」，**不决定人称**
# ⚠️ 不含 ったら——那是条件形（気に入っちゃったら），不是断言（实测误报已修）
ASSERTION = ("ってば", "だって")
# 解释/确认/发现语气：不能推成「你……吧」
EXPLAIN = ("んだ", "のだ")
# 回忆式疑问
RECALL_Q = ("っけ",)
# 寻求共识：预设听者参与，但不总是疑问句
CONSENSUS = ("でしょ", "じゃん", "っしょ", "だろ")
# ⚠️ 2026-10-17 影子模式实测（GPT 第十轮 Q4）：`っしょ` 几乎全是**拟声/かけ声**，
# 不是寻求共识——`よっしょ`（嘿咻）/ `うっしょ`（呜咻）/ `っしょっ`（用力声）
# 在 134 轨里贡献了 118 条 soft 告警里的大部分噪声。真正的 `〜っしょ` 极少。
# → 命中 `っしょ` 时，若前面是拟声语境的假名或本身是个短促的声音，一律不算共识。
KASSEI_PREFIX = ("よ", "う", "ど", "ほ", "そ", "は", "や", "ん", "く", "ぐ", "ぬ",
                 # 2026-10-22 Round 5 补：`いっしょ`（一緒）/ `こしょこしょ`（悄悄）/
                 # `しっ…っしょっ`（气声かけ声）都不是共识标记
                 "い", "し", "こ")


def _consensus_hits(ja):
    """CONSENSUS 的**精确**命中（排除三类系统性噪声）。

    2026-10-17 全量 134 轨实测（GPT 第十轮 Q4 影子模式产出）：
      · `っしょ` 几乎全是かけ声：`よっしょ`（嘿咻）/ `うっしょ` / `っしょっ`
        → 前一字是かけ声假名 或 出现在句首的，不算共识。
      · `でしょう` / `だろ(う)` 是**推测语气**（推量形），中文不加「吧」「呢/吗」
        是完全正常的译法（「会很辛苦吧」→「这样下去会很辛苦」），实测贡献 60+ 条噪声。
        GPT 第十轮也明说「R4 用于分层和影子审计，不把统计偏好伪装成硬规则」。
        → 长形 `でしょう` / `だろう` **不算共识标记**；只认短形 `でしょ`
          （求认同，「对吧」）和 `じゃん`（女性口语求认同）。
        ⚠️ `だろう` 常带缩约 `だろ` 单独出现（`言っちゃうんだろ` /
          `ゴムつけるだろうから`），所以要按**后接字符**判，不能只认整词。
    """
    out = []
    for m in CONSENSUS:
        k = ja.find(m)
        if k < 0:
            continue
        if m in ("でしょ", "だろ"):
            nxt = ja[k + len(m):k + len(m) + 1]
            if nxt == "う":
                continue                   # でしょう / だろう = 推测，不是求认同
            # 2026-10-22 Round 5：`だろ` 单独出现也是推量形的口语缩约
            # （`何年ぶりだろ…` / `言っちゃうんだろ`），与 だろう 同义
            # → 只有紧跟问号（`だろ？` 求认同）才算共识标记。
            if m == "だろ" and nxt not in ("？", "?"):
                continue
            # R41：裸 `でしょ` **仍然算**共识标记（与 question_missing 的 R40 收窄
            # 不同口径）——`でしょ` 本质是求认同，「これおいしいでしょ」→「这个很好吃」
            # 确实丢了「吧」；而 question 判的是「是不是疑问句」，两者语义不同。
            # 门禁 `これおいしいでしょ` 正例（Round 5）保持有效。
        if m == "じゃん":
            # R41：`じゃん`（= じゃない 的口语断言）中文译成陈述句是标准做法。
            # 抽样 8/8 全正确（「偉いじゃん」→「我很了不起。」「すごいじゃん」→
            # 「真厉害。」「恥ずかしがらなくてもいいじゃん」→「不用害羞」）→ 不算共识。
            continue
        if m == "っしょ":
            prev = ja[k - 1] if k > 0 else ""
            if k == 0 or prev in KASSEI_PREFIX:
                continue                   # よっしょ / うっしょ / っしょっ
            # R41：`しっ…っしょっ`（气声かけ声）——前一字不是假名时一律不算
            if not re.match(r"[ぁ-んァ-ヶ]", prev):
                continue
        out.append(m)
    return out
# 明显否定——**子句级**：否定形必须落在子句边界（断言标记/标点/句尾）才算
# 「明显否定」。不加这条，「しょうがない奴だって」这种词中 ない 会被误判成
# 否定反驳（实测误报已修）。
# ⚠️ 不收 ねえ（ない 方言 vs 感叹词 ねえ 歧义大）、不收 じゃん（共识标记）
NEG_RE = re.compile(
    r"(ない|じゃない|てない|でない|ません|まい|ず|ぬ)"
    r"(ってば|だって|って|。|、|$|…|ね|よ|か)")
# 过去/状态（弱判据，配合听者标记用；宁松勿紧，confidence 兜底 medium）
# 含 でる（進んでる 的口语缩约，实测漏过）
PAST_RE = re.compile(
    r"[ただ](ん|の)?(だ|。|、|$|…|ね|よ|か)"
    r"|てる|ている|ていた|でる|でいる|いた|いる")


# ⚠️ 影子样本 #3（2026-10-18，盲听抽检发现）：`そっち` 整条撤除后仍有**主语翻转**漏网——
#    `そっちもよく聞こえる` → 译成「**我这边**也听得很清楚」（そっち 指听者，被译成说话人）。
#    这类不能用「そっち 一律算人称」修（语料抽检指人仅 37%，会大面积误报），
#    只认**强模式**：そっち + 听者才做的动作（听得见/看得见/带过来/继续/站着）。
#    这是「宁漏不错」下的最小补丁：只覆盖 6 个动词，其余仍走 medium。
def cue_hint(ja):
    """扫描单条 cue 日文，返回 hint dict（无命中返回 None）。

    act 取值（GPT 给的标签族）：
      listener_address              强听者侧标记（高置信）
      listener_experience_confirmation  听者侧标记 + 过去/状态（中置信）
      speaker_denial               明显否定 + 断言标记（中置信）
      recall_question              回忆式疑问 っけ（中置信）
      consensus_seeking            寻求共识 でしょ/じゃん…（中置信）
      explanation                  解释语气 んだ/のだ（中置信）
      assertion                    裸断言标记（中置信）
    多条可同时成立（如 じゃない+でしょ → consensus + evidence 记否定）。
    """
    if not ja:
        return None
    strong = _person_hits(LISTENER_STRONG, ja)
    # そっち 也去掉「前面紧跟汉字/片假名」的复合（のけぞる 之类）与已知非方位用法
    medium = [m for m in _hits(LISTENER_MEDIUM, ja)
              if not (ja.find(m) > 0 and RE_KANJI_OR_KATA.match(ja[ja.find(m) - 1]))]
    neg = bool(NEG_RE.search(ja))
    assertion = _hits(ASSERTION, ja)
    # ってば 是呼びかけ → 不算断言（实测误报源，见 _tteba_is_vocative）
    if assertion and _tteba_is_vocative(ja) and not _neg_before_tteba(ja):
        assertion = [a for a in assertion if a != "ってば"]
    hint = {"act": None, "confidence": None, "evidence": []}
    if strong:
        hint["explicit_listener_marker"] = strong
    if medium:
        hint["soft_listener_marker"] = medium

    if neg and assertion:
        hint["act"] = "speaker_denial"
        hint["confidence"] = "medium"
        hint["evidence"] = ["否定"] + assertion
    elif strong:
        hint["act"] = "listener_address"
        hint["confidence"] = "high"
    elif medium and _socchi_is_listener(ja):
        # 影子样本 #3：そっち + 听者动作动词 → 强听者指向（升 high）
        hint["act"] = "listener_address"
        hint["confidence"] = "high"
        hint["evidence"] = ["そっち+听者动作"]
    elif medium and PAST_RE.search(ja):
        hint["act"] = "listener_experience_confirmation"
        hint["confidence"] = "medium"
        hint["evidence"] = medium + ["过去/状态"]
    elif any(m in ja for m in RECALL_Q):
        hint["act"] = "recall_question"
        hint["confidence"] = "medium"
        hint["evidence"] = _hits(RECALL_Q, ja)
    elif _consensus_hits(ja):
        hint["act"] = "consensus_seeking"
        hint["confidence"] = "medium"
        hint["evidence"] = _consensus_hits(ja)
    elif any(m in ja for m in EXPLAIN):
        hint["act"] = "explanation"
        hint["confidence"] = "medium"
        hint["evidence"] = _hits(EXPLAIN, ja)
    elif assertion:
        hint["act"] = "assertion"
        hint["confidence"] = "medium"
        hint["evidence"] = assertion
    elif medium:
        # そっち 但无过去/状态 → 仅方位可能性，记最弱提示
        hint["act"] = "listener_reference_weak"
        hint["confidence"] = "low"
        hint["evidence"] = medium

    if hint["act"] is None:
        return None
    return hint


def build_track_state(cue_texts, participants=None):
    """按轨维护的状态（GPT JSON 形状），供未来 polish/translate 注入用。

    participants 默认 ASMR 独白作品的常设：speaker=CV、listener=user。
    """
    state = {"participants": participants or {"speaker": "CV", "listener": "user"},
             "cue_hints": {}}
    for i, ja in enumerate(cue_texts):
        h = cue_hint(ja)
        if h:
            state["cue_hints"]["c%03d" % i] = h
    return state


# ---------------- 注入渲染（GPT Q1「polish 时只注入当前 block 的标签」） ----------------

# act → 给模型的一句话提示。口径严格照 GPT 答复：
# **只提示「注意归属」，不直接给「这句的主语是你/我」**——句尾助词不映射固定人称。
# GPT 第十轮 Q1（P2）：high 用「必须保留某个语义关系」，medium 用「优先检查/倾向于」，
# low 一律不注入（只进 audit / shadow）。
ACT_PROMPT = {
    "listener_address": {
        "high": "「{ev}」明确指向听者。**必须保留听者指向**——默认用「你」，"
                "也可用明确的对方称谓；不能写成说话人自己的经历。",
        "medium": "含「{ev}」。优先检查这句讲的是不是**听者**的事。",
        "low": "含「{ev}」。",
    },
    "listener_experience_confirmation": {
        "high": "「{ev}」明确指向听者。**必须保留听者指向**。",
        "medium": "含「{ev}」——很可能是在确认**听者**的经历（「你那边的…吧」），"
                  "也可能只是方位；优先检查归属，上下文不足时勿强补人称。",
        "low": "含「{ev}」。",
    },
    "speaker_denial": {
        "high": "含否定 + 断言标记（{ev}）。**必须保留「说话人否认/反驳」这层关系**——"
                "不能翻成对听者提问、也不能翻成陈述听者如何。",
        "medium": "含否定 + 断言标记（{ev}）。优先检查否定极性与说者归属，"
                  "上下文不足时勿强补人称。",
        "low": "含断言标记（{ev}）。",
    },
    "recall_question": {
        "high": "含回忆式疑问（{ev}）。**必须保留疑问语气**。",
        "medium": "含回忆式疑问（{ev}）——优先检查是不是疑问句，别翻成陈述。",
        "low": "含「{ev}」。",
    },
    "consensus_seeking": {
        "high": "含寻求共识语气（{ev}）。**必须保留「把听者拉进来」的语气**。",
        "medium": "含寻求共识语气（{ev}）——预设听者参与，但**不一定**是疑问句，"
                  "按语气定句式，别硬加「吧？」。",
        "low": "含「{ev}」。",
    },
    "explanation": {
        "high": "含解释/确认语气（{ev}）。**必须判断清楚这段说明的是谁的状况**。",
        "medium": "含解释/确认语气（{ev}）——不能推成「你……吧」，"
                  "优先判断这段说明的是谁的状况。",
        "low": "含「{ev}」。",
    },
    "assertion": {
        "high": "含断言标记（{ev}）。**保留陈述语气，不是疑问**。",
        "medium": "含断言标记（{ev}）——说者在陈述/强调，不是疑问。",
        "low": "含「{ev}」。",
    },
    "listener_reference_weak": {
        "high": "含「{ev}」。",
        "medium": "含「{ev}」——可能指「你那边」，也可能仅指方位；优先检查，勿强补人称。",
        "low": "含「{ev}」。",
    },
}

# GPT 第十轮 Q1：一个 block 超过这个条数时，只注 high（medium 仍记进 audit）
MAX_HINTS_IN_PROMPT = 8
# D3b（2026-10-18）：高风险 cue 附**前后各 N 条**完整日文+初译作上下文。
# 为什么：`君は大丈夫なわけ?` 单独看是"你没问题吗"，但前一句可能是"我刚才…"，
# 上下文一给，归属就明确了。GPT 第九轮 Q1 也要求「hint 要带证据，不是孤立标签」。
# 预算控制：只有 high 且本块 high ≤ CONTEXT_MAX_HIGH 时才附（多了会挤掉正文）。
CONTEXT_N = int(os.environ.get("KIKOERU_PRAG_CONTEXT", "2") or 2)
CONTEXT_MAX_HIGH = 3


def render_hint_line(cue_id, h):
    """单条 hint → `※ c042｜高｜必须｜ …` 一行（P2 格式）。"""
    lvl = h.get("confidence") or "medium"
    lvl_cn = {"high": "高", "medium": "中", "low": "低"}.get(lvl, "中")
    force_cn = {"high": "必须", "medium": "提示", "low": "提示"}.get(lvl, "提示")
    tpl = ACT_PROMPT.get(h.get("act"), {})
    body = tpl.get(lvl) or tpl.get("medium") or ""
    ev = "、".join(h.get("evidence") or h.get("explicit_listener_marker")
                   or h.get("soft_listener_marker") or []) or h.get("act", "")
    # ⚠️ 2026-10-18 实测事故：原来用 `[c042｜高｜必须] …`，模型把这行**当成待译 cue**
    #    改写成了中文正文（`“んだ”这个词用于表达某种语气，用来说明是谁处于某种状况中。`），
    #    直接漏进成品。方括号 + cNNN 太像 cue 行了 → 换成 `※`，并去掉方括号。
    return "※ %s｜%s｜%s｜%s" % (cue_id, lvl_cn, force_cn, body.format(ev=ev))


RE_SPK_LABEL = re.compile(r"^SPEAKER_\d+$", re.I)


def speaker_switch_notes(items, prev_spk_raw=""):
    """说话人切换提示（2026-10-21 C1，TRIAL_PLAN 试行 A 的接入②）。

    items: [(cue_id, ja, zh_so_far, spk_raw)]；spk_raw = 声纹聚类原始标签
    （SPEAKER_xx）。只认「相邻两条从有标签变成另一个标签」的切换。

    ⚠️ **绝不把 SPEAKER_xx 编号写进 prompt**——实测模型会把它当文本照抄进成品
    （polish.py 的 spk→人名映射同源教训）。这里只给「换了人」这个事实，
    让模型重置二人称指向。没有 spk 数据（独白作品/未跑 diarize）时返回空。
    """
    notes, prev = [], (prev_spk_raw or "")
    for it in items:
        spk = it[3] if len(it) > 3 else ""
        if not spk or not RE_SPK_LABEL.match(spk):
            prev = ""                      # 无标签段打断连续性，不当切换
            continue
        if prev and spk != prev:
            notes.append((it[0],
                          "本句**换了说话人**（系统检测到与上一句不是同一人）："
                          "「你/我」与称谓的指向可能已随换人重置，"
                          "请按本句说话人的角度重新判断，别沿用上一句的指向。"))
        prev = spk
    return notes


def format_block_hints(items, max_hints=None, prev_spk_raw=""):
    """把当前 block 的 hint 渲染成 prompt 片段（**GPT 第十轮 Q1 选定 P2**）。

    items: [(cue_id, ja, zh_so_far)] 或 [(cue_id, ja, zh_so_far, spk_raw)]——
    第 4 位带原始声纹标签时启用「换人→人称重置」提示（C1，2026-10-21）。
    prev_spk_raw：本块第一条的**上一句**声纹标签（换人判定的对比基线）。
    返回 ""（无命中）或一段结构提示。**调用方负责把它放在「待润色 cue 列表正上方」**，
    不是整个 user 内容的最开头（P2 的核心要求）。

    分层规则（GPT 第十轮 Q1 + C1 增补）：
      · high  → `※ c042｜高｜必须｜ …必须保留某个语义关系…`
      · 换人  → `※ c042｜换人｜提示｜…`（C1 新增，只给「换了人」的事实，不含编号）
      · medium→ `※ c043｜中｜提示｜ …优先检查/倾向于…`
      · low   → **不注入**（只留在 audit / shadow mode）
      · 本块 hint 数 > `max_hints`（默认 8）时，只注 high；medium 仍记录但不进 prompt。

    返回 (text, meta)；meta = {"n_high", "n_medium", "n_low", "dropped", "n_switch"}。
    为兼容旧调用（只取字符串）另提供 `format_block_hints_text()`。
    """
    max_hints = MAX_HINTS_IN_PROMPT if max_hints is None else max_hints
    highs, mediums, n_low = [], [], 0
    for _it in items:                      # 兼容 3 元组与 4 元组（第 4 位=spk_raw）
        cue_id, ja = _it[0], _it[1]
        h = cue_hint(ja)
        if not h:
            continue
        lvl = h.get("confidence") or "medium"
        if lvl == "low":
            n_low += 1
            continue
        (highs if lvl == "high" else mediums).append((cue_id, h, ja))

    total = len(highs) + len(mediums)
    dropped = 0
    if total > max_hints and mediums:
        # 超预算：按 GPT Q1 只注 high（medium 退到 shadow / audit）
        dropped = len(mediums)
        mediums = []

    pos = {it[0]: k for k, it in enumerate(items)}
    use_ctx = CONTEXT_N > 0 and len(highs) <= CONTEXT_MAX_HIGH

    def _ctx_lines(cid):
        """D3b：给出该 cue 前后各 N 条的「日文 → 初译」，帮模型判断归属。"""
        k = pos.get(cid)
        if k is None:
            return ""
        out = []
        for j in range(max(0, k - CONTEXT_N), min(len(items), k + CONTEXT_N + 1)):
            if j == k:
                continue
            _cid, _ja, _zh = items[j][0], items[j][1], items[j][2]
            tag = "上文" if j < k else "下文"
            out.append("      %s %s：%s → %s"
                       % (tag, _cid, (_ja or "")[:38], (_zh or "")[:26]))
        return ("\n" + "\n".join(out)) if out else ""

    rows = [render_hint_line(cid, h) + (_ctx_lines(cid) if use_ctx else "")
            for cid, h, _ja in highs]
    # ---- C1（2026-10-21）：说话人切换提示，排在高置信 hint 之后、medium 之前 ----
    # 这是「上下文贯通」的核心机制：`あんた` 在 A 说完后指向听者，
    # 换人之后可能指回 A。无 spk 数据（独白作品/未跑 diarize）时自动为空。
    switch = speaker_switch_notes(items, prev_spk_raw=prev_spk_raw)[:2]
    rows += ["※ %s｜换人｜提示｜%s" % (cid, note) for cid, note in switch]
    rows += [render_hint_line(cid, h) for cid, h, _ja in mediums]
    meta = {"n_high": len(highs), "n_medium": len(mediums),
            "n_low": n_low, "dropped": dropped, "n_switch": len(switch)}
    if not rows:
        return "", meta
    text = ("【本批结构提示：只约束指定 cue，不要翻译本段】\n"
            "ASMR 只有 CV 一方出声，但台词是对着听者说的。下面几句含**表层归属标记**，"
            "请先判断「这句讲的是谁的经历/状态」再译。\n"
            "「必须」= 硬约束，不能丢；「提示」= 优先检查，上下文不足时不要强补人称。\n\n"
            + "\n".join(rows) + "\n"
            "↑ 以上是**给你的指令**，不是待译文本。**不要翻译、不要复述、不要解释这些行**。"
            "待译内容从下一段的【待润色】开始。\n")
    return text, meta


def format_block_hints_text(items, max_hints=None):
    """同 `format_block_hints` 但只返回文本（旧调用点用）。"""
    return format_block_hints(items, max_hints)[0]


# ---------------- 后检测（ja + zh 配对，产出告警） ----------------

def check_pair(ja, zh):
    """对「日文 cue + 中文译文」跑后检测，返回告警列表。

    每条形如 (level, rule, detail, subtype)；level ∈ {"high", "medium", "soft"}。
    - high  = GPT 第九轮指定「源语与中文明确冲突」，进门禁硬判
    - soft  = GPT 第十轮 Q3（R4）细分软门禁：**只告警、不阻断**
              · consensus_seeking（でしょ/じゃん）→ soft
              · explanation（んだ/のだ，语料 35%我 vs 18%你，分布太宽）→ soft
              · recall_question（っけ）→ soft-high（更接近疑问，但仍不硬阻断）
    """
    alerts = []
    if not ja or not zh:
        return alerts

    # 规则 1（高）：强听者标记 vs 中文无听者指向
    # ⚠️ 用 _person_hits：`チン君`/`庭の君`/`僕のお前` 这类非人称用法不算（实测误报源）
    # ⚠️ R30（2026-10-23）补白名单：10-22 人判定过的两种口径误报一直没进代码——
    #    `あんたも私も`→「我们俩」（集合式，听者已在译文中）、`お前なんかに`→「这种人」
    #    （间接式）。实测 4 条残留里 2 条是这两个模式。
    strong = _person_hits(LISTENER_STRONG, ja)
    if strong and not re.search(r"你|妳|对方|您|我们俩|我俩|俩人|两人|彼此|双方|这种人", zh):
        alerts.append(("high", "listener_marker_unrendered",
                       "ja 含 %s，中文无听者指向" % "/".join(sorted(set(strong))),
                       "listener_address"))

    # 规则 2（高）：否定 + 断言 vs 中文极性翻反（变疑问 / 无否定表达）
    # ⚠️ 区别于 v1：① ってば 是呼びかけ（先生ってば…）时不算断言；
    #               ② 否定必须在 ってば 之前的同一断言里（排除跨句 `わけないって。…ってば`）
    if _neg_before_tteba(ja) and not _tteba_is_vocative(ja):
        zh_q = bool(re.search(r"[？?]|吗", zh))
        zh_neg = bool(re.search(r"不|没|别|无|未|非|甭|莫", zh))
        if zh_q or not zh_neg:
            why = "变疑问" if zh_q else "无否定表达"
            alerts.append(("high", "denial_polarity_flip",
                           "ja 否定+ってば，中文%s" % why, "speaker_denial"))

    # ---- GPT 第十轮 Q3（R4）：G3 细分软门禁 ----
    # 只在这些「有相对明确词面依据」的情况下出 soft 告警，**永不阻断**。
    # 依据：GPT 明说 `んだ` 的 35%:18% 远不够做确定性规则；`っけ` 也不应仅凭词尾硬阻断。
    # 1) 回忆式疑问 っけ：中文完全没有疑问/回忆色彩 → soft-high
    # ⚠️ R27（2026-10-23）两处修正（9 条全量人判）：
    #    ① `おっけ`（=OK）不是回忆助词——`おっけ…`→「好…」被误报（RJ9000006 实录），
    #       先剥掉再判；
    #    ② ZH 回忆词表补「对了」——`んだっけ`/`だっけ` 的标准译法就是「对了」
    #       （RJ9000008/RJ9000010 两条「啊对了」被误报）。
    _ja_rk = ja.replace("おっけ", "")
    if any(m in _ja_rk for m in RECALL_Q):
        if not re.search(r"[？?]|吗|呢|吧|记|想|来着|对吧|是不是|对了", zh):
            alerts.append(("soft", "recall_mood_missing",
                           "ja 含回忆疑问 っけ，中文无疑问/回忆色彩", "recall_question"))
    # 2) 寻求共识 でしょ/じゃん：中文完全没有求认同色彩 → soft
    # ⚠️ 2026-10-17 全量实测收窄（GPT 第十轮 Q4 影子模式的直接产物）：
    #    初版 118 条里绝大多数是 `でしょう`（推测语气，「会很辛苦吧」）与
    #    `じゃん`（女性口语断言）——中文不加「吧」是**完全正常**的译法。
    #    → 只在「中文连一个语气词都没有」时才轻提示，否则静音。
    #      这样 soft 从中枢噪声变成真正的可疑样本（GPT：软门禁要能抽得动人）。
    if _consensus_hits(ja):
        # 2026-10-22 Round 5 收窄：
        #   ① 原表漏了最常见的疑问语气词「吗」（只写了「嘛」）——「这不是…吗」
        #      是 じゃん 的标准译法，却因为漏字被判「无求认同色彩」（实测 6 条）。
        #   ② 中文把求认同**实词化**（应该/肯定/当然/不是/真是/反正）同样是忠实翻译。
        _MOOD = (r"[？?吧呢嘛啊呀哦喔啦哟噢诶吗]|对不对|是不是|对吧|不是吗"
                 r"|应该|肯定|当然|明明|不是|真是|反正")
        if not re.search(_MOOD, zh) and len(zh) >= 4:
            alerts.append(("soft", "consensus_mood_missing",
                           "ja 含寻求共识语气，中文无求认同色彩", "consensus_seeking"))
    # 3) 解释语气 んだ/のだ：这里**故意不做人称判断**（分布太宽），
    #    只在「中文完全没有解释/确认色彩」时轻提示——而且这条几乎总是误报，
    #    GPT 第十轮明确要求不要伪装成硬规则。所以只在 detail 里记账，不产生告警。
    return alerts


# G3 细分级别表（GPT 第十轮 Q3 · R4）：决定每条 act 用什么级别
G3_LEVEL = {
    "consensus_seeking": "soft",        # でしょ / じゃん
    "explanation": "soft",              # んだ / のだ（**不能**进硬门禁）
    "recall_question": "soft-high",     # っけ（更接近疑问，但仍只告警）
}


def is_g3_act(act):
    return act in G3_LEVEL


def g3_level(act):
    return G3_LEVEL.get(act, "soft")


# ---------------- 自检（与 mask_nonlexical 同风格：宁可漏判不可误判） ----------------

CASES = [
    # (ja, 期望 act 或 None)
    ("だから歌ってないってば", "speaker_denial"),
    ("私は、あんたみたいに友達がいるわけじゃないから。", "listener_address"),
    ("そう、そっちは清水寺、見たんだ。私は見てないな。",
     "listener_experience_confirmation"),
    ("おう、こんなので寝かしつけ良かったっけ?", "recall_question"),
    ("うん、やっぱり実は嫌いじゃないでしょ？", "consensus_seeking"),
    ("うんそっちは仕事進んでるのほら見せて", "listener_experience_confirmation"),
    ("まったく、しょうがない奴だって", "assertion"),
    ("そっちの方が気に入っちゃったらどうしよう", "listener_reference_weak"),
    ("やめてよはぁうーんもうマジ無理なんだけど", "explanation"),
]

PAIR_CASES = [
    # (ja, zh, 期望告警 rule 集合)
    ("だから歌ってないってば", "所以你不唱歌？",
     {"denial_polarity_flip"}),                                  # 旧版错译 → 高
    ("だから歌ってないってば", "所以说 你根本就没有唱歌呢。",
     set()),        # ⚠️ 主语错但极性对（含「没有」）→ 规则 2 按设计不报：
                    # 主语归属是 hint 层 + 上下文层的职责，后检测只拦极性冲突
    ("だから歌ってないってば", "都说了 我不会唱。", set()),        # 订正版 → 干净
    ("私は、あんたみたいに友達がいるわけじゃないから。",
     "我没有像谁一样的朋友。", {"listener_marker_unrendered"}),   # 丢听者 → 高
    ("私は、あんたみたいに友達がいるわけじゃないから。",
     "我又不像你一样有朋友。", set()),                            # 正确 → 干净
    ("うんそっちは仕事進んでるのほら見せて", "嗯，你那边工作有进度吗",
     set()),                                                     # そっち→你那边 → 干净
    # ---- 2026-10-22 Round 5：consensus 收窄的三条反例 + 一条正例 ----
    ("いっしょに行こう", "一起去吧", set()),          # いっしょ（一緒）不是かけ声/共识
    ("ぶりかも…何年ぶりだろ…", "有几年了…", set()),    # だろ 单独 = 推量缩约
    ("私の声に反応してるじゃん。", "这不是对我的声音有反应吗。",
     set()),                                          # 「吗」是求认同的标准译法
    ("これおいしいでしょ", "这个很好吃", {"consensus_mood_missing"}),  # 真缺语气 → 仍要报
    ("先に湯船に浸かってて、すぐにお姉ちゃんもそっちに行くから。",
     "你先进浴缸吧 姐姐等下也会进去的", set()),                   # そっち→进去 → 干净
    ("まったく、しょうがない奴だって", "真是的 没救了的家伙",
     set()),        # 词中 ない（しょうがない）不判否定 → 无告警（实测误报已修）
    # ---- v2 反例（133 轨实测暴露的误报原文 + 当时的译文，必须零告警）----
    ("僕のチン君が、お姉さんの中で震えて、", "我的肉棒…", set()),
    ("庭の君みたいに舌の長さが控えめだったら", "如果像庭院之君那样，舌头比较短的话", set()),
    ("お姉さんに、僕のお前に、ぎゅーんって、", "对姐姐 对我的面前 紧紧地", set()),
    ("先生ってばすぐ照れちゃうんだから", "老师你总是这么害羞。", set()),
    ("終わるわけないって。先生ってば気が早いよー", "怎么可能结束呢。老师你真是急人。", set()),
    ("もう、ママってば恥ずかしがり屋だな。", "妈 你真是个害羞的人呢。", set()),
]

# v2 反例的 ja 侧（cue_hint 必须**不报** listener/denial 类）
NEGATIVE_JA = [
    "僕のチン君が、お姉さんの中で震えて",
    "ギクロンビ君震えておねだりしてるよ",
    "ウォーマン君しつけられるの好き",
    "いいですよもっと追い込みたいドM君にはぴったりの仕様でしょ",
    "庭の君みたいに舌の長さが控えめだったら",
    "お姉さんに、僕のお前に、ぎゅーんって、",
    "のけそっちゃいますおまんこ見てほしくて",
    # ⚠️ 2026-10-17 影子模式（GPT 第十轮 Q4）复核后**移出**两条：
    #    · `ケイさんもお前との用意にしない` —— 原来是「ASR 残缺句」被顺手放进反例，
    #      实际 `お前と` 就是「和你」，该报 listener_address（报对了）。
    #    · `僕のお前` 仍应排除，但用的是 OWNED_ONMAE（所有格+方位），不是「のお前」整串。
]


def _selftest():
    bad = 0
    for ja, want in CASES:
        h = cue_hint(ja)
        got = h["act"] if h else None
        if got != want:
            bad += 1
            print("  ✗ hint: %r → %s（期望 %s）" % (ja[:34], got, want))
    for ja, zh, want in PAIR_CASES:
        got = {r[1] for r in check_pair(ja, zh)}
        if got != want:
            bad += 1
            print("  ✗ pair: %r / %r → %s（期望 %s）"
                  % (ja[:26], zh[:22], sorted(got), sorted(want)))
    # v2 反例：这些 ja 不该被判成「听者指向 / 说者否认」
    for ja in NEGATIVE_JA:
        h = cue_hint(ja)
        if h and h["act"] in ("listener_address", "speaker_denial"):
            bad += 1
            print("  ✗ neg-ja: %r → %s（应不报 listener/denial）" % (ja[:30], h["act"]))
    n_fmt = _selftest_format()
    bad += n_fmt
    n = len(CASES) + len(PAIR_CASES) + len(NEGATIVE_JA) + FORMAT_N
    print("pragmatics_hints 自检：%d/%d %s" % (n - bad, n, "全过" if not bad else "有失败"))
    return bad == 0


FORMAT_N = 9      # _selftest_format 的用例数（+2 元话语泄漏防护）


def _selftest_format():
    """P2 注入格式自检（GPT 第十轮 Q1）。"""
    bad = 0
    # 1) high 用「必须」，medium 用「提示」，low 不注入
    txt, meta = format_block_hints([("c001", "あんたの寝顔", ""),
                                    ("c002", "うん、やっぱり嫌いじゃないでしょ？", "")])
    # ⚠️ 2026-10-18：行格式从 `[c001｜高｜必须]` 改成 `※ c001｜高｜必须｜`
    #    （方括号+cNNN 会被模型当成待译 cue 改写输出，实测漏 15 处元话语进成品）
    if "｜高｜必须｜" not in txt:
        bad += 1; print("  ✗ P2: high 未用「必须」→ %r" % txt[:90])
    if "｜中｜提示｜" not in txt:
        bad += 1; print("  ✗ P2: medium 未用「提示」→ %r" % txt[:90])
    if meta["n_high"] != 1 or meta["n_medium"] != 1:
        bad += 1; print("  ✗ P2: meta 计数错 → %s" % meta)
    # 2) cue ID 必须出现（对应关系清晰）
    if "※ c001" not in txt or "※ c002" not in txt:
        bad += 1; print("  ✗ P2: cue ID 未出现")
    # 2b) 块尾必须有「这不是待译文本」的终止标记
    if "不是待译文本" not in txt:
        bad += 1; print("  ✗ P2: 缺「不是待译文本」终止标记")
    # 2c) 行首不能再用方括号（会被模型当成 cue 行）
    if "[c001" in txt or "[c002" in txt:
        bad += 1; print("  ✗ P2: 行首用了方括号+cNNN（元话语泄漏的根因）")
    # 3) 标题必须是「只约束指定 cue」
    if "只约束指定 cue" not in txt:
        bad += 1; print("  ✗ P2: 标题缺「只约束指定 cue」")
    # 4) 超 8 条只注 high
    many = [("c%03d" % i, "あんたの話%d" % i, "") for i in range(6)]
    many += [("m%03d" % i, "嫌いじゃないでしょ？%d" % i, "") for i in range(6)]
    txt2, meta2 = format_block_hints(many)
    if meta2["n_medium"] != 0 or meta2["dropped"] != 6:
        bad += 1; print("  ✗ P2: 超预算未裁掉 medium → %s" % meta2)
    # 5) low 不注入
    txt3, meta3 = format_block_hints([("c001", "そっちの方がいい", "")])
    if txt3 or meta3["n_low"] != 1:
        bad += 1; print("  ✗ P2: low 被注入了 → %r" % txt3)
    return bad


if __name__ == "__main__":
    import sys
    if not _selftest():
        sys.exit(1)
