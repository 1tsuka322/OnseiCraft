# -*- coding: utf-8 -*-
"""run 目录 + manifest.json：可追溯与断点续跑的唯一依据。

manifest 回答四个问题：
1. 成品由哪版知识表/哪版代码产生 → `knowledge` + 每阶段的 `code` 哈希
2. 这句为何被改 → `stages.05_repair.changes` / `stages.04_polish`
3. 改一个 guard 是否导致回归 → 阶段指纹（输入+参数+知识+代码）变了才重跑
4. 失败从哪续 → 每阶段独立 `status`，`run` 只重跑指纹不符/失败的那一段
"""
import hashlib
import json
import os
import platform
import socket
import subprocess
import time

from . import paths

SCHEMA = 1


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def sha256_file(p, block=1 << 20):
    if not os.path.exists(p) or not os.path.isfile(p):
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(block)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def file_rec(p):
    """文件指纹记录（相对 ROOT 的路径便于跨机阅读）。"""
    ap = os.path.abspath(p)
    try:
        rel = os.path.relpath(ap, paths.ROOT).replace("\\", "/")
    except ValueError:
        rel = ap.replace("\\", "/")
    return {"path": rel, "sha256": sha256_file(ap),
            "bytes": os.path.getsize(ap) if os.path.isfile(ap) else None}


def dir_rec(d, pattern="*"):
    """目录指纹：逐文件哈希 + 汇总哈希（顺序敏感，稳定排序）。"""
    import glob
    files = sorted(glob.glob(os.path.join(d, pattern)))
    files = [f for f in files if os.path.isfile(f)]
    recs = [file_rec(f) for f in files]
    agg = sha256_text("".join("%s:%s" % (r["path"], r["sha256"]) for r in recs))
    return {"path": os.path.relpath(d, paths.ROOT).replace("\\", "/"),
            "n_files": len(recs), "agg_sha256": agg, "files": recs}


def dir_agg(d, pattern="*"):
    import glob
    files = sorted(glob.glob(os.path.join(d, pattern)))
    files = [f for f in files if os.path.isfile(f)]
    return sha256_text("".join("%s:%s" % (os.path.basename(f), sha256_file(f))
                               for f in files))


def env_rec(cfg):
    py = paths.python_exe(cfg)
    rec = {
        "python": py.replace("\\", "/"),
        "platform": platform.platform(),
        "host": socket.gethostname(),
        "cwd": os.getcwd().replace("\\", "/"),
        "device": cfg.get("device"),
        "compute": cfg.get("compute"),
    }
    try:
        rec["python_version"] = subprocess.run(
            [py, "-c", "import sys;print(sys.version.split()[0])"],
            capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        rec["python_version"] = None
    return rec


class Run(object):
    def __init__(self, run_id, data=None):
        self.run_id = run_id
        self.dir = os.path.join(paths.RUNS, run_id)
        self.path = os.path.join(self.dir, "manifest.json")
        self.data = data or {}

    # ---- 生命周期 ----
    @classmethod
    def create(cls, project, profile, cfg, source=None, script_dir=None, note="",
               planned_stages=None, planned_gates=None):
        rid = "%s_%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), project, profile)
        r = cls(rid)
        os.makedirs(r.dir, exist_ok=False)
        r.data = {
            "schema": SCHEMA,
            "run_id": rid,
            "project": project,
            "profile": profile,
            "note": note,
            "created_at": now(),
            "updated_at": now(),
            "status": "created",
            "source": source.replace("\\", "/") if source else None,
            "script_dir": script_dir.replace("\\", "/") if script_dir else None,
            "root": paths.ROOT.replace("\\", "/"),
            "env": env_rec(cfg),
            "config": {k: v for k, v in cfg.items() if k != "profiles"},
            # 计划执行的阶段/门禁在创建时固化：只有「计划内的阶段全部完成 + 硬门禁通过」
            # 才算 ok。否则「只跑了 06_checks 的 run」也会被 _refresh_status 判成 ok。
            "planned_stages": list(planned_stages or []),
            "planned_gates": list(planned_gates or []),
            # 知识表：创建时写一次，之后**永不改写**（历史 run 的归因不能被新版本覆盖）。
            "knowledge_at_creation": None,
            "knowledge_at_resume": None,
            "knowledge": {},
            "stages": {},
            "gates": {},
        }
        r.save()
        return r

    @classmethod
    def load(cls, run_id):
        r = cls(run_id)
        if not os.path.exists(r.path):
            raise SystemExit("找不到 run：%s（看 `pipeline status` 列已存在的 run）" % r.path)
        r.data = json.load(open(r.path, encoding="utf-8"))
        # 兼容旧 manifest：新字段缺了就按现有内容补，不伪造计划
        r.data.setdefault("planned_stages", [n for n in
                                             ("01_asr", "02_asr_fix", "03_translate",
                                              "04_polish", "05_term_lock", "06_checks")
                                             if n in r.data.get("stages", {})])
        r.data.setdefault("planned_gates", [])
        r.data.setdefault("knowledge_at_creation", r.data.get("knowledge") or None)
        r.data.setdefault("knowledge_at_resume", None)
        return r

    @classmethod
    def latest(cls, project=None):
        if not os.path.isdir(paths.RUNS):
            return None
        ids = [d for d in os.listdir(paths.RUNS)
               if os.path.exists(os.path.join(paths.RUNS, d, "manifest.json"))]
        if project:
            ids = [d for d in ids if ("_%s_" % project) in d]
        if not ids:
            return None
        return cls.load(sorted(ids)[-1])

    def save(self):
        self.data["updated_at"] = now()
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ---- 目录 ----
    def stage_dir(self, name):
        d = os.path.join(self.dir, name)
        os.makedirs(d, exist_ok=True)
        return d

    def path_of(self, *parts):
        return os.path.join(self.dir, *parts)

    # ---- 阶段记录 ----
    def stage(self, name):
        return self.data["stages"].get(name)

    def begin_stage(self, name, params, inputs, knowledge, code,
                    cache_miss_reason="", extra=None):
        fp = fingerprint(name, params, inputs, knowledge, code)
        st = {
            "name": name,
            "status": "running",
            "started_at": now(),
            "duration_sec": None,
            "cache": "miss",
            "cache_miss_reason": cache_miss_reason,
            "fingerprint": fp,
            "params": params,
            "code": code,
            "inputs": inputs,
            "knowledge": knowledge,
            "outputs": [],
            "gate": None,
            "log": None,
        }
        if extra:
            st.update(extra)
        self.data["stages"][name] = st
        self.save()
        return st

    def finish_stage(self, name, outputs=None, gate=None, status="ok", extra=None):
        st = self.data["stages"][name]
        st["status"] = status
        st["ended_at"] = now()
        try:
            st["duration_sec"] = round(
                time.mktime(time.strptime(st["ended_at"], "%Y-%m-%dT%H:%M:%S"))
                - time.mktime(time.strptime(st["started_at"], "%Y-%m-%dT%H:%M:%S")), 1)
        except Exception:
            pass
        if outputs is not None:
            st["outputs"] = outputs
        if gate is not None:
            st["gate"] = gate
        if extra:
            st.update(extra)
        self.save()

    def set_status(self, status):
        self.data["status"] = status
        self.save()


def fingerprint(name, params, inputs, knowledge, code):
    """阶段指纹：参数 + 输入哈希 + 相关知识表哈希 + 代码哈希。

    **代码也进指纹**是刻意的：本项目最大的坑就是「规则/脚本悄悄改了，
    旧产物看起来没变」。改了 guard 就必然换指纹 → 该阶段重跑，回归可见。
    """
    payload = {
        "stage": name,
        "params": params,
        "inputs": inputs,
        "knowledge": knowledge,
        "code": code,
    }
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def knowledge_now(names=None):
    """当前生产知识表的哈希（names 为文件名列表；None = 全部登记文件）。"""
    out = {}
    for fn, role, desc in paths.KNOWLEDGE_FILES:
        if names is not None and fn not in names:
            continue
        p = os.path.join(paths.STYLE, fn)
        out[fn] = {"role": role, "desc": desc, "sha256": sha256_file(p),
                   "exists": os.path.exists(p)}
    return out


def knowledge_agg(rec):
    return sha256_text(json.dumps(
        {k: v.get("sha256") for k, v in sorted((rec or {}).items())},
        sort_keys=True))
