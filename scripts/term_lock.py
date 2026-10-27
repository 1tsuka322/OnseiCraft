# -*- coding: utf-8 -*-
r"""称谓/术语一致性锁定（确定性后处理，不靠 LLM）。

## 为什么需要它（2026-10-16 实测结论）

B1 实验暴露：同一部作品里同一个人物，3 秒内「先生」「老师」交替出现。

- ASR 原文 RJ9000016 Tr.03 含 27 段「先生」（全部指同一位家庭教师）。
- 四版对照（行内「先生/老师」计数）：
    初译 16/10 → B1强 8/18 → B1保 12/14 → 补术语后 10/16
  **每版都在摇摆**，只是概率略不同；单句层面同一人在同一场戏交替。
- 注入链路已核实**没有断**：`hard_rules.txt`（含称谓段）确实进 system prompt，
  `glossary.json` 的 `先生→老师` 也在 prompt 的【术语对照表】里。
- 真实根因：**「先生」在中文里是合法词**，LLM 面对「先生/老师」二选一时按局部
  随机性选，prompt 白名单**挡不住语义合法词**。

→ 结论：**一致性必须用确定性后处理兜底**。prompt 负责"大概率译对"，
  本脚本负责"全篇统一"。

## 做法

按 `LOCK_TERMS` 里的每一组「同一指称的多个中文变体」：
1. **数票**：统计全篇（或整轨/整作品）里各变体出现次数；
2. **选锚**：票数最多者为该组的标准写法（平票时按 `prefer` 排优先级）；
3. **覆写**：把其余变体替换为标准写法。

⚠️ **安全边界**（重要）：
- 只处理**当面称呼用法**，不碰「先生」作"丈夫/男性/老师傅"等其他义项。
  因此变体匹配要求「**变体后紧跟称呼语境**」或「**变体独立成句**」，
  避免把「那位先生」里的"先生"也改掉。
- 只在**同一作品内**锁定（跨作品允许不同，因为设定不同）。
- 默认 `--dry-run`，必须显式 `--apply` 才写盘。

用法：
  # 统计+预览（不写盘）
  python term_lock.py <LRC目录或文件> [--scope track|work]
  # 实际覆写
  python term_lock.py <LRC目录或文件> --apply
  # 只处理某几组
  python term_lock.py <目录> --only sensei,kyu
"""
import os
import re
import sys
import glob
import json
import argparse
import collections

sys.stdout.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------------------
# 锁定组定义
# ---------------------------------------------------------------------------
# 每组：key -> {variants, prefer, when, desc}
#   variants: 互为变体的中文写法（同一指称的不同译法）
#   prefer  : 平票时的优先级（越靠前越优先）
#   when    : 日文侧的触发条件（正则）。提供时，只有该行日文命中才覆写，
#             用来避免误伤其他义项。为 None 表示不做日文侧约束（谨慎使用）。
LOCK_TERMS = {
    "sensei": {
        # 变体池 = 同一日文「先生」可能被译成的所有中文写法。
        # 语境探测会把锚点换成 老师/医生/律师/议员（见 detect_sensei_context），
        # 所以这四个都必须在这里，才能在覆写时被"收敛到锚点"。
        "variants": ["老师", "先生", "医生", "医师", "大夫", "律师", "议员"],
        "prefer": ["老师", "先生"],
        "when": r"先生",
        "desc": "「先生」当面尊称：主译「老师」（语料 61.4%），按语境分化",
    },
    "kyu": {
        "variants": ["你", "君"],
        "prefer": ["你"],
        "when": r"[^\w]君|^君",
        "desc": "「君」当面称呼：主译「你」（语料 64.8%）",
    },
    "danna": {
        "variants": ["老公大人", "老公"],
        "prefer": ["老公", "老公大人"],
        "when": r"旦那様|旦那さん|旦那",
        "desc": "「旦那様」：译「老公」（不是「主人」），语料实测",
    },
    "goshujin": {
        "variants": ["主人大人", "主人", "老爷"],
        "prefer": ["主人"],
        "when": r"ご主人様|主人様",
        "desc": "「ご主人様」：译「主人」（主仆/BDSM 语境）",
    },
    "okyakusama": {
        "variants": ["客人您", "贵客", "客人"],
        "prefer": ["客人"],
        "when": r"お客様|客様|客さん",
        "desc": "「お客様」：译「客人」",
    },
    # ---- 身体部位/性术语语料级统一（2026-10-21 用户拍板，先例 おまんこ→小穴）----
    # 背景：成品曾出现 59 行 `ちんぽ→阴茎` 而术语表 strict 是「肉棒」——term_lock
    # 原来的「按作品数票选锚」在 ASR 只有文本、无锚可依时让各作品各自漂移。
    # 已用 unify_body_terms.py 全库统一（肉棒417/鸡鸡257/小穴359/胸部315/乳头101），
    # 这里把三个词族加成锁组，保证**整管重跑**也收敛到同一套译法
    # （锚仍按作品投票：ちんぽ 主导的作品投「肉棒」、ちんちん 主导的投「鸡鸡」，
    #  与用户对「旦那」的「作品级锁」口径一致）。
    # ⚠️ penis 组**合成一组**而不按ちんぽ/ちんちん拆两组：拆组会出现「肉棒↔鸡鸡」
    #    互相覆写的振荡（同一行窗口常同时含两个词），合成一组由作品级投票一次定锚。
    "penis": {
        "variants": ["肉棒", "鸡鸡", "阴茎", "鸡巴", "小鸡鸡"],
        "prefer": ["肉棒", "鸡鸡"],
        "when": r"ちんぽ|チンポ|おちんぽ|オチンポ|ちんこ|チンコ|おちんこ|オチンコ|チンパ"
                r"|ちんちん|チンチン|おちんちん|オチンチン",
        "desc": "阴茎词族：语料级统一（ちんぽ族→肉棒、ちんちん族→鸡鸡；用户 2026-10-21 拍板）",
    },
    "manko": {
        "variants": ["小穴", "阴道", "私处"],
        "prefer": ["小穴"],
        "when": r"まんこ|おまんこ|マンコ|オマンコ",
        "desc": "「まんこ」：统一「小穴」，不译「阴道」（用户两次点名，语料 215:97）",
    },
    "oppai": {
        "variants": ["胸部", "乳房"],
        "prefer": ["胸部"],
        "when": r"おっぱい|オッパイ|ぱいぱい|パイパイ|デカパイ",
        "desc": "「おっぱい」：统一「胸部」",
    },
}

# ---------------------------------------------------------------------------
# 禁用译法表（**无条件覆写**，与选锚解耦；用户 2026-10-25 拍板）
# ---------------------------------------------------------------------------
# 为什么不加进 LOCK_TERMS 的 variants：那组的锚是「票数最多者胜」。
# 把「欧派」塞进 oppai 组的 variants，一旦某部作品里欧派出 20 次、胸部 3 次，
# 锚就会变成「欧派」—— 反向失效。BAN 表不看票、不看语境，出现就换成标准译法。
#
# 现状（2026-10-25 全库实测）：成品 22415 行里「欧派」0 次 —— 这是**预防性**的洞：
# 语料 973 句「おっぱい」里 203 句译成「欧派」（集中在 6 部作品的译者个人用词，
# RJ405055 一部就 83/93），而 oppai 组的 variants 只有 [胸部, 乳房]，
# 模型若吐出「欧派」**没有任何机制纠正**（成品 0 次是靠 glossary 钉「胸部」+ 模型偏好）。
#
# ⚠️ 用户 2026-10-25 明确：**「奶子/奶」不禁**（原话"那就粗俗一点"）——
#    更粗的作品要留口子。所以这里只禁「欧派」（ACG/台湾圈译法）。
BAN_TERMS = {
    "欧派": "胸部",
}

# 「先生」作其他义项时**绝不能**动：丈夫 / 男性 / 对陌生男性的客气称呼
# 只要「先生」前面是这些词或量词，就跳过（这些是真正的"先生=男性"用法）
SENSEI_NON_VOCATIVE = re.compile(r"(那位|这位|一位|某位|老公|丈夫|祖|老公公)")

# ---------------------------------------------------------------------------
# 语境探测（用户 2026-10-16 明确要求：「sensei 的师生/医生语境」）
# ---------------------------------------------------------------------------
# 日语「先生」是对教师/医生/律师/议员的**通用当面尊称**，中文必须按语境分化。
# 判据 = 全篇共现词投票（不看单句，因为单句信息太少容易误判）。
SENSEI_CONTEXT = {
    "老师": r"生徒|授業|学校|教室|教え子|宿題|先生っぽ|教えて|先生業|補習|進路",
    "医生": r"病院|診察|患者|注射|カルテ|看護|症状|熱がある|入院|お薬|治療",
    "律师": r"弁護士|裁判|訴え|法律|法廷",
    "议员": r"議員|選挙|国会|演説",
}


def detect_sensei_context(all_ja):
    """全篇共现投票，判「先生」该译什么。

    返回 (锚点, 明细)。全篇没有语境线索时返回 ("老师", {})——老师是语料主流（61.4%）。
    """
    votes = collections.Counter()
    for t in all_ja:
        for zh, pat in SENSEI_CONTEXT.items():
            if re.search(pat, t):
                votes[zh] += 1
    if not votes:
        return "老师", {}
    # 语境票数需 ≥2 且有明显优势，否则回退到「老师」
    top, top_n = votes.most_common(1)[0]
    if top_n < 2:
        return "老师", dict(votes)
    return top, dict(votes)


def load_lrc(path):
    """返回 [(时间戳原串, 中文文本), ...]，保持顺序。"""
    out = []
    for ln in open(path, encoding="utf-8"):
        s = ln.rstrip("\n")
        m = re.match(r"^(\[[^\]]*\])(.*)$", s)
        if m:
            out.append([m.group(1), m.group(2)])
    return out


def save_lrc(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for ts, txt in rows:
            f.write(ts + txt + "\n")


def count_votes(texts, group, exclude=None):
    """按「当面称呼」口径数票，返回 Counter。

    exclude: 要排除的写法（属于**其它**组的锚点/变体）。
      ⚠️ 必要性：同一个中文写法可能属于多组。最典型的是「先生」——
      既可能是 sensei 组（应当译「老师」），也可能是"丈夫/男性"义。
      如果 danna 组的变体里含「先生」，它会把 sensei 组的 10 票也算走，
      于是两组得出互相冲突的锚点。**跨组必须互斥**。
    """
    exclude = exclude or set()
    cnt = collections.Counter()
    for t in texts:
        for v in group["variants"]:
            if v in exclude:
                continue
            cnt[v] += t.count(v)
    return cnt


def anchors_conflict(anchors):
    """检查各组锚点是否互斥（同一写法不能被两组同时选为锚）。"""
    seen = {}
    bad = []
    for k, a in anchors.items():
        if a in seen:
            bad.append((k, seen[a], a))
        seen[a] = k
    return bad


def pick_anchor(cnt, group):
    """选锚：票数最多；平票时按 prefer 顺序。"""
    if not cnt:
        return None
    mx = max(cnt.values())
    tied = [v for v, n in cnt.items() if n == mx]
    for p in group["prefer"]:
        if p in tied:
            return p
    return tied[0]


def rewrite_text(text, group, anchor, ja_text=None):
    """把 text 里该组的非锚变体改成锚变体。

    ja_text 提供时，若日文没命中 group["when"]，则不改（避免误伤其他义项）。
    """
    if ja_text is not None and group["when"] and not re.search(group["when"], ja_text):
        return text, 0
    n = 0
    new = text
    for v in group["variants"]:
        if v == anchor:
            continue
        # 「先生」是汉语合法词，做语义保护：前面是量词/指示词时跳过该处
        if v == "先生":
            parts = re.split(r"(先生)", new)
            buf = []
            for i, seg in enumerate(parts):
                if seg != "先生":
                    buf.append(seg)
                    continue
                prev = "".join(buf)[-6:]
                if SENSEI_NON_VOCATIVE.search(prev):
                    buf.append(seg)          # 保留（这是"那位先生"式的男性义）
                else:
                    buf.append(anchor)
                    n += 1
            new = "".join(buf)
        else:
            c = new.count(v)
            if c:
                new = new.replace(v, anchor)
                n += c
    return new, n


def load_ja_texts(asr_dir):
    """读 ASR json 目录下全部原文，供语境投票用。"""
    out = []
    if not asr_dir or not os.path.isdir(asr_dir):
        return out
    for f in glob.glob(os.path.join(asr_dir, "**", "*.json"), recursive=True):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for s in (d.get("segments") if isinstance(d, dict) else d) or []:
            t = s.get("text") if isinstance(s, dict) else None
            if t:
                out.append(t)
    return out


def load_asr_segments(path, with_spk=False):
    """读单个 ASR json -> [(start, end, text), ...]（with_spk=True 时 4 元组带 speaker）。

    speaker 来源：`run_all --diarize` 写的 `SPEAKER_xx`（C1）或 script_align 写的
    角色名（`みう`/`ねね`）——两者都当「说话人桶 ID」用。
    """
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return []
    segs = d.get("segments") if isinstance(d, dict) else d
    out = []
    for s in segs or []:
        if isinstance(s, dict) and s.get("text"):
            if with_spk:
                out.append((float(s.get("start", 0)), float(s.get("end", 0)),
                            s["text"], s.get("speaker") or s.get("spk") or ""))
            else:
                out.append((float(s.get("start", 0)), float(s.get("end", 0)), s["text"]))
    out.sort()
    return out


def spk_at(segs, sec):
    """该时刻的主导说话人（±2s 窗内按重叠时长取最大；没有带 spk 的段 → None）。

    ⚠️ 与 ja_at 同窗口口径：一个 cue 常跨 2-3 条 segment，
    按重叠时长加权比取首条稳。
    """
    if not segs:
        return None
    lo, hi = sec - 2.0, sec + 2.0
    best, best_ov = None, 0.0
    for st, en, _tx, spk in segs:
        if not spk:
            continue
        ov = max(0.0, min(en, hi) - max(st, lo))
        if ov > best_ov:
            best, best_ov = spk, ov
    return best


def bucket_anchors_for(rows_by_file, spk_rows, only, work_anchors):
    """C1-3：按说话人分桶数票选锚（2026-10-21，docs/archive/20261026/C1_SPEAKER_20261021.md）。

    为什么：term_lock 原来按**作品**投票选锚。双角色作品里两个角色对同一
    referent 的称谓可能不同（A 叫「老师」、B 叫「您」），作品级锚会强行统一。

    spk_rows: {file: [每行的说话人 or None]}（与 rows_by_file 的行一一对应）。

    规则：
      - 只有 ≥2 个说话人桶时才启用；单桶/无 spk 数据 → 返回 {}（行为与旧版一致）。
      - 每桶每组单独数票，桶内票数 ≥2 才给锚；与作品锚相同的桶**不记**
        （只覆盖真的分歧，减少无意义打印）。
      - 桶内票不够的行，回写时用作品级锚（调用方兜底）。
    返回 {group: {bucket: anchor}}。
    """
    buckets = sorted({s for v in spk_rows.values() for s in v if s})
    if len(buckets) < 2:
        return {}
    out = {}
    for key in only:
        if key not in work_anchors:
            continue
        g = LOCK_TERMS[key]
        excl = set()
        for other in only:
            if other != key:
                excl.update(LOCK_TERMS[other]["variants"])
        ba = {}
        for b in buckets:
            texts = [t for f, rows in rows_by_file.items()
                     for (ts, t), sp in zip(rows, spk_rows.get(f, []))
                     if sp == b]
            cnt = count_votes(texts, g, exclude=excl)
            if sum(cnt.values()) < 2:
                continue
            anc = pick_anchor(cnt, g)
            if anc != work_anchors[key]:
                ba[b] = anc
        if ba:
            out[key] = ba
    return out


LRC_TS = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def ts_to_sec(ts):
    """LRC 时间戳「[01:27.90]」-> 87.9 秒。"""
    m = LRC_TS.match(ts)
    if not m:
        return None
    return int(m.group(1)) * 60 + float(m.group(2))


def find_lrcs(target):
    if os.path.isfile(target):
        return [target]
    return sorted(glob.glob(os.path.join(target, "**", "*.lrc"), recursive=True))


# ---------------------------------------------------------------------------
# 允许集校验（2026-10-25，GPT 第七轮 Q1 落地）
# ---------------------------------------------------------------------------
# GPT 的判据：「term_lock 应优先做**允许集校验/最小替换**，不要把所有变体覆写成一个词」。
# 原因：单值锚会抹平语域差异（`ちんぽ` 族 肉棒/鸡鸡/鸡巴 本来就是受控变体）。
# 本函数只读不写：逐行看「日文命中某术语 → 中文有没有用允许译法 / 有没有用禁用译法」，
# 产出清单交给 fixline 或人工，而不是自动改。
TERMS_DB = os.path.join(os.environ.get("KIKOERU_STYLE",
                                       r"D://aiwork//kikoeru//.workbuddy//style"),
                        "terms_db.json")


def allow_set_check(files, cache, out_json=""):
    """逐行做允许集校验。**ASR 版本按轨自动选**（坑 90）：成品与 draft/asr(v2) 混存，
    16 部里 12 部其实对齐 asr_v1_20261020，直接读 asr/ 会把日文对错行。"""
    try:
        db = json.load(open(TERMS_DB, encoding="utf-8"))["rows"]
    except Exception as e:
        print("读不到 terms_db.json（先跑 tmp/daily/build_terms_db.py）：%s" % e)
        return
    try:
        import scan_violations as SV
    except Exception as e:
        print("无法复用 scan_violations.pick_asr（%s）→ 退回单版本模式" % e)
        SV = None
    terms = [r for r in db if r.get("allowed")]
    terms.sort(key=lambda r: -len(r["ja"]))      # 长词优先：おちんこ 先于 ちんこ
    findings, vers = [], collections.Counter()
    for f in files:
        rows = cache[f]
        rj = os.path.basename(os.path.dirname(f))
        track = os.path.basename(f)[:-4]          # 去掉 .lrc → xxx.mp3
        # ⚠️ pick_asr 要的是「秒数」行，term_lock 的 rows 是「时间戳字符串」→ 先转
        rows_sec = [(ts_to_sec(t), x) for t, x in rows if ts_to_sec(t) is not None]
        d, ver = (SV.pick_asr(rj, track, rows_sec) if SV else (None, None))
        if not d:
            print("  [skip] %s：找不到 ASR json（rj=%s）" % (track[:24], rj))
            continue
        vers[ver] += 1
        segs = d.get("segments") or []
        # ⚠️ 口径（坑 131/132）：LRC 是**显示行**，一个 cue 常被拆成 2–3 行，
        # 而日文只在 cue 层。按行比对会把「同 cue 的另一行用了允许译法」误判成 missing。
        # 所以：`banned` 仍按行判（行内有禁用词就是错），`missing` 改成**轨级**——
        # 该术语在本轨日文里出现 ≥2 次、但全轨中文一次都没用允许译法 → 才算没执行。
        ja_all = "".join(sg.get("text") or "" for sg in segs)
        zh_all = "\n".join(x for _t, x in rows if x.strip())
        for r in terms:
            n_ja = ja_all.count(r["ja"])
            if n_ja < 2:
                continue
            if not any(a in zh_all for a in r["allowed"]):
                findings.append(dict(file=os.path.basename(f), ts="-", ja=r["ja"],
                                     zh="", kind="never_used", n_ja=n_ja,
                                     hit=[], allowed=r["allowed"]))
        for ts, txt in rows:
            if not txt.strip():
                continue
            for r in terms:
                banned = [b for b in (r.get("banned") or []) if b in txt]
                if banned:
                    findings.append(dict(file=os.path.basename(f), ts=ts, ja=r["ja"],
                                         zh=txt.strip(), kind="banned",
                                         hit=banned, allowed=r["allowed"]))
    kb = collections.Counter(x["ja"] for x in findings if x["kind"] == "banned")
    kn = collections.Counter(x["ja"] for x in findings if x["kind"] == "never_used")
    print("\n=== 允许集校验：%d 条可疑行（ASR 版本：%s）===" %
          (len(findings), " ".join("%s×%d" % (k, v) for k, v in vers.items())))
    print("  禁用译法命中 %d 条（%s）" % (sum(kb.values()), "、".join(
        "%s×%d" % (k, v) for k, v in kb.most_common(10)) or "无"))
    print("  全轨从未用允许译法 %d 个词（%s）" % (len(kn), "、".join(
        "%s(%d次)" % (k, v) for k, v in kn.most_common(10)) or "无"))
    for x in findings[:12]:
        print("  [%s] %s %s  日文=%s  中文=%s" %
              (x["kind"], x["file"][:14], x["ts"], x["ja"], x["zh"][:34]))
    if out_json:
        os.makedirs(os.path.dirname(out_json), exist_ok=True)
        json.dump(dict(n=len(findings), asr_versions=dict(vers),
                       by_ja_banned=dict(kb), by_ja_never_used=dict(kn), rows=findings),
                  open(out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("  json →", out_json)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="LRC 目录或单个文件")
    ap.add_argument("--apply", action="store_true", help="实际写盘（默认只预览）")
    ap.add_argument("--only", default="", help="只处理指定组，逗号分隔，如 sensei,kyu")
    ap.add_argument("--ja-dir", "--asr-dir", dest="ja_dir", default="",
                    help="ASR json 目录：提供后可用共现词判「先生」该译老师/医生/律师，更准")
    ap.add_argument("--check", action="store_true",
                    help="只做「允许集校验」：日文命中术语但中文没用允许译法/用了禁用译法 → 只报不改")
    ap.add_argument("--json", default="", help="--check 时把明细写到这个 json")
    a = ap.parse_args()

    only = [x for x in a.only.split(",") if x] or list(LOCK_TERMS.keys())
    files = find_lrcs(a.target)
    if not files:
        print("没有找到 .lrc：%s" % a.target)
        return

    print("待处理 %d 个 LRC，锁定组：%s" % (len(files), ", ".join(only)))

    # ---- 第一遍：全作品数票（跨轨统一）----
    all_texts = []
    cache = {}
    for f in files:
        rows = load_lrc(f)
        cache[f] = rows
        all_texts.extend(t for _, t in rows)

    anchors = {}
    for key in only:
        g = LOCK_TERMS[key]
        # 排除属于**其它**组的写法，保证跨组互斥（见 count_votes 注释）
        excl = set()
        for other in only:
            if other == key:
                continue
            excl.update(LOCK_TERMS[other]["variants"])
        cnt = count_votes(all_texts, g, exclude=excl)
        total_v = sum(cnt.values())
        if total_v < 2:
            # 票数太少（0/1）没有统计意义，锚定它只会误改
            print("  [%s] 跳过：全篇只有 %d 票，无统计意义" % (key, total_v))
            continue
        anc = pick_anchor(cnt, g)
        anchors[key] = anc
        detail = " ".join("%s=%d" % (k, v) for k, v in cnt.most_common())
        print("  [%s] %s  → 锚定「%s」   (%s)" % (key, g["desc"], anc, detail))

    bad = anchors_conflict(anchors)
    if bad:
        print("\n⚠️ 锚点冲突（同一写法被多组选中），已放弃冲突组：")
        for k, other, a in bad:
            print("   %s 与 %s 都想锚「%s」→ 跳过 %s" % (k, other, a, k))
            anchors.pop(k, None)

    # ---- 「先生」语境覆写：师生 / 医生 / 律师 / 议员（用户明确要求）----
    if "sensei" in anchors and a.ja_dir:
        ja_texts = load_ja_texts(a.ja_dir)
        if ja_texts:
            ctx, votes = detect_sensei_context(ja_texts)
            if votes:
                print("  [sensei] 语境投票 %s → 改锚「%s」"
                      % (" ".join("%s=%d" % (k, v) for k, v in
                                  sorted(votes.items(), key=lambda x: -x[1])), ctx))
            if ctx != anchors["sensei"]:
                anchors["sensei"] = ctx

    if not anchors:
        # 2026-10-25：锚全空时不能直接 return —— 禁用译法表（BAN_TERMS）与选锚无关，
        # 一部只有「欧派」没有任何锁组词的作品也必须被扫到。
        print("没有可锚定的组（语料里没出现）—— 只跑禁用译法表")
        if not BAN_TERMS:
            return

    # ---- 逐行日文校验（可选但强烈建议）----
    # 用 ASR 时间戳把每行 LRC 对到原文；只有原文确实含触发词才改写。
    # 这是防止误伤的关键：医生语境下锚点是「医生」，但「老师」若出现在
    # 日文没有「先生」的行里（真·学校老师），就不该被改成「医生」。
    ja_by_file = {}
    spk_by_file = {}
    if a.ja_dir:
        for f in files:
            base = os.path.basename(f)
            stem = os.path.splitext(base)[0]          # 去掉 .lrc → xxx.mp3
            cands = glob.glob(os.path.join(a.ja_dir, "**", stem + ".json"), recursive=True)
            if not cands:
                cands = glob.glob(os.path.join(a.ja_dir, "**", stem + "*.json"), recursive=True)
            if cands:
                segs4 = load_asr_segments(cands[0], with_spk=True)
                ja_by_file[f] = [(st, en, tx) for st, en, tx, _sp in segs4]
                # C1-3：spk 索引（空串段已由 load 过滤成 ""，这里只留有 spk 的）
                spk_by_file[f] = [(st, en, _tx, sp) for st, en, _tx, sp in segs4 if sp]
        if ja_by_file:
            print("  日文侧校验：%d/%d 个 LRC 对到了 ASR json" % (len(ja_by_file), len(files)))

    def ja_at(f, sec):
        """取该文件在 sec 时刻附近的原文。

        ⚠️ 必须聚合**时间窗内多条 segment**，不能只返回"命中那一条"：
        一个 cue 常跨 2-3 条 ASR segment，只取首条会漏掉「先生」出现在
        后半段的情况 → 白名单式漏改（实测改写数 9→4 就是这个原因）。
        """
        segs = ja_by_file.get(f)
        if not segs:
            return None
        hit = [tx for st, en, tx in segs if st - 2.0 <= sec <= en + 2.0]
        return "".join(hit) if hit else ""

    # ---- C1-3：按说话人分桶选锚（2026-10-21，双角色各自统一不互覆）----
    # 数据流：ASR segments[].speaker（diarize 的 SPEAKER_xx 或台本角色名）
    # → 每行 LRC 的主导 spk（spk_at，±2s 窗按重叠时长）→ 桶内数票选锚。
    # 单桶/无 spk → bucket_anchors_for 返回 {}，第二遍行为与旧版完全一致。
    row_spk = {}
    bucket_anchors = {}
    if spk_by_file:
        for f in files:
            row_spk[f] = [spk_at(spk_by_file.get(f), ts_to_sec(ts))
                          if ts_to_sec(ts) is not None else None
                          for ts, _t in cache[f]]
        bucket_anchors = bucket_anchors_for(cache, row_spk, only, anchors)
        for key, ba in bucket_anchors.items():
            print("  [%s] 说话人分桶锚：%s（作品锚「%s」不覆盖这些桶）"
                  % (key, " / ".join("%s→%s" % (b, a) for b, a in sorted(ba.items())),
                     anchors[key]))

    # ---- 允许集校验（只读，GPT 第七轮 Q1）----
    if a.check:
        allow_set_check(files, cache, a.json)
        return

    # ---- 第二遍：逐文件覆写 ----
    _no_ban = os.environ.get("KIKOERU_NO_BAN", "") in ("1", "true", "True")
    total = 0
    ban_total = 0
    for f in files:
        rows = cache[f]
        changed = 0
        spk_row = row_spk.get(f) or [None] * len(rows)
        for i, (ts, txt) in enumerate(rows):
            cur = txt
            sec = ts_to_sec(ts)
            ja_line = ja_at(f, sec) if sec is not None else None
            sp = spk_row[i] if i < len(spk_row) else None
            for key in only:
                if key not in anchors:
                    continue
                g = LOCK_TERMS[key]
                # C1-3：本行说话人桶有anchor 且与作品锚不同 → 用桶锚（不互覆）
                anc_row = bucket_anchors.get(key, {}).get(sp) or anchors[key]
                # ja_line 为 None（没对到 json）→ 不传校验，按原逻辑改；
                # 对到了 → 传 ja_line，只有日文命中 when 才改。
                cur, n = rewrite_text(cur, g, anc_row,
                                      ja_text=ja_line if ja_line is not None else None)
                changed += n
            # ---- 禁用译法：无条件覆写（不看票、不看日文语境）----
            # 这些词是**任何语境下都不该出现**的译法（见 BAN_TERMS 注释）。
            if not _no_ban:
                for _bad, _good in BAN_TERMS.items():
                    if _bad in cur:
                        _n = cur.count(_bad)
                        cur = cur.replace(_bad, _good)
                        changed += _n
                        ban_total += _n
                        print("    [ban] %s → %s（%s）"
                              % (_bad, _good, os.path.basename(f)))
            if cur != txt:
                rows[i][1] = cur
        if changed:
            total += changed
            print("  %s：改写 %d 处" % (os.path.basename(f), changed))
            if a.apply:
                save_lrc(f, rows)

    print("\n合计改写 %d 处。" % total)
    if ban_total:
        print("其中禁用译法（BAN_TERMS）%d 处。" % ban_total)
    if not a.apply:
        print("（预览模式，未写盘。确认无误后加 --apply）")
    else:
        print("已写盘。")


if __name__ == "__main__":
    main()
