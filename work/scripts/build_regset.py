# -*- coding: utf-8 -*-
r"""构建「分层回归集」—— 从已产出的成品字幕里抽高风险 cue，生成人工标注表。

## 为什么（背景）
所有提质动作（cue 层重切、换初译模型、检索增强、角色状态层）都**需要一个客观的尺子**，
否则改了之后无法判断是真变好还是指标漂移。公开 BLEU/COMET 在成人向口语/呻吟/拟声域
基本失效，所以必须自建**小型、分层、冻结**的回归集。本脚本只做**候选抽取 + 分层抽样**，
标注与打分分别由人工和 `score_regset.py` 完成。

## 分层依据（高风险场景，按此自动打标签）
- `first_min`  首分钟（rescan 修复区，历史故障高发）
- `short`      过短 cue（<7 字，断行粒度的产物）
- `long`       过长 cue（>=15 字 或 显示 >6s）
- `negation`   含否定（ない/ません/じゃない ↔ 不/没/别）—— 译反会改变命题
- `question`   疑问（か/? ↔ ？）
- `onomat`     拟声/重复（通用翻译模型的重灾区）
- `confusable` 含已知近音混淆词（男子禁制/男子近世 等，见 CONFUSABLE）
- `fixed`      落在本轮修过的「重复行」位置附近（回归验证）
- `overlap`    与该轨的 ASR 段有时间重叠（交付层风险）

## 用法
  python build_regset.py [--out DIR] [--total 300] [--per-stratum 40] [--seed 20261013]

产物（默认 `$KIKOERU_ROOT/regset/`）：
  · `candidates.jsonl`  全部 cue（含标签），供程序化使用
  · `review.csv`        分层抽样后的**人工标注表**（UTF-8 BOM，Excel 可直接开）
  · `STATS.md`          分层计数与抽样说明
"""
import os
import re
import sys
import csv
import json
import glob
import random
import argparse

sys.stdout.reconfigure(encoding="utf-8")

import rootloc  # noqa: E402  仓库三层结构：work/ 里，成品/音频在上一级
OUT_ROOT = os.environ.get("KIKOERU_LRC_ROOT") or rootloc.PROD
ASR_ROOT = os.path.join(os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "draft")
DEF_OUT = os.path.join(os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "regset")

# 已知「两次解码易混」的近音词（用于 confusable 标签；可从平行语料继续扩）
CONFUSABLE = ["男子禁制", "男子近世", "重罪人", "10歳人", "料金", "料件",
              "悪い", "わりぃ", "我慢", "がまん"]

# 拟声/重复：连续同一字符 >=3，或 2~4 字单元连续重复 >=3 次
RE_CHARREP = re.compile(r"(.)\1{2,}")
RE_UNITREP = re.compile(r"(.{2,4})\1{2,}")
RE_QUESTION_JA = re.compile(r"[か?？]\s*$")
RE_NEG_JA = re.compile(r"ない|ません|じゃない|なかった|ないで|ねえ|ねぇ")
RE_NEG_ZH = re.compile(r"不|没|别|未|否|莫")
PUNCT = "。、，,.!！?？…♪~〜ー♡ \u3000「」『』（）()"


def norm(s):
    return "".join(c for c in (s or "") if c not in PUNCT)


def parse_lrc(path):
    """返回 [(t, zh)]，t 为秒。"""
    rows = []
    for ln in open(path, encoding="utf-8"):
        m = re.match(r"\[(\d+):(\d+\.\d+)\](.*)", ln.strip())
        if m:
            rows.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
    return rows


def load_asr(rj, track_name):
    """按 lrc 的音频名找同源 ASR json。"""
    cand = os.path.join(ASR_ROOT, rj, "asr", track_name + ".json")
    if os.path.exists(cand):
        return json.load(open(cand, encoding="utf-8"))
    # 兜底：按 source 字段扫
    for jf in glob.glob(os.path.join(ASR_ROOT, rj, "asr", "*.json")):
        try:
            d = json.load(open(jf, encoding="utf-8"))
        except Exception:
            continue
        if d.get("source") == track_name:
            return d
    return None


def ja_in_range(segs, t0, t1):
    """取 [t0,t1) 内的日文（优先词级，退化到段级）。"""
    out = []
    for s in sorted(segs, key=lambda z: z["start"]):
        ws = s.get("words") or []
        if ws:
            for w in ws:
                if t0 - 0.25 <= w["s"] < t1 - 0.05:
                    out.append(w["w"])
        elif t0 - 0.25 <= s["start"] < t1 - 0.05:
            out.append(s["text"])
    return "".join(out).strip()


def tags_of(t, dur, zh, ja):
    tg = []
    if t < 60:
        tg.append("first_min")
    nzh = norm(zh)
    if len(nzh) < 7:
        tg.append("short")
    if len(nzh) >= 20 or (dur >= 8 and len(nzh) >= 12):
        tg.append("long")
    if RE_NEG_JA.search(ja) or RE_NEG_ZH.search(zh):
        tg.append("negation")
    if RE_QUESTION_JA.search(ja) or zh.rstrip().endswith(("？", "?")):
        tg.append("question")
    if RE_CHARREP.search(ja) or RE_UNITREP.search(ja) or RE_CHARREP.search(zh) or RE_UNITREP.search(zh):
        tg.append("onomat")
    if any(c in ja for c in CONFUSABLE):
        tg.append("confusable")
    return tg


def sample(cues, total, per_stratum, rnd, strata):
    """分层抽样：每层取风险最高的 per_stratum 条，再随机补齐到 total。"""
    picked, seen = [], set()
    for st in strata:
        grp = [c for c in cues if st in c["tags"]]
        grp.sort(key=lambda c: (-len(c["tags"]), -c["cps"]))
        for c in grp[:per_stratum]:
            if c["id"] not in seen:
                seen.add(c["id"])
                picked.append(c)
    rest = [c for c in cues if c["id"] not in seen]
    rnd.shuffle(rest)
    for c in rest:
        if len(picked) >= total:
            break
        seen.add(c["id"])
        picked.append(c)
    picked = picked[:total]
    picked.sort(key=lambda c: (c["rj"], c["track"], c["start"]))
    return picked


COLS = ["id", "rj", "track", "start", "dur", "chars", "cps", "tags", "ja", "zh",
        "asr_ok", "faith_ok", "faith_err", "cohere_ok", "style_ok", "onomat_ok", "note"]


def write_review(path, picked):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for c in picked:
            row = {k: c.get(k, "") for k in COLS}
            row["tags"] = ";".join(c["tags"])
            for k in COLS[10:]:
                row[k] = ""
            w.writerow(row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEF_OUT)
    ap.add_argument("--total", type=int, default=300)
    ap.add_argument("--per-stratum", type=int, default=40)
    ap.add_argument("--holdout-works", type=int, default=3,
                    help="按作品隔离的最终留出集作品数（永不参与调参）")
    ap.add_argument("--dev-works", type=int, default=4, help="开发集作品数（调参用）")
    ap.add_argument("--seed", type=int, default=20261013)
    a = ap.parse_args()
    rnd = random.Random(a.seed)

    cues = []
    for rjdir in sorted(glob.glob(os.path.join(OUT_ROOT, "RJ*"))):
        rj = os.path.basename(rjdir)
        for lrc in sorted(glob.glob(os.path.join(rjdir, "*.lrc"))):
            track = os.path.basename(lrc)[:-4]          # 去掉 .lrc
            d = load_asr(rj, track)
            segs = (d or {}).get("segments", [])
            fixed = bool(d and d.get("overflow_fixed"))
            rows = parse_lrc(lrc)
            for i, (t, zh) in enumerate(rows):
                t1 = rows[i + 1][0] if i + 1 < len(rows) else t + 3.0
                dur = max(0.01, t1 - t)
                ja = ja_in_range(segs, t, t1)
                tg = tags_of(t, dur, zh, ja)
                if fixed:
                    tg.append("fixed")
                ov = 0
                for k in range(len(segs) - 1):
                    if t <= segs[k]["start"] < t1 and segs[k]["end"] > segs[k + 1]["start"] + 0.3:
                        ov += 1
                if ov:
                    tg.append("overlap")
                cues.append({
                    "id": "%s|%s|%.2f" % (rj, track, t),
                    "rj": rj, "track": track, "start": round(t, 2),
                    "dur": round(dur, 2), "zh": zh, "ja": ja,
                    "chars": len(norm(zh)), "cps": round(len(norm(zh)) / dur, 2),
                    "tags": tg,
                })

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "candidates.jsonl"), "w", encoding="utf-8") as f:
        for c in cues:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    strata = ["first_min", "short", "long", "negation", "question",
              "onomat", "confusable", "fixed", "overlap"]

    # ---- 按作品隔离划分 dev / regress / holdout（绝不能按句随机，否则同角色同术语泄漏）----
    works = sorted({c["rj"] for c in cues})
    wsh = works[:]
    random.Random(a.seed).shuffle(wsh)
    holdout = set(wsh[:a.holdout_works])
    dev = set(wsh[a.holdout_works:a.holdout_works + a.dev_works])
    regress = set(wsh[a.holdout_works + a.dev_works:])

    splits = {
        "review.csv": ([c for c in cues if c["rj"] in regress], a.total),
        "dev.csv": ([c for c in cues if c["rj"] in dev], max(60, a.total // 3)),
        "holdout.csv": ([c for c in cues if c["rj"] in holdout], max(60, a.total // 3)),
    }
    picked_all = {}
    for fn, (pool, tot) in splits.items():
        picked = sample(pool, tot, a.per_stratum, rnd, strata)
        picked_all[fn] = picked
        write_review(os.path.join(a.out, fn), picked)

    from collections import Counter
    cnt = Counter()
    for c in cues:
        for t in c["tags"]:
            cnt[t] += 1
    with open(os.path.join(a.out, "STATS.md"), "w", encoding="utf-8") as f:
        f.write("# 回归集分层统计\n\n")
        f.write("- 候选 cue 总数：**%d**（%d 部作品）\n" % (len(cues), len(works)))
        f.write("- **按作品隔离**划分（避免同角色/同术语泄漏）：\n")
        f.write("  - 冻结回归集 `review.csv`：%d 部 → %s\n" % (len(regress), "、".join(sorted(regress))))
        f.write("  - 开发集 `dev.csv`：%d 部 → %s\n" % (len(dev), "、".join(sorted(dev))))
        f.write("  - **最终留出集 `holdout.csv`（永不调参）**：%d 部 → %s\n\n"
                % (len(holdout), "、".join(sorted(holdout))))
        f.write("| 抽样文件 | 条数 |\n|---|---|\n")
        for fn, picked in picked_all.items():
            f.write("| %s | %d |\n" % (fn, len(picked)))
        f.write("\n| 分层 | 候选数 |\n|---|---|\n")
        for st in strata:
            f.write("| %s | %d |\n" % (st, cnt[st]))
        f.write("\n> 一个 cue 可命中多个分层；`tags` 列用 `;` 分隔。\n")

    print("候选 %d 条（%d 部作品）" % (len(cues), len(works)))
    print("  冻结回归集 review.csv : %d 条 / %d 部" % (len(picked_all["review.csv"]), len(regress)))
    print("  开发集     dev.csv    : %d 条 / %d 部" % (len(picked_all["dev.csv"]), len(dev)))
    print("  最终留出   holdout.csv: %d 条 / %d 部" % (len(picked_all["holdout.csv"]), len(holdout)))
    print("输出目录：%s" % a.out)


if __name__ == "__main__":
    main()

