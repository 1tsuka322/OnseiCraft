# -*- coding: utf-8 -*-
"""词汇层审计：找「看起来是译文、其实是垃圾」的行（P2，2026-10-31）。

## 为什么单独做这一层

`check` 查日文残留/标点，`empty` 查空行，`trunc` 查行尾悬空，`semantic` 查具体物被泛化。
这四道全放过的一类缺陷是**词汇层塌陷**：

```
だーいすきー            → 「哒——！」        （整行只剩语气，实词全丢）
ハイボク                → 「嗨波克」        （音译，中文读者看不懂）
DS                      → 「DS」            （2 字母拉丁残留，check 只抓 ≥3 字母）
```

它们**不是漏译**（中文里有字）、**不是空行**、**不是日文残留**，所以只有拿
「日文侧有没有实词 × 中文侧是不是全虚词」这个交叉判据才抓得到。

## 四个检测器（互不重叠）

| 检测器 | 判据 | 处置 |
|---|---|---|
| `content_drop` | 日文**有实词** 且 中文分词后**全部**是 `y/e/w/o`（语气/叹词/标点/拟声） | 红，人工核 |
| `ellipsis_only` | 中文整行只有「…」而日文有实词 | 黄，**单独计数**（那是重试阶梯的兜底，不是虚化） |
| `translit` | 中文里有个**不成词**的 2~4 字片段，其拼音与日文罗马字高度重合 | 黄，人工核 |
| `latin2` | 中文里出现 2 个字母的拉丁串（`check.py` 只抓 ≥3） | 黄，人工核 |

## 日文侧「有实词」的三层启发式（不依赖分词器，任一层 True 即算）

1. **含汉字** → True
2. **去掉纯母音/促音/拔音/长音后，剩 ≥3 个假名** → True
3. 前两层都不中，再用 `fugashi` 看 `pos1 ∈ {名詞,動詞,形容詞}` 且 `pos2 ∉ {非自立,接尾}`

第 2 层是关键：`fugashi` 对口语拉长形会失效（`だーいつきー` 切不出 `好き`），
但去掉长音后剩 `だいつき`（4 个假名）就够判了。`あああああ` 去掉后为空 → False，正确。

## 覆盖边界（**期望要摆正**）

这套只抓「**整行虚化**」，抓不到「漏后半句」：`もきたてくりとりす、きもち、いきー`
译成「刚拔出来的鸡鸡 好舒服」时中文有实词，判据不触发。
历史 19 条实际案例里它只能命中 3~4 条。**它的价值是「以后不再漏」，不是「清掉存量」。**

## 用法

    python lexical_audit.py --lrc ../kikoerusubtitle --asr runs/<run_id> --rj RJ1228193
    python lexical_audit.py --all --lrc ../kikoerusubtitle --asr-root runs
    python lexical_audit.py --selftest

`--asr` 给 run 目录（自动找 `02_asr_fix/<RJ>/asr_fix`，退 `01_asr/<RJ>/asr`），
或给「含 `<RJ>/<子目录>/*.json`」的普通目录。

⚠️ 只报不改。修一行用 `fixline.py`，批量用 `tmp/daily/fix_lines_batch.py`。
"""
import argparse
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rootloc  # noqa: E402

TS = re.compile(r"^\[(\d+):(\d+\.\d+)\](.*)$")
KANJI = re.compile(r"[\u4e00-\u9fff\u3005\u3006]")
KANA = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
KANA_ONLY = re.compile(r"^[\u3040-\u309f\u30a0-\u30ff\u30fc]+$")
# 中文侧「全虚词」的允许集：y=语气 e=叹词 w=标点 o=拟声 x=非语素字
FUNC_FLAGS = ("y", "e", "w", "o", "x")
# ⚠️ jieba 的标注和 ICTCLAS 不一样，光看 flag 会漏：
#   `哒` → zg（状态词语素）、`哈` → j（简称略语）、`咻` → g（语素）
# 这些字的**词性标签本身就有误导性**（"哒"明明是拟声），所以补一条「字形兜底」：
# token 的每个字都在这张表里 → 也判为「虚」。
# 只收**叹词/拟声**常用字，不收「波/克/拉/娜」这类会出现在音译里的人名字。
INTERJ_CHARS = set("啊呀嗯唔呜哈呵嘿嗨哦噢喔唉哎诶欸呃额咻噗啪啾咕唧咚哒嗒滴嘟噜"
                   "嘣砰咣唰嘶嗞滋呼嘻嗷哇嘤哔嘀嗒嘭噜呐咯哟嘿")
LATIN2 = re.compile(r"(?<![A-Za-z])[A-Za-z]{2}(?![A-Za-z])")
# 复用 check.py 的白名单（AV/JK/VIP/OK… 都是正常借词，不是残留）
try:
    from check import LATIN_OK as _LATIN_OK
except Exception:
    _LATIN_OK = {"AV", "JK", "SM", "VIP", "OK", "NG", "BGM", "SE", "CM", "DVD",
                 "ID", "HP", "TV", "OL", "BBA", "NTR", "POV", "ASMR", "LINE"}
# 2 字母拉丁里**本来就没问题**的：中文句子里夹英文缩写是正常写法。
# `check.py` 的 LATIN 是 {3,}，管不到这些；这里补一张小表，避免把 EX2/DLsite 报成残留。
# ⚠️ 只收「出现在专有名词里」的缩写，别把 `DS`/`JQ` 这类漏进来的垃圾也加进来。
LATIN2_OK = {"EX", "DL", "MC", "JW", "QQ", "Me", "AV", "JK",
             "SM", "HP", "TV", "ID", "SE", "CM", "NG", "PC", "VR", "CV"}

# 假名 → 罗马字（够用即可：转写判定只比音，不比拼写规范）
_ROMA = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "を": "o", "ん": "n",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o",
    "ゎ": "wa", "ゔ": "vu",
    "ゃ": "ya", "ゅ": "yu", "ょ": "yo",
}
# 拗音两字组合优先匹配
_YOON = {
    "きゃ": "kya", "きゅ": "kyu", "きょ": "kyo", "しゃ": "sha", "しゅ": "shu", "しょ": "sho",
    "ちゃ": "cha", "ちゅ": "chu", "ちょ": "cho", "にゃ": "nya", "にゅ": "nyu", "にょ": "nyo",
    "ひゃ": "hya", "ひゅ": "hyu", "ひょ": "hyo", "みゃ": "mya", "みゅ": "myu", "みょ": "myo",
    "りゃ": "rya", "りゅ": "ryu", "りょ": "ryo", "ぎゃ": "gya", "ぎゅ": "gyu", "ぎょ": "gyo",
    "じゃ": "ja", "じゅ": "ju", "じょ": "jo", "びゃ": "bya", "びゅ": "byu", "びょ": "byo",
    "ぴゃ": "pya", "ぴゅ": "pyu", "ぴょ": "pyo", "ふぁ": "fa", "ふぃ": "fi", "ふぇ": "fe",
    "ふぉ": "fo", "てぃ": "ti", "でぃ": "di", "うぇ": "we", "うぃ": "wi", "ゔぁ": "va",
}


def kata2hira(s):
    out = []
    for ch in s:
        o = ord(ch)
        out.append(chr(o - 0x60) if 0x30A1 <= o <= 0x30F6 else ch)
    return "".join(out)


def romanize(ja):
    """假名 → 罗马字。汉字原样丢弃（音译判定只对片假名外来词有意义）。"""
    s = kata2hira(ja or "")
    out = []
    i = 0
    gem = False
    while i < len(s):
        two = s[i:i + 2]
        if two in _YOON:
            out.append(_YOON[two])
            i += 2
            gem = False
            continue
        ch = s[i]
        if ch == "っ":                     # 促音：双写下一个辅音
            gem = True
            i += 1
            continue
        if ch == "ー":                     # 长音：重复上一个元音
            if out and out[-1][-1:] in "aeiou":
                out.append(out[-1][-1])
            i += 1
            continue
        r = _ROMA.get(ch)
        if r:
            if gem:
                out.append(r[0])
                gem = False
            out.append(r)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------- 日文侧
_FUGA = [None]


def _fugashi():
    if _FUGA[0] is None:
        try:
            import fugashi
            _FUGA[0] = fugashi.Tagger()
        except Exception:
            _FUGA[0] = False
    return _FUGA[0] or None


_STRIP_KANA = re.compile(r"[あいうえおアイウエオっッんンー・\s\u3000、。，．\.!?！？…‥「」『』（）()]")
KATA_RUN = re.compile(r"[\u30a0-\u30ff\u30fc]{2,}")
# unidic 的 pos1 里算「实词」的几类（接続詞 / 感動詞 / 助詞 / 助動詞 / 補助記号 都不算）
CONTENT_POS = {"名詞", "動詞", "形容詞", "形状詞", "副詞", "連体詞", "代名詞"}


def _is_repetition(s, max_unit=5):
    """`むむむ` / `ガルガルガル` / `たたたた` —— 同一个短单元反复，是拟声不是实词。

    没有这一条，第 2 层（去母音后 ≥3 假名）会把「むむむ」（3 个 む）判成有实词，
    然后「唔唔唔」被判成整行虚化 —— 实测全库这类误报占 content_drop 的三分之一。

    ⚠️ `max_unit` 从 3 提到 5：拟声重复单元常常是 4~5 个假名
    （`グッチュ`×2、`とっぴゅー`×2、`しっこー`×2），卡在 3 会让它们全部漏过，
    而 unidic 把任何未知片假名都标成 `名詞` → 一律判成「有实词」。
    """
    n = len(s)
    if n < 2:
        return False
    for u in range(1, min(max_unit, n // 2) + 1):
        if n % u == 0 and s[:u] * (n // u) == s:
            return True
    return False


def ja_has_content(ja):
    """日文侧有没有实词。

    三层，**优先级从硬到软**：
    1. **含汉字** → 有（最快，也最不容易错）
    2. **fugashi 分词**（unidic-lite）→ 看 pos1 是不是实词类；**这一层是权威的**：
       跑通了、且一个实词都没有 → 就是没有，直接 False（不再往下猜）。
       实测它把 `よっしょ`/`むむむ`/`ガルガルガルガルうん`/`むはっ、うっ、ふっ` 全判成
       `感動詞`/`副詞` —— 这些译成「嘿咻」「唔唔唔」是**对的**，第一版误报的一大半就是它们。
    3. **假名兜底**（没装 fugashi 时）：去掉母音/促音/拔音/长音后 ≥3 个假名，
       且**不是同一个短单元反复**（`むむむ`/`たたたた` 是拟声）。

    ⚠️ 第 3 层原来排在 fugashi 之前，`だーいつきー` 确实靠它救回；但 unidic-lite 实测
    能把 `だーいつきー` 切成 `だー/いつき`（名詞），所以现在让 fugashi 先说话，
    第 3 层只在没装分词器时兜底 —— 顺序换过来后误报少了约三分之一。
    """
    if not ja or not ja.strip():
        return False
    if KANJI.search(ja):
        return True
    # ① 纯重复（`ぎゅぎゅぎゅ`/`くるくるくるくる`/`ねえねえ`/`おうおう`）→ 拟声，不是实词。
    #    fugashi 会把 `ぎゅぎゅぎゅ` 切成名詞，所以这条必须在它**前面**。
    bare = "".join(ch for ch in ja if KANA.match(ch))
    if len(bare) >= 2 and _is_repetition(bare):
        return False
    # ② 短且音素单调（`あぁーふっ`/`んふ`）→ 喘息，不是实词。
    #    去掉促音/长音/拔音/小写母音再数不同的字：`あぁーふっ` 只剩 あ/ふ 两种。
    _core = set(bare) - set("っッーんンぁぃぅぇぉ")
    if len(bare) <= 5 and len(_core) <= 2:
        return False
    tg = _fugashi()
    if tg:
        try:
            pos1s = []
            for w in tg(ja):
                f = w.feature
                pos1 = getattr(f, "pos1", "") or ""
                pos2 = getattr(f, "pos2", "") or ""
                if pos1 in CONTENT_POS and pos2 not in ("非自立", "接尾"):
                    pos1s.append(pos1)
            if pos1s:
                # ③ **全假名 + 实词词性只有「名詞」→ 拟声**。unidic 把任何未知片假名都
                #    标成 `名詞`，所以 `グッチュグッチュ`/`チコチコ`/`しゅっこしゅこ` 全被
                #    判成「有实词」，译成「咕啾咕啾」「滋啵滋啵」就被误报成脱落。
                #    ⚠️ 判据是「**只有**名詞」，不是「没有动词形容词」：
                #    `だーいつきー`（=だーいすきー）会被切成 `副詞 + 名詞`，
                #    用「没有 hard 词性」会把这条**最经典的实词脱落案例**一起放掉。
                if set(pos1s) <= {"名詞"}:
                    return False
                return True
            return False              # fugashi 跑通了且没实词 → 定论
        except Exception:
            pass
    rest = _STRIP_KANA.sub("", ja)
    rest = "".join(ch for ch in rest if KANA.match(ch))
    return len(rest) >= 3 and not _is_repetition(rest)


# ---------------------------------------------------------------- 中文侧
_JIEBA = [None]


def _jieba():
    if _JIEBA[0] is None:
        try:
            import jieba
            import jieba.posseg as pseg
            jieba.setLogLevel(60)
            # ⚠️ 必须显式 initialize：`jieba.dt.FREQ` 在词典加载前**全是 0**，
            # 而音译判据第一步就是「这个词在词表里吗」。没初始化 → 每个候选都
            # 「不成词」→ 全库 2638 条假阳性里有一大半是这个原因。
            jieba.initialize()
            _JIEBA[0] = (jieba, pseg)
        except Exception:
            _JIEBA[0] = False
    return _JIEBA[0] or None


def zh_tokens(zh):
    j = _jieba()
    if not j:
        return []
    return [(w.word, w.flag) for w in j[1].cut(zh or "") if w.word.strip()]


def zh_is_all_function(zh):
    """中文整行是不是只剩语气/叹词/标点/拟声。空行不算（那是 empty 门禁的事）。"""
    toks = zh_tokens(zh)
    if not toks:
        return False
    for w, flag in toks:
        if flag and flag[0] in FUNC_FLAGS:
            continue
        # 字形兜底：整个 token 都是叹词/拟声字（治 jieba 把「哒」标成 zg 这类）
        if w and all(ch in INTERJ_CHARS for ch in w):
            continue
        # 其余一律算「有内容」
        return False
    return True


def zh_is_ellipsis_only(zh):
    s = re.sub(r"[\s\u3000]", "", zh or "")
    return bool(s) and set(s) <= set("…‥.")


def pinyin_of(s):
    try:
        from pypinyin import lazy_pinyin
        return "".join(lazy_pinyin(s))
    except Exception:
        return ""


def load_ok_names(path=""):
    """人工确认的专有名词白名单（一行一个，# 开头是注释）。

    角色名音译是**合法策略**（`レイナ`→「蕾娜」），不是缺陷。这张表只用来
    压掉审计噪音，**不进 03 的 prompt**（那是 `glossary.json` 的事）。
    """
    p = path
    if not p:
        try:
            import styleloc
            p = os.path.join(styleloc.style_dir(), "translit_ok.txt")
        except Exception:
            p = ""
    out = set()
    if p and os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            w = ln.split("#", 1)[0].strip()
            if w:
                out.add(w)
    return out


# ---------------------------------------------------------------- 检测器
def _ratio(a, b):
    """**整体**相似度，不是 partial。

    partial_ratio 会把「插进/chajin」判成 0.80（`chin` 在 `chinpo` 里），但两者根本不是一个词；
    音译要求的是「整串音都对上」，用整体 ratio 才能把这类局部巧合挡掉（0.67 < 0.75）。
    """
    try:
        from rapidfuzz import fuzz
        return fuzz.ratio(a, b) / 100.0
    except Exception:
        import difflib
        return difflib.SequenceMatcher(None, a, b).ratio()


def _cands(zh):
    """音译候选：**「松散 token」连成的串**里取 2~4 字组合。

    「松散」= 单字，或者多字但不在词表里（`freq<=50`）。
    多字真词（`妈妈`/`胸部`/`学姐`…）会**打断**串，所以 `妈妈的胸部` 不会
    被拼出 `妈妈的` 这种跨词片段 —— 那是第一版 326 条里 250 条假阳性的来源
    （`妈妈` 本来就是 ママ 的正确译法）。

    为什么要拼到 4 字：`嗨波克` 被 jieba 切成 `嗨` + `波克`（后者不在词表），
    只看相邻两字会漏掉三字音译。
    """
    j = _jieba()
    if not j:
        return []
    freq = j[0].dt.FREQ
    toks = [w for w, _f in zh_tokens(zh) if re.fullmatch(r"[\u4e00-\u9fff]+", w)]
    out = []
    for w in toks:
        if len(w) >= 2 and freq.get(w, 0) <= 50:   # 多字且不成词
            out.append(w)
    # 把连续的「松散 token」连成串，再在串里取 2~4 字组合
    loose = []
    for w in toks + [""]:
        if w and (len(w) == 1 or freq.get(w, 0) <= 50):
            loose.append(w)
            continue
        if len(loose) >= 2:
            s = "".join(loose)[:8]
            for n in range(2, 5):
                for i in range(0, len(s) - n + 1):
                    ab = s[i:i + n]
                    if freq.get(ab, 0) <= 50:
                        out.append(ab)
        loose = []
    return out


def find_translit(zh, ja, min_ratio=0.75, ok=None):
    """中文里有没有「不成词但音近日文片假名」的片段。返回 [(片段, 拼音, 分数)]。

    ⚠️ 只对**片假名外来词**做（`KATA_RUN`），不做整行比对。
    第一版拿整行罗马字去 partial_ratio，结果 2638 条里绝大多数是「要是你←yaoshini(0.71)」
    这类假阳性 —— 4~6 个字母的拼音在 20+ 字母的长串里几乎总能找到局部匹配。
    音译只会发生在片假名借词上（`ハイボク`→「嗨波克」），把范围收到片假名串就干净了。

    `ok`：人工确认过的专有名词（`data/style/translit_ok.txt`），命中就跳过 ——
    角色名音译是**合法策略**，不该反复报警。
    """
    j = _jieba()
    if not j or not zh or not ja:
        return []
    runs = [r for r in KATA_RUN.findall(ja) if len(romanize(r)) >= 4]
    if not runs:
        return []
    ok = ok or ()

    def _has_ok(c):
        """候选与白名单「沾边」就跳过 —— 双向都算。

        正向（`w in c`）：`和玛雅`/`是西耶娜` —— 分词把虚词粘到了名字上。
        反向（`c in w`）：`帕喀梅`⊂`帕喀梅斯`、`米娜`⊂`卢米娜` —— 候选只是
        名字的一截，报出来等于把同一个名字报两遍。
        """
        for w in ok:
            if c in w or w in c:
                return True
        return False

    cands = [c for c in _cands(zh) if not _has_ok(c)]
    if not cands:
        return []
    hits = []
    for sub_run in runs:
        roma = romanize(sub_run)
        for sub in cands:
            py = pinyin_of(sub)
            if len(py) < 4:
                continue
            r = _ratio(py, roma)
            if r >= min_ratio:
                hits.append((sub, py, round(r, 3)))
    # 只留最长、最高分的前两条，避免同一处被重复报
    hits.sort(key=lambda x: (-len(x[0]), -x[2]))
    out, seen = [], set()
    for sub, py, r in hits:
        if any(sub in o[0] for o in out):
            continue
        if sub in seen:
            continue
        seen.add(sub)
        out.append((sub, py, r))
        if len(out) >= 2:
            break
    return out


def find_latin2(zh):
    """2 字母拉丁残留（`check.py` 的 LATIN 是 {3,}，漏这一类）。

    ⚠️ 必须带词边界：`[A-Za-z]{2}` 会把 `VIP` 里的 `VI`、`DOLLARS` 里的 `DO`/`LL`/`AR`
    全捞出来（实测 73 条里 60+ 条是这种）。再复用 `check.py` 的白名单挡掉 AV/OK/JK。
    """
    out = []
    for m in LATIN2.finditer(zh or ""):
        w = m.group(0)
        if w.upper() in _LATIN_OK or w.lower() in _LATIN_OK:
            continue
        if w in LATIN2_OK:
            continue
        out.append(w)
    return out


def audit_row(ja, zh, cfg):
    """返回 (kind, detail) 或 None。kind ∈ content_drop / ellipsis_only / translit / latin2。"""
    if not (zh or "").strip():
        return None
    if cfg["skip_nonlex"] and _pure_nonlex(ja):
        return None
    has = ja_has_content(ja)
    # ⚠️ 顺序要紧：`…` 在 jieba 里是 `x`（非语素字），会被 `zh_is_all_function` 判成
    # 「全虚词」→ 全落进 content_drop，把 270 行兜底占位和真·虚化混在一起。
    # 先判纯省略，两者才分得开。
    if has and zh_is_ellipsis_only(zh):
        return "ellipsis_only", {"ja": ja[:60], "zh": zh[:60]}
    if has and zh_is_all_function(zh):
        return "content_drop", {"ja": ja[:60], "zh": zh[:60],
                                "tokens": ["%s/%s" % t for t in zh_tokens(zh)][:8]}
    hit = find_translit(zh, ja, cfg["translit_ratio"], cfg.get("ok"))
    if hit:
        return "translit", {"ja": ja[:60], "zh": zh[:60],
                            "hits": ["%s←%s(%.2f)" % h for h in hit]}
    lat = find_latin2(zh)
    if lat:
        return "latin2", {"ja": ja[:60], "zh": zh[:60], "latin": lat[:4]}
    return None


_PNL = [None]


def _pure_nonlex(ja):
    """复用 mask_nonlexical 的「纯非实义」判定：那是已知可确定性直出的拟声行，不算虚化。"""
    if _PNL[0] is None:
        try:
            import mask_nonlexical as MN
            _PNL[0] = MN.pure_nonlexical
        except Exception:
            _PNL[0] = False
    fn = _PNL[0]
    if not fn:
        return False
    try:
        return fn(ja) is not None
    except Exception:
        return False


# ---------------------------------------------------------------- ASR 配对
def find_asr(root, rj, stem):
    """按优先级找该轨 ASR：02_asr_fix（03 真正吃的）→ 01_asr/asr → v1 → 通用子目录。"""
    cands = [
        os.path.join(root, "02_asr_fix", rj, "asr_fix", stem + ".json"),
        os.path.join(root, "01_asr", rj, "asr", stem + ".json"),
        os.path.join(root, "01_asr", rj, "asr_v1_20261020", stem + ".json"),
    ]
    cands += sorted(glob.glob(os.path.join(root, rj, "*", stem + ".json")))
    for p in cands:
        if os.path.exists(p):
            try:
                d = json.load(open(p, encoding="utf-8"))
                if d.get("segments"):
                    return d["segments"]
            except Exception:
                continue
    return []


def rows_of(path):
    out = []
    for line in open(path, encoding="utf-8"):
        m = TS.match(line.strip())
        if m:
            out.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3)))
    return out


def pair_ja(rows, segs):
    """把 ASR 段按起点分给「起点 ≤ seg.start 的最后一行」——分区干净，不重叠。"""
    if not rows or not segs:
        return [""] * len(rows)
    times = [t for t, _ in rows]
    buf = [[] for _ in rows]
    for s in sorted(segs, key=lambda z: float(z.get("start") or 0)):
        st = float(s.get("start") or 0)
        i = 0
        for k, t in enumerate(times):
            if t <= st + 0.05:
                i = k
            else:
                break
        buf[i].append(s.get("text") or "")
    return ["".join(x) for x in buf]


def audit_track(rows, segs, cfg):
    """**按 ASR 段聚合**后审计，不是逐显示行。

    ⚠️ 逐行判会把「显示行拆分」全报成实词脱落：一条 cue 的译文被
    `render_display_lines` 按逗号切成 2 行时，ASR 段的起点落在第 1 行，
    整段日文就都归给第 1 行，而第 1 行只承载「啊。」—— 看着就像整句丢了。
    实测 RJ1530549 123.08 的「啊。」，下一行 124.98 就是「我 那个 随时都可以」，
    整句一个词没丢。第一版 302 条里一大半是这么来的（memory 里
    「per-row 视图会让他误判」说的就是这个）。

    正确做法：**一个 ASR 段拥有它时间范围内的所有显示行**，把这些行的中文
    拼起来再判「是不是只剩语气」。
    """
    if not rows or not segs:
        return []
    segs = sorted(segs, key=lambda z: float(z.get("start") or 0))
    starts = [float(s.get("start") or 0) for s in segs]
    owner = [-1] * len(rows)
    j = -1
    for i, (t, _zh) in enumerate(rows):
        while j + 1 < len(segs) and starts[j + 1] <= t + 0.05:
            j += 1
        if j >= 0:
            owner[i] = j
    groups = {}
    for i, o in enumerate(owner):
        if o >= 0:
            groups.setdefault(o, []).append(i)
    hits = []
    for o, idxs in groups.items():
        ja = segs[o].get("text") or ""
        zh = "".join(rows[i][1] for i in idxs)
        r = audit_row(ja, zh, cfg)
        if r:
            hits.append({"t": round(rows[idxs[0]][0], 2), "kind": r[0],
                         "rows": len(idxs), **r[1]})
    return hits


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lrc", default=os.path.join(rootloc.PROD), help="含 RJ 子目录的父目录")
    ap.add_argument("--asr", default="", help="单个 run 目录，或含 <RJ>/<子目录>/ 的根")
    ap.add_argument("--asr-root", default=os.path.join(rootloc.ROOT, "runs"),
                    help="--all 时按 run 自动找 ASR")
    ap.add_argument("--rj", action="append", help="只查这些作品")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", default=os.path.join(rootloc.ROOT, "tmp", "lexical_audit.json"))
    ap.add_argument("--md", default="")
    ap.add_argument("--translit-ratio", type=float, default=0.75)
    ap.add_argument("--ok-file", default="",
                    help="专有名词白名单（默认 <style>/translit_ok.txt）")
    ap.add_argument("--no-nonlex-skip", dest="no_nonlex_skip", action="store_true",
                    help="不跳过「纯拟声可直出」的行（A/B 用）")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    ok = load_ok_names(a.ok_file)
    cfg = {"translit_ratio": a.translit_ratio, "skip_nonlex": not a.no_nonlex_skip,
           "ok": ok}
    if ok and not a.quiet:
        print("音译白名单：%d 条（%s）" % (len(ok), a.ok_file))

    rjs = a.rj or (sorted(os.path.basename(d) for d in glob.glob(os.path.join(a.lrc, "RJ*"))
                          if os.path.isdir(d)) if a.all or not a.rj else [])
    if not rjs:
        ap.error("要给 --rj 或 --all")

    out_rows, tot = [], {"rows": 0, "content_drop": 0, "ellipsis_only": 0,
                         "translit": 0, "latin2": 0}
    for rj in rjs:
        asr_root = a.asr
        if not asr_root and a.asr_root:
            cands = sorted(glob.glob(os.path.join(a.asr_root, "*_%s_production" % rj)))
            asr_root = cands[-1] if cands else ""
        rec = {"rj": rj, "asr_root": asr_root, "rows": 0, "hits": []}
        for p in sorted(glob.glob(os.path.join(a.lrc, rj, "*.lrc"))):
            stem = os.path.basename(p)[:-4]
            rows = rows_of(p)
            segs = find_asr(asr_root, rj, stem) if asr_root else []
            rec["rows"] += len(rows)
            for h in audit_track(rows, segs, cfg):
                h["track"] = stem
                rec["hits"].append(h)
        for h in rec["hits"]:
            tot[h["kind"]] = tot.get(h["kind"], 0) + 1
        tot["rows"] += rec["rows"]
        out_rows.append(rec)
        if not a.quiet:
            c = {k: sum(1 for h in rec["hits"] if h["kind"] == k)
                 for k in ("content_drop", "ellipsis_only", "translit", "latin2")}
            print("%-11s 行 %6d  实词脱落 %3d  纯省略 %3d  音译 %3d  拉丁2 %3d"
                  % (rj, rec["rows"], c["content_drop"], c["ellipsis_only"],
                     c["translit"], c["latin2"]))

    if not a.quiet:
        print("\n合计 %d 行：实词脱落 %d / 纯省略 %d / 音译 %d / 拉丁2 %d"
              % (tot["rows"], tot["content_drop"], tot["ellipsis_only"],
                 tot["translit"], tot["latin2"]))
        print("（均为**告警**：实词脱落抓「整行虚化」，抓不到「漏后半句」）")

    if a.json:
        os.makedirs(os.path.dirname(a.json) or ".", exist_ok=True)
        json.dump({"translit_ratio": a.translit_ratio, "summary": tot, "rj": out_rows},
                  open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        if not a.quiet:
            print("明细 → %s" % a.json)
    if a.md:
        write_md(a.md, out_rows, tot)
        if not a.quiet:
            print("报表 → %s" % a.md)
    return 0


def write_md(path, rows, tot):
    L = ["# 词汇层审计（实词脱落 / 音译 / 拉丁残留）", "",
         "| 类别 | 条数 |", "|---|---|",
         "| 实词脱落 | %d |" % tot.get("content_drop", 0),
         "| 纯省略（「…」） | %d |" % tot.get("ellipsis_only", 0),
         "| 音译可疑 | %d |" % tot.get("translit", 0),
         "| 2 字母拉丁 | %d |" % tot.get("latin2", 0), ""]
    for r in rows:
        if not r["hits"]:
            continue
        L.append("## %s（%d 条）" % (r["rj"], len(r["hits"])))
        L.append("")
        L.append("| 时间 | 轨 | 类别 | 日文 | 中文 | 依据 |")
        L.append("|---|---|---|---|---|---|")
        for h in r["hits"]:
            why = h.get("tokens") or h.get("hits") or h.get("latin") or []
            L.append("| %.2f | %s | %s | %s | %s | %s |"
                     % (h["t"], h["track"][:14], h["kind"],
                        h["ja"].replace("|", "/"), h["zh"].replace("|", "/"),
                        "；".join(why)[:40].replace("|", "/")))
        L.append("")
    open(path, "w", encoding="utf-8").write("\n".join(L))


# ---------------------------------------------------------------- 自检
def selftest():
    ok = []

    def ck(name, cond, detail=""):
        ok.append(bool(cond))
        print("  %s %-46s %s" % ("✅" if cond else "❌", name, detail))

    # ① 日文侧三层启发式
    ck("含汉字 → 有实词", ja_has_content("大きくなったおちんぽ"))
    ck("去长音后 ≥3 假名 → 有实词", ja_has_content("だーいつきー"))
    ck("纯母音拉长 → 无实词", not ja_has_content("あああああ"))
    ck("んっ 类 → 无实词", not ja_has_content("んんっ"))
    ck("空串 → 无实词", not ja_has_content(""))
    # 这三条是**误报的主力**：都是感叹词，译成「嘿咻」「唔唔唔」是对的。
    # 它们必须走 fugashi 的 感動詞 判定，不能落到「≥3 假名」的兜底。
    ck("よっしょ。 → 无实词（感動詞）", not ja_has_content("よっしょ。"))
    ck("むむむ。 → 无实词（感動詞）", not ja_has_content("むむむ。"))
    ck("ガルガルガルガルうん → 无实词（重复）",
       not ja_has_content("ガルガルガルガルうん"))
    ck("ああ、これは君のための補習なのよ。 → 有实词（名詞）",
       ja_has_content("ああ、これは君のための補習なのよ。"))

    # ② 中文侧「全虚词」
    ck("「哒——！」判全虚词", zh_is_all_function("哒——！"))
    ck("「啊啊…」判全虚词", zh_is_all_function("啊啊…"))
    ck("「好舒服」不算全虚词", not zh_is_all_function("好舒服"))
    ck("「刚拔出来的鸡鸡 好舒服」不算全虚词",
       not zh_is_all_function("刚拔出来的鸡鸡 好舒服"))

    # ③ 罗马字
    ck("ハイボク → haiboku", romanize("ハイボク") == "haiboku", romanize("ハイボク"))
    ck("きゃく → kyaku", romanize("きゃく") == "kyaku", romanize("きゃく"))
    ck("がっこう → gakkou", romanize("がっこう") == "gakkou", romanize("がっこう"))

    # ④ 音译嗅探
    h = find_translit("嗨波克", "ハイボク")
    ck("「嗨波克」←ハイボク 判音译", bool(h), str(h[:1]))
    h2 = find_translit("我好舒服", "きもちいい")
    ck("正常译文不误报", not h2, str(h2))

    # ⑤ 2 字母拉丁
    ck("「DS」判拉丁2", find_latin2("DS好厉害") == ["DS"])
    ck("「好厉害」无拉丁", not find_latin2("好厉害"))

    # ⑥ 端到端：一行虚化 + 一行正常
    cfg = {"translit_ratio": 0.75, "skip_nonlex": True, "ok": set()}
    rows = [(0.0, "哒——！"), (3.0, "好舒服")]
    segs = [{"start": 0.0, "end": 2.0, "text": "だーいすきー"},
            {"start": 3.0, "end": 5.0, "text": "きもちいい"}]
    hits = audit_track(rows, segs, cfg)
    ck("端到端只命中虚化那一行",
       len(hits) == 1 and hits[0]["kind"] == "content_drop", str(hits[:1]))

    n = sum(1 for _, g in ok if g) if ok and isinstance(ok[0], tuple) else sum(ok)
    print("%s lexical_audit selftest %d/%d" % ("✅" if n == len(ok) else "❌", n, len(ok)))
    return 0 if n == len(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
