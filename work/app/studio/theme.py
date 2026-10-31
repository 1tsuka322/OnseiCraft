# -*- coding: utf-8 -*-
"""界面样式（浅色、卡片式）。颜色集中在这里，改主题只改这一处。"""
import os
import sys

from PySide6.QtGui import QFontDatabase

BG = "#F7F5F2"
CARD = "#FFFFFF"
BORDER = "#E5E0D8"
TEXT = "#2B2723"
MUTED = "#8A8178"
ACCENT = "#C15F3C"
ACCENT_DARK = "#A94E2E"
OK = "#3F7D4E"
WARN = "#B7791F"
ERR = "#B23B3B"

QSS = """
QWidget {
    background: %(BG)s;
    color: %(TEXT)s;
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog { background: %(BG)s; }
/* QLabel 默认继承 QWidget 的米色背景，放在白卡片上会显出一条灰底 —— 必须透明 */
QLabel { background: transparent; }
QCheckBox { background: transparent; }

QLabel#H1 { font-size: 26px; font-weight: 600; }
QLabel#H2 { font-size: 18px; font-weight: 600; }
QLabel#Muted { color: %(MUTED)s; }
QLabel#StepNum { color: %(MUTED)s; font-size: 12px; }

QFrame#Card {
    background: %(CARD)s;
    border: 1px solid %(BORDER)s;
    border-radius: 14px;
}
QFrame#Card:hover { border: 1px solid %(ACCENT)s; }
QFrame#CardFlat {
    background: %(CARD)s;
    border: 1px solid %(BORDER)s;
    border-radius: 12px;
}
QFrame#DropZone {
    background: %(CARD)s;
    border: 2px dashed %(BORDER)s;
    border-radius: 14px;
}
QFrame#DropZone[drag="true"] {
    border: 2px dashed %(ACCENT)s;
    background: #FFF7F2;
}

QPushButton {
    background: %(CARD)s;
    border: 1px solid %(BORDER)s;
    border-radius: 8px;
    padding: 7px 16px;
}
QPushButton:hover { border-color: %(ACCENT)s; color: %(ACCENT)s; }
QPushButton:disabled { color: #BDB6AD; border-color: #EDE9E3; }

QPushButton#Primary {
    background: %(ACCENT)s;
    color: white;
    border: 1px solid %(ACCENT)s;
    font-weight: 600;
    padding: 9px 22px;
}
QPushButton#Primary:hover { background: %(ACCENT_DARK)s; border-color: %(ACCENT_DARK)s; }
QPushButton#Primary:disabled { background: #E0D6CF; border-color: #E0D6CF; color: #FFFFFF; }

QPushButton#Ghost {
    background: transparent;
    border: none;
    color: %(MUTED)s;
    padding: 6px 10px;
}
QPushButton#Ghost:hover { color: %(ACCENT)s; }

QPushButton#Danger { color: %(ERR)s; border-color: #E7C9C9; }
QPushButton#Danger:hover { background: #FDF3F3; border-color: %(ERR)s; }

QProgressBar {
    background: #EDE8E1;
    border: none;
    border-radius: 6px;
    height: 12px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk { background: %(ACCENT)s; border-radius: 6px; }

QPlainTextEdit, QTextEdit, QListWidget, QTableWidget {
    background: %(CARD)s;
    border: 1px solid %(BORDER)s;
    border-radius: 10px;
    selection-background-color: #F3D9CE;
    selection-color: %(TEXT)s;
}
QPlainTextEdit#Log {
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 12px;
    color: #4A443E;
}
QHeaderView::section {
    background: #FBF9F6;
    border: none;
    border-bottom: 1px solid %(BORDER)s;
    padding: 7px 8px;
    color: %(MUTED)s;
    font-weight: 600;
}
QTableWidget { gridline-color: #F0ECE6; }
QTableWidget::item { padding: 5px; }
QLineEdit, QComboBox, QSpinBox {
    background: %(CARD)s;
    border: 1px solid %(BORDER)s;
    border-radius: 8px;
    padding: 6px 10px;
}
QLineEdit:focus, QComboBox:focus { border-color: %(ACCENT)s; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #CFC8BF; border-radius: 4px; background: white;
}
QCheckBox::indicator:checked { background: %(ACCENT)s; border-color: %(ACCENT)s; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #D9D2C9; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #C3BAAE; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QToolTip {
    background: #2B2723; color: #FFF; border: none; padding: 6px 8px; border-radius: 6px;
}
""" % {"BG": BG, "CARD": CARD, "BORDER": BORDER, "TEXT": TEXT, "MUTED": MUTED,
       "ACCENT": ACCENT, "ACCENT_DARK": ACCENT_DARK, "ERR": ERR, "WARN": WARN, "OK": OK}


def apply(app):
    app.setStyleSheet(QSS)
    # 中文字体回退：微软雅黑在 Win 上一定有
    fam = ["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"]
    have = set(QFontDatabase.families())
    for f in fam:
        if f in have:
            f0 = app.font()
            f0.setFamily(f)
            app.setFont(f0)
            break
