# -*- coding: utf-8 -*-
"""ASR 暂存区：把一次运行的 ASR 原文 / 修后原文 / 成品字幕留下来，供反馈日志用。

为什么不直接读 `runs/<id>/`：
`runs/` 是执行沙盒，随时可以清（`work/README.md` §4 明写「能删」）。
用户「暂存」的语义是**我要留着它**，所以必须复制到一个不会被清理的地方。
"""
import glob
import json
import os
import re
import shutil
import time

from . import settings

LRC_RE = re.compile(r"^\[(\d+):([\d.]+)\](.*)$")


def store_dir(project, run_id):
    return os.path.join(settings.load_app()["asr_cache_dir"], project, run_id)


def _seg_lines(path):
    """ASR json → [(秒, 文本)]。"""
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return []
    out = []
    for s in (d.get("segments") or []):
        txt = (s.get("text") or "").strip()
        if txt:                     # 空段对反馈没有意义
            out.append((float(s.get("start") or 0), txt))
    return out


def _lrc_lines(path):
    out = []
    try:
        for ln in open(path, encoding="utf-8", errors="replace"):
            m = LRC_RE.match(ln.rstrip("\n"))
            if m and m.group(3).strip():    # 清屏 spacer（空文本行）不进反馈日志
                out.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
    except Exception:
        pass
    return out


def save_from_run(run_id, project, source=None):
    """从 run 目录复制三份文本到暂存区，返回 store 路径。"""
    run_dir = os.path.join(settings.ROOT, "runs", run_id)
    if not os.path.isdir(run_dir):
        raise ValueError("找不到 run：%s" % run_dir)

    asr_d = os.path.join(run_dir, "01_asr", project, "asr")
    fix_d = os.path.join(run_dir, "02_asr_fix", project, "asr_fix")
    lrc_d = os.path.join(run_dir, "05_term_lock", project)
    if not glob.glob(os.path.join(lrc_d, "*.lrc")):
        lrc_d = os.path.join(run_dir, "04_polish", project)

    dst = store_dir(project, run_id)
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)

    tracks = []
    for f in sorted(glob.glob(os.path.join(asr_d, "*.json"))):
        name = os.path.basename(f)[:-5]
        rec = {"name": name, "asr": 0, "fix": 0, "lrc": 0}
        for kind, srcdir, ext in (("asr", asr_d, ".json"), ("fix", fix_d, ".json"),
                                  ("lrc", lrc_d, ".lrc")):
            s = os.path.join(srcdir, name + ext)
            if not os.path.exists(s):
                continue
            d = os.path.join(dst, kind)
            os.makedirs(d, exist_ok=True)
            shutil.copy2(s, os.path.join(d, name + ext))
            if kind == "lrc":
                rec["lrc"] = 1
            else:
                rec[kind] = len(_seg_lines(s))
        tracks.append(rec)

    meta = {
        "project": project,
        "run_id": run_id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source or "",
        "tracks": tracks,
    }
    with open(os.path.join(dst, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return dst


def list_stores():
    root = settings.load_app()["asr_cache_dir"]
    out = []
    for meta in sorted(glob.glob(os.path.join(glob.escape(root), "*", "*", "meta.json")),
                       reverse=True):
        try:
            d = json.load(open(meta, encoding="utf-8"))
        except Exception:
            continue
        path = os.path.dirname(meta)
        size = sum(os.path.getsize(os.path.join(dp, f))
                   for dp, _, fs in os.walk(path) for f in fs)
        out.append({
            "project": d.get("project") or os.path.basename(os.path.dirname(path)),
            "run_id": d.get("run_id") or os.path.basename(path),
            "created_at": d.get("created_at") or "",
            "tracks": d.get("tracks") or [],
            "path": path,
            "size": size,
            "source": d.get("source") or "",
        })
    return out


def delete_store(path):
    shutil.rmtree(path, ignore_errors=True)
    # 顺手清掉空的项目目录
    parent = os.path.dirname(path)
    try:
        if os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
    except Exception:
        pass


def track_text(path, kind, track):
    """返回 [(秒, 文本)]；kind ∈ asr / fix / lrc。"""
    ext = ".lrc" if kind == "lrc" else ".json"
    p = os.path.join(path, kind, track + ext)
    if not os.path.exists(p):
        return []
    return _lrc_lines(p) if kind == "lrc" else _seg_lines(p)


def purge_run_asr(run_id, project):
    """「不暂存」→ 删掉 run 里的 ASR 中间产物（保留成品与 manifest）。

    这是**破坏性**操作：删完这个 run 无法续跑 02/03，也再也不能生成反馈日志。
    调用方必须先明确确认。
    """
    run_dir = os.path.join(settings.ROOT, "runs", run_id)
    removed = 0
    for d in (os.path.join(run_dir, "01_asr", project, "asr"),
              os.path.join(run_dir, "01_asr", project, "asr_raw"),
              os.path.join(run_dir, "02_asr_fix", project, "asr_fix")):
        if os.path.isdir(d):
            removed += len(glob.glob(os.path.join(d, "*.json")))
            shutil.rmtree(d, ignore_errors=True)
    mp = os.path.join(run_dir, "manifest.json")
    try:
        d = json.load(open(mp, encoding="utf-8"))
        d["asr_purged"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "files_removed": removed,
                           "why": "用户在 OnseiCraft 完成页选择不暂存 ASR"}
        json.dump(d, open(mp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    return removed
