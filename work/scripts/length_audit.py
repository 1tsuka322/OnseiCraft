#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""长度审计：找「漏译」与「脑补」（2026-11-08，D9 后续）。

## 为什么需要

`lexical` 的 `content_drop` 只抓「整行只剩拟声」，抓不到两类真实缺陷：
- **漏译**：日文长句、中文只译了前半
  （`こうやってデカパイ押し付けておちんぽこきこきもしないの?` → 「就是这么把大奶子贴上来，」后半没了）
- **脑补**：日文很短、中文编出一长串
  （`んー?` → 「其实我想看看你的小鸡鸡抽动着喷出精液的样子…」）

GM9 的 100 条判定里这两类共 21 条（13 漏译 + 8 脑补），`lexical` 一个都没报。

## 判据

**漏译候选按多特征打分排序**（2026-11-10，GPT R2 Q5）：数字缺失 +3 / 中文未完结 +2 /
比例极端 +2 / 实体待核 +1 / 多分句未承接 +1。⚠️ 不匹配连接词（`て/から/けど`），
⚠️ 数字只认阿拉伯（`一緒`/`一番` 里的 一/二 是词不是数）。

**按 ASR 段聚合**（一个段拥有它时间范围内的所有显示行），再比字数：
- 漏译候选：`ja ≥ 8 字` 且 `zh/ja < 0.55`
- 脑补候选：`ja 全是拟声/气声`（A8 遮完没剩实义）且 `zh ≥ 6 字` 且含汉字

⚠️ **必须按段聚合，不能按单行**：一句被拆成多行时，短行对长段看着像漏译、
长行对短段看着像脑补。单行版实测几乎全是假阳性。

## 实测精度（2026-11-08，拿 GM9 的 21 条标注对撞）

| 类别 | 召回 | 说明 |
|---|---|---|
| 漏译 | **13/13** | 全中 |
| 脑补（纯拟声） | 4/8 | 漏掉的 4 条 ja 里**有实词**（`もっと成長するー`），不满足前提 |
| 脑补扩张 | +1（021） | 2026-11-10 加 Gemini GM11-6 的三条件 AND，多抓到 021 |

⚠️ **`脑补扩张` 是低置信度候选**：ASR 会把「あ」「姉上」「実は私」这种**半句**单独成段，
而显示行是整句——碎片段拿到整句中文，长度必然暴涨。已加两道门槛（段时长 ≥0.8s、
下一段不能以助词开头），但全库仍有 53 条，**大部分是这类碎片误报**。
定位真问题时优先看「漏译」和「脑补（纯拟声）」，`脑补扩张` 只当补充线索。

精确率约 **50%**（抽查漏译候选，一半是合理的拟声压缩或短句）。

⚠️⚠️ **这是候选生成器，不是门禁。** 不要接进 06，也不要拿它的条数直接判定改动好坏——
判定改动好坏请用 `scripts/ab03.py`（带 A/A 噪声地板）。

用法：
    python scripts/length_audit.py --rj RJ01654115
    python scripts/length_audit.py --json /tmp/len.json
"""
import argparse
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('KIKOERU_CUE_PIPELINE', '1')
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import mask_nonlexical as M  # noqa: E402

# 已登记术语表（GPT Q5 的「实体缺失」要用）。读不到就退化成空表，不影响其它判据。
try:
    import styleloc  # noqa: E402
    _g = json.load(open(styleloc.table('glossary.json'), encoding='utf-8'))
    GLOSSARY = {k: (v.get('zh') if isinstance(v, dict) else v) for k, v in (_g or {}).items()}
except Exception:
    GLOSSARY = {}

TS = re.compile(r'^\[(\d+):(\d+\.\d+)\](.*)$')
STRIP = re.compile(r'[\s、。…‥・!?！？~〜ー—\-「」♡♪()（）]')
HAN = re.compile(r'[\u4e00-\u9fff]')
RATIO_MAX = 0.55
JA_MIN = 8


def cnt(s):
    return len(STRIP.sub('', s or ''))


def latest(rj):
    ds = sorted(glob.glob(os.path.join(ROOT, 'runs', '*_%s_production' % rj)))
    return ds[-1] if ds else None


def load_lrc(p):
    out = []
    for ln in open(p, encoding='utf-8'):
        m = TS.match(ln.rstrip('\n'))
        if m:
            out.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3)))
    return out


def is_pure_ono(ja):
    masked, spans = M.mask_ja(ja, literal=True)
    if not spans:
        return False
    rest = re.sub(r'[、。…‥・\s!?！？~〜ー—\-「」]+', '', re.sub(r'〔[^〕]*〕', '', masked))
    return len(rest) < 2


# ---------------------------------------------------------------------------
# 脑补扩张（2026-11-10，Gemini GM11-6 的三条件 AND）
# ---------------------------------------------------------------------------
# 为什么单独一条：`is_pure_ono` 只抓「ja 全是拟声、中文却有实词」，
# 而 GM9 判的 8 条脑补里有 4 条 **ja 本身带实词**（`もっと成長するー`、`こんなの`），
# 不满足「纯拟声」前提，所以漏掉一半（实测 4/8）。
#
# Gemini 给的判据是三条**同时**成立：
#   ① 长度暴增：`len(ja) <= 8` 且 `len(zh) - len(ja) >= 10`
#   ② 性词无中生有：ja 里没有任何性器官/性行为词，zh 里却出现了
#   ③ 符号单向泄漏：ja 没有 `♡`，zh 有
#
# ⚠️ ② 必须严格：只要 ja 里**有**性词就放过（那说明中文写性词是合理的）。
EROTIC_ZH = ("高潮", "侵犯", "乳头", "肉棒", "小穴", "射精", "咕啾", "啾噗", "阴蒂", "阴唇", "龟头")
EROTIC_JA = re.compile(r'(イく|イク|イッ|射精|絶頂|ちん|まん|乳首|乳|犯|ぐちゅ|クリ|おっぱい|膣|亀頭|ペニス)')
HALLU_MAX_JA = 8
HALLU_MIN_JUMP = 10


def is_halluc_expanded(ja, zh):
    """短句脑补扩张。返回命中的条件列表（空 = 未命中）。"""
    # 用 cnt()（去空白/标点）而不是原始 len —— 聚合串带空格，原始 len 会漂（GPT R3 Q4）
    a, b = cnt(ja), cnt(zh)
    if not (a <= HALLU_MAX_JA and b - a >= HALLU_MIN_JUMP):
        return []
    why = ["长度暴增 %d→%d" % (a, b)]
    if any(k in zh for k in EROTIC_ZH) and not EROTIC_JA.search(ja or ""):
        why.append("性词无中生有")
    if "♡" in (zh or "") and "♡" not in (ja or ""):
        why.append("凭空爱心")
    # ①②③ 是 AND：光长度暴增不算（「嫌」→「不要这样嘛」天然膨胀）
    return why if len(why) >= 2 else []


# ---------------------------------------------------------------------------
# 漏译候选的多特征打分（2026-11-10，GPT R2 Q5）
# ---------------------------------------------------------------------------
# GPT 的要点：**不要找一个新硬阈值替代长度比**，而是「多特征排序 + 人工只看前 N 条」。
# 并明确「て/から/けど 没有中文字面连接词很正常，不能据此判漏」——所以不做连接词匹配。
#
# 强证据（权重高）：数字缺失、已登记实体缺失、中文停在未完成处
# 弱证据（权重低）：长度比极端、源句多分句、拟声占比高
DANGLING = ("的", "和", "跟", "与", "但", "但是", "而且", "因为", "所以", "如果", "虽然",
            "然后", "还有", "一边", "把", "被", "在", "对", "给", "从", "向")
# ⚠️ 只认**阿拉伯数字**：漢数字 一/二 在 `一緒`/`一番`/`二人` 里是词不是数，
# 拿它当「数字缺失」会天天误报（实测首版前 5 条有 4 条栽在这上面）。
DIGIT = re.compile(r'[0-9０-９]+')


def score_missing(ja, zh):
    """给「漏译」候选打分，返回 (分数, 证据列表)。分越高越可能是真漏。"""
    ev, sc = [], 0
    # ① 数字缺失（强）：日文有数字，中文一个都没有
    jd = set(DIGIT.findall(ja or ""))
    jd = {x for x in jd if len(x) >= 1}
    if jd:
        # 中文侧也要认**数词**：`2回` → 「两次」是正常译法，不能算缺失
        zd = set(DIGIT.findall(zh or "")) | set(re.findall(r'[零一二三四五六七八九十百千万两]', zh or ""))
        if not (zd and (jd & zd or re.search(r'[零一二三四五六七八九十百千万两]', zh or ""))):
            sc += 3
            ev.append("数字缺失 %s" % "/".join(sorted(jd)[:3]))
    # ② 已登记实体缺失（强）：glossary 里登记过的日文词出现在 ja，中文却没有对应译法
    try:
        hit_terms = [k for k in GLOSSARY if k and k in (ja or "")]
    except Exception:
        hit_terms = []
    # ⚠️ 只算「日文词 ≥2 字 且 中文译法 ≥2 字」的术语，且只给弱权重：
    # 中文完全可以用同义词（気持ち→感觉/心情），字面不在不等于漏译。
    miss = [k for k in hit_terms
            if len(k) >= 2 and len(GLOSSARY[k] or "") >= 2 and GLOSSARY[k] not in (zh or "")]
    if miss:
        sc += 1
        ev.append("实体待核 %s" % "/".join(miss[:3]))
    # ③ 中文停在未完成处（强）：结尾是连接词/介词 → 后半句很可能丢了
    tail = (zh or "").rstrip("…　 ")
    if any(tail.endswith(w) for w in DANGLING):
        sc += 2
        ev.append("中文未完结「%s」" % tail[-3:])
    # ④ 长度比极端（弱）
    # ⚠️ 2026-11-10（GPT R3 Q4）：必须用 `cnt()`（去空白/标点），不能用原始 `len`。
    # 显示行聚合改成空格拼接后，行数越多空格越多，原始 len 会虚增 → 阈值漂移。
    a, b = cnt(ja), cnt(zh)
    if a and b / a < 0.35:
        sc += 2
        ev.append("比例极端 %.2f" % (b / a))
    elif a and b / a < 0.55:
        sc += 1
    # ⑤ 源句多分句（弱）：日文有 ≥2 个顿号/逗号，中文只有一个
    if (ja or "").count("、") + (ja or "").count("，") >= 2 and \
            (zh or "").count("、") + (zh or "").count("，") == 0:
        sc += 1
        ev.append("多分句未承接")
    return sc, ev


def audit(rj):
    rd = latest(rj)
    if not rd:
        return []
    asr = {}
    for p in glob.glob(os.path.join(rd, '02_asr_fix', rj, 'asr_fix', '*.json')):
        d = json.load(open(p, encoding='utf-8'))
        asr[os.path.basename(p)[:-5]] = [(float(s.get('start') or 0), float(s.get('end') or 0),
                                          (s.get('text') or '').strip())
                                         for s in (d.get('segments') or [])]
    out = []
    for f in glob.glob(os.path.join(rd, '05_term_lock', rj, '*.lrc')):
        track = os.path.basename(f)[:-4]
        segs = asr.get(track) or []
        if not segs:
            continue
        # ⚠️ 按**显示行的时间窗**聚合日文，不是按段。
        # 行和段不是一对一：ASR 常把「姉上」「あ」这种片段单独成段，
        # 而显示行是整句。按段比长度，片段拿整句的中文必然像「脑补」
        # （实测 71 条脑补扩张里绝大多数是它），反过来长段配短行又像「漏译」。
        # 单元 = **ASR 段**：一个段聚合「中点落在它时间范围内的所有显示行」。
        # 为什么不是反过来（行 → 段）：一个段常被切成 2-3 个显示行，
        # 拿整段的日文去比单行的中文，两边都会误报（实测漏译从 2396 虚增到 5264）。
        # 为什么不是「有交集就算」：3 秒的长段横跨 3 行会被数三遍（虚增到 10042）。
        rows = [(t, zh) for t, zh in load_lrc(f) if zh.strip()]
        for si, (st, en, ja) in enumerate(segs):
            if not ja:
                continue
            zhs = [zh for t, zh in rows if st <= t < en]
            if not zhs:
                continue
            # ⚠️ 用空格拼，不能 `"".join`：显示行是分开的两句，直接拼会造出假词
            # （RJ1458584 实测：`…艾尔 M M` + `M 莉娜…` 拼成 `MM` → 触发 2 字母拉丁判据）
            zh_all = " ".join(zhs)
            a, b = cnt(ja), cnt(zh_all)
            kind, why, score = None, [], 0
            if a >= JA_MIN and b / a < RATIO_MAX:
                kind = '漏译'
                score, why = score_missing(ja, zh_all)
            elif is_pure_ono(ja) and b >= 6 and HAN.search(zh_all):
                kind = '脑补'
            else:
                # 碎片段（「あ」「姉上」这类 0.3 秒的独立段）会拿到整句的中文，
                # 拿它比长度必然像脑补。0.8 秒以下一律不算。
                # 下一段以助词/接续开头 → 当前段是半句，不是独立话语（「実は私」+「は…」）
                _nxt = (segs[si + 1][2] if si + 1 < len(segs) else "")
                _cont = bool(re.match(r'^[はがをにでとも の、,，]|^から|^けど|^けれど|^ので|^のに|^って', _nxt))
                if (en - st) >= 0.8 and not _cont:
                    why = is_halluc_expanded(ja, zh_all)
                    if why:
                        kind = '脑补扩张'
            if kind:
                out.append(dict(rj=rj, track=track, t=round(st, 2),
                                ja_chars=a, zh_chars=b,
                                ratio=round(b / a, 2) if a else 0,
                                kind=kind, why=why, score=score, ja=ja, zh=zh_all))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rj', default='', help='只查某一部')
    ap.add_argument('--json', default='', help='明细写到这个 json')
    ap.add_argument('--show', type=int, default=6, help='每类打印前 N 条')
    a = ap.parse_args()

    if a.rj:
        rjs = [a.rj]
    else:
        base = os.path.dirname(ROOT)
        rjs = sorted(os.path.basename(p) for p in glob.glob(os.path.join(base, 'kikoerusubtitle', 'RJ*'))
                     if os.path.isdir(p))

    rows = []
    for rj in rjs:
        rows += audit(rj)
    # 漏译按多特征得分降序（GPT Q5：让人工只看前 N 条）
    rows.sort(key=lambda r: (-(r.get('score') or 0), r['kind']))
    short = [r for r in rows if r['kind'] == '漏译']
    hallu = [r for r in rows if r['kind'] == '脑补']
    expand = [r for r in rows if r['kind'] == '脑补扩张']
    print('=== 长度审计（候选，不是错误清单）===')
    print('漏译 %d 条 / 脑补(纯拟声) %d 条 / 脑补扩张 %d 条' % (len(short), len(hallu), len(expand)))
    for title, sub in (('漏译（ja≥%d 且 zh/ja<%.2f）' % (JA_MIN, RATIO_MAX), short),
                       ('脑补（ja 纯拟声但中文有实词）', hallu),
                       ('脑补扩张（长度暴增 + 性词/爱心无中生有）', expand)):
        print('\n--- %s ---' % title)
        for r in sub[:a.show]:
            print('  %-11s %-22s %7.2f  %d→%d (%.2f) 分=%d' % (r['rj'], r['track'][:22], r['t'],
                                                               r['ja_chars'], r['zh_chars'], r['ratio'],
                                                               r.get('score') or 0))
            print('      ja %r' % r['ja'][:46])
            print('      zh %r' % r['zh'][:46])
            if r.get('why'):
                print('      判据 %s' % '；'.join(r['why']))
    if a.json:
        json.dump(rows, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('\n明细 → %s' % a.json)
    print('\n⚠️ 精确率约 50%：一半是合理的拟声压缩或短句。**不要接门禁**；'
          '判定改动好坏请用 scripts/ab03.py。')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
