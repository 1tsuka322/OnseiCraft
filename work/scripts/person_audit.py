# -*- coding: utf-8 -*-
"""人称风险审计：原文没有第三人称实体，译文却出现「她 / 他」。

## 为什么需要（2026-11-06）

RJ1058739 试用反馈里 37 处问题有 16 处是「人称漂移」——女主明明在跟男主说话，
译文却补成第三人称「她」。试过 5 条自动化路线（PERSON_LOCK / STYLE_RULES /
prompt 冲突修正 / 块边界对齐 / 事实层注入）**全部无效**，只有 CHUNK 3→4 把
22 行降到 15 行。既然修不掉，至少要**让用户知道哪几行要人工看**。

## 为什么只报不拦（hard=False）

实测 RJ1058739：抓 20 行、误报 3 行（15%）。误报来自 GPT 列的四类反例：
  ① 「其他」里的「他」不是代词
  ② 原文是复数他者（`ハンの子たち`），用「她」合法
  ③ 引用（`って言うから`）—— 说的是别人
  ④ 称呼听者（`先生、優しいね`）—— 既可能叫听者，也可能谈论第三人
日文还常常不标性别，「她/他」本来就可能是对的选择。

所以定位是**抽样清单**，不是判据。门禁里 hard=False。

用法：
    python person_audit.py <成品目录> <asr目录> [--json out.json] [--quiet]
"""
import argparse
import glob
import json
import os
import re
import sys

# 第三人称 / 他者实体线索（出现即认为「原文允许用第三人称」）
ENTITY = [
    r"あの子", r"あの人", r"あいつ", r"彼女", r"彼氏", r"彼(?!ら)",
    r"先生", r"せんせい", r"友達", r"友だち", r"同級生", r"クラスメイト",
    r"妹", r"姉", r"兄", r"弟", r"お母さん", r"お父さん", r"母さん", r"父さん",
    r"みんな", r"みなさん", r"クラス", r"班", r"グループ", r"部活", r"担任",
    # 复数标记：只认「人」类名词 + たち/ら。
    # ⚠️ 不能用「任意假名 + ら」——`もらう`（貰う）会被当成复数，实测误放过 2 行。
    r"(子|人|彼女?|奴|君|僕|俺|私|あんた|お前|みんな|連中|野郎)たち",
    r"(彼|奴|君|僕|俺|あいつ|こいつ|そいつ)ら",
    r"[一-龥ぁ-んァ-ヶ]{2,5}(さん|君|ちゃん|様|くん)",
]
ENTITY_RE = re.compile("|".join(ENTITY))

# 译文里的第三人称代词。`(?<!其)` 排掉「其他 / 其它」——那是限定词，不是代词。
PRONOUN_ZH = re.compile(r"(?<!其)[她他]")

# 引用标记：`って言` / `と言` —— 转述别人的话，主体可能合法是第三人
QUOTE_RE = re.compile(r"(って|と)(言|話|聞)")


def lrc_lines(path):
    out = []
    for line in open(path, encoding="utf-8-sig"):
        m = re.match(r"\[(\d+):(\d+\.\d+)\](.*)", line.strip())
        if m and m.group(3).strip():
            out.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
    return out


def asr_segs(path):
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return []
    return [(float(s.get("start", 0)), float(s.get("end", 0)), s.get("text") or "")
            for s in d.get("segments", [])]


def audit(prod_dir, asr_dir, span=1.5, lookback=6.0):
    hits = []
    total_pron = 0
    for lp in sorted(glob.glob(os.path.join(prod_dir, "*.lrc"))):
        stem = os.path.basename(lp)[:-4]              # 去掉 .lrc
        segs = asr_segs(os.path.join(asr_dir, stem + ".json"))
        if not segs:
            continue
        for t, zh in lrc_lines(lp):
            if not PRONOUN_ZH.search(zh):
                continue
            total_pron += 1
            here = "".join(s[2] for s in segs if s[0] - span <= t <= s[1] + span)
            prev = "".join(s[2] for s in segs if t - lookback <= s[0] < t - span)
            ctx = here + prev
            reasons = []
            if ENTITY_RE.search(here):
                reasons.append("本句有实体")
            if ENTITY_RE.search(prev):
                reasons.append("前句有实体")
            if QUOTE_RE.search(ctx):
                reasons.append("引用转述")
            hits.append({
                "track": stem, "t": round(t, 2), "zh": zh, "ja": here,
                "risk": not reasons, "pass_reason": "；".join(reasons),
            })
    risk = [h for h in hits if h["risk"]]
    return {
        "total_pronoun_lines": total_pron,
        "hits": len(hits),
        "risk": len(risk),
        "false_positive_est": 0,      # 需人工核，留字段占位
        "items": hits,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prod_dir")
    ap.add_argument("asr_dir")
    ap.add_argument("--json", default="")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    r = audit(a.prod_dir, a.asr_dir)
    if a.json:
        json.dump(r, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if not a.quiet:
        print("含第三人称代词的行：%d" % r["total_pronoun_lines"])
        print("其中原文前后 6s 无任何他者实体（**风险**）：%d" % r["risk"])
        print("（只报不改：实测误报约 15%，见文件头注释）")
        for h in r["items"]:
            if h["risk"]:
                print("  ❌ %s %7.2f | %s" % (h["track"][:16], h["t"], h["zh"][:32]))
            else:
                print("  ✓  %s %7.2f | %s（%s）"
                      % (h["track"][:16], h["t"], h["zh"][:28], h["pass_reason"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
