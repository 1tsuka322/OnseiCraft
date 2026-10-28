# -*- coding: utf-8 -*-
r"""确定性「源语必要条件 + 跨行泄漏」扫描器。

## 定位（2026-10-13，按 GPT 第三轮建议）
不做「通用自动真值」——只做**可审计的必要条件检查**。漏掉一条 ≠ 翻错；
**违反一条则高度可疑**，进入人工二分类复核。标签口径：
  · 中文体验类问题 → `影响交付 / 不影响交付`
  · 源语规则告警   → `规则误报 / 需要日语复核`

## 规则
| rule | 检查 | 依据 |
|---|---|---|
| `neg_missing` | 日文有否定（ない/ません/じゃない…）但中文无否定词 | 译反会改变命题 |
| `question_missing` | 日文是疑问（か/?/かな/でしょ）但中文无 ？/吗/呢/吧 | 语气丢失 |
| `number_mismatch` | 日文有数字，中文数字集合不一致 | 数量错误 |
| `dup_suffix` | 后一行文本 ⊂ 前一行（后缀重复） | 翻译泄漏 |
| `dup_ngram` | 相邻行 n-gram 重叠过高（叠词/拟声白名单豁免） | 翻译泄漏 |
| `len_imbalance` | 前一行异常长 + 后一行异常短，且后行 ⊂ 前行 | 翻译泄漏 |
| `confusable` | 日文命中已知近音混淆词表 | ASR 高召回告警 |
| `omit_suspect` | 日文内容 ≥12 字但中文显著偏短（<30%） | **漏译**（2026-10-14 新增） |
| `onomat_translit` | 日文是片假名主导的拟声，中文里一个拟声字都没有 | **拟声被音译**（2026-10-14 新增） |
| `cue_too_long` | 单行停留 ≥5.5s 且行内含句末标点，且停留超过「阅读+该行语音」8s 以上（挂静音） | **cue 过大**（2026-10-14 新增，2026-10-23 R23 收窄，坑 106） |
| `moan_drift` | 日文是纯气声串，中文里出现 ≥2 个非语气词内容字 | **气声被上下文带跑 / 借字面义**（2026-10-14 新增；2026-10-23 R29 降 info：残留均合法） |
| `ono_guess` | 日文只是拟声串，中文却出现「电击/鞭打」这类具体名词 | **脑补名词**（2026-10-14 新增） |
| `asr_suspect` | ASR 段「长且内容稀」→ 疑似非语音上的幻觉 | **凭空出字幕**（2026-10-15 新增，P2 两阶段规则） |
| `kana_variant` | 日文里出现全库极少见的小写假名替换（じゃぁ/あぁー，**纯拖音「ってー」类已滤除**） | **疑似 ASR 变体**（2026-10-15 新增，只标记不归一化；2026-10-23 R26 滤拖音） |
| `person_flip` | 源含呼称词（ボク/君/お前/あんた）、**无明确第一人称代词**且译文含「我」 | **呼称被当自称**（2026-10-21 新增，坑 84；2026-10-23 R24 收窄+降 info） |
| `spk_leak` | asr json 带 spk 的轨：译文含 5 种格式的说话人名泄漏（行首 `X：` 是规范前缀不算） | **台本说话人名泄漏**（2026-10-23 R36 新增，判据 `polish.spk_leak`） |
| `kekkon_confusion` | ja 段组含 結婚 而中文无「婚」，或含 結構 而中文有「婚」 | **結婚/結構/血行 同音混淆**（けっこん/けっこう 一音差）；只进清单不自动改（2026-10-25 T6 新增） |

## 用法
  python scan_violations.py [--out DIR] [--min-sev N]
产物：`violations.jsonl`（逐条）+ `VIOLATIONS.md`（按规则汇总 + 样例）
"""
import os
import re
import sys
import glob
import json
import bisect
import argparse
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cues  # hold_cap（R23 cue_too_long 收窄，坑 106）

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 可用 KIKOERU_LRC_ROOT 指向别的成品目录（做 A/B 对照时扫另一组输出）
import rootloc  # noqa: E402  仓库三层结构：work/ 里，成品/音频在上一级
OUT_ROOT = os.environ.get("KIKOERU_LRC_ROOT") or rootloc.PROD
ASR_ROOT = os.path.join(ROOT, "data/draft")
DEF_OUT = os.path.join(ROOT, "regset")
PUNCT = "。、，,.!！?？…♪~〜ー♡ \u3000「」『』（）()"

# ⚠️ 裸 `ない` 必须锚**从句边界**（2026-10-23 R18，与 pragmatics_hints.NEG_RE 同口径）：
#    不锚会把 `みんないい`（みんな+いい）、`かない`（つかない…）这种**词中的ない**当成否定
#    （实测贡献 9 条误报）。`ないで`/`なけれ`/`なかった`/`ません`/`じゃない` 是明确否定形，不锚。
# ⚠️ `(?<!みん)`：`みんないい` 是 みんな+いい（「大家都是好人」），不是否定。
#    第一版试过「锚从句边界」，把 `人来ない空き教室` 这种真否定也挡掉了 → 回退，只排这一种混淆。
RE_NEG_JA = re.compile(r"ません|じゃない|なかった|ないで|なけれ|(?<!みん)ない")
# `かもしれない` 是**或谓**（推量），不是否定；中文给出情态词即算译出（R18 实测 14 条，9 条可豁免）
# 或说的六种形态（含普及与非普及）：
RE_POSSIBLE_JA = re.compile(r"かもしれない|かも知れない|かもしんない"
                       r"|かもしれません|かも知れません|かもしんません")
RE_POSSIBLE_ZH = re.compile(r"可能|也许|大概|说不定|恐怕"
                            # R39：推量的其他常见译法（「或许你有看过」）
                            r"|或许|好像|似乎|应该")


def neg_possible_exempt(ja, zh):
    """`かもしれない`（或谓/推量）且中文已给情态词 → 不算丢否定（R18）。

    与「语法否定→情态词豁免」（NEG_MODAL_*）同思路，但针对的是**推量**而不是否定：
    「きついかも知れないから」→「可能会有点疼」是完整译法。
    """
    return bool(RE_POSSIBLE_JA.search(ja or "")) and bool(RE_POSSIBLE_ZH.search(zh or ""))
RE_NEG_ZH = re.compile(r"不|没|别|未|否|莫|免|无|勿|难以|没法|很少"
                        # 2026-10-22 Round 8：否定被译成这些实词，同样算「有否定」
                        r"|很难|哪有|有什么|没什么"
                        # R39（2026-10-23）：20 条抽样挖出的实词化译法——
                        #   しか〜ない → 「只是/只有」；わけがない → 「怎么可能」；
                        #   分かりません → 「谁知道」；やってやろうじゃないの → 「偏要」
                        r"|只|才|怎么可能|谁知道|偏要|看你怎么")
# ⚠️ `かな` **必须锚定到句尾**：不锚定会把所有「〜かない」动词否定形式当成疑问
#    （行かない / 動かない / 書かない…实测贡献 question 误报的大头）。
# ⚠️ `じゃないですか` 是**断言**（じゃないか 反问，中文译成陈述即可），不是疑问。
#   实测：`立派な大人じゃないですか`→「您已经是出色的成年人了」是正确译法（R19）。
#   所以 ですか 前面是 じゃない 时不算疑问；裸 `ですか`（誰ですか/どこ行きたいですか）仍算。
RE_Q_JA = re.compile(r"(?<!し)か[?？]?\s*$|かな[。、！!？?…]?\s*$"
                     r"|(?<!じゃない)ですか[?？]?\s*$"
                     r"|でしょ(?!う)|だろ[?？]|の[?？]")
# 2026-10-22 Round 8b：`でしょう`/`んだろ` 都是**推量**（中文不给「吧/吗」是正常译法），
#   与 consensus_mood_missing 的口径保持一致——`だろ` 只有紧跟问号才算疑问。
# 句尾「か」但**不是疑问**的固定说法（Round 8 实测占 question 误报的一半）：
#   っつーか / っつか =「或者说」；そっか =「原来如此」；行こっか = 劝诱；
#   なかなか 里的「か」不是句尾助词
Q_JA_STRIP = ("っつーか", "っつか", "そっか", "なかなか", "であろう",
              # Round 8b：句尾「か」但不是疑问的固定说法
              "じゃんか", "やろうか", "でしょうか", "だか", "っけ",
              # Round 11（语料挖出）：「〜というか／ていうか」=「或者说」，接续用法
              "というか", "っていうか", "ていうか",
              # Round 19：`とか`（列举「…之类的」）占 question 误报的 24%（44/185），
              #   「感じようとか」=「感觉被压迫之类的」，根本不是疑问
              "とか",
              # Round 19：`じゃないですか` 是**断言**（じゃないか 反问），中文译成陈述即可。
              #   ⚠️ 不能只用正则 lookbehind：句尾「か」那个分支照样会命中，必须先剥掉。
              "じゃないですか",
              # R40（2026-10-23）：20 条抽样挖出的非疑问「か」尾巴
              #   ばか/ばーか（骂人）、つか（=っていうか 的省略）、じゃねえか（断言）
              "ばーか", "ばか", "つか", "じゃねえか", "じゃねーか")
RE_Q_ZH = re.compile(r"[？?]|吗|呢|吧|么|是不是|难道|莫非|该不会|对吧|如何")
# ---- 2026-10-22 校准（Gemini 辅助判定 + 人工核验 30 条：question 精度 ~27%、neg ~20%）----
# 已确认的误报源，别再用裸正则：
#   ① `しか` 结尾被 `か$` 误判疑问（「喘ぐしか」不是问句）→ RE_Q_JA 加 (?<!し)
#   ② `こないだ`（前几天）里含「ない」→ 匹配前先剥掉这个词
#   ③ 语法否定（ないと/なくちゃ/なきゃ/しか…ない/ないほど/たまんない）译成
#      「必须/只能/太」是**正确译法**，不算漏否定 → 中文有情态词就豁免
#   ④ `危ない`/`もったいない` 是い形容词，压根不是否定 → 豁免
NEG_JA_STRIP = ("こないだ",
                # Round 19：`じゃないですか` 是 `じゃないか` 反问=**断言**，中文译成陈述即可
                #   （「立派な大人じゃないですか」→「您已经是出色的成年人了」）。
                #   同一个模式同时污染 question 和 neg 两条规则，两边都要剥。
                "じゃないですか",
                # 2026-10-22 Round 8：这些含「ない/けない」但**不是否定**
                #   情けない（可悲）/ いけない（不行·不该）/ 仕方ない·しょうがない（没办法）
                #   くせに（明明…却）——都是语气/评价，不是命题否定
                "情けない", "いけない", "仕方ない", "しょうがない", "くせに",
                "なちゃけない",
                # 2026-10-22 Round 8b：以「ない」结尾的**い形容词**（不是否定命题）
                "だらしない", "みっともない", "つまらない", "とんでもない", "くだらない",
                "しょうもない", "はしたない", "あどけない", "せつない", "なさけない",
                # Round 11（语料挖出）：申し訳ない＝「对不起」，形容词不是否定
                "申し訳ない",
                # R38（2026-10-23）：24 条抽样新发现的 4 类误报源
                # ① 惯用语/形容词的汉字形（ASR 常写汉字，旧表只有かな形）：
                "たまらない", "堪らない", "切ない", "情けない",
                # ③ なんじゃない＝なん+じゃない 断言（「お見通しなんじゃないか」→「被你看透了」）
                "なんじゃない",
                # R39（2026-10-23）：20 条抽样挖出的固定说法
                #   大して〜ない（「大して仲良くもない」→「关系一般」是正确译法）
                #   かまいません（「可以」，不是命题否定）
                #   ないたずら（= な+いたずら，na 形容词，不是否定）
                #   じゃないか / じゃないの（反问断言，中文译成陈述即可）
                "かまいません", "ないたずら", "じゃないか", "じゃないの")
                # ⚠️ 大して 不能放这里——先剥掉 大して 会让 `大して〜ない` 的正则失去锚点，
                #    残留的 ない 仍会命中（R39 实测）。它只在 NEG_JA_RESTRIP 里处理。
# R38：正则型剥除（比子串更精确的两类）
# ② 跨词界「ない」：こんな/あんな/そんな/どんな + い形容词（「あんないやらしい」）
#    → 把 ([こあそど])んない 还原成 \1んな，消掉假「ない」
NEG_JA_RESTRIP = [
    (re.compile(r"([こあそど])んない"), r"\1んな"),
    # ④ 句尾「じゃない?」= 反问断言（「終わりじゃない?」→「已经结束了」）
    (re.compile(r"じゃない[?？♡!！]"), ""),
    # ⑤ 句尾「ませんか」= 礼貌邀约（「感じませんか?」→「能感受到吗？」）
    (re.compile(r"ません[か？?]"), ""),
    # R39：句尾裸「じゃない」（无标点）同样是断言（「いつもより感じちゃうじゃない」）
    (re.compile(r"じゃない\s*$"), ""),
    # R39：`大して〜ない` 整体是「并不怎么…」，中文译成「关系一般」即完整
    (re.compile(r"大して.{0,12}?ない"), ""),
]
# R38：question 侧的同类正则剥除（只收「反问断言」这一类，**不收 ませんか**——
# 「気持ちよく感じませんか?」是真的疑问，剥掉就漏报）。
Q_JA_RESTRIP = [
    (re.compile(r"じゃない[?？♡!！]"), ""),
    (re.compile(r"なんじゃない[?？♡!！]?"), ""),
    # R39：`じゃないの?` 也是反问断言（「浅い恋心しかなかったんじゃないの?」）
    (re.compile(r"じゃないの[?？♡!！]?"), ""),
    # R40：句尾无标点的 `じゃないか`（「上手いじゃないか」→「还真不错」）
    (re.compile(r"じゃないか\s*$"), ""),
    # R40：**裸 でしょ**（后面没有 ？）不算疑问——141 条里 33 条由它触发，
    #   抽 7 条人判全是「很正常。」「挺麻烦的。」「没办法啦…。」这类自然陈述译法。
    #   ⚠️ 带 ？的「でしょ?」仍算（正则锚 $ 天然区分）。
    (re.compile(r"でしょ(?!う)\s*$"), ""),
    # R40：句尾 `のか`（自问/感叹，中文陈述即可；带 ？的仍算）
    (re.compile(r"のか\s*$"), ""),
    # R40：叠音拟声「ふかふか」「ぺろぺろ」被当成「か」结尾
    (re.compile(r"(.)か\1か\s*$"), r"\1\1"),
    # R40：`行こっか` = 勧誘（「那就去吧」），不是疑问
    (re.compile(r"こっか\s*$"), ""),
]
NEG_MODAL_JA = re.compile(r"ないと|なくちゃ|なきゃ|しか.{0,12}ない|ないほど|たまんない"
                          r"|危ない|もったいない")
NEG_MODAL_ZH = re.compile(r"必须|要|得|只能|只好|不得不|难以|受不了|太|可惜|危险|快")
# ---- 语料挖出的语法/语气模式（learningskill 第三条通道，2026-10-22）----
# `data/style/grammar_patterns.json` 由 kikoeru-style-learn/scripts/mine_grammar.py
# 从 8,667 句口语语料挖出、并经 UniDic 词典校验。缺失时退回内置保守集。
GRAMMAR_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "style", "grammar_patterns.json")
_GRAM = {}


def load_grammar(path=GRAMMAR_PATH):
    """载入挖出的模式；文件不存在或损坏时静默退回空（不影响扫描）。"""
    global _GRAM
    if _GRAM:
        return _GRAM
    try:
        _GRAM = json.load(open(path, encoding="utf-8"))
    except Exception:
        _GRAM = {}
    return _GRAM


_TK_CACHE = [None]


def _tk():
    if _TK_CACHE[0] is None:
        from janome.tokenizer import Tokenizer
        _TK_CACHE[0] = Tokenizer()
    return _TK_CACHE[0]


def is_filler_only(ja):
    """整行只是 filler/感叹词（`もうなんか` / `ほら` / `まあ`）→ 不构成命题。

    这种行**不该**参与否定/疑问判定：它没有命题内容，谈不上「丢掉否定/疑问」。
    判据 = UniDic 词性（フィラー/感動詞/記号）+ 语料挖出的高频 filler 词表（双保险）。
    """
    global _GRAM
    if not _GRAM:
        load_grammar()
    # 快筛：filler 行必然很短（>14 字一定含命题），避免给 2.2 万行全部跑分词
    if len(ja or "") > 14:
        return False
    try:
        toks = list(_tk().tokenize(ja or ""))
    except Exception:
        return False
    mined = set((_GRAM.get("filler") or {}).keys())
    # 副词/接续词/感叹/符号都不构成命题；一旦出现动词·名词·形容词·助动词内容就算有命题
    SOFT = ("フィラー", "感動詞", "記号", "副詞", "接続詞", "連体詞", "助詞")
    for tk in toks:
        p1 = tk.part_of_speech.split(",")[0]
        if p1 in SOFT:
            continue
        if tk.base_form in mined or tk.surface in mined:
            continue
        return False
    return bool(toks)


RE_DIGIT = re.compile(r"\d+")

# ---- 数字比对归一化（2026-10-22 Round 4，坑 93）----
# 旧判据只比 `\d+`：`3階→三楼`、`2ヶ月→两个月` 这类**汉字数字译法**全部误报（实测 67 条里 63 条）。
_CN_D = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
         '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
_CN_U = {'十': 10, '百': 100, '千': 1000, '万': 10000}
RE_CN_NUM = re.compile(r"[零〇一二两三四五六七八九十百千万]+")
# 阿拉伯数字：排除嵌在拉丁词里的（MAG3A / MP3 这类型号）
RE_ASCII_NUM = re.compile(r"(?<![A-Za-z0-9])(\d+)([万千百])?(?![A-Za-z0-9])")


def cn2int(s):
    """中文数字 → int（十五=15 / 二十五=25 / 一千二百=1200 / 五万=50000）。"""
    if not s:
        return None
    total = section = num = 0
    for ch in s:
        if ch in _CN_D:
            num = _CN_D[ch]
        elif ch in _CN_U:
            u = _CN_U[ch]
            if u >= 10000:
                total += (section + num) * u
                section = num = 0
            else:
                section += (num or 1) * u
                num = 0
        else:
            return None
    return total + section + num


def text_nums(text):
    """抽出文本里的数值集合（阿拉伯 + 汉字数字，含 `5万` 这类混合写法）。"""
    out = set()
    for m in RE_ASCII_NUM.finditer(text or ""):
        try:
            out.add(int(m.group(1)) * _CN_U.get(m.group(2) or "", 1))
        except ValueError:
            pass
    for m in RE_CN_NUM.findall(text or ""):
        if m != "十" and not any(c in _CN_D for c in m):   # 纯单位（万/百）不算数
            continue
        v = cn2int(m)
        if v is not None:
            out.add(v)
    return out


def number_mismatch(ja, zh, ja_ctx="", zh_ctx=""):
    """数字不一致？返回 (日文数字, 中文数字) 或 None。

    两边都**必须有**数字才判（中文全无数字属 omit_suspect 的域）。
    三类豁免：
      ① 数字字符序列相同 → `12345` vs `1 2 3 4 5`（数数）；
      ② 差异只有 1 → 「一起/一下」的「一」不是数量；
      ③ **跨行豁免**：缺的数字出现在相邻行的日文里 / 多的数字出现在相邻行的中文里
         （cue 级翻译 + 显示行拆分 → 数字落在邻行，单行比对必然误报）。
    """
    dj, dz = text_nums(ja), text_nums(zh)
    if not dj or not dz:
        return None
    if dj == dz:
        return None
    if "".join(c for c in ja if c.isdigit()) == "".join(c for c in zh if c.isdigit()):
        return None
    if (dj ^ dz) <= {1}:
        return None
    missing, extra = dj - dz, dz - dj
    if missing and missing <= text_nums(ja_ctx):    # 缺的都在日文邻行
        return None
    if extra and extra <= text_nums(zh_ctx):        # 多的都在中文邻行
        return None
    return sorted(dj), sorted(dz)
# 已知「两次解码易混」的近音词（高召回告警）
CONFUSABLE = ["男子禁制", "男子近世", "重罪人", "10歳人", "料金", "料件"]
RE_UNITREP = re.compile(r"^(.{1,4})\1+.{0,2}$")

# ---------------- 2026-10-14 新增：3 类「cue 管线换出的新问题」----------------
# 这三条都是**必要条件告警**（高召回、需人工复核），不是「翻错」的证明。
KATA = re.compile(r"[\u30a0-\u30ff\u31f0-\u31ff]")
KATA_RUN = re.compile(r"[\u30a0-\u30ff\u31f0-\u31ff]+")
KATA_RUN_ALL = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u30fc]+")
KANJI = re.compile(r"[\u4e00-\u9fff]")
# 片假名 run 在本轨出现 ≥ 此次数 → 视为**角色名/常用外来语**，不判音译。
# 理由：角色名（リリア→莉亚、マヤ→玛雅）本来就是音译，重复率高；
# 而真正的拟声（キュー、カヤマヤマ）多是零星出现。
KATA_NAME_FREQ = 3
# 「中文里可以接受」的拟声/语气/动作字。中文译文只要沾上这些字，就不判音译。
# 注意要包含**动作动词**（撸蹭揉捏…），因为日文拟声的正确答案常常是动作词而非拟声词。
ZH_ONO = set(
    "啾咕噗啪砰咚哗嗒哒噜嘎叽嗡嘟嘶咯咔嚓嚓沙滋哔嘀咻嗖嗯啊哈呜嗷嘿哼呣唔啵啧嘬呼嘻呀哟哦唉嘘咝唰喵咩哞汪"
    "撸蹭揉捏抖摇扭抽插舔吸咬吮抱亲吻摸按压拍打顶挤喷射流滴答嗤噗嘻噫嘤唔"
)
# 音译高危字：**只留正常中文里几乎不会连用的字**（把 那/多/拉/米/特/德/克/丽 这类
# 常见字全部剔除，否则「用莉亚那半遮半掩的胸部」会因为「亚那」相邻而假阳性）。
ZH_TRANSLIT = set("基欧叽喀噜嘶嗒嘭砰啵嘚咝吡咪咔哒嗵咚玛雅卡喵咩哞汪")
# ---- ABB/AAB/ABAB 式汉语拟态词白名单（2026-10-16，B2 改进②）----
# `ニコニコ→笑咪咪` 是**正确的汉语拟态词译法**，不是音译；但 `ZH_ONO` 里没有
# 「笑/咪」，旧判据会把它误报成音译（B2 扫描 4 条 onomat_translit 里 3 条误报）。
#
# ⚠️ 不能只用「叠字」正则泛化——「哥哥 / 妈妈 / 爸爸」这些**名词叠字**会假阳性
#    （实测 `哥哥的脸 基欧` 因「哥哥」被判成合格拟态，反而放过了真音译「基欧」）。
#    所以叠字必须是**拟态字**：状态/声音/触感类，不含亲属称谓与普通名词。
# 形态：ABB（笑咪咪/湿漉漉）、AAB（麻酥酥/颤巍巍）、ABAB（咕啾咕啾/噗嗤噗嗤）。
MIMETIC_CHARS = set(
    "咪漉糊溜乎邦酥麻巍悠软硬绵黏滑湿干亮暗甜酸苦辣咸香臭烫凉热冷暖"
    "咚啵啪砰哗嗒咕啾噗嗤滴答怦嗡哗噜沙滋哔咻嗖"
)
RE_ABB = re.compile(r"[\u4e00-\u9fff]([\u4e00-\u9fff])\1")       # ABB: 笑咪咪 / 湿漉漉
RE_AAB = re.compile(r"([\u4e00-\u9fff])\1[\u4e00-\u9fff]")       # AAB: 麻酥酥 / 颤巍巍
RE_ABAB = re.compile(r"([\u4e00-\u9fff]{2})\1")                  # ABAB: 咕啾咕啾

# 常见片假名**外来语**（不是拟声）：命中即跳过音译检测。
# 这些词本来就要按词义译（ダブル→双人、セックス→做爱），不该拿音译规则去管。
KATA_LOAN = {
    "ダブル", "セックス", "ミルク", "トレーニング", "ブルマ", "スケベ", "サービス",
    "プレイ", "ゲーム", "カウント", "タイム", "ラブ", "キス", "ベッド", "ホテル",
    "ビデオ", "カメラ", "スタイル", "リズム", "テンポ", "パワー", "スピード", "サイズ",
    "タイプ", "レベル", "アップ", "ダウン", "カード", "マスク", "エッチ", "ローション",
    "コンドーム", "バイブ", "クリトリス", "バスト", "ウエスト", "ヒップ", "アイドル",
    "サンキュー", "グッズ", "アニメ", "ドラマ", "グループ", "クラス", "メール", "ページ",
}
# ⚠️ 2026-10-23 Round 15 实测：**别用长度比阈值去分「敬语压缩」和「整句漏译」**。
#   试过 0.30→0.16：omit_suspect 68→12，但门禁用例（真漏译 `修学旅行一日目、やっと終わったね`
#   被丢，ratio 0.21）被一起毙掉 → 同一个比值既可能是敬语压缩（正确），也可能是整句漏掉（缺陷），
#   **字符长度比区分不了这两者**，只能靠人工看「有没有丢从句」。
#   想加「敬语豁免」也只覆盖 16% → 根因是日文整体更长，不是敬语。
OMIT_RATIO = 0.30       # 中文可读字 / 日文内容字 < 此值 → 疑似漏译
OMIT_MIN_JA = 12        # 日文内容字少于此值不判（太短没意义）
LINE_LONG_SEC = 5.5     # 单行停留超此值且行内含句末标点 → cue 过大（跟随 MAX_HOLD_SEC，2026-10-16 裁决）
# R23（坑 106）：停留超过「阅读时间 / 该行语音剩余」此值才算挂静音。
# 依据：段间静音 gap 全库中位 ~2.8s 是 ASR 分段常态（无人投诉）；用户投诉的
# A1–A7 是 20s+ 挂死。取 8s = 自然 gap 上界 × ~3，只抓极端尾。
LINGER_TOL = 8.0
EOS_ALL = "。！？!?"
RE_PIECE = re.compile(r"[^、。！？!?…\s]+")

# ---- 纯标点丢句（2026-10-19 加）----
# 与 `polish._PUNCT_ONLY` / `polish._JA_LEX` **必须一致**（那边命中回退初译，这里只扫）
RE_PUNCT_ONLY = re.compile(
    r"^[\s…‥・.．,，、。！？!?~～\-—–♪♡♥☆★（）()\[\]「」『』\"'“”‘’]+$")
RE_JA_LEX = re.compile(r"[\u3040-\u30ff\u4e00-\u9fffA-Za-z0-9]")

# ---- 纯气声碎片（2026-10-14 加）：`お…あ…あ…足…` / `お…お…` / `ん…` ----
# 这类段**没有词义**，只有气息；中文应译成语气词。实测模型被前文「妈妈」带跑，
# 把 `お…あ…あ…足…` 译成「妈…妈…妈…脚…」（`translate.py` 初译阶段就错了）。
RE_ELL = re.compile(r"…+")
ZH_INTERJ = set("啊哦嗯唔呜呀哈唉呃咿噫噢嘿哼嗷啵哟欸嗨喔呐哪呕吁")
# 气声假名（もぅ/うぇぁ/んん…）。判据 = 每段 ≤1 字，或**全部由这些假名组成**
MOAN_KANA = set("もぅうぇあんひっえおはぁ")
# 被「脑补」出来的危险名词：日文只是拟声串，中文却冒出这类具体动作/道具
GUESS_NOUNS = ["电击", "鞭打", "触电", "殴打", "滴蜡", "蜡烛", "皮鞭"]

# ---- 呼称人称反转（2026-10-21，SKILL.md 坑 84 通用化）----
# RJ9000014 实测：作品里 `ボク` 是**呼称**（对听者的称呼，相当于「あなた」），
# 不是第一人称「我」；初译按「我」译 → 本作 493 行人称反转。
# 修法是「源文预替换 ボク→あなた 再翻」（实测 3/3；只给提示只有 3/5）。
# 这里做**通用检查**：源含呼称词（ボク/君/お前/あんた）且译文含「我」→ 报警。
# ⚠️ 这是 per-work 确认项：有些作品 ぼく 确实是第一人称（「我是…」合法），
#    所以 severity 只给 medium、confidence 给 low——报警后要按作品定夺，
#    确认是呼称的作品应走「源文预替换」修法，而不是逐行改译文。
RE_HONORIFIC = re.compile(r"ボク|ぼく|僕|君|お前|あんた")
RE_ZH_WO = re.compile(r"我")
# R24（2026-10-23）：ja 含这些**明确第一人称代词**时，「我」是合法翻译。
# ⚠️ 僕/ぼく/ボク **不在排除表**：它们既是自称也可作呼称（坑 84 的缺陷词正是ボク），
#    排除它规则就永远抓不到原缺陷；这类歧义命中按 info 级存档（per-work 确认）。
RE_SELF_PRON = re.compile(r"私|わたし|自分|わたくし|あたし|アタシ|俺")


def person_flip(ja, zh):
    """呼称词被当自称：ja 无**明确**第一人称代词且源含呼称词、译文含「我」→ True。

    ⚠️ 2026-10-23 R24 收窄（坑 84）：旧口径「源含呼称词且译文含我」残留 293 条，
    抽 25 条人判 23 条是正确译法——ja 里另有 私/僕 等第一人称代词（「我」的合法
    来源），或日语省略主语由模型正确补出。收窄为「ja 无明确第一人称代词」后
    293 → 228，severity 降 info。残留多为 僕/ボク 自称作品的歧义命中，靠
    per-work 确认消化（坑 84 修法：源文预替换 ボク→あなた）。
    """
    if not ja or not zh:
        return False
    if RE_SELF_PRON.search(ja):
        return False
    return bool(RE_HONORIFIC.search(ja) and RE_ZH_WO.search(zh))


def is_mimetic(zh):
    """中文里是否含**汉语拟态词**（叠字结构）→ 视为合格译法，不算音译。

    命中 ABB / AAB / ABAB 任一**且叠字是拟态字**（MIMETIC_CHARS）即 True。
    ⚠️ 叠字必须是拟态字：否则 `哥哥的脸` 里的「哥哥」会被误当拟态词。
    """
    for m in RE_ABB.finditer(zh):
        if m.group(1) in MIMETIC_CHARS:
            return True
    for m in RE_AAB.finditer(zh):
        if m.group(1) in MIMETIC_CHARS:
            return True
    for m in RE_ABAB.finditer(zh):
        if any(c in MIMETIC_CHARS for c in m.group(1)):
            return True
    return False


# ---- 人名豁免（2026-10-17，B2 改进①）----
# 片假名 run 紧邻人称后缀（ちゃん/さん/さま/様/君/くん/殿/嬢）→ 是**角色名**，不是拟声。
# B2 误报②：`では、カカラちゃん`（RJ9000004 05）——「カカラ」全库仅出现 1 次，
# 高频豁免（≥3 次）覆盖不到，只能靠后缀判据。
# ⚠️ `KATA_RUN` 会把 `カカラちゃん` 提成连体的 6 字 run（`カカラチャン`），
#    所以先剥掉尾部后缀再比对母名。
# ⚠️ 句末裸呼语（`勝手に動くなマカ`）无后缀，抓不到；全库仅 1 次的边缘角色，
#    接受这 1 条误报，不为此上复杂规则（误伤风险高于收益）。
RE_NAME_SUFFIX = re.compile(
    r"([\u30a0-\u30ff]{2,6})(?:ちゃん|さん|さま|様|君|くん|殿|嬢)")
RE_NAME_SUFFIX_TAIL = re.compile(r"(?:ちゃん|さん|さま|様|君|くん|殿|嬢)$")


def name_mothers(ja):
    """日文里「人称后缀前的片假名母名」集合（角色名候选）。"""
    return set(RE_NAME_SUFFIX.findall(ja or ""))


def strip_name_suffix(run):
    """片假名 run 剥掉尾部人称后缀 → 母名（カカラチャン → カカラ）。"""
    return RE_NAME_SUFFIX_TAIL.sub("", run)


def onomat_translit(ja, zh, common_runs=(), common_zh=()):
    """日文里有**纯片假名拟声段**，中文却像音译 → 疑似音译（キュー → ✗基欧 / ✓啾）。

    精度优先，判据全部满足才报：
      ① 日文里存在一段**连续片假名**，长度 2–8，且**不是本轨高频 run**（高频=角色名/外来语）、
         不在常见外来语黑名单里、**不是人称后缀前的母名**（角色名）
      ② 中文里**一个「可接受字」都没有**（拟声字或动作动词），且**不是汉语拟态词**
         （ABB/AAB/ABAB 叠字，如 笑咪咪/湿漉漉/咕啾咕啾）
      ③ 中文里存在 **≥2 个相邻**的音译高危字（音译是逐音节连写），
         且这段**不是本轨高频串**（高频=角色名的固定音译，如 玛雅/莉亚）

    这样能命中 カヤマヤマ→卡雅玛雅、キュー→基欧，而放过 ダメ→不行、サンキュー→谢谢、
    シコシコ→撸撸、ドキドキする→心跳加速、ニコニコ→笑咪咪、カカラちゃん→卡喀啦妹妹，
    以及 リリア→莉亚 / マヤ→玛雅 这类角色名。
    """
    jc = norm(ja)
    mothers = name_mothers(jc)
    runs = [r for r in KATA_RUN.findall(jc)
            if 2 <= len(r) <= 8 and r not in common_runs and r not in KATA_LOAN
            and strip_name_suffix(r) not in mothers]                  # ①
    if not runs:
        return False
    if any(c in ZH_ONO for c in zh) or is_mimetic(zh):        # ②
        return False
    for m in re.finditer(r"[%s]{2,}" % re.escape("".join(ZH_TRANSLIT)), zh):     # ③
        if m.group() not in common_zh:
            return True
    return False


def _has_repeat_phrase(ja):
    """日文按标点切成短语后，有 ≥2 字短语重复出现 → 中文压缩属正常。"""
    pieces = [p for p in RE_PIECE.findall(ja or "") if len(p) >= 2]
    return len(pieces) != len(set(pieces))


def omit_ratio(ja, zh):
    """返回「中文可读字 / 日文内容字」，不适合判定的返回 None。

    排除项（中文天然更短，判了就是噪声）：
      · 日文片假名 ≥40%（拟声/外来语，中文常压缩）
      · 日文本身是重复串（ゼロ、ゼロ、ゼロ…）
      · 日文是重复短语（ありがとうございます。ありがとうございます / いきましょう、いきましょう…）
    """
    jc, zc = norm(ja), norm(zh)
    if len(jc) < OMIT_MIN_JA:
        return None
    if len(KATA.findall(jc)) >= 0.4 * len(jc):
        return None
    if is_rep(ja) or _has_repeat_phrase(ja):
        return None
    return len(zc) / len(jc)


def kekkon_confusion(ja, zh):
    """結婚/結構/血行 同音混淆候选（2026-10-25，T6）。命中返回方向说明，否则 None。

    けっこん/けっこう 只差一个音，Whisper 会互相误识。2026-10-25 实测语料：
    73 句含「結婚」里 **59 句是真结婚、只有 2 句是误识** → 不能进 errata 自动替换
    （会改坏那 59 句），只做 advisory 扫描候选。两个方向一次抓全：
      A ja 含 結婚 而 zh 无「婚」→ 可能实为 結構（挺/相当）或 血行（血液循环）；
      B ja 含 結構 而 zh 有「婚」→ 反向（把 結構 当 結婚 译了）。
    ⚠️ 调用方必须传**段组级**文本（group_text 的 ja/zh）：翻译在 cue 层做、显示层
       会把一条 cue 拆成多行，「婚」经常落在续行——单行判会成片误报
       （坑 131/132，与 neg/question 的跨行聚合同思路）。
    """
    if not ja:
        return None
    if "結婚" in ja and "婚" not in (zh or ""):
        return "ja 含 結婚，中文无「婚」（可能实为 結構/血行）"
    if "結構" in ja and "婚" in (zh or ""):
        return "ja 含 結構，中文却出「婚」（反向误识）"
    return None


# ---- 器官术语对照（2026-10-26 R132）----
# 为什么单独一条：`term_lock` 的允许集只管**已登记进 glossary/terms_db 的词**，
# 而「クリトリス→阴蒂」在 terms_db 里是 variant 档（share 72.7%），锁不住；
# 实测 RJ9000010 轨09 有 5 行把 クリトリス 译成「龟头」（该作 クリトリス 8 次、
# チンポ 0 次，说话人全是女性）—— 全是 ASR 把 クリトリス 听成 栗/熊/木架/民间/波基
# 之后按字面硬译的产物。
# 判据刻意窄：只查**日文明确出现该词、而中文整组里没有对应词族**的情况。
ORGAN_MAP = (
    ("クリトリス", ("阴蒂", "阴核", "豆豆", "小豆")),
    ("亀頭", ("龟头", "龟菇")),
)


def organ_term_mismatch(ja, zh):
    """器官术语对不上（日文点名了某个器官，中文整组里没有对应词族）→ 说明。"""
    if not ja:
        return None
    for jp, allowed in ORGAN_MAP:
        if jp in ja and not any(a in (zh or "") for a in allowed):
            return "ja 含 %s，中文无「%s」" % (jp, "/".join(allowed))
    return None


def group_text(rows, segs, i, bridge_prev=False):
    """第 i 行所属 **ASR 段组**的 (日文全文, 中文全文, 行号列表)（2026-10-22，坑 94）。

    翻译在 **cue 层**做，显示层再把一条 cue 拆成多行 → 任何「单行 vs 单行」的判据
    都会被拆分骗到。段组 = 与该行时间窗重叠的所有 ASR 段覆盖的范围。

    `bridge_prev`（2026-10-26，坑 143）：把**前一条显示行**也算进中文侧——
    仅当它的时间窗 [t_prev, t_i) 与段组起点重叠（即这条显示行跨了段组边界）。
    起因：RJ9000007 轨07 80.44s 的显示行同时覆盖 ASR 的 `バカすぎ` 与
    `結婚詐欺…` 两段，段组起点 81.78 落在它**内部** → 按「行起点落在段组内」
    取中文时它被排除，「婚」字（在上一行）就丢了 → `kekkon_confusion` 误报
    （全库唯一命中就是这条，实际译文完整）。默认关，只有需要「一句话必须
    整体可见」的规则（如 kekkon）才开——改默认值会动到 neg/question/
    omit_suspect 的已校准口径。
    """
    t = rows[i][0]
    t1 = rows[i + 1][0] if i + 1 < len(rows) else t + 3.0
    ov = [s for s in segs if s["start"] < t1 and s.get("end", s["start"]) > t]
    if not ov:
        return None, None, []
    g0 = min(s["start"] for s in ov)
    g1 = max(s.get("end", s["start"]) for s in ov)
    ja = "".join(s.get("text", "") for s in segs if g0 - 0.01 <= s["start"] < g1)
    idx = [k for k, (tt, _) in enumerate(rows) if g0 - 0.01 <= tt < g1]
    if bridge_prev and idx:
        k = idx[0] - 1
        # 前一行的时间窗末端 = 本行起点；它伸进了段组（起点晚于 g0）才算跨边界
        if k >= 0 and rows[idx[0]][0] > g0 + 0.01 and (rows[k][1] or "").strip():
            idx.insert(0, k)
    zh = "".join(rows[k][1] for k in idx)
    return ja, zh, idx


def omit_group_ratio(rows, segs, i):
    """第 i 行的 **ASR 段组**中/日长度比（2026-10-22 Round 6，坑 94）。

    为什么需要：翻译在 **cue 层**做，显示层再把一条 cue 拆成多行。
    单行比 `omit_ratio` 会把「一条 cue 拆成 3 行」的**正常拆分**判成漏译
    （实测 142 条里 76 条是这种，ja 窗口跨了整条 cue，中文只是其中一句）。
    → 只有**段组级**也偏短才算真漏译。
    """
    ja, zh, _ = group_text(rows, segs, i)
    if ja is None:
        return None
    return omit_ratio(ja, zh)


def find_dup_blocks(rows, gap=3, tol=1.5, min_len=4):
    """整块复制：连续多行按**同一时间偏移**重复（2026-10-22 Round 9，坑 97）。

    RJ9000015 / RJ9000010 / RJ9000016 实录：装配环节把一段中文复制到了后面的时间点，
    导致 5–8 行字幕与音频完全无关（日文相似度 0.07–0.19，中文却一模一样）。
    为什么必须要求「偏移一致 + 连续 ≥2 行」：
      · 单行重复是 ASMR 常态（好舒服/喜欢喜欢），偏移各不相同 → 不能报；
      · 整块复制是**同一个错误平移量**，这是它唯一的指纹。
    返回 [[(i, j, delta), ...], ...]，i 是后出现的位置，j 是原始位置。
    """
    idx, pairs = {}, []
    for i, (t, zh) in enumerate(rows):
        k = re.sub(r"[^\u4e00-\u9fff]", "", zh or "")
        if len(k) < min_len:
            continue
        if k in idx and i - idx[k][0] >= gap:
            j, tj = idx[k]
            pairs.append((i, j, t - tj))
        else:
            idx.setdefault(k, (i, t))
    blocks, run = [], []
    for p in pairs:
        if run and p[0] == run[-1][0] + 1 and abs(p[2] - run[-1][2]) <= tol:
            run.append(p)
        else:
            if len(run) >= 2:
                blocks.append(run)
            run = [p]
    if len(run) >= 2:
        blocks.append(run)
    return blocks


RE_CLAUSE_SEP = re.compile(r"[。、…！!？?]")
RE_ZH_CLAUSE_SEP = re.compile(r"[。，？！!]")


def _clauses(s, seps):
    """按标点切从句，只保留有实质内容（≥2 个字/假名）的。"""
    return [c for c in seps.split(s or "")
            if len(re.sub(r"[^\u4e00-\u9fff\u3040-\u30ff]", "", c)) >= 2]


def omit_clause_gap(rows, segs, i, min_clauses=3, min_ratio=0.75):
    """从句覆盖缺口：段组内日文从句数 ≥3 而中文从句明显更少（2026-10-23 Round 16，坑 103 的落地）。

    为什么需要：`omit_ratio` 比**总字符数**，分不清「敬语压缩」（正确）和「整句漏译」（缺陷）
    ——Round 15 实测同一个比值两种情况都有。改用**从句覆盖**：
    日文 5 个从句、中文只译出 2 个 → 真有从句被丢掉。
    必须用**段组级**中文（group_text）：翻译在 cue 层做，中文分散在同组多个显示行里。
    """
    ja_g, zh_g, _ = group_text(rows, segs, i)
    if not ja_g or not zh_g:
        return False
    jc = _clauses(ja_g, RE_CLAUSE_SEP)
    if len(jc) < min_clauses:
        return False                      # 1–2 从句的短句不判（误报源）
    zc = _clauses(zh_g, RE_ZH_CLAUSE_SEP)
    return len(zc) / float(len(jc)) < min_ratio


def cue_too_long(zh, dur, cap=None, cue_left=None):
    """单行停留太久且行内含句末标点 → 一条字幕挂了多句（显示层观感问题）。

    纯重复行（「嗯！嗯！嗯！…」）豁免——那是拟声，不是多句。

    ⚠️ 2026-10-23 R23 收窄（坑 106）：LRC 行显示到**下一行时间戳**为止，
    段间静音 gap（全库中位 ~2.8s）被算进停留 → 旧口径 410 条里 294 条是
    「cue 自身 <5.5s、超时全来自 gap」的假象。新口径要求停留同时超过
    **阅读时间**（cues.hold_cap）与**该行语音剩余**（cue 尾 - 行起点）
    `LINGER_TOL` 秒以上——即「挂静音」而非「语音长」。幻觉段（`おやすみ
    なさい` 类静音填充）只按阅读时间算。实测 410 → 43。
    """
    if dur < LINE_LONG_SEC:
        return False
    if is_rep(zh):
        return False
    if sum(1 for ch in zh if ch in EOS_ALL) < 2:
        return False
    if cap is None:                        # 无 ASR 证据时退回旧口径
        return True
    intended = max(cap, cue_left or 0.0)
    return dur > intended + LINGER_TOL


def is_moan_ja(ja):
    """日文是否为「纯气声串」：`お…あ…あ…足…` / `お…お…` / `もぅ…` / `んん…`。

    判据：① **必须带「…」**（拖音才像气声；`もう。` 是陈述句「已经」，不算）；
          ② 按「…」切出的**每一段**满足其一——
      · **≤1 字**（お/あ/足/ひ…），或
      · **全部由气声假名组成且 ≤6 字**（もぅ/うぇぁ/んん/もぅもぅ）。
    第②条第二支很关键：`もぅ…`（もう＝已经）会借字面义混进来，去掉就抓不到。
    ⚠️ `ん…わかった…`、`ひ…ひらくな…`、`お…明るい…` 都含**真词**（わかった/ひらく/明るい），
       必须判为非气声，别误伤（`test_regressions.py` 的 MOAN_NEGATIVE 固化了这几条反例）。
    """
    j = (ja or "").strip()
    if not j or "…" not in j:                       # ①
        return False
    runs = [r.strip() for r in RE_ELL.split(j) if r.strip()]
    if not runs:
        return False
    for r in runs:                                   # ②
        if len(r) <= 1:
            continue
        if len(r) <= 6 and all(c in MOAN_KANA for c in r):
            continue
        return False
    return True


def moan_drift(ja, zh):
    """气声串的中文里出现 ≥2 个「非语气词」内容字 → 疑似被上下文带跑。

    `お…あ…あ…足…` → ✗「妈…妈…妈…脚…」（4 个内容字）／✓「哦…啊…啊…脚…」（0 个）。
    """
    if not is_moan_ja(ja):
        return False
    return sum(1 for c in norm(zh) if c not in ZH_INTERJ) >= 2


def zh_collapse(ja, zh, zh_group=None):
    """**内容塌缩**：中文只剩 1–2 个实义字，日文却是一整句（2026-10-22 RJ9000014 实录）。

    为什么需要：说话人名泄漏（坑 91）让 74 行整行只剩「甘/白」，而现有规则全放过了——
    1 字行不算空行/日文残留/纯标点，`omit_suspect` 又要求日文 ≥12 字。
    判据（三条同时满足，宁窄不宽）：
      ① 中文实义字 ≤2；② 日文假名+汉字 ≥10 **且含 ≥2 汉字**（排除气声串/短应答）；
      ③ 不是公式化寒暄（おやすみ/ありがとう…）、不是疑问（嗯？/是吗？）、不含拉丁字母。
    ⚠️ 别放宽 ②：`先生`→「老师」、`気持ちいい`→「舒服」这类**短句短译是对的**。
    ⚠️ R32（2026-10-23）：加段组判据——显示层会把一个 cue 拆成多行，首行可能只剩
      连接词（RJ9000007 freetalk「所以 / 这次这部作品的女主角 / 也由我来饰演」）。
      翻译在 cue 层做，**只有段组整体**也 ≤2 实义字才算塌缩（同坑 94/103 口径）。
    """
    nz = len(re.sub(r"[^\u4e00-\u9fff]", "", zh))
    if nz > 2:
        return False
    if zh_group is not None and len(re.sub(r"[^\u4e00-\u9fff]", "", zh_group)) > 2:
        return False                      # 段组有其他内容 → 显示拆分，不是塌缩
    nj = len(re.findall(r"[\u3040-\u30ff\u4e00-\u9fff]", ja))
    nk = len(re.findall(r"[\u4e00-\u9fff]", ja))
    if nj < 10 or nk < 2:
        return False
    if RE_FORMULAIC.search(ja):
        return False
    if re.search(r"[？?]|吗|呢|吧", zh):
        return False
    if re.search(r"[A-Za-z]", zh):
        return False
    return True


# 公式化寒暄：短译是对的（晚安/谢谢/早安…），不算塌缩
RE_FORMULAIC = re.compile(r"おやすみ|ありがとう|おはよう|いらっしゃい|いただきます"
                          r"|ごちそうさま|おめでとう|ごめん|すみません")


def ono_guess(ja, zh):
    """日文是看不懂的拟声串，中文却出现「电击/鞭打」这类脑补名词 → 疑似编造。

    实测 `僕がチパチパされている間` 被译成「我被**电击**的时候」——ASR 里压根没有电击。
    判据：① 日文有 ≥4 字的片假名 run（チパチパ/ピチャピチャ…）；
          ② 中文出现 GUESS_NOUNS 里的词。
    """
    if not any(len(r) >= 4 for r in KATA_RUN.findall(norm(ja))):
        return False
    return [n for n in GUESS_NOUNS if n in zh]


# ---- 小写假名 / 浊音变体标记（A3，GPT 第六轮 Q2）----
# ⚠️ **只标记、不自动归一化**：变体可能是角色刻意的口齿不清，归一化会把表演抹掉。
# ⚠️ 浊音差（ちんぼ→ちんぽ）只算「近音风险」，**绝不能当气声遮蔽**。
_KV = None


def _kv():
    """惰性加载 `style/kana_variants.json`。

    ⚠️ R26（2026-10-23）：`small` 表 51 条里 **47 条是纯拖音**（変体去掉「ー」
    就等于标准形，如 `ってー`/`いーっぱい`/`ゆっくりー`）——ASMR 抽咽/拖长音是
    **表演本身**不是 ASR 变体（实测 17 条命中全是这类，译文也都正确渲染了拖音）。
    这里过滤掉纯拖音，只保留真小写假名替换（じゃぁ/あぁー/いっも/ぅーんー）。
    ⚠️ `voice` 表（浊音近音，63 条）**不参与报告**：短变体子串匹配误报率 ~90%
    （`ぱい` 命中 `いっぱい`、`くで` 命中 `近くで`），要启用须先做上下文感知重挖
    （记入 knowledge gap；`ちんぼ`→`ちんぽ` 15 处是真 ASR 错听但译文均被上下文
    救回，暂无缺陷）。
    """
    global _KV
    if _KV is None:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..",
                        "style", "kana_variants.json")
        p = os.path.normpath(p)
        try:
            d = json.load(open(p, encoding="utf-8"))
            _KV = ({r["variant"] for r in d.get("small", [])
                    if r["variant"].replace("ー", "") != r["standard"]},
                   {r["variant"] for r in d.get("voice", [])})
        except Exception:
            _KV = (set(), set())
    return _KV


def kana_variant(ja):
    """日文里含「疑似 ASR 小写假名变体」或「浊音近音变体」。

    返回 (小写变体列表, 浊音近音变体列表)。都只是**风险标记**。
    """
    small, voice = _kv()
    runs = KATA_RUN_ALL.findall(ja or "")
    a = sorted({r for r in runs if r in small})
    b = sorted({r for r in runs if r in voice})
    return a, b


def asr_timing_anomaly(segs):
    """找出**时长/字数比不可能**的 ASR 段（2026-10-15，用户试听后定位）。

    这是**不需要重跑 ASR** 就能拿到的幻觉信号——ASR 没存 `no_speech_prob`，
    但「段时长 ÷ 字数」本身就够用：

      · **段 ≥3 秒却 <1 字/秒** → 要么是幻觉（在非语音上编出词），
        要么是**段尾延伸进静音**（人说话 3–8 字/秒，不可能 0.2 字/秒）。
        实测 `ぎゅぎゅぎゅ`（5 字）跨 **28.4 秒**、`おやすみなさい` 跨 **24.7 秒**、
        `くっ くっ` 跨 16.2 秒 —— **正是用户投诉的那几处**。
      · **段 <0.35 秒** → 短到不可能，也是幻觉。

    返回 [(类型, start, end, 字/秒, text)]，按可疑度降序。
    """
    out = []
    for s in segs:
        t = (s.get("text") or "").strip()
        dur = s["end"] - s["start"]
        if not t or dur <= 0:
            continue
        cps = len(t) / dur
        if dur >= 3.0 and cps < 1.0:
            out.append(("slow", s["start"], s["end"], cps, t))
        elif dur < 0.35:
            out.append(("tiny", s["start"], s["end"], cps, t))
    return out


# ---- ASR 疑似非语音段：P2 两阶段规则（2026-10-15，GPT 第七轮）----
# 基线来自**6305 个正样本**（人工确实放了字幕的段，见 ASR_WEAK_LABELS.md）：
#   段时长   p10 1.08 / 中位 2.60 / **p90 6.76** 秒
#   字符密度 p10 **2.17** / 中位 4.14 / p90 6.41 字/秒
# 幻觉段的字符密度是 0.21–0.99 —— 与真实语音的 p10（2.17）**差一个数量级**。
ASR_DUR_P90 = 6.76         # 阶段 1：段时长超过真实语音 p90
ASR_CPS_LOW = 1.0          # 阶段 1：字符密度低于 1 字/秒
ASR_PHRASE_REP = 3         # 阶段 1：同轨同一短语重复 ≥3 次（幻觉爱循环同一句）
# 阶段 2：`no_speech_prob` 的**三档带**（2026-10-15 校准）。
# ⚠️ 只在 5 个样本上取过（1 真台词 + 4 幻觉），**不是统计出来的**。
#    实测它把 `くっ くっ`（nsp 0.085，用户确认是舔耳音幻觉）判成「保留」——
#    所以**中间留一大段不确定带**，只有两端敢下结论。
ASR_NSP_SUPPRESS = 0.45    # ≥ 此值 → 高非语音可信度（抑制）
ASR_NSP_KEEP = 0.10        # ≤ 此值 → 高语音可信度（保留）
# 中间 (0.10, 0.45) → `uncertain`，**必须人工听**，不要自动处置。
# ⚠️ 阶段 1 会**误伤真台词**（段尾拖进静音，如 `あ、気持ちよかったですか?` 26.6s）。
#    所以它只标 `asr_suspect` **告警**，绝不自动删；要抑制必须过阶段 2。


def asr_suspect(segs, conf=None):
    """P2 两阶段路由：这段到底该不该出语言字幕。

    ⭐ 目标不是 `contains_any_sound`，而是 `should_display_linguistic_text`——
    人工字幕留空 ≠ 没声音，只表示「不值得显示语言字幕」（GPT 第七轮）。

    `conf`: 可选的 {start_round2: no_speech_prob}，来自 `asr_conf/`（只有 12 轨试点有）。

    返回 [(tier, start, dur, cps, rep, nsp, text)]：
      · `suppress` 高非语音可信度 → 不出字幕、保留时间空档
      · `tail`     真台词但段尾拖长 → **保留**，只缩短显示（`cues.cap_holds()` 已在做）
      · `suspect`  不确定 → 只告警（历史作品没有置信度时全落这里）
    """
    cnt = Counter((s.get("text") or "").strip() for s in segs)
    out = []
    for s in segs:
        t = (s.get("text") or "").strip()
        dur = s["end"] - s["start"]
        if not t or dur <= 0:
            continue
        cps = len(t) / dur
        rep = cnt[t]
        # ⚠️ **必须同时「够长」**。单用 `rep >= 3` 会把 `ぎゅ`(0.7s)、`ゼロゼロゼロ`(1.8s)
        #    这些**真实的重复拟声**全标上（实测 RJ9000002 トラック2 一次标出 18 段）。
        #    幻觉的特征是「长 **且** 内容稀」，所以长是前置条件。
        if dur <= ASR_DUR_P90:
            continue
        if not (cps < ASR_CPS_LOW or rep >= ASR_PHRASE_REP):
            continue
        nsp = None
        if conf:
            # 置信度是按「重跑后的分段」算的，两边分段不同 → 只能按时间就近取
            best = None
            for (cs, ce, v) in conf:
                if ce >= s["start"] and cs <= s["end"]:
                    if best is None or abs(cs - s["start"]) < abs(best[0] - s["start"]):
                        best = (cs, v)
            nsp = best[1] if best else None
        if nsp is None:
            tier = "suspect"                      # 没置信度 → 只告警
        elif nsp >= ASR_NSP_SUPPRESS:
            tier = "suppress"                     # 高非语音可信度
        elif nsp <= ASR_NSP_KEEP:
            tier = "tail"                         # 高语音可信度：真台词段尾拖长 → 保留
        else:
            tier = "uncertain"                    # 中间带 → **必须人工听**
        out.append((tier, s["start"], dur, cps, rep, nsp, t))
    return out


def load_conf(rj, track):
    """读 `asr_conf/` 的置信度（只有 12 轨试点重跑过）。返回 [(start, end, nsp)]。

    ⚠️ `track` 可以是**片段**（和 `load_asr` 一样按子串匹配）——
       我们平时传的是 `トラック1` 这种 key，不是完整文件名。
    """
    out = None
    for jf in glob.glob(os.path.join(ASR_ROOT, rj, "asr_conf", "*.json")):
        base = os.path.basename(jf)[:-5]
        if track == base or track in base:
            try:
                d = json.load(open(jf, encoding="utf-8"))
            except Exception:
                continue
            out = [(s["start"], s["end"], s.get("no_speech_prob"))
                   for s in d.get("segments", []) if s.get("no_speech_prob") is not None]
            break
    return out


def norm(s):
    return "".join(c for c in (s or "") if c not in PUNCT)


def is_rep(s):
    """整体是叠词/拟声（白名单豁免用）。"""
    return bool(RE_UNITREP.match(norm(s)))


def parse_lrc(path):
    # R23（坑 106）：空文本行 = 清屏 spacer（读完即走），**必须留在 rows 里**——
    # 停留量程（t1-t）要用「下一行时间戳」（含 spacer）才是真实显示时长；
    # 内容规则在主循环顶部 `if not zh: continue` 跳过。
    rows = []
    for ln in open(path, encoding="utf-8"):
        m = re.match(r"\[(\d+):(\d+\.\d+)\](.*)", ln.strip())
        if m:
            rows.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
    return rows


def load_asr(rj, track, sub="asr"):
    """读指定版本目录的 ASR json。`sub` ∈ {"asr"（v2 重跑版，现行）, "asr_v1_20261020"（v1）}。

    先按精确文件名（v1 文件名带 .mp3，v2 不带），再按 `source` 字段回退匹配。
    """
    base = os.path.join(ASR_ROOT, rj, sub)
    p = os.path.join(base, track + ".json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    for jf in glob.glob(os.path.join(base, "*.json")):
        try:
            d = json.load(open(jf, encoding="utf-8"))
        except Exception:
            continue
        if d.get("source") == track:
            return d
    return None


# ---- ASR 版本自动选择（2026-10-22，坑 90）----
# 成品 LRC 与 `draft/asr`（v2 重跑版）**不是一一对应**：实测 16 部成品里 12 部
# 时间戳对齐 v1（asr_v1_20261020）、4 部对齐 v2，且 RJ9000005 是**逐轨混合**
# （フリートーク轨 v2 匹配 0.965 vs v1 0.452，其余轨 v1）。v2 重跑后只重做了
# 部分作品的翻译/交付（记忆：`draft/draft` 仍是 v1），scan 却固定读 v2 →
# ja_in_range 取到无关文本，全部 ja 配对类规则（neg/question/listener/
# person_flip/moan_drift…）误报（实测 listener_marker 10 条里 6 条是版本错配）。
# 这里逐轨按「LRC 时间戳 vs 词级起始时间」匹配率选版本；平分优先 v1（12/16 的基率）。
ASR_VERSION_DIRS = ("asr_v1_20261020", "asr")
VER_STATS = Counter()


def _word_starts(d):
    ts = []
    for s in (d or {}).get("segments") or []:
        ws = s.get("words") or []
        if ws:
            ts.extend(w["s"] for w in ws)
        else:
            ts.append(s.get("start", 0.0))
    return sorted(ts)


def _ts_match_rate(rows_ts, wts, tol=0.15):
    """LRC 行时间戳能对上词级起始时间的比例（cue 起点=词起点，停顿 flush 行除外）。"""
    if not rows_ts or not wts:
        return -1.0
    hit = 0
    for t in rows_ts:
        i = bisect.bisect_left(wts, t - tol)
        while i < len(wts) and wts[i] <= t + tol:
            if abs(wts[i] - t) <= tol:
                hit += 1
                break
            i += 1
    return hit / len(rows_ts)


def pick_asr(rj, track, rows):
    """逐轨选 ASR 版本，返回 (data, version_tag)；两者都无则 (None, None)。

    v2 需比 v1 匹配率高出 0.02 才胜出（平分保留先遍历的 v1）。
    """
    rows_ts = [t for t, _ in rows]
    best = None
    for sub in ASR_VERSION_DIRS:
        d = load_asr(rj, track, sub)
        if not d:
            continue
        rate = _ts_match_rate(rows_ts, _word_starts(d))
        if best is None or rate > best[2] + 0.02:
            best = (d, sub, rate)
    if best is None:
        return None, None
    VER_STATS[best[1]] += 1
    return best[0], best[1]


def ja_in_range(segs, t0, t1):
    out = []
    for s in sorted(segs, key=lambda z: z["start"]):
        ws = s.get("words") or []
        if ws:
            for w in ws:
                if t0 - 0.25 <= w["s"] < t1 - 0.05:
                    out.append(w["w"])
        elif t0 - 0.25 <= s["start"] < t1 - 0.05:
            out.append(s["text"])
    return "".join(out).strip()


# ---- 归属结构层后检测（GPT 第九轮 Q1；逻辑在 pragmatics_hints.check_pair）----
_PRAG = [None]


def prag_check_pair(ja, zh):
    """懒加载 pragmatics_hints；模块不可用则静默返回 []（不影响扫描）。"""
    if _PRAG[0] is None:
        try:
            import pragmatics_hints as _ph
            _PRAG[0] = _ph.check_pair
        except Exception:
            _PRAG[0] = False
    return _PRAG[0](ja, zh) if _PRAG[0] else []


# ---- 幻觉段豁免（2026-10-22）----
# ja 段是 asr_halluc 判定的幻觉（`おやすみなさい` 独占 20s 一类，Whisper 静音填充词）时，
# 译文「…」是**正确处理**（pipeline 已把幻觉段换成清屏行），punct_only 等规则不应再报。
_HALLUC = [None]


def halluc_hit(seg):
    """段命中 asr_halluc 判定则返回记录，否则 None；模块不可用返回 None（不豁免）。"""
    if _HALLUC[0] is None:
        try:
            import asr_halluc as _ah
            _HALLUC[0] = _ah.scan_one
        except Exception:
            _HALLUC[0] = False
    return _HALLUC[0](seg) if _HALLUC[0] else None


# ---- 说话人名泄漏（R36，2026-10-23；判据在 polish.spk_leak）----
# 对 asr json 带 spk 的轨（目前仅 RJ9000014 台本轨）逐行检查 5 种泄漏格式。
# R35 起「行首 X：」是规范前缀不算泄漏；本规则对未来台本轨常备。
_SPKLEAK = [None]


def spk_leak_hit(zh, spk):
    if not spk:
        return False
    if _SPKLEAK[0] is None:
        try:
            import polish as _pl
            _SPKLEAK[0] = _pl.spk_leak
        except Exception:
            _SPKLEAK[0] = False
    return _SPKLEAK[0](zh, spk) if _SPKLEAK[0] else False


def span_zh(rows, t0, t1):
    """一个 cue 时间跨度内的全部显示行中文拼接（显示层可把 cue 切成多行）。"""
    return "".join(z for t, z in rows if t0 - 0.05 <= t <= t1 + 0.05)


def ngrams(s, n):
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def leak_rules(zh_a, zh_b, ja_a=None, ja_b=None):
    """相邻两行 (前, 后) 的**跨行泄漏**检测。返回规则名或 None。

    被 `test_regressions.py` 直接 import —— 别在这里改口径而不改测试。

    R42（2026-10-23）：传入两侧日文时，**日文相同 = 角色真的复读**（ASMR 极常见，
    「くりちゃん、きもし」×2 / 「腰が勝手に浮いちゃう」×3），中文相同是忠实翻译，
    不是泄漏 → 直接放过。37 条里 12 条属于这一类，是规则最大的误报源。
    """
    if ja_a and ja_b and norm(ja_a) == norm(ja_b):
        return None
    nzh, nb = norm(zh_a), norm(zh_b)
    if len(nb) >= 4 and nb in nzh and len(nzh) > len(nb):
        return "len_imbalance" if (len(nzh) >= 14 and len(nb) <= 7) else "dup_suffix"
    # n-gram 重叠：只对**足够长**的两行才判（短串的 n-gram 集合太小，重叠率会假性 100%）
    if min(len(nzh), len(nb)) >= 6 and not is_rep(zh_b):
        for n in (4, 5, 6):
            A, B = ngrams(nzh, n), ngrams(nb, n)
            if A and B and len(A & B) / min(len(A), len(B)) >= 0.75:
                return "dup_ngram"
    return None


def punct_only_loss(ja, zh):
    """整行只有标点、而**日文侧确有实义字符** → 疑似丢句（2026-10-19）。

    与 `polish.bad_cell` 的同名判据必须一致（那边命中会回退初译，这里只负责扫出来）。
    ⚠️ 日文侧是 `…` / 纯气声时不判——那种情况下纯标点是**忠实翻译**，
       而且这些行还在兼作长静默处的「清屏行」（删掉会让上一行挂 20–40 秒）。
    ⚠️ 2026-10-22：ja 段是幻觉（`おやすみなさい` 独占 20s，asr_halluc A 类）时
       也由调用方豁免——那时「…」是 pipeline 的正确处理，不是丢句。
    ⚠️ R33（2026-10-23）：docstring 承诺的「纯气声不判」一直没实现（9 条
       `ばー/あぁん` 类 accepted 边界全在误报）——用 `_is_pure_sound` 补上。
    """
    if not zh or not RE_PUNCT_ONLY.match(zh.strip()):
        return False
    if _is_pure_sound(ja):
        return False
    return bool(RE_JA_LEX.search(ja or ""))


# 纯气声/拟声短串：整句只由气声字组成（含 ばー 呕吐声、おっ/ぅっ 用力声）。
# 这些段译成「…」是忠实翻译 + 兼作清屏行（docs 结论 #34），不算丢句。
_SOUND_KANA = MOAN_KANA | {"ば", "ー"}


def _is_pure_sound(ja):
    core = re.sub(r"[^ぁ-んァ-ヶー]", "", ja or "")
    return bool(core) and len(core) <= 4 and all(c in _SOUND_KANA for c in core)


def scan(only=None):
    """only：作品号集合，只扫这些（run 级口径）。None = 全库。"""
    V = []
    for rjdir in sorted(glob.glob(os.path.join(OUT_ROOT, "RJ*"))):
        rj = os.path.basename(rjdir)
        if only and rj not in only:
            continue
        for lrc in sorted(glob.glob(os.path.join(rjdir, "*.lrc"))):
            track = os.path.basename(lrc)[:-4]
            rows = parse_lrc(lrc)
            d, asr_ver = pick_asr(rj, track, rows)
            segs = (d or {}).get("segments", [])
            # 本轨高频片假名 run（≥3 次）→ 角色名/常用外来语，音译检测时豁免
            # ---- P2：ASR 疑似非语音段（独立路由，2026-10-15）----
            # 只告警、**不自动删**：阶段 1 会误伤「真台词段尾拖长」，
            # 阶段 2 的阈值又只在 5 个样本上取过（实测把 `くっ くっ` 判成保留）。
            # 所以这里只产出候选，处置交给人工。
            _conf = load_conf(rj, track)
            for tier, st0, dur0, cps0, rep0, nsp0, txt0 in asr_suspect(segs, _conf):
                if tier == "tail":
                    continue                       # 真台词，只是段尾长 → 不算问题
                # R25（2026-10-23）：用 asr_halluc 判据给弱信号分层——
                # A_halluc_word = 置信幻觉（应清屏）；B_ts_collapse = 真文本+
                # 词级时间戳崩塌（译文可信，停留由 cap_holds/spacer 管）。
                # 实测 194 条 = 179 真台词（弱信号固有 FP）+ 15 幻觉确认。
                _seg0 = next((s for s in segs
                              if abs(s.get("start", -99) - st0) < 0.35
                              and (s.get("text") or "").strip() == (txt0 or "").strip()),
                             None)
                _hrec = halluc_hit(_seg0) if _seg0 is not None else None
                _tag = ("【幻觉确认·%s】" % ("A填充词" if _hrec["kind"] == "A_halluc_word"
                                        else "B时间戳崩塌")) if _hrec else ""
                V.append(dict(rj=rj, track=track, start=st0, ja=txt0, zh="",
                              asr_ver=asr_ver,
                              rule="asr_suspect",
                              detail="ASR 段 %s%s（%.1fs，%.2f 字/秒，同短语×%d，nsp=%s）"
                                     % (_tag,
                                        {"suppress": "疑似非语音",
                                         "uncertain": "不确定（要人工听）",
                                         "suspect": "无置信度，疑似非语音"}[tier],
                                        dur0, cps0, rep0,
                                        "%.3f" % nsp0 if nsp0 is not None else "—")))

            allja = "".join(w["w"] for s in segs for w in (s.get("words") or [])) \
                or "".join(s.get("text", "") for s in segs)
            _c = Counter(KATA_RUN.findall(norm(allja)))
            common_runs = {r for r, c in _c.items() if c >= KATA_NAME_FREQ}
            # 本轨高频中文 bigram（≥3 次）→ 角色名的固定音译（玛雅/莉亚），音译检测时豁免
            _z = Counter()
            for _, _zz in rows:
                nz = norm(_zz)
                for k in range(len(nz) - 1):
                    _z[nz[k:k + 2]] += 1
            common_zh = {g for g, c in _z.items() if c >= KATA_NAME_FREQ}
            # 段组预计算（omit 的长度比 + neg/question 的跨行聚合，坑 94/96）
            # 整块复制（坑 97）：装配层把一段中文复制到后面时间点
            for _blk in find_dup_blocks(rows):
                for _i, _j, _d in _blk:
                    _t = rows[_i][0]
                    _t1 = rows[_i + 1][0] if _i + 1 < len(rows) else _t + 3.0
                    V.append({"rj": rj, "track": track, "start": round(_t, 2),
                              "ja": ja_in_range(segs, _t, _t1), "zh": rows[_i][1],
                              "asr_ver": asr_ver, "rule": "dup_block",
                              "detail": "与 %.1fs 处同文（偏移 %+.1fs，疑似整块复制）"
                                        % (rows[_j][0], _d)})
            _grp, _gzh, _gsz, _cgap = [], [], [], []
            for k in range(len(rows)):
                _ja_g, _zh_g, _idx_g = group_text(rows, segs, k)
                _gzh.append(_zh_g or "")
                _gsz.append(len(_idx_g))
                _grp.append(omit_ratio(_ja_g, _zh_g) if _ja_g else None)
                _cgap.append(omit_clause_gap(rows, segs, k))
            # ---- kekkon_confusion（2026-10-25，T6）：結婚/結構/血行 同音混淆 ----
            # 按段组判 + 按 idx 列表去重（每 distinct 组只报一条，锚在组首行）。
            # 不加「k 必须是组首」的守卫：行时间戳可能早于段起点（cue 锚在词起点），
            # 那时 _ki[0] 是后面的行，按 k==_ki[0] 判会把整组漏掉。
            # 独立成块不进下方逐行循环：不动存量规则的执行路径，保证默认行为不变。
            _seen_grp = set()
            for k in range(len(rows)):
                _kj, _kz, _ki = group_text(rows, segs, k, bridge_prev=True)
                if not _ki or not _kj or tuple(_ki) in _seen_grp:
                    continue
                _seen_grp.add(tuple(_ki))
                _why = kekkon_confusion(_kj, _kz)
                if _why:
                    V.append({"rj": rj, "track": track, "start": round(rows[_ki[0]][0], 2),
                              "ja": _kj, "zh": _kz or "", "asr_ver": asr_ver,
                              "rule": "kekkon_confusion", "detail": _why})
                # 器官术语对不上（2026-10-26 R132）：同一段组级口径，独立计数
                _why2 = organ_term_mismatch(_kj, _kz)
                if _why2:
                    V.append({"rj": rj, "track": track, "start": round(rows[_ki[0]][0], 2),
                              "ja": _kj, "zh": _kz or "", "asr_ver": asr_ver,
                              "rule": "organ_term_mismatch", "detail": _why2})
            for i, (t, zh) in enumerate(rows):
                t1 = rows[i + 1][0] if i + 1 < len(rows) else t + 3.0
                # R23（坑 106）：清屏 spacer（空文本行）不是内容，跳过全部规则；
                # 但它参与上一行的停留计算（t1 已含它）。
                if not zh:
                    continue
                ja = ja_in_range(segs, t, t1)
                nzh = norm(zh)
                base = {"rj": rj, "track": track, "start": round(t, 2), "ja": ja, "zh": zh,
                        "asr_ver": asr_ver}

                # --- 源语必要条件 ---
                # 纯 filler 行（もうなんか / ほら）没有命题，不判否定/疑问
                _filler_only = is_filler_only(ja) if ja else False
                if ja:
                    _ja_neg = ja
                    for _w in NEG_JA_STRIP:
                        _ja_neg = _ja_neg.replace(_w, "")
                    # R38：正则型剥除（こあそど+んない 边界 / 句尾じゃない? / ませんか）
                    for _rx, _rep in NEG_JA_RESTRIP:
                        _ja_neg = _rx.sub(_rep, _ja_neg)
                    # 跨行聚合（坑 96）：cue 被拆成多行时，否定可能落在邻行中文里
                    _neg_x = _gsz[i] > 1 and RE_NEG_ZH.search(_gzh[i])
                    _poss = neg_possible_exempt(_ja_neg, zh)
                    if (not _filler_only and not _poss and RE_NEG_JA.search(_ja_neg)
                            and not RE_NEG_ZH.search(zh) and not _neg_x
                            and not (NEG_MODAL_JA.search(_ja_neg)
                                     and NEG_MODAL_ZH.search(zh))):
                        V.append(dict(base, rule="neg_missing", detail="日文有否定，中文无否定词"))
                _ja_q = ja
                for _w in Q_JA_STRIP:
                    _ja_q = _ja_q.replace(_w, "")
                # R38：反问断言（じゃない? / なんじゃない）不是疑问
                for _rx, _rep in Q_JA_RESTRIP:
                    _ja_q = _rx.sub(_rep, _ja_q)
                if ja and not _filler_only and RE_Q_JA.search(_ja_q) and not RE_Q_ZH.search(zh):
                    # 跨行聚合（坑 96）：疑问标记可能落在同组邻行
                    if not (_gsz[i] > 1 and RE_Q_ZH.search(_gzh[i])):
                        V.append(dict(base, rule="question_missing", detail="日文疑问，中文无疑问标记"))
                _nm = number_mismatch(
                    ja, zh,
                    ja_ctx=(ja_in_range(segs, rows[i - 1][0], t) if i > 0 else "") +
                           (ja_in_range(segs, t1, rows[i + 2][0])
                            if i + 2 < len(rows) else ""),
                    zh_ctx=(rows[i - 1][1] if i > 0 else "") +
                           (rows[i + 1][1] if i + 1 < len(rows) else ""))
                if _nm:
                    V.append(dict(base, rule="number_mismatch",
                                  detail="日文数字 %s vs 中文 %s" % (_nm[0], _nm[1])))
                if ja and any(c in ja for c in CONFUSABLE):
                    V.append(dict(base, rule="confusable", detail="命中近音混淆词：%s" % [c for c in CONFUSABLE if c in ja]))

                # --- 2026-10-14 新增 3 类 ---
                r = omit_ratio(ja, zh)
                # 单行偏短但**段组级正常** → 是 cue 被拆成多行的正常显示拆分，不是漏译
                # 三道判据（坑 94/103）：长度比 + 段组长度比 + **从句覆盖缺口**。
                # 第三道是 Round 16 加的：只有「日文 ≥3 从句而中文从句明显更少」才算疑似漏译，
                # 把 60 条「敬语压缩但语义完整」的正确译法挡掉（68 → 13）。
                if (r is not None and r < OMIT_RATIO
                        and (_grp[i] is None or _grp[i] < OMIT_RATIO)
                        and _cgap[i]):
                    V.append(dict(base, rule="omit_suspect",
                                  detail="中文/日文长度比 %.2f（段组 %.2f，从句覆盖不足，疑似漏译）"
                                         % (r, _grp[i] if _grp[i] is not None else -1)))
                # 纯标点丢句（2026-10-19）：模型整句只吐「…」/「？」，内容全丢
                # ⚠️ 这里必须用**起点精确匹配**的日文，不能用 `ja_in_range` 的合并结果：
                #    cue 边界落在某段中间时，`ja_in_range` 会把邻段的尾巴（`ろうか。`）
                #    算进来 → 把合法的停顿行误报（实测 RJ9000007 09:34 就是这条）。
                # ⚠️ 2026-10-22：该段是 asr_halluc 判定的幻觉（おやすみなさい独占 20s 一类）
                #    时不判——那时「…」是 pipeline 的正确处理（幻觉段→清屏行）。
                _ex_seg = next((s for s in segs
                               if abs(s.get("start", -99) - t) < 0.35), None)
                _ex = (_ex_seg or {}).get("text", "")
                if _ex_seg is not None and halluc_hit(_ex_seg):
                    pass
                # R33（2026-10-23）：段组判据——显示层把 cue 拆成多行时，首行可能
                # 只是开头的「…」（RJ9000003 Tr.03 实录：'……、うぅ、見ないで' 拆成
                # 「…」/「呜呜…」/「不要看我啦，」三行）。只有**段组中文也没有实义
                # 字**时才是真丢句（同坑 94/103/R32 口径）。
                elif (punct_only_loss(_ex, zh)
                      and not re.sub(r"[^\u4e00-\u9fff]", "",
                                     (_gzh[i] if _gzh and i < len(_gzh) else "") or "")):
                    V.append(dict(base, rule="punct_only",
                                  detail="整行只有标点、日文侧有实义内容（疑似丢句）"))
                if ja and onomat_translit(ja, zh, common_runs, common_zh):
                    V.append(dict(base, rule="onomat_translit",
                                  detail="日文为片假名拟声，中文无拟声字（疑似音译）"))
                # 内容塌缩（2026-10-22 新增，坑 91 的规则化）：整行只剩 1–2 字
                # R32：段组判据——显示拆分的首行（「所以」）不是塌缩
                if ja and zh_collapse(ja, zh, _gzh[i] if _gzh else None):
                    V.append(dict(base, rule="zh_collapse",
                                  detail="中文只剩 %d 个实义字，日文是整句（疑似内容塌缩）"
                                         % len(re.sub(r"[^\u4e00-\u9fff]", "", zh))))
                # R23（坑 106）：cue_too_long 收窄为「挂静音」判据——
                # 停留需同时超过阅读时间与该行语音剩余 LINGER_TOL 秒。
                _cs = next((s for s in segs
                            if s.get("start", 0) <= t < s.get("end", 0)), None)
                if _cs is not None and halluc_hit(_cs):
                    _left = 0.0            # 幻觉段：没有真语音，只按阅读时间
                else:
                    _left = (_cs["end"] - t) if _cs is not None else 0.0
                _cap = cues.hold_cap(zh)
                # R36：说话人名泄漏（仅 asr json 带 spk 的轨；R35 起行首 X： 是规范前缀）
                if zh:
                    _sl_seg = next((s for s in segs
                                    if s.get("start", 0) <= t < s.get("end", 0)), None)
                    _sl_spk = ((_sl_seg or {}).get("spk") or "").strip().strip("（）()")
                    if _sl_spk and spk_leak_hit(zh, _sl_spk):
                        V.append(dict(base, rule="spk_leak",
                                      detail="译文泄漏说话人名「%s」（R28 五格式；R35 起行首 X： 为规范前缀）"
                                             % _sl_spk))
                if cue_too_long(zh, t1 - t, _cap, _left):
                    V.append(dict(base, rule="cue_too_long",
                                  detail="单行停留 %.1fs（阅读 %.1fs + 语音 %.1fs + 挂静音 %.1fs，一行挂多句）"
                                         % (t1 - t, _cap, _left, t1 - t - max(_cap, _left))))
                if ja and ono_guess(ja, zh):
                    V.append(dict(base, rule="ono_guess",
                                  detail="日文只是拟声串，中文却出现 %s（疑似脑补）"
                                         % ono_guess(ja, zh)))
                if ja:
                    _sm, _vc = kana_variant(ja)
                    if _sm:
                        V.append(dict(base, rule="kana_variant",
                                      detail="日文含小写假名变体 %s（疑似 ASR 变体，**不归一化**）" % _sm))
                if ja and moan_drift(ja, zh):
                    V.append(dict(base, rule="moan_drift",
                                  detail="日文是纯气声串，中文出现 %d 个内容字（疑似被上下文带跑）"
                                         % sum(1 for c in norm(zh) if c not in ZH_INTERJ)))
                # 呼称人称反转（坑 84 通用化 + R24 收窄）：ja **无明确第一人称代词**
                # 且源含呼称词、译文含「我」。per-work 确认项——确认是呼称的作品走
                # 「源文预替换 ボク→あなた」，不是逐行改译文。
                if person_flip(ja, zh):
                    V.append(dict(base, rule="person_flip",
                                  detail="源含呼称词（ボク/君/お前/あんた）、无明确第一人称代词，"
                                         "译文含「我」（坑 84 签名；R24 收窄，severity=info）"))

                # --- 2026-10-17 新增：归属结构层后检测（GPT 第九轮 Q1）---
                # ⚠️ 必须用「该 cue 时间跨度内的**全部**显示行」拼接来判，
                #    不能只用当前行：显示层会把一个 cue 切成多条，
                #    「你」经常落在续行里（实测单行匹配假警报 15 条 → 聚合后 7 条）。
                if ja:
                    zg = span_zh(rows, t, t1)
                    for lv, rule, detail, _sub in prag_check_pair(ja, zg):
                        # GPT 第十轮 Q3（R4）：level ∈ high / soft。
                        # high → severity=high（门禁硬判）；soft → severity=low（只记录）。
                        V.append(dict(base, rule=rule,
                                      severity="high" if lv == "high" else "low",
                                      detail=detail))

                # --- 跨行泄漏 ---
                if i + 1 < len(rows):
                    # R42：把两侧日文一起传进去——日文相同说明是角色复读，不是泄漏
                    _t2 = rows[i + 2][0] if i + 2 < len(rows) else t1 + 6.0
                    _ja_next = ja_in_range(segs, t1, _t2)
                    r = leak_rules(zh, rows[i + 1][1], ja, _ja_next)
                    if r:
                        V.append(dict(base, rule=r, detail="与后一行疑似跨行泄漏"))
    return V


def main():
    global OUT_ROOT, ASR_ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEF_OUT)
    # 2026-10-26：run 级口径。管线在 runs/<id>/ 里跑门禁时，
    # 必须扫**这次 run 的产物**而不是生产目录，否则 16 个 run 的 scan 数字全一样。
    ap.add_argument("--lrc-root", default="", help="LRC 根目录（默认 kikoerusubtitle）")
    ap.add_argument("--asr-root", default="", help="ASR 根目录（默认 data/draft）")
    ap.add_argument("--rj", default="", help="只扫这个作品号（逗号分隔可多个）")
    a = ap.parse_args()
    if a.lrc_root:
        OUT_ROOT = a.lrc_root
    if a.asr_root:
        ASR_ROOT = a.asr_root
    only = {x.strip() for x in a.rj.split(",") if x.strip()} or None
    os.makedirs(a.out, exist_ok=True)

    V = scan(only)
    with open(os.path.join(a.out, "violations.jsonl"), "w", encoding="utf-8") as f:
        for v in V:
            f.write(json.dumps(v, ensure_ascii=False) + "\n")

    cnt = Counter(v["rule"] for v in V)
    by = defaultdict(list)
    for v in V:
        by[v["rule"]].append(v)

    L = ["# 确定性扫描 · 疑似问题清单", "",
         "> **必要条件检查**，不是「翻译正确证明」。漏掉一条 ≠ 翻错；**违反一条则高度可疑**。",
         "> 复核标签：中文体验类 → `影响交付/不影响交付`；源语规则告警 → `规则误报/需要日语复核`。",
         "> 口径：%s%s。" % (OUT_ROOT,
                            "（仅 %s）" % ",".join(sorted(only)) if only else "（全库）"),
         "> ASR 版本**逐轨自动选择**（坑 90）：成品 LRC 可能基于 v1/v2 任一版"
         "（实测 16 部成品 12 部对齐 v1），按 LRC 时间戳匹配率选，记录在每条 `asr_ver`。", ""]

    # ---- 规则元数据（GPT 第四轮 Q4：分层报告 + 界面只展示 Top-N）----
    # 没有金标准、标注者不懂日语，所以**不能给每条规则一个伪精确率**。
    # 只标三层，让人知道每条告警「该用什么态度对待」：
    #   severity  blocker=直接影响交付 / high=高度可疑 / medium=需复核 / info=参考
    #   confidence high=基本不会误报 / medium=有已知误报源 / low=噪声大
    #   reviewability 中文可判 / 仅源语风险
    RULE_META = {
        # R22（2026-10-23）实测：22/22 样本全部 legit（拟声重复/角色名/台词重复）。
        #   Round 3 修掉 9 处真泄露后，该规则该干的活已干完 → 降级为 info。
        "dup_suffix":       ("info",    "low",    "R22 实测 22/22 均为正常重复"),
        # R29（2026-10-23）：残留 12 条逐条人判 **12/12 均为可接受译法**
        # （`もう…`→「真是的/不行/已经够了」是合法口语化；`口…口…`→「嘴巴…嘴巴…」
        # 是实词重复；纯气声→气声全对）。B2 轮（10-16）修掉的 3 条真跑偏是第一波，
        # 清完后残留迅速劣化为噪声（同 dup_suffix/consensus 路径）→ 降 info 存档。
        # ⚠️ 检测器本身不变（门禁 3b 的 moan_drift 正/反例仍全过），降的只是优先级。
        "moan_drift":       ("info", "low", "R29 降级·残留均为可接受译法"),
        "ono_guess":        ("blocker", "high",   "中文可判"),
        "asr_suspect":      ("high",    "medium", "仅源语风险"),
        "kana_variant":     ("medium",  "medium", "仅源语风险"),
        "onomat_translit":  ("high",    "high",   "中文可判"),
        "cue_too_long":     ("high",    "high",   "中文可判"),
        "punct_only":       ("high",    "high",   "中文可判"),
        # 2026-10-22 坑 91 的规则化：整行只剩 1–2 字（甘/白/上层…）
        "zh_collapse":      ("blocker", "medium", "中文可判"),
        # 2026-10-22 坑 97：整块复制（中文重复但音频不同）
        "dup_block":        ("blocker", "high",   "中文可判"),
        "omit_suspect":     ("high",    "medium", "仅源语风险"),
        # R36（2026-10-23）：说话人名泄漏（仅带 spk 的轨；判据 polish.spk_leak）
        "spk_leak":         ("high",    "high",   "中文可判"),
        "neg_missing":      ("high",    "medium", "仅源语风险"),
        "question_missing": ("medium",  "medium", "仅源语风险"),
        "number_mismatch":  ("medium",  "low",    "仅源语风险"),
        "confusable":       ("medium",  "medium", "仅源语风险"),
        "len_imbalance":    ("info",    "medium", "仅源语风险"),
        "dup_ngram":        ("info",    "low",    "中文可判"),
        # 2026-10-17 GPT 第九轮 Q1：归属结构层后检测
        # ⚠️ confidence 用 medium 而非 high：ja 侧判据是确定的，但中文侧
        #    「没有你/没有否定词」可能是显示层时间漂移导致（实测 2/7 条如此）
        #    → 交给人工确认，不做硬门禁（GPT：只有「源中明确冲突」的才算高）
        "listener_marker_unrendered": ("high", "medium", "仅源语风险"),
        "denial_polarity_flip":       ("high", "medium", "仅源语风险"),
        # GPT 第十轮 Q3（R4）：G3 细分软门禁——**只告警，不阻断**
        # 依据：GPT 明说 `んだ` 的 35%:18% 远不够做确定性规则；`っけ` 也不应仅凭词尾硬阻断。
        "recall_mood_missing":        ("low", "low", "软门禁·只告警"),
        # R20（2026-10-23）实测：16/16 样本全部是正确译法
        #   （`すごいじゃん`→「真厉害。」、`ダメじゃん`→「就窃了。」）。
        #   该域的 `じゃん`/`でしょ` 作强调用，中文不给语气词完全正常 → 降级为只存档。
        "consensus_mood_missing":     ("info", "low", "R20 实测近全误报·仅存档"),
        # 2026-10-21 坑 84 通用化 + 2026-10-23 R24 收窄/demote：呼称被当自称。
        # 旧口径 293 条精度 ~4%（「我」多来自 ja 里的私/僕或省略主语正确补出），
        # 收窄为「ja 无明确第一人称代词」（293→228）后降 info——残留是 僕/ボク
        # 自称作品的歧义命中，per-work 确认消化；真反转的新作品仍整片命中。
        "person_flip":               ("info", "low", "R24 收窄·per-work 确认项"),
        # T6（2026-10-25）：結婚/結構/血行 同音混淆（けっこん/けっこう 一音差）。
        # advisory：只进复核清单、不阻塞——73 句「結婚」里 59 句真结婚，自动替换必翻车；
        # confidence=medium：真结婚句也可能译成「成家」这类无「婚」写法（已知误报源）。
        "kekkon_confusion":          ("advisory", "medium", "同音混淆·仅源语风险"),
        # R132（2026-10-26）：器官术语对不上（ja 点名 クリトリス/亀頭，中文整组没有对应词族）。
        # advisory：只进清单——クリトリス 在语料里 18% 被译成「小穴」，那不算错；
        # 这条的价值是抓「ASR 把 クリトリス 听成栗/熊/木架/民间/波基 后按字面硬译」的整片崩坏。
        "organ_term_mismatch":       ("advisory", "medium", "器官词对不上·仅源语风险"),
    }
    ORDER = ["dup_suffix", "moan_drift", "ono_guess", "asr_suspect", "kana_variant",
             "onomat_translit", "cue_too_long", "punct_only", "zh_collapse", "dup_block",
             "listener_marker_unrendered", "denial_polarity_flip",
             "recall_mood_missing", "consensus_mood_missing",
             "omit_suspect", "neg_missing", "question_missing", "number_mismatch",
             "confusable", "kekkon_confusion", "organ_term_mismatch", "len_imbalance", "dup_ngram", "person_flip", "spk_leak"]
    L += ["## 规则分层（先看 blocker/high，低优先级抽样复核即可）", "",
          "| 规则 | 命中 | severity | confidence | 可判性 |",
          "|---|---|---|---|---|"]
    for r in ORDER:
        sev, conf, rev = RULE_META[r]
        L.append("| `%s` | %d | %s | %s | %s |" % (r, cnt[r], sev, conf, rev))
    L.append("")
    L.append("**合计 %d 条**（覆盖 %d 轨）。"
             "⚠️ 规则不是真值：`number_mismatch`/`dup_ngram` 已知有大批噪声"
             "（中文用汉字数字、叠词是真重复），只作方向参考。"
             % (len(V), len({(v["rj"], v["track"]) for v in V})))
    L.append("")
    _v1, _v2 = VER_STATS.get("asr_v1_20261020", 0), VER_STATS.get("asr", 0)
    L.append("ASR 版本分布：v1 %d 轨 / v2 %d 轨（共 %d 轨，按时间戳匹配率自动选择）。"
             % (_v1, _v2, _v1 + _v2))
    L.append("")
    for r in ORDER:
        if not by[r]:
            continue
        sev, conf, rev = RULE_META[r]
        L.append("## `%s`（%d 条 · %s · 置信%s · %s）"
                 % (r, len(by[r]), sev, conf, rev))
        L.append("")
        for v in by[r][:25]:
            L.append("- **%s** `%s` `[%.1fs]`  %s" % (v["rj"], v["track"][:30], v["start"], v["detail"]))
            L.append("  - 日：%s" % (v["ja"][:60] or "（无）"))
            L.append("  - 中：%s" % v["zh"][:60])
        if len(by[r]) > 25:
            L.append("- …（另有 %d 条，见 `violations.jsonl`）" % (len(by[r]) - 25))
        L.append("")
    open(os.path.join(a.out, "VIOLATIONS.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")

    print("命中 %d 条，覆盖 %d 轨" % (len(V), len({(v["rj"], v["track"]) for v in V})))
    print("  ASR 版本: v1 %d 轨 / v2 %d 轨" % (VER_STATS.get("asr_v1_20261020", 0),
                                              VER_STATS.get("asr", 0)))
    for r in ORDER:
        print("  %-18s %d" % (r, cnt[r]))


if __name__ == "__main__":
    main()
