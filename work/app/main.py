# -*- coding: utf-8 -*-
"""OnseiCraft 启动入口。

三种身份共用一个 exe：
1. **GUI**（无参数）：正常打开界面
2. **脚本转发**（第一个参数是 `xxx.py`）：管线阶段是子进程调 `[python, script.py, ...]`，
   打包后没有 python 了，`paths.python_exe()` 返回的就是本 exe，所以要在这里把参数
   转给 `work/scripts/` 下的脚本执行。少了这段，01_asr 会「启动一个 GUI」然后卡死。
3. **`-c`**（`manifest.env_rec` 会调 `[python, "-c", "print(version)"]`）：只回显版本号。

⚠️ 顺序很重要：`KIKOERU_ROOT` / `KIKOERU_CONFIG` 必须在 `import pipeline_lib` **之前**注入。
`paths.py` 里的 ROOT/PROD/CONFIG 是 import 时求值的常量，晚一步就改不动——
表现是「设置里改了输出目录，成品还进老地方」。
"""
import os
import sys

FROZEN = getattr(sys, "frozen", False)


def _ensure_streams():
    """windowed（无控制台）模式下 `sys.stdout` / `sys.stderr` 是 None。

    但管线是「把 exe 当 python 用」跑子进程的（`OnseiCraft.exe asr.py …`），
    父进程用 PIPE 接子进程输出——子进程必须有一个能写的 stdout，否则日志全丢、
    `print` 还会抛 AttributeError 把阶段搞挂。

    `os.fdopen(1)` 能拿回父进程给的管道句柄；拿不到（真没有控制台也没有管道）
    就退到 devnull，保证 print 不崩。
    """
    for name, fd in (("stdout", 1), ("stderr", 2)):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, os.fdopen(fd, "w", encoding="utf-8",
                                             errors="replace", buffering=1))
            except Exception:
                try:
                    setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
                except Exception:
                    pass


if FROZEN:
    _ensure_streams()
    # exe 所在目录（安装目录）。work/ 与它同级，由安装包铺好。
    HOME = os.path.dirname(os.path.abspath(sys.executable))
    ROOT = os.path.join(HOME, "work")
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    # ⚠️ 必须在**任何**子进程启动之前设好：阶段脚本（rootloc.py）靠它算路径，
    # 读不到就会按脚本位置反推 → 在打包布局里差一层，写到安装目录外面去。
    os.environ.setdefault("KIKOERU_ROOT", ROOT)
    os.environ.setdefault("KIKOERU_CONFIG", os.path.join(ROOT, "pipeline", "pipeline.json"))
else:
    # 开发模式：本文件在 work/app/，ROOT 是上一级 work
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    ROOT = os.path.abspath(os.path.join(APP_DIR, ".."))

VERSION = "0.1.2"


def _run_stage_script(arg):
    """把 exe 当 python 用：`OnseiCraft.exe <脚本> <参数...>`。"""
    import runpy
    name = os.path.basename(arg)
    for sub in ("scripts", "pipeline"):
        p = os.path.join(ROOT, sub)
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    # scripts/ 优先，其次 pipeline/（后者是 CLI 入口 pipeline.py）
    path = None
    for sub in ("scripts", "pipeline"):
        cand = os.path.join(ROOT, sub, name)
        if os.path.exists(cand):
            path = cand
            break
    if not path:
        sys.stderr.write("找不到脚本：%s（在 %s/{scripts,pipeline} 下都没有）\n" % (name, ROOT))
        return 2
    sys.argv = [path] + sys.argv[2:]
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


if FROZEN and len(sys.argv) >= 2:
    _a = sys.argv[1]
    if _a == "-c":
        print(sys.version.split()[0])
        sys.exit(0)
    if _a.endswith(".py"):
        sys.exit(_run_stage_script(_a))

# 到这里才是 GUI
os.environ.setdefault("KIKOERU_ROOT", ROOT)
os.environ.setdefault("KIKOERU_CONFIG", os.path.join(ROOT, "pipeline", "pipeline.json"))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from studio import settings  # noqa: E402

settings.apply_env()

from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from studio import theme  # noqa: E402
from studio.main_window import MainWindow  # noqa: E402


def _icon():
    for p in (os.path.join(APP_DIR, "studio", "assets", "icon.png"),
              os.path.join(APP_DIR, "studio", "assets", "icon_256.png")):
        if os.path.exists(p):
            return QIcon(p)
    return QIcon()


def _install_crash_log():
    """无控制台时崩溃是「静默消失」。把未捕获异常写进日志文件，用户能发给我们。"""
    if not FROZEN:
        return
    import traceback

    def hook(exc_type, exc, tb):
        try:
            d = os.path.join(ROOT, "data", "studio_logs")
            os.makedirs(d, exist_ok=True)
            import time
            p = os.path.join(d, "crash_%s.log" % time.strftime("%Y%m%d_%H%M%S"))
            with open(p, "w", encoding="utf-8") as f:
                f.write("OnseiCraft %s\nROOT=%s\n\n" % (VERSION, ROOT))
                f.write("".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook


def main():
    app = QApplication(sys.argv)
    if not os.path.isdir(ROOT):
        QMessageBox.critical(
            None, "安装不完整",
            "找不到程序数据目录：\n%s\n\n请重新运行安装程序。" % ROOT)
        return 1
    _install_crash_log()
    app.setApplicationName("OnseiCraft")
    app.setApplicationVersion(VERSION)
    app.setWindowIcon(_icon())
    theme.apply(app)
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
