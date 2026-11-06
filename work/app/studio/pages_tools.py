# -*- coding: utf-8 -*-
"""首页、反馈日志、ASR 暂存管理、设置。"""
import os
import threading
import time

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDialog, QTextBrowser,
                               QFileDialog, QFrame,
                               QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from . import feedback, models, project as proj, runner as R, settings, store, theme, update
from .widgets import CardButton, hline, open_path
from .pages_wizard import Page


# ---------------------------------------------------------------- 首页
class HomePage(Page):
    open_audio = Signal()
    open_feedback = Signal()
    open_store = Signal()
    open_settings = Signal()

    def __init__(self, parent=None):
        super(HomePage, self).__init__("", "", parent)
        self.lay.setContentsMargins(36, 34, 36, 28)
        t = QLabel("OnseiCraft")
        t.setObjectName("H1")
        self.lay.addWidget(t)
        s = QLabel("把日语音声做成中文 LRC 字幕 —— 识别、纠错、翻译、术语统一、质量门禁，一条流水线跑完。")
        s.setObjectName("Muted")
        s.setWordWrap(True)
        self.lay.addWidget(s)
        self.lay.addSpacing(10)

        grid = QHBoxLayout()
        grid.setSpacing(16)
        c1 = CardButton("audio", "音频文件", "拖入音频文件或整个文件夹，开始制作字幕")
        c2 = CardButton("mail", "反馈日志", "挑出不满意的轨道，导出意见 + ASR 原文 + 修后原文")
        c3 = CardButton("stack", "ASR 暂存管理", "查看、删除已暂存的识别原文")
        for c in (c1, c2, c3):
            c.setMinimumHeight(190)
            grid.addWidget(c)
        self.lay.addLayout(grid)
        c1.clicked.connect(self.open_audio)
        c2.clicked.connect(self.open_feedback)
        c3.clicked.connect(self.open_store)

        self.lay.addStretch(1)

        self.lay.addSpacing(8)
        self.env_card = QFrame()
        self.env_card.setObjectName("CardFlat")
        el = QVBoxLayout(self.env_card)
        el.setContentsMargins(18, 14, 18, 14)
        el.setSpacing(6)
        self.env_title = QLabel("运行环境")
        self.env_title.setStyleSheet("font-weight:600;")
        el.addWidget(self.env_title)
        self.env_body = QLabel("")
        self.env_body.setObjectName("Muted")
        self.env_body.setWordWrap(True)
        el.addWidget(self.env_body)
        row = QHBoxLayout()
        self.btn_models = QPushButton("下载缺失的模型")
        self.btn_models.setObjectName("Primary")
        self.btn_models.clicked.connect(self.open_settings)
        self.btn_models.hide()
        row.addWidget(self.btn_models)
        self.btn_settings = QPushButton("设置")
        self.btn_settings.clicked.connect(self.open_settings)
        row.addWidget(self.btn_settings)
        self.btn_out = QPushButton("打开输出目录")
        self.btn_out.clicked.connect(lambda: open_path(settings.load_app()["prod_dir"]))
        row.addWidget(self.btn_out)
        row.addStretch(1)
        el.addLayout(row)
        self.add(self.env_card)
        self.refresh()

    def refresh(self):
        cfg = settings.load_app()
        dev, compute = R.detect_device()
        miss = settings.missing_models()
        lines = []
        lines.append("计算设备：%s（%s）%s" % (
            "GPU / CUDA" if dev == "cuda" else "CPU",
            compute,
            "" if dev == "cuda" else "  ⚠️ 未检测到 CUDA，识别会非常慢"))
        lines.append("输出目录：%s" % cfg["prod_dir"])
        if miss:
            lines.append("缺少 %d 个模型，点「下载缺失的模型」自动装好（约 %.1fGB）：%s"
                         % (len(miss), sum(it["approx"] for it in models.ITEMS),
                            "、".join(m[1].split("（")[0] for m in miss)))
            self.env_title.setStyleSheet("font-weight:600;color:%s;" % theme.WARN)
            self.btn_models.show()
        else:
            lines.append("模型：全部就绪")
            self.env_title.setStyleSheet("font-weight:600;color:%s;" % theme.OK)
            self.btn_models.hide()
        self.env_body.setText("\n".join(lines))


# ---------------------------------------------------------------- 反馈日志
FEEDBACK_KINDS = ("（未分类）", "听错", "人称错", "漏译", "幻觉", "断句", "术语", "其他")


class _FeedbackEntry(QWidget):
    """一条意见：时间点 + 问题类型 + 多行说明 + 删除。"""

    removed = Signal(object)

    def __init__(self, parent=None):
        super(_FeedbackEntry, self).__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.ts = QLineEdit()
        self.ts.setPlaceholderText("00:34.1")
        self.ts.setFixedWidth(88)
        self.ts.setToolTip("这一条对应的时间点；可写 00:34.1 或 34.1")
        self.kind = QComboBox()
        self.kind.addItems(FEEDBACK_KINDS)
        self.kind.setFixedWidth(100)
        self.kind.setToolTip("问题类型：导出时按类聚合")
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("这一行哪里不对？（可以写多行）")
        self.text.setMinimumHeight(56)
        self.text.setMaximumHeight(96)
        btn = QPushButton("－")
        btn.setFixedWidth(30)
        btn.setToolTip("删除这条意见")
        btn.clicked.connect(lambda: self.removed.emit(self))
        row.addWidget(QLabel("时间"))
        row.addWidget(self.ts)
        row.addWidget(self.kind)
        row.addWidget(self.text, 1)
        row.addWidget(btn)

    def value(self):
        return {
            "ts": self.ts.text().strip(),
            "kind": "" if self.kind.currentIndex() == 0 else self.kind.currentText(),
            "comment": self.text.toPlainText().strip(),
        }


class _TrackCard(QGroupBox):
    """一条轨道：勾选标题 + 若干条意见 + 「＋ 添加」。"""

    def __init__(self, name, parent=None):
        super(_TrackCard, self).__init__(parent)
        self.track_name = name
        self.setCheckable(True)
        self.setChecked(False)
        self.setTitle(name)
        self.setToolTip(name)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)
        self.rows = []
        self.box = QVBoxLayout()
        self.box.setSpacing(6)
        lay.addLayout(self.box)
        bar = QHBoxLayout()
        self.btn_add = QPushButton("＋ 添加一条意见")
        self.btn_add.setFixedWidth(148)
        self.btn_add.clicked.connect(self.add_row)
        bar.addWidget(self.btn_add)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.toggled.connect(self._sync)
        self.add_row()

    def add_row(self):
        r = _FeedbackEntry()
        r.removed.connect(self._drop_row)
        self.rows.append(r)
        self.box.addWidget(r)
        self._sync()

    def _drop_row(self, r):
        # 至少留一条空行，避免整卡变成空白
        if len(self.rows) <= 1:
            r.ts.clear()
            r.text.clear()
            r.kind.setCurrentIndex(0)
            return
        self.rows.remove(r)
        r.setParent(None)
        r.deleteLater()

    def _sync(self):
        on = self.isChecked()
        for r in self.rows:
            r.setEnabled(on)
        self.btn_add.setEnabled(on)

    def values(self):
        """勾选了才收；既没时间点也没说明的空条目跳过。"""
        if not self.isChecked():
            return []
        out = []
        for r in self.rows:
            d = r.value()
            if not d["ts"] and not d["comment"]:
                continue
            d["track"] = self.track_name
            out.append(d)
        return out


class FeedbackPage(Page):
    def __init__(self, parent=None):
        super(FeedbackPage, self).__init__(
            "反馈日志",
            "勾选有问题的轨道，一条意见填一个时间点；同一条轨可以点「＋」加多条。"
            "导出文件会带上你的意见、问题类型、ASR 识别原文、修后原文和当前字幕。", parent)
        self.entries = []
        self.cards = []

        top = QHBoxLayout()
        top.addWidget(QLabel("选择作品"))
        self.combo = QComboBox()
        self.combo.setMinimumWidth(420)
        self.combo.currentIndexChanged.connect(self._load_tracks)
        top.addWidget(self.combo)
        btn_reload = QPushButton("刷新")
        btn_reload.clicked.connect(self.reload)
        top.addWidget(btn_reload)
        top.addStretch(1)
        self.lay.addLayout(top)

        self.empty = QLabel("还没有暂存的 ASR 原文。先跑一部作品，并在完成页选「保留 ASR 原文」。")
        self.empty.setObjectName("Muted")
        self.add(self.empty)

        # 轨道卡片放进滚动区：6 轨 × 每轨若干条意见，窗口再高也放得下
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.host = QWidget()
        self.host_lay = QVBoxLayout(self.host)
        self.host_lay.setContentsMargins(0, 0, 8, 0)
        self.host_lay.setSpacing(8)
        self.host_lay.addStretch(1)
        self.scroll.setWidget(self.host)
        self.add(self.scroll, 1)

        opt = QHBoxLayout()
        self.full = QCheckBox("附带全轨原文（不勾选则只给标记时间点附近 ±60 秒）")
        self.full.setChecked(True)
        opt.addWidget(self.full)
        opt.addStretch(1)
        self.btn_export = QPushButton("导出反馈")
        self.btn_export.setObjectName("Primary")
        self.btn_export.clicked.connect(self._export)
        opt.addWidget(self.btn_export)
        self.lay.addLayout(opt)

        self.tip = QLabel("提示：时间点可以写 00:34.1 或 34.1；问题类型用来按类聚合。")
        self.tip.setObjectName("Muted")
        self.add(self.tip)
        self.reload()

    # ---------------------------------------------------------------- 数据
    def reload(self):
        self.entries = store.list_stores()
        self.combo.blockSignals(True)
        self.combo.clear()
        for e in self.entries:
            self.combo.addItem("%s · %s · %d 轨" % (e["project"], e["created_at"], len(e["tracks"])))
        self.combo.blockSignals(False)
        has = bool(self.entries)
        self.empty.setVisible(not has)
        self.scroll.setVisible(has)
        # 隐藏的 widget 仍然占着 addWidget 时给的 stretch，
        # 不把它清零就会在页面中间留一大片空白。
        self.lay.setStretch(self.lay.indexOf(self.scroll), 1 if has else 0)
        self.btn_export.setEnabled(has)
        if has:
            self.combo.setCurrentIndex(0)
            self._load_tracks()

    def _clear_cards(self):
        for c in self.cards:
            self.host_lay.removeWidget(c)
            c.setParent(None)
            c.deleteLater()
        self.cards = []

    def _load_tracks(self):
        i = self.combo.currentIndex()
        if i < 0 or i >= len(self.entries):
            return
        self._clear_cards()
        for tr in self.entries[i]["tracks"]:
            card = _TrackCard(tr["name"])
            self.host_lay.insertWidget(self.host_lay.count() - 1, card)
            self.cards.append(card)

    # ---------------------------------------------------------------- 导出
    def _collect(self):
        items = []
        for c in self.cards:
            items.extend(c.values())
        return items

    def _export(self):
        i = self.combo.currentIndex()
        if i < 0 or i >= len(self.entries):
            return
        e = self.entries[i]
        items = self._collect()
        if not items:
            QMessageBox.information(
                self, "还没有内容",
                "请先勾选有问题的轨道，并至少写一条意见（时间点或说明）。")
            return
        fb_dir = settings.load_app()["feedback_dir"]
        try:
            os.makedirs(fb_dir, exist_ok=True)
            p = os.path.join(fb_dir, "feedback_%s_%s.md" % (
                e["project"], time.strftime("%Y%m%d_%H%M%S")))
            text = feedback.build_report(e, items, full=self.full.isChecked(), fmt="md")
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as ex:      # 不再静默失败：写不出来就当场报出来
            QMessageBox.critical(self, "导出失败", "写入反馈文件时出错：\n%s" % ex)
            return

        self.tip.setText("上次导出：%s（%d 条意见）" % (p, len(items)))
        box = QMessageBox(self)
        box.setWindowTitle("已导出")
        box.setText("反馈已保存：\n%s\n\n共 %d 条意见。把这个文件发给开发者即可。"
                    % (p, len(items)))
        b1 = box.addButton("打开所在文件夹", QMessageBox.ActionRole)
        b2 = box.addButton("另存为…", QMessageBox.ActionRole)
        box.addButton("好", QMessageBox.AcceptRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b1:
            open_path(p)
        elif clicked is b2:
            self._save_as(p)

    def _save_as(self, src):
        p, _sel = QFileDialog.getSaveFileName(
            self, "另存为", src, "Markdown (*.md);;JSON (*.json);;纯文本 (*.txt)")
        if not p:
            return
        low = p.lower()
        fmt = "json" if low.endswith(".json") else ("txt" if low.endswith(".txt") else "md")
        i = self.combo.currentIndex()
        e = self.entries[i] if 0 <= i < len(self.entries) else {}
        try:
            body = feedback.build_report(e, self._collect(),
                                         full=self.full.isChecked(), fmt=fmt)
            with open(p, "w", encoding="utf-8") as f:
                f.write(body)
        except Exception as ex:
            QMessageBox.critical(self, "另存为失败", str(ex))
            return
        open_path(p)


# ---------------------------------------------------------------- ASR 暂存管理
class StorePage(Page):
    def __init__(self, parent=None):
        super(StorePage, self).__init__(
            "ASR 暂存管理",
            "这些是跑完后保留的识别原文与修后原文，用于生成反馈日志。删除后不可恢复。", parent)
        self.entries = []
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["选择", "作品号", "运行时间", "轨道数", "占用", "运行 ID"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 52)
        for c in (1, 2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.Stretch)
        self.add(self.table, 1)

        bar = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setObjectName("Muted")
        bar.addWidget(self.summary)
        bar.addStretch(1)
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self.reload)
        bar.addWidget(btn_refresh)
        self.btn_open = QPushButton("打开暂存目录")
        self.btn_open.clicked.connect(lambda: open_path(settings.load_app()["asr_cache_dir"]))
        bar.addWidget(self.btn_open)
        self.btn_del = QPushButton("删除所选")
        self.btn_del.setObjectName("Danger")
        self.btn_del.clicked.connect(self._delete)
        bar.addWidget(self.btn_del)
        self.lay.addLayout(bar)
        self.reload()

    def reload(self):
        self.entries = store.list_stores()
        self.table.setRowCount(0)
        total = 0
        for e in self.entries:
            total += e["size"]
            r = self.table.rowCount()
            self.table.insertRow(r)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Unchecked)
            self.table.setItem(r, 0, chk)
            for c, v in enumerate([e["project"], e["created_at"], "%d" % len(e["tracks"]),
                                   proj.human_size(e["size"]), e["run_id"]], start=1):
                it = QTableWidgetItem(v)
                it.setFlags(Qt.ItemIsEnabled)
                self.table.setItem(r, c, it)
        self.summary.setText("共 %d 份暂存，占用 %s" % (len(self.entries), proj.human_size(total))
                             if self.entries else "暂无暂存")
        self.btn_del.setEnabled(bool(self.entries))

    def _delete(self):
        idx = [r for r in range(self.table.rowCount())
               if self.table.item(r, 0).checkState() == Qt.Checked]
        if not idx:
            QMessageBox.information(self, "没有选择", "请先勾选要删除的暂存。")
            return
        names = "\n".join("· %s  %s" % (self.entries[r]["project"], self.entries[r]["created_at"])
                          for r in idx)
        res = QMessageBox.question(
            self, "确认删除",
            "将永久删除以下 %d 份暂存（识别原文与修后原文）：\n\n%s\n\n删除后这些作品将无法再生成反馈日志。"
            % (len(idx), names), QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if res != QMessageBox.Yes:
            return
        for r in idx:
            store.delete_store(self.entries[r]["path"])
        self.reload()


# ---------------------------------------------------------------- 设置
class ModelDownloader(QThread):
    """后台下载一个模型。大文件支持断点续传，取消走 Event。"""
    progress = Signal(int, str)
    done = Signal(bool, str)

    def __init__(self, it, endpoint, parent=None):
        super(ModelDownloader, self).__init__(parent)
        self.it = it
        self.endpoint = endpoint
        self.cancel = threading.Event()

    def stop(self):
        self.cancel.set()

    def run(self):
        try:
            def on_prog(done, total, text):
                pct = int(min(100, done * 100.0 / max(1, total)))
                self.progress.emit(pct, "%s  %s / %s" % (
                    text, proj.human_size(done), proj.human_size(total)))

            if self.it["kind"] == "hf":
                path = models.download_hf(self.it, self.endpoint, on_prog, self.cancel)
            else:
                dev, _ = R.detect_device()
                path = models.download_llama(self.it, on_prog, self.cancel, cuda=(dev == "cuda"))
            models.apply_to_config(self.it, path)
            self.done.emit(True, path)
        except models.Cancelled:
            self.done.emit(False, "已取消")
        except Exception as e:
            self.done.emit(False, "%s: %s" % (type(e).__name__, e))


def show_notes_dialog(parent, title, header, notes, ok_text="下载并安装"):
    """展示更新日志。返回 True 表示用户点了 ok_text。

    用 QTextBrowser + setMarkdown，而不是 QMessageBox.setDetailedText——
    后者默认折叠，用户得再点一次「显示详情」才看得到更新内容。
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.resize(620, 440)
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(10)
    h = QLabel(header)
    h.setWordWrap(True)
    lay.addWidget(h)
    tb = QTextBrowser()
    tb.setOpenExternalLinks(True)
    tb.setMarkdown(notes or "（这个版本没有写更新说明）")
    lay.addWidget(tb, 1)
    row = QHBoxLayout()
    row.addStretch(1)
    btn_no = QPushButton("以后再说")
    btn_yes = QPushButton(ok_text)
    btn_yes.setObjectName("Primary")
    row.addWidget(btn_no)
    row.addWidget(btn_yes)
    lay.addLayout(row)
    btn_no.clicked.connect(dlg.reject)
    btn_yes.clicked.connect(dlg.accept)
    return dlg.exec() == QDialog.Accepted


class UpdateWorker(QThread):
    """查更新 / 下安装包。网络操作不能放 UI 线程，否则界面假死。"""
    progress = Signal(int, str)
    checked = Signal(object, str)
    downloaded = Signal(str, str)

    def __init__(self, mode, info=None, parent=None):
        super(UpdateWorker, self).__init__(parent)
        self.mode = mode
        self.info = info
        self.cancel = threading.Event()

    def run(self):
        if self.mode == "check":
            try:
                self.checked.emit(update.check(), "")
            except Exception as e:
                self.checked.emit(None, str(e))
            return
        try:
            def cb(done, total):
                self.progress.emit(int(done * 100 / max(1, total)),
                                   "%s / %s" % (proj.human_size(done), proj.human_size(total)))

            p = update.download(self.info, cb, self.cancel)
            self.downloaded.emit(p, "")
        except Exception as e:
            self.downloaded.emit("", str(e))


class SettingsPage(Page):
    saved = Signal()
    def __init__(self, parent=None):
        super(SettingsPage, self).__init__(
            "设置", "输出目录与模型。模型不在安装包里（约 8GB），"
                    "点「下载」会自动放到安装目录下的 models\\ 并写好配置。", parent)
        self.rows = {}
        self.states = {}
        self.downloader = None
        self.upd_worker = None
        self._update_info = None

        # 输出目录
        self.add(self._section("输出"))
        self.out = self._path_row("字幕输出目录", settings.load_app()["prod_dir"], dir=True)

        self.add(self._section("模型（可一键下载）"))
        for it in models.ITEMS:
            self._model_row(it)

        self.add(self._section("其它模型（旧路线，可留空）"))
        for key, label, kind, hint in settings.MODEL_KEYS:
            if key in ("asr_model", "model_sakura", "llama_dir"):
                continue
            self._path_row(label, settings.get_model(key),
                           dir=(kind == "dir"), key=key, hint=hint)

        # 下载源 + 进度
        src = QHBoxLayout()
        src.addWidget(QLabel("下载源"))
        self.src_combo = QComboBox()
        self.src_combo.addItem("官方（huggingface.co）", "official")
        self.src_combo.addItem("国内镜像（hf-mirror.com）", "mirror")
        src.addWidget(self.src_combo)
        src.addStretch(1)
        self.lay.addLayout(src)

        self.pbar = QProgressBar()
        self.pbar.setRange(0, 100)
        self.pbar.setValue(0)
        self.pbar.hide()
        self.add(self.pbar)
        self.pmsg = QLabel("")
        self.pmsg.setObjectName("Muted")
        self.add(self.pmsg)

        # ---- 关于 / 检查更新 ----
        self.add(self._section("关于"))
        ver_row = QHBoxLayout()
        ver_row.addWidget(QLabel("OnseiCraft %s" % update.__version__))
        self.btn_update = QPushButton("检查更新")
        self.btn_update.clicked.connect(self._check_update)
        ver_row.addWidget(self.btn_update)
        ver_row.addStretch(1)
        self.lay.addLayout(ver_row)

        self.upd_msg = QLabel("")
        self.upd_msg.setObjectName("Muted")
        self.upd_msg.setWordWrap(True)
        self.add(self.upd_msg)

        self.upd_bar = QProgressBar()
        self.upd_bar.setRange(0, 100)
        self.upd_bar.hide()
        self.add(self.upd_bar)

        self.btn_install = QPushButton("下载并安装")
        self.btn_install.setObjectName("Primary")
        self.btn_install.hide()
        self.btn_install.clicked.connect(self._do_update)
        inst_row = QHBoxLayout()
        inst_row.addWidget(self.btn_install)
        inst_row.addStretch(1)
        self.lay.addLayout(inst_row)

        self.add_stretch()
        bar = QHBoxLayout()
        self.device = QLabel("")
        self.device.setObjectName("Muted")
        bar.addWidget(self.device)
        bar.addStretch(1)
        self.btn_save = QPushButton("保存")
        self.btn_save.setObjectName("Primary")
        self.btn_save.clicked.connect(self._save)
        bar.addWidget(self.btn_save)
        self.lay.addLayout(bar)
        self.refresh_device()

    def _section(self, text):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
        lb = QLabel(text)
        lb.setStyleSheet("font-weight:600;color:%s;" % theme.MUTED)
        lay.addWidget(lb)
        lay.addWidget(hline(), 1)
        return w

    def _model_row(self, it):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lb = QLabel(it["label"])
        lb.setFixedWidth(240)
        lb.setToolTip(it["desc"])
        state = QLabel("")
        state.setFixedWidth(76)
        edit = QLineEdit(settings.get_model(it["key"]) or "")
        edit.setPlaceholderText("未设置 —— 点右侧「下载」自动安装")
        btn = QPushButton("下载")
        btn.setFixedWidth(88)
        btn.clicked.connect(lambda: self._start_download(it, edit, btn))
        lay.addWidget(lb)
        lay.addWidget(edit, 1)
        lay.addWidget(state)
        lay.addWidget(btn)
        self.lay.addWidget(w)
        self.rows[it["key"]] = edit
        self.states[it["key"]] = (state, btn)
        self._refresh_state(it, edit)

    def _refresh_state(self, it, edit=None):
        state, btn = self.states[it["key"]]
        p = edit.text().strip() if edit is not None else ""
        ok = bool(p) and os.path.exists(p)
        if not ok:
            ok = models.is_ready(it)
        if ok:
            state.setText("已就绪")
            state.setStyleSheet("color:%s;font-size:12px;" % theme.OK)
            btn.setText("重新下载")
        else:
            state.setText("未安装")
            state.setStyleSheet("color:%s;font-size:12px;" % theme.WARN)
            btn.setText("下载")

    def _start_download(self, it, edit, btn):
        if self.downloader and self.downloader.isRunning():
            QMessageBox.information(self, "正在下载", "上一个模型还没下完，等它结束。")
            return
        endpoint = models.ENDPOINTS[self.src_combo.currentData()]
        res = QMessageBox.question(
            self, "下载模型",
            "将下载：%s\n约 %.1f GB\n保存到：%s\n\n下载源：%s\n\n"
            "大文件支持断点续传，中途关掉下次会接着下。"
            % (it["label"], it["approx"], models.target_path(it), endpoint),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if res != QMessageBox.Yes:
            return
        self.pbar.show()
        self.pbar.setValue(0)
        btn.setEnabled(False)
        self.downloader = ModelDownloader(it, endpoint, self)
        self.downloader.progress.connect(self._on_prog)
        self.downloader.done.connect(lambda ok, msg: self._on_dl_done(it, edit, btn, ok, msg))
        self.downloader.start()
        self.pmsg.setText("开始下载 %s…" % it["label"])

    def _on_prog(self, pct, text):
        self.pbar.setValue(pct)
        self.pmsg.setText(text)

    def _on_dl_done(self, it, edit, btn, ok, msg):
        btn.setEnabled(True)
        self.pbar.setValue(100 if ok else 0)
        if ok:
            p = settings.get_model(it["key"]) or msg
            edit.setText(p)
            self.pmsg.setText("完成：%s" % p)
            self._refresh_state(it, edit)
            self.saved.emit()
        else:
            self.pmsg.setText("失败：%s" % msg)
            QMessageBox.warning(self, "下载失败", msg)

    def _path_row(self, label, value, dir=True, key=None, hint=""):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lb = QLabel(label)
        lb.setFixedWidth(240)
        lb.setToolTip(hint)
        edit = QLineEdit(value or "")
        edit.setPlaceholderText(hint)
        btn = QPushButton("浏览")
        btn.setFixedWidth(64)
        btn.clicked.connect(lambda: self._browse(edit, dir))
        lay.addWidget(lb)
        lay.addWidget(edit, 1)
        lay.addWidget(btn)
        self.lay.addWidget(w)
        self.rows[key or "_out"] = edit
        return edit

    def _browse(self, edit, is_dir):
        if is_dir:
            p = QFileDialog.getExistingDirectory(self, "选择目录", edit.text() or "")
        else:
            p, _ = QFileDialog.getOpenFileName(self, "选择文件", edit.text() or "")
        if p:
            edit.setText(p.replace("\\", "/"))

    def refresh_device(self):
        dev, compute = R.detect_device()
        self.device.setText("计算设备：%s（%s）" % (
            "GPU / CUDA" if dev == "cuda" else "CPU（识别会很慢）", compute))

    # ---- 检查更新 ----
    def _check_update(self):
        if self.upd_worker and self.upd_worker.isRunning():
            return
        self.btn_update.setEnabled(False)
        self.upd_msg.setText("正在检查…")
        self.btn_install.hide()
        self.upd_worker = UpdateWorker("check", parent=self)
        self.upd_worker.checked.connect(self._on_checked)
        self.upd_worker.start()

    def _on_checked(self, info, err):
        self.btn_update.setEnabled(True)
        if err:
            self.upd_msg.setText("检查失败：%s" % err)
            return
        if not info:
            self.upd_msg.setText("已是最新版本（%s）。" % update.__version__)
            self.btn_install.hide()
            return
        self._update_info = info
        self.upd_msg.setText(
            "发现新版本 %s（%s）。" % (
                info["tag"], proj.human_size(info["size"]) if info["size"] else "大小未知"))
        self.btn_install.show()
        # 直接把更新日志摆出来，而不是让用户自己找
        if show_notes_dialog(
                self, "发现新版本 %s" % info["tag"],
                "OnseiCraft %s 可用（%s）。\n"
                "更新只替换程序文件——模型、输出目录、已生成的字幕都不会动，不用重新配置。"
                % (info["tag"], proj.human_size(info["size"]) if info["size"] else "大小未知"),
                info["notes"]):
            self._do_update()

    def _do_update(self):
        if not self._update_info:
            return
        self.btn_install.setEnabled(False)
        self.upd_bar.show()
        self.upd_bar.setValue(0)
        self.upd_msg.setText("正在下载…")
        self.upd_worker = UpdateWorker("download", self._update_info, parent=self)
        self.upd_worker.progress.connect(self._on_upd_prog)
        self.upd_worker.downloaded.connect(self._on_downloaded)
        self.upd_worker.start()

    def _on_upd_prog(self, pct, text):
        self.upd_bar.setValue(pct)
        self.upd_msg.setText(text)

    def _on_downloaded(self, path, err):
        self.btn_install.setEnabled(True)
        if err:
            self.upd_msg.setText("下载失败：%s" % err)
            return
        r = QMessageBox.question(
            self, "准备安装",
            "新版已下载完成。\n\n点「是」会关闭 OnseiCraft 并自动安装，装完自动打开新版。\n"
            "模型、配置、已生成的字幕都不会动。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if r != QMessageBox.Yes:
            self.upd_msg.setText("已下载到：%s（可稍后手动运行）" % path)
            return
        # 记一笔「从哪个版本升到哪个版本 + 更新内容」，新版首次启动时弹出来给用户看
        try:
            import json
            pend = os.path.join(settings.ROOT, "data", "update_pending.json")
            os.makedirs(os.path.dirname(pend), exist_ok=True)
            json.dump({"from": update.__version__,
                       "to": (self._update_info or {}).get("tag", ""),
                       "notes": (self._update_info or {}).get("notes", "")},
                      open(pend, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception:
            pass
        update.install_and_restart(path)
        QApplication.quit()

    def _save(self):
        cfg = settings.load_app()
        cfg["prod_dir"] = self.rows["_out"].text().strip() or cfg["prod_dir"]
        settings.save_app(cfg)
        os.makedirs(cfg["prod_dir"], exist_ok=True)
        for key, label, kind, hint in settings.MODEL_KEYS:
            settings.set_model(key, self.rows[key].text().strip())
        # 输出目录改了要同步到 pipeline 的 paths.PROD（它是 import 时的常量）
        try:
            from pipeline_lib import paths
            paths.PROD = cfg["prod_dir"]
        except Exception:
            pass
        self.saved.emit()
        QMessageBox.information(self, "已保存", "设置已保存。")
