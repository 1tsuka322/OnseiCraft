# -*- mode: python ; coding: utf-8 -*-
"""OnseiCraft 打包配置（PyInstaller onedir）。

为什么要 onedir 而不是 onefile：
onefile 每次启动都要把 ~1GB 解压到临时目录（含 CUDA DLL），冷启动 20 秒以上；
而且管线每跑一个阶段都要起一次子进程（`OnseiCraft.exe asr.py ...`），
onefile 会把这个代价乘以 6 次。onedir 只在首次加载 DLL。

体积主要来自 ctranslate2 自带的 cuBLAS/cuDNN（cublasLt 638MB）。
不打包它 GPU 推理就起不来；模型本身**不进包**（whisper 3GB + Sakura 4GB）。

    python -m PyInstaller --noconfirm work/app/OnseiCraft.spec
"""
import os

from PyInstaller.utils.hooks import collect_all, collect_data_files

HERE = os.path.dirname(os.path.abspath(SPEC))
APP = HERE                      # work/app
WORK = os.path.abspath(os.path.join(HERE, ".."))

datas = [(os.path.join(APP, "studio", "assets"), "studio/assets")]
binaries = []
hiddenimports = [
    "pipeline_lib", "pipeline_lib.cli", "pipeline_lib.stages", "pipeline_lib.manifest",
    "pipeline_lib.fingerprint", "pipeline_lib.knowledge", "pipeline_lib.queue",
    "pipeline_lib.paths", "pipeline_lib.selftest",
]

# 管线脚本是**子进程 runpy 执行**的，PyInstaller 静态分析看不到它们的 import，
# 必须显式收集，否则 01_asr 一跑就 ModuleNotFoundError。
for pkg in ("faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub",
            "onnxruntime", "av", "tqdm", "pypdf", "pdfplumber", "pdfminer"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:
        print("⚠️ collect_all(%s) 失败：%s" % (pkg, e))

# 排除掉用不到的大件。torch 只被 asr.py 用来 empty_cache()，那里已经 try/except，
# 缺了不影响（打包后 539MB 的 torch 目录不进安装包）。
excludes = [
    "torch", "torchvision", "torchaudio", "torchgen",
    "matplotlib", "scipy", "pandas", "IPython", "notebook", "jupyter",
    # 这四件合计约 285MB，管线运行时用不到（被某个 hook 顺带拖进来的）：
    # llvmlite/numba 来自 transformers → sklearn 的链；cv2 来自图像相关 hook。
    "llvmlite", "numba", "cv2", "sklearn", "transformers",
    "pytest", "sphinx", "tkinter",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets", "PySide6.QtQml",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtUiTools", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtNetworkAuth",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
]

a = Analysis(
    [os.path.join(APP, "main.py")],
    pathex=[APP, os.path.join(WORK, "pipeline")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OnseiCraft",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 纯 GUI 程序：双击不弹任何控制台。
    # 代价是 sys.stdout/stderr 为 None，由 main.py 的 _ensure_streams() 用
    # os.fdopen(1) 把子进程管道的句柄拿回来（管线靠 PIPE 抓 asr.py 的日志）。
    # 早先为了省事用了 console=True + ShowWindow 藏窗口，结果是双击会闪一个黑框，
    # 任务栏还留个控制台——看起来就像「启动了个脚本」，用户不会接受。
    console=False,
    icon=os.path.join(APP, "studio", "assets", "OnseiCraft.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="OnseiCraft",
)

# ---------------------------------------------------------------- 收尾瘦身
# excludes 管不住「被 hook 注入的 hiddenimports」，这几件还是会进 _internal。
# 实测管线运行时一个都不需要（llvmlite/numba/sklearn 是 transformers→sklearn 链拖进来的，
# cv2 是图像 hook），删掉省约 300MB。删完必须实跑一遍 01-06 验证，别只看体积。
import shutil  # noqa: E402

_DROP = ("llvmlite", "llvmlite.libs", "numba", "cv2", "transformers", "sklearn",
         "scipy", "scipy.libs", "matplotlib", "pandas")
for _d in _DROP:
    _p = os.path.join(DISTPATH, "OnseiCraft", "_internal", _d)
    if os.path.isdir(_p):
        shutil.rmtree(_p, ignore_errors=True)
        print("dropped _internal/%s" % _d)

# 顺带把 work/ 铺到 exe 旁边：这样 dist 目录本身就是「绿色版」，解压即用。
# 安装包（installer.iss）会 Excludes 掉这份、改用源目录复制，避免重复。
_work_dst = os.path.join(DISTPATH, "OnseiCraft", "work")
if not os.path.isdir(_work_dst):
    os.makedirs(_work_dst, exist_ok=True)
    _ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.log", "pipeline.local.json")
    for _sub in ("scripts", "pipeline", "presets"):
        _src = os.path.join(WORK, _sub)
        if os.path.isdir(_src):
            shutil.copytree(_src, os.path.join(_work_dst, _sub), ignore=_ignore)
    print("work/ staged for portable use")
