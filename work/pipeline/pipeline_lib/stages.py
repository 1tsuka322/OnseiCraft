# -*- coding: utf-8 -*-
"""六个阶段的确定性执行器：01_asr → 02_asr_fix → 03_translate → 04_polish
→ 05_term_lock → 06_checks。

原则：
- 每阶段只做一件事，产物落在 `runs/<id>/NN_xxx/`，**从不写生产目录**；
  进生产只有一条路：`pipeline promote`。
- 每阶段的输入/输出/参数/知识表/代码都进指纹；指纹不变就跳过（断点续跑）。
- 失败**不吞**：退出码非 0 即停，manifest 记 `failed`，`run` 再跑只重试该阶段。
- GPU 阶段持串行锁；`KIKOERU_GPU_LOCK_HELD=1` 表示外层（队列）已持锁，避免自锁。
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

from . import fingerprint as FP
from . import manifest, paths, queue


# ---------------------------------------------------------------- 上下文
class Ctx(object):
    def __init__(self, run, cfg, dry=False):
        self.run = run
        self.cfg = cfg
        self.dry = dry
        self.py = paths.python_exe(cfg)
        self.project = run.data["project"]
        self.source = run.data.get("source")
        self.script_dir = run.data.get("script_dir")
        self.env = dict(os.environ)
        self.env.setdefault("PYTHONIOENCODING", "utf-8")
        self.env["KIKOERU_ROOT"] = paths.ROOT
        # 「谁调谁传」的两个开关必须由管线显式给，否则子进程吃自己的默认值：
        #   translate.py 的 ENGINE 默认是 qwen（不是 hymt！），不传就会静默换引擎；
        #   cues.py 在 import 时读 KIKOERU_CUE_PIPELINE（默认 0=显示行）。
        # 2026-10-26 实测踩到：params 里记 hymt、实际跑 qwen —— 正是 manifest 要防的事。
        if cfg.get("engine"):
            self.env["KIKOERU_ENGINE"] = str(cfg["engine"])
        self.env["KIKOERU_CUE_PIPELINE"] = str(int(cfg.get("cue_pipeline", 1)))
        if cfg.get("spk_prefix"):
            self.env["KIKOERU_SPK_PREFIX"] = "1"
        if cfg.get("track_memory"):
            self.env["KIKOERU_TRACK_MEMORY"] = "1"

    def dir(self, stage, sub=""):
        d = os.path.join(self.run.stage_dir(stage), self.project, sub) if sub \
            else os.path.join(self.run.stage_dir(stage), self.project)
        os.makedirs(d, exist_ok=True)
        return d

    def log(self, stage, name):
        return os.path.join(self.run.stage_dir(stage), name)

    # 常用目录
    def asr_dir(self):
        return self.dir("01_asr", "asr")

    def asr_raw_dir(self):
        return self.dir("01_asr", "asr_raw")

    def fix_dir(self):
        return self.dir("02_asr_fix", "asr_fix")

    def draft_dir(self):
        return self.dir("03_translate", "draft")

    def out_dir(self):
        return self.dir("04_polish")

    def lock_dir(self):
        return self.dir("05_term_lock")


def final_dir(ctx):
    """最终产物目录：05_term_lock 存在就用它，否则退回 04_polish。

    05 必须是**独立目录**（GPT 评审 P0）：原地改 04 会让 04 记录的输出哈希失效，
    再也回答不了「润色模型原本输出什么」「term_lock 改了什么」。
    """
    d = ctx.lock_dir()
    if os.path.isdir(d) and glob.glob(os.path.join(d, "*.lrc")):
        return d
    return ctx.out_dir()


def effective_asr(ctx):
    """翻译/润色的源：有 asr_fix 用修正版，**但必须确认它真的是 ASR**。

    2026-10-26 踩坑：`asr_fix/` 里也可能放的是**候选清单**
    （`[{rule,t,base,other,diff}]`，asr_fix_scan 的扫描结果），
    只判「目录里有 json」会把候选清单当 ASR 喂给翻译 → 直接崩/静默错。
    判定标准：至少一个 json 是 `{"segments": [...]}` 形态。
    """
    fx = ctx.fix_dir()
    if os.path.isdir(fx):
        for p in sorted(glob.glob(os.path.join(fx, "*.json"))):
            try:
                d = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            if isinstance(d, dict) and d.get("segments"):
                return fx
            break          # 第一个不是 ASR → 这目录不是修正版
    return ctx.asr_dir()


# ---------------------------------------------------------------- 工具
# 控制台回显守卫：`| head` 之类的截断会让 sys.stdout.write 抛 OSError(22)，
# 以前这会被当成「阶段失败」写进 manifest —— 假失败比没日志更糟。
_ECHO = [True]


def _echo(s):
    if not _ECHO[0]:
        return
    try:
        sys.stdout.write(s)
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        _ECHO[0] = False


def _tee(cmd, log_path, cwd=None, env=None):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    t0 = time.time()
    _echo("\n$ %s\n" % " ".join('"%s"' % c if " " in c else c for c in cmd))
    with open(log_path, "a", encoding="utf-8") as lf:
        lf.write("\n$ %s\n" % " ".join(cmd))
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", bufsize=1)
        tail = []
        for line in p.stdout:
            _echo(line)
            lf.write(line)
            tail.append(line)
            if len(tail) > 300:
                tail.pop(0)
        p.wait()
    return p.returncode, "".join(tail), round(time.time() - t0, 1)


def _script(name):
    return os.path.join(paths.SCRIPTS, name)


def code_rec(scripts):
    return {s: manifest.sha256_file(_script(s)) for s in scripts}


def _src_rec(ctx):
    """音频源指纹：用 name+size+mtime（内容哈希对几百 MB 音频太贵，且音频不改）。"""
    src = ctx.source
    if not src or not os.path.isdir(src):
        return {"path": src, "method": "missing", "n_files": 0, "agg_sha256": None}
    files = sorted(glob.glob(os.path.join(src, "**", "*"), recursive=True))
    files = [f for f in files if os.path.isfile(f)]
    parts = []
    for f in files:
        st = os.stat(f)
        parts.append("%s|%d|%d" % (os.path.basename(f), st.st_size, int(st.st_mtime)))
    return {"path": src.replace("\\", "/"), "method": "stat",
            "n_files": len(files), "agg_sha256": manifest.sha256_text("\n".join(parts))}


def _locks_rec(ctx):
    p = os.path.join(paths.LOCKS, ctx.project + ".json")
    return manifest.file_rec(p) if os.path.exists(p) else {"path": p, "sha256": None, "bytes": None}


def _overlay_rec(ctx):
    p = os.path.join(paths.OVERLAY, ctx.project + ".json")
    return manifest.file_rec(p) if os.path.exists(p) else {"path": p, "sha256": None, "bytes": None}


def _ensure_server(ctx, model=None, stage="03_translate"):
    """启动 llama-server（幂等），并**按引擎校验模型**。KIKOERU_SLOTS 进环境，影响确定性，记进参数。

    model: 要哪个模型（serve.py 的 key）。默认取 cfg["engine"]（03 初译用）。
    04 润色必须显式传 polish_engine（q5）——润色是「指令 + 20 条规则 + 编号协议」，
    纯翻译模型 HY-MT 做不了；反过来初译也不该用 Qwen（它就是英文残留的源头）。

    2026-10-28 事故（R153）：原来调 `serve.py start` 不带模型 → 按显存自动选 q5（Qwen2.5），
    而 translate.py 因 KIKOERU_ENGINE=hymt 用 HY-MT 的裸指令 → 英文/拒答/日文残留混进初译。
    改调 `serve.py ensure --<engine>`：端口上模型对不上就重启，起完再校验一次。

    stage: 日志落在哪个阶段目录。04 也调本函数，日志不能再写进 03（R154 实测两段
    输出混在 `03_translate/serve.log` 里，`tail` 只能看到最后一次，排障时误导）。
    """
    slots = ctx.cfg.get("slots")
    if slots:
        ctx.env["KIKOERU_SLOTS"] = str(int(slots))
    key = str(model or ctx.cfg.get("engine") or "").strip()
    cmd = [ctx.py, _script("serve.py"), "ensure"]
    if key:
        cmd.append("--" + key)
    if ctx.dry:
        print("[dry] " + " ".join(cmd))
        return
    code, tail, _ = _tee(cmd, ctx.log(stage, "serve.log"), env=ctx.env)
    if code != 0:
        raise RuntimeError("推理服务启动失败（引擎 %s）" % (key or "auto"))
    m = re.search(r"模型校验通过：(.+)", tail or "")
    if key and not m:
        raise RuntimeError("推理服务未通过模型校验（期望引擎 %s）" % key)
    ctx.extra_server_model = m.group(1).strip() if m else None
    print("[%s] 实际加载模型：%s（声明引擎 %s）"
          % (stage, ctx.extra_server_model or "未知", key or "auto"), flush=True)


# ---------------------------------------------------------------- 各阶段
def stage_asr(ctx):
    asr = ctx.asr_dir()
    raw = ctx.asr_raw_dir()
    ext = ctx.cfg.get("ext", "auto")
    target = raw if ctx.script_dir else asr
    cmd = [ctx.py, _script("asr.py"), ctx.source, target, ext,
           "--device", ctx.cfg.get("device", "cuda"),
           "--compute", ctx.cfg.get("compute", "float16")]
    if ctx.cfg.get("recursive"):
        cmd.append("--recursive")
    if ctx.dry:
        print("[dry] " + " ".join(cmd))
        return {"outputs": [], "gate": None, "extra": {"dry_run": True}}
    with queue.GpuLock("stage:01_asr"):
        code, tail, dur = _tee(cmd, ctx.log("01_asr", "asr.log"), env=ctx.env)
    if code != 0:
        raise RuntimeError("asr.py 退出码 %d" % code)
    if ctx.script_dir:
        code, tail2, _ = _tee([ctx.py, _script("script_align.py"), ctx.script_dir,
                               raw, asr], ctx.log("01_asr", "script_align.log"), env=ctx.env)
        if code != 0:
            raise RuntimeError("script_align.py 退出码 %d" % code)
    elif ctx.cfg.get("diarize"):
        dia = os.path.join(paths.ROOT, "data", "skills", "kikoeru-style-learn",
                           "scripts", "diarize.py")
        _tee([ctx.py, dia, asr, ctx.source, "--device", "cpu",
              "--thr", str(ctx.cfg.get("diarize_thr", 0.30))],
             ctx.log("01_asr", "diarize.log"), env=ctx.env)
    n = len(glob.glob(os.path.join(asr, "*.json")))
    return {"outputs": [manifest.dir_rec(asr, "*.json")],
            "gate": {"ok": n > 0, "tracks": n},
            "extra": {"tracks": n, "duration_tool_sec": dur}}


def stage_asr_fix(ctx):
    sys.path.insert(0, paths.SCRIPTS)
    import asr_fix_scan as AF
    src = ctx.asr_dir()
    fix = ctx.fix_dir()
    if ctx.dry:
        print("[dry] auto_apply_errata(%s → %s)" % (src, fix))
        return {"outputs": [], "gate": None, "extra": {"dry_run": True}}
    n = AF.auto_apply_errata(os.path.join(ctx.run.stage_dir("02_asr_fix")),
                             src, fix)
    # auto_apply_errata 只写「有改动」的轨；03_translate 读的是 fix_dir，
    # 不补就会**静默丢掉没命中 errata 的轨**（R152 实测：5 轨只出了 2 轨）。
    copied = 0
    for p in glob.glob(os.path.join(src, "*.json")):
        dst = os.path.join(fix, os.path.basename(p))
        if not os.path.exists(dst):
            shutil.copy2(p, dst)
            copied += 1
    st = AF.errata_stats(reset=True)
    rec = {"applied": st["applied"], "ctx_hit": st["ctx_hit"],
           "new_ctx": len(st["new_ctx"]), "scope_skip": len(st["scope_skip"]),
           "boundary_skip": len(st["boundary_skip"]),
           "multi_skip": st.get("multi_skip", 0), "review_skip": st.get("review_skip", 0),
           "files_touched": n}
    json.dump(rec, open(ctx.log("02_asr_fix", "errata_stats.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    if st["new_ctx"]:
        json.dump(st["new_ctx"], open(ctx.log("02_asr_fix", "errata_new_ctx.json"),
                                      "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("[02_asr_fix] ⚠️ 新语境 %d 处（未改，需 review）→ errata_new_ctx.json"
              % len(st["new_ctx"]))
    # 绿灯（可选）：需要第二版 ASR 才谈得上互证；没有就跳过并写明原因
    green = {"enabled": False, "reason": "未配置 asr_other_dir（单版本 ASR 无法互证）"}
    other = ctx.cfg.get("asr_other_dir")
    if other and os.path.isdir(other):
        dst = os.path.join(ctx.run.stage_dir("02_asr_fix"), ctx.project, "asr_other")
        if not os.path.isdir(dst):
            shutil.copytree(other, dst)
        g = AF.auto_apply_green(os.path.join(ctx.run.stage_dir("02_asr_fix")),
                                src, fix, other_ver="asr_other")
        green = {"enabled": True, "applied": g}
    else:
        print("[02_asr_fix] 绿灯跳过：%s" % green["reason"])
    rec["green"] = green
    json.dump(rec, open(ctx.log("02_asr_fix", "errata_stats.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return {"outputs": [manifest.dir_rec(fix, "*.json")],
            "gate": {"ok": True, "errata_applied": rec["applied"],
                     "new_ctx": rec["new_ctx"]},
            "extra": {"errata": rec}}


def stage_translate(ctx):
    _ensure_server(ctx)
    asr = effective_asr(ctx)
    out = ctx.draft_dir()
    cmd = [ctx.py, _script("translate.py"), asr, out]
    if ctx.dry:
        print("[dry] " + " ".join(cmd))
        return {"outputs": [], "gate": None, "extra": {"dry_run": True}}
    with queue.GpuLock("stage:03_translate"):
        code, tail, dur = _tee(cmd, ctx.log("03_translate", "translate.log"), env=ctx.env)
    if code != 0:
        raise RuntimeError("translate.py 退出码 %d" % code)
    n = len(glob.glob(os.path.join(out, "*.lrc")))
    return {"outputs": [manifest.dir_rec(out, "*.lrc")],
            "gate": {"ok": n > 0, "tracks": n},
            "extra": {"tracks": n,
                      "server_model": getattr(ctx, "extra_server_model", None)}}


def stage_polish(ctx):
    # 润色必须用**能听指令的聊天模型**（Qwen），不能用初译的 HY-MT：
    # polish 的输入是「20 条规则 + 术语表 + 编号协议」，纯翻译模型会把它当待译文本。
    _ensure_server(ctx, ctx.cfg.get("polish_engine") or "q5", stage="04_polish")
    asr = effective_asr(ctx)
    draft = ctx.draft_dir()
    out = ctx.out_dir()
    cmd = [ctx.py, _script("polish.py"), asr, draft, out,
           "--style", paths.STYLE, "--system-mode",
           ctx.cfg.get("system_mode", "strong")]
    if ctx.cfg.get("allow_loss"):
        # 丢行硬门禁的显式放行口。默认关：丢行必须是人看过之后的决定，
        # 而且会写进 params（指纹）与 gate，promote 时还会再拦一次。
        cmd.append("--allow-loss")
    if ctx.dry:
        print("[dry] " + " ".join(cmd))
        return {"outputs": [], "gate": None, "extra": {"dry_run": True}}
    with queue.GpuLock("stage:04_polish"):
        code, tail, dur = _tee(cmd, ctx.log("04_polish", "polish.log"), env=ctx.env)
    if code != 0:
        raise RuntimeError("polish.py 退出码 %d" % code)
    n = len(glob.glob(os.path.join(out, "*.lrc")))
    lines = 0
    for p in glob.glob(os.path.join(out, "*.lrc")):
        lines += sum(1 for l in open(p, encoding="utf-8") if re.match(r"^\[\d+:", l))
    m = re.search(r"锁定覆写\s*(\d+)", tail)
    losses = [l.strip() for l in tail.splitlines()
              if ("丢" in l or "只写出" in l) and "--allow-loss" in l]
    return {"outputs": [manifest.dir_rec(out, "*.lrc")],
            "gate": {"ok": n > 0, "tracks": n, "lines": lines,
                     "allow_loss": bool(ctx.cfg.get("allow_loss")),
                     "loss_notes": losses[:5]},
            "extra": {"tracks": n, "lines": lines,
                      "lock_overrides": int(m.group(1)) if m else None,
                      "allow_loss": bool(ctx.cfg.get("allow_loss")),
                      # R153 的事故就是「04 实际加载的模型 ≠ 声明」，必须逐 run 留证
                      "server_model": getattr(ctx, "extra_server_model", None)}}


def stage_term_lock(ctx):
    src = ctx.out_dir()
    out = ctx.lock_dir()
    if ctx.dry:
        print("[dry] 复制 %s → %s，再跑 term_lock" % (src, out))
        print("[dry] %s term_lock.py %s --apply --ja-dir %s" % (ctx.py, out, effective_asr(ctx)))
        return {"outputs": [], "gate": None, "extra": {"dry_run": True}}
    # 每次从 04 重建 05（幂等）：不删旧文件会让上一轮的残留混进来
    for f in glob.glob(os.path.join(out, "*.lrc")):
        os.remove(f)
    n_copy = 0
    for f in sorted(glob.glob(os.path.join(src, "*.lrc"))):
        shutil.copy2(f, os.path.join(out, os.path.basename(f)))
        n_copy += 1
    if n_copy == 0:
        raise RuntimeError("04_polish 没有 LRC，无法做术语锁：%s" % src)
    asr = effective_asr(ctx)
    cmd1 = [ctx.py, _script("term_lock.py"), out, "--apply", "--ja-dir", asr]
    cmd2 = [ctx.py, _script("term_lock.py"), out, "--check", "--ja-dir", asr,
            "--json", ctx.log("05_term_lock", "term_lock_check.json")]
    if ctx.dry:
        print("[dry] " + " ".join(cmd1))
        print("[dry] " + " ".join(cmd2))
        return {"outputs": [], "gate": None, "extra": {"dry_run": True}}
    code1, tail1, _ = _tee(cmd1, ctx.log("05_term_lock", "apply.log"), env=ctx.env)
    code2, tail2, _ = _tee(cmd2, ctx.log("05_term_lock", "check.log"), env=ctx.env)
    m = re.search(r"改写\s*(\d+)\s*处", tail1)
    changed = int(m.group(1)) if m else 0
    bad = re.search(r"(\d+)\s*处不符合", tail2) or re.search(r"不合规\s*[:：]\s*(\d+)", tail2)
    return {"outputs": [manifest.dir_rec(out, "*.lrc")],
            "gate": {"ok": code2 == 0, "rewrites": changed,
                     "violations": int(bad.group(1)) if bad else None},
            "extra": {"rewrites": changed}}


GATE_DEFS = {
    "check": {"hard": True, "cmd": lambda ctx, d: [ctx.py, _script("check.py"),
                                                   final_dir(ctx), "--style", paths.STYLE]},
    # 空字幕（R142/R143）：拿 ASR 确定性重建「应有的行」，逐行对成品。
    # check.py 抓不到这一类 —— 空文本行被当成清屏 spacer 了。
    # ⚠️ `hard` 按 run **动态**判定：adopt 的 run 是「导入的历史成品」，它的 ASR 与成品
    # 本来就不同版（cue 边界/时间戳差 0.4–2s，实测逐行漂移 5%–51%）→ 重建必然误报，
    # 只作参考项。新跑的 run 才当硬门禁（实测 0/547 假报）。
    "empty": {"hard": lambda ctx: ctx.run.data.get("profile") != "adopted",
              "cmd": lambda ctx, d: [
        ctx.py, _script("empty_check.py"),
        "--lrc", final_dir(ctx),
        # 传 effective_asr 的**父目录**：里面同时有 asr/ 与 asr_raw/，
        # empty_check 按版本优先级挑（asr_fix > asr > 其他 > asr_raw）。
        "--asr", os.path.dirname(effective_asr(ctx)),
        "--json", os.path.join(d, "empty_check.json"),
        "--max-miss", str(float(ctx.cfg.get("empty_max_miss", 0.0))),
        "--quiet"]},
    "scan": {"hard": False, "cmd": lambda ctx, d: [
        ctx.py, _script("scan_violations.py"), "--out", os.path.join(d, "scan"),
        # run 级口径（2026-10-26）：扫**这次 run 的产物 + 它自己的 ASR**，
        # 不然 16 个 run 的 scan 数字全是同一个全库值。
        "--lrc-root", os.path.dirname(final_dir(ctx)),
        "--asr-root", os.path.join(ctx.run.dir, "01_asr"),
        "--rj", ctx.project]},
    "errata_audit": {"hard": True, "cmd": lambda ctx, d: [ctx.py, _script("errata_audit.py"),
                                                          "--test"]},
    "test_regressions": {"hard": True, "cmd": lambda ctx, d: [ctx.py, _script("test_regressions.py")]},
    "overlay": {"hard": False, "cmd": lambda ctx, d: [ctx.py, _script("repair_overlay.py"),
                                                      "--check"]},
    # 两个「抽样清单」型参考项（精度 39%，只报不改）
    "trunc": {"hard": False, "cmd": lambda ctx, d: [
        ctx.py, _script("trunc_audit.py"), "--dir", final_dir(ctx),
        "--json", os.path.join(d, "trunc_audit.json"),
        "--md", os.path.join(d, "TRUNC_AUDIT.md")]},
    "slot_guard": {"hard": False, "cmd": lambda ctx, d: [
        ctx.py, _script("slot_guard.py"), effective_asr(ctx), ctx.draft_dir(),
        final_dir(ctx), "--report", os.path.join(d, "slot_guard.json")]},
    # 语义覆盖（P2，2026-10-28）：原文出现的具体物被中文泛化成公文腔
    # （おしっこ→排泄行为、ちんこ→那个部位）时，check/scan/trunc 三道全放过。
    # ⚠️ 现在是**参考项**：loss 档已知 8 条未修（全库 23416 行），
    #    先报不改，等 `--fix` 清完再提成硬门禁（与 trunc 同一条升级路径）。
    "semantic": {"hard": False, "cmd": lambda ctx, d: [
        ctx.py, _script("semantic_coverage.py"),
        # --lrc 要的是「含 RJ*/ 的父目录」；--asr 要的是「含 <RJ>/asr 的父目录」
        "--lrc", os.path.dirname(final_dir(ctx)),
        "--asr", os.path.join(ctx.run.dir, "01_asr"),
        "--rj", ctx.project,
        "--json", os.path.join(d, "semantic_coverage.json")]},
}


def stage_checks(ctx):
    d = os.path.join(ctx.run.stage_dir("06_checks"))   # 检查对象由 GATE_DEFS 里的 final_dir() 决定
    os.makedirs(d, exist_ok=True)
    gates = ctx.cfg.get("_gates") or ["check", "scan", "errata_audit"]
    res = {}
    all_ok = True
    for g in gates:
        spec = GATE_DEFS[g]
        cmd = spec["cmd"](ctx, d)
        # `hard` 允许写成 callable(ctx)：有的门禁只在特定 profile 下才算硬门禁
        # （empty 在 adopt 的 run 上只作参考，见 GATE_DEFS 注释）。
        hard = spec["hard"](ctx) if callable(spec["hard"]) else spec["hard"]
        if ctx.dry:
            print("[dry] " + " ".join(cmd))
            continue
        code, tail, dur = _tee(cmd, os.path.join(d, g + ".log"), env=ctx.env)
        ok = (code == 0)
        summary = _summarize(g, tail, code, d)
        res[g] = {"ok": ok, "exit": code, "hard": hard,
                  "duration_sec": dur, "summary": summary}
        if hard and not ok:
            all_ok = False
    if ctx.dry:
        return {"outputs": [], "gate": None, "extra": {"dry_run": True}}
    json.dump(res, open(os.path.join(d, "gates.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return {"outputs": [manifest.dir_rec(d, "*.json")],
            "gate": {"ok": all_ok, "detail": res},
            "extra": {"gates": res}}


def _summarize(gate, tail, code, d=""):
    if gate == "check":
        m = re.search(r"结论:\s*(.+)", tail)
        return m.group(1).strip() if m else ("通过" if code == 0 else "失败")
    if gate == "scan":
        m = re.search(r"命中\s*(\d+)\s*条.*?覆盖\s*(\d+)\s*轨", tail)
        return ("%s 条 / %s 轨" % (m.group(1), m.group(2))) if m else "?"
    if gate == "empty":
        m = re.search(r"缺失\s*(\d+)\s*行（([\d.]+)%）", tail)
        return ("缺 %s 行（%s%%）" % (m.group(1), m.group(2))) if m else \
               ("通过" if code == 0 else "失败")
    if gate == "errata_audit":
        m = re.search(r"(\d+)/(\d+)\s*通过", tail) or re.search(r"通过\s*(\d+)/(\d+)", tail)
        return ("%s/%s" % (m.group(1), m.group(2))) if m else ("通过" if code == 0 else "失败")
    if gate == "test_regressions":
        bad = re.findall(r"❌[^\n]*", tail)
        return "全过" if not bad else "%d 段未过" % len(bad)
    if gate == "overlay":
        m = re.search(r"时间轴校验：([^\n（]+)", tail)
        return m.group(1).strip() if m else "?"
    if gate in ("trunc", "slot_guard"):
        # 这两个门禁把结果写成 json，直接数条数比抓 stdout 稳
        fn = os.path.join(d, "trunc_audit.json" if gate == "trunc" else "slot_guard.json")
        try:
            arr = json.load(open(fn, encoding="utf-8"))
            n = len(arr) if isinstance(arr, list) else len(arr.get("rows") or arr)
            return "%d 条（参考）" % n
        except Exception:
            return "通过" if code == 0 else "失败"
    if gate == "semantic":
        m = re.search(r"违规\s*(\d+)\s*条（其中真信息丢失 loss\s*(\d+)\s*条", tail)
        if m:
            return "%s 条（loss %s）" % (m.group(1), m.group(2))
        return "通过" if code == 0 else "失败"
    return "通过" if code == 0 else "失败"


# ---------------------------------------------------------------- 注册表
STAGES = {
    "01_asr": {
        "desc": "ASR（+台本打轴/声纹）", "gpu": True,
        "knowledge": [],
        "code": ["asr.py", "script_align.py"],
        "run": stage_asr,
        "params": lambda ctx: {"ext": ctx.cfg.get("ext", "auto"),
                               "device": ctx.cfg.get("device"),
                               "compute": ctx.cfg.get("compute"),
                               "script": bool(ctx.script_dir),
                               "diarize": bool(ctx.cfg.get("diarize")) and not ctx.script_dir,
                               "model": "faster-whisper-large-v3"},
        "inputs": lambda ctx: [_src_rec(ctx)],
    },
    "02_asr_fix": {
        "desc": "确定性 ASR 改错（errata / 绿灯）", "gpu": False,
        "knowledge": ["asr_errata.json", "do_not_change.json"],
        "code": ["asr_fix_scan.py"],
        "run": stage_asr_fix,
        "params": lambda ctx: {"green": bool(ctx.cfg.get("asr_other_dir"))},
        "inputs": lambda ctx: [manifest.dir_rec(ctx.asr_dir(), "*.json")],
    },
    "03_translate": {
        "desc": "初译（HY-MT）", "gpu": True,
        "knowledge": ["glossary.json", "pin_sets.json", "user_rules.txt", "terms_db.json"],
        "code": ["translate.py", "serve.py"],
        "run": stage_translate,
        "params": lambda ctx: {"engine": ctx.cfg.get("engine", "hymt"),
                               "cue_pipeline": int(ctx.cfg.get("cue_pipeline", 1)),
                               "slots": ctx.cfg.get("slots")},
        "inputs": lambda ctx: [manifest.dir_rec(effective_asr(ctx), "*.json")],
    },
    "04_polish": {
        "desc": "风格润色（Qwen2.5-7B + E1 锁 + A8）", "gpu": True,
        "knowledge": ["glossary.json", "hard_rules.txt", "user_rules.txt",
                      "parallel.json", "pin_sets.json"],
        "code": ["polish.py", "serve.py", "cues.py", "mask_nonlexical.py",
                 "postprocess.py", "pragmatics_hints.py", "entity_ledger.py",
                 "term_repair.py", "term_lock.py"],
        "run": stage_polish,
        "params": lambda ctx: {"slots": ctx.cfg.get("slots"),
                               "polish_engine": ctx.cfg.get("polish_engine", "q5"),
                               "cue_pipeline": int(ctx.cfg.get("cue_pipeline", 1)),
                               "allow_loss": bool(ctx.cfg.get("allow_loss")),
                               "spk_prefix": bool(ctx.cfg.get("spk_prefix")),
                               "track_memory": bool(ctx.cfg.get("track_memory")),
                               "system_mode": ctx.cfg.get("system_mode", "strong")},
        "inputs": lambda ctx: [manifest.dir_rec(ctx.draft_dir(), "*.lrc"),
                               _locks_rec(ctx), _overlay_rec(ctx)],
    },
    "05_term_lock": {
        "desc": "术语锁落地（确定性覆写）", "gpu": False,
        "knowledge": ["pin_sets.json", "glossary.json", "do_not_change.json",
                      "terms_db.json"],
        "code": ["term_lock.py"],
        "run": stage_term_lock,
        "params": lambda ctx: {"only": ctx.cfg.get("term_only", "")},
        "inputs": lambda ctx: [manifest.dir_rec(ctx.out_dir(), "*.lrc")],
    },
    "06_checks": {
        "desc": "门禁（check / scan / errata / 回归 / 覆盖层 / 语义覆盖）", "gpu": False,
        "knowledge": ["asr_errata.json", "glossary.json", "stats.json", "coverage.json"],
        "code": ["check.py", "empty_check.py", "scan_violations.py", "errata_audit.py",
                 "test_regressions.py", "repair_overlay.py", "semantic_coverage.py"],
        "run": stage_checks,
        "params": lambda ctx: {"gates": ctx.cfg.get("_gates")},
        "inputs": lambda ctx: [manifest.dir_rec(final_dir(ctx), "*.lrc")],
    },
}

ORDER = ["01_asr", "02_asr_fix", "03_translate", "04_polish", "05_term_lock", "06_checks"]


# ---------------------------------------------------------------- 执行
def _miss_reason(prev, fp, params, inputs, know, code, models=None):
    if not prev:
        return "首次运行"
    if prev.get("status") not in ("ok", "imported"):
        return "上次状态=%s" % prev.get("status")
    why = []
    if prev.get("params") != params:
        why.append("参数变了")
    if prev.get("code") != code:
        changed = [k for k in code if (prev.get("code") or {}).get(k) != code[k]]
        why.append("代码变了(%s)" % ",".join(changed[:3]))
    if manifest.knowledge_agg(prev.get("knowledge")) != manifest.knowledge_agg(know):
        why.append("知识表变了")
    if json.dumps(prev.get("inputs"), sort_keys=True) != json.dumps(inputs, sort_keys=True):
        why.append("输入变了")
    # 模型也进指纹（R153 的教训：声明 hymt 实际跑 Qwen）。漏了这一条，
    # 换 gguf 时只会得到一句「指纹不符」，看不出到底哪里变了。
    if models is not None and json.dumps(prev.get("models"), sort_keys=True) \
            != json.dumps(models, sort_keys=True):
        why.append("模型变了")
    # 执行层（pipeline_lib/*.py）变了：默认引擎、门禁清单、_ensure_server 行为都在这里。
    # 不单列一条，改执行层只会得到一句「指纹不符」，看不出是不是真重跑过。
    if prev.get("runner") and prev.get("runner") != FP.runner_hash():
        why.append("执行层代码变了")
    return "；".join(why) or "指纹不符"


def execute(run, name, cfg, force=False, dry=False):
    spec = STAGES[name]
    ctx = Ctx(run, cfg, dry=dry)
    params = spec["params"](ctx)
    inputs = spec["inputs"](ctx)
    know = manifest.knowledge_now(spec["knowledge"])
    code = code_rec(spec["code"])
    models = FP.model_records(ctx.cfg, name)
    fp = FP.stage_fingerprint(name, params, inputs, know, code, ctx.cfg, models=models)
    prev = run.stage(name) or {}

    if not force and not dry and prev.get("fingerprint") == fp \
            and prev.get("status") in ("ok", "imported"):
        print("[%s] ✅ 缓存命中，跳过（%s，%s）"
              % (name, prev.get("ended_at", "?"), spec["desc"]))
        return True

    reason = "强制重跑" if force else _miss_reason(prev, fp, params, inputs, know, code,
                                                 models=models)
    print("\n[%s] ▶ %s%s" % (name, spec["desc"], "" if dry else "（%s）" % reason))
    run.begin_stage(name, params, inputs, know, code, cache_miss_reason=reason,
                    extra={"config_hash": FP.config_hash(ctx.cfg),
                           "env": FP.env_whitelist(ctx.cfg),
                           "runner": FP.runner_hash(),
                           "models": models})
    try:
        res = spec["run"](ctx)
    except Exception as e:
        run.finish_stage(name, status="failed", extra={"error": str(e)})
        print("[%s] ❌ 失败：%s" % (name, e))
        print("       续跑：pipeline run --run %s --from %s" % (run.run_id, name))
        return False
    if dry:
        run.finish_stage(name, status="dry_run", extra={"dry_run": True})
        return True
    gate = res.get("gate") or {}
    run.finish_stage(name, outputs=res.get("outputs"), gate=gate,
                     status="ok" if gate.get("ok", True) else "gate_failed",
                     extra=res.get("extra"))
    if not gate.get("ok", True):
        print("[%s] ⚠️ 门禁未过：%s" % (name, json.dumps(gate, ensure_ascii=False)[:300]))
        return False
    print("[%s] ✅ 完成" % name)
    return True


def run_stages(run, cfg, names=None, force=False, dry=False):
    """按顺序执行；**失败必须让 run 状态与退出码都反映出来**。

    三种失败要分开：阶段异常 → failed；硬门禁不过 → gate_failed；Ctrl-C → interrupted。
    混成一个 "failed" 会让「门禁拦下了内容缺陷」看起来像「程序崩了」，
    也会让「只跑了一半」看起来像可 promote。
    """
    names = names or ORDER
    for n in names:
        if n not in STAGES:
            raise SystemExit("未知阶段：%s（可选：%s）" % (n, ", ".join(ORDER)))
    run.set_status("running")
    cur = None
    try:
        for n in names:
            cur = n
            if not execute(run, n, cfg, force=force, dry=dry):
                st = (run.stage(n) or {}).get("status")
                run.set_status("gate_failed" if st == "gate_failed" else "failed")
                return False
    except KeyboardInterrupt:
        st = run.data["stages"].get(cur) or {}
        if st.get("status") == "running":
            st["status"] = "interrupted"
            st["ended_at"] = manifest.now()
            run.save()
        run.set_status("interrupted")
        print("\n[中断] 已停止在 %s；续跑：pipeline run --run %s --from %s"
              % (cur, run.run_id, cur))
        return False
    run.set_status("dry_run" if dry else "ok")
    return True
