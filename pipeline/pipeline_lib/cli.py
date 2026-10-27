# -*- coding: utf-8 -*-
"""命令行入口：`python pipeline.py <command>`。"""
import argparse
import contextlib
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

from . import fingerprint as FP
from . import knowledge, manifest, paths, queue, stages

PY = paths.python_exe()


# ---------------------------------------------------------------- 小工具
def _cfg(args):
    cfg = paths.load_config()
    for k in ("slots", "ext", "device", "compute", "engine", "system_mode",
              "asr_other_dir", "diarize", "allow_loss", "spk_prefix",
              "track_memory", "cue_pipeline"):
        v = getattr(args, k, None)
        if v not in (None, ""):
            cfg[k] = v
    profile = getattr(args, "profile", None) or "production"
    prof = cfg["profiles"].get(profile)
    if not prof:
        raise SystemExit("未知 profile：%s（可选 %s）"
                         % (profile, ", ".join(cfg["profiles"])))
    cfg["_profile"] = profile
    cfg["_gates"] = prof["gates"]
    cfg["_stages"] = prof["stages"]
    return cfg


def _pick_run(args):
    if getattr(args, "run", None):
        return manifest.Run.load(args.run)
    r = manifest.Run.latest(getattr(args, "project", None))
    if not r:
        raise SystemExit("没有可用 run：先 `pipeline run --project RJxxxxxxx --source <音频目录>`")
    return r


def _assert_rj(rj):
    if not re.match(r"^RJ\d{5,}$", rj or ""):
        raise SystemExit("作品号不合法：%r（必须形如 RJ1234567）" % (rj,))
    return rj


def _safe_track(track):
    """track 只允许文件名：挡住 `../../某作品/x.lrc` 这类路径穿越。"""
    t = (track or "").replace("\\", "/")
    if not t or t != os.path.basename(t) or t in (".", "..") or ".." in t:
        raise SystemExit("track 只能是文件名，不能含路径：%r" % (track,))
    return t


def _final_dir(run, rj):
    """最终产物目录：有 05_term_lock 就用它（05 是独立不可变目录），否则退回 04。"""
    d5 = os.path.join(run.dir, "05_term_lock", rj)
    if os.path.isdir(d5) and glob.glob(os.path.join(d5, "*.lrc")):
        return d5
    return os.path.join(run.dir, "04_polish", rj)


def _stamp_knowledge(run):
    """知识表归因：**创建时写一次，之后永不改写**。

    以前每次 run 都覆盖 run.data["knowledge"]，等于把旧 run 的「我用的是 v0001」
    改成「v0002」—— 历史成品的归因被悄悄改写。现在只追加 knowledge_at_resume。
    """
    reg = knowledge.ensure_registry()
    files = manifest.knowledge_now()
    snap = {"version": reg["current"], "agg": manifest.knowledge_agg(files), "files": files}
    created = run.data.get("knowledge_at_creation")
    if not created:
        run.data["knowledge_at_creation"] = snap
        run.data["knowledge"] = snap          # 兼容旧读取方，语义同 creation
        run.save()
        return
    if created.get("agg") == snap["agg"]:
        return
    changed = [fn for fn in files
               if ((created.get("files") or {}).get(fn) or {}).get("sha256")
               != files[fn]["sha256"]]
    run.data["knowledge_at_resume"] = snap
    run.data["knowledge_drift"] = changed
    run.save()
    print("⚠️ 生产知识表已变化（%s）→ 依赖它们的阶段会重跑；"
          "run 的创建时归因保持 %s 不变" % (", ".join(changed), created.get("version")))


def _stages_for(args, cfg):
    order = stages.ORDER
    if getattr(args, "only", None):
        names = [s.strip() for s in args.only.split(",") if s.strip()]
        bad = [n for n in names if n not in stages.STAGES]
        if bad:
            raise SystemExit("未知阶段：%s" % ",".join(bad))
        return names
    if getattr(args, "frm", None):
        if args.frm not in order:
            raise SystemExit("--from 需是 %s" % ",".join(order))
        return order[order.index(args.frm):]
    return [s for s in cfg["_stages"] if s in stages.STAGES]


def _lrc_rows(path):
    out = []
    if not os.path.exists(path):
        return out
    for ln in open(path, encoding="utf-8", errors="replace"):
        m = re.match(r"^\[(\d+):([\d.]+)\](.*)$", ln.rstrip("\n"))
        if m:
            out.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
    return out


def _line_at(rows, t, tol=1.0):
    """严格定位（repair 用）：±tol 秒内没有行就算失败，避免改错行。"""
    if not rows:
        return None
    i = min(range(len(rows)), key=lambda k: abs(rows[k][0] - t))
    return rows[i] if abs(rows[i][0] - t) <= tol else None


def _display_at(rows, t):
    """显示语义定位（explain 用）：t 时刻屏幕上那一行（LRC 行显示到下一条时间戳）。"""
    if not rows:
        return None
    if t < rows[0][0]:
        return rows[0]
    cur = rows[0]
    for r in rows:
        if r[0] <= t + 1e-6:
            cur = r
        else:
            break
    return cur


def _prod_path(rj, track):
    return os.path.join(paths.PROD, rj, track if track.endswith(".lrc") else track + ".lrc")


# ---------------------------------------------------------------- init / run
def cmd_init(args):
    cfg = _cfg(args)
    paths.ensure_dirs()
    run = manifest.Run.create(args.project, cfg["_profile"], cfg,
                              source=args.source, script_dir=args.script_dir,
                              note=args.note,
                              planned_stages=cfg["_stages"],
                              planned_gates=cfg["_gates"])
    _stamp_knowledge(run)
    print("已创建 run：%s" % run.run_id)
    print("目录：%s" % run.dir.replace("\\", "/"))
    print("阶段：%s" % " → ".join(cfg["_stages"]))
    print("门禁：%s" % ", ".join(cfg["_gates"]))
    print("\n下一步：pipeline run --run %s" % run.run_id)
    return 0


def cmd_run(args):
    cfg = _cfg(args)
    paths.ensure_dirs()
    if args.run:
        run = manifest.Run.load(args.run)
        if args.source:
            run.data["source"] = args.source.replace("\\", "/")
        if args.script_dir:
            run.data["script_dir"] = args.script_dir.replace("\\", "/")
        run.save()
    else:
        if not args.project or not args.source:
            raise SystemExit("新建 run 需要 --project 和 --source")
        run = manifest.Run.create(args.project, cfg["_profile"], cfg,
                                  source=args.source, script_dir=args.script_dir,
                                  note=args.note,
                                  planned_stages=cfg["_stages"],
                                  planned_gates=cfg["_gates"])
        print("新建 run：%s" % run.run_id)
    _stamp_knowledge(run)
    names = _stages_for(args, cfg)
    print("执行：%s（profile=%s，%s）"
          % (" → ".join(names), cfg["_profile"], "干跑" if args.dry_run else "真实执行"))
    with queue.FileLock(os.path.join(run.dir, ".run.lock"), "run:" + run.run_id):
        ok = stages.run_stages(run, cfg, names, force=args.force, dry=args.dry_run)
    print("\nrun %s：%s" % (run.run_id, "✅ 完成" if ok else "❌ 未完成（可 --from 续跑）"))
    return 0 if ok else 1


def cmd_check(args):
    cfg = _cfg(args)
    run = _pick_run(args)
    print("对 run %s 跑门禁（%s）" % (run.run_id, ", ".join(cfg["_gates"])))
    with queue.FileLock(os.path.join(run.dir, ".run.lock"), "check:" + run.run_id):
        ok = stages.execute(run, "06_checks", cfg, force=args.force)
    _refresh_status(run)
    gates = (run.stage("06_checks") or {}).get("gate") or {}
    _print_gates(gates)
    return 0 if ok else 1


def _refresh_status(run):
    """按**计划阶段**重算 run.status。

    以前只看「已存在的阶段是否都 ok」，于是「只跑了 06_checks 的 run」也会被判成 ok。
    现在：planned_stages 全部完成 + 硬门禁通过才是 ok；缺阶段 = incomplete。
    """
    st = run.data["stages"]
    planned = run.data.get("planned_stages") or [n for n in stages.ORDER if st.get(n)]
    if any((st.get(n) or {}).get("status") == "failed" for n in planned):
        run.set_status("failed")
        return
    if any((st.get(n) or {}).get("status") == "interrupted" for n in planned):
        run.set_status("interrupted")
        return
    if any((st.get(n) or {}).get("status") == "gate_failed" for n in planned):
        run.set_status("gate_failed")
        return
    missing = [n for n in planned
               if (st.get(n) or {}).get("status") not in ("ok", "imported")]
    if missing:
        started = any(st.get(n) for n in planned)
        run.set_status("incomplete" if started else "created")
        return
    g = (st.get("06_checks") or {}).get("gate") or {}
    run.set_status("ok" if (not g or g.get("ok")) else "gate_failed")


def _print_gates(gate):
    d = gate.get("detail") or {}
    if not d:
        return
    print("\n门禁结果：")
    for k, v in d.items():
        print("  %-18s %s  %-22s %s"
              % (k, "✅" if v["ok"] else ("❌" if v["hard"] else "⚠️"),
                 v.get("summary") or "", "硬门禁" if v["hard"] else "参考"))


# ---------------------------------------------------------------- status
def cmd_status(args):
    if not args.run:
        if not os.path.isdir(paths.RUNS):
            print("还没有任何 run")
            return 0
        ids = sorted([d for d in os.listdir(paths.RUNS)
                      if os.path.exists(os.path.join(paths.RUNS, d, "manifest.json"))])
        print("%-40s %-10s %-9s %-6s %-10s %s"
              % ("run_id", "project", "profile", "阶段", "门禁", "更新时间"))
        for i in ids:
            try:
                r = manifest.Run.load(i)
            except Exception:
                continue
            d = r.data
            done = sum(1 for n in stages.ORDER
                       if (d["stages"].get(n) or {}).get("status") in ("ok", "imported"))
            g = (d["stages"].get("06_checks") or {}).get("gate") or {}
            print("%-40s %-10s %-9s %d/%d     %-10s %s"
                  % (d["run_id"], d.get("project"), d.get("profile"), done,
                     len(stages.ORDER), "✅" if g.get("ok") else ("❌" if g else "—"),
                     d.get("updated_at")))
        print("\n（看单个 run 的阶段明细：pipeline status --run <run_id>）")
        return 0
    runs = [manifest.Run.load(args.run)]
    _refresh_status(runs[0])          # 修掉「阶段全 ok、run 还是 failed」的陈旧状态
    for r in runs:
        d = r.data
        print("\n%s  %s / %s  [%s]" % (d["run_id"], d.get("project"),
                                       d.get("profile"), d.get("status")))
        print("  创建 %s  更新 %s" % (d.get("created_at"), d.get("updated_at")))
        for name in stages.ORDER:
            st = d["stages"].get(name)
            if not st:
                print("  %-14s —" % name)
                continue
            g = st.get("gate") or {}
            print("  %-14s %-11s %6.1f 分钟  %s%s"
                  % (name, st.get("status"),
                     (st.get("duration_sec") or 0) / 60,
                     "门禁✅" if g.get("ok") else ("门禁❌" if g else ""),
                     "  ↻ %s" % st["cache_miss_reason"]
                     if st.get("cache_miss_reason") and st.get("status") == "ok" else ""))
            if st.get("error"):
                print("       错误：%s" % str(st["error"])[:160])
        if d.get("gates"):
            _print_gates({"detail": d["gates"]})
    return 0


# ---------------------------------------------------------------- promote
def cmd_promote(args):
    run = manifest.Run.load(args.run) if args.run else _pick_run(args)
    rj = _assert_rj(run.data["project"])
    src = _final_dir(run, rj)
    files = sorted(glob.glob(os.path.join(src, "*.lrc")))
    if not files:
        raise SystemExit("run 里没有成品 LRC：%s" % src)
    pol = (run.stage("04_polish") or {}).get("params") or {}
    if pol.get("allow_loss") and not args.force:
        raise SystemExit("这个 run 是 --allow-loss 放行过的（丢行硬门禁被绕过），"
                         "拒绝直接进生产。确认要推再 --force。")
    gates = (run.stage("06_checks") or {}).get("gate") or {}
    if not gates.get("ok") and not args.force:
        raise SystemExit("门禁未过，拒绝进生产（要硬推用 --force）：%s"
                         % json.dumps(gates, ensure_ascii=False)[:200])
    if args.dry_run:
        print("[dry] %d 个文件 %s → %s" % (len(files), src, os.path.join(paths.PROD, rj)))
        return 0

    dst = os.path.join(paths.PROD, rj)
    src_names = {os.path.basename(f) for f in files}
    with queue.FileLock(os.path.join(paths.TMP, "locks", "prod_%s.lock" % rj),
                        "promote:" + rj):
        os.makedirs(dst, exist_ok=True)
        # 文件集核对：生产里有、run 里没有 → 说明新 run 少产出一轨，
        # 静默留着就是「新旧字幕混在一起」，必须显式失败。
        extra = {os.path.basename(f) for f in glob.glob(os.path.join(dst, "*.lrc"))} - src_names
        if extra and not args.allow_extra:
            raise SystemExit(
                "生产目录里有本 run 未产出的 LRC（%d 个）：%s\n"
                "  直接推会留下旧文件，新旧混用。确认要保留请加 --allow-extra。"
                % (len(extra), ", ".join(sorted(extra)[:5])))
        bak = os.path.join(paths.TMP,
                           "lrc_backup_promote_%s" % time.strftime("%Y%m%d_%H%M%S"), rj)
        if args.backup:
            os.makedirs(bak, exist_ok=True)
            for f in glob.glob(os.path.join(dst, "*.lrc")):
                shutil.copy2(f, os.path.join(bak, os.path.basename(f)))
            print("备份 %d 个旧成品 → %s"
                  % (len(glob.glob(os.path.join(bak, "*.lrc"))), bak))

        # 逐文件「先写 .new 再 os.replace」：单个文件永远不会出现半截内容
        # （之前直接 copy2 覆盖，中断会留下写了一半的 LRC）。跨文件仍非原子，
        # 所以 promote 之后 06_checks 会重跑一遍兜底。
        changed = []
        for f in files:
            tgt = os.path.join(dst, os.path.basename(f))
            if manifest.sha256_file(f) != manifest.sha256_file(tgt):
                changed.append(os.path.basename(f))
            tmp = tgt + ".new"
            shutil.copy2(f, tmp)
            if manifest.sha256_file(tmp) != manifest.sha256_file(f):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise SystemExit("复制校验失败，已中止：%s" % os.path.basename(f))
            os.replace(tmp, tgt)
    rec = {"files": len(files), "changed": changed, "atomic": "per-file(.new→replace)",
           "backup": bak.replace("\\", "/"),
           "dest": dst.replace("\\", "/"), "source_dir": src.replace("\\", "/"),
           "extra_kept": sorted(extra), "at": manifest.now()}
    run.data["stages"]["07_promote"] = {
        "name": "07_promote", "status": "ok", "started_at": rec["at"],
        "duration_sec": 0, "outputs": [manifest.dir_rec(dst, "*.lrc")],
        "gate": {"ok": True}, "params": {"backup": args.backup,
                                         "allow_extra": bool(args.allow_extra)},
        "code": {}, "inputs": [manifest.dir_rec(src, "*.lrc")],
        "knowledge": manifest.knowledge_now(["glossary.json", "pin_sets.json"]),
        "extra": rec,
    }
    run.save()
    print("已进生产：%s（%d 个文件，改动 %d 个）" % (dst, len(files), len(changed)))
    for c in changed[:10]:
        print("  ~ %s" % c)
    return 0


# ---------------------------------------------------------------- repair
def cmd_repair(args):
    run = _pick_run(args)
    target = getattr(args, "target", "run") or "run"
    if target == "prod" and not getattr(args, "confirm_prod", False):
        raise SystemExit(
            "改生产成品是高风险动作：请显式加 --confirm-prod（默认 target=run 只改暂存）。\n"
            "  建议流程：repair --target run → 复跑门禁 → promote。")
    with contextlib.ExitStack() as stack:
        stack.enter_context(queue.FileLock(os.path.join(run.dir, ".run.lock"),
                                           "repair:" + run.run_id))
        if target == "prod":
            stack.enter_context(queue.FileLock(
                os.path.join(paths.TMP, "locks", "prod_%s.lock" % run.data["project"]),
                "repair-prod:" + run.data["project"]))
        return _repair_impl(args, run, target)


def _repair_impl(args, run, target):
    rj = _assert_rj(run.data["project"])
    plan = json.load(open(args.plan, encoding="utf-8"))
    items = plan if isinstance(plan, list) else [plan]
    base_dir = (_final_dir(run, rj) if target == "run" else os.path.join(paths.PROD, rj))
    print("修复计划：%d 条（目标=%s，%s）"
          % (len(items), "run 暂存" if target == "run" else "生产 LRC",
             "写盘" if args.apply else "干跑"))

    changes = []
    fail = 0
    for it in items:
        track = _safe_track(it.get("track") or "")
        start = float(it.get("start") or 0)
        zh = (it.get("zh") or "").strip()
        p = os.path.join(base_dir, track if track.endswith(".lrc") else track + ".lrc")
        row = _line_at(_lrc_rows(p), start)
        if not track or not zh or not row:
            print("  ❌ 跳过（缺 track/zh 或定位失败）：%s" % json.dumps(it, ensure_ascii=False)[:90])
            fail += 1
            continue
        old = row[1]
        if old == zh:
            print("  = 无变化：%s %.2fs" % (track, row[0]))
            continue
        changes.append({"track": os.path.basename(p), "t0": row[0], "old": old, "new": zh,
                        "why": it.get("why", ""), "ja": it.get("ja", "")})
        print("  %s [%02d:%05.2f]\n    旧：%s\n    新：%s"
              % (track, row[0] // 60, row[0] % 60, old, zh))
        if not args.apply:
            continue
        cmd = [PY, os.path.join(paths.SCRIPTS, "fixline.py"), "--rj", rj,
               "--track", os.path.splitext(track)[0], "--start", "%.2f" % row[0],
               "--zh", zh, "--apply"]
        if target == "run":
            # 暂存修复：显式把 lrc/asr 指到 run 里，别让 fixline 默认写生产
            asr_p = os.path.join(run.dir, "01_asr", rj, "asr",
                                 os.path.splitext(os.path.basename(p))[0] + ".json")
            cmd += ["--lrc", p]
            if os.path.exists(asr_p):
                cmd += ["--asr", asr_p]
        if it.get("ja"):
            cmd += ["--ja", it["ja"]]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        sys.stdout.write(r.stdout)
        if r.returncode != 0:
            print("  ❌ fixline 失败（%d）：%s" % (r.returncode, (r.stderr or "")[-200:]))
            fail += 1
            changes.pop()

    if not args.apply:
        print("\n干跑结束（加 --apply 写盘；写盘会经 fixline，自带备份 + 校验）")
        return 0

    # 写进 E1 锁：暂存修复如果不进锁，下一次重跑 04 就被模型输出冲掉（实测过）。
    # 锁里带 ja，重跑时间戳漂了也能按日文认回（见 lock_match.py）。
    if getattr(args, "lock", False) and changes:
        lp = os.path.join(paths.LOCKS, rj + ".json")
        os.makedirs(paths.LOCKS, exist_ok=True)
        lk = json.load(open(lp, encoding="utf-8")) if os.path.exists(lp) else {}
        for c in changes:
            m, sec = divmod(c["t0"], 60)
            key = "%s|%s" % (c["track"], "[%02d:%05.2f]" % (int(m), sec))
            lk[key] = {"zh": c["new"], "old": c.get("old") or "",
                       "ja": c.get("ja") or ""}
        json.dump(lk, open(lp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("已登记 E1 锁 %d 条（重跑不会再被冲掉）→ %s" % (len(changes), lp))

    # 覆盖层只记录（不改 LRC）——fixline 已经改了
    if args.overlay and target == "run":
        print("⚠️ --overlay 只对生产成品有意义（覆盖层按作品号 + 生产 LRC 定位），"
              "本次 target=run 已跳过")
        args.overlay = False
    if args.overlay:
        r = subprocess.run([PY, os.path.join(paths.SCRIPTS, "repair_overlay.py"),
                            "--add", args.plan, "--confidence", args.confidence],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        sys.stdout.write(r.stdout)

    # 暂存修复会改掉「已记录输出」的那份文件 —— 不更新记录，manifest 就在撒谎。
    # 记到 outputs_after_repair，原 outputs 保留（那是模型刚跑完的样子）。
    for sname, sdir in (("04_polish", os.path.join(run.dir, "04_polish", rj)),
                        ("05_term_lock", os.path.join(run.dir, "05_term_lock", rj))):
        if os.path.abspath(base_dir) == os.path.abspath(sdir):
            rec = run.data["stages"].setdefault(sname, {"name": sname})
            rec["outputs_after_repair"] = [manifest.dir_rec(sdir, "*.lrc")]
            rec["modified_by_repair"] = {"at": manifest.now(), "n": len(changes)}
    st = {"name": "07_repair", "status": "ok" if not fail else "partial",
          "started_at": manifest.now(), "duration_sec": 0, "changes": changes,
          "gate": {"ok": not fail}, "params": {"plan": os.path.basename(args.plan),
                                               "target": target},
          "code": {"fixline.py": manifest.sha256_file(
              os.path.join(paths.SCRIPTS, "fixline.py"))},
          "inputs": [manifest.file_rec(args.plan)],
          "knowledge": manifest.knowledge_now(["asr_errata.json", "glossary.json"]),
          "outputs": [manifest.dir_rec(base_dir, "*.lrc")]}
    run.data["stages"].setdefault("07_repair", {}).update(st)
    run.data.setdefault("repairs", []).append(
        {"at": st["started_at"], "n": len(changes), "plan": args.plan.replace("\\", "/")})
    run.save()
    print("\n修复 %d 行，失败 %d 条；已记入 manifest（07_repair）" % (len(changes), fail))
    if args.recheck:
        print("\n重跑门禁：")
        cfg = paths.load_config()
        cfg["_gates"] = cfg["profiles"]["production"]["gates"]
        stages.execute(run, "06_checks", cfg, force=True)
        _refresh_status(run)
        _print_gates((run.stage("06_checks") or {}).get("gate") or {})
    return 0 if not fail else 1


# ---------------------------------------------------------------- explain
def cmd_explain(args):
    run = _pick_run(args)
    rj = run.data["project"]
    track = args.track if args.track.endswith(".lrc") else args.track + ".lrc"
    t = float(args.start)

    stage_lrc = os.path.join(_final_dir(run, rj), track)
    prod_lrc = _prod_path(rj, track)
    a = _display_at(_lrc_rows(stage_lrc), t)
    b = _display_at(_lrc_rows(prod_lrc), t)

    print("run %s / %s / %s @ %.2fs" % (run.run_id, rj, track, t))
    kv = (run.data.get("knowledge") or {}).get("version") or "(未登记)"
    print("知识表版本：%s" % kv)

    ja = ""
    for d in ("02_asr_fix", "01_asr"):
        cands = [os.path.join(run.dir, d, rj, "asr_fix", track[:-4] + ".json"),
                 os.path.join(run.dir, d, rj, "asr", track[:-4] + ".json"),
                 os.path.join(run.dir, d, rj, "asr", track[:-4] + ".mp3.json")]
        for c in cands:
            if os.path.exists(c):
                segs = (json.load(open(c, encoding="utf-8")) or {}).get("segments") or []
                near = None
                for s in segs:
                    if float(s.get("start", 0)) <= t + 0.05:
                        near = s
                    else:
                        break
                if near is None and segs:
                    near = segs[0]
                if near:
                    ja = near.get("text", "")
                    print("ASR 源（%s @%.2fs）：%s" % (d, float(near.get("start", 0)), ja))
                    if near.get("fix_log"):
                        print("  改错记录：%s" % json.dumps(near["fix_log"],
                                                         ensure_ascii=False)[:200])
                break
        if ja:
            break

    print("润色产物：%s" % (a[1] if a else "（该时刻无行）"))
    print("生产成品：%s" % (b[1] if b else "（该时刻无行）"))
    if a and b and a[1] != b[1]:
        if run.stage("07_promote"):
            print("⚠️ 生产与 run 不一致 → promote 之后又被 repair / 手工改动改过")
        else:
            print("ℹ️ 本 run 尚未 promote，生产目录里仍是旧成品（不是被改动过）")

    locks = os.path.join(paths.LOCKS, rj + ".json")
    if os.path.exists(locks):
        d = json.load(open(locks, encoding="utf-8"))
        stem = track[:-4]
        hit = [(k, v) for k, v in d.items()
               if k.startswith(stem + "|") and
               abs(_ts_sec(k.split("|", 1)[1]) - t) <= 1.0]
        for k, v in hit:
            print("E1 锁：%s → %s" % (k, (v.get("zh") if isinstance(v, dict) else v)))

    ovl = os.path.join(paths.OVERLAY, rj + ".json")
    if os.path.exists(ovl):
        for e in (json.load(open(ovl, encoding="utf-8")).get("entries") or []):
            if e.get("track") == track and abs(float(e.get("t0", 0)) - t) <= 1.0:
                print("覆盖层：%s [%s/%s] %s"
                      % (e["id"], e.get("confidence"), e.get("status"),
                         e.get("evidence", "")[:120]))
                print("  改前：%s\n  改后：%s" % (e.get("original_zh"), e.get("proposed_zh")))

    for rep in (run.data.get("repairs") or []):
        pass
    st = run.stage("07_repair") or {}
    for c in st.get("changes") or []:
        if c["track"] == track and abs(c["t0"] - t) <= 1.0:
            print("run 内修复记录（%s）：%s → %s（%s）"
                  % (st.get("started_at"), c["old"], c["new"], c.get("why")))

    scan = os.path.join(run.dir, "06_checks", "scan", "violations.jsonl")
    if os.path.exists(scan):
        for ln in open(scan, encoding="utf-8"):
            try:
                v = json.loads(ln)
            except Exception:
                continue
            if v.get("track", "").startswith(track[:-4]) and abs(float(v.get("start", 0)) - t) <= 1.0:
                print("门禁告警：%s（%s）%s" % (v.get("rule"), v.get("severity"),
                                              v.get("detail", "")[:100]))
    return 0


def _ts_sec(ts):
    m = re.match(r"\[(\d+):([\d.]+)\]", ts.strip())
    return int(m.group(1)) * 60 + float(m.group(2)) if m else -1


# ---------------------------------------------------------------- adopt
def _pick_asr_dir(rj, prod):
    """选与成品时间轴最贴合的 ASR 版本（坑 90：16 部里 12 部对齐 v1，别写死 asr/）。"""
    draft = os.path.join(paths.DRAFT, rj)
    best, best_rate, rates = None, -1.0, {}
    for sub in ("asr_v1_20261020", "asr"):
        d = os.path.join(draft, sub)
        if not os.path.isdir(d):
            continue
        hit = tot = 0
        for lrc in glob.glob(os.path.join(prod, "*.lrc")):
            stem = os.path.basename(lrc)[:-4]
            jp = os.path.join(d, stem + ".json")
            if not os.path.exists(jp):
                jp = os.path.join(d, stem + ".mp3.json")
            if not os.path.exists(jp):
                continue
            try:
                segs = (json.load(open(jp, encoding="utf-8")) or {}).get("segments") or []
            except Exception:
                continue
            starts = [round(float(s.get("start", 0)), 2) for s in segs]
            for t, _ in _lrc_rows(lrc):
                tot += 1
                if any(abs(t - s) <= 0.05 for s in starts):
                    hit += 1
        rate = (hit / tot) if tot else 0.0
        rates[sub] = round(rate, 4)
        if rate > best_rate:
            best, best_rate = d, rate
    return best, rates


def cmd_adopt(args):
    """把已交付的作品接进 runs/，立刻获得可追溯基线（不重跑、不改成品）。"""
    cfg = _cfg(args)
    paths.ensure_dirs()
    if getattr(args, "all", False):
        targets = [d for d in sorted(os.listdir(paths.PROD))
                   if os.path.isdir(os.path.join(paths.PROD, d))]
        done = set()
        if os.path.isdir(paths.RUNS):
            for rid in os.listdir(paths.RUNS):
                m = os.path.join(paths.RUNS, rid, "manifest.json")
                if os.path.exists(m):
                    try:
                        d = json.load(open(m, encoding="utf-8"))
                    except Exception:
                        continue
                    if d.get("profile") == "adopted":
                        done.add(d.get("project"))
        todo = [t for t in targets if t not in done]
        print("接管：共 %d 部，已接管 %d 部跳过，本次 %d 部"
              % (len(targets), len(done), len(todo)))
        bad = []
        for rj in todo:
            try:
                _adopt_one(rj, args, cfg)
            except SystemExit as e:
                bad.append((rj, str(e)))
                print("  ❌ %s：%s" % (rj, e))
        print("\n接管完成：%d 部成功，%d 部失败%s"
              % (len(todo) - len(bad), len(bad),
                 ("（%s）" % ", ".join(b[0] for b in bad)) if bad else ""))
        return 0
    return _adopt_one(args.project, args, cfg)


def _adopt_one(rj, args, cfg):
    draft = os.path.join(paths.DRAFT, rj)
    prod = os.path.join(paths.PROD, rj)
    if not os.path.isdir(prod):
        raise SystemExit("没有成品目录：%s" % prod)
    run = manifest.Run.create(rj, "adopted", cfg, source=args.source,
                              script_dir=args.script_dir,
                              note=args.note or "从既有产物接管（未重跑）",
                              planned_stages=["01_asr", "02_asr_fix", "03_translate",
                                              "04_polish"],
                              planned_gates=["check", "scan", "errata_audit"])
    _stamp_knowledge(run)

    def copy_stage(stage, sub, src, pattern="*.json", label=""):
        dst = os.path.join(run.dir, stage, rj, sub)
        os.makedirs(dst, exist_ok=True)
        files = sorted(glob.glob(os.path.join(src, pattern))) if src and os.path.isdir(src) else []
        for f in files:
            shutil.copy2(f, os.path.join(dst, os.path.basename(f)))
        print("  %-12s %-10s ← %s（%d 个文件）"
              % (stage, sub, src or "（无）", len(files)))
        return dst if files else None

    print("接管 %s：" % rj)
    asr_src, rates = _pick_asr_dir(rj, prod)
    print("  ASR 版本匹配率：%s → 选 %s"
          % (rates, os.path.basename(asr_src) if asr_src else "（无）"))
    run.data["asr_version"] = os.path.basename(asr_src) if asr_src else None
    run.data["asr_match_rates"] = rates
    run.save()
    copy_stage("01_asr", "asr", asr_src)
    copy_stage("02_asr_fix", "asr_fix", os.path.join(draft, "asr_fix"))
    copy_stage("03_translate", "draft", os.path.join(draft, "draft"), "*.lrc")
    copy_stage("04_polish", "", prod, "*.lrc")

    for name in ("01_asr", "02_asr_fix", "03_translate", "04_polish"):
        spec = stages.STAGES[name]
        ctx = stages.Ctx(run, cfg)
        try:
            params = spec["params"](ctx)
            inputs = spec["inputs"](ctx)
        except Exception as e:
            params, inputs = {"error": str(e)}, []
        know = manifest.knowledge_now(spec["knowledge"])
        code = stages.code_rec(spec["code"])
        models = FP.model_records(cfg, name)
        fp = FP.stage_fingerprint(name, params, inputs, know, code, cfg, models=models)
        run.begin_stage(name, params, inputs, know, code,
                        cache_miss_reason="接管既有产物",
                        extra={"config_hash": FP.config_hash(cfg),
                               "env": FP.env_whitelist(cfg), "models": models})
        run.data["stages"][name].update({
            "status": "imported", "started_at": run.data["created_at"],
            "ended_at": manifest.now(), "duration_sec": 0,
            "outputs": [manifest.dir_rec(os.path.join(run.dir, name, rj, s), "*.*")
                        for s in ("asr", "asr_fix", "draft", "")],
            "gate": {"ok": True, "imported": True},
        })
        run.data["stages"][name]["fingerprint"] = fp
        run.save()

    run.set_status("imported")
    print("\n已接管 → %s" % run.run_id)
    print("下一步：pipeline check --run %s（只读门禁，确认基线）" % run.run_id)
    return 0


# ---------------------------------------------------------------- knowledge
def cmd_knowledge(args):
    act = args.action
    if act == "status":
        knowledge.status()
    elif act == "snapshot":
        knowledge.snapshot(args.note or "")
    elif act == "diff":
        knowledge.diff(args.v1, args.v2)
    elif act == "propose":
        knowledge.propose(args.file)
    elif act == "validate":
        ok = knowledge.validate(args.id)
        return 0 if ok else 1
    elif act == "approve":
        knowledge.approve(args.id, by=args.by, confirm=args.confirm)
    elif act == "reject":
        knowledge.reject(args.id, args.reason or "")
    elif act == "list":
        knowledge.list_proposals(show_all=args.all)
    else:
        raise SystemExit("未知子命令")
    return 0


# ---------------------------------------------------------------- queue
def cmd_queue(args):
    if args.action == "add":
        queue.add(args.argv, gpu=not args.cpu, note=args.note or "")
    elif args.action == "run":
        queue.run_queue(limit=args.limit)
    elif args.action == "status":
        queue.status()
    else:
        raise SystemExit("未知子命令")
    return 0


def cmd_version(args):
    v = "unknown"
    try:
        v = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "VERSION"), encoding="utf-8").read().strip()
    except OSError:
        pass
    print("kikoeru-pipeline %s" % v)
    print("代码：%s" % os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print("说明：docs/rules/PIPELINE.md · 变更：pipeline/CHANGELOG.md")
    return 0


def cmd_selftest(args):
    from . import selftest
    return selftest.main()


# ---------------------------------------------------------------- 入口
def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        prog="pipeline",
        description="kikoeru 确定性字幕管线（软件层；Agent 只做监督与例外处理）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("--project", help="RJ 号")
        p.add_argument("--run", help="已存在的 run_id")
        p.add_argument("--profile", default="production",
                       help="production / fast / checks")
        p.add_argument("--slots", type=int, help="llama-server 并行槽（1=可复现，2=默认）")
        p.add_argument("--ext", help="音频扩展名 auto/mp3/wav")
        p.add_argument("--device", help="cuda/cpu")
        p.add_argument("--compute", help="float16/int8")
        p.add_argument("--engine", help="初译引擎（hymt/qwen/sakura）")
        p.add_argument("--system-mode", dest="system_mode", help="润色强度 strong/weak")
        p.add_argument("--asr-other-dir", dest="asr_other_dir",
                       help="第二版 ASR 目录（启用绿灯自动落地）")
        p.add_argument("--diarize", action="store_true", help="ASR 后跑声纹聚类")

    p = sub.add_parser("init", help="只创建 run（不执行）")
    add_common(p)
    p.add_argument("--source", required=True, help="音频目录")
    p.add_argument("--script-dir", default="", help="台本目录（有则走打轴）")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("run", help="执行阶段（幂等、可续跑）")
    add_common(p)
    p.add_argument("--source", help="音频目录（新建 run 时必填）")
    p.add_argument("--script-dir", default="", help="台本目录")
    p.add_argument("--note", default="")
    p.add_argument("--from", dest="frm", help="从某阶段开始（含）")
    p.add_argument("--only", help="只跑这些阶段，逗号分隔")
    p.add_argument("--force", action="store_true", help="忽略缓存强制重跑")
    p.add_argument("--allow-loss", dest="allow_loss", action="store_true",
                   help="放行润色的丢行硬门禁（会记进 manifest，promote 时需再 --force）")
    p.add_argument("--dry-run", action="store_true", help="只打印命令")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("check", help="只跑门禁（06_checks）")
    add_common(p)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("status", help="看 run 与阶段状态")
    add_common(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("promote", help="把 run 的成品推进生产目录")
    add_common(p)
    p.add_argument("--force", action="store_true", help="门禁未过也推（不建议）")
    p.add_argument("--no-backup", dest="backup", action="store_false", default=True)
    p.add_argument("--allow-extra", dest="allow_extra", action="store_true",
                   help="生产里有多余的旧 LRC 也照推（默认拒绝，防新旧混用）")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("repair", help="按计划修成品（走 fixline，自带备份）")
    add_common(p)
    p.add_argument("--plan", required=True, help="repairs.json")
    p.add_argument("--apply", action="store_true", help="写盘（默认干跑）")
    p.add_argument("--target", default="run", choices=["prod", "run"],
                   help="run=只改本 run 的暂存产物（默认）；prod=改生产成品（需 --confirm-prod）")
    p.add_argument("--confirm-prod", dest="confirm_prod", action="store_true",
                   help="确认要直接改生产成品（高风险）")
    p.add_argument("--lock", action="store_true",
                   help="把修好的行写进 E1 锁（带日文），重跑该阶段不会冲掉")
    p.add_argument("--overlay", action="store_true", help="同时登记修复覆盖层")
    p.add_argument("--confidence", default="low")
    p.add_argument("--recheck", action="store_true", help="修完重跑门禁")
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("explain", help="这句为何被改（provenance）")
    add_common(p)
    p.add_argument("--track", required=True)
    p.add_argument("--start", type=float, required=True)
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("adopt", help="把既有成品接管进 runs/（不重跑）")
    add_common(p)
    p.add_argument("--all", action="store_true",
                   help="接管 kikoerusubtitle/ 下全部作品（已接管的跳过）")
    p.add_argument("--source", default=None)
    p.add_argument("--script-dir", default="")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_adopt)

    p = sub.add_parser("knowledge", help="知识表版本与提案")
    ks = p.add_subparsers(dest="action", required=True)
    ks.add_parser("status")
    k = ks.add_parser("snapshot")
    k.add_argument("--note", default="")
    k = ks.add_parser("diff")
    k.add_argument("--from", dest="v1", default=None)
    k.add_argument("--to", dest="v2", default=None)
    k = ks.add_parser("propose")
    k.add_argument("file")
    k = ks.add_parser("validate")
    k.add_argument("id")
    k = ks.add_parser("approve")
    k.add_argument("id")
    k.add_argument("--by", default="human")
    k.add_argument("--confirm", action="store_true")
    k = ks.add_parser("reject")
    k.add_argument("id")
    k.add_argument("--reason", default="")
    k = ks.add_parser("list")
    k.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_knowledge)

    p = sub.add_parser("version", help="版本号")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("selftest", help="管线自检（P0 断言，沙箱内跑）")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("queue", help="GPU 串行队列")
    qs = p.add_subparsers(dest="action", required=True)
    q = qs.add_parser("add")
    q.add_argument("--cpu", action="store_true")
    q.add_argument("--note", default="")
    q.add_argument("argv", nargs=argparse.REMAINDER)
    q = qs.add_parser("run")
    q.add_argument("--limit", type=int, default=0)
    qs.add_parser("status")
    p.set_defaults(func=cmd_queue)

    args = ap.parse_args(argv)
    try:
        return args.func(args) or 0
    except BrokenPipeError:
        return 0
    except OSError as e:
        # Windows 上「管道被 head 截断」报 EINVAL(22) 而不是 EPIPE(32)
        if getattr(e, "errno", None) in (22, 32):
            return 0
        raise
