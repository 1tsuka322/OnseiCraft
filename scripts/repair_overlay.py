"""修复覆盖层（repair_overlay）—— 时间轴不变的人工修复记录（2026-10-26 R133，GPT Q2）。

## 为什么不是「重跑 ASR 再替换成品」

没有台本时，**新 ASR 看起来更像话 ≠ 更正确**。原始音频才是唯一证据，重跑只是提出
另一份假设。GPT 的要求是：把重跑/推断的结果当**候选**存进覆盖层，时间轴一格不动，
人工（或听辨）确认后才生效；永远不在无 GT 的区域直接自动覆盖。

## 数据结构

`.workbuddy/repair_overlay/<RJ>.json`：

```json
{"rj": "RJ9000010",
 "entries": [
   {"id": "t09-0546",
    "track": "09.xxx_SE.mp3.lrc",
    "cue": 57,                     // 原 LRC 行号（1-based），时间轴锚点
    "t0": 346.52, "t1": 354.32,    // 该 cue 的音频窗（原时间轴，不改）
    "original_ja": "はい、真っ赤に充血した古ぼっきクリトリスでスンドメオナにします",
    "proposed_ja": "はい、真っ赤に充血したフル勃起クリトリスで寸止めオナニーにします",
    "original_zh": "是的 用这颗充血发红的老化龟头进行吮吸",
    "proposed_zh": "是的 就用这颗充血发红的完全勃起阴蒂 来做寸止自慰",
    "evidence": "古ぼっき→フル勃起（errata, 语料10段/7部）；クリトリス→阴蒂（本作8次/0次チンポ）",
    "confidence": "low",           // high=台本/双源；mid=术语表+上下文；low=语义推断
    "status": "pending_listen",    // pending_listen / confirmed / rejected
    "source": "R132 semantic reconstruction"}
 ]}
```

## 状态机

- `pending_listen`：**已写进 LRC 但没听音频确认**——这类必须在文档里列出来，别当已核实。
- `confirmed`：听过音频（或台本/双源）确认 → 可以写 LRC 并登记 E1 锁。
- `rejected`：听了发现原译更好 → 回滚 `original_zh`。

## 用法

```bash
PY="C:/Users/zyk/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
"$PY" scripts/repair_overlay.py --list RJ9000010        # 看该作品的覆盖层
"$PY" scripts/repair_overlay.py --pending               # 全部待听条目（跨作品）
"$PY" scripts/repair_overlay.py --check                 # 时间轴 + confirmed 锁校验
# T8（2026-10-26）写入与状态流转：
"$PY" scripts/repair_overlay.py --add plan.json --confidence low --status pending_listen
"$PY" scripts/repair_overlay.py --fill-original          # 补 original_zh（从 fixline 备份）
"$PY" scripts/repair_overlay.py --confirm t09-0346 [--zh "…"]   # 听完确认（可选覆盖译文）
"$PY" scripts/repair_overlay.py --reject  t09-0346              # 原译更好：original_zh 经 fixline 回滚
```

## `original_zh` 为什么要能补（2026-10-26）

`--add` 只认 plan 里的 `original_zh`。R132 有几条 plan 没带，导致条目**没有「改之前」的文本**，
`--reject` 直接不可用（听完了想说「原译更好」却回滚不了）。`--fill-original` 从
`.workbuddy/tmp/lrc_backup_fixline/<rj>/<轨>.<时间戳>.lrc` 取**最早**一份该时间戳的文本回填。

⚠️ 口径限制：取的是**最早**备份，如果该行在本次修改之前已被别的批次改过，那它不是「紧邻上一版」。
所以回填只补空值、不覆盖已有值，并在 `original_zh_src` 里记来源文件名备查。
"""
import argparse
import glob
import json
import os
import re
import sys

ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OVL = os.path.join(ROOT, ".workbuddy/repair_overlay")
LRC = os.path.join(ROOT, "kikoerusubtitle")
LOCKS = os.path.join(ROOT, ".workbuddy/locks")
BAK = os.path.join(ROOT, ".workbuddy/tmp/lrc_backup_fixline")
sys.stdout.reconfigure(encoding="utf-8")


def load(rj):
    p = os.path.join(OVL, rj + ".json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"rj": rj, "entries": []}


def save(d):
    os.makedirs(OVL, exist_ok=True)
    json.dump(d, open(os.path.join(OVL, d["rj"] + ".json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def lrc_rows(rj, track):
    p = os.path.join(LRC, rj, track)
    out = []
    for ln in open(p, encoding="utf-8"):
        m = re.match(r"^\[(\d+):(\d+\.\d+)\](.*)$", ln.rstrip("\n"))
        if m:
            out.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3)))
    return out


def fmt_ts(t):
    """锁 key 的时间戳格式（与 polish.fmt_lrc / review.py 同口径）。"""
    m, s = divmod(max(0.0, t), 60)
    return "[%02d:%05.2f]" % (int(m), s)


def _iter_overlays():
    if not os.path.isdir(OVL):
        return
    for f in sorted(os.listdir(OVL)):
        if f.endswith(".json"):
            yield load(f[:-5])


# ---- T8（2026-10-26）：写入与状态流转 ------------------------------------------------
def cmd_add(plan_path, confidence="low", status="pending_listen"):
    """从 fix 计划批量写入覆盖层（只记录，**不改 LRC**——R133 协议：改成品走 fixline）。

    plan：`[{rj, track, start, zh, why, ja, original_zh?}]`（fix_lines_batch 同格式）。
    定位：±0.6s 容差取最近时间戳行；id = `<轨前缀>-<秒数四位>`（t09-0346 式，秒数取
    **定位后 t0** 的整数部分）。已存在的 id 不覆盖（幂等）。
    original_zh 只认 plan——LRC 当前文本可能是 fix_lines_batch 已改过的新文，不能当「改之前」。
    """
    plan = json.load(open(plan_path, encoding="utf-8"))
    added = skipped = missed = 0
    for it in plan:
        rj = it.get("rj") or ""
        track = it.get("track") or ""
        if not rj or not track:
            print("skip 字段不全（缺 rj/track）：%s" % json.dumps(it, ensure_ascii=False)[:80])
            missed += 1
            continue
        tname = track if track.endswith(".lrc") else track + ".lrc"
        try:
            rows = lrc_rows(rj, tname)
        except OSError as e:
            print("skip 找不到 LRC %s/%s（%s）" % (rj, tname, e))
            missed += 1
            continue
        start = float(it.get("start") or 0)
        if not rows:
            print("skip LRC 无时间戳行 %s/%s" % (rj, tname))
            missed += 1
            continue
        idx = min(range(len(rows)), key=lambda i: abs(rows[i][0] - start))
        if abs(rows[idx][0] - start) > 0.6:
            print("skip 定位失败 %s/%s %.2fs（最近行 %.2fs，超 ±0.6s）"
                  % (rj, tname, start, rows[idx][0]))
            missed += 1
            continue
        t0 = rows[idx][0]
        t1 = rows[idx + 1][0] if idx + 1 < len(rows) else t0
        stem = tname[:-4]
        m = re.match(r"\s*(\d{1,3})", stem)
        eid = "%s-%04d" % ("t" + (m.group(1) if m else "00"), int(t0))
        d = load(rj)
        if any(e["id"] == eid for e in d["entries"]):
            print("skip 已存在 %s" % eid)
            skipped += 1
            continue
        oz = (it.get("original_zh") or "").strip()
        d["entries"].append({
            "id": eid, "track": tname, "cue": idx + 1,
            "t0": round(t0, 2), "t1": round(t1, 2),
            "original_ja": it.get("original_ja", ""),
            "proposed_ja": it.get("ja", ""),
            "original_zh": oz,
            "proposed_zh": it.get("zh", ""),
            "evidence": it.get("why", ""),
            "confidence": confidence, "status": status,
            "source": "--add %s" % os.path.basename(plan_path),
        })
        d["entries"].sort(key=lambda e: (e["track"], e["t0"]))
        save(d)
        added += 1
        if not oz:
            print("  提示 %s original_zh 为空（plan 未提供改前文本；--reject 回滚将不可用）" % eid)
    print("add 完成：新增 %d · skip 已存在 %d · 定位失败 %d（来自 %s）"
          % (added, skipped, missed, os.path.basename(plan_path)))


def _recover_zh_from_backup(rj, track, t0, tol=0.02):
    """从 fixline 备份里取**最早**一份该轨在 t0 的文本；返回 (text, 文件名) 或 ("", "")。"""
    for f in sorted(glob.glob(os.path.join(BAK, rj, track + ".*.lrc"))):
        try:
            rows = []
            for ln in open(f, encoding="utf-8"):
                m = re.match(r"^\[(\d+):(\d+\.\d+)\](.*)$", ln.rstrip("\n"))
                if m:
                    rows.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3)))
        except OSError:
            continue
        for t, txt in rows:
            if abs(t - t0) <= tol and txt.strip():
                return txt.strip(), os.path.basename(f)
    return "", ""


def cmd_fill_original():
    """补 `original_zh` 空值（只补空、不覆盖），来源记进 `original_zh_src`。

    为什么需要：`--add` 只认 plan 里的 original_zh；R132 有 3 条没带 →
    `--reject` 无法回滚（听辨结论「原译更好」落不了地）。
    """
    filled = missing = kept = 0
    for d in _iter_overlays():
        changed = False
        for e in d["entries"]:
            if (e.get("original_zh") or "").strip():
                kept += 1
                continue
            txt, src = _recover_zh_from_backup(d["rj"], e["track"], float(e.get("t0") or 0))
            if not txt:
                missing += 1
                print("⚠️ %s %s %.2fs 备份里也找不到改前文本（--reject 仍不可用）"
                      % (d["rj"], e["id"], float(e.get("t0") or 0)))
                continue
            e["original_zh"] = txt
            e["original_zh_src"] = "fixline_backup:" + src
            changed = True
            filled += 1
            print("✅ %s %s → %s   [%s]" % (d["rj"], e["id"], txt[:40], src))
        if changed:
            save(d)
    print("fill-original：补 %d 条 · 原有 %d 条 · 找不到 %d 条" % (filled, kept, missing))


def _find_entry(eid):
    """按 id 跨作品找条目，返回 (data, entry) 或 (None, None)。"""
    for d in _iter_overlays():
        for e in d["entries"]:
            if e["id"] == eid:
                return d, e
    return None, None


def cmd_confirm(eid, zh=None):
    """听完音频确认。可选 --zh 覆盖最终译文（只记录；写 LRC/登记锁是独立步骤，
    --check 会查 confirmed 必须有锁）。"""
    d, e = _find_entry(eid)
    if e is None:
        sys.exit("找不到 id：%s" % eid)
    e["status"] = "confirmed"
    if zh:
        e["proposed_zh"] = zh
    save(d)
    print("confirmed %s（%s cue%s %.2fs）%s"
          % (eid, d["rj"], e.get("cue", "?"), e.get("t0", 0),
             "→ %s" % zh if zh else ""))
    print("  提醒：E1 锁需另行登记（lock_from_plan / review 页），--check 会校验")


def cmd_reject(eid):
    """听了发现原译更好：标 rejected，并把 original_zh 写回 LRC——**统一走 fixline**
    （--zh + --apply），它自带备份；本工具绝不自己改成品。"""
    d, e = _find_entry(eid)
    if e is None:
        sys.exit("找不到 id：%s" % eid)
    oz = (e.get("original_zh") or "").strip()
    if not oz:
        sys.exit("%s 的 original_zh 为空，无法回滚（该条 plan 未提供改前文本）" % eid)
    e["status"] = "rejected"
    save(d)
    track_stem = e["track"][:-4] if e["track"].endswith(".lrc") else e["track"]
    cmd = [sys.executable,
           os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixline.py"),
           "--rj", d["rj"], "--track", track_stem,
           "--start", "%.2f" % e.get("t0", 0), "--zh", oz, "--apply"]
    print("rejected %s → 回滚 original_zh（经 fixline，含备份）：" % eid)
    print("  " + " ".join(cmd))
    import subprocess
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("  ⚠️ fixline 回滚失败（退出码 %d），状态已标 rejected，请手工核对 LRC" % r.returncode)


def lock_keys(rj):
    """该作品的 E1 锁 key 集合（fn 归一化去掉 .lrc，与 polish.load_locks 同口径）。"""
    p = os.path.join(LOCKS, rj + ".json")
    if not os.path.exists(p):
        return set()
    out = set()
    try:
        for k in (json.load(open(p, encoding="utf-8")) or {}):
            if "|" not in k:
                continue
            fn, ts = k.split("|", 1)
            out.add((fn[:-4] if fn.lower().endswith(".lrc") else fn, ts))
    except Exception:
        pass
    return out


def show(rj=None, only_pending=False):
    files = ([rj + ".json"] if rj else
             [f for f in sorted(os.listdir(OVL)) if f.endswith(".json")] if os.path.isdir(OVL) else [])
    n = 0
    for f in files:
        d = load(f[:-5])
        for e in d["entries"]:
            if only_pending and e.get("status") != "pending_listen":
                continue
            n += 1
            print("%s  %-8s cue%-4s %7.2f  [%s/%s]  %s" % (
                d["rj"], e["id"], e.get("cue", "?"), e["t0"],
                e.get("confidence", "?"), e.get("status", "?"), e.get("proposed_zh", "")[:34]))
            if e.get("evidence"):
                print("        证据：%s" % e["evidence"][:90])
    print("\n共 %d 条" % n)


def check():
    """校验两件事：
    ① t0 必须还能在原 LRC 里找到（时间轴没被重跑改过）；
    ② T8 新增：confirmed 条目必须在 .workbuddy/locks/<rj>.json 里有对应锁——
       锁是「重跑别冲掉人工修正」的**唯一**保障，缺锁的 confirmed 等于白确认。
    """
    bad = no_lock = 0
    if not os.path.isdir(OVL):
        print("没有覆盖层")
        return
    for d in _iter_overlays():
        locks = None          # 懒加载：该作品出现第一条 confirmed 才读锁文件
        for e in d["entries"]:
            rows = dict(lrc_rows(d["rj"], e["track"]))
            hit = any(abs(t - e["t0"]) < 0.02 for t in rows)
            if not hit:
                bad += 1
                print("❌ %s %s %.2f 在 LRC 里找不到对应时间轴（可能重跑过）" % (d["rj"], e["id"], e["t0"]))
            if e.get("status") == "confirmed":
                if locks is None:
                    locks = lock_keys(d["rj"])
                stem = e["track"][:-4] if e["track"].endswith(".lrc") else e["track"]
                if (stem, fmt_ts(e["t0"])) not in locks:
                    no_lock += 1
                    print("❌ %s %s 已 confirmed 但无 E1 锁（重跑会冲掉，先 lock_from_plan/review 登记）"
                          % (d["rj"], e["id"]))
    print("时间轴校验：%s（%d 条异常）" % ("✅ 全对得上" if not bad else "⚠️", bad))
    if no_lock:
        print("锁校验：⚠️ %d 条 confirmed 缺锁" % no_lock)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", dest="rj")
    ap.add_argument("--pending", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--add", metavar="PLAN",
                    help="从 fix 计划批量写入覆盖层（只记录不改 LRC；fix_lines_batch 同格式）")
    ap.add_argument("--confidence", default="low", help="--add 用（high/mid/low）")
    ap.add_argument("--status", default="pending_listen",
                    choices=["pending_listen", "confirmed", "rejected"], help="--add 用")
    ap.add_argument("--confirm", metavar="ID", help="听完确认（可选 --zh 覆盖最终译文）")
    ap.add_argument("--reject", metavar="ID", help="听完拒绝：original_zh 经 fixline 写回 LRC")
    ap.add_argument("--fill-original", dest="fill_original", action="store_true",
                    help="补 original_zh 空值（从 fixline 备份取最早一份；只补空不覆盖）")
    ap.add_argument("--zh", default="", help="--confirm 的可选译文覆盖")
    a = ap.parse_args()
    if a.check:
        check()
    elif a.add:
        cmd_add(a.add, a.confidence, a.status)
    elif a.fill_original:
        cmd_fill_original()
    elif a.confirm:
        cmd_confirm(a.confirm, a.zh or None)
    elif a.reject:
        cmd_reject(a.reject)
    elif a.pending:
        show(None, True)
    else:
        show(a.rj)


if __name__ == "__main__":
    main()
