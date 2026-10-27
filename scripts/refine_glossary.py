# -*- coding: utf-8 -*-
"""术语表重构：去子串冗余 + 删单字汉字条目 + 用语料分词找缺口。

背景（2026-10-18）：`glossary_v2.json` 的 ja 字段是**字面共现**统计出来的子串，
不是词条。1491 条里 810 条是别的条目的严格子串（同 zh），strict 档 316 条里 136 条。
典型污染：`孫 → 小穴`（お孫さん 会命中）、`宮 → 子宫`、`持 → 舒服`（任何"持"都中）。

用法：
  python refine_glossary.py --report      # 只出报告，不写文件
  python refine_glossary.py --apply       # 写 glossary_v3.json + 备份
  python refine_glossary.py --gaps        # 用语料分词找「高频但术语表没有」的词
"""
import json
import os
import re
import sys
import shutil
import collections

STY = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "..", "style"))
V2 = os.path.join(STY, "glossary_v2.json")
V3 = os.path.join(STY, "glossary_v3.json")
PAR = os.path.join(STY, "parallel.json")

# 单字汉字条目一律不要：共现统计对单字毫无术语价值，只会污染注入
# （`孫→小穴` 在「お孫さん」里命中；`宮→子宫` 在「神宮」里命中）。
# 例外：单字假名（如 `え`）也没价值，一并删。
PUNCT_RE = re.compile(r"[?？!！。、,，…\s「」『』（）()\[\]【】]")
MIN_JA_LEN = 2
# 条目必须至少这么多次**作为连续 token 序列**出现在语料里（否则是跨界碎片）
MIN_TOKEN_FREQ = 3

# 这些 zh 是「虚词/语气」而非术语，共现再高也不该进术语表
STOP_ZH = {"的", "了", "是", "我", "你", "他", "她", "不", "在", "有", "就",
           "也", "都", "还", "很", "会", "说", "这", "那", "个", "上", "下"}

# ---- 分词合法性校验（本轮核心）----
# `glossary_v2` 的 ja 是**字面 n-gram 共现**，大量条目是跨词边界的碎片：
#   `、気持`（标点开头）、`い気持`（形容词词尾开头）、`持ちいい`（动词连用形开头）、
#   `気持ちい`（切在「いい」中间）。这些既不是词，注入时还会误导模型。
# 判据：用 janome 分词，看 token 序列是不是**一个完整的词/词组**。
_TK = None


def _tk():
    global _TK
    if _TK is None:
        from janome.tokenizer import Tokenizer
        _TK = Tokenizer()
    return _TK


def is_lexical(j):
    """条目是不是「一个词或一个正常词组」（而不是跨界碎片）。

    合法形态（只认这三种）：
      1. 单个内容词：気持ち / お兄さん / 舐める
      2. 名词 + 形容词/助动词（気持ちいい / 気持ちよく / 舐めたく）
      3. 名词 + 名词（おまんこ先生 / 子宮口）
    其余（标点开头/结尾、助词开头、动词连用形起头、切在词中间）一律判非法。
    """
    toks = list(_tk().tokenize(j))
    if not toks:
        return False
    pos = [t.part_of_speech.split(",") for t in toks]
    # 首尾不能是标点/助词/助动词
    if pos[0][0] in ("記号", "助詞", "助動詞", "接続詞", "フィラー"):
        return False
    if pos[-1][0] in ("記号", "助詞", "助動詞"):
        return False
    # 拼回来必须等于原文（防 janome 对未知词的奇怪切分）
    if "".join(t.surface for t in toks) != j:
        return False
    n = len(toks)
    if n == 1:
        return True
    if n == 2:
        a, b = pos[0], pos[1]
        if a[0] == "名詞" and b[0] in ("形容詞", "動詞", "助動詞"):
            return True
        if a[0] == "名詞" and b[0] == "名詞":
            return True
        return False
    return False


def load_v2():
    return json.load(open(V2, encoding="utf-8"))


def dedupe_substring(rows):
    """同 zh 下去掉「子串碎片」和「超串死条目」。

    ⚠️ 不能简单「长的优先」：`気持ち`(hit 4466) 是 `気持ちい`(hit 更低) 的子串，
       但 `気持ち` 才是真词，`気持ちい` 是切在「気持ちいい」中间的碎片。
    → 按 **hit 降序** 处理（真词单独出现频率最高，先落位），再做双向判断：
       · 当前条目是已保留条目的子串 → 是碎片，删
       · 当前条目**包含**已保留条目（且 hit 更低）→ 是死条目（那个序列实际不会出现），删
    """
    by_zh = collections.defaultdict(list)
    for r in rows:
        by_zh[r["zh"]].append(r)
    keep, dropped = [], []
    for zh, rs in by_zh.items():
        survivors = []
        for r in sorted(rs, key=lambda x: (-x.get("hit", 0), -len(x["ja"]))):
            j = r["ja"]
            sub = next((k for k in survivors if j != k and j in k), None)
            if sub:
                dropped.append(dict(r, _why="substring_of=%s" % sub))
                continue
            sup = next((k for k in survivors if k != j and k in j), None)
            if sup:
                dropped.append(dict(r, _why="superseded_by=%s" % sup))
                continue
            survivors.append(j)
            keep.append(r)
    return keep, dropped


def build_seq_freq():
    """统计语料里**连续 token 序列**（长度 1–4）的出现句数。

    为什么不直接用字面 n-gram：字面切片会切在词中间（`マッ`/`ルフ`/`マホ` 是
    `マッサージ`/`エルフ`/`スマホ` 的碎片）。按 token 边界切，序列才是「词或词组」。
    结果缓存到 style/.seq_freq.json（分词 10 万句要 ~50s，重复跑没必要）。
    """
    cache = os.path.join(STY, ".seq_freq.json")
    if os.path.exists(cache):
        try:
            return collections.Counter(json.load(open(cache, encoding="utf-8")))
        except Exception:
            pass
    from janome.tokenizer import Tokenizer
    tk = Tokenizer()
    p = json.load(open(PAR, encoding="utf-8"))
    seq = collections.Counter()
    for pr in p.get("pairs") or []:
        ja = pr.get("ja") or ""
        toks = []
        for tok in tk.tokenize(ja):
            pos = tok.part_of_speech.split(",")[0]
            if pos in ("記号", "フィラー"):
                continue
            base = tok.base_form if pos in ("動詞", "形容詞") else tok.surface
            toks.append(base)
        seen = set()
        for i in range(len(toks)):
            for n in range(1, 5):
                if i + n > len(toks):
                    break
                s = "".join(toks[i:i + n])
                if s in seen:
                    continue
                seen.add(s)
                seq[s] += 1
    try:
        json.dump(dict(seq), open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return seq


_SEQ = None


def seq_freq(j):
    global _SEQ
    if _SEQ is None:
        _SEQ = build_seq_freq()
    return _SEQ.get(j, 0)


def is_substring_fragment(j, min_ratio=0.8, max_base=50):
    """j 是不是「更长词的一部分」（碎片）。

    判据：语料里存在更长的 token 序列 k（k 含 j），且 k 的出现次数占 j 的 ≥ min_ratio
    → j 几乎只在 k 内部出现 → 碎片。
      ルフ   : エルフ 12 / ルフ 12 = 1.00 → 碎片 ✓
      マホ   : スマホ  7 / マホ  7 = 1.00 → 碎片 ✓
      良すぎ : 気持ち良すぎ 5 / 良すぎ 5 = 1.00 → 碎片 ✓

    ⚠️ 必须加 `max_base` 门槛：`気持ち`(base 600) 的更长序列 `気持ちいい`(2026)
       比率 3.4 ≥ 0.8，但它不是碎片——`気持ち`（心情）和 `気持ちいい`（舒服）
       是两个词，`気持ちいい` 是**常用搭配**不是更长的词。
       → 只有「自身出现很少（< max_base）」时，包含率才有判碎片的意义。
    """
    base = seq_freq(j)
    if base <= 0 or base >= max_base:
        return False
    sf = _SEQ
    best = 0
    for k, c in sf.items():
        if len(k) > len(j) and j in k and len(k) - len(j) <= 3:
            if c > best:
                best = c
    return best > 0 and best / float(base) >= min_ratio


def refine(rows):
    dropped = []
    keep = []
    for r in rows:
        j = (r.get("ja") or "").strip()
        if PUNCT_RE.search(j):
            # `好き?` 这类带标点的条目是统计伪影：剥离标点后的 `好き` 本来就在表里
            dropped.append(dict(r, _why="punct"))
        elif len(j) < MIN_JA_LEN:
            dropped.append(dict(r, _why="single_char"))
        elif r.get("zh") in STOP_ZH:
            dropped.append(dict(r, _why="stop_zh"))
        elif not is_lexical(j):
            dropped.append(dict(r, _why="fragment"))
        elif seq_freq(j) < MIN_TOKEN_FREQ:
            dropped.append(dict(r, _why="rare_seq", _tf=seq_freq(j)))
        elif is_substring_fragment(j):
            dropped.append(dict(r, _why="substring_fragment"))
        else:
            keep.append(r)
    # ⚠️ 不做「同 zh 子串去重」：`気持ち`（心情）和 `気持ちいい`（舒服）是两个词。
    # 但同一 zh 下「A ⊂ B 且 A 出现更少」的，是 ASR 缺字变体（`気持` ⊂ `気持ち`），
    # 降级为 observe（保留供 audit，不占 strict 注入名额）。
    by_zh = collections.defaultdict(list)
    for r in keep:
        by_zh[r["zh"]].append(r)
    for zh, rs in by_zh.items():
        for r in rs:
            if r.get("tier") != "strict":
                continue
            for o in rs:
                if o is r or o["ja"] == r["ja"]:
                    continue
                if r["ja"] in o["ja"] and seq_freq(r["ja"]) < seq_freq(o["ja"]):
                    r["tier"] = "observe"
                    r["_note"] = "variant_of=%s" % o["ja"]
                    break
    keep.sort(key=lambda r: (-r.get("hit", 0), r["ja"]))
    return keep, dropped


def tokenize_corpus():
    """用 janome 分词，返回 (词频, 词→zh 投票)。只跑一次，缓存到 tmp。"""
    from janome.tokenizer import Tokenizer
    tk = Tokenizer()
    p = json.load(open(PAR, encoding="utf-8"))
    pairs = p.get("pairs") or []
    freq = collections.Counter()
    vote = collections.defaultdict(collections.Counter)
    KEEP_POS = ("名詞", "動詞", "形容詞", "副詞", "感動詞")
    for pr in pairs:
        ja = pr.get("ja") or ""
        zh = pr.get("zh") or ""
        seen = set()
        for tok in tk.tokenize(ja):
            pos = tok.part_of_speech.split(",")[0]
            if pos not in KEEP_POS:
                continue
            base = tok.base_form if pos in ("動詞", "形容詞") else tok.surface
            if len(base) < 2 or base in seen:
                continue
            seen.add(base)
            freq[base] += 1
    return freq, len(pairs)


def gaps(topn=200):
    rows = load_v2()
    have = {(r["ja"]) for r in rows}
    have_zh = collections.Counter()
    for r in rows:
        have_zh[r["zh"]] += 1
    freq, n = tokenize_corpus()
    print("语料 %d 句对，分词得到 %d 个候选词" % (n, len(freq)))
    miss = [(w, c) for w, c in freq.most_common(topn * 6) if w not in have]
    print("\n=== 高频但术语表缺失（前 %d）===" % topn)
    for w, c in miss[:topn]:
        print("  %-8s %6d" % (w, c))
    return miss


# ---------------- 权威条目 + 浏览器查证条目 ----------------
# ① hard_rules.txt 里用户指定的 40 条映射：优先级最高，直接进 strict。
HARD_RULES = os.path.join(STY, "hard_rules.txt")

# ② 浏览器查证（2026-10-18）：缩约形/感叹词，模型最容易糊成"看起来像词"的近义词。
#    `くっさ` 实测被译成「苦涩」（味觉词），实际是 `くさい`（臭）的年轻人口语强调形。
#    来源：Wiktionary くさい 词条（stinking/smelly）+ HiNative 母语者解释
#    （くさー! / くさーい! / くっさい! / くっさ! 都是"臭死人了"，非正式日语）。
VERIFIED = [
    # (ja, zh, note)
    ("くっさ", "好臭", "くさい 的强调口语形（非正式）；**不是**「苦涩」"),
    ("くっさい", "好臭", "同上，加强形"),
    ("くさーい", "好臭", "同上，拖音形"),
    ("くさっ", "好臭", "同上，促音形"),
    ("くせえ", "好臭", "くさい 的粗俗形；作后缀时是「…味」（ガキくせえ→幼稚）"),
    ("くっせえ", "好臭", "同上，加强形"),
    ("におい", "味道", "气味（中性）；臭い 才是「臭」"),
    ("匂い", "味道", "同上（汉字形）"),
]

# ③ 明确**禁止**的错误译法（写进 hard_rules 的禁令节，不是术语映射）
BANNED = [
    ("苦しい / 苦しこ / 苦しめ", "苦涩",
     "「苦涩」是中文味觉词；日语 苦しい 是「难受/痛苦」。"
     "实测 `くっさ`、`苦しこー`、`お笑顔さらして苦しこー` 三处全被译成「苦涩」，全是错的。"),
    ("くさい 家族", "苦涩",
     "くさい 是「臭」，与「苦」无关。"),
]


def load_hard_rules():
    """从 hard_rules.txt 提取 `ja -> zh` 权威映射。"""
    out = []
    if not os.path.exists(HARD_RULES):
        return out
    for ln in open(HARD_RULES, encoding="utf-8"):
        m = re.match(r"\s*(\S+)\s*->\s*(\S+)\s*$", ln)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def build_v3(rows):
    keep, dropped = refine(rows)
    idx = {r["ja"]: r for r in keep}
    added = []
    # 权威映射：覆盖同名条目的 zh 并升为 strict
    for ja, zh in load_hard_rules():
        if ja in idx:
            r = idx[ja]
            if r["zh"] != zh:
                r["_prev_zh"] = r["zh"]
            r["zh"] = zh
            r["tier"] = "strict"
            r["source"] = "hard_rules"
        else:
            r = {"ja": ja, "zh": zh, "hit": 0, "support": 0, "cond": 1.0,
                 "tier": "strict", "source": "hard_rules"}
            keep.append(r)
            idx[ja] = r
            added.append(ja)
    # 查证条目
    for ja, zh, note in VERIFIED:
        if ja in idx:
            r = idx[ja]
            r["zh"] = zh
            r["tier"] = "strict"
            r["source"] = "verified"
            r["note"] = note
        else:
            r = {"ja": ja, "zh": zh, "hit": 0, "support": 0, "cond": 1.0,
                 "tier": "strict", "source": "verified", "note": note}
            keep.append(r)
            idx[ja] = r
            added.append(ja)
    # 已知可疑映射：降级为 observe，留人工核（不占 strict 注入名额）
    SUSPECT = {
        "マンゴー": "ASR 常把 おまんこ 听成 マンゴー；真说芒果时会误译，降 observe",
        "性液": "非标准词（应为 精液/淫液），疑似 ASR 产物",
        "良すぎ": "良すぎ = 太好了，不是「舒服」",
        "初めて": "初めて = 第一次/初次，不是「第一」",
        "こと好き": "跨界碎片，不是词",
        "興奮する": "与 興奮 重复，保留 興奮 即可",
    }
    for ja, why in SUSPECT.items():
        if ja in idx and idx[ja].get("tier") == "strict":
            idx[ja]["tier"] = "observe"
            idx[ja]["note"] = why
    keep.sort(key=lambda r: (0 if r.get("tier") == "strict" else 1,
                             -r.get("hit", 0)))
    return keep, dropped, added


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--report"
    if mode == "--gaps":
        gaps()
        return
    rows = load_v2()
    keep, dropped, added = build_v3(rows)
    print("原 %d 条 → v3 %d 条（删 %d，新增权威/查证 %d）"
          % (len(rows), len(keep), len(dropped), len(added)))
    why = collections.Counter(d["_why"].split("=")[0] for d in dropped)
    for k, v in why.most_common():
        print("  删因 %-20s %d" % (k, v))
    st = collections.Counter(r.get("tier") for r in keep)
    print("保留档位", dict(st))
    src = collections.Counter(r.get("source") for r in keep)
    print("来源", dict(src))
    print("\n--- 权威条目（hard_rules）---")
    print(" ".join("%s→%s" % (r["ja"], r["zh"]) for r in keep
                   if r.get("source") == "hard_rules"))
    print("\n--- 查证条目（browser verified）---")
    for r in keep:
        if r.get("source") == "verified":
            print("  %-8s → %-6s  %s" % (r["ja"], r["zh"], r.get("note", "")))
    print("\n--- 降级为 observe 的可疑映射 ---")
    for r in keep:
        if r.get("note") and r.get("tier") == "observe":
            print("  %-8s → %-6s  %s" % (r["ja"], r["zh"], r["note"]))
    if mode == "--apply":
        if os.path.exists(V3):
            shutil.copy2(V3, V3 + ".bak")
        json.dump(keep, open(V3, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        json.dump(dropped, open(os.path.join(STY, "glossary_v3_dropped.json"), "w",
                                encoding="utf-8"), ensure_ascii=False, indent=1)
        print("\n已写 %s（%d 条）+ glossary_v3_dropped.json（%d 条，可复查）"
              % (V3, len(keep), len(dropped)))


if __name__ == "__main__":
    main()
