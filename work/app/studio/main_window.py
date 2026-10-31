# -*- coding: utf-8 -*-
"""主窗口：页面路由 + 向导状态。"""
import os

import json

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from . import pages_tools as T
from . import pages_wizard as W
from . import runner as R, settings, theme


class MainWindow(QMainWindow):
    def __init__(self):
        super(MainWindow, self).__init__()
        self.setWindowTitle("OnseiCraft")
        self.resize(1100, 780)
        self.setMinimumSize(900, 640)

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # 顶栏（只在非首页显示返回）
        self.topbar = QWidget()
        tl = QHBoxLayout(self.topbar)
        tl.setContentsMargins(20, 10, 20, 0)
        self.btn_back = QPushButton("← 返回首页")
        self.btn_back.setObjectName("Ghost")
        self.btn_back.clicked.connect(self.go_home)
        tl.addWidget(self.btn_back)
        tl.addStretch(1)
        self.crumb = QLabel("")
        self.crumb.setObjectName("Muted")
        tl.addWidget(self.crumb)
        lay.addWidget(self.topbar)
        self.topbar.hide()

        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)

        self.home = T.HomePage()
        self.audio = W.AudioPage()
        self.script = W.ScriptPage()
        self.run_page = W.RunPage()
        self.done = W.DonePage()
        self.feedback = T.FeedbackPage()
        self.store = T.StorePage()
        self.settings = T.SettingsPage()
        for p in (self.home, self.audio, self.script, self.run_page, self.done,
                  self.feedback, self.store, self.settings):
            self.stack.addWidget(p)

        self.home.open_audio.connect(self.go_audio)
        self.home.open_feedback.connect(self.go_feedback)
        self.home.open_store.connect(self.go_store)
        self.home.open_settings.connect(self.go_settings)
        self.audio.next_ready.connect(self.go_script)
        self.script.done.connect(self.on_script_done)
        self.run_page.finished.connect(self.on_run_finished)
        self.settings.saved.connect(self.home.refresh)

        self.job = {}
        self._show(0, "首页")
        QTimer.singleShot(0, self._show_update_notes)

    # ---- 路由 ----
    def _show(self, idx, crumb=""):
        self.stack.setCurrentIndex(idx)
        self.crumb.setText(crumb)
        self.topbar.setVisible(idx != 0)

    def go_home(self):
        self.home.refresh()
        self._show(0)

    def go_audio(self):
        miss = settings.missing_models()
        if miss:
            r = QMessageBox.question(
                self, "模型还没配好",
                "检测到 %d 个模型路径无效：\n%s\n\n"
                "不配模型可以跑完「纠错 / 术语 / 门禁」这些不调模型的阶段，"
                "但识别和翻译会失败。要先设置吗？"
                % (len(miss), "\n".join("· " + m[1] for m in miss)),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if r == QMessageBox.Yes:
                self.go_settings()
                return
        self.script.reset()
        self._show(1, "新建字幕 · 第 1 步 / 共 3 步")

    def go_script(self, info):
        self.job.update(info)
        self.script.project = info.get("project", "")
        self._show(2, "新建字幕 · 第 2 步 / 共 3 步")

    def on_script_done(self, script_dir):
        if script_dir == "__back__":
            self._show(1, "新建字幕 · 第 1 步 / 共 3 步")
            return
        cfg = settings.load_app()
        dev, compute = R.detect_device()
        job = {
            "source": self.job["source"],
            "project": self.job["project"],
            "script_dir": script_dir,
            "minutes": self.job.get("minutes", 10),
            "n_files": self.job.get("n_files", 1),
            "out_dir": cfg["prod_dir"],
            "device": dev,
            "compute": compute,
            "rate01": cfg.get("rate_01", 0.18),
            "rate03": cfg.get("rate_03", 0.12),
            "tail": cfg.get("fixed_tail_sec", 54.0),
            "note": "studio",
        }
        self.job["job"] = job
        self._show(3, "新建字幕 · 第 3 步 / 共 3 步")
        self.run_page.start(job)

    def on_run_finished(self, ok, run_id, msg, lrc_dir):
        self.done.show_result(ok, run_id, msg, lrc_dir,
                              self.job.get("project", ""), self.job.get("source", ""))
        self._show(4, "完成")
        self.home.refresh()

    def go_feedback(self):
        self.feedback.reload()
        self._show(5, "反馈日志")

    def go_store(self):
        self.store.reload()
        self._show(6, "ASR 暂存管理")

    def go_settings(self):
        self._show(7, "设置")

    # ---- 更新后的「本次更新内容」 ----
    def _show_update_notes(self):
        """上次是「一键更新」装的：新版首次启动把更新日志摆给用户看。"""
        p = os.path.join(settings.ROOT, "data", "update_pending.json")
        if not os.path.exists(p):
            return
        try:
            d = json.load(open(p, encoding="utf-8"))
            os.remove(p)
        except Exception:
            return
        T.show_notes_dialog(
            self, "已更新到 %s" % d.get("to", ""),
            "OnseiCraft 已从 %s 更新到 %s。\n本次更新内容：" % (
                d.get("from", "?"), d.get("to", "?")),
            d.get("notes", ""), ok_text="知道了")

    # ---- 关窗 ----
    def closeEvent(self, e):
        if self.run_page.runner and self.run_page.runner.isRunning():
            r = QMessageBox.question(
                self, "还在生成中",
                "管线还在跑。现在退出会中断这次运行（已完成的阶段会保留，下次可续跑）。确定退出？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                e.ignore()
                return
            self.run_page.runner.stop()
            self.run_page.runner.wait(5000)
        e.accept()
