# -*- coding: utf-8 -*-
"""通用控件：拖拽区、步骤条、阶段行、可点击卡片。"""
import os

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget, QFileDialog)

from . import settings, theme


class IconBadge(QWidget):
    """手绘线性图标（不依赖 emoji 字体，任何机器都不会显示成豆腐块）。"""

    def __init__(self, kind, size=46, parent=None):
        super(IconBadge, self).__init__(parent)
        self.kind = kind
        self.setFixedSize(size, size)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width()
        s = w / 46.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#FBEEE8"))
        p.drawRoundedRect(QRectF(0, 0, w, w), 12 * s, 12 * s)
        pen = QPen(QColor(theme.ACCENT), 2.4 * s)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        c = w / 2.0
        if self.kind == "audio":
            for i, h in enumerate((10, 20, 26, 16, 8)):
                x = c - 14 * s + i * 7 * s
                p.drawLine(QPointF(x, c - h * s / 2), QPointF(x, c + h * s / 2))
        elif self.kind == "mail":
            r = QRectF(c - 15 * s, c - 10 * s, 30 * s, 21 * s)
            p.drawRoundedRect(r, 3 * s, 3 * s)
            p.drawPolyline([QPointF(r.left() + 2 * s, r.top() + 3 * s),
                            QPointF(c, c + 3 * s),
                            QPointF(r.right() - 2 * s, r.top() + 3 * s)])
        elif self.kind == "stack":
            for i in range(3):
                y = c - 11 * s + i * 9 * s
                p.drawRoundedRect(QRectF(c - 14 * s, y, 28 * s, 5 * s), 2 * s, 2 * s)
        elif self.kind == "script":
            path = QPainterPath()
            path.moveTo(c - 11 * s, c - 15 * s)
            path.lineTo(c + 4 * s, c - 15 * s)
            path.lineTo(c + 11 * s, c - 8 * s)
            path.lineTo(c + 11 * s, c + 15 * s)
            path.lineTo(c - 11 * s, c + 15 * s)
            path.closeSubpath()
            p.drawPath(path)
            p.drawLine(QPointF(c + 4 * s, c - 15 * s), QPointF(c + 4 * s, c - 8 * s))
            p.drawLine(QPointF(c + 4 * s, c - 8 * s), QPointF(c + 11 * s, c - 8 * s))
        elif self.kind == "check":
            p.drawPolyline([QPointF(c - 12 * s, c + 1 * s),
                            QPointF(c - 3 * s, c + 10 * s),
                            QPointF(c + 13 * s, c - 10 * s)])
        elif self.kind == "warn":
            p.drawPolyline([QPointF(c, c - 13 * s),
                            QPointF(c + 14 * s, c + 11 * s),
                            QPointF(c - 14 * s, c + 11 * s),
                            QPointF(c, c - 13 * s)])
            p.drawLine(QPointF(c, c - 3 * s), QPointF(c, c + 3 * s))
            p.drawPoint(QPointF(c, c + 7 * s))
        p.end()

    def set_kind(self, kind):
        self.kind = kind
        self.update()


class DropZone(QFrame):
    """拖入文件/文件夹，或点击选择。"""
    dropped = Signal(list)

    def __init__(self, title, hint, mode="audio", parent=None):
        super(DropZone, self).__init__(parent)
        self.mode = mode
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(190)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(8)

        self.icon = IconBadge("audio" if mode == "audio" else "script", 54)
        self.t1 = QLabel(title)
        self.t1.setObjectName("H2")
        self.t1.setAlignment(Qt.AlignCenter)
        self.t2 = QLabel(hint)
        self.t2.setObjectName("Muted")
        self.t2.setAlignment(Qt.AlignCenter)
        self.t2.setWordWrap(True)
        for w in (self.icon, self.t1, self.t2):
            lay.addWidget(w)

    # ---- 拖拽 ----
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._set_drag(True)

    def dragLeaveEvent(self, e):
        self._set_drag(False)

    def dropEvent(self, e):
        self._set_drag(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.dropped.emit(paths)

    def _set_drag(self, on):
        self.setProperty("drag", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, e):
        if self.mode == "audio":
            f, _ = QFileDialog.getOpenFileNames(
                self, "选择音频文件", "", "音频 (*.mp3 *.wav *.flac *.m4a *.ogg *.opus)")
            if not f:
                d = QFileDialog.getExistingDirectory(self, "或选择音频文件夹")
                f = [d] if d else []
        else:
            f, _ = QFileDialog.getOpenFileNames(
                self, "选择台本文件", "", "台本 (*.txt *.pdf)")
            if not f:
                d = QFileDialog.getExistingDirectory(self, "或选择台本文件夹")
                f = [d] if d else []
        if f:
            self.dropped.emit(f)


class StepBar(QWidget):
    """1 音频 → 2 台本 → 3 生成"""
    def __init__(self, steps, parent=None):
        super(StepBar, self).__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addStretch(1)
        self.labels = []
        for i, s in enumerate(steps):
            lb = QLabel("%d  %s" % (i + 1, s))
            lb.setAlignment(Qt.AlignCenter)
            lb.setFixedHeight(34)
            lb.setMinimumWidth(104)
            lay.addWidget(lb)
            self.labels.append(lb)
            if i < len(steps) - 1:
                arrow = QLabel("→")
                arrow.setStyleSheet("color:%s;font-size:14px;" % theme.BORDER)
                lay.addWidget(arrow)
        lay.addStretch(1)
        self.set_step(0)

    def set_step(self, idx):
        for i, lb in enumerate(self.labels):
            if i < idx:
                lb.setStyleSheet("color:%s;font-weight:600;" % theme.OK)
            elif i == idx:
                lb.setStyleSheet("background:%s;color:#fff;border-radius:8px;"
                                 "font-weight:600;padding:2px 14px;" % theme.ACCENT)
            else:
                lb.setStyleSheet("color:%s;padding:2px 14px;" % theme.MUTED)


class StageRow(QFrame):
    """一个阶段：状态色块 + 名称 + 说明（色块而非字符，避免字体缺字）"""

    def __init__(self, name, label, parent=None):
        super(StageRow, self).__init__(parent)
        self.setObjectName("CardFlat")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        self.dot = QLabel("")
        self.dot.setFixedSize(10, 10)
        self.dot.setStyleSheet("background:%s;border-radius:5px;" % theme.BORDER)
        self.name = QLabel(label)
        self.name.setStyleSheet("font-weight:600;")
        self.detail = QLabel("等待")
        self.detail.setObjectName("Muted")
        self.detail.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.dot)
        lay.addSpacing(8)
        lay.addWidget(self.name)
        lay.addStretch(1)
        lay.addWidget(self.detail)

    def set_state(self, state, text=""):
        color = {"wait": theme.BORDER, "running": theme.ACCENT, "ok": theme.OK,
                 "failed": theme.ERR, "skip": theme.MUTED}.get(state, theme.MUTED)
        self.dot.setStyleSheet("background:%s;border-radius:5px;" % color)
        self.detail.setText(text or {"wait": "等待", "running": "进行中…",
                                     "ok": "完成", "failed": "失败", "skip": "跳过"}[state])
        self.detail.setStyleSheet(
            "color:%s;" % (theme.ERR if state == "failed" else theme.MUTED))


class CardButton(QFrame):
    """首页三张卡片：图标 + 标题 + 说明，整块可点。"""
    clicked = Signal()

    def __init__(self, icon, title, desc, parent=None):
        super(CardButton, self).__init__(parent)
        self.setObjectName("Card")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(8)
        lay.addStretch(1)
        ic = IconBadge(icon, 46)
        t = QLabel(title)
        t.setObjectName("H2")
        d = QLabel(desc)
        d.setObjectName("Muted")
        d.setWordWrap(True)
        lay.addWidget(ic)
        lay.addWidget(t)
        lay.addWidget(d)
        lay.addStretch(1)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super(CardButton, self).mouseReleaseEvent(e)


def hline():
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet("background:%s;" % theme.BORDER)
    return f


def open_path(path):
    """在资源管理器里打开（文件则选中它）。"""
    try:
        if os.path.isdir(path):
            os.startfile(path)
        elif os.path.exists(path):
            os.startfile(os.path.dirname(path))
        else:
            os.startfile(os.path.dirname(path))
    except Exception:
        pass


class Toast(QWidget):
    """右下角浮动提示。用于「出错了 → 导出日志」这种需要随手点的动作。

    为什么不用 QMessageBox：模态弹窗会挡住用户本来要做的事，
    而且关掉之后就没有入口了。这里常驻到用户主动关或下一次成功运行。
    """

    action = Signal()

    def __init__(self, parent, text, btn_text="导出日志", kind="err"):
        super(Toast, self).__init__(parent)
        self.setObjectName("Toast")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._kind = kind
        color = theme.ERR if kind == "err" else theme.WARN
        self.setStyleSheet(
            "QWidget#Toast{background:%s;border-radius:10px;}"
            "QLabel#ToastText{color:#FFFFFF;font-size:12px;}"
            "QPushButton#ToastBtn{background:rgba(255,255,255,0.16);color:#FFFFFF;"
            "border:none;border-radius:6px;padding:5px 12px;font-size:12px;}"
            "QPushButton#ToastBtn:hover{background:rgba(255,255,255,0.28);}"
            "QPushButton#ToastX{background:transparent;color:rgba(255,255,255,0.7);"
            "border:none;font-size:14px;padding:0 4px;}"
            "QPushButton#ToastX:hover{color:#FFFFFF;}" % color)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 10)
        lay.setSpacing(10)
        lb = QLabel(text)
        lb.setObjectName("ToastText")
        lay.addWidget(lb)
        self.btn = QPushButton(btn_text)
        self.btn.setObjectName("ToastBtn")
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.clicked.connect(self.action.emit)
        lay.addWidget(self.btn)
        x = QPushButton("×")
        x.setObjectName("ToastX")
        x.setCursor(Qt.PointingHandCursor)
        x.setFixedWidth(22)
        x.clicked.connect(self.hide)
        lay.addWidget(x)

        self.adjustSize()
        self.hide()

    def set_text(self, text, btn_text=None):
        lb = self.findChild(QLabel, "ToastText")
        if lb:
            lb.setText(text)
        if btn_text:
            self.btn.setText(btn_text)
        self.adjustSize()
        self.reposition()

    def popup(self):
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()

    def reposition(self):
        p = self.parentWidget()
        if not p:
            return
        self.move(max(8, p.width() - self.width() - 22),
                  max(8, p.height() - self.height() - 22))
