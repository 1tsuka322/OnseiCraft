# -*- coding: utf-8 -*-
"""按**文本**重绑修复覆盖层（repair_overlay）的时间轴。

## 为什么需要

`repair_overlay` 的主键是「精确时间戳 `t0`」，`check()` 用 `abs(t - t0) < 0.02` 判定有效性。
流水线只要动一次切分（`SEG_FLUSH_CHARS` 16→10、VAD、重采样），cue 边界就漂 0.3~1.6 秒，
**所有基于时间戳强绑定的条目瞬间脱靶**——2026-10-30 实测 78 条里 54 条失效，
而且每部 run 都跑全库 `--check`，一次全库重跑刷出 864 条警告噪声。

脆弱寻址（brittle addressing）的解法是换成**语义锚定**：用条目里记录的日文
（`proposed_ja` / `original_ja`）在当前 ASR 里重新定位，再落到当前 LRC 的行上。

## 算法（两级）

1. **硬过滤**：同轨 + 时间窗口 `|t - t0| <= --window`（默认 6 秒）
2. **打分**：`score = 0.7 * lcs_ratio(锚, ASR) + 0.3 * (1 - 漂移/窗口)`
   - `lcs_ratio` = 最长公共子串长度 / min(len)，**不用整体相似度**：
     条目里的日文是人工修正过的完整句，ASR 是碎片（一句被切成多段），
     整体 ratio 天然偏低（实测 ≥0.85 只有 12/78），lcs 能到 22/78。
   - 但 lcs 对**短 ASR 会虚高**（`さくら…` 被长锚整段包含 → 1.00），
     所以加时间项压住，并且 `sim < --min-sim` 一律降级为 manual。

## 用法

    python work/scripts/rebase_overlay_by_text.py                  # 只出 diff（默认）
    python work/scripts/rebase_overlay_by_text.py --rj RJ1352698   # 只跑一个作品
    python work/scripts/rebase_overlay_by_text.py --apply          # 重写 overlay + 改成品 + 重打锁

输出 `work/tmp/overlay_migration_diff.json`：
`status` 三态 —— `already_ok`（时间轴本来就对得上）/ `auto`（可自动重绑）/ `manual`（要人看）。

⚠️ `--apply` 会**改成品 LRC**（走 fixline，自带备份），只处理 `auto` 且 `sim >= min-sim` 的条目。
先看 diff 再决定要不要 apply —— 这是本脚本的默认姿势。
"""
import argparse
import difflib
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rootloc  # noqa: E402

ROOT = rootloc.ROOT          # work/
BASE = rootloc.BASE          # 仓库根
PROD = rootloc.PROD          # kikoerusubtitle
DATA = rootloc.DATA          # work/data
OVL = os.path.join(DATA, "repair_overlay")
LOCKS = os.path.join(DATA, "locks")
PY = sys.executable
sys.stdout.reconfigure(encoding="utf-8")

STRIP = re.compile(r"[\s、。，．…！？!?「」『』（）()～~ー・,.\-—―♡♪☆★●○◆■]")
TS = re.compile(r"^\[(\d+):(\d+\.\d+)\](.*)$")


# ---------------------------------------------------------------- 基础工具
def norm(s):
    return STRIP.sub("", s or "")


def lcs_ratio(a, b):
    """最长公共子串占**较短一方**的比例。短文本被长文本包含时=1.0（所以要配时间项）。"""
    if not a or not b:
        return 0.0
    m = difflib.SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    return m.size / float(min(len(a), len(b)))


def jaccard(a, b, n=2):
    """2-gram 集合的 Jaccard。用来兜住**汉字/假名写法差异**：

    条目里是 `さくらの胸が`，ASR 是 `桜の胸が` —— lcs 只找连续公共子串，
    假名与汉字一换就断，实测 `t00-0214` 因此只有 0.32（人工看明明是同一句）；
    2-gram 有 `の胸が` 这类交集，能到 0.6 以上。
    """
    if len(a) < n or len(b) < n:
        return lcs_ratio(a, b)
    A = {a[i:i + n] for i in range(len(a) - n + 1)}
    B = {b[i:i + n] for i in range(len(b) - n + 1)}
    return len(A & B) / float(len(A | B))


def sim_ratio(a, b):
    """两种指标取大：lcs 抓长片段、jaccard 容忍写法差异。"""
    return max(lcs_ratio(a, b), jaccard(a, b))


def read_lrc(rj, track):
    """[(秒, 译文)]，按时间升序。track 可带或不带 .lrc。"""
    p = os.path.join(PROD, rj, track if track.endswith(".lrc") else track + ".lrc")
    out = []
    if not os.path.exists(p):
        return out
    for ln in open(p, encoding="utf-8"):
        m = TS.match(ln.rstrip("\n"))
        if m:
            out.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
    return sorted(out)


def asr_segs(rj, track):
    """当前 run 的 02_asr_fix 段（ASR 的最终版本）。"""
    cands = sorted(glob.glob(os.path.join(ROOT, "runs", "*_%s_production" % rj)))
    if not cands:
        return []
    stem = track[:-4] if track.endswith(".lrc") else track
    fs = [f for f in glob.glob(os.path.join(cands[-1], "02_asr_fix", rj, "asr_fix", "*.json"))
          if os.path.basename(f)[:-5] == stem]
    if not fs:
        return []
    return json.load(open(fs[0], encoding="utf-8")).get("segments") or []


def ja_at(segs, t, span=3.0, maxn=3):
    """取覆盖 t 的日文：起点落在 [t-span, t+span] 的段，最多拼 maxn 段。

    为什么拼接：一行 LRC 常常覆盖多个 ASR 段（cue 合并），只取单段会漏掉后半句，
    而条目里的 `proposed_ja` 是**完整句** → 只比单段会让 sim 系统性偏低。
    """
    near = [s for s in segs if -1.0 <= (s["start"] - t) <= span]
    if not near:
        return ""
    near.sort(key=lambda s: s["start"])
    return "".join((s.get("text") or "") for s in near[:maxn])


# ---------------------------------------------------------------- 匹配
def match_one(entry, segs, rows, window, min_sim):
    """返回 (status, best)。best = {new_t0, sim, score, ja, zh}。"""
    old_t0 = float(entry["t0"])
    anchor = norm(entry.get("proposed_ja") or entry.get("original_ja") or "")
    # ① 时间轴本来就对得上 → 不用动
    for t, _zh in rows:
        if abs(t - old_t0) < 0.02:
            return "already_ok", {"new_t0": round(t, 2), "sim": 1.0, "score": 1.0,
                                  "ja": ja_at(segs, t), "zh": _zh}
    if not anchor:
        return "manual", {"new_t0": None, "sim": 0.0, "score": 0.0,
                          "ja": "", "zh": "", "why": "条目没有日文，无法做文本锚定"}
    # ② 时间唯一性：窗口内只有一行，且漂移在实测范围内（0.3~1.6s，留到 2.5）
    #    这条比文本相似度更硬 —— 漂 0.4s 的条目，文本 sim 可能只有 0.4（汉字/假名写法差异），
    #    但「2 秒内就这一行」已经足够定位。sim >= 0.2 只是防完全错位（防窗口边界撞上别句）。
    cands = [(t, zh) for t, zh in rows if abs(t - old_t0) <= 2.5]
    if len(cands) == 1:
        t, zh = cands[0]
        ja = ja_at(segs, t)
        sim = sim_ratio(anchor, norm(ja))
        if sim >= 0.2:
            return "auto", {"new_t0": round(t, 2), "sim": round(sim, 3),
                            "score": round(0.7 * sim + 0.3 * (1.0 - abs(t - old_t0) / 2.5), 3),
                            "ja": ja, "zh": zh, "why": "时间唯一（2.5s 内仅此一行）"}
    best = None
    for t, zh in rows:
        drift = abs(t - old_t0)
        if drift > window:
            continue
        sim = sim_ratio(anchor, norm(ja_at(segs, t)))
        score = 0.7 * sim + 0.3 * (1.0 - drift / window)
        if best is None or score > best["score"]:
            best = {"new_t0": round(t, 2), "sim": round(sim, 3), "score": round(score, 3),
                    "ja": ja_at(segs, t), "zh": zh}
    if best is None:
        return "manual", {"new_t0": None, "sim": 0.0, "score": 0.0,
                          "ja": "", "zh": "", "why": "窗口内没有候选行"}
    # 文本证据路径也要限漂移：实测 cue 漂移是 0.3~1.6s，超过 2.0s 的多半是撞上了
    # 相邻的重复句（`大きくなってる` 这类短句一轨里出现好几次，sim 会虚高到 1.00）。
    ok = (best["score"] >= 0.75 and best["sim"] >= min_sim
          and abs(best["new_t0"] - old_t0) <= 2.0)
    return ("auto" if ok else "manual"), best


def build(rj_list, window, min_sim):
    items = []
    for rj in rj_list:
        p = os.path.join(OVL, rj + ".json")
        if not os.path.exists(p):
            continue
        d = json.load(open(p, encoding="utf-8"))
        seg_cache, row_cache = {}, {}
        for e in d["entries"]:
            tr = e["track"]
            if tr not in row_cache:
                row_cache[tr] = read_lrc(rj, tr)
                seg_cache[tr] = asr_segs(rj, tr)
            status, best = match_one(e, seg_cache[tr], row_cache[tr], window, min_sim)
            items.append({
                "rj": rj, "id": e["id"], "track": tr,
                "old_t0": round(float(e["t0"]), 2),
                "new_t0": best.get("new_t0"),
                "drift": (round(best["new_t0"] - float(e["t0"]), 2)
                          if best.get("new_t0") is not None else None),
                "sim": best.get("sim"), "score": best.get("score"),
                "status": status,
                "why": best.get("why", ""),
                "anchor_ja": (e.get("proposed_ja") or e.get("original_ja") or "")[:80],
                "matched_ja": (best.get("ja") or "")[:80],
                "lrc_now": (best.get("zh") or "")[:50],
                "proposed_zh": (e.get("proposed_zh") or "")[:50],
            })
    return items


# ---------------------------------------------------------------- apply
def apply_items(items, plan_out):
    """只处理 auto：改成品（走 fixline）+ 重写 overlay 的 t0 + 重打 E1 锁。"""
    todo = [x for x in items if x["status"] == "auto"]
    if not todo:
        print("没有 auto 条目，什么都没做")
        return 0
    plan = [{"rj": x["rj"], "track": x["track"][:-4] if x["track"].endswith(".lrc") else x["track"],
             "start": x["new_t0"], "zh": x["proposed_zh"],
             "why": "rebase_overlay_by_text: %s 漂 %.2fs" % (x["id"], x["drift"] or 0)}
            for x in todo if x["proposed_zh"]]
    json.dump(plan, open(plan_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("修复清单 → %s（%d 条）" % (plan_out, len(plan)))
    rc = subprocess.run([PY, os.path.join(ROOT, "tmp", "daily", "fix_lines_batch.py"),
                         plan_out, "--apply"], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    print((rc.stdout or "")[-1500:])
    if rc.returncode != 0:
        print("fixline 批量失败，中止（overlay 未改）")
        return 1
    # 重写 overlay 的 t0（保留 old_t0 作审计痕迹）
    by_rj = {}
    for x in todo:
        by_rj.setdefault(x["rj"], []).append(x)
    for rj, xs in by_rj.items():
        p = os.path.join(OVL, rj + ".json")
        d = json.load(open(p, encoding="utf-8"))
        idx = {e["id"]: e for e in d["entries"]}
        for x in xs:
            e = idx.get(x["id"])
            if not e or x["new_t0"] is None:
                continue
            e["old_t0"] = e.get("old_t0") or e["t0"]
            e["t0"] = x["new_t0"]
            e["rebase"] = {"by": "text-anchor", "at": "rebase_overlay_by_text",
                           "sim": x["sim"], "score": x["score"]}
        json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("overlay 已更新：%s（%d 条）" % (rj, len(xs)))
    # 重打 E1 锁（按当前盘上文本）
    lk = subprocess.run([PY, os.path.join(ROOT, "tmp", "daily", "lock_from_plan.py"), plan_out],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    print((lk.stdout or "")[-800:])
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rj", action="append", help="只处理这些作品（可多次）；默认全部")
    ap.add_argument("--window", type=float, default=6.0, help="时间窗口秒数（默认 6）")
    ap.add_argument("--min-sim", type=float, default=0.55,
                    help="auto 的最低文本相似度（默认 0.55；lcs 对短 ASR 会虚高，别调太低）")
    ap.add_argument("--out", default=os.path.join(ROOT, "tmp", "overlay_migration_diff.json"))
    ap.add_argument("--apply", action="store_true", help="真的写：改成品 + 重写 overlay + 重打锁")
    a = ap.parse_args()

    rj_list = a.rj or sorted(f[:-5] for f in os.listdir(OVL) if f.endswith(".json"))
    items = build(rj_list, a.window, a.min_sim)
    from collections import Counter
    cnt = Counter(x["status"] for x in items)
    doc = {"window": a.window, "min_sim": a.min_sim,
           "summary": {"total": len(items), **cnt}, "items": items}
    json.dump(doc, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("共 %d 条：already_ok %d · auto %d · manual %d"
          % (len(items), cnt["already_ok"], cnt["auto"], cnt["manual"]))
    print("diff → %s\n" % a.out)
    for st in ("auto", "manual"):
        sub = [x for x in items if x["status"] == st]
        if not sub:
            continue
        print("=== %s（%d 条）===" % (st, len(sub)))
        for x in sub[:12]:
            print("  %-11s %-9s %7.2f → %s  sim=%.2f score=%.2f"
                  % (x["rj"], x["id"], x["old_t0"],
                     ("%7.2f" % x["new_t0"]) if x["new_t0"] is not None else "   ——",
                     x["sim"] or 0, x["score"] or 0))
            if x["matched_ja"]:
                print("      锚: %s" % x["anchor_ja"][:44])
                print("      配: %s" % x["matched_ja"][:44])
        print()

    if a.apply:
        return apply_items(items, os.path.join(ROOT, "tmp", "overlay_rebase_plan.json"))
    print("（只出了 diff。核对无误后加 --apply 才写盘）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
