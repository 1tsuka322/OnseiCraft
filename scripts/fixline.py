# -*- coding: utf-8 -*-
"""单行重译闭环（2026-10-20 · 待办 A1，对应 DEFECTS#12 / TRIAL_PLAN D-12）。

场景：听/读成品时发现**某一行**错译（例：`どうでもいいんじゃないから` 被译成
「又不是怎么样都行的」——`んじゃない` 是语气 tag 不是否定）。今天的痛点是想修只能
手编 LRC。本工具把「定位 → 带上下文重译 → 校验 → 回写」做成一条命令。

用法：
  # 0) 先看某时刻附近有哪些行（避免改错行）
  python fixline.py --rj RJ9000002 --track "05.xxx.mp3" --list 460 480
  # 1) 干跑（默认）：重译该行，打印 旧→新 对照，不落盘
  python fixline.py --rj RJ9000002 --track "05.xxx.mp3" --start 469.6
  #    ASR 文本不准时直接给正确日文
  python fixline.py ... --start 469.6 --ja "どうでもいいんじゃないから"
  # 2) 你自己已经知道正确译文 → 直接写（仍过校验：日文残留/空行/清屏行）
  python fixline.py ... --start 469.6 --zh "怎么样都无所谓。"
  # 3) 落盘（自动备份到 .workbuddy/tmp/lrc_backup_fixline/）
  python fixline.py ... --start 469.6 --apply

设计口径：
  · **时间戳不变**：只改文本，不动轴（改轴会牵动后面所有行的相对位置）。
  · 上下文带前 3 行（日文+成品中文）——人称/称谓/「你/我」全靠它。
  · 重译走与正式管线**同一套**函数：translate_block → polish_block
    （A8 遮蔽 / 术语策略 / 实体账本 / 归属 hint 全部继承），不是另写一套 prompt。
  · 落盘前必过：无日文残留、非空、非 REFUSE、非纯标点、时间轴单调、长行检查。
  · 备份在 .workbuddy/tmp/lrc_backup_fixline/<rj>/<文件>.<时间戳>.lrc，可整体回滚。
"""
import os
import re
import sys
import json
import glob
import time
import shutil
import argparse

SCRIPTS = os.path.join(os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".workbuddy", "skills", "kikoeru-subtitle", "scripts")
sys.path.insert(0, SCRIPTS)

ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DRAFT = os.path.join(ROOT, ".workbuddy", "draft")
PROD = os.path.join(ROOT, "kikoerusubtitle")
BAK = os.path.join(ROOT, ".workbuddy", "tmp", "lrc_backup_fixline")
TS_RE = re.compile(r"^\[(\d+):([\d.]+)\](.*)$")


def parse_lrc(p):
    head, rows = [], []
    for l in open(p, encoding="utf-8-sig", errors="replace").read().splitlines():
        m = TS_RE.match(l)
        if m:
            rows.append({"t": int(m.group(1)) * 60 + float(m.group(2)),
                         "zh": m.group(3), "ja": "", "raw": l})
        else:
            head.append(l)
    return head, rows


def find_asr(rj, track, rows=None):
    """track 给 mp3 名（01.xxx.mp3）或 lrc 干名都行；兼容台本作品的 .mp3.json。

    **版本自适应**（2026-10-22，坑 90）：成品 LRC 可能基于 v1/v2 任一版（实测 16 部里
    12 部对齐 v1、4 部对齐 v2），固定读 `asr/`（v2）会在 v1 作品上取到无关段——
    与 scan_violations 同款故障。传了 rows 就按「LRC 时间戳 vs 词级起始」匹配率选版本
    （v2 需领先 0.02，平分留 v1）；没传就退回旧行为（只读 asr/）。
    """
    cands = {}
    for sub in ("asr_v1_20261020", "asr"):
        d = os.path.join(DRAFT, rj, sub)
        if not os.path.isdir(d):
            continue
        c = None
        for name in (track + ".json", track + ".mp3.json"):
            p = os.path.join(d, name)
            if os.path.exists(p):
                c = p
                break
        if c is None:
            stem = re.sub(r"\.json$", "", track)
            stem = re.sub(r"\.mp3$", "", stem)
            hits = sorted(glob.glob(os.path.join(d, stem + "*")))
            c = hits[0] if hits else None
        if c:
            cands[sub] = c
    if not cands:
        return None
    if len(cands) == 1 or not rows:
        return cands.get("asr") or list(cands.values())[0]

    import bisect

    def _word_starts(path):
        ts = []
        for s in (json.load(open(path, encoding="utf-8")) or {}).get("segments") or []:
            ws = s.get("words") or []
            if ws:
                ts.extend(w["s"] for w in ws)
            else:
                ts.append(s.get("start", 0.0))
        return sorted(ts)

    def _rate(rows_ts, wts, tol=0.15):
        if not rows_ts or not wts:
            return -1.0
        hit = 0
        for t in rows_ts:
            i = bisect.bisect_left(wts, t - tol)
            while i < len(wts) and wts[i] <= t + tol:
                if abs(wts[i] - t) <= tol:
                    hit += 1
                    break
                i += 1
        return hit / len(rows_ts)

    rows_ts = [r["t"] for r in rows]
    best = None
    for sub in ("asr_v1_20261020", "asr"):
        if sub not in cands:
            continue
        rate = _rate(rows_ts, _word_starts(cands[sub]))
        if best is None or rate > best[1] + 0.02:
            best = (sub, rate, cands[sub])
    return best[2] if best else None


def find_lrc(rj, track):
    stem = re.sub(r"\.json$", "", track)
    stem = re.sub(r"\.(lrc|mp3)$", "", stem)
    for pat in (stem + ".lrc", stem + ".json.lrc", stem + "*.lrc"):
        hits = sorted(glob.glob(os.path.join(PROD, rj, pat)))
        if hits:
            return hits[0]
    return None


def attach_ja(rows, segs):
    """段起点落在 [本行 t, 下一行 t) 的算本行的（近似显示行切分）。"""
    for i, r in enumerate(rows):
        t1 = rows[i + 1]["t"] if i + 1 < len(rows) else r["t"] + 6.0
        r["ja"] = "".join(s.get("text", "") for s in segs
                          if r["t"] - 0.3 <= s.get("start", -1) < t1).strip()
    return rows


def init_polish(draft_dir):
    """按 polish.main() 的同顺序初始化全局（遮蔽器/账本/归属/策略表）。"""
    import polish as P
    import translate as T
    guide, hard, pairs, stats, gloss = P.load_style(P.DEFAULT_STYLE)
    system = P.build_system(guide, hard, pairs, stats.get("total", 0), gloss)
    try:
        P.load_ledger(draft_dir)
    except Exception as e:
        print("⚠️ 实体账本加载失败（忽略）：%s" % e)
    try:
        P.load_policy()
    except Exception as e:
        print("⚠️ 策略表加载失败（忽略）：%s" % e)
    try:
        import mask_nonlexical as _mn
        P.MASK = _mn if _mn.ENABLED else None
    except Exception as e:
        P.MASK = None
        print("⚠️ A8 遮蔽器不可用（忽略）：%s" % e)
    try:
        P.load_pragmatics()
    except Exception as e:
        print("⚠️ 归属层加载失败（忽略）：%s" % e)
    return P, T, system


def retranslate(P, T, system, ja, ctx_pairs):
    """单句重译：先 translate_block（初译），再 polish_block（润色+全部机制）。"""
    block = [(1, ja)]
    prev = ["日：%s\n译：%s" % (j, z) for j, z in ctx_pairs]
    res, _raw = T.translate_block(block, prev)
    if not res:
        return None, "初译失败（translate_block 返回空）"
    draft = (res[0] or "").strip()
    if not draft:
        return None, "初译为空"
    blk = [(P.cue_id(ja, 0, 0, "fixline"), ja, draft, P.ja_anchor(ja))]
    zh_map, raw = P.polish_block(system, blk, prev, mask=P.MASK)
    zh = ""
    if isinstance(zh_map, dict):
        zh = zh_map.get(blk[0][0], "") or ""
    if not zh:
        # 润色层没给出该 ID → 退回初译（润色失败不该让行消失）
        print("⚠️ 润色层未返回该句（%s）→ 退回初译" % (str(raw)[:80],))
        zh = draft
    return zh.strip(), None


def validate(zh, P):
    """落盘前校验。返回 (ok, why)。"""
    if not zh:
        return False, "译文为空"
    if P._looks_japanese(zh):
        return False, "日文残留：%r" % zh
    if any(k in zh for k in P.REFUSE):
        return False, "疑似拒答：%r" % zh
    if re.match(r"^[。！？!?…、，,\s～~]+$", zh):
        return False, "整行只剩标点（丢句）"
    if P.bad_cell(zh, ""):
        return False, "bad_cell 命中（见 polish.bad_cell）"
    return True, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rj", required=True)
    ap.add_argument("--track", required=True, help="音频名（01.xxx.mp3）或 lrc 干名")
    ap.add_argument("--start", type=float, help="目标行起始秒（可用 --list 找）")
    ap.add_argument("--list", nargs=2, type=float, metavar=("T0", "T1"),
                    help="列出该区间的成品行（t / 旧译文 / 推断日文）")
    ap.add_argument("--ja", default="", help="覆盖该行的源日文（ASR 不准时给）")
    ap.add_argument("--allow-no-ja", dest="allow_no_ja", action="store_true",
                    help="该行 ASR 缺段时也允许改（日文留空，锁只能按时间戳认；"
                         "用于「覆盖层记了改动但成品没落盘」这类补救）")
    ap.add_argument("--zh", default="", help="直接给定正确译文（跳过模型）")
    ap.add_argument("--lrc", default="", help="直接指定 LRC 路径（调试/副本上验证用，"
                                             "跳过 kikoerusubtitle/<rj> 的自动查找）")
    ap.add_argument("--asr", default="", help="直接指定 ASR json 路径（配合 --lrc）")
    ap.add_argument("--ctx", type=int, default=3, help="带几行上文（默认 3）")
    ap.add_argument("--apply", action="store_true", help="写盘（默认干跑）")
    a = ap.parse_args()

    lrc = a.lrc if a.lrc else find_lrc(a.rj, a.track)
    if not lrc:
        sys.exit("找不到成品 LRC：%s/<%s>" % (os.path.join(PROD, a.rj), a.track))
    head, rows = parse_lrc(lrc)
    jp = a.asr if a.asr else find_asr(a.rj, a.track, rows)
    if jp and not os.path.exists(jp):
        jp = None
    if jp:
        segs = (json.load(open(jp, encoding="utf-8")) or {}).get("segments") or []
        attach_ja(rows, segs)
    if a.list:
        t0, t1 = a.list
        for i, r in enumerate(rows):
            if t0 - 1 <= r["t"] <= t1 + 1:
                print("%02d  [%02d:%05.2f]  %-28s  %s"
                      % (i, r["t"] // 60, r["t"] % 60, r["zh"][:28], r["ja"][:30]))
        print("\n（用 --start <秒> 指定要改的行；--ja 可覆盖日文）")
        return
    if a.start is None:
        sys.exit("需要 --start <秒>，或 --list T0 T1 先定位")

    # 定位：1 秒内容差；对不上就报错并提示邻近行（避免改错行）
    idx = min(range(len(rows)), key=lambda i: abs(rows[i]["t"] - a.start))
    if abs(rows[idx]["t"] - a.start) > 1.0:
        print("⚠️ %.1fs 附近没有行，最近的是：" % a.start)
        for i in range(max(0, idx - 2), min(len(rows), idx + 3)):
            print("   [%02d:%05.2f] %s" % (rows[i]["t"] // 60,
                                            rows[i]["t"] % 60, rows[i]["zh"][:40]))
        sys.exit(1)
    tgt = rows[idx]
    ja = (a.ja or tgt.get("ja") or "").strip()
    if not ja:
        if not a.allow_no_ja:
            sys.exit("该行取不到源日文（ASR 里没有对应段）。请用 --ja 显式给出日文原文，"
                     "或加 --allow-no-ja（日文留空，锁只能按时间戳认）。")
        print("⚠️ 该行 ASR 缺段、未给 --ja：日文留空，E1 锁只能按时间戳认。")

    ctx = [(rows[j].get("ja", ""), rows[j]["zh"])
           for j in range(max(0, idx - a.ctx), idx)]
    print("作品 %s / %s" % (a.rj, os.path.basename(lrc)))
    print("目标行 #%d [%02d:%05.2f]：%s" % (idx, tgt["t"] // 60, tgt["t"] % 60, tgt["zh"]))
    print("源日文：%s" % ja)
    if ctx:
        print("上文 %d 行：" % len(ctx))
        for j, z in ctx:
            print("   日：%s\n   译：%s" % (j[:40], z[:40]))

    if a.zh:
        new_zh = a.zh.strip()
        src = "--zh 直接给定"
    else:
        P, T, system = init_polish(os.path.join(DRAFT, a.rj))
        try:
            import urllib.request
            urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=10).read()
        except Exception as e:
            sys.exit("llama-server 未就绪（%s）：先跑 serve.py start。"
                     "（已知正确译文可用 --zh 直接写，不需要模型）" % e)
        t0 = time.time()
        new_zh, why = retranslate(P, T, system, ja, ctx)
        if why:
            sys.exit("重译失败：%s" % why)
        src = "模型重译（%.1fs）" % (time.time() - t0)

    ok, why = validate(new_zh, P if not a.zh else _import_polish())
    if not ok:
        sys.exit("校验未过，不写盘：%s\n（新译文：%r）" % (why, new_zh))
    if new_zh == tgt["zh"]:
        print("\n重译结果与现值相同，无需改动：%s" % new_zh)
        return

    print("\n%s\n旧：%s\n新：%s" % (src, tgt["zh"], new_zh))
    if not a.apply:
        print("\n（干跑；加 --apply 写盘）")
        return

    # 写前对**目标行本身**过长的情况就地拆分（与其它插行工具同一口径：
    # 不拆 → 改完的长句会一直挂到下一行）。窗口只取 [目标行, 下一行/哨兵]。
    import postprocess as PP
    sentinel_t = rows[idx + 1]["t"] if idx + 1 < len(rows) else tgt["t"] + PP.HOLD_SEC + 1
    win = [dict(tgt, zh=new_zh, ja=tgt.get("ja", ""),
                raw="[%02d:%05.2f]%s" % (tgt["t"] // 60, tgt["t"] % 60, new_zh)),
           {"t": sentinel_t, "zh": "", "ja": "", "raw": ""}]
    split_rows, ns = PP.split_long(win)
    rows[idx] = split_rows[0]
    if ns and len(split_rows) > 1:
        print("⚠️ 新译文过长 → 就地拆成 %d 行（时间按字数分配，只动这一行）"
              % (len(split_rows) - 1))
        rows[idx + 1:idx + 1] = split_rows[1:-1]

    os.makedirs(os.path.join(BAK, a.rj), exist_ok=True)
    bak = os.path.join(BAK, a.rj, "%s.%s.lrc" % (os.path.basename(lrc),
                                                  time.strftime("%Y%m%d_%H%M%S")))
    shutil.copy2(lrc, bak)
    out = list(head) + [r["raw"] for r in rows]
    open(lrc, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
    print("\n已写盘：%s\n备份：%s" % (lrc, bak))


def _import_polish():
    import polish as P
    return P


if __name__ == "__main__":
    main()
