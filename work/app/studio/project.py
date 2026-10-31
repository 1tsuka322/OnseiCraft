# -*- coding: utf-8 -*-
"""音频归集、作品号推断、台本（txt/pdf）转换。

管线只认「一个音频目录 + 一个可选台本目录」，而用户拖进来的是零散文件或
一个带作品名的文件夹。这一层负责把用户意图翻译成管线能吃的形状，
并且**不复制大文件**（同盘优先硬链接，跨盘才真复制）。
"""
import glob
import os
import re
import shutil
import time

from . import settings

RJ_RE = re.compile(r"RJ\s*0*(\d{5,})", re.I)


def list_audio(path):
    """列出目录下的音频（只看顶层，与 asr.py 默认非递归一致）。"""
    out = []
    for f in sorted(glob.glob(os.path.join(glob.escape(path), "*"))):
        if os.path.isfile(f) and f.lower().endswith(settings.AUDIO_EXTS):
            out.append(f)
    return out


def infer_project(name):
    """从目录名/文件名里抠 RJ 号；抠不到就用时间戳造一个合法的本地号。"""
    m = RJ_RE.search(name or "")
    if m:
        return "RJ" + m.group(1)
    return "RJ9" + time.strftime("%m%d%H%M")


def _link_or_copy(src, dst):
    if os.path.exists(dst):
        return dst
    try:
        os.link(src, dst)          # 同盘硬链接：零拷贝、零额外空间
        return dst
    except Exception:
        pass
    try:
        shutil.copy2(src, dst)
    except Exception:
        return None
    return dst


def prepare_source(dropped, project=None, work_root=None):
    """把用户拖入的东西变成一个音频目录。

    dropped: 单个目录 / 单个文件 / 多个文件（路径列表）
    返回 dict(source, project, files, n_files, total_bytes, copied, note)
    """
    if isinstance(dropped, str):
        dropped = [dropped]
    dropped = [os.path.abspath(p) for p in dropped if p and os.path.exists(p)]
    if not dropped:
        raise ValueError("没有可用的音频路径")
    hint = None        # 作品名提示（子目录归集时用父目录名，而不是 CD1）

    # 情况一：拖入单个目录 → 直接用（只读，不动用户文件）
    if len(dropped) == 1 and os.path.isdir(dropped[0]):
        d = dropped[0]
        files = list_audio(d)
        if not files:
            # 顶层没有但子目录里有（CD1/CD2 这种）→ 自动归集，而不是让用户自己翻
            deep = []
            for dp, _, fs in os.walk(d):
                for f in fs:
                    if f.lower().endswith(settings.AUDIO_EXTS):
                        deep.append(os.path.join(dp, f))
            if not deep:
                raise ValueError("这个文件夹里没有音频文件（%s）"
                                 % "/".join(x.lstrip(".") for x in settings.AUDIO_EXTS[:4]))
            dropped = deep          # 落到下面的「归集」分支
            hint = os.path.basename(d)
        else:
            proj = project or infer_project(os.path.basename(d))
            return {"source": d, "project": proj, "files": files,
                    "n_files": len(files),
                    "total_bytes": sum(os.path.getsize(f) for f in files),
                    "copied": False, "note": "直接使用原文件夹（不复制、不改动）"}

    # 情况二：零散文件（或混合）→ 归集到一个工作目录
    files = []
    for p in dropped:
        if os.path.isdir(p):
            files.extend(list_audio(p))
        elif p.lower().endswith(settings.AUDIO_EXTS):
            files.append(p)
    if not files:
        raise ValueError("拖入的内容里没有音频文件")

    # 项目号：优先用显式指定，其次从文件夹名推断（子目录归集时用最外层目录名）
    proj = project or infer_project(hint or os.path.basename(os.path.dirname(files[0])))
    base = work_root or settings.load_app()["import_dir"]
    dest = os.path.join(base, proj)
    os.makedirs(dest, exist_ok=True)

    copied, failed = 0, []
    for f in files:
        dst = os.path.join(dest, os.path.basename(f))
        # ⚠️ samefile 在 Windows 上比较「不同磁盘」的两个路径会抛 OSError
        # （`path is on mount 'D:', start on mount 'C:'`），必须先兜住再比较。
        if os.path.exists(dst):
            try:
                if os.path.samefile(dst, f):
                    continue
            except OSError:
                pass
        if _link_or_copy(f, dst):
            copied += 1
        else:
            failed.append(os.path.basename(f))

    got = list_audio(dest)
    note = "已归集 %d 个文件到 %s" % (copied, dest)
    if failed:
        note += "（%d 个失败：%s）" % (len(failed), "、".join(failed[:3]))
    return {"source": dest, "project": proj, "files": got,
            "n_files": len(got),
            "total_bytes": sum(os.path.getsize(f) for f in got),
            "copied": True, "note": note}


# ---------------------------------------------------------------- 台本
PAGE_NO = re.compile(r"^\s*[-—–\s]*\d{1,3}\s*[-—–\s]*$")


def pdf_to_text(path):
    """PDF → 纯文本。pypdf 优先，失败再试 pdfplumber。"""
    try:
        from pypdf import PdfReader
    except Exception:
        PdfReader = None
    pages = []
    if PdfReader is not None:
        try:
            r = PdfReader(path)
            for pg in r.pages:
                pages.append(pg.extract_text() or "")
        except Exception:
            pages = []
    if not pages:
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                pages = [(p.extract_text() or "") for p in pdf.pages]
        except Exception as e:
            raise RuntimeError("PDF 解析失败：%s" % e)
    # 去页眉页脚：短行且在多页重复的，直接丢
    lines = []
    for t in pages:
        for ln in t.splitlines():
            s = ln.strip()
            if not s or PAGE_NO.match(s):
                continue
            lines.append(s)
    from collections import Counter
    cnt = Counter(lines)
    npages = max(1, len(pages))
    out = [ln for ln in lines
           if not (len(ln) <= 24 and cnt[ln] >= max(3, npages // 2))]
    return "\n".join(out)


def prepare_script(dropped, dest_dir):
    """把 txt/pdf 台本收进 dest_dir（统一成 .txt），返回 (dir, files, warnings)。"""
    if isinstance(dropped, str):
        dropped = [dropped]
    paths = []
    for p in dropped:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            paths.extend(sorted(glob.glob(os.path.join(glob.escape(p), "*.txt")))
                         + sorted(glob.glob(os.path.join(glob.escape(p), "*.pdf"))))
        elif os.path.isfile(p):
            paths.append(p)
    paths = [p for p in paths if p.lower().endswith((".txt", ".pdf"))]
    if not paths:
        raise ValueError("没有找到 txt / pdf 台本")

    os.makedirs(dest_dir, exist_ok=True)
    # 清掉上一次的台本：同一作品重复拖入不同版本时，旧文件留着会被 load_script 一起读进去
    for old in glob.glob(os.path.join(glob.escape(dest_dir), "*.txt")):
        try:
            os.remove(old)
        except Exception:
            pass
    out, warns = [], []
    for p in paths:
        stem = os.path.splitext(os.path.basename(p))[0]
        dst = os.path.join(dest_dir, stem + ".txt")
        if p.lower().endswith(".txt"):
            shutil.copy2(p, dst)
        else:
            txt = pdf_to_text(p)
            if len(txt.strip()) < 40:
                warns.append("%s：几乎没提取到文字（可能是扫描版）" % os.path.basename(p))
            with open(dst, "w", encoding="utf-8") as f:
                f.write(txt)
        out.append(dst)

    # 轨道标记体检：没有「トラックN / TrackN」而有多条音轨时，打轴会失败
    blob = ""
    for p in out:
        blob += open(p, encoding="utf-8", errors="ignore").read()
    n_tr = len(re.findall(r"(?:トラック|Track|TRACK)\s*[０-９0-9]{1,2}", blob))
    n_ch = len(re.findall(r"[●◯○■◇◆]\s*[０-９0-9]{1,2}\s*[章話]", blob))
    if n_tr == 0:
        warns.append("台本里没有「トラックN / TrackN」标记（章标记 %d 个）。"
                     "若音轨多于一条，打轴会拒绝执行——请用逐轨台本。" % n_ch)
    return dest_dir, out, warns


def script_preview(path, n=6):
    try:
        lines = [ln.strip() for ln in open(path, encoding="utf-8", errors="ignore").read().splitlines()
                 if ln.strip()]
        return lines[:n]
    except Exception:
        return []


# ---------------------------------------------------------------- 杂项
def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def estimate_minutes(total_bytes):
    """粗估总时长：mp3 约 1MB/分钟，flac 约 6MB/分钟，取 1.6MB/分钟折中。"""
    return max(0.5, total_bytes / (1024.0 * 1024.0) / 1.6)
