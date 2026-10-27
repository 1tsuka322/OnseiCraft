# -*- coding: utf-8 -*-
"""插行/改行后处理规范化（2026-10-20 · 待办 A2，对应 DEFECTS#13 / TRIAL_PLAN D-13）。

**为什么存在**：按时间戳插行的工具（`recover_splice.splice` / `rerun_segment` 补回 /
未来的 `fixline`）**绕过了 cue 层**，于是带上两个已被实测踩中的病：
  ① 幻觉词原样插入（恢复集里的 `おやすみなさい` 20 条有 16 条在中段 → 全是幻觉）；
  ② 长句一直挂到下一行（实测 13.6s，应 5.5s 封顶）。
今天靠人肉事后修（fix_recover_pack.py）→ 固化成每个插行工具**落盘前必经**的一步。

提供三件事（只做确定性动作，绝不改时间戳语义）：
  ① `drop_halluc`：ja 命中 `asr_halluc.HALLUC_WORDS` 且挂载时长 ≥ HALLUC_MIN_DUR → 剔除
     （时长 + 词表双条件：真实轨尾晚安只占 1.5–3s，长挂的才是幻觉）
  ② `split_long`：挂载 > HOLD_SEC 且字数 > MAX_CHARS → 按标点拆、按字数比例分配时间
     （不拆纯标点行——那是长静默清屏行，删了上一行要挂更久）
  ③ `check_timeline`：单调性 / 重叠检查，**只报告不自动改**
     （改时间戳会改变成品结构，属上游 cue 层的职责）

数据格式（本模块与调用方的唯一约定）：
    rows = [{"t": float 起始秒, "zh": 文本, "ja": 源日文或 "", "raw": 原始LRC行}]
    · ja 可空：没有 ASR 侧数据时（纯 LRC 后处理）自动跳过 ①
    · raw 为 None 时由 t/zh 重新生成

CLI（默认干跑；--apply 才写盘）：
    python postprocess.py <lrc文件或目录> [--apply] [--hold 6.0] [--max-chars 14]
"""
import os
import re
import sys
import glob
import json
import argparse

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)

HOLD_SEC = 6.0        # 挂载超过这么久就要拆（cue 层 MAX_HOLD_SEC=5.5 的同源口径）
MAX_CHARS = 14        # 且字数超过这么多才拆（短句闪一下是可接受的）
HARD_CHARS = 28       # 没有标点可拆时的强制拆分线（再长玩家真读不完）
MIN_PIECE_SEC = 0.4   # 拆出的每段至少给这么多秒（切太碎不如不拆）
BREAKS = "。！？!?…、"          # 优先断点（句读）
SOFT_BREAKS = "，,；;～~ "     # 次级断点（逗号/空格——本管线句内停顿本就常用空格）
# 纯标点行（清屏行）绝不拆：ja 本身就是「…」的忠实翻译，删/拆会让上一行挂 20–42 秒
PUNCT_ONLY = re.compile(r"^[。！？!?…、，,\s～~]+$")


def fmt_t(t):
    m, s = divmod(max(0.0, float(t)), 60)
    return "[%02d:%05.2f]" % (int(m), s)


def parse_lrc(path):
    """→ (header_lines, rows)。兼容 utf-8-sig / \r\n。"""
    ts_re = re.compile(r"^\[(\d+):([\d.]+)\](.*)$")
    head, rows = [], []
    for l in open(path, encoding="utf-8-sig", errors="replace").read().splitlines():
        m = ts_re.match(l)
        if m:
            rows.append({"t": int(m.group(1)) * 60 + float(m.group(2)),
                         "zh": m.group(3), "ja": "", "raw": l})
        else:
            head.append(l)
    return head, rows


def write_lrc(path, head, rows):
    out = list(head) + [r["raw"] for r in rows]
    open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")


def rebuild_raw(r):
    r["raw"] = "%s%s" % (fmt_t(r["t"]), r["zh"])
    return r


def attach_ja(rows, segs):
    """用 ASR 段按时间重叠回填 ja（供幻觉词判据）。

    归属口径：段起点落在 [本行 t, 下一行 t) 内即算本行的（与 cue 显示行切分同源；
    对 fixline 这种单行场景是近似，但用户可用 --ja 覆盖）。
    """
    for i, r in enumerate(rows):
        t1 = rows[i + 1]["t"] if i + 1 < len(rows) else r["t"] + HOLD_SEC
        r["ja"] = "".join(s.get("text", "") for s in segs
                          if r["t"] - 0.3 <= s.get("start", -1) < t1)
    return rows


def drop_halluc(rows):
    """① 幻觉词剔除。返回 (kept, dropped)。ja 为空的行全部保留。"""
    try:
        import asr_halluc as _ah
        words = getattr(_ah, "HALLUC_WORDS", [])
        min_dur = getattr(_ah, "HALLUC_MIN_DUR", 8.0)
    except Exception:
        return list(rows), []
    kept, dropped = [], []
    for i, r in enumerate(rows):
        t1 = rows[i + 1]["t"] if i + 1 < len(rows) else r["t"] + HOLD_SEC
        dur = t1 - r["t"]
        ja = r.get("ja") or ""
        hit = [w for w in words if w in ja]
        if hit and dur >= min_dur:
            dropped.append(dict(r, why="幻觉词%s 挂%.1fs≥%.0fs" % (hit, dur, min_dur)))
        else:
            kept.append(r)
    return kept, dropped


def _split_points(text):
    """返回可切的字符下标列表（切在该字符**之后**），按优先级从高到低。"""
    pts = [i for i, c in enumerate(text) if c in BREAKS]
    if not pts:
        pts = [i for i, c in enumerate(text) if c in SOFT_BREAKS]
    return pts


def _cut(text, breaks):
    """按 breaks 里的字符切句（切点字符归左段），丢 1 字碎片。"""
    pts = [i for i, c in enumerate(text) if c in breaks]
    pieces, last = [], 0
    for p in pts:
        seg = text[last:p + 1]
        if len(seg) >= 2:                # 不切出 1 字碎片
            pieces.append(seg)
            last = p + 1
    if last < len(text):
        pieces.append(text[last:])
    return [p for p in pieces if p]


def _alloc(t0, dur, pieces):
    """把 [t0, t0+dur] 按 pieces 的字数比例分配，保证每段 ≥ MIN_PIECE_SEC。

    最后一段吃掉剩余时间；任何一段排不下 MIN_PIECE_SEC 就整体放弃拆分
    （窗口太窄时宁可贵着，也不切碎）。
    """
    n = len(pieces)
    total = max(1, sum(len(p) for p in pieces))
    if dur < n * MIN_PIECE_SEC:
        return None
    out, cur = [], t0
    for k, p in enumerate(pieces):
        out.append(cur)
        if k < n - 1:
            remaining = (t0 + dur) - cur
            share = max(MIN_PIECE_SEC, dur * len(p) / total)
            share = min(share, remaining - (n - 1 - k) * MIN_PIECE_SEC)
            if share < MIN_PIECE_SEC:
                return None
            cur += share
    return out


def _split_pass(rows, hold, max_chars):
    """单轮拆分。返回 (new_rows, n_split)。"""
    new_rows, n_split = [], 0
    for i, r in enumerate(rows):
        t1 = rows[i + 1]["t"] if i + 1 < len(rows) else r["t"] + hold
        dur = t1 - r["t"]
        zh = (r.get("zh") or "").strip()
        need = (dur > hold and len(zh) > max_chars
                and not PUNCT_ONLY.match(zh or " "))
        if not need:
            new_rows.append(r)
            continue
        pieces = _cut(zh, BREAKS)
        if len(pieces) < 2:
            # 句读不够切（或唯一句读在句尾）→ 退到逗号/空格再切一刀
            pieces = _cut(zh, BREAKS + SOFT_BREAKS)
        # 无标点但已超长 → 二分强拆（玩家真读不完；重复词也可切）
        if len(pieces) == 1 and len(zh) > HARD_CHARS:
            mid = len(zh) // 2
            pieces = [zh[:mid], zh[mid:]]
        if len(pieces) < 2 or dur <= 2 * MIN_PIECE_SEC:
            new_rows.append(r)           # 拆不动/窗口太窄 → 原样留着并报告
            continue
        starts = _alloc(r["t"], dur, pieces)
        if starts is None:
            new_rows.append(r)
            continue
        n_split += 1
        for k, (p, st) in enumerate(zip(pieces, starts)):
            nr = dict(r, t=round(st, 3), zh=p)
            rebuild_raw(nr)
            new_rows.append(nr)
    return new_rows, n_split


def split_long(rows, hold=HOLD_SEC, max_chars=MAX_CHARS):
    """② 长行拆分。返回 (new_rows, n_split)。只拆「挂载久 + 字数多」的行。

    迭代到不再变化（每轮优先用句读断，句读用尽后退到逗号/空格），
    ——一次性只用句读会把「A？B，C，D。」切成两大块，第二块仍然超长。
    """
    cur = list(rows)
    total = 0
    for _ in range(6):                    # 安全上限，防病态文本死循环
        nxt, ns = _split_pass(cur, hold, max_chars)
        total += ns
        if ns == 0:
            return nxt, total
        cur = nxt
    return cur, total


def check_timeline(rows):
    """③ 单调 + 重叠检查。返回问题列表（字典）。"""
    issues = []
    for i in range(1, len(rows)):
        gap = rows[i]["t"] - rows[i - 1]["t"]
        if gap < 0:
            issues.append({"idx": i, "t": rows[i]["t"], "why":
                           "时间倒序 %.2fs（上一行 %.2f）" % (-gap, rows[i - 1]["t"])})
        elif gap == 0:
            issues.append({"idx": i, "t": rows[i]["t"], "why": "与上一行时间戳相同"})
    return issues


def normalize(rows, hold=HOLD_SEC, max_chars=MAX_CHARS, do_split=True, do_halluc=True):
    """插行工具的统一入口。返回 (new_rows, report)。"""
    rep = {"in": len(rows), "halluc_dropped": [], "split": 0, "timeline": []}
    kept, dropped = (drop_halluc(rows) if do_halluc else (rows, []))
    rep["halluc_dropped"] = dropped
    new_rows, ns = (split_long(kept, hold, max_chars) if do_split else (kept, 0))
    rep["split"] = ns
    issues = check_timeline(new_rows)
    rep["timeline"] = issues
    rep["out"] = len(new_rows)
    return new_rows, rep


def _fmt_report(path, rep):
    L = ["[%s] 入 %d 行 → 出 %d 行" % (os.path.basename(path), rep["in"], rep["out"])]
    for d in rep["halluc_dropped"]:
        L.append("  排幻觉 @%s %r（%s）" % (fmt_t(d["t"]), d.get("zh", "")[:20], d["why"]))
    if rep["split"]:
        L.append("  长行拆分 %d 行" % rep["split"])
    for it in rep["timeline"]:
        L.append("  ⚠️ 时间轴 @%s %s" % (fmt_t(it["t"]), it["why"]))
    return L


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="lrc 文件或目录")
    ap.add_argument("--apply", action="store_true", help="写盘（默认干跑）")
    ap.add_argument("--hold", type=float, default=HOLD_SEC)
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS)
    ap.add_argument("--no-split", dest="do_split", action="store_false")
    ap.add_argument("--no-halluc", dest="do_halluc", action="store_false")
    a = ap.parse_args()

    files = ([a.target] if os.path.isfile(a.target)
             else sorted(glob.glob(os.path.join(a.target, "*.lrc"))))
    if not files:
        sys.exit("没有找到 lrc：%s" % a.target)
    need_write = False
    for p in files:
        head, rows = parse_lrc(p)
        new_rows, rep = normalize(rows, a.hold, a.max_chars,
                                  a.do_split, a.do_halluc)
        changed = len(new_rows) != len(rows) or any(
            r1["raw"] != r2["raw"] for r1, r2 in zip(rows, new_rows))
        for l in _fmt_report(p, rep):
            print(l)
        if changed and not a.apply:
            print("  （干跑；加 --apply 写盘）")
        if changed:
            need_write = True
            if a.apply:
                bak = p + ".bak_prepost"
                if not os.path.exists(bak):
                    import shutil
                    shutil.copy2(p, bak)
                write_lrc(p, head, new_rows)
                print("  已写盘（备份 %s）" % os.path.basename(bak))
    if not need_write:
        print("\n无需处理（没有长行/幻觉/时间轴问题）")


if __name__ == "__main__":
    main()
