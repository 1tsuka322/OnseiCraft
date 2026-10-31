# -*- coding: utf-8 -*-
"""管线执行器：在后台线程里跑六阶段，把日志/进度/ETA 变成 Qt 信号。

为什么要自己写阶段循环而不用 `stages.run_stages`：
1. 需要在**阶段之间**响应「停止」（run_stages 中间没有钩子）；
2. 需要在阶段开始/结束时立刻发信号，而不是等整条跑完；
3. 需要按阶段把「当前进度」算成 0–100%。

⚠️ 进度只信 `manifest.json`（管线自己写的唯一真相），日志只用来算**阶段内**百分比。
不要反过来从日志猜「跑到第几步」——日志会被重试/重跑覆盖。
"""
import glob
import json
import math
import os
import sys
import time
import traceback

from PySide6.QtCore import QThread, Signal

from . import settings

sys.path.insert(0, os.path.join(settings.ROOT, "pipeline"))

from pipeline_lib import manifest, paths, stages  # noqa: E402
from pipeline_lib import cli  # noqa: E402

STAGE_LABELS = {
    "01_asr": "语音识别（ASR）",
    "02_asr_fix": "识别纠错",
    "03_translate": "日文初译",
    "04_polish": "字幕后处理",
    "05_term_lock": "术语统一",
    "06_checks": "质量门禁",
}
STAGE_ORDER = ["01_asr", "02_asr_fix", "03_translate", "04_polish", "05_term_lock", "06_checks"]


# ---------------------------------------------------------------- 进度估算
class Estimator(object):
    """按音频时长估每阶段秒数；跑完用实测校准（写回 app.json）。

    01/03 与音频长度成正比，02/04/05/06 基本是固定开销（`tail`）。
    """

    def __init__(self, minutes, rate01, rate03, tail=54.0):
        self.minutes = max(0.5, minutes)
        self.plan = {
            "01_asr": max(12.0, self.minutes * 60.0 * rate01),
            "02_asr_fix": max(2.0, tail * 0.12),
            "03_translate": max(12.0, self.minutes * 60.0 * rate03),
            "04_polish": max(2.0, tail * 0.18),
            "05_term_lock": max(1.5, tail * 0.08),
            "06_checks": max(6.0, tail * 0.62),
        }
        self.total = sum(self.plan.values())

    def overall(self, done_stages, cur_stage=None, inner=0.0):
        acc = sum(self.plan[s] for s in done_stages if s in self.plan)
        if cur_stage in self.plan:
            acc += self.plan[cur_stage] * max(0.0, min(1.0, inner))
        return max(0.0, min(1.0, acc / self.total))


# ---------------------------------------------------------------- 输出捕获
class _Stream(object):
    """把 print 的每一行转成回调（管线所有输出都走 sys.stdout）。

    ⚠️ 只捕获**本线程**（跑管线的那条）的写入，其他线程直通原始 stdout。
    否则会形成递归：worker print → emit → 主线程槽里 print → 又进本流 → 又 emit …
    实测表现是进程直接崩掉（退出码 127），而不是抛异常，非常难查。
    """

    def __init__(self, on_line, orig=None):
        import threading
        self.on_line = on_line
        self.orig = orig
        self.owner = threading.get_ident()
        self.buf = ""
        self.encoding = "utf-8"
        self._busy = False

    def write(self, s):
        if not s:
            return 0
        import threading
        if threading.get_ident() != self.owner or self._busy:
            if self.orig is not None:
                try:
                    self.orig.write(s)
                except Exception:
                    pass
            return len(s)
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self._emit(line.rstrip("\r"))
        if len(self.buf) > 8000:            # 防止无换行的超长输出把内存撑爆
            self._emit(self.buf[:8000])
            self.buf = ""
        return len(s)

    def _emit(self, line):
        self._busy = True
        try:
            self.on_line(line)
        except Exception:
            pass
        finally:
            self._busy = False

    def flush(self):
        if self.buf.strip():
            self._emit(self.buf.rstrip("\r"))
            self.buf = ""

    def isatty(self):
        return False

    def reconfigure(self, **kw):
        """脚本里常写 `sys.stdout.reconfigure(encoding="utf-8")`（asr_fix_scan 就这么干）。
        没有这个方法 → 阶段直接抛 AttributeError 失败。吞掉即可：我们本来就用 utf-8。"""
        if "encoding" in kw:
            self.encoding = kw["encoding"]

    @property
    def buffer(self):
        return self

    def fileno(self):
        raise OSError("no fileno")


# ---------------------------------------------------------------- 线程
class PipelineRunner(QThread):
    sig_log = Signal(str)
    sig_stage = Signal(str, str)          # stage, status(开始/完成/失败/跳过)
    sig_progress = Signal(int, str)       # 0-100, ETA 文案
    sig_done = Signal(bool, str, str)     # ok, run_id, 消息

    def __init__(self, job, parent=None):
        super(PipelineRunner, self).__init__(parent)
        self.job = job
        self._stop = False
        self.run_id = ""
        self.lrc_dir = ""
        self.log_path = ""
        self.error = ""
        # 进度状态（GUI 用 QTimer 轮询，避免线程里跑事件循环）
        self.cur_stage = ""
        self.done_stages = []
        self.est = None
        self.t0 = 0.0
        self.stage_t0 = 0.0
        self.run_dir = ""

    def stop(self):
        self._stop = True

    def progress_info(self):
        """返回 (百分比, 剩余秒数或 None, 当前阶段名)。

        阶段内百分比用**时间**估算而不是解析日志：`stages._tee` 写日志时没有
        flush，跑的过程中日志文件里是空的（实测 01 全程显示 0%）。
        时间估算在这个项目上意外地准（01 预计 118s / 实际 109s）。
        """
        if not self.est:
            return 0, None, ""
        elapsed = time.time() - self.t0 if self.t0 else 0
        if self.cur_stage in ("", None):
            f = 1.0 if len(self.done_stages) >= len(STAGE_ORDER) else 0.0
        else:
            plan = max(1.0, self.est.plan.get(self.cur_stage, 30.0))
            spent = time.time() - (self.stage_t0 or time.time())
            inner = min(0.97, 1.0 - math.exp(-1.25 * spent / plan))
            f = self.est.overall(self.done_stages, self.cur_stage, inner)
        if f >= 0.995:
            return 100, 0, self.cur_stage
        eta = max(0.0, self.est.total - elapsed)
        return int(f * 100), eta, self.cur_stage

    # ---- 主流程 ----
    def run(self):
        job = self.job
        old_out, old_err = sys.stdout, sys.stderr
        t0 = time.time()
        est = Estimator(job["minutes"], job.get("rate01", 0.18), job.get("rate03", 0.12),
                        tail=job.get("tail", 54.0))
        done = []
        self.est = est
        self.t0 = t0
        self.done_stages = done
        try:
            cfg = paths.load_config()
            cfg.update({
                "device": job.get("device", "cuda"),
                "compute": job.get("compute", "float16"),
                "ext": "auto",
            })
            paths.ensure_dirs()
            run = manifest.Run.create(job["project"], "production", cfg,
                                      source=job["source"],
                                      script_dir=job.get("script_dir") or "",
                                      note=job.get("note", "studio"),
                                      planned_stages=STAGE_ORDER,
                                      planned_gates=cfg["profiles"]["production"]["gates"])
            self.run_id = run.run_id
            self.run_dir = run.dir
            cli._stamp_knowledge(run)

            self.log_path = os.path.join(settings.ROOT, "data", "studio_logs",
                                         run.run_id + ".log")
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            logf = open(self.log_path, "w", encoding="utf-8")

            def on_line(line):
                self.sig_log.emit(line)
                try:
                    logf.write(line + "\n")
                    logf.flush()
                except Exception:
                    pass

            sys.stdout = _Stream(on_line, old_out)
            sys.stderr = _Stream(on_line, old_err)
            self.sig_log.emit("run 已创建：%s" % run.run_id)
            self.sig_log.emit("音频目录：%s" % job["source"])

            run.set_status("running")
            n_tracks = job.get("n_files") or 0
            ok_all = True
            for name in STAGE_ORDER:
                if self._stop:
                    run.set_status("interrupted")
                    ok_all = False
                    self.error = "已停止"
                    break
                self.sig_stage.emit(name, "running")
                self.cur_stage = name
                self.stage_t0 = time.time()
                self.sig_progress.emit(int(est.overall(done) * 100), "正在%s…" % STAGE_LABELS[name])

                # 阶段内进度：起一个轮询线程不安全，改成执行前后各算一次 + 定时由 GUI 轮询 manifest
                ok = stages.execute(run, name, cfg)
                st = run.stage(name) or {}
                status = st.get("status")
                if ok and status in ("ok", "imported"):
                    done.append(name)
                    self.sig_stage.emit(name, "ok")
                    self.sig_progress.emit(int(est.overall(done) * 100),
                                           "已完成%s" % STAGE_LABELS[name])
                else:
                    self.sig_stage.emit(name, "failed")
                    self.error = (st.get("error") or
                                  json.dumps((st.get("gate") or {}), ensure_ascii=False)[:300] or
                                  "阶段未通过")
                    ok_all = False
                    break

            elapsed = time.time() - t0
            if ok_all:
                run.set_status("ok")
                self._report_gates(run)
                self.lrc_dir = self._promote(run, job)
                self._learn_rates(run, elapsed, job)
                self.sig_progress.emit(100, "全部完成")
                self.sig_done.emit(True, run.run_id,
                                   "完成，用时 %s" % _fmt(elapsed))
            else:
                self.sig_done.emit(False, run.run_id,
                                   "%s（用时 %s，可续跑）" % (self.error, _fmt(elapsed)))
        except Exception as e:
            self.error = "%s\n%s" % (e, traceback.format_exc()[-800:])
            try:
                self.sig_log.emit("❌ 异常：%s" % e)
            except Exception:
                pass
            self.sig_done.emit(False, self.run_id, str(e))
        finally:
            sys.stdout, sys.stderr = old_out, old_err

    # ---- 门禁明细 ----
    def _report_gates(self, run):
        """06 的十一项门禁逐条回显到日志：用户看不到「门禁过了」就不知道成品可不可信。"""
        g = (run.stage("06_checks") or {}).get("gate") or {}
        d = g.get("detail") or {}
        if not d:
            return
        self.sig_log.emit("")
        self.sig_log.emit("门禁结果：")
        for k, v in d.items():
            mark = "OK  " if v.get("ok") else ("FAIL" if v.get("hard") else "WARN")
            self.sig_log.emit("  %-4s %-14s %s%s"
                              % (mark, k, v.get("summary") or "",
                                 "" if v.get("hard") else "（参考项）"))

    # ---- promote ----
    def _promote(self, run, job):
        rj = run.data["project"]
        src = stages.final_dir(stages.Ctx(run, paths.load_config()))
        files = sorted(glob.glob(os.path.join(src, "*.lrc")))
        if not files:
            raise RuntimeError("没有产出 LRC：%s" % src)
        # 走官方 promote：自动备份、文件集核对、门禁检查都在这
        args = type("A", (), {})()
        args.run = run.run_id
        args.project = rj
        args.force = False
        args.backup = True
        args.allow_extra = False
        args.dry_run = False
        try:
            cli.cmd_promote(args)
        except SystemExit as e:
            # 门禁没过等情况：不阻断交付，但把成品直接放到输出目录并告警
            self.sig_log.emit("⚠️ 未进生产目录：%s" % e)
            dst = os.path.join(job.get("out_dir") or paths.PROD, rj)
            os.makedirs(dst, exist_ok=True)
            import shutil
            for f in files:
                shutil.copy2(f, os.path.join(dst, os.path.basename(f)))
            self.sig_log.emit("已直接复制 %d 个字幕到 %s（未经门禁保护）" % (len(files), dst))
            return dst
        return os.path.join(paths.PROD, rj)

    def _learn_rates(self, run, elapsed, job):
        """用**每阶段自己的 duration_sec** 校准倍率，别拿总耗时按比例瞎分摊。

        实测（1 轨 11.9 分钟）：01=110s / 03=25s，比例约 8:2，
        而早期按 6:4 分摊会同时高估 03、低估 01，ETA 越跑越歪。
        """
        try:
            cfg = settings.load_app()
            n = cfg.get("rate_samples", 0) + 1
            w = min(0.6, 1.0 / n)
            mins = max(0.5, job["minutes"])
            st = run.data.get("stages") or {}

            def dur(name):
                return float((st.get(name) or {}).get("duration_sec") or 0)

            d01, d03 = dur("01_asr"), dur("03_translate")
            if d01 > 5:
                r01 = max(0.01, min(2.0, d01 / 60.0 / mins))
                cfg["rate_01"] = cfg.get("rate_01", 0.18) * (1 - w) + r01 * w
            if d03 > 3:
                r03 = max(0.01, min(2.0, d03 / 60.0 / mins))
                cfg["rate_03"] = cfg.get("rate_03", 0.12) * (1 - w) + r03 * w
            # 其余阶段是固定开销，一起学一个值（大库小库差很多）
            rest = sum(dur(x) for x in ("02_asr_fix", "04_polish", "05_term_lock", "06_checks"))
            if rest > 1:
                cfg["fixed_tail_sec"] = cfg.get("fixed_tail_sec", 54.0) * (1 - w) + rest * w
            cfg["rate_samples"] = n
            settings.save_app(cfg)
        except Exception:
            pass


def _fmt(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分 %d 秒" % (sec // 60, sec % 60)
    return "%d 小时 %d 分" % (sec // 3600, (sec % 3600) // 60)


def fmt_eta(sec):
    if sec is None:
        return "估算中…"
    if sec <= 0:
        return "即将完成"
    return "约剩 " + _fmt(sec)


def detect_device():
    """ctranslate2 能不能用 CUDA —— 这才是 01 阶段的真实依赖（不是 torch）。"""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"
