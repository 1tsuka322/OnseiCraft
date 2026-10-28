# -*- coding: utf-8 -*-
r"""
术语挖掘 v4：从**句子级**平行语料挖术语 / 修复被切碎的键。

与 v2/v3 的区别（2026-10-20）：
  v2/v3 用「显示行」语料（ja 被切成 5.5s 片段）→ 1491 条里 1243 条是碎片
  （`孫→小穴`、`持→舒服`、`パイズ`、`マッ`/`ルフ`）。
  v4 用 `align_style.py --mode segment` 产出的**完整 ASR 段**语料，
  再用 janome 校验「ja 键是否是合法词」——碎片在分词后不是独立 token，一眼可辨。

两个产出：
  ① **修复**：现有术语表里 ja 不是合法词的条目 → 给出语料中的完整形态（`パイズ`→`パイズリ`）
  ② **新增**：zh 锚定（中文完整可信）+ 跨作品稳定（≥N 部作品里对应同一 ja 词）的候选

用法：
  python mine_terms.py [--lib <seg_lib目录>] [--min-works 3] [--min-hit 8]
"""
import os
import re
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json
import glob
import argparse
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
GLOSS = os.path.join(ROOT, "data", "style", "glossary_v3.json")
OUTDIR = os.path.join(ROOT, "data", "tmp", "terms_v4")

# zh 侧通用词：不作为术语候选（出现再多也不是「术语」）
STOP_ZH = set("""
我 你 他 她 它 我们 你们 他们 自己 这个 那个 这些 那些 这样 那样 什么 怎么 为什么
的 了 是 在 有 和 与 就 都 也 还 又 很 太 更 最 不 没 别 会 能 要 想 说 做 去 来 到
啊 呀 呢 吧 哦 嗯 哈 嘛 啦 么 着 过 得 地 被 把 给 让 对 从 向 跟 比 为 以 而 但 却
一个 一下 一点 一些 一样 一定 真的 就是 还是 但是 因为 所以 如果 虽然 然后 现在 时候
""".split())

KEEP_JA = re.compile(r"^[ぁ-んァ-ヶ一-鿿ー]+$")


def load_lib(lib_dir):
    """读 segment 库，返回 [{ja, zh, src}]。"""
    pairs = []
    for p in sorted(glob.glob(os.path.join(lib_dir, "seg_*.json"))):
        src = os.path.basename(p)[4:-5]
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for x in d.get("pairs") or []:
            ja, zh = (x.get("ja") or "").strip(), (x.get("zh") or "").strip()
            if ja and zh:
                pairs.append({"ja": ja, "zh": zh, "src": src,
                              "spk": x.get("spk"), "role": x.get("role")})
    return pairs


def ja_tokens(text, tok):
    """janome 分词，返回 token 列表（只保留实词/名词性成分）。"""
    out = []
    for t in tok.tokenize(text):
        pos = t.part_of_speech.split(",")
        out.append((t.surface, t.base_form, pos[0], pos[1] if len(pos) > 1 else ""))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", default=os.path.join(ROOT, "data", "tmp", "seg_lib"))
    ap.add_argument("--min-works", type=int, default=3)
    ap.add_argument("--min-hit", type=int, default=8)
    ap.add_argument("--apply", action="store_true",
                    help="把 ① 的修复结果写回 glossary_v3.json（默认只报告；"
                         "写前自动备份 glossary_v3.json.bak_mine4）")
    a = ap.parse_args()

    os.makedirs(OUTDIR, exist_ok=True)
    pairs = load_lib(a.lib)
    if not pairs:
        print("没有语料：%s（先跑 align_style.py --mode segment）" % a.lib)
        return
    works = len({p["src"] for p in pairs})
    print("语料 %d 句对 / %d 部作品" % (len(pairs), works))

    try:
        from janome.tokenizer import Tokenizer
        tok = Tokenizer()
    except Exception:
        print("需要 janome：pip install janome")
        return

    gloss = json.load(open(GLOSS, encoding="utf-8"))
    print("现有术语表 %d 条\n" % len(gloss))

    # ---------- ① 修复：ja 键不是合法词 ----------
    print("=" * 70)
    print("① 碎片修复：现有条目里 ja 不是独立 token 的")
    print("=" * 70)
    # 全库「ja0 作为独立 token 出现」次数（不限于 occ 子集）：
    # 只在 occ（zh 也匹配的句对）里看会把合法词误判——分词器对生僻词
    # 根本不产出 token（janome 的未知词切分把 `チンポ` 切成 チ+ンポ）。
    sa_all = collections.Counter()
    for p in pairs:
        seen = {s for s, _b, _p, _s2 in ja_tokens(p["ja"], tok)}
        for s in seen:
            sa_all[s] += 1

    fix = []
    for g in gloss:
        ja0, zh0 = g.get("ja", ""), g.get("zh", "")
        if not ja0 or not KEEP_JA.match(ja0):
            continue
        occ = [p for p in pairs if zh0 and zh0 in p["zh"] and ja0 in p["ja"]]
        if not occ:
            continue
        # 找包含 ja0 的「最长合法 token」
        cand = collections.Counter()
        cand_works = collections.defaultdict(set)
        standalone = 0
        for p in occ:
            for surface, base, pos, sub in ja_tokens(p["ja"], tok):
                if ja0 not in surface:
                    continue
                if surface == ja0:
                    standalone += 1
                elif len(surface) > len(ja0):
                    cand[surface] += 1
                    cand_works[surface].add(p["src"])
        if not cand:
            continue
        best, n = cand.most_common(1)[0]
        w = len(cand_works[best])
        # ⚠️ 判据必须严格：ja0 **从不独立出现**（standalone==0）才是碎片。
        #    只看「更长形态出现更多」会把合法词误判——实测 `気持ち`(独立 5 次) 被
        #    误并成 `気持ちいい`，而这两个词意思不同（记忆：别做同 zh 子串去重）。
        if standalone or sa_all.get(ja0, 0) > 0:
            continue
        # 「未被解释的出现」测试：ja0 出现在句子里、但更长形态 best 不在 → 该句中
        # ja0 是**独立词**，不能并入（实测 `チンポ` 多数出现不以 `チンポン` 形态存在，
        # 它是合法口语词；而 `ッチ`/`エッ` 的 95% 出现都在 `エッチ` 里）。
        tot_ja0 = sum(1 for p in pairs if ja0 in p["ja"])
        unexpl = sum(1 for p in pairs if ja0 in p["ja"] and best not in p["ja"])
        ratio = unexpl / max(1, tot_ja0)
        if ratio >= 0.05 or w < 2 or n < 3:
            print("  %-12s 保留（并入 %s 会有 %.0f%% 的出现解释不了 → 独立词）"
                  % (ja0, best, ratio * 100))
            continue
        dup = any(g2.get("ja") == best and g2.get("zh") == zh0 for g2 in gloss)
        fix.append({"ja_old": ja0, "ja_new": best, "zh": zh0,
                    "works": w, "hit": n, "standalone": standalone,
                    "unexplained": unexpl, "unexplained_ratio": round(ratio, 3),
                    "type": "drop_dup" if dup else "merge"})
    for f in sorted(fix, key=lambda x: -x["hit"]):
        print("  %-12s -> %-14s (%s)  %d 部/%d 次（独立 %d / 未解释 %d 条=%.0f%%）-> %s"
              % (f["ja_old"], f["ja_new"], f["zh"], f["works"], f["hit"],
                 f["standalone"], f["unexplained"], f["unexplained_ratio"] * 100,
                 f["type"]))
    print("共 %d 条待修\n" % len(fix))

    # ---------- ② 新增：zh 锚定 + 跨作品稳定 ----------
    print("=" * 70)
    print("② 新增候选：zh 词在 ≥%d 部作品里稳定对应同一 ja 词" % a.min_works)
    print("=" * 70)
    have = {(g.get("ja"), g.get("zh")) for g in gloss}
    have_zh = {g.get("zh") for g in gloss if g.get("zh")}

    # zh 候选：中文 2-4 字片段（从语料 zh 侧统计），排除停用词与已有术语
    # ⚠️ 不能用 n-gram：实测产出 `最喜`（最喜欢被切）、`己的`（自己的被切）。
    #    必须用分词器的完整词。
    import jieba
    jieba.setLogLevel(60)
    zh_cnt = collections.Counter()
    for p in pairs:
        for w in jieba.cut(p["zh"]):
            w = w.strip()
            if len(w) < 2 or w in STOP_ZH:
                continue
            if all("\u4e00" <= c <= "\u9fff" for c in w):
                zh_cnt[w] += 1
    cands = [z for z, c in zh_cnt.most_common(400) if c >= a.min_hit]
    print("zh 候选 %d 个（频次 ≥%d）" % (len(cands), a.min_hit))

    new = []
    for z in cands:
        occ = [p for p in pairs if z in p["zh"]]
        if len(occ) < a.min_hit:
            continue
        # ja 侧：统计「名词性 token」在含该 zh 的句对里出现多少部作品
        cnt = collections.Counter()
        wk = collections.defaultdict(set)
        for p in occ:
            seen = set()
            for surface, base, pos, sub in ja_tokens(p["ja"], tok):
                if pos not in ("名詞", "動詞", "形容詞"):
                    continue
                # 用**基本形**：`なっ`/`優しく`/`大きく` 都是活用形碎片，
                # 统一到 base_form 才能当术语键。
                key = base if len(base) >= 2 else surface
                if len(key) < 2 or not KEEP_JA.match(key):
                    continue
                if key in seen:
                    continue
                seen.add(key)
                cnt[key] += 1
                wk[key].add(p["src"])
        for ja, n in cnt.most_common(3):
            w = len(wk[ja])
            if w < a.min_works or n < a.min_hit:
                continue
            if (ja, z) in have:
                continue
            cond = n / len(occ)
            new.append({"ja": ja, "zh": z, "hit": n, "occ": len(occ),
                        "works": w, "cond": round(cond, 3)})

    # 去重：同一 ja 只留 cond 最高的一条
    best_by_ja = {}
    for x in new:
        k = x["ja"]
        if k not in best_by_ja or x["cond"] > best_by_ja[k]["cond"]:
            best_by_ja[k] = x
    new = sorted(best_by_ja.values(), key=lambda x: (-x["works"], -x["cond"]))
    for x in new[:40]:
        mark = " [已有 zh]" if x["zh"] in have_zh else ""
        print("  %-14s -> %-8s  %d 部/%d 次 cond=%.2f%s"
              % (x["ja"], x["zh"], x["works"], x["hit"], x["cond"], mark))
    print("共 %d 条新候选\n" % len(new))

    json.dump(fix, open(os.path.join(OUTDIR, "fix.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(new, open(os.path.join(OUTDIR, "new.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("已写 %s/{fix,new}.json" % OUTDIR)

    # ---------- ③ 写回术语表（--apply） ----------
    if not a.apply or not fix:
        if fix:
            print("\n（未写盘；加 --apply 把 %d 条修复写回 glossary_v3.json）" % len(fix))
        return
    import shutil
    import time
    bak = GLOSS + ".bak_mine4"
    if not os.path.exists(bak):
        shutil.copy2(GLOSS, bak)
    out, touched = [], set()
    for g in gloss:
        ja0 = g.get("ja", "")
        f = next((x for x in fix if x["ja_old"] == ja0), None)
        if not f:
            out.append(g)
            continue
        if f["type"] == "drop_dup":
            # 更长形态已是同 zh 的条目 → 删掉碎片条目（去重，不再新增）
            print("  删除碎片条目 %s（%s 已存在）" % (ja0, f["ja_new"]))
            touched.add(ja0)
            continue
        g = dict(g, ja=f["ja_new"],
                 note=(g.get("note", "") + " | mine4 修复 %s→%s"
                       % (ja0, f["ja_new"])).strip(" |"))
        print("  修复 %s → %s" % (ja0, f["ja_new"]))
        touched.add(ja0)
        out.append(g)
    if len(out) != len(gloss) or any(x.get("ja") in touched for x in out):
        json.dump(out, open(GLOSS, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("\n已写回 %s（%d → %d 条；备份 %s）"
              % (GLOSS, len(gloss), len(out), bak))
    else:
        print("\n无需改动")


if __name__ == "__main__":
    main()
