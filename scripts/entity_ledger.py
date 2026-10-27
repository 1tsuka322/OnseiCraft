# -*- coding: utf-8 -*-
r"""作品级实体账本（entity ledger）——称谓/术语的"谁是谁"事实层。

## 为什么要这一层（2026-10-16，GPT 建议后落地）

*词级规则*（`先生 → 老师`）解决不了 *实体级事实*（「这 27 次先生都指同一位
家庭教师」）。分块 7B 不会可靠地自行维护后者，而且**中文全篇票选**这个口径
本身有缺陷：「中文里恰好有 25 个老师」也可能是错位/漏译/误改别人。

正确口径是 **源语驱动**：先在 ASR 日文里定位 mention（「先生」出现在哪 27 处），
把每处归到某个 **entity**（同一位人），再要求中文在这些位置落在**同一称谓族**。

## 账本长什么样

```json
{
  "work": "RJ9000016",
  "entities": {
    "sensei": {
      "ja_aliases": ["先生"],
      "zh_family": ["老师", "老师您", "教师"],
      "source_hint": "标题「家教」+ tag「学生」+ 轨名「壞老師」",
      "confidence": "high"
    }
  }
}
```

## 账本从哪来（按可靠性排序）

1. **meta.md 的日文标题 + Tag**（最强）：
   RJ9000016 标题「學生JK七草姐妹與**家教**失職背德H」+ tag「学生」
   → 先生 = 家庭教师 → 译「老师」。
   RJ9000014 标题「痴女**警官**」→ 若出现「先生」则可能是警察上司/医生，需小心。
2. **音轨名**（强，且 100% 存在）：RJ9000016 轨名「對學生發情的壞**老師**♪」
   「看媽媽看呆了的壞**老師**需要懲罰♪」→ 直接给出「老师」。
3. **ASR 共现词投票**（中，兜底）：`生徒/授業/学校` → 老师；`病院/診察` → 医生。

⚠️ 前两层是**作品级**（一次判全篇），第三层是**统计级**（可能误判）。

## 多族框架（2026-10-16 扩展）

原先只做了 `sensei`。现在改成注册表 `ENTITY_SPECS`，每族声明：
- `ja_aliases`：源语触发词（正则）
- `zh_family`：中文称谓族（第一个是默认锚点）
- `anchor_from_work`：是否用作品级语境（标题/Tag）动态选锚点（仅 sensei 需要）
- `dormant_ok`：源语无 mention 时是否静默（不注入）

新增族：`onii`（お兄さん）、`onee`（お姉さん）、`senpai`（先輩）、
`goshujin`（ご主人様）、`okyakusama`（お客様）、`danna`（旦那様）、
`mama`/`papa`（ママ/パパ）、`kyu`（君）。

⚠️ 这些族**大多不需要作品级语境**（`お兄さん → 哥哥` 语料 0.82 已足够稳），
所以默认锚点直接取 `zh_family[0]`，只在出现强竞争时降 confidence。
"""
import os
import re
import sys
import glob
import json

sys.stdout.reconfigure(encoding="utf-8")


# ===========================================================================
# ① 实体族注册表
# ===========================================================================
# 每族：
#   ja_aliases    源语触发词（正则串，多个用 |）
#   zh_family     中文称谓族，[0] = 默认锚点
#   anchor_from_work  是否用「标题+Tag+轨名」动态选锚（只有 sensei 需要）
#   desc          说明
ENTITY_SPECS = {
    # ---- 需要作品级语境分化的（沿用原 sensei 逻辑）----
    "sensei": {
        "ja_aliases": r"先生",
        "zh_family": ["老师", "老师您", "教师"],
        # ⚠️ to_converge = 「必须收敛到锚点」的对立写法（**不含**合法礼貌形式）。
        #   「老师您」是合法礼貌说法（不收敛）；「先生/医师/大夫」才是要收敛的。
        "to_converge": ["先生", "医师", "大夫", "律师", "议员"],
        "anchor_from_work": True,
        "desc": "「先生」当面尊称：按语境分化 老师/医生/律师/警官",
    },
    # ---- 语料里条件概率已很高，不需要语境（见 GLOSSARY_V2.md）----
    "onii": {
        "ja_aliases": r"お?兄(さん|ちゃん|ち|さ)",
        "zh_family": ["哥哥", "老哥"],
        "to_converge": ["老哥", "大哥", "兄长"],
        "anchor_from_work": False,
        "desc": "「お兄さん/お兄ちゃん」→ 哥哥（语料 0.82–0.91，跨 17–19 作品）",
    },
    "onee": {
        "ja_aliases": r"お?姉(さん|ちゃん|ち|さ)",
        "zh_family": ["姐姐", "大姐姐"],
        "to_converge": ["大姐姐", "大姐", "姊姊"],
        "anchor_from_work": False,
        "desc": "「お姉さん/お姉ちゃん」→ 姐姐（语料 0.84–0.85）",
    },
    "senpai": {
        "ja_aliases": r"先輩",
        "zh_family": ["前辈", "学长", "学姐"],
        "to_converge": ["学长", "学姐"],
        "anchor_from_work": False,
        "desc": "「先輩」→ 前辈（语料 0.70，跨 18 作品）",
    },
    "goshujin": {
        "ja_aliases": r"ご?主人様?",
        "zh_family": ["主人", "主人大人"],
        "to_converge": ["主人大人", "老爷", "少爷"],
        "anchor_from_work": False,
        "desc": "「ご主人様」→ 主人（主仆/BDSM 语境，语料 0.87–0.91）",
    },
    "okyakusama": {
        "ja_aliases": r"お?客様?|客さん",
        "zh_family": ["客人", "客人您", "贵客"],
        "to_converge": ["贵客", "顾客"],
        "anchor_from_work": False,
        "desc": "「お客様」→ 客人（语料 0.78–0.79）",
    },
    "danna": {
        "ja_aliases": r"旦那",
        "zh_family": ["老公", "老公大人"],
        "to_converge": ["老公大人", "老爷"],
        "anchor_from_work": False,
        "desc": "「旦那様」→ 老公（**不是**主人），语料实测",
    },
    "mama": {
        "ja_aliases": r"ママ|お母さん|母親",
        "zh_family": ["妈妈", "母亲"],
        "to_converge": ["母亲", "妈咪"],
        "anchor_from_work": False,
        "desc": "「ママ/お母さん」→ 妈妈（语料 0.82）",
    },
    "papa": {
        "ja_aliases": r"パパ|お父さん|父親",
        "zh_family": ["爸爸", "父亲"],
        "to_converge": ["父亲", "爹地"],
        "anchor_from_work": False,
        "desc": "「パパ/お父さん」→ 爸爸",
    },
    "kyu": {
        "ja_aliases": r"(?<![ぁ-んァ-ヶ])君(?![ぁ-んァ-ヶ])",
        "zh_family": ["你"],
        "to_converge": [],
        "anchor_from_work": False,
        "desc": "「君」当面称呼 → 你（语料 64.8%）",
    },
}

# 「先生」语境判据（作用在「标题 + tag + 轨名」这一整块文本上）
# ⚠️ 判据要点（2026-10-16 收紧，之前实测误判过 RJ9000007）：
#   光有 tag「学生」**不足以**推出师生关系——「学生」只是角色身份标签
#   （RJ9000007 是「不良JK恋爱」，tag 有「学生」但没有师生关系）。
#   必须出现**关系词**：家教/家庭教師/担任/教え子/補習/授業/先生 等。
WORK_CONTEXT = {
    "老师": (r"家教|家庭教師|教師|担任|教え子|補習|授業|学校|學校|学園|先生|"
             r"生徒|老師|老师",
             r"学生|學生|JK|JC|制服|同級生|後輩|先輩"),
    "医生": (r"医者|医師|歯医者|看護師|ナース|医院|診察室",
             r"病院|診察|患者|注射|入院|お薬|治療"),
    "律师": (r"弁護士|法律事務所|法廷|裁判",
             r"裁判|訴え|法律"),
    "警察": (r"警官|警察官|刑事|ポリス|巡査|痴女警官",
             r"警察|職務質問|取調べ"),
    "议员": (r"議員|国会議員|知事|大臣",
             r"選挙|国会|演説"),
}

# 明确的否定线索：标题/tag 里出现这些，说明**不是**该语境
WORK_CONTEXT_NEG = {
    "老师": r"医者|病院|警官|警察|痴女警官",
}

# 日文「先生」在 警察 语境下不译「警察」（那是对人称呼，不是职业名），
# 而是「警官」或保留。
SENSEI_FAMILY_BY_CTX = {
    "老师": ["老师", "老师您", "教师"],
    "医生": ["医生", "医师", "大夫"],
    "律师": ["律师"],
    "议员": ["议员"],
    "警察": ["警官", "警察"],
}


# ===========================================================================
# ② meta.md 解析
# ===========================================================================
def parse_meta(meta_path):
    """从 meta.md 抽 (日文标题, tag列表)。找不到返回 (None, [])。"""
    if not meta_path or not os.path.exists(meta_path):
        return None, []
    txt = open(meta_path, encoding="utf-8").read()
    title = ""
    m = re.search(r"\*\*(.+?)\*\*", txt)
    if m:
        title = m.group(1).strip()
    tags = []
    mt = re.search(r"##\s*Tag\s*\n+([^\n#]+)", txt)
    if mt:
        tags = [t.strip() for t in re.split(r"[、,，/]", mt.group(1)) if t.strip()]
    return title, tags


# ===========================================================================
# ③ 作品级语境判据（仅 sensei 用）
# ===========================================================================
def infer_sensei_family(work_text):
    """从「标题 + tag + 轨名」整块文本推「先生」该归哪个称谓族。

    返回 (族锚点, 命中的线索, 是否明确)。
    明确 = 有**强关系词**命中（弱身份词单独计不足以定性）。
    """
    hits = {}
    strong = {}
    for zh, (pat_s, pat_w) in WORK_CONTEXT.items():
        ms = re.findall(pat_s, work_text)
        if not ms:
            continue
        # 明确否定：出现医生的强线索时，「老师」的泛线索不算
        if zh == "老师" and "老师" not in work_text and "老師" not in work_text:
            neg = WORK_CONTEXT_NEG.get(zh)
            if neg and re.search(neg, work_text):
                continue
        strong[zh] = len(ms)
        hits[zh] = len(ms) * 3      # 强词 ×3
        # 弱身份词只做加成，不单独定性
        weak = len(re.findall(WORK_CONTEXT[zh][1], work_text))
        hits[zh] += weak
    if not strong:
        return None, {}, False
    ranked = sorted(hits.items(), key=lambda x: -x[1])
    top, top_w = ranked[0]
    clear = len(strong) == 1 or (len(ranked) > 1 and top_w >= ranked[1][1] * 1.5)
    return top, dict(ranked), clear


# ===========================================================================
# ④ 源语 mention 定位（泛化：任意 alias 正则）
# ===========================================================================
JA_DET = re.compile(r"この|その|あの|どの|例の")


def locate_mentions(asr_dir, alias_re):
    """扫 ASR json，定位某称谓族的每一处 mention。

    alias_re: 已编译的正则（源语触发词）。
    返回 [(track, seg_idx, start, text, has_determiner), ...]。
    has_determiner = 源语该处前紧邻この/その/あの等指示词
    （决定中文该不该带「这位/那位」）。
    """
    out = []
    if not asr_dir or not os.path.isdir(asr_dir):
        return out
    for f in sorted(glob.glob(os.path.join(asr_dir, "**", "*.json"), recursive=True)):
        track = os.path.basename(f)
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        segs = d.get("segments") if isinstance(d, dict) else d
        for i, s in enumerate(segs or []):
            if not isinstance(s, dict):
                continue
            t = s.get("text") or ""
            for m in alias_re.finditer(t):
                pre = t[max(0, m.start() - 3):m.start()]
                has_det = bool(JA_DET.search(pre))
                out.append((track, i, float(s.get("start", 0)), t, has_det))
    return out


# ===========================================================================
# ⑤ 组装账本
# ===========================================================================
def build_ledger(rj, meta_path, asr_dir):
    """构建一部作品的实体账本（遍历所有实体族）。

    返回 dict：
      {"work": rj, "title": ..., "tags": [...], "track_names": [...],
       "entities": {...}, "evidence": {...}, "mentions": {...}}
    """
    title, tags = parse_meta(meta_path)
    tracks = []
    if asr_dir and os.path.isdir(asr_dir):
        for f in sorted(glob.glob(os.path.join(asr_dir, "*.json"))):
            base = os.path.basename(f)
            base = re.sub(r"\.json$", "", base)
            base = re.sub(r"\.(mp3|wav|flac|m4a)$", "", base, flags=re.I)
            tracks.append(base)
    work_text = " ".join([title or ""] + tags + tracks)

    entities = {}
    evidence = {}
    mentions = {}

    for key, spec in ENTITY_SPECS.items():
        alias_re = re.compile(spec["ja_aliases"])
        ms = locate_mentions(asr_dir, alias_re)
        # ---- mention 门禁（2026-10-16）----
        # 源语全篇没有该称谓时，这个锁是**空转**的：既无收益，也说明
        # 作品级线索（tag 里的「学生」）未必指向该关系。标为 dormant，
        # 不注入 prompt、不参与复核，避免制造「看起来有依据」的假约束。
        active = len(ms) > 0
        if not active and not spec.get("anchor_from_work"):
            # 不需要语境的族：无 mention 直接跳过（不占账本）
            continue

        zh_family = list(spec["zh_family"])
        ctx = ""
        hits = {}
        clear = True
        if spec.get("anchor_from_work"):
            ctx, hits, clear = infer_sensei_family(work_text)
            if not ctx:
                # 作品级无线索：用默认锚点，但置信降级（除非源语 mention 很稳）
                if not active:
                    continue
                ctx = zh_family[0]
                clear = False
            zh_family = SENSEI_FAMILY_BY_CTX.get(ctx, zh_family)

        conf = "high" if (clear and active) else ("medium" if active else "dormant")
        entities[key] = {
            "ja_aliases": [spec["ja_aliases"]],
            "ja_alias_text": spec["ja_aliases"],
            "zh_family": zh_family,
            "to_converge": list(spec.get("to_converge") or []),
            "context": ctx,
            "confidence": conf,
            "n_mentions": len(ms),
            "desc": spec["desc"],
        }
        evidence[key] = {
            "ctx": ctx, "hits": hits, "clear": clear, "active": active,
            "axis": "title/tag/trackname" if spec.get("anchor_from_work") else "corpus",
        }
        mentions[key] = [
            {"track": tk, "seg": si, "start": st, "has_determiner": hd}
            for tk, si, st, _t, hd in ms
        ]

    return {
        "work": rj,
        "title": title,
        "tags": tags,
        "track_names": tracks,
        "entities": entities,
        "evidence": evidence,
        "mentions": mentions,
    }


def find_meta_for(rj, roots=None):
    """在若干候选根下找 <rj>/meta.md。"""
    roots = roots or ["kikoerusubtitle"]
    for r in roots:
        p = os.path.join(r, rj, "meta.md")
        if os.path.exists(p):
            return p
    return None


def ledger_for_draft(draft_dir, work_root="kikoerusubtitle"):
    """从 <work>/draft_xxx 目录推作品号并建账本。"""
    draft_dir = os.path.abspath(draft_dir)
    rj = os.path.basename(os.path.dirname(draft_dir))
    asr_dir = os.path.join(os.path.dirname(draft_dir), "asr")
    meta = find_meta_for(rj, [work_root])
    return build_ledger(rj, meta, asr_dir)


# ===========================================================================
# ⑥ 块级命中（供 polish 注入）
# ===========================================================================
def block_hits(ledger, ja_list):
    """本块 ja_list 命中了账本里哪些实体锁（GPT Q2-③：只注入本块命中的）。

    返回 [(ja_alias, zh_canonical, context, zh_family), ...]，没有命中返回 []。
    dormant（源语无 mention）的实体**不注入**——空转的约束只会制造噪声。
    """
    blob = "\n".join(ja_list)
    res = []
    for k, v in (ledger.get("entities") or {}).items():
        if v.get("confidence") == "dormant":
            continue
        try:
            alias_re = re.compile(v.get("ja_alias_text") or "")
        except re.error:
            continue
        m = alias_re.search(blob)
        if m:
            fam = v.get("zh_family") or []
            canon = fam[0] if fam else ""
            res.append((m.group(0), canon, v.get("context", ""), fam))
    return res


def format_block_hits(hits):
    """把 block_hits 结果渲染成 prompt 片段（无命中返回 ""）。"""
    if not hits:
        return ""
    lines = ["【本批出现的称谓·必须按此统一（作品级事实，来自作品名/Tag/轨名）】"]
    for alias, canon, ctx, fam in hits:
        alt = "／".join(fam[1:]) if len(fam) > 1 else ""
        extra = "（同族写法：%s，也只能用这些）" % alt if alt else ""
        lines.append("  %s → 「%s」%s" % (alias, canon, extra))
        lines.append("     ⚠️ 全篇同一人物只用「%s」这一种，不许与「%s」交替。"
                     % (canon, alt if alt else alias))
    return "\n".join(lines) + "\n"


# ===========================================================================
# ⑦ CLI
# ===========================================================================
def main():
    import argparse
    ap = argparse.ArgumentParser(description="构建/预览作品级实体账本")
    ap.add_argument("rj", help="作品号，如 RJ9000016")
    ap.add_argument("--meta-root", default="kikoerusubtitle")
    ap.add_argument("--asr-dir", default="",
                    help="ASR json 目录（默认 data/draft/<rj>/asr）")
    ap.add_argument("--dump", default="", help="写出到该 json 路径")
    a = ap.parse_args()

    asr = a.asr_dir or os.path.join("data/draft", a.rj, "asr")
    meta = find_meta_for(a.rj, [a.meta_root])
    led = build_ledger(a.rj, meta, asr)

    print("作品 %s" % led["work"])
    print("  日文标题: %s" % (led["title"] or "（无 meta.md）"))
    print("  Tag: %s" % ("、".join(led["tags"]) or "（无）"))
    print("  轨名 %d 条，示例: %s" % (len(led["track_names"]),
                                     " | ".join(led["track_names"][:3])))
    print()
    if not led["entities"]:
        print("  ⚠️ 未能推断任何实体（线索不足）")
    for k, v in led["entities"].items():
        nm = v.get("n_mentions", 0)
        tag = "  ⚠️ 空转（不注入）" if v.get("confidence") == "dormant" else ""
        print("  [%-10s] %s → 称谓族 %s（语境=%s, 置信=%s, mention=%d）%s"
              % (k, v["ja_alias_text"], v["zh_family"], v["context"],
                 v["confidence"], nm, tag))
    if a.dump:
        json.dump(led, open(a.dump, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("\n已写出 -> %s" % a.dump)


if __name__ == "__main__":
    main()
