# -*- coding: utf-8 -*-
"""三步向导：音频 → 台本 → 生成 → 完成。"""
import os
import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from . import feedback, project as proj, runner as R, settings, store, theme
from .widgets import DropZone, IconBadge, StageRow, StepBar, hline, open_path

RJ_OK = re.compile(r"^RJ\d{5,}$")


class Page(QWidget):
    def __init__(self, title="", sub="", parent=None):
        super(Page, self).__init__(parent)
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(0, 0, 0, 0)
        self.outer.setSpacing(0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.body = QWidget()
        self.scroll.setWidget(self.body)
        self.outer.addWidget(self.scroll)
        self.lay = QVBoxLayout(self.body)
        self.lay.setContentsMargins(36, 28, 36, 28)
        self.lay.setSpacing(14)
        if title:
            h = QLabel(title)
            h.setObjectName("H1")
            self.lay.addWidget(h)
        if sub:
            s = QLabel(sub)
            s.setObjectName("Muted")
            s.setWordWrap(True)
            self.lay.addWidget(s)

    def add(self, w, stretch=0):
        self.lay.addWidget(w, stretch)
        return w

    def add_stretch(self):
        self.lay.addStretch(1)


# ---------------------------------------------------------------- 1 音频
class AudioPage(Page):
    next_ready = Signal(dict)          # 音频信息
    def __init__(self, parent=None):
        super(AudioPage, self).__init__(
            "选择音频",
            "把音频文件或整个文件夹拖进来。支持 mp3 / wav / flac / m4a / ogg 等格式。", parent)
        self.info = None
        self.zone = DropZone("拖入音频文件，或点击选择",
                             "可以是一个文件夹，也可以是多条音频文件", mode="audio")
        self.zone.dropped.connect(self.on_drop)
        self.add(self.zone)

        self.card = QFrame()
        self.card.setObjectName("CardFlat")
        self.cl = QVBoxLayout(self.card)
        self.cl.setContentsMargins(18, 16, 18, 16)
        self.cl.setSpacing(10)
        self.summary = QLabel("尚未选择音频")
        self.summary.setObjectName("Muted")
        row = QHBoxLayout()
        row.addWidget(QLabel("作品号"))
        self.proj_edit = QLineEdit()
        self.proj_edit.setPlaceholderText("RJ1234567")
        self.proj_edit.setFixedWidth(200)
        self.proj_edit.textChanged.connect(self._validate)
        row.addWidget(self.proj_edit)
        self.hint = QLabel("用于命名输出目录；不含 RJ 号时会自动生成")
        self.hint.setObjectName("Muted")
        row.addWidget(self.hint)
        row.addStretch(1)
        self.cl.addWidget(self.summary)
        self.cl.addLayout(row)
        self.filelist = QListWidget()
        self.filelist.setMaximumHeight(118)
        self.filelist.setStyleSheet("QListWidget{background:#FCFAF8;}")
        self.cl.addWidget(self.filelist)
        self.add(self.card)
        self.card.hide()

        self.add_stretch()
        bar = QHBoxLayout()
        bar.addStretch(1)
        self.btn_next = QPushButton("下一步：台本")
        self.btn_next.setObjectName("Primary")
        self.btn_next.setEnabled(False)
        self.btn_next.clicked.connect(self._next)
        bar.addWidget(self.btn_next)
        self.lay.addLayout(bar)

    def on_drop(self, paths):
        try:
            info = proj.prepare_source(paths, project=self._guess_project(paths))
        except Exception as e:
            QMessageBox.warning(self, "读不了这些文件", str(e))
            return
        self.info = info
        self.card.show()
        self.proj_edit.setText(info["project"])
        mins = proj.estimate_minutes(info["total_bytes"])
        self.summary.setText(
            "共 %d 个音频 · %s · 约 %.0f 分钟音频 · %s"
            % (info["n_files"], proj.human_size(info["total_bytes"]), mins, info["note"]))
        self.filelist.clear()
        for f in info["files"]:
            it = QListWidgetItem(os.path.basename(f))
            it.setToolTip(f)
            self.filelist.addItem(it)
        self._validate()

    def _guess_project(self, paths):
        for p in paths:
            m = proj.RJ_RE.search(os.path.basename(p.rstrip("\\/")))
            if m:
                return "RJ" + m.group(1)
        return None

    def _validate(self):
        ok = bool(self.info) and bool(RJ_OK.match(self.proj_edit.text().strip()))
        self.btn_next.setEnabled(ok)
        if self.info and not RJ_OK.match(self.proj_edit.text().strip()):
            self.hint.setText("作品号必须形如 RJ1234567")
            self.hint.setStyleSheet("color:%s;" % theme.ERR)
        else:
            self.hint.setText("用于命名输出目录")
            self.hint.setStyleSheet("color:%s;" % theme.MUTED)

    def _next(self):
        if not self.info:
            return
        self.info["project"] = self.proj_edit.text().strip()
        self.info["minutes"] = proj.estimate_minutes(self.info["total_bytes"])
        self.next_ready.emit(self.info)


# ---------------------------------------------------------------- 2 台本
class ScriptPage(Page):
    done = Signal(str)                 # script_dir（空 = 跳过）
    def __init__(self, parent=None):
        super(ScriptPage, self).__init__(
            "有台本文件吗？",
            "官方台本（txt 或 pdf）可以让识别只当时间锚点，精度显著更高。"
            "没有就跳过——跳过不影响生成，只是字幕可能略差。", parent)
        self.script_dir = ""
        self.project = ""
        self.zone = DropZone("拖入台本（txt / pdf），或点击选择",
                             "可以是单个文件，也可以是一个装台本的文件夹", mode="script")
        self.zone.dropped.connect(self.on_drop)
        self.add(self.zone)

        self.card = QFrame()
        self.card.setObjectName("CardFlat")
        self.cl = QVBoxLayout(self.card)
        self.cl.setContentsMargins(18, 16, 18, 16)
        self.title = QLabel("")
        self.title.setStyleSheet("font-weight:600;")
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(74)
        self.detail.setStyleSheet("background:#FCFAF8;border:1px solid %s;" % theme.BORDER)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(112)
        self.preview.setObjectName("Log")
        for w in (self.title, self.detail, self.preview):
            self.cl.addWidget(w)
        self.add(self.card)
        self.card.hide()

        self.add_stretch()
        bar = QHBoxLayout()
        btn_skip = QPushButton("没有台本，跳过")
        btn_skip.setObjectName("Ghost")
        btn_skip.clicked.connect(self._skip)
        bar.addWidget(btn_skip)
        bar.addStretch(1)
        self.btn_back = QPushButton("上一步")
        self.btn_back.clicked.connect(lambda: self.done.emit("__back__"))
        bar.addWidget(self.btn_back)
        self.btn_next = QPushButton("开始生成")
        self.btn_next.setObjectName("Primary")
        self.btn_next.setEnabled(False)
        self.btn_next.clicked.connect(lambda: self.done.emit(self.script_dir))
        bar.addWidget(self.btn_next)
        self.lay.addLayout(bar)

    def reset(self):
        self.script_dir = ""
        self.card.hide()
        self.btn_next.setEnabled(False)

    def on_drop(self, paths):
        # ⚠️ 台本目录必须按作品隔离：共用一个 `_scripts/` 时，第二部作品的台本
        # 会和上一部混在一起被 load_script 一起读进去（打轴会静默串味）。
        sub = self.project or "_misc"
        dest = os.path.join(settings.load_app()["import_dir"], "_scripts", sub)
        try:
            d, files, warns = proj.prepare_script(paths, dest)
        except Exception as e:
            QMessageBox.warning(self, "读不了台本", str(e))
            return
        self.script_dir = d
        self.card.show()
        self.title.setText("已读取 %d 个台本文件" % len(files))
        msg = "；".join(os.path.basename(f) for f in files)
        if warns:
            msg += "\n[注意] " + "\n[注意] ".join(warns)
        self.detail.setPlainText(msg)
        lines = proj.script_preview(files[0])
        self.preview.setPlainText("\n".join(lines) if lines else "（预览为空）")
        self.btn_next.setEnabled(True)

    def _skip(self):
        self.done.emit("")


# ---------------------------------------------------------------- 3 运行
class RunPage(Page):
    finished = Signal(bool, str, str, str)     # ok, run_id, msg, lrc_dir
    back = Signal()
    def __init__(self, parent=None):
        super(RunPage, self).__init__("正在生成字幕", "", parent)
        self.runner = None
        self.job = None
        self.timer = QTimer(self)
        self.timer.setInterval(800)
        self.timer.timeout.connect(self._tick)

        self.steps = StepBar(["音频", "台本", "生成"])
        self.steps.set_step(2)
        self.add(self.steps)

        self.card = QFrame()
        self.card.setObjectName("CardFlat")
        cl = QVBoxLayout(self.card)
        cl.setContentsMargins(18, 14, 18, 16)
        cl.setSpacing(8)
        self.rows = {}
        for name in R.STAGE_ORDER:
            row = StageRow(name, R.STAGE_LABELS[name])
            cl.addWidget(row)
            self.rows[name] = row
        self.add(self.card)

        self.pbar = QProgressBar()
        self.pbar.setRange(0, 100)
        self.pbar.setValue(0)
        self.add(self.pbar)

        line = QHBoxLayout()
        self.pct = QLabel("0%")
        self.pct.setStyleSheet("font-weight:600;")
        self.eta = QLabel("估算中…")
        self.eta.setObjectName("Muted")
        line.addWidget(self.pct)
        line.addWidget(self.eta)
        line.addStretch(1)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.clicked.connect(self._stop)
        line.addWidget(self.btn_stop)
        self.lay.addLayout(line)

        self.log = QPlainTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setMinimumHeight(200)
        self.add(self.log, 1)

    def start(self, job):
        self.job = job
        self.pbar.setValue(0)
        self.pct.setText("0%")
        self.eta.setText("估算中…")
        self.log.clear()
        for r in self.rows.values():
            r.set_state("wait")
        self.btn_stop.setEnabled(True)
        self.runner = R.PipelineRunner(job)
        self.runner.sig_log.connect(self._on_log)
        self.runner.sig_stage.connect(self._on_stage)
        self.runner.sig_done.connect(self._on_done)
        self.runner.start()
        self.timer.start()

    def _on_log(self, line):
        self.log.appendPlainText(line)

    def _on_stage(self, name, status):
        row = self.rows.get(name)
        if not row:
            return
        row.set_state("running" if status == "running" else status)

    def _tick(self):
        if not self.runner:
            return
        pct, eta, cur = self.runner.progress_info()
        self.pbar.setValue(pct)
        self.pct.setText("%d%%" % pct)
        self.eta.setText(R.fmt_eta(eta))

    def _stop(self):
        if self.runner and self.runner.isRunning():
            self.runner.stop()
            self.btn_stop.setEnabled(False)
            self.eta.setText("正在停止（当前阶段结束后停下）…")

    def _on_done(self, ok, run_id, msg):
        self.timer.stop()
        self.btn_stop.setEnabled(False)
        self.pbar.setValue(100 if ok else self.pbar.value())
        self.finished.emit(ok, run_id, msg, self.runner.lrc_dir if self.runner else "")


# ---------------------------------------------------------------- 4 完成
class DonePage(Page):
    def __init__(self, parent=None):
        super(DonePage, self).__init__("", "", parent)
        self.run_id = ""
        self.project = ""
        self.lrc_dir = ""
        self.source = ""

        self.icon = IconBadge("check", 58)
        self.add(self.icon)
        self.h1 = QLabel("字幕已生成")
        self.h1.setObjectName("H1")
        self.add(self.h1)
        self.sub = QLabel("")
        self.sub.setObjectName("Muted")
        self.sub.setWordWrap(True)
        self.add(self.sub)

        card = QFrame()
        card.setObjectName("CardFlat")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 16, 18, 16)
        cl.setSpacing(8)
        cl.addWidget(QLabel("字幕输出位置"))
        self.path_lb = QLabel("")
        self.path_lb.setWordWrap(True)
        self.path_lb.setStyleSheet("font-family:Consolas,monospace;color:%s;" % theme.ACCENT)
        cl.addWidget(self.path_lb)
        row = QHBoxLayout()
        self.btn_open = QPushButton("打开文件夹")
        self.btn_open.setObjectName("Primary")
        self.btn_open.clicked.connect(lambda: open_path(self.lrc_dir))
        row.addWidget(self.btn_open)
        row.addStretch(1)
        cl.addLayout(row)
        self.add(card)

        keep = QFrame()
        keep.setObjectName("CardFlat")
        kl = QVBoxLayout(keep)
        kl.setContentsMargins(18, 16, 18, 16)
        kl.setSpacing(8)
        t = QLabel("要保留这次的 ASR 原文吗？")
        t.setStyleSheet("font-weight:600;")
        kl.addWidget(t)
        d = QLabel("保留后可以在「反馈日志」里挑出不满意的轨道，软件会把你的意见、"
                   "ASR 识别原文、修后原文一起导出成 txt，方便发回给开发者。\n"
                   "不保留则删除这次的识别中间产物（字幕文件不受影响，"
                   "但之后无法再为这一部生成反馈日志）。")
        d.setObjectName("Muted")
        d.setWordWrap(True)
        kl.addWidget(d)
        row2 = QHBoxLayout()
        self.btn_keep = QPushButton("保留 ASR 原文")
        self.btn_keep.setObjectName("Primary")
        self.btn_keep.clicked.connect(self._keep)
        self.btn_drop = QPushButton("不保留，删除")
        self.btn_drop.setObjectName("Danger")
        self.btn_drop.clicked.connect(self._drop)
        row2.addWidget(self.btn_keep)
        row2.addWidget(self.btn_drop)
        row2.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("Muted")
        row2.addWidget(self.status)
        kl.addLayout(row2)
        self.add(keep)

        self.add_stretch()
        bar = QHBoxLayout()
        bar.addStretch(1)
        self.btn_home = QPushButton("回到首页")
        self.btn_home.clicked.connect(lambda: self.window().go_home())
        bar.addWidget(self.btn_home)
        self.lay.addLayout(bar)

    def show_result(self, ok, run_id, msg, lrc_dir, project, source):
        self.run_id, self.project, self.lrc_dir, self.source = run_id, project, lrc_dir, source
        if ok:
            self.icon.set_kind("check")
            self.h1.setText("字幕已生成")
            self.sub.setText("%s" % msg)
        else:
            self.icon.set_kind("warn")
            self.h1.setText("没有跑完")
            self.sub.setText(msg)
        self.path_lb.setText(lrc_dir or "（没有产出目录）")
        self.btn_open.setEnabled(bool(lrc_dir))
        self.btn_keep.setEnabled(ok)
        self.btn_drop.setEnabled(ok)
        self.status.setText("")
        if not ok:
            self.btn_keep.setEnabled(False)
            self.btn_drop.setEnabled(False)

    def _keep(self):
        try:
            d = store.save_from_run(self.run_id, self.project, self.source)
            self.status.setText("已暂存到 %s" % d)
            self.btn_keep.setEnabled(False)
            self.btn_drop.setEnabled(False)
        except Exception as e:
            QMessageBox.warning(self, "暂存失败", str(e))

    def _drop(self):
        r = QMessageBox.question(
            self, "确认删除",
            "删除这次运行的 ASR 原文？\n\n字幕文件不受影响，但删除后无法再为这一部生成反馈日志，"
            "该次运行也不能从中间阶段续跑。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        n = store.purge_run_asr(self.run_id, self.project)
        self.status.setText("已删除 %d 个识别文件" % n)
        self.btn_keep.setEnabled(False)
        self.btn_drop.setEnabled(False)
