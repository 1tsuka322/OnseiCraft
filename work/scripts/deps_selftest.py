# -*- coding: utf-8 -*-
"""打包自检：在 **exe 环境里** 验证管线依赖齐不齐。

为什么要这个：管线脚本是子进程 `OnseiCraft.exe xxx.py` 用 runpy 执行的，
PyInstaller 的静态分析**看不到它们的 import**，只能靠 `OnseiCraft.spec` 里
那份 collect_all 白名单。白名单漏一个，症状是「GUI 好好的，跑到 02 阶段才
ModuleNotFoundError」——2026-10-31 就这么漏了 7 个包（janome / fugashi /
unidic_lite / jieba / rapidfuzz / pypinyin / joblib / zhconv）。

用法（打包后、发布前必须跑）：
    OnseiCraft.exe work/scripts/deps_selftest.py
    OnseiCraft.exe work/scripts/deps_selftest.py D:\\某台本.pdf   # 顺带测 PDF 提取
"""
import os
import sys

OK, BAD = [], []


def check(name, fn):
    try:
        fn()
        OK.append(name)
    except Exception as e:
        BAD.append((name, type(e).__name__, str(e)[:110]))


# ---------------------------------------------------------------- 1. 能不能 import
MODULES = [
    # 识别 / 翻译
    "faster_whisper", "ctranslate2", "av", "onnxruntime", "tokenizers", "huggingface_hub",
    # 日语 / 中文处理
    "janome", "fugashi", "unidic_lite", "jieba", "pypinyin", "zhconv",
    # 通用
    "numpy", "rapidfuzz", "joblib", "tqdm",
    # 台本
    "pypdf", "pdfplumber", "pdfminer",
    # 界面
    "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
]

for _m in MODULES:
    check("import %s" % _m, (lambda m: lambda: __import__(m))(_m))


# ---------------------------------------------------------------- 2. 能不能真用
def _janome():
    from janome.tokenizer import Tokenizer
    assert list(Tokenizer().tokenize("これはテストです"))


def _fugashi():
    import fugashi
    tg = fugashi.Tagger()
    assert list(tg("これはテストです"))


def _jieba():
    import jieba
    assert jieba.lcut("这是测试")


def _pinyin():
    import pypinyin
    assert pypinyin.lazy_pinyin("测试")


def _zhconv():
    import zhconv
    assert zhconv.convert("测试", "zh-tw")


def _fuzz():
    from rapidfuzz import fuzz
    assert fuzz.ratio("abc", "abc") == 100


def _pdf():
    import pdfplumber
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        raise RuntimeError("没给 PDF 路径（跳过我）")
    with pdfplumber.open(path) as pdf:
        txt = pdf.pages[0].extract_text() or ""
    assert txt.strip(), "PDF 提取为空"


for _n, _f in (("janome 分词", _janome), ("fugashi 分词", _fugashi), ("jieba 分词", _jieba),
               ("pypinyin 注音", _pinyin), ("zhconv 繁简", _zhconv),
               ("rapidfuzz 匹配", _fuzz), ("pdfplumber 提取", _pdf)):
    check(_n, _f)


# ---------------------------------------------------------------- 3. 知识表在不在
def _presets():
    from rootloc import ROOT
    d = os.path.join(ROOT, "presets")
    need = ("glossary.json", "do_not_change.json", "coverage.json")
    miss = [f for f in need if not os.path.exists(os.path.join(d, f))]
    assert not miss, "缺 %s" % miss


check("presets 知识表", _presets)


# ---------------------------------------------------------------- 报告
print("=" * 60)
for n in OK:
    print("  OK    %s" % n)
for n, t, e in BAD:
    if n == "pdfplumber 提取" and "没给 PDF 路径" in e:
        print("  SKIP  %s（未提供 PDF）" % n)
        continue
    print("  FAIL  %-22s %s: %s" % (n, t, e))
print("=" * 60)
print("%d 通过 / %d 失败" % (len(OK), len(BAD)))
sys.exit(1 if BAD else 0)
