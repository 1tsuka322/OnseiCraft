# -*- coding: utf-8 -*-
"""R122：自建「日中口语 + 成人向用语」术语库（按 Gemini 给的三步方案落地）。

Gemini 的方案（用户转述）
------------------------
① 优先补：成人向身体部位/生理现象 > 口语缩略粘连（〜ておく→とく、〜ちゃった）> 高频语气词 > 惯用句
② 挖法：双语词对齐（FastAlign/GIZA++）、共现统计、正则精准捞取 —— 「自动化计算 + 局部人工筛选」
③ 验证：黄金种子词表查召回 + 每候选抽 3–5 个上下文人工扫

本脚本的落地方式
----------------
- **词对齐**：没有 FastAlign/GIZA++，用「关联强度」近似——ja 词元 ↔ 中文 2–4 字 n-gram 的
  条件概率 × 专指度（IDF 惩罚「的/了/我」这类通用片段）。对术语抽取这个目标足够，
  且完全可复现（不依赖外部二进制）。
- **共现统计**：对目标词取左右各 2 个 ja 词元，报 top 搭配。
- **正则捞取**：核心成人向词的正则 → 聚句 → 定向人工审。
- **验证**：内置 100 词黄金种子表查召回；每候选抽 5 个上下文，直接生成给 Gemini 的复审批。

产出
----
  regset/r122/terms.json        候选术语表（ja → 中文候选 + 分数 + 证据句数）
  regset/r122/collocations.json 目标词的左右搭配
  regset/r122/seed_recall.md    种子词召回报告
  regset/r122/review/prompts/   给 Gemini 的复审提示词（每批 10 个候选术语）
"""
import os
import re
import sys
import json
import math
import glob
import collections

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "regset", "r122")
CORPUS = os.path.join(ROOT, ".workbuddy/style/parallel.json")

# ---- 黄金种子表（Gemini 方案③：100 个核心高频成人用语/惯用语）----
SEED = """おまんこ まんこ おちんぽ ちんぽ チンポ ちんこ おっぱい お乳 乳首 乳頭
ふたなり 処女 童貞 中出し 種付け ザーメン ざーめん 精液 精子 射精 イク イかせる
アクメ 絶頂 おかず 自慰 オナニー しこしこ 手コキ フェラ フェラチオ イラマチオ
咥える しゃぶる 舐める クンニ まん汁 愛液 我慢汁 がまん汁 おもらし 放尿 おしっこ
肛門 アナル ケツマンコ 肉便器 孕ませ 妊娠 子宮 膣 ヴァギン クリトリス クリくり
敏感 感じる きもちいい 気持ちいい だらしない だらしねぇ 喘ぐ あえぐ 啼く 鳴く
勃起 ふにゃふにゃ 硬い ぬるぬる ぐちゅぐちゅ びちゃびちゃ ぴゅっぴゅ どくどく
びゅるる 噴く 飛び散る 垂れる 溢れる 注入 受け止める 奥 突く 掻き回す 締め付ける
吸い付く きゅんきゅん とろける 溶ける ふやける バカになる おかしくなる 狂う
しごく 扱く しごき 抜いて 挟む パイズリ 膝枕 騎乗位 正常位 後背位 寝バック
恥ずかしい はずかしい やめて だめ ダメ いく イく 逝く 果てる 乱れる""".split()

# 正则捞取：核心成人向词的假名/汉字结构
REGEXES = [
    (r"[おオ]?まんこ|ま〇こ|マンコ", "女性器"),
    (r"[おオ]?ちん[ぽポ]|ちんこ|チンポ|チンコ", "男性器"),
    (r"ざーめん|ザーメン|精液|精子", "精液"),
    (r"がまん汁|我慢汁|我慢治療", "我慢汁族"),
    (r"おもらし|放尿|おしっこ", "排泄"),
    (r"イかせ|イク|いく|絶頂|アクメ", "高潮"),
    (r"フェラ|しゃぶろ|咥え", "口交"),
    (r"しこしこ|しごか|扱|手コキ", "手交"),
    (r"アナル|肛門|ケツ", "肛交"),
    (r"孕ませ|種付|中出", "受孕"),
]

ZH_STOP = set("的了是我你他她不在人都一上也很到说要去会着没看自己这那个什么怎么")


def ja_tokens(s):
    """ja 词元（janome，取表层 + 读音）。"""
    out = []
    for t in _TK.tokenize(s):
        pos = t.part_of_speech.split(",")[0]
        if pos in ("助詞", "助動詞", "記号", "補助記号"):
            continue
        surf = t.surface
        if len(surf) < 2 and not re.match(r"[ぁ-んァ-ヶ一-鿿]", surf):
            continue
        out.append(surf)
    return out


def zh_ngrams(s, lo=2, hi=4):
    """中文 n-gram（无 jieba，用字 n-gram；术语抽取足够）。"""
    s = re.sub(r"[，。！？、；：「」（）…—\s]", "", s)
    out = []
    for n in range(lo, hi + 1):
        for i in range(len(s) - n + 1):
            out.append(s[i:i + n])
    return out


def main():
    global _TK
    from janome.tokenizer import Tokenizer
    _TK = Tokenizer()
    os.makedirs(OUT, exist_ok=True)

    pairs = json.load(open(CORPUS, encoding="utf-8")).get("pairs") or []
    print("语料 %d 对，开始分词…" % len(pairs))

    # 目标词表：Gemini 方案①的优先类别。**按子串匹配**而不是 janome 词元——
    # janome 会把 `おまんこ` 切成 `お+まんこ`、`ザーメン` 切成 `ザー+メン`，
    # 导致成人向核心词在词元频次里全是 0（实测 おまんこ 语料 1607 次但词元 0 次）。
    targets = set(SEED)
    gp = os.path.join(ROOT, ".workbuddy/style/glossary.json")
    if os.path.exists(gp):
        targets |= set(json.load(open(gp, encoding="utf-8")).keys())
    for rx, _ in REGEXES:
        for m in re.findall(rx, "おまんこ おちんぽ ざーめん がまん汁 おもらし アクメ フェラ しこしこ アナル 孕ませ"):
            targets.add(m)
    targets = {t for t in targets if len(t) >= 2}
    print("目标词 %d 个（子串匹配）" % len(targets))

    ja_tok_freq = collections.Counter()
    zh_ng_freq = collections.Counter()
    ctx = collections.defaultdict(collections.Counter)   # ja 词 → zh n-gram 计数
    left = collections.defaultdict(collections.Counter)
    right = collections.defaultdict(collections.Counter)
    sents = collections.defaultdict(list)                # ja 词 → [(ja, zh)]

    for k, p in enumerate(pairs):
        ja, zh = p.get("ja") or "", p.get("zh") or ""
        if not ja or not zh:
            continue
        toks = ja_tokens(ja)
        ngs = zh_ngrams(zh)
        for t in toks:
            ja_tok_freq[t] += 1
        for g in ngs:
            zh_ng_freq[g] += 1
        hits = set(t for t in targets if t in ja)
        for t in set(toks) | hits:
            for g in set(ngs):
                ctx[t][g] += 1
            sents[t].append((ja, zh))
            if t in hits:
                ja_tok_freq[t] += 1
            for i, t2 in enumerate(toks):
                if t2 == t and i > 0:
                    left[t][toks[i - 1]] += 1
                if t2 == t and i + 1 < len(toks):
                    right[t][toks[i + 1]] += 1
        if (k + 1) % 20000 == 0:
            print("  %d/%d" % (k + 1, len(pairs)), flush=True)

    # ---- 候选术语：ja 词元 → top 中文 n-gram ----
    N = max(1, len(pairs))
    terms = {}
    for t, f in ja_tok_freq.items():
        if f < 5 or len(t) < 2:
            continue
        if not re.search(r"[ぁ-んァ-ヶ一-鿿]", t):
            continue
        EDGE = set("的了着是我你他她不在人都一上也很到说要去会着没看这那个什么怎么把被和与在有把")
        cand = []
        for g, c in ctx[t].most_coming(400) if False else ctx[t].most_common(400):
            if g in ZH_STOP or zh_ng_freq[g] < 3:
                continue
            if g[0] in EDGE or g[-1] in EDGE:
                continue          # `的肉棒`/`肉棒的` 这类碎片，信息量在更长的候选里
            # 专指度：该 n-gram 多大比例出现在含 t 的句子里
            spec = c / max(1, zh_ng_freq[g])
            score = spec * math.log(1 + c)
            cand.append({"zh": g, "n": c, "spec": round(spec, 3), "score": round(score, 3)})
        cand.sort(key=lambda x: -x["score"])
        keep = []
        for c in cand:
            if any(c["zh"] != k["zh"] and c["zh"] in k["zh"] and c["n"] < 0.6 * k["n"]
                   for k in keep):
                continue
            keep.append(c)
        if keep:
            terms[t] = {"ja_freq": f, "n_sent": len(sents[t]),
                        "top": keep[:6], "samples": sents[t][:5]}
    json.dump(terms, open(os.path.join(OUT, "terms.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("候选术语 %d 个" % len(terms))

    # ---- 共现 ----
    coll = {}
    for t in list(REGEX_TARGETS):
        if t in ja_tok_freq:
            coll[t] = {"left": left[t].most_common(8), "right": right[t].most_common(8)}
    json.dump(coll, open(os.path.join(OUT, "collocations.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # ---- 种子召回 ----
    hit = [s for s in SEED if s in terms]
    miss = [s for s in SEED if s not in terms]
    md = ["# 种子词召回（%d 词）" % len(SEED), "",
          "命中 **%d（%.0f%%）**，未命中 %d" % (len(hit), 100 * len(hit) / len(SEED), len(miss)),
          "", "## 未命中（要么语料里没出现，要么频次 <5）", ""]
    md += ["- %s（语料 %d 次）" % (s, ja_tok_freq.get(s, 0)) for s in miss]
    open(os.path.join(OUT, "seed_recall.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")
    print("种子召回 %d/%d" % (len(hit), len(SEED)))

    # ---- 给 Gemini 的复审批（每批 10 个候选术语，带 3 个上下文）----
    rp = os.path.join(OUT, "review", "prompts")
    os.makedirs(rp, exist_ok=True)
    keys = sorted(terms, key=lambda k: -terms[k]["ja_freq"])[:200]
    HEAD = ("你是日译中术语审校。下面是脚本从 8.8 万句对里自动挖出的「日文词 → 中文候选」，"
            "每条带 3 个真实上下文。\n"
            "请判断：这个中文候选是不是该日文词在**字幕里应该用的固定译法**？\n"
            "输出一行一条：`[编号] 采纳|中文` / `[编号] 拒绝|理由` / `[编号] 存疑|理由`\n"
            "只采纳**固定对应**的（一个日文词几乎总译成那个中文）；偶尔对应的拒绝。\n"
            "全部判完最后单独一行 `[[[END]]]`。\n\n")
    for k in range(0, len(keys), 10):
        batch = keys[k:k + 10]
        lines = [HEAD, "======== 第 %d 批 ========" % (k // 10 + 1)]
        for i, t in enumerate(batch, 1):
            d = terms[t]
            lines.append("【%d】日文 `%s`（语料 %d 句）→ 候选 %s"
                         % (i, t, d["ja_freq"],
                            " / ".join("%s(%d)" % (c["zh"], c["n"]) for c in d["top"][:3])))
            for ja, zh in d["samples"][:3]:
                lines.append("   · %s → %s" % (ja[:52], zh[:40]))
        lines.append("\n共 %d 条，请逐条判定。" % len(batch))
        open(os.path.join(rp, "T_%02d.txt" % (k // 10 + 1)), "w", encoding="utf-8").write("\n".join(lines))
    print("复审提示词 %d 个 → %s" % ((len(keys) + 9) // 10, rp))


REGEX_TARGETS = ["おまんこ", "ちんぽ", "ザーメン", "我慢汁", "おもらし", "アクメ",
                 "フェラ", "しこしこ", "アナル", "孕ませ", "まん汁", "乳首", "処女", "イク"]

if __name__ == "__main__":
    main()
