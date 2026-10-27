# -*- coding: utf-8 -*-
r"""语法/语气模式挖掘 v1（2026-10-22，learningskill 第三条通道）。

`mine_terms.py` 挖**术语**，本脚本挖**语言知识**——scan_violations 里那些手写的
`Q_JA_STRIP` / `NEG_JA_STRIP` / `NEG_MODAL_JA` 本质是日语语法知识，应该从语料里挖出来，
而不是靠人记。四个类目：

  ① `na_adj`     「ない」结尾但**不是否定**的い形容词（だらしない/情けない/つまらない…）
  ② `ka_end`     句尾「か」的三种性质（疑问 / 推量 / 非疑问），按 UniDic 词性判定
  ③ `filler`     フィラー・感動詞（なんか/まあ/えっと…），單独出现不成命题
  ④ `inference`  推量形（でしょう/だろう/んだろ），中文不给「吧/吗」是正确译法

判据纪律（同 mine_terms）：
  · 全部基于 **janome/UniDic 词性**，不靠正则猜；
  · 必须**跨作品稳定**（≥N 部作品）才收录，单作品高频的不收（角色口癖不算语法）；
  · 每条带 hit / works 计数，写进 `data/style/grammar_patterns.json`；
  · ⚠️ 产出的列表**不是终态**：要接一门禁（`test_regressions.py` 的 QNEG_CASES）
    确认并入后不把已知正例判错，才允许 scan_violations 使用。

用法：
  python mine_grammar.py [--lib <seg_lib目录>] [--min-works 3] [--min-hit 8] [--out <json>]
"""
import os
import re
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json
import glob
import argparse
import collections
from struct import unpack

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
DEFAULT_LIB = os.path.join(ROOT, "data", "tmp", "seg_lib")
DEFAULT_OUT = os.path.join(ROOT, "data", "style", "grammar_patterns.json")

from janome.tokenizer import Tokenizer  # noqa: E402

_TK = Tokenizer()
_LEX = None


def lex_init():
    """初始化 UniDic 直接查词（比 tokenize 更权威：tokenize 会把词切碎）。

    ⚠️ 坑：`Matcher.run()` 只吃 **bytes**，传 str 永远匹配不上（实测全部 False）。
    """
    global _LEX
    if _LEX is None:
        tk = Tokenizer(mmap=False)
        _LEX = (tk.sys_dic, tk.matcher)
    return _LEX


def lex_entries(surface):
    """返回 UniDic 里该表面的全部 (词性, 基本形)；词表没有则空。"""
    sd, m = lex_init()
    try:
        r = m.run(surface.encode("utf-8"))
    except Exception:
        return []
    if not r or not r[0]:
        return []
    out = []
    for e in r[1]:
        try:
            num = unpack("I", e)[0]
            ex = sd.lookup_extra(num)
            out.append((ex[0], ex[5]))
        except Exception:
            continue
    return out


def is_na_adjective(surface):
    """UniDic 判定：该形式是**形容词/ナイ形容词语干**（而不是動詞+ない 的否定）。"""
    for pos, _base in lex_entries(surface):
        if pos.startswith("形容詞") or "ナイ形容詞語幹" in pos:
            return True
    return False


def lex_pos(surface):
    """该表面的首选词性（用于把候选分类/否决）。"""
    es = lex_entries(surface)
    return es[0][0] if es else None

# 「ない」前的功能词：出现这些说明「ない」是**语法否定**，不是い形容词
NEG_FUNC = {"しか", "だけ", "より", "まで", "は", "が", "を", "に", "と", "も", "から",
            "て", "で", "る", "るる", "き", "く"}
# 推量助動詞（です/だ/う 的基本形）
INFER_BASE = {"です", "だ", "う"}
# イ形容詞として扱いたい活用語尾
ADJ_TAIL = re.compile(r"(ない|よい|いい)$")


def pos1(t):
    return t.part_of_speech.split(",")[0]


def pos_detail(t):
    return t.part_of_speech.split(",")


ASR_ROOT = os.path.join(ROOT, "data", "draft")


def load_pairs(libdir):
    """句子级 (ja, zh) 对——挖「中文实际怎么译」必须成对，只有 ja 不够。"""
    for f in sorted(glob.glob(os.path.join(libdir, "seg_*.json"))):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for p in d.get("pairs") or []:
            if p.get("ja") and p.get("zh"):
                yield p["ja"], p["zh"], p.get("src") or os.path.basename(f)


def mine_zh_mood(pairs):
    """挖「日文语气标记 → 中文实际用了什么」（Round 13）。

    目的：把 pragmatics_hints 里**手写**的 `_MOOD` 中文语气词表变成有语料背书的表。
    已验证：`でしょ/じゃん`（341 句）→ 吧？66/吧。42/吗？29/不是27/应该14，全部已在现表内；
    `っけ`（22 句）→ 只有 吗？，也在现表内。**没挖到新词 = 现表无需扩充**，
    这本身是结论（否则又会回到「靠人猜」）。
    """
    out = collections.defaultdict(collections.Counter)
    for ja, zh, src in pairs:
        if not re.search(r"でしょ|じゃん|っけ|だろう|でしょうか", ja):
            continue
        for k in range(len(zh) - 1):
            g = zh[k:k + 2]
            # ⚠️ 必须放行标点：语气词的关键形态是「吧？」「吗？」「吧。」，
            #    只匹配汉字会把最重要的几项全滤掉（第一版就这么漏的）。
            if re.match(r"^[\u4e00-\u9fff～？?。！!]+$", g):
                out[g][src] += 1
    return out


def load_corpus(libdir):
    for f in sorted(glob.glob(os.path.join(libdir, "seg_*.json"))):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for p in d.get("pairs") or []:
            if p.get("ja"):
                yield p["ja"], p.get("src") or os.path.basename(f)


def load_asr_corpus(root=ASR_ROOT):
    """全量 ASR 语料（2026-10-22 Round 11）：比句子级语料大 10 倍，能挖到罕见口语模式。

    ⚠️ 必须**按 (作品, 轨) 去重**：同一轨有 asr / asr_v1_20261020 / asr_pre_rescan
    三个版本，不去重会把每个模式算三遍、works 也虚高。
    """
    order = ("asr", "asr_v1_20261020", "asr_pre_rescan")
    by_track = {}
    for rj in sorted(os.listdir(root)):
        rjdir = os.path.join(root, rj)
        if not os.path.isdir(rjdir):
            continue
        for sub in order:
            d = os.path.join(rjdir, sub)
            if not os.path.isdir(d):
                continue
            for f in sorted(glob.glob(os.path.join(d, "*.json"))):
                tr = os.path.basename(f)[:-5]
                by_track.setdefault((rj, tr), f)
    n = 0
    for (rj, tr), f in sorted(by_track.items()):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for s in d.get("segments") or []:
            txt = s.get("text") or ""
            if txt.strip():
                n += 1
                yield txt, rj
    print("ASR 语料: %d 轨 / %d 段" % (len(by_track), n))


def mine_na_adj(sents):
    """名詞/形状詞 + 助動詞「ない」→ 候选い形容词。

    「行かない」是 動詞+ない（否定）；「だらしない」「情けない」是 名詞+ない（形容词）。
    UniDic 的词性就是分界线，不用正则猜。
    """
    cnt = collections.defaultdict(collections.Counter)
    for ja, src in sents:
        for t in _TK.tokenize(ja):
            if t.surface != "ない" and t.base_form != "ない":
                continue
            if pos1(t) != "助動詞":
                continue
            # 回溯找实义头（跳过助词）
            toks = list(_TK.tokenize(ja[:ja.find(t.surface)] or ""))
            head = None
            for h in reversed(toks):
                if pos1(h) in ("助詞", "動詞"):
                    if h.surface in NEG_FUNC or pos1(h) == "動詞":
                        head = None
                        break
                    continue
                head = h
                break
            if head is None or pos1(head) not in ("名詞", "形状詞"):
                continue
            surf = head.surface + "ない"
            if not ADJ_TAIL.search(surf) or len(surf) < 3:
                continue
            cnt[surf][src] += 1
    return cnt


def verify_na_adj(cnt):
    """候选过两道关：① UniDic 说是形容词；② 跨作品稳定。

    被 UniDic 否决的（動詞+ない 的真否定）单独返回——这是学习产出的一部分：
    语料觉得像、词典说不是，这种对照本身就是知识。
    """
    ok, vetoed = {}, {}
    for k, per in cnt.items():
        if is_na_adjective(k):
            ok[k] = per
        else:
            vetoed[k] = {"pos": lex_pos(k), "per": per}
    return ok, vetoed


def classify_ka(s):
    """单句句尾「か」三态判定：question / inference / non_question / None。

    坑 100 的三处修正都在这：
      ① `ないんですか`/`いいんですか` 是**否定疑问**，不是推量；
      ② `〜しましょうか` 是**邀约**，中文要给「吧」；
      ③ `〜というか/っていうか` 是接续的「或者说」，**根本不是疑问**。
    ⚠️ 两个踩过的坑：`ん` 在「ないんですか」里被 UniDic 标成**名词**（不能按 POS 过滤）；
       「というか」被切成 と+いう+か（不能用 token 匹配，必须用字符串后缀）。
    """
    s = s.rstrip("。、…！!？? 　")
    if not s.endswith("か"):
        return None
    toks = list(_TK.tokenize(s))
    if not toks or toks[-1].surface != "か":
        return None
    if pos1(toks[-1]) != "助詞":
        return "non_question"
    chain = toks[-4:-1]
    neg_q = any(x.surface in ("ない", "ん", "の", "ませ")
                or x.base_form in ("ない", "の", "ん", "ませ") for x in chain)
    invite = any(x.surface in ("ましょ", "ましょう") for x in chain)
    quot = s.endswith(("というか", "っていうか", "ていうか", "つーか", "っつーか", "っつか"))
    if quot:
        return "non_question"
    prev = toks[-2] if len(toks) >= 2 else None
    if (not neg_q and not invite and prev is not None and pos1(prev) == "助動詞"
            and prev.base_form in INFER_BASE):
        return "inference"
    return "question"


def mine_ka_end(sents):
    """句尾「か」的三态判定：question / inference / non_question。"""
    out = {"question": collections.defaultdict(collections.Counter),
           "inference": collections.defaultdict(collections.Counter),
           "non_question": collections.defaultdict(collections.Counter)}
    for ja, src in sents:
        k = classify_ka(ja)
        if k is None:
            continue
        out[k][ja[-6:]][src] += 1
    return out


def mine_filler(sents):
    """フィラー / 句首感動词：单独出现时不构成命题。"""
    cnt = collections.defaultdict(collections.Counter)
    for ja, src in sents:
        toks = list(_TK.tokenize(ja))
        if not toks:
            continue
        t0 = toks[0]
        if pos1(t0) == "フィラー":
            cnt[t0.base_form][src] += 1
        elif pos1(t0) == "感動詞" and len(t0.surface) <= 4:
            cnt[t0.surface][src] += 1
    return cnt


def mine_inference(sents):
    """推量形（でしょう/だろう/んだろ）及其中文对应不需要疑问标记。"""
    cnt = collections.defaultdict(collections.Counter)
    for ja, src in sents:
        for m in re.finditer(r"(でしょう|だろう|んだろ|でしょ|だろ|だろうか|ですか)", ja):
            cnt[m.group(1)][src] += 1
    return cnt


def pack(cnt, min_works, min_hit):
    out = {}
    for k, per in cnt.items():
        hit = sum(per.values())
        if hit >= min_hit and len(per) >= min_works:
            out[k] = {"hit": hit, "works": len(per)}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["hit"]))


def gap_report(data):
    """与 scan_violations 里**手写**的知识对照，报出「语料没覆盖」的部分。

    手写列表是这几轮逐条核验出来的，不一定错；但语料挖不到 → 说明它罕见，
    要么补语料、要么接受「只能靠人记」。写进产出，让缺口显式化。
    """
    sys.path.insert(0, os.path.join(ROOT, "data", "skills",
                                    "kikoeru-subtitle", "scripts"))
    try:
        import scan_violations as SV
    except Exception as e:
        print("!! 无法 import scan_violations：%s" % e)
        return {}
    mined_na = set(data.get("na_adj") or {})
    mined_inf = set(data.get("inference") or {})
    gaps = {
        "na_adj_手写未覆盖": sorted(set(SV.NEG_JA_STRIP) - mined_na),
        "inference_手写未覆盖": sorted(set(SV.Q_JA_STRIP) - mined_inf),
        "na_adj_挖到但需人判": sorted(mined_na - set(SV.NEG_JA_STRIP)),
    }
    for k, v in gaps.items():
        print("\n== %s (%d)" % (k, len(v)))
        for w in v:
            print("   %s" % w)
    return gaps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", default=DEFAULT_LIB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--min-works", type=int, default=3)
    ap.add_argument("--min-hit", type=int, default=8)
    ap.add_argument("--no-gap", action="store_true", help="跳过与手写列表的对照")
    ap.add_argument("--src", choices=["seg", "asr"], default="seg",
                    help="seg=句子级语料（干净但小）；asr=全量 ASR（大 10 倍，能挖罕见模式）")
    a = ap.parse_args()

    if a.src == "asr":
        sents = list(load_asr_corpus())
        print("语料段数: %d" % len(sents))
    else:
        sents = list(load_corpus(a.lib))
        print("语料句数: %d" % len(sents))
    if not sents:
        print("!! 语料为空")
        return 1

    ka = mine_ka_end(sents)
    # 中文语气词表（只有 seg 源有成对译文；asr 源没有 zh）
    zh_mood = {}
    if a.src == "seg":
        zh_mood = pack(mine_zh_mood(load_pairs(a.lib)), a.min_works, a.min_hit)
    na_ok, na_vetoed = verify_na_adj(mine_na_adj(sents))
    data = {
        "_meta": {"lib": a.lib, "src": a.src, "sents": len(sents),
                  "min_works": a.min_works, "min_hit": a.min_hit,
                  "note": "learningskill 第三条通道：语法/语气模式，供 scan_violations 并入"},
        "na_adj": pack(na_ok, a.min_works, a.min_hit),
        "ka_end": {k: pack(v, a.min_works, a.min_hit) for k, v in ka.items()},
        "zh_mood": zh_mood,
        "filler": pack(mine_filler(sents), a.min_works, a.min_hit),
        "inference": pack(mine_inference(sents), a.min_works, a.min_hit),
    }
    if a.src == "asr":
        a.out = a.out.replace(".json", "_asr.json")
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(data, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    for k in ("na_adj", "filler", "inference"):
        print("\n== %s (%d)" % (k, len(data[k])))
        for w, m in list(data[k].items())[:12]:
            print("   %-14s hit=%-4d works=%d" % (w, m["hit"], m["works"]))
    if zh_mood:
        print("\n== zh_mood（日文语气标记的中文译法，%d 项）" % len(zh_mood))
        for w, m in list(zh_mood.items())[:14]:
            print("   %-8s hit=%-4d works=%d" % (w, m["hit"], m["works"]))
    for k in ("question", "inference", "non_question"):
        print("\n== ka_end.%s (%d)" % (k, len(data["ka_end"][k])))
        for w, m in list(data["ka_end"][k].items())[:10]:
            print("   %-14s hit=%-4d works=%d" % (w, m["hit"], m["works"]))
    # UniDic 否决的候选（語料像、词典说不是）——显式落盘，供人工复核
    data["na_adj_vetoed"] = {
        k: {"pos": v["pos"], "hit": sum(v["per"].values()), "works": len(v["per"])}
        for k, v in sorted(na_vetoed.items(), key=lambda kv: -sum(kv[1]["per"].values()))}
    print("\n== na_adj 被 UniDic 否决（真否定，别并进去）(%d)" % len(data["na_adj_vetoed"]))
    for k, v in list(data["na_adj_vetoed"].items())[:12]:
        print("   %-12s pos=%-22s hit=%d" % (k, v["pos"], v["hit"]))
    gaps = {} if a.no_gap else gap_report(data)
    data["_meta"]["gaps"] = gaps
    json.dump(data, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n写出: %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
