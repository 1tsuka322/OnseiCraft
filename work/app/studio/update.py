# -*- coding: utf-8 -*-
"""软件内检查更新 + 一键升级。

设计原则：**升级只换程序，绝不动用户数据**。
安装包只覆盖 `_internal/`、`work/{scripts,pipeline,presets}`，
而模型、`pipeline.local.json`、`work/data`、`work/runs`、成品目录都不在包里——
所以「一键更新」= 重新跑一遍安装包，配置和模型原地保留。

流程：查 GitHub Release → 下载到临时目录 → `/SILENT` 启动安装包
（`/CLOSEAPPLICATIONS` 让它自己关掉本程序）→ 装完自动启动新版。
"""
import os
import re
import subprocess
import sys
import tempfile

from . import __version__

REPO = "1tsuka322/OnseiCraft"
API = "https://api.github.com/repos/%s/releases/latest" % REPO


def _ver(s):
    s = (s or "").strip().lstrip("vV")
    parts = re.findall(r"\d+", s)
    return tuple(int(x) for x in parts[:3]) if parts else (0,)


def check():
    """有新版返回 dict；已是最新返回 None。网络/接口异常抛 RuntimeError。"""
    import requests
    try:
        r = requests.get(API, timeout=15,
                         headers={"Accept": "application/vnd.github+json"})
    except Exception as e:
        raise RuntimeError("连不上 GitHub：%s" % e)
    if r.status_code != 200:
        raise RuntimeError("GitHub 返回 %d（仓库可能是私有的或限流）" % r.status_code)
    d = r.json()
    tag = d.get("tag_name") or ""
    if _ver(tag) <= _ver(__version__):
        return None
    asset = None
    for a in d.get("assets", []):
        if a.get("name", "").lower().endswith(".exe"):
            asset = a
            break
    return {
        "tag": tag,
        "name": d.get("name") or tag,
        "notes": d.get("body") or "",
        "page": d.get("html_url") or "",
        "url": (asset or {}).get("browser_download_url") or d.get("html_url") or "",
        "size": int((asset or {}).get("size") or 0),
        "fname": (asset or {}).get("name") or "OnseiCraft-Setup.exe",
    }


def download(info, on_progress, cancel=None):
    """下到临时目录，返回本地路径。支持断点续传（复用 models._download）。"""
    from .models import _download
    d = os.path.join(tempfile.gettempdir(), "OnseiCraft-update")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, info["fname"])
    total = info.get("size") or 0
    done = [0]

    def cb(n):
        done[0] += n
        on_progress(min(done[0], total or done[0]), total or done[0])

    _download(info["url"], dst, cb, cancel)
    return dst


def install_and_restart(setup_path):
    """启动安装包（静默、装完自动开新版），然后调用方应当立刻退出本程序。

    `/CLOSEAPPLICATIONS` 让安装程序用 Restart Manager 关掉还占着文件的进程
    （也就是我们自己）——少了它会卡在「文件正在使用」。
    """
    args = [setup_path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"]
    subprocess.Popen(args, close_fds=True)
