# -*- coding: utf-8 -*-
r"""A8：非实义 span 遮蔽器（GPT 第五轮 Q3 的「遮蔽原文 + 强校验」方案）。

**为什么需要它**：prompt 已经说了「不要把拟声/气声按字面译」，但 7B 有时还是不听——
`もぅ` 被译成「已经」、`チパチパ` 被译成「电击」。prompt 是**软的**，遮蔽是**硬的**。

**方案（GPT 原话）**：
  ① 按规则把非实义 span 抽出来，用 `<M1>` `<M2>` … **占位符**替换原文
  ② 模型译完后按序号**重建**这些片段
  ③ **强校验**：漏掉 / 改写 / 重复 / 数量不符 → **重试** → 仍失败回退**该行原译** + 标风险

**优先口径（GPT）**：宁可漏判、不可误判——
真正的实义内容被遮蔽，比放过 10 个非实义片段更严重。

  python mask_nonlexical.py                 # 自检 + 演示

## 接入翻译管线（A8 第二步，2026-10-16）

`polish.py::polish_block` 里做**润色层遮蔽**（不是初译层——初译是 HY-MT 逐句翻译
专用模型，遮蔽会破坏它的输入；真正会「把 もぅ 按字面译成『已经』」的是润色层的 Qwen）。

四步：
  ① `mask_ja(ja)` → 把非实义 span 换成 `<M1>`，模型看到的是占位符
  ② 模型**原样保留占位符**（prompt 明确要求），中文里出现 `<M1>`
  ③ `restore_zh(zh, spans)` → 把 `<M1>` 换成**中文对应**（不还原回日文！）
  ④ 强校验：数量/序号/残留不符 → 返回 None 触发上层重试 → 仍失败回退该句初译

⚠️ 关键区别：翻译场景的占位符要还原成**中文对应**（`もぅ`→`唔`），
   不是还原回日文原文（`restore_ja` 是给「日→日遮蔽」用的，这里不用它）。
"""
import os
import re
import sys

MAX_SPANS = 4          # 一条 cue 最多遮几个
# 是否在 polish 管线里启用遮蔽（可用 KIKOERU_MASK_NONLEX=0 关掉做 A/B）
ENABLED = os.environ.get("KIKOERU_MASK_NONLEX", "1") != "0"
# GPT 第十轮 Q2（M2 选型）：True = 用 `〔中文字面值〕` 作模型侧载体。
# **2026-10-18 起默认开启**（原来默认 0 是为了先跑对撞实验，现已通过）：
#   30 条 / 5 轨同批对撞 —— 核心保留 93.3% vs 66.7%，回退率 6.7% vs 33.3%，
#   重复/顺序错/残留包装全 0。机理：`<M1>` 要求 7B 做「符号守恒」这种非语言任务，
#   `〔哈啊…〕` 只要求它保留一段**已经正确的中文**（模型擅长的事）。
# 关掉（回到旧 `<M1>`）：`KIKOERU_MASK_LITERAL=0`。
LITERAL = os.environ.get("KIKOERU_MASK_LITERAL", "1") not in ("0", "false", "False")
# D9/D11（2026-10-18，GPT 第九轮 Q2「表外拟声标 unknown_nonlexical，**不伪装「…」**」）：
# True = 只有 ZH_MAP 里有确定中文的 span 才遮蔽；表外的一律**不遮**（保留日文原文），
# 记进 unknown_nonlexical 清单交人工试听。
# 为什么：兜底「…」占全部 span 的 25.9%（304/1176），`〔…〕` 无信息量，
# 模型很可能当普通省略号忽略——遮了反而制造一个假信息点。不遮时模型看到原始假名，
# 至少能靠上下文猜（实测 `ぴゅっぴゅ` 不遮也能译对）。
STRICT_ONLY = os.environ.get("KIKOERU_MASK_STRICT", "1") not in ("0", "false", "False")
# D9 第三路：纯非实义独立 cue 确定性直出（不过模型）。`KIKOERU_DETERMINISTIC=0` 关。
DETERMINISTIC = os.environ.get("KIKOERU_DETERMINISTIC", "1") not in ("0", "false", "False")

KANA_RUN = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u30fc]+")
MOAN_RE = re.compile(r"^[\u3040-\u309f\u30fc\u30a0-\u30ff]+$")
REPEAT_RE = re.compile(r"^(.{1,4})\1+$")

SMALL_KANA = set("ぁぃぅぇぉゃゅょっゎァィゥェォャュョッヮ")

# ---- 白名单：这些绝不遮蔽（遮了就是把实义内容杀掉）----
SAFE = {
    "もう", "まだ", "すぐ", "ちょっと", "やだ", "やめて", "だめ",
    "いや", "ほら", "さあ", "そう", "そうそう", "あ", "あー", "ああ",
    "ん", "んん", "うん", "はい", "ええ", "え", "えー", "えっと", "あの",
    "その", "これ", "あれ", "どれ", "いく", "だれ", "なに", "なん",
    "すごい", "やばい", "きもち", "きもちいい", "だめえ", "だめー",
    "いっぱい", "たくさん", "もっと", "ずっと", "やっぱり", "やはり",
    "ねえ", "なあ", "そうだ", "そうか", "なるほど", "たしかに",
    # 2 字常用词：规则 b 「只剩一种字」会把它们当气声，必须显式放过
    "いい", "よい", "ママ", "まま", "パパ", "ぱぱ", "つま", "はは", "ちち",
    "ままっ", "こわ", "こわい", "たの", "たのしい",
}
# 助词/短虚词：遮了就把句子结构拆了
PARTICLES = {
    "を", "が", "は", "の", "に", "で", "と", "も", "や", "か", "わ", "ぞ", "さ",
    "ね", "よ", "な", "い", "う", "ん", "あ", "え", "お", "へ",
    "から", "より", "まで", "など", "しか", "でも", "だけ", "ほど", "きり",
    "ばかり", "ぐらい", "くらい", "ながら", "たり", "ので", "けど", "けれど", "って",
}
# 片假名单词白名单（ASR 常吐的真实外来语/称谓，不是拟声）
KATA_SAFE = {
    "ダメ", "ハイ", "ノー", "イエス", "オーケー", "コーヒー", "シャワー",
    "ソファ", "ベッド", "ドア", "テレビ", "スマホ", "パソコン",
    "クリスマス", "バレンタイン", "ゴメン", "サンキュー", "マイク", "プレゼント",
    "ママ", "パパ",
    # 数字/计数类片假名——念的是「零」，是台词不是拟声（B2 实测：ゼロゼロゼロ 曾被误遮）
    "ゼロ", "イチ", "ニ", "サン", "ヨン", "ゴ", "ロク", "ナナ", "ハチ", "キュウ",
    "ワン", "ツー", "スリー", "フォー", "ファイブ",
}
# 显式片假名拟声（宁可漏判不可误判，片假名走显式表而不是猜）
ONO_KATA = {
    "キュー", "チュー", "キュン", "ズキュン", "ドキュ", "ピチャ", "グチュ",
    "ヌチュ", "ズプ", "ドク", "パチ", "ビチャ", "ズリ", "グリ",
    "ズン", "ドス", "ビク", "ガク", "ブル", "ゾク", "カク",
    # A8 接入实测补充：语料高频的片假名重复单元（シコシコ/バブバブ 等）
    "シコ", "バブ", "ジュプ", "グチュ", "ピチャ", "ビチャ", "パチ", "ゴシ",
    "クリ", "ニュ", "ミチ", "ズポ", "チュパ", "クチュ",
    # B2 实测补充：ビリビリ（酥麻感）曾被模型脑补成名词「电击」，显式纳入
    "ビリ", "ビリビリ", "ビリッ",
}

# 平假名「气声字」——只有这些字构成的重复/单字才可能是非实义气声
# （んんん / あーーー / ふふ / うぅ）。别的平假名（こ/て/どん/そろ/もっと）都是实义词，
# 一旦重复就遮会误伤（实测 ここ/てて/どんどん/そろそろ/もっともっと 全被误遮）。
# 含 も（もぅ 的小写变体）。
MOAN_CHARS = set("んんふぁあぃぃうぅー〜っはひへほむぬぐちゅぴびぱばも")

# 显式平假名拟声（重复型，但单元不是气声字——必须点名，否则被 SAFE_REPEAT 误伤的
# 恰恰是这些真正该遮的）。宁可漏判：表外的平假名重复一律不遮。
ONO_HIRA = {
    "しこしこ", "ぴちゃぴちゃ", "びちゃびちゃ", "ぐちゅぐちゅ", "じゅぷじゅぷ",
    "ずりずり", "ごしごし", "くりくり", "にゅるにゅる", "ぬちゅぬちゅ",
    "ちゅぱちゅぱ", "ぺろぺろ", "くちゅくちゅ", "しゃぶしゃぶ", "ぱくぱく",
    "もちもち", "すりすり", "こすこす", "しゅこしゅこ", "ぱんぱん",
}

# 实义叠词显式白名单（REPEAT 型但显然是词，不是拟声）——双保险
SAFE_REPEAT = {
    "どんどん", "そろそろ", "だんだん", "もっともっと", "ますます", "ますます",
    "いよいよ", "とうとう", "はっきり", "しっかり", "ゆっくり", "にこにこ",
    "いろいろ", "ときどき", "だいたい", "たまたま", "しばしば", "つぎつぎ",
    "すらすら", "ぐんぐん", "どきどき", "わくわく", "うきうき", "にやにや",
    "そこそこ", "そこら", "ここ", "てて", "って", "った", "ちょっと",
    "じゃあ", "またまた", "まだまだ", "あとあと", "たびたび",
}


def _is_moan_token(tok):
    """判断单个假名 run 是否非实义（气声/拟声）。高精度低召回。

    四轮迭代的教训（都记在这里）：
      ① 用 len>=3 卡长度，把 もぅ / んー（2 字，最典型的 ASR 产物）全挡掉；
      ② 一个分支里提前 return False，害得片假名拟声分支永远走不到（ピチャピチャ 漏了）；
      ③ 按「汉字/假名」切分，早く 被切成 早+く、助词 を 被当成拟声；
      ④ 最严重：规则「只剩一种字 / 很短」把真词的假名后缀当气声——
         気持ちいい→ちいい(17次)、見てくれる→ける(27次)、リリアさん→リリア(8次)、
         して(10次) 全被误遮，触发率一度 33%。

    最终只用四条毫不含糊的判据：
      (a) 同一 1-4 字单元反复（片假名要求单元 >=2 字）
      (b) 去长音后只剩一种字——仅平假名
      (c) 含小写假名且很短（<=2 字）——仅平假名
      (d) 命中显式片假名拟声表
    """
    if not tok or len(tok) < 2 or len(tok) > 12:
        return False
    if tok in SAFE or tok in PARTICLES or tok in KATA_SAFE or tok in SAFE_REPEAT:
        return False
    if not MOAN_RE.match(tok):
        return False

    bare = re.sub(r"\u30fc", "", tok)
    if not bare:
        return False
    small = any(c in SMALL_KANA for c in tok)
    # ⚠️ 長音符「ー」(U+30FC) 不在平假名区间内，必须单独放行——
    #    否则 'あーーー' 会被判成「非平假名」而漏掉规则 b。
    kana_is_hira = all("\u3040" <= c <= "\u309f" or c == "\u30fc" for c in tok)
    kana_is_kata = all("\u30a0" <= c <= "\u30ff" for c in tok)

    # (d) 显式片假名拟声（最可靠，放最前）
    if tok in ONO_KATA:
        return True
    # (d2) 显式平假名拟声（しこしこ/ぴちゃぴちゃ…——单元非气声字的名表）
    if tok in ONO_HIRA:
        return True
    # (a) 同一单元反复——**只对片假名或气声字生效**
    #     实测教训（2026-10-16 A8 接入）：平假名任意单元重复会把实义词误遮——
    #     ここ(こ×2) / てて(て×2) / どんどん(どん×2) / そろそろ(そろ×2) /
    #     もっともっと(もっと×2) 全部中招。所以平假名重复必须**每个字都是气声字**。
    m = REPEAT_RE.match(bare)
    if m:
        unit = m.group(1)
        # 重复单元的「母词」若是有实义的**多字**词（ゼロ/ダメ 等），是叠用强调，不遮。
        # 单字单元（ん/あ/ふ）不适用这条——ん 虽在 SAFE 里，但 んんん 确是气声，要遮。
        if len(unit) >= 2 and (unit in KATA_SAFE or unit in SAFE or unit in SAFE_REPEAT):
            return False
        if kana_is_kata and 2 <= len(unit) <= 4 and len(bare) <= 12:
            return True                   # ピチャピチャ / シコシコ / バブバブ
        if kana_is_hira and all(c in MOAN_CHARS for c in bare):
            return True                   # んんん / あーーー / ふふ / うぅ
    # (b) 只有一种字——仅平假名，且该字必须是气声字
    if kana_is_hira and len(set(bare)) == 1 and bare[0] in MOAN_CHARS:
        return True                       # ふふ / あーーー
    # (c) 含小写假名且很短——仅平假名，且都是气声字
    if small and kana_is_hira and len(bare) <= 2 and all(c in MOAN_CHARS for c in bare):
        return True                       # もぅ
    return False


def mask_ja(ja_text, max_spans=None, literal=False, strict=None, unknown=None):
    """把日文里的非实义 span 换成占位符。

    只切「假名 run」，汉字整块保留——日文词是汉字假名混排的。

    literal=False（默认，A8 旧协议）：`<M1>` `<M2>` 抽象占位符。
    literal=True（GPT 第十轮 Q2 · M2 协议）：`〔哈啊…〕` —— 模型看到**最终中文**，
        程序事后剥括号。`〔〕` 只是模型侧临时载体，不是给读者看的标点。

    ⚠️ 既然 `` literal=True `` 已经直接把中文写进日文串，调用方**不需要**再
    经过 `restore_zh`（没有占位符可还原），只需「校验 + 剥包装」→ 见 `check_literal`。
    """
    if not ja_text:
        return ja_text, []
    max_spans = MAX_SPANS if max_spans is None else max_spans
    strict = STRICT_ONLY if strict is None else strict
    spans = []
    parts = re.split(r"([\u3040-\u309f\u30a0-\u30ff\u30fc]+)", ja_text)
    out = []
    for seg in parts:
        if not seg:
            continue
        if (KANA_RUN.fullmatch(seg) and _is_moan_token(seg)
                and len(spans) < max_spans):
            # D9/D11：表外拟声**不遮**（标 unknown_nonlexical），只有表内确定的才遮
            if strict:
                zh = zh_for_known(seg)
                if zh is None:
                    if unknown is not None:
                        unknown.append(seg)
                    out.append(seg)      # 原样保留日文，让模型靠上下文处理
                    continue
            else:
                zh = zh_for(seg)
            if literal:
                ph = LITERAL_OPEN + zh + LITERAL_CLOSE
            else:
                ph = "<M%d>" % (len(spans) + 1)
            spans.append((ph, seg))
            out.append(ph)
        else:
            out.append(seg)
    return "".join(out), spans


# ---- M2 协议（GPT 第十轮 Q2）：〔中文字面值〕作为模型侧可读载体 ----
LITERAL_OPEN = "〔"
LITERAL_CLOSE = "〕"
# 模型可能自行"规范化"成的各种包装形态。只在这些形态的**紧邻位置**剥，
# 绝不全局删括号（会把旁白/注释的（）一起吃掉）。
_LIT_WRAP_VARIANTS = [
    ("〔", "〕"), ("（", "）"), ("(", ")"), ("【", "】"),
    ("[", "]"), ("「", "」"), ("『", "』"), ("〈", "〉"), ("＜", "＞"),
]


def _is_bare_ellipsis(zh):
    """核心值是否**只有省略号**（兜底值「…」）。

    这类值不能靠 count 校验：中文正文里省略号本来就多，count>1 是常态。
    """
    return bool(zh) and all(c in "…⋯.·" for c in zh)


def _has_standalone_ellipsis(s):
    """正文里是否存在成串的省略号（「…」或「……」）。"""
    return bool(re.search(r"…+|⋯+", s))


def _drop_one_ellipsis(s):
    """从第一个省略号串里去掉一个「…」（表示那个占位符已被消费）。"""
    return re.sub(r"…+", lambda m: m.group(0)[1:] if len(m.group(0)) > 1 else "…",
                  s, count=1)


def check_literal(out_zh, spans):
    """M2 协议的校验：**不还原**，只判断「核心中文值是否还在 + 包装形态」。

    返回 (fixed_zh, problems, info)；info = {"observed": [...], "wrapper": ...}。
    - problems 为空 = 通过（硬校验）；非空 = 该句应走回退阶梯。
    - 硬校验：每个期望的核心中文值恰好出现一次；无残留 `<M1>`；无原始日文 span。
    - 软审计：包装是 bracket/paren/none/other 哪一种——**只记录，不触发回退**。

    ⚠️ `spans` 里的占位符就是 `〔哈啊…〕` 整串（literal 模式产出），
    与 `restore_zh` 的 ``<M1>`` 形态不同——这里**不能**假设抽象占位符结构。
    核心值以 `zh_for(orig)` 为准（如 `はぁ` → `哈啊`，不含省略号）。
    """
    if not spans:
        return out_zh, [], {"observed": [], "wrapper": "none"}

    want = [zh_for(orig) for _ph, orig in spans]
    # 占位符整串本身（literal 模式下 = 〔哈啊…〕，含省略号）→ 优先精确匹配
    phs = [_ph for _ph, _orig in spans]
    origs = [orig for _ph, orig in spans]
    problems = []
    observed = []
    wrappers = set()
    out = out_zh

    for zh, ph, orig in zip(want, phs, origs):
        hit = None
        # 1) 原样保留整串占位符（最理想：连省略号都对）→ bracket
        if ph in out:
            hit = ("〔", "〕", ph, "bracket")
        else:
            # 2) 原占位符的**内部文本**（含省略号，如「哈啊…」）被换了包装
            inner = ph[1:-1] if ph[:1] in "〔（(" else zh
            for op, cl in _LIT_WRAP_VARIANTS:
                pat = op + inner + cl
                if pat in out:
                    hit = (op, cl, pat,
                           "bracket" if (op, cl) == ("〔", "〕") else
                           "paren" if (op, cl) in (("（", "）"), ("(", ")")) else "other")
                    break
            if hit is None:
                # 3) 只剩核心值（省略号也丢了）
                for op, cl in _LIT_WRAP_VARIANTS:
                    pat = op + zh + cl
                    if pat in out:
                        hit = (op, cl, pat,
                               "bracket" if (op, cl) == ("〔", "〕") else
                               "paren" if (op, cl) in (("（", "）"), ("(", ")")) else "other")
                        break
        if hit:
            op, cl, pat, wrap = hit
            wrappers.add(wrap)
            n = out.count(pat)
            out = out.replace(pat, zh)
            observed.append(zh)
            if n > 1:
                problems.append("核心值「%s」重复 %d 次" % (zh, n))
        else:
            # ⚠️ 兜底核心值就是「…」时不能直接 `count(zh)`——中文正文里的省略号
            # 到处都是，会把「…」判成重复。这类值只能靠「有包装」或
            # 「在占位符原位置附近」来定位，定位不到就按**消失**处理（保守）。
            if _is_bare_ellipsis(zh):
                # 「…」类：只有在占位符原位置前后有独立省略号才算可接受
                stripped = out.replace("…", "").replace("⋯", "")
                if len(stripped) >= 1 and _has_standalone_ellipsis(out):
                    wrappers.add("none")
                    observed.append(zh)
                    out = _drop_one_ellipsis(out)
                else:
                    problems.append("核心值「%s」消失（原文 %s）" % (zh, orig))
                continue
            n = out.count(zh)
            if n >= 1:
                wrappers.add("none")
                observed.append(zh)
                if n > 1:
                    problems.append("核心值「%s」重复 %d 次" % (zh, n))
            else:
                problems.append("核心值「%s」消失（原文 %s）" % (zh, orig))

    # 残留抽象占位符 / 原始日文 span
    if re.findall(r"<M\d+>", out):
        problems.append("残留占位符 %s" % ",".join(re.findall(r"<M\d+>", out)))
    for orig in origs:
        if orig in out:
            problems.append("残留原始日文 span「%s」" % orig)

    # ---- 最后一道：剥掉**登记 span 周边**的残留包装（GPT 第十轮 Q2 明确要求）----
    # 上面「核心值只剩省略号」的分支无法靠 zh 定位，会漏剥〔…〕。这里统一兜底：
    # 把所有 `〔…〕` / `（…）` / `【…】` 这种**只含符号/省略号**的空包装删掉。
    # ⚠️ 只剥「括号内没有任何汉字/假名/字母数字」的——旁白注释里必有实词，不会被误伤。
    before = out
    out = re.sub(r"[〔（(【\[「『〈＜]\s*[…⋯.·、\s]+\s*[〕）)】\]」』〉＞]", "", out)
    if out != before:
        wrappers.add("bracket")

    wrapper = "bracket" if wrappers == {"bracket"} else \
              "paren" if wrappers == {"paren"} else \
              "none" if wrappers == {"none"} else \
              "other" if wrappers else "missing"
    return out, problems, {"observed": observed, "wrapper": wrapper}


def restore_ja(masked, spans):
    """把 <M1> 还原回原文。返回 (restored, problems)。

    problems 非空即需要重试：漏掉 / 改写 / 重复 / 数量不符 / 序号不连续。
    """
    if not spans:
        return masked, []

    want = {ph: orig for ph, orig in spans}
    found = re.findall(r"<M\d+>", masked)
    seen = {}
    for ph in found:
        seen[ph] = seen.get(ph, 0) + 1

    problems = []
    if len(found) != len(want):
        problems.append("占位符数量 %d != 输入 %d" % (len(found), len(want)))
    for ph in seen:
        if ph not in want:
            problems.append("出现未知占位符 %s" % ph)
    for ph in want:
        if ph not in seen:
            problems.append("漏掉 %s（原文 %s）" % (ph, want[ph]))
    for ph, n in seen.items():
        if n > 1:
            problems.append("%s 重复 %d 次" % (ph, n))
    idxs = sorted(int(re.sub(r"\D", "", ph)) for ph in seen)
    if idxs != list(range(1, len(idxs) + 1)):
        problems.append("占位符序号不连续：%s" % idxs)

    out = masked
    for ph, orig in spans:
        out = out.replace(ph, orig)
    if re.findall(r"<M\d+>", out):
        problems.append("还原后仍残留占位符")
    return out, problems


# ---- 中文对应表：非实义 span → 中文拟声/语气（A8 接入用）----
# 口径：**能对上就用固定译法，对不上就按类别给个通用中文**——绝不音译成汉字
# （「キュー」绝不译「基欧」，宁可给「啾」）。
# 这张表出现在「日→中」翻译场景，还原的是**中文**，不是日文。
ZH_MAP = {
    # ---- D9/D11 补充（2026-10-18）：strict 模式下高频 unknown 的补表 ----
    # 来源：全库 309 个 unknown span 的词频统计 + parallel.json 语料共现验证。
    # 口径：**用中文拟声/动作词**，不音译假名。
    "はー": "哈—", "はあー": "哈—", "はぁー": "哈—",
    "ひひ": "嘻嘻", "ひひひ": "嘻嘻嘻", "ふふっ": "呵呵",
    "ふんふん": "哼哼", "んふ": "嗯呼",
    "ドクドク": "突突跳", "どくどく": "突突跳",
    "クルクル": "转啊转", "くるくる": "转啊转",
    "ゴリゴリ": "蹭呀蹭", "ごりごり": "蹭呀蹭",
    "クリクリ": "搓搓", "くりくり": "搓搓",
    "カリカリ": "挠挠", "かりかり": "挠挠",
    "カチカチ": "硬邦邦", "かちかち": "硬邦邦",
    "トントン": "咚咚", "とんとん": "咚咚",
    "ビクビク": "一颤一颤", "びくびく": "一颤一颤",
    "すりすり": "蹭蹭", "ずりずり": "来回蹭",
    "ぐりぐり": "揉压", "こりこり": "揉捏",
    "ぬちぬち": "黏黏的", "ぬるぬる": "滑溜溜",
    "ぱくぱく": "一张一合", "もぐもぐ": "嚼嚼",
    "ちゅうちゅう": "吸吮", "ちゅくちゅく": "啵啾啵啾",
    "シュッシュッ": "咻咻", "しゅっしゅっ": "咻咻",
    "しこしこ": "撸撸", "シコシコ": "撸撸",
    # 语气/气声
    "もぅ": "唔", "もう": "唔", "んー": "嗯—", "んん": "嗯嗯", "んんん": "嗯嗯嗯",
    "ふふ": "呵呵", "ふふふ": "呵呵呵", "あーーー": "啊———", "あ〜": "啊～",
    "はぁ": "哈啊", "はあ": "哈啊", "うぅ": "呜…", "うう": "呜…",
    "うん": "嗯", "うーん": "嗯—", "えっ": "诶", "ええ": "嗯", "あっ": "啊",
    # 动作/水声（音·声类）
    "ピチャピチャ": "啪叽啪叽", "ぴちゃぴちゃ": "啪叽啪叽",
    "ビチャビチャ": "啪嗒啪嗒", "びちゃびちゃ": "啪嗒啪嗒",
    "ぐちゅぐちゅ": "咕啾咕啾", "グチュグチュ": "咕啾咕啾",
    "ジュプ": "啾噗", "じゅぷ": "啾噗", "ぬちゅ": "啵啾", "ヌチュ": "啵啾",
    "ずりずり": "蹭蹭", "ズリズリ": "蹭蹭",
    "しこしこ": "撸撸", "シコシコ": "撸撸",
    "くりくり": "转着揉揉", "ゴシゴシ": "搓搓", "ごしごし": "搓搓",
    "ぎゅ": "抱紧", "ぎゅー": "抱紧—", "ギュー": "抱紧—",
    "キュー": "啾", "チュー": "啾", "ちゅ": "啾", "ちゅっ": "啾",
    "ぴゅっ": "噗", "びゅっ": "噗", "ぴゅ": "噗", "びゅ": "噗",
    "カチャカチャ": "咔哒咔哒", "がちゃがちゃ": "咔哒咔哒",
    "パチパチ": "啪啪", "ぱちぱち": "啪啪",
    "いやいや": "不不", "やだやだ": "不要不要",
    # A8 接入实测补充（语料高频）
    "うっ": "呜…", "んっ": "嗯…", "ふっ": "呼…", "ぐっ": "唔…",
    "はぁー": "哈啊—", "へー": "嘿—", "へへ": "嘿嘿", "へへへ": "嘿嘿嘿",
    "ふぅ": "呼…", "ふぅー": "呼—", "ふー": "呼—", "うー": "唔—",
    "あぁ": "啊啊", "ああ": "啊啊", "うう…": "呜呜…",
    "んーっ": "嗯—…", "ぴゅー": "噗—", "もー": "唔—", "もぅー": "唔—",
    "うんうん": "嗯嗯", "ふふー": "呵呵—", "へへっ": "嘿嘿",
    "シコシコシコ": "撸撸撸",
    "バブバブ": "咿呀咿呀", "ばぶばぶ": "咿呀咿呀",
    "コリコリ": "揉揉", "こりこり": "揉揉", "ドロドロ": "黏糊糊",
    "ちんちん": "小肉棒", "チンチン": "小肉棒",
    "んーー": "嗯——", "んーーー": "嗯———",
    # B2 实测补充：ビリビリ=酥麻/麻（不是「电击」——那是模型脑补）
    "ビリビリ": "酥麻酥麻", "びりびり": "酥麻酥麻", "ビリッ": "麻",
}


def zh_for(span):
    """给一个非实义 span 选中文对应。命中固定表优先；否则按类别兜底。

    兜底口径（宁可中性也别音译成汉字）：
      · 片假名 → 「…」（用省略号表示持续的气声/动作音）
      · 平假名含小写/长音 → 「…」
      · 其余重复型 → 单字重复（用首字）
    """
    if span in ZH_MAP:
        return ZH_MAP[span]
    bare = re.sub(r"\u30fc", "", span)
    if not bare:
        return "…"
    # 重复型（ピチャピチャ 类）→ 取单元重复两次
    m = REPEAT_RE.match(bare)
    if m:
        u = m.group(1)
        if u in ZH_MAP:
            return ZH_MAP[u] + ZH_MAP[u]
        return "…"
    return "…"


def zh_for_known(span):
    """只返回**表内确定**的中文对应；表外返回 None（**不**伪装成「…」）。

    与 `zh_for` 的区别：`zh_for` 兜底返回「…」，那是给旧协议用的。
    D9/D11 要求表外拟声标 `unknown_nonlexical` 而不是伪装成省略号，
    所以这里严格区分「表内命中」和「表外未知」。
    """
    if span in ZH_MAP:
        return ZH_MAP[span]
    bare = re.sub(r"\u30fc", "", span)
    if not bare:
        return None
    m = REPEAT_RE.match(bare)
    if m:
        u = m.group(1)
        if u in ZH_MAP:
            return ZH_MAP[u] + ZH_MAP[u]
    return None


# ---- D9 第三路：纯非实义独立 cue → 确定性直出（不过模型）----
# 判据：整句只有拟声/气声 + 标点，没有任何可翻译的实义内容，且每段都能在 ZH_MAP 里
# 找到确定中文。这类句子过 7B 毫无意义（模型只能瞎猜），而且会引入波动
# （`はぁ` 有时「哈」有时「啊」）。直接用表里的值 → 零成本、零波动。
# 实测全库这类 cue 占 0.6%（约 130/22064），单句省一次采样。
_NONLEX_ONLY = re.compile(r"[、。…‥・\s!?！？~〜ー—\-]+")


def pure_nonlexical(ja_text):
    """整句是否「纯非实义」且可确定性直出。返回中文串或 None。

    ⚠️ 表外拟声（ZH_MAP 里没有）**不能**直出——那是 D9 第一路的 unknown 清单，
       要留给模型靠上下文猜，不能替它拍板。
    """
    if not ja_text:
        return None
    vals = []
    rest = []
    for seg in re.split(r"([\u3040-\u309f\u30a0-\u30ff\u30fc]+)", ja_text):
        if not seg:
            continue
        if KANA_RUN.fullmatch(seg) and _is_moan_token(seg):
            zh = zh_for_known(seg)
            if zh:
                vals.append(zh)
                continue
        rest.append(seg)
    if not vals:
        return None
    leftover = _NONLEX_ONLY.sub("", "".join(rest))
    if len(leftover) > 2:
        return None                    # 还有实义内容 → 必须过模型
    zh = "…".join(vals)
    # ⚠️ 问号要保留：`クルクルクルクル?` 是疑问（"要转吗？"），直出丢了问号
    #    就变成陈述（实测踩到）。感叹号同理。
    if re.search(r"[?？]", ja_text):
        zh += "？"
    elif re.search(r"[!！]", ja_text):
        zh += "！"
    return zh


def restore_zh(masked_zh, spans):
    """把中文里的 <M1> 换成**中文对应**。返回 (restored, problems)。

    与 restore_ja 的区别：这里是翻译场景，占位符要填中文，不是填回日文。
    problems 非空 → 调用方应重试（或回退该句初译）。
    """
    if not spans:
        return masked_zh, []

    want = {ph: zh_for(orig) for ph, orig in spans}
    found = re.findall(r"<M\d+>", masked_zh)
    seen = {}
    for ph in found:
        seen[ph] = seen.get(ph, 0) + 1

    problems = []
    if len(found) != len(want):
        problems.append("占位符数量 %d != 输入 %d" % (len(found), len(want)))
    for ph in seen:
        if ph not in want:
            problems.append("出现未知占位符 %s" % ph)
    for ph in want:
        if ph not in seen:
            problems.append("漏掉 %s（应填 %s）" % (ph, want[ph]))
    for ph, n in seen.items():
        if n > 1:
            problems.append("%s 重复 %d 次" % (ph, n))

    out = masked_zh
    for ph, zh in want.items():
        out = out.replace(ph, zh)
    if re.findall(r"<M\d+>", out):
        problems.append("还原后仍残留占位符")
    return out, problems


# ---- 自检：只测灵敏度 + 往返，不测精度（GPT 第五轮）----
CASES = [
    ("もぅ…", True), ("んー", True), ("あーーー", True), ("んんん", True),
    ("ピチャピチャ", True), ("ぐちゅぐちゅ", True), ("しこしこ", True),
    ("ビリビリ", True),                   # B2 实测补充：酥麻，应遮（防脑补成「电击」）
    ("ふふ", True), ("キュー", True), ("ぴちゃぴちゃ", True),
    ("いやいや", False),                 # ⚠️ 实义（「不不」）——A8 接入实测改判
    # 不应遮蔽（真词 / 称谓 / 助词 / 实义叠词）
    ("もうおちんぽちょっと大きくなってる", False),
    ("早く寝れるわけじゃない", False),
    ("私はあんたみたいに友達がいるわけじゃないから", False),
    ("ここにある", False),               # ここ=这里（曾误遮）
    ("そろそろ寝る", False),              # そろそろ=差不多该（曾误遮）
    ("どんどん気持ちよくしてもらえる", False),   # どんどん=不断（曾误遮，A8 实测）
    ("見ててね", False),                 # てて=词尾（曾误遮）
    ("ゼロゼロゼロ", False),              # 念「零」是台词（B2 实测曾误遮）
    ("ゼロから始める", False),
    ("コーヒーを飲む", False), ("気持ちいいですか", False),
    ("いや", False), ("ねえ", False), ("ダメ", False),
    ("いい", False), ("ママ", False), ("リリア", False),
]


def selftest():
    bad = []
    for ja, want in CASES:
        masked, spans = mask_ja(ja)
        restored, problems = restore_ja(masked, spans)
        ok_rt = (restored == ja) and not problems
        got = bool(spans)
        if want != got or not ok_rt:
            bad.append((ja, want, got, masked, problems))
        print("  %-36s 遮蔽=%-5s 往返=%s" % (ja[:34], got, "✓" if ok_rt else "✗"))
    # restore_zh 的校验用例：漏填 / 重复 / 残留必须被抓到
    rc_cases = [
        ("<M1>唔", [("<M1>", "もぅ")], True, "正常单占位符"),
        ("<M1><M2>", [("<M1>", "もぅ"), ("<M2>", "ふふ")], True, "两占位符正常"),
        ("唔", [("<M1>", "もぅ")], False, "漏掉占位符→应报错"),
        ("<M1><M1>", [("<M1>", "もぅ")], False, "重复占位符→应报错"),
        ("<M1>", [("<M1>", "もぅ"), ("<M2>", "ふふ")], False, "数量不足→应报错"),
        ("<M9>", [("<M1>", "もぅ")], False, "未知占位符→应报错"),
    ]
    print()
    for zh, spans, want_ok, desc in rc_cases:
        out, probs = restore_zh(zh, spans)
        ok = not probs
        mark = "✓" if ok == want_ok else "✗"
        print("  %-30s → %-14s 问题=%s %s" % (zh, out[:12], probs or "无", mark))
        if ok != want_ok:
            bad.append((desc, want_ok, ok, zh, probs))
    print()
    if bad:
        print("  ❌ 失败 %d 条：" % len(bad))
        for ja, want, got, masked, probs in bad:
            print("     %r 期望=%s 实际=%s 问题=%s" % (ja[:22], want, got, probs))
    else:
        print("  ✅ 全过（%d 应遮 + %d 不应遮 + %d restore_zh 校验；"
              "这是灵敏度+往返检查，不是精度）"
              % (sum(1 for _, w in CASES if w), sum(1 for _, w in CASES if not w),
                 len(rc_cases)))

    # ---- M2 协议（GPT 第十轮 Q2）：〔中文字面值〕的三档校验 ----
    # 核心值 = zh_for(orig)，如 はぁ→「哈啊」（**不含**省略号）
    print()
    print("  --- M2 协议：mask_ja(literal=True) + check_literal ---")
    _SP = [("〔哈啊…〕", "はぁ")]
    # (模型返回的中文, spans, 期望通过?, 期望 wrapper, 说明)
    m2_cases = [
        ("〔哈啊…〕哥哥…", _SP, True, "bracket", "括号完整=成功"),
        ("（哈啊…）哥哥…", _SP, True, "paren", "转全角括号=可接受（软审计）"),
        ("(哈啊…)哥哥…", _SP, True, "paren", "转半角括号=可接受"),
        ("哈啊…哥哥…", _SP, True, "none", "括号没了但核心在=可接受"),
        ("哈啊哥哥…", _SP, True, "none", "括号和省略号都没了=可接受"),
        ("哥哥…", _SP, False, "missing", "核心消失=失败"),
        ("〔哈啊…〕〔哈啊…〕哥哥", _SP, False, "bracket", "重复=失败"),
        ("<M1>哥哥…", _SP, False, "missing", "残留抽象占位符=失败"),
        ("〔哈啊…〕哥哥 はぁ", _SP, False, "bracket", "残留原始日文=失败"),
        ("哈啊…哥哥（旁白）", _SP, True, "none", "旁白的（）不剥（只动登记 span 周边）"),
        ("〔哈啊…〕哥哥〔哈啊…〕", _SP, False, "bracket", "多处重复也是失败"),
        # 对撞实验实测：核心值 = 省略号（兜底值）时，模型会保留〔…〕包装。
        # 这类必须靠「空包装兜底剥」处理，且**不能**把正文里的其他「…」判成重复。
        ("烧过的面皮〔…〕肉", [("〔…〕", "もも")], True, "bracket",
         "省略号核心：剥空包装，且不误判正文省略号"),
        ("……果然不容易醒来……〔…〕", [("〔…〕", "ギュ")], True, "bracket",
         "省略号核心：正文已有省略号时也不能判重复"),
    ]
    for zh, spans, want_ok, want_wrap, desc in m2_cases:
        out, probs, info = check_literal(zh, spans)
        ok = not probs
        wrap_ok = info["wrapper"] == want_wrap
        mark = "✓" if (ok == want_ok and wrap_ok) else "✗"
        print("     %-22s → %-14s 通过=%-5s wrapper=%-8s %s"
              % (zh[:20], out[:12], ok, info["wrapper"], mark))
        if ok != want_ok or not wrap_ok:
            bad.append((desc, want_ok, ok, zh, probs))
    # literal 模式必须产出 〔中文〕 而不是 <M1>
    lit, lsp = mask_ja("もぅ…お兄ちゃん", literal=True)
    lit_ok = (LITERAL_OPEN in lit and "<M1>" not in lit and lsp)
    print("     literal 产出：%r  %s" % (lit, "✓" if lit_ok else "✗"))
    if not lit_ok:
        bad.append(("literal mask_ja", True, lit_ok, lit, lsp))
    return bad


if __name__ == "__main__":
    print("=== A8 非实义 span 遮蔽器 · 自检 ===\n")
    bad = selftest()
    print()
    demo = "もぅ…お兄ちゃんのもっと舐めたい…ピチャピチャ…ふふ…んー"
    masked, spans = mask_ja(demo)
    print("演示：%r" % demo)
    print("  遮蔽后：%r" % masked)
    print("  占位符：%s" % spans)
    rest, probs = restore_ja(masked, spans)
    print("  还原回日文：%r\n  问题：%s" % (rest, probs or "无"))
    # 翻译场景：模型返回的中文里带占位符
    zh_demo = "<M1>…哥哥再舔多一点…<M2>…<M3>…<M4>"
    rz, pz = restore_zh(zh_demo, spans)
    print("  模型中文：%r" % zh_demo)
    print("  还原成中文：%r\n  问题：%s" % (rz, pz or "无"))
    sys.exit(1 if bad else 0)
