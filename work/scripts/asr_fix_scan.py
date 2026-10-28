# -*- coding: utf-8 -*-
"""ASR 改错阶段（2026-10-24，用户提出：先整轨改错、再翻译）。

为什么要有这一步
----------------
现有管线是 ASR → 初译 → 润色。初译拿到的是**带错字的日文**，错字会被一路带到成品。
而错误往往只有「看整轨」才看得出来——实测 RJ9000006 轨01 的「象」，
佐证（お母さんがやってんじゃない?）在 **95 秒之后**，cue 级扫描根本看不到。

两条确定性规则（不需要模型，可批量跑）
------------------------------------
  A 多版本不一致：同轨两版 ASR 在同一时间窗文本不同 → 必有一版错，候选。
  B 同轨重复怪词：同一 token 在一轨出现 ≥2 次、且不在 janome 词典里 / 是生僻汉字词
    → 系统性误识候选（「象」×2 就是这样浮出来的）。

⚠️ 已知盲区：**两版一致 ≠ 正确**。「ゾウ」在 v1/v2 都出现（`象って…` / `ゾウって…`），
   规则 A 抓不到，只能靠 B + 整轨语义复核（LLM 或人工）。

产出（不覆盖 asr/）
------------------
  data/draft/<rj>/asr_fix/<track>.candidates.json   候选清单（含证据）
  data/draft/<rj>/asr_fix/<track>.review.md         人工/LLM 复核单
  人工确认后写 <track>.json（结构同 asr/，但带 fix_log），translate 优先读它。

用法：
  python asr_fix_scan.py --rj RJ9000006 --track "01 プロローグ…mp3"
  python asr_fix_scan.py --rj RJ9000006 --track "..." --apply-fix 3=お母 --apply-fix 5=うんうん
"""
import os
import sys
import json
import glob
import argparse
import collections
import re

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DRAFT = os.path.join(ROOT, "data", "draft")

# styleloc 解析知识表位置（KIKOERU_STYLE → data/style → presets）。
# 2026-10-28（R151）：原来只有 load_errata() 在函数内 import，另两处
# （load_do_not_change / load_all_ja）引用的是模块级全局 → NameError，
# 于是 **02_asr_fix 一碰到 errata 命中就整段崩**，errata 表在管线里等于没生效。
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import styleloc  # noqa: E402
import asr_words  # noqa: E402  text 改了必须同步 words（cue 管线读 words 不读 text）

KANJI = lambda c: "\u4e00" <= c <= "\u9fff"


def _resolve(ver_dir, track):
    """轨名 → 实际文件。不同版本目录的命名不统一（坑 126）：

    - 多数作品：`<track>.mp3.json`（带音频扩展名）
    - RJ9000007 的 `asr/`（台本打轴产物）：`<track>.json`（**没有** .mp3）
    - 同一个作品两个版本目录的轨名因此对不上，直接拼路径会 FileNotFoundError，
      表现为「该作品 A 规则 0 候选」（实测 RJ9000007 全 11 轨漏扫）。
    """
    import re as _re
    p = os.path.join(ver_dir, track + ".json")
    if os.path.exists(p):
        return p
    base = _re.sub(r"\.(mp3|wav|m4a|flac|ogg|opus)$", "", track, flags=_re.I)
    p = os.path.join(ver_dir, base + ".json")
    if os.path.exists(p):
        return p
    mt = _re.match(r"(\d+)", base)
    if mt and os.path.isdir(ver_dir):
        pat = _re.compile(r"0*%d[,._ ]" % int(mt.group(1)))
        cands = [f for f in os.listdir(ver_dir) if pat.match(f)]
        if len(cands) == 1:
            return os.path.join(ver_dir, cands[0])
    return None


def load(ver_dir, track):
    p = _resolve(ver_dir, track)
    if not p:
        return None
    return json.load(open(p, encoding="utf-8")).get("segments") or []


def align(a_segs, b_segs, tol=1.5):
    """按时间中点对齐。"""
    used, pairs = set(), []
    for a in a_segs:
        am = (float(a["start"]) + float(a["end"])) / 2
        best, bi = None, None
        for i, b in enumerate(b_segs):
            if i in used:
                continue
            bm = (float(b["start"]) + float(b["end"])) / 2
            if abs(bm - am) <= tol and (best is None or abs(bm - am) < abs(best - am)):
                best, bi = bm, i
        if bi is not None:
            used.add(bi)
            pairs.append((a, b_segs[bi]))
        else:
            pairs.append((a, None))
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rj", required=True)
    ap.add_argument("--track", default=None, help="不给则扫整部作品（--work 模式）")
    ap.add_argument("--base", default="asr_v1_20261020", help="主版本（成品对齐的那个）")
    ap.add_argument("--other", default="asr", help="对照版本")
    ap.add_argument("--apply-fix", action="append", default=[],
                    help="落地修正：'秒=改后文本'，可多次。写 asr_fix/<track>.json（带 fix_log），不覆盖 asr/")
    ap.add_argument("--why", default="人工确认（候选来自多版本/同轨重复 + 语义场复核）",
                    help="--apply-fix 写入 fix_log 的证据说明")
    ap.add_argument("--jsonl", default=None,
                    help="整部模式：把全部候选追加写进这个 jsonl（R109 日志化）")
    a = ap.parse_args()

    if a.track is None:
        scan_work(a.rj, a.base, a.other, a.jsonl)
        return
    main_track(a)


def _reading(tk, s):
    out = []
    for t in tk.tokenize(s or ""):
        r = t.reading
        if r and r != "*":
            out.append(r)
    return "".join(out)



_ERRATA = [None]


def load_errata():
    """已知系统性错字表（`data/style/asr_errata.json`）。

    为什么要有这条规则（2026-10-25）：A1/A2/A3 全靠「两版不一致」，
    可**两版一致地错**时它们全都看不见（`写生`/`処世` 就是这样漏了整年）。
    错字表是唯一能覆盖「两版同错」的确定性来源，且每条都有人工/双源背书。
    """
    if _ERRATA[0] is None:
        import styleloc  # noqa: E402
        p = styleloc.table("asr_errata.json")
        _ERRATA[0] = json.load(open(p, encoding="utf-8"))["entries"] if os.path.exists(p) else []
    return _ERRATA[0]


_DNC = [None]


def load_do_not_change():
    """表记差异白名单（`data/style/do_not_change.json`）。

    2026-10-26：Gemini 任务07 的 233 条「表记」判定 → 86 组成对写法。
    这些是**同一个词的不同表记**（`ちんぽ/チンポ`、`こと/事`、`だめ/ダメ`、`ぼっき/勃起`），
    任何工具都不得把其中一个当错字改掉。用途：errata 应用前过滤 + 候选生成时跳过。
    """
    if _DNC[0] is None:
        p = styleloc.table("do_not_change.json")
        try:
            d = json.load(open(p, encoding="utf-8"))
            _DNC[0] = {frozenset((x["a"], x["b"])) for x in (d.get("pairs") or [])}
        except Exception:
            _DNC[0] = set()
    return _DNC[0]


def is_table_variant(a, b):
    """两个写法是否只是表记差异（是 → 不许当错字处理）。"""
    return frozenset((a, b)) in load_do_not_change()


def scan_track(rj, track, base="asr_v1_20261020", other="asr", all_ja=None, tk=None,
               base_segs=None, other_segs=None):
    """扫一轨，返回候选列表（纯函数，供单轨与整部模式共用）。

    all_ja: 全库 ja 文本（B 规则的「全库罕见」判据）；None 则跳过该过滤。
    """
    from janome.tokenizer import Tokenizer
    tk = tk or Tokenizer()
    if base_segs is None:
        base_segs = load(os.path.join(DRAFT, rj, base), track)
    if other_segs is None and other:
        other_segs = load(os.path.join(DRAFT, rj, other), track)
    if not base_segs:
        return []
    base = base_segs
    other = other_segs
    cands = []

    # ---- A 多版本不一致 ----
    # 2026-10-24 修订：直接比文本会被标点/假名宽度/♡ 淹没（实测 A=129 里九成是这种）。
    # 改为**按读音比**：读音相同 = 正字法差异（不是错）；读音不同 = 真差异。
    #   A1 同读音不同字 → 汉字层误识（最高价值，如 近世/禁制、写生/射精）
    #   A2 读音也不同   → 内容不同或分段不同
    if other:
        for A, B in align(base, other):
            if B is None:
                continue
            ta, tb = (A.get("text") or "").strip(), (B.get("text") or "").strip()
            if not ta or not tb or ta == tb:
                continue
            ra, rb = _reading(tk, ta), _reading(tk, tb)
            if ra == rb:
                continue                     # 只是正字法/标点差异
            if len(ra) == len(rb) and sum(1 for x, y in zip(ra, rb) if x != y) <= 2:
                diff = [(x, y) for x, y in zip(ra, rb) if x != y]
                cands.append({"rule": "A1同读音不同字", "t": round(float(A["start"]), 1),
                              "base": ta, "other": tb, "diff": diff})
            else:
                cands.append({"rule": "A2内容不同", "t": round(float(A["start"]), 1),
                              "base": ta, "other": tb, "diff": ["读音不同"]})

    # ---- A3 字符级对齐差异（2026-10-24 晚新增，R109 实测必需）----
    # 为什么加：A1/A2 按**段中点 ±1.5s** 配对，段数不同的两版（如 asr/=台本 254 段 vs
    # v1 274 段）会整体错位，真错字（秘逆→被虐、写生→射精、教養→強要）就漏掉——
    # R109 实测 A1 对同音错误召回只有 71.9%，9 条漏检里 5 条是 RJ9000014 的段错位。
    # 改成**整轨字符级对齐**（difflib），差异再映射回段，边界不再依赖段切分。
    if other:
        import difflib
        import unicodedata

        def _n(s):
            s = unicodedata.normalize("NFKC", s or "")
            return "".join(ch for ch in s if not unicodedata.category(ch)[0] in "PSZC")

        na = "".join(_n(s.get("text")) for s in base)
        nb = "".join(_n(s.get("text")) for s in other)
        if len(na) > 30 and len(nb) > 30:
            cum, acc = [], 0
            for s in base:
                cum.append(acc)
                acc += len(_n(s.get("text")))
            cum.append(acc)
            import bisect as _bs
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, na, nb,
                                                               autojunk=False).get_opcodes():
                if tag == "equal":
                    continue
                a, b = na[i1:i2], nb[j1:j2]
                if max(len(a), len(b)) < 2:
                    continue
                si = max(0, min(_bs.bisect_right(cum, i1) - 1, len(base) - 1))
                same = bool(a and b and _reading(tk, a) == _reading(tk, b))
                cands.append({"rule": "A3字符级差异", "t": round(float(base[si]["start"]), 1),
                              "seg": si, "asr_span": a, "other_span": b,
                              "same_reading": same,
                              "base": (base[si].get("text") or ""),
                              "other": b, "diff": [tag]})

    # ---- C 已知错字表（2026-10-25 新增，唯一能覆盖「两版同错」的规则）----
    for e in load_errata():
        ja, fx = e.get("ja"), e.get("fix")
        if not ja or not fx:
            continue
        for s0 in base:
            tx = s0.get("text") or ""
            if ja in tx:
                cands.append({"rule": "C已知错字", "t": round(float(s0["start"]), 1),
                              "asr_span": ja, "other_span": fx,
                              "base": tx, "other": tx.replace(ja, fx),
                              "evidence": e.get("evidence", ""),
                              "same_reading": True})

    # ---- B 同轨重复怪词 ----
    tok_pos = collections.defaultdict(list)   # token -> [(t, text)]
    for s in base:
        t = (s.get("text") or "").strip()
        if not t:
            continue
        for tok in tk.tokenize(t):
            surf = tok.surface
            if len(surf) < 2:
                continue
            if not (any(KANJI(c) for c in surf) or any("\u30a0" <= c <= "\u30ff" for c in surf)):
                continue
            if tok.part_of_speech.split(",")[0] in ("記号", "助詞", "助動詞"):
                continue
            tok_pos[surf].append((round(float(s["start"]), 1), t, tok.reading))

    # 全库词频：B 只保留「同轨重复 + 全库罕见」的 token
    # （否则 時間/特別/姉さん 这类正常词全混进来，实测 40 条里只有 1 条真错）
    # 统计口径：token 作为**子串**在全库 ja 里出现的次数。
    for surf, occ in tok_pos.items():
        if len(occ) < 2:
            continue
        oov = any(r == "*" for _, _, r in occ)          # 词典里没有
        rare_kanji = any(KANJI(c) for c in surf) and len(surf) <= 3
        if not (oov or rare_kanji):
            continue
        if all_ja is not None:
            cf = all_ja.count(surf) if surf else 0
            if cf > 30:                                  # 全库常见 → 不是误识候选
                continue
        else:
            cf = -1
        cands.append({"rule": "B同轨重复怪词", "t": occ[0][0], "token": surf,
                      "n": len(occ), "oov": oov, "corpus_hits": cf,
                      "occ": [{"t": t, "text": x} for t, x, _ in occ]})
    # ---- 表记差异白名单过滤（2026-10-26，Gemini 任务07 的 233 条「表记」→ 86 对）----
    # A3 的差异段如果是「同一个词的两种表记」（ちんぽ/チンポ、こと/事、ぼっき/勃起…），
    # 它不是错字，不该进候选队列（否则每轮都要人工重复否掉）。
    if cands:
        before = len(cands)
        cands = [c for c in cands
                 if not (c.get("rule") == "A3字符级差异"
                         and is_table_variant(c.get("asr_span") or "",
                                              c.get("other_span") or ""))]
        if before != len(cands):
            print("    [白名单] A3 表记差异过滤 %d 条（%d→%d）"
                  % (before - len(cands), before, len(cands)), flush=True)
    return cands


# ---- errata 运行时监控（2026-10-26 R133，GPT Q1 要求）----
# 每条 guard 要能回答两个问题：① 有多少命中是**新语境**（ctx 白名单没覆盖）；
# ② 有多少被**作用域/边界**拒掉。新语境一律不自动改，落 review 清单。
PREFIX_STATS = {"cleaned": 0}

ERRATA_STATS = {"applied": 0, "ctx_hit": 0, "new_ctx": [], "scope_skip": [],
                "boundary_skip": [], "review_skip": 0, "multi_skip": 0}


def errata_stats(reset=False):
    d = {k: (v if not isinstance(v, list) else list(v)) for k, v in ERRATA_STATS.items()}
    if reset:
        for k in ERRATA_STATS:
            ERRATA_STATS[k] = [] if isinstance(ERRATA_STATS[k], list) else 0
    return d


def _kanji_boundary_ok(tx, ja):
    """可选严格检查：命中片段两侧不得紧贴汉字（仅 `strict_boundary: true` 的条目用）。

    ⚠️ 2026-10-26 R133 降级为**可选**：实测它对日语过严——`さっきっぽ向いちゃえ`
    `咲き走り液も` `否認効果` 全是该改的，却被汉字邻接误拦（日语本来就没有空格，
    汉字紧邻是常态）。真正防「复合词内部误命中」应该用**反例枚举**（`block_if`）。
    """
    i = tx.find(ja)
    while i >= 0:
        l = tx[i - 1] if i > 0 else ""
        r = tx[i + len(ja)] if i + len(ja) < len(tx) else ""
        if not (KANJI(l) or KANJI(r)):
            return True
        i = tx.find(ja, i + 1)
    return False


def errata_blocked(tx, ja, e):
    """该条在本段是否被**反例白名单**拦下。返回原因字符串或 None。

    `block_if`：已知「错形出现在更长合法词里」的正则列表（反例枚举，GPT Q1 要求）。
    `strict_boundary`：可选的汉字邻接检查（默认关，见 _kanji_boundary_ok 注释）。
    """
    import re as _re
    for pat in e.get("block_if") or []:
        if _re.search(pat, tx):
            return "block_if:%s" % pat
    if e.get("strict_boundary") and not _kanji_boundary_ok(tx, ja):
        return "strict_boundary"
    return None


def validate_errata(verbose=False):
    """表内自检：链式替换 / 重叠 / 缺 ctx 的 guard。返回问题列表（不抛异常）。"""
    import re as _re
    errata = [e for e in load_errata() if e.get("ja") and e.get("fix")]
    probs = []
    for e in errata:
        if e.get("mode") == "guard" and not e.get("ctx") and e.get("guard"):
            # 只有**负向断言**（(?! / (?<! ) 才算待迁移；正向白名单（ピュ|真っ白|…）本来就是 ctx
            if "(?!" in e["guard"] or "(?<!" in e["guard"]:
                probs.append(("DEPRECATED_NEG", e["ja"],
                              "仍是排除式负向断言，建议改正向 ctx 白名单"))
            else:
                probs.append(("LEGACY_POS", e["ja"],
                              "旧式正向 guard（等价 ctx 单条），建议搬进 ctx 数组"))
        if e.get("mode") == "guard" and not e.get("ctx") and not e.get("guard"):
            probs.append(("GUARD_NO_CTX", e["ja"], "guard 但没有 ctx/guard → 等于 auto，危险"))
        for o in errata:
            if o is e:
                continue
            # 链式：A 的替换结果里含 B 的错形 → A→B 会连改两次
            if e["fix"] and e["ja"] != o["ja"] and o["ja"] in e["fix"]:
                probs.append(("CHAIN", e["ja"], "替换结果含 %s（链式）" % o["ja"]))
            # 重叠：A 的错形是 B 错形的子串（同段同时命中会互相干扰）
            if e["ja"] != o["ja"] and e["ja"] in o["ja"] and len(e["ja"]) < len(o["ja"]):
                _ok = set(e.get("overlap_ok") or [])
                if o["ja"] not in _ok:
                    probs.append(("OVERLAP", e["ja"], "是 %s 的子串（结果相同可加 overlap_ok）" % o["ja"]))
        if e.get("ctx"):
            for c in e["ctx"]:
                try:
                    _re.compile(c)
                except Exception as ex:
                    probs.append(("BAD_CTX", e["ja"], str(ex)))
    if verbose:
        for t, ja, msg in probs:
            print("  [errata] %-14s %-12s %s" % (t, ja, msg))
    return probs


# ---- ASR 前缀垃圾清理（2026-10-27 R143）----------------------------------------
# 现象：RJ9000013 的 ASR 有 88 段带着 `数字+接`（`９接`/`３接`）、`数字+顿号`（`３、、`）、
# `「」` 这类**前缀垃圾**，导致翻译时整句被丢弃 → 成品出现**空字幕**（实测 19 行）。
# 全库统计：`数字+接` 55 / `数字+顿号` 15 / `「」` 18，**几乎全在 RJ9000013**。
# 只清这三种确定性前缀；清完为空就退回原文（宁可不改）。
PREFIX_JUNK = [
    re.compile(r"^\s*\d+接\s*"),
    re.compile(r"^\s*\d+[、,\s]{2,}"),
    re.compile(r"^\s*「」\s*"),
]


def clean_asr_prefix(t):
    """去掉 ASR 文本开头的确定性垃圾前缀；清空则返回原文。"""
    s = t or ""
    for _ in range(3):
        prev = s
        for p in PREFIX_JUNK:
            s = p.sub("", s, count=1)
        if s == prev:
            break
    s = s.strip()
    return s if s else (t or "")


def auto_apply_errata(draft, src_dir, fix_dir):
    """把「已知错字表」自动落到 asr_fix/（只做确定性替换，不调模型）。

    2026-10-26（R133）按 GPT Q1 改成**正向上下文白名单**模型：
      `mode=auto`   错形不是日语词，直接换（仍过边界检查 + 表记白名单）
      `mode=guard`  **必须**命中 `ctx`（正向白名单，任一命中即可）+ 作用域内 + 边界 OK
                    → 三者缺一不换，并把「错形出现但 ctx 没覆盖」记成 new_ctx（新语境）
      `mode=review` 永不自动换，只进清单
    另：`works` 限定作用域（作品号列表）；`boundary: false` 可关掉汉字邻接检查（默认开）。
    """
    import re as _re
    import hashlib
    errata = [e for e in load_errata() if e.get("ja") and e.get("fix")]
    if not errata or not os.path.isdir(src_dir):
        return 0
    work = os.path.basename(os.path.dirname(os.path.abspath(src_dir)))
    # 知识表指纹：用来判断 fix/ 里的旧产物是不是按当前表算的。
    # ⚠️ 以前这里是「fix 里已有同名文件就 continue」——**知识表更新后重跑 02
    #    等于什么都没做**（R154 实测：新增 9 条 errata，applied=0、files_touched=0，
    #    而日志还写着「知识表变了，重跑」）。这就是又一个「声明 ≠ 实际」。
    errata_hash = hashlib.md5(
        "|".join(sorted("%s→%s" % (e.get("ja", ""), e.get("fix", ""))
                        for e in errata)).encode("utf-8")).hexdigest()[:12]
    os.makedirs(fix_dir, exist_ok=True)
    n_total = 0
    for p in sorted(glob.glob(os.path.join(src_dir, "*.json"))):
        name = os.path.basename(p)
        dst = os.path.join(fix_dir, name)
        manual = []
        if os.path.exists(dst):
            try:
                old = json.load(open(dst, encoding="utf-8"))
            except Exception:
                old = {}
            if (old.get("fix_meta") or {}).get("errata_hash") == errata_hash:
                continue        # 表没变，跳过（增量语义保留）
            # 表变了 → 重做；但 `--apply-fix` 落地的人工修正必须留下
            manual = [sg for sg in (old.get("segments") or [])
                      if (sg.get("fix_log") or {}).get("rule") == "人工"]
        try:
            src = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        segs = src.get("segments") or []
        n = 0
        # 先清前缀垃圾（确定性，不调模型），再走 errata
        for sg in segs:
            _t0 = sg.get("text") or ""
            _t1 = clean_asr_prefix(_t0)
            if _t1 != _t0:
                sg["text"] = _t1
                sg["prefix_clean"] = _t0
                PREFIX_STATS["cleaned"] += 1
        for sg in segs:
            tx = sg.get("text") or ""
            for e in errata:
                if e["ja"] not in tx:
                    continue
                # 同段多次出现：默认跳过（防误伤），但作用域已核实的条目可 `all_ok: true` 全换
                if tx.count(e["ja"]) != 1 and not e.get("all_ok"):
                    ERRATA_STATS["multi_skip"] += 1
                    continue
                # 表记差异白名单（如 ちんぽ/チンポ）永不当错字
                if is_table_variant(e["ja"], e["fix"]):
                    continue
                mode = e.get("mode", "auto")
                if mode == "review":
                    ERRATA_STATS["review_skip"] += 1
                    continue
                # 作用域：限定作品时，范围外一律不动
                if e.get("works") and work not in e["works"]:
                    ERRATA_STATS["scope_skip"].append(
                        {"ja": e["ja"], "work": work, "track": name, "t": round(float(sg.get("start", 0)), 1)})
                    continue
                # 正向上下文白名单：guard 必须命中 ctx；命中不了 = 新语境 → 不换
                if mode == "guard":
                    if e.get("ctx"):
                        if not any(_re.search(c, tx) for c in e["ctx"]):
                            ERRATA_STATS["new_ctx"].append(
                                {"ja": e["ja"], "fix": e["fix"], "work": work, "track": name,
                                 "t": round(float(sg.get("start", 0)), 1), "text": tx})
                            continue
                        ERRATA_STATS["ctx_hit"] += 1
                    elif e.get("guard"):
                        # 旧式负向断言：仍执行，但记账（validate_errata 会提示迁移）
                        if not _re.search(e["guard"], tx):
                            ERRATA_STATS["new_ctx"].append(
                                {"ja": e["ja"], "fix": e["fix"], "work": work, "track": name,
                                 "t": round(float(sg.get("start", 0)), 1), "text": tx})
                            continue
                        ERRATA_STATS["ctx_hit"] += 1
                    else:
                        continue
                # 反例白名单 / 可选严格边界（GPT Q1：反例枚举 > 粗判据）
                _blk = errata_blocked(tx, e["ja"], e)
                if _blk:
                    ERRATA_STATS["boundary_skip"].append(
                        {"ja": e["ja"], "work": work, "track": name, "why": _blk,
                         "t": round(float(sg.get("start", 0)), 1), "text": tx})
                    continue
                new = tx.replace(e["ja"], e["fix"])
                sg["text"] = new
                # ⚠️ cue 管线（cues.flatten_for_cues）优先用 words、不看 text。
                #    只改 text 等于没改（实测：性液→精液 改对了，译文仍是「性液」）。
                if not asr_words.patch_words(sg, e["ja"], e["fix"]):
                    asr_words.clear_words(sg)
                sg["fix_log"] = {"old": tx, "new": new, "rule": "C已知错字",
                                 "evidence": e.get("evidence", ""), "mode": mode,
                                 "ctx": e.get("ctx", [])}
                tx = new
                n += 1
                ERRATA_STATS["applied"] += 1
        if manual:
            # 人工修正优先级最高：按起点覆盖回去（词级时间戳已对不上新文本，清掉）
            by_start = {round(float(x.get("start", 0)), 2): x for x in segs}
            for m in manual:
                k = round(float(m.get("start", 0)), 2)
                if k in by_start:
                    by_start[k]["text"] = m.get("text")
                    by_start[k]["fix_log"] = m.get("fix_log")
                    asr_words.clear_words(by_start[k])
            n += len(manual)
        if n:
            src["segments"] = segs
            src["fix_meta"] = {"applied": n, "source": "asr_errata.json",
                               "errata_hash": errata_hash,
                               "time": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")}
            json.dump(src, open(os.path.join(fix_dir, name), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            n_total += n
    return n_total


def auto_apply_green(draft, src_dir, fix_dir, other_ver="asr"):
    """自动落「绿灯」候选：同读音 + 双侧汉字 + 候选写法在同轨别处出现过。

    依据（R112 实测）：dev 89 轨里 23 条绿灯，**人工逐条核验 23/23 全真（precision 100%）**。
    安全边界：
    - 只处理**无台本**的作品（有台本的 asr/ 是台本文本，改 ASR 无意义）。
    - 已存在的 `asr_fix/<track>.json` 会被**读出后叠加**（不覆盖 errata 的修正）。
    - span 必须在段内唯一出现。
    """
    if not os.path.isdir(src_dir):
        return 0
    os.makedirs(fix_dir, exist_ok=True)
    KANJI = lambda x: any("\u4e00" <= c <= "\u9fff" for c in x)
    n_total = 0
    for p in sorted(glob.glob(os.path.join(src_dir, "*.json"))):
        name = os.path.basename(p)
        rj = os.path.basename(os.path.dirname(os.path.dirname(p)))
        if os.path.isdir(os.path.join(draft, rj, "script")):
            continue
        tgt = os.path.join(fix_dir, name)
        if os.path.exists(tgt):
            src = json.load(open(tgt, encoding="utf-8"))
        else:
            src = json.load(open(p, encoding="utf-8"))
        segs = src.get("segments") or []
        base_txt = "".join(x.get("text") or "" for x in segs)
        other = load(os.path.join(draft, rj, other_ver), name[:-5])
        if not other:
            continue
        n = 0
        for c in scan_track(rj, name[:-5], base_segs=segs, other_segs=other, all_ja=""):
            if not c["rule"].startswith("A3"):
                continue
            a, b = c.get("asr_span"), c.get("other_span")
            if not a or not b or not c.get("same_reading"):
                continue
            if not (KANJI(a) and KANJI(b)):
                continue
            if base_txt.count(b) < 1:            # 锚点：候选写法在同轨出现过
                continue
            si = c.get("seg", -1)
            if not (0 <= si < len(segs)):
                continue
            tx = segs[si].get("text") or ""
            if tx.count(a) != 1:
                continue
            new = tx.replace(a, b)
            segs[si]["text"] = new
            if not asr_words.patch_words(segs[si], a, b):
                asr_words.clear_words(segs[si])
            segs[si]["fix_log"] = {"old": tx, "new": new, "rule": "A3+绿灯",
                                   "evidence": "同读音+双侧汉字+同轨锚点（R112 实测 23/23 全真）"}
            base_txt = "".join(x.get("text") or "" for x in segs)
            n += 1
        if n:
            src["segments"] = segs
            src.setdefault("fix_meta", {})
            src["fix_meta"]["green"] = n
            json.dump(src, open(tgt, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            n_total += n
    return n_total


def load_all_ja():
    p = styleloc.table("parallel.json")
    if not os.path.exists(p):
        return None
    return "\n".join((x.get("ja") or "") for x in
                     json.load(open(p, encoding="utf-8")).get("pairs") or [])


def scan_work(rj, base, other, jsonl=None):
    """整部作品扫描（R109：候选日志化）。输出每轨 candidates.json + review.md，可选 jsonl。"""
    from janome.tokenizer import Tokenizer
    tk = Tokenizer()
    all_ja = load_all_ja()
    bdir = os.path.join(DRAFT, rj, base)
    tracks = sorted(os.path.splitext(os.path.basename(p))[0]
                    for p in glob.glob(os.path.join(bdir, "*.json")))
    outdir = os.path.join(DRAFT, rj, "asr_fix")
    os.makedirs(outdir, exist_ok=True)
    tot = collections.Counter()
    recs = []
    for tr in tracks:
        cands = scan_track(rj, tr, base, other, all_ja=all_ja, tk=tk)
        json.dump(cands, open(os.path.join(outdir, tr + ".candidates.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        for c in cands:
            tot[c["rule"][:2]] += 1
            r = {"rj": rj, "track": tr}
            r.update(c)
            recs.append(r)
    if jsonl:
        os.makedirs(os.path.dirname(jsonl) or ".", exist_ok=True)
        with open(jsonl, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("%s：%d 轨 · A1 %d / A2 %d / B %d → %s"
          % (rj, len(tracks), tot["A1"], tot["A2"], tot["B"], outdir))
    return recs


def main_track(a):
    base_dir = os.path.join(DRAFT, a.rj, a.base)
    other_dir = os.path.join(DRAFT, a.rj, a.other)
    base = load(base_dir, a.track)
    other = load(other_dir, a.track)
    if not base:
        sys.exit("主版本不存在：%s" % os.path.join(base_dir, a.track + ".json"))

    print("主版本 %s：%d 段；对照 %s：%d 段"
          % (a.base, len(base), a.other, len(other or [])))

    cands = scan_track(a.rj, a.track, a.base, a.other, all_ja=load_all_ja(),
                       base_segs=base, other_segs=other)

    # ---- 输出 ----
    outdir = os.path.join(DRAFT, a.rj, "asr_fix")
    os.makedirs(outdir, exist_ok=True)
    json.dump(cands, open(os.path.join(outdir, a.track + ".candidates.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    A1 = [c for c in cands if c["rule"].startswith("A1")]
    A2 = [c for c in cands if c["rule"].startswith("A2")]
    B = [c for c in cands if c["rule"].startswith("B")]
    print("A1 同读音不同字：%d 条 · A2 内容不同：%d 条 · B 同轨重复怪词：%d 条"
          % (len(A1), len(A2), len(B)))

    md = ["# ASR 改错复核单 · %s / %s" % (a.rj, a.track), "",
          "- 主版本：%s（成品对齐版）· 对照：%s" % (a.base, a.other),
          "- A 多版本不一致 %d 条 · B 同轨重复怪词 %d 条" % (len(A1) + len(A2), len(B)),
          "- **两版一致 ≠ 正确**：`ゾウ` 在 v1/v2 都出现，规则 A 抓不到，需 B + 整轨语义复核", ""]
    md.append("## A1 同读音不同字（汉字层误识，最高价值）")
    for c in A1[:40]:
        dif = " / ".join(x if isinstance(x, str) else "%s→%s" % x for x in c["diff"])
        md.append("- `[%.1f]` **%s**" % (c["t"], dif))
        md.append("  - base: %s" % c["base"][:60])
        md.append("  - other: %s" % c["other"][:60])
    md.append("")
    md.append("## A2 内容不同（分段/表述差异，需人工判）")
    for c in A2[:20]:
        md.append("- `[%.1f]` base: %s" % (c["t"], c["base"][:60]))
        md.append("  - other: %s" % c["other"][:60])
    md.append("")
    md.append("## B 同轨重复怪词（系统性误识候选）")
    for c in B[:40]:
        md.append("- **%s** ×%d%s%s" % (c["token"], c["n"],
                                          "（词典外）" if c["oov"] else "",
                                          "（全库 %d 次）" % c.get("corpus_hits", -1)))
        for o in c["occ"][:3]:
            md.append("  - `[%.1f]` %s" % (o["t"], o["text"][:56]))
    open(os.path.join(outdir, a.track + ".review.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")

    print("→ %s" % outdir)
    for c in (A1 + A2 + B)[:12]:
        if c["rule"].startswith("A1"):
            print("  A1 [%.1f] %s | %s" % (c["t"], c["base"][:28], c["other"][:28]))
        elif c["rule"].startswith("A2"):
            print("  A2 [%.1f] %s | %s" % (c["t"], c["base"][:28], c["other"][:28]))
        else:
            print("  B  %s ×%d  @%.1f" % (c["token"], c["n"], c["t"]))

    # ---- 落地修正（--apply-fix）----
    if a.apply_fix:
        src = json.load(open(os.path.join(base_dir, a.track + ".json"), encoding="utf-8"))
        segs = src.get("segments") or []
        applied, missed = [], []
        for spec in a.apply_fix:
            if "=" not in spec:
                missed.append((spec, "格式应为 秒=文本"))
                continue
            ts, new = spec.split("=", 1)
            try:
                t0 = float(ts)
            except ValueError:
                missed.append((spec, "秒不是数字"))
                continue
            best, bi = 9e9, None
            for i, s in enumerate(segs):
                d = abs(float(s["start"]) - t0)
                if d < best:
                    best, bi = d, i
            if bi is None or best > 1.5:
                missed.append((spec, "1.5s 内没有对应段"))
                continue
            old = segs[bi].get("text") or ""
            segs[bi]["text"] = new
            # 人工整句替换：词级时间戳对不上新文本了，直接丢掉让下游用 text
            asr_words.clear_words(segs[bi])
            segs[bi]["fix_log"] = {"old": old, "new": new, "rule": "人工", "evidence": a.why}
            applied.append({"t": round(float(segs[bi]["start"]), 2), "old": old, "new": new})

        outjson = os.path.join(outdir, a.track + ".json")
        src["segments"] = segs
        src["fix_meta"] = {"applied": len(applied), "base": a.base,
                           "time": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")}
        json.dump(src, open(outjson, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("\n已落地 %d 条 → %s" % (len(applied), outjson))
        for x in applied:
            print("  [%.1f] %s → %s" % (x["t"], x["old"][:34], x["new"][:34]))
        for m in missed:
            print("  ✗ 未落地 %s（%s）" % (m[0], m[1]))


if __name__ == "__main__":
    main()
