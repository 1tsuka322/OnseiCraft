# -*- coding: utf-8 -*-
"""管线自检（`pipeline selftest`）—— 针对外部评审指出的 P0 做**可复跑的断言**。

全部在 `tmp/selftest_*/` 沙箱里跑（运行时改 `paths.*` 指向沙箱），
不碰生产表、不碰 `runs/`、不碰 `kikoerusubtitle/`。

每条断言都对应一个真实故障模式：
  S1 门禁失败必须让 run_stages 返回 False 且状态是 gate_failed（不能显示 ok）
  S2 只跑了一部分的 run 不能是 ok（planned_stages 语义）
  S3 知识表归因：创建时快照不可被后续 resume 改写
  S4 promote 拒绝「生产里有本 run 未产出的 LRC」（防新旧混用）
  S5 锁：释放只删自己的 token；PID 存活时即使锁很旧也不抢
  S6 05_term_lock 必须是独立目录（04 的产物不可变）
  S7 阶段 `code` 列表必须覆盖脚本实际 import 的同目录模块（防「改了不重算」）
"""
import json
import os
import re
import shutil
import sys
import time

from . import cli, manifest, paths, queue, stages

OK = []
BAD = []


def ck(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("  %s %-46s %s" % ("✅" if cond else "❌", name, detail))


def _sandbox():
    box = os.path.join(paths.TMP, "selftest_%s" % time.strftime("%H%M%S"))
    if os.path.isdir(box):
        shutil.rmtree(box)
    for sub in ("runs", "prod", "style", "knowledge", "queue"):
        os.makedirs(os.path.join(box, sub), exist_ok=True)
    return box


def main():
    box = _sandbox()
    saved = {k: getattr(paths, k) for k in
             ("RUNS", "PROD", "STYLE", "KNOW", "KNOW_REGISTRY", "QUEUE", "TMP")}
    saved_lock = queue.LOCK
    paths.RUNS = os.path.join(box, "runs")
    paths.PROD = os.path.join(box, "prod")
    paths.STYLE = os.path.join(box, "style")
    paths.KNOW = os.path.join(box, "knowledge")
    paths.KNOW_REGISTRY = os.path.join(paths.KNOW, "registry.json")
    paths.QUEUE = os.path.join(box, "queue")
    paths.TMP = box
    queue.LOCK = os.path.join(box, "queue", "gpu.lock")
    cfg = paths.load_config()
    cfg["_profile"] = "test"
    cfg["_gates"] = ["check"]
    cfg["_stages"] = ["06_checks"]

    print("=== S1 门禁失败必须传播 ===")
    run = manifest.Run.create("RJ9999999", "test", cfg, planned_stages=["06_checks"],
                              planned_gates=["check"])
    real = dict(stages.STAGES)

    def fake_fail(ctx):
        return {"outputs": [], "gate": {"ok": False, "detail": {"check": {"ok": False}}},
                "extra": {}}
    stages.STAGES["06_checks"] = dict(real["06_checks"], run=fake_fail)
    try:
        ok = stages.run_stages(run, cfg, ["06_checks"])
    finally:
        stages.STAGES.clear()
        stages.STAGES.update(real)
    run = manifest.Run.load(run.run_id)
    ck("S1a run_stages 返回 False", ok is False, "ok=%s" % ok)
    ck("S1b run 状态 = gate_failed", run.data["status"] == "gate_failed",
       run.data["status"])

    print("\n=== S2 缺阶段不能判 ok ===")
    cli._refresh_status(run)
    ck("S2a 门禁失败阶段 → gate_failed", run.data["status"] == "gate_failed",
       run.data["status"])
    r2 = manifest.Run.create("RJ9999998", "test", cfg,
                             planned_stages=["01_asr", "06_checks"],
                             planned_gates=["check"])
    r2.data["stages"]["06_checks"] = {"name": "06_checks", "status": "ok",
                                      "gate": {"ok": True}}
    r2.save()
    cli._refresh_status(r2)
    ck("S2b 只跑 06、缺 01 → incomplete", r2.data["status"] == "incomplete",
       r2.data["status"])
    r2.data["stages"]["01_asr"] = {"name": "01_asr", "status": "ok", "gate": {"ok": True}}
    r2.save()
    cli._refresh_status(r2)
    ck("S2c 计划阶段齐全 → ok", r2.data["status"] == "ok", r2.data["status"])

    print("\n=== S3 知识表归因不可改写 ===")
    open(os.path.join(paths.STYLE, "glossary.json"), "w", encoding="utf-8").write("{}")
    r3 = manifest.Run.create("RJ9999997", "test", cfg, planned_stages=[],
                             planned_gates=[])
    cli._stamp_knowledge(r3)
    v_creation = json.loads(json.dumps(r3.data["knowledge_at_creation"]))
    open(os.path.join(paths.STYLE, "glossary.json"), "w", encoding="utf-8").write(
        '{"新词": {"zh": "新译", "hit": 0}}')
    cli._stamp_knowledge(r3)
    ck("S3a 创建时快照未变", r3.data["knowledge_at_creation"] == v_creation,
       "version=%s" % r3.data["knowledge_at_creation"].get("version"))
    ck("S3b 记录了 resume 差异",
       (r3.data.get("knowledge_drift") or []) == ["glossary.json"],
       str(r3.data.get("knowledge_drift")))

    print("\n=== S4 promote 拒绝遗留文件 ===")
    rj = "RJ9999996"
    d5 = os.path.join(runs_dir := paths.RUNS, r2.run_id, "05_term_lock", rj)
    os.makedirs(d5, exist_ok=True)
    open(os.path.join(d5, "01.mp3.lrc"), "w", encoding="utf-8").write("[00:01.00]测试。\n")
    r2.data["project"] = rj
    r2.data["stages"]["06_checks"] = {"name": "06_checks", "status": "ok",
                                      "gate": {"ok": True, "detail": {}}}
    r2.save()
    prod = os.path.join(paths.PROD, rj)
    os.makedirs(prod, exist_ok=True)
    open(os.path.join(prod, "01.mp3.lrc"), "w", encoding="utf-8").write("[00:01.00]旧。\n")
    open(os.path.join(prod, "02.mp3.lrc"), "w", encoding="utf-8").write("[00:01.00]多余。\n")

    class A(object):
        run = r2.run_id
        force = False
        backup = False
        dry_run = False
        allow_extra = False
    try:
        cli.cmd_promote(A())
        refused = False
    except SystemExit as e:
        refused = "未产出" in str(e) or "多余" in str(e) or "extra" in str(e).lower()
    ck("S4a 有多余旧 LRC → 拒绝", refused)

    print("\n=== S5 锁的 token 语义 ===")
    lk = queue.FileLock(os.path.join(box, "queue", "t.lock"), "selftest")
    with lk:
        info = json.load(open(lk.path, encoding="utf-8"))
        ck("S5a 锁内写入了 token", bool(info.get("token")), info.get("token", "")[:8])
        # 模拟「锁已被别人接管」：改 token 后再退出，文件必须还在
        info["token"] = "someone-else"
        json.dump(info, open(lk.path, "w", encoding="utf-8"))
    ck("S5b 退出不删别人的锁", os.path.exists(lk.path))

    lk2 = queue.FileLock(os.path.join(box, "queue", "t2.lock"), "selftest")
    json.dump({"token": "x", "pid": os.getpid(), "job": "other",
               "since": "2020-01-01T00:00:00"},
              open(lk2.path, "w", encoding="utf-8"))
    old = time.time() - 90 * 24 * 3600
    os.utime(lk2.path, (old, old))
    try:
        lk2.__enter__()
        stolen = True
    except SystemExit:
        stolen = False
    ck("S5c PID 存活时即使 90 天也不抢", not stolen)

    print("\n=== S6 05 必须是独立目录 ===")
    ctx = stages.Ctx(r2, cfg, dry=True)
    p4 = os.path.join(ctx.out_dir(), "x.lrc")
    p5 = os.path.join(ctx.lock_dir(), "x.lrc")
    os.makedirs(os.path.dirname(p5), exist_ok=True)
    open(p5, "w", encoding="utf-8").write("[00:01.00]a\n")
    ck("S6a final_dir 优先 05", os.path.abspath(stages.final_dir(ctx)) ==
       os.path.abspath(ctx.lock_dir()))
    ck("S6b 04 与 05 是不同目录", os.path.abspath(ctx.out_dir()) !=
       os.path.abspath(ctx.lock_dir()))

    print("\n=== S7 阶段 code 列表覆盖实际 import ===")
    # 为什么需要这条：03 的 code 只写了 translate.py/serve.py，而 translate.py 在
    # **函数体内** `import cues`（:274）——改 cue 切分不会让 03 失效，普通 run 会
    # 误判「缓存命中」（R156 的四个盲区之一，人肉审计才发现）。
    # 这里扫全文件的 import（含函数内），凡是同目录 scripts/*.py 就必须登记进 code。
    _scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "scripts")
    _siblings = set()
    if os.path.isdir(_scripts):
        _siblings = {fn[:-3] for fn in os.listdir(_scripts) if fn.endswith(".py")}
    _imp = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)
    for _st, _def in stages.STAGES.items():
        _code = _def.get("code") or []
        if not _code:
            continue
        _declared = {c[:-3] if c.endswith(".py") else c for c in _code}
        _miss = set()
        # 不动点迭代：补进来的模块自己也有 import（`translate.py→polish.py→cues.py`），
        # 只查一层会漏。闭包收敛很快（本目录最大深度 3）。
        _queue = [c for c in _code if not os.path.basename(c).startswith("test_")]
        _seen = set()
        while _queue:
            _fn = _queue.pop()
            _key = _fn if _fn.endswith(".py") else _fn + ".py"
            if _key in _seen:
                continue
            _seen.add(_key)
            _p = os.path.join(_scripts, _key)
            if not os.path.exists(_p):
                _miss.add("(缺文件)%s" % _fn)
                continue
            try:
                _src = open(_p, encoding="utf-8").read()
            except OSError:
                continue
            for _m in _imp.finditer(_src):
                _mod = _m.group(1)
                if _mod not in _siblings:
                    continue
                if _mod not in _declared:
                    _miss.add("%s→%s.py" % (_key, _mod))
                _queue.append(_mod + ".py")
        ck("S7 %s 的 code 覆盖 import" % _st, not _miss, "漏登记：%s" % sorted(_miss))

    # 还原
    for k, v in saved.items():
        setattr(paths, k, v)
    queue.LOCK = saved_lock
    shutil.rmtree(box, ignore_errors=True)

    print("\n结果：%d 通过 / %d 失败" % (len(OK), len(BAD)))
    if BAD:
        print("失败项：%s" % ", ".join(BAD))
        return 1
    print("✅ selftest 全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
