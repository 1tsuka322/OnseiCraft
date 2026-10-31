# -*- coding: utf-8 -*-
"""模型管理：检测缺失 + 一键下载。

模型不随安装包分发（whisper large-v3 约 3.1GB、Sakura-7B 约 4.2GB），
但**不该让用户自己去找**——这里把「下载 → 放对位置 → 写进配置」全包了。

下载默认走 HuggingFace 官方；国内可在设置里切镜像（hf-mirror.com）。
大文件支持 Range 断点续传：断了重来不会从 0 开始。
"""
import os
import shutil
import threading
import zipfile

from . import settings

# 模型根目录：安装目录下的 models/（开发模式下是仓库根 models/，已在 .gitignore）
def models_root():
    return os.path.join(settings.BASE, "models")


ENDPOINTS = {
    "official": "https://huggingface.co",
    "mirror": "https://hf-mirror.com",
}

ITEMS = [
    {
        "key": "asr_model",
        "label": "语音识别 · Whisper large-v3",
        "desc": "约 3.1 GB。识别日语音频，GPU 上约 7 倍实时。",
        "kind": "hf",
        "repo": "Systran/faster-whisper-large-v3",
        "subdir": "faster-whisper-large-v3",
        "approx": 3.1,
    },
    {
        "key": "model_sakura",
        "label": "翻译 · Sakura-7B（IQ4_XS）",
        "desc": "约 4.2 GB。日中翻译，懂 ACGN 词汇。",
        "kind": "hf",
        "repo": "SakuraLLM/Sakura-7B-Qwen2.5-v1.0-GGUF",
        "only": ["sakura-7b-qwen2.5-v1.0-iq4xs.gguf"],
        "subdir": "sakura-7b",
        "approx": 4.2,
    },
    {
        "key": "llama_dir",
        "label": "推理引擎 · llama.cpp（llama-server）",
        "desc": "GPU 版约 0.7 GB（主程序 + CUDA runtime）；无显卡会自动选 CPU 版 25MB。",
        "kind": "gh",
        "repo": "ggml-org/llama.cpp",
        "subdir": "llama.cpp",
        "approx": 0.7,
    },
]


def item(key):
    for it in ITEMS:
        if it["key"] == key:
            return it
    return None


def target_path(it):
    return os.path.join(models_root(), it["subdir"])


def is_ready(it):
    p = target_path(it)
    if not os.path.isdir(p):
        return False
    if it["kind"] == "hf":
        if os.path.isfile(os.path.join(p, "model.bin")):
            return True
        return any(f.endswith(".gguf") for f in os.listdir(p))
    return _find_exe(p) is not None


def _find_exe(root):
    if not os.path.isdir(root):
        return None
    for dp, _, fs in os.walk(root):
        for f in fs:
            if f.lower() == "llama-server.exe":
                return os.path.join(dp, f)
    return None


def status_all():
    out = []
    for it in ITEMS:
        out.append({"item": it, "ready": is_ready(it), "path": target_path(it)})
    return out


# ---------------------------------------------------------------- 下载
class Cancelled(Exception):
    pass


def _download(url, path, on_bytes, cancel=None, timeout=30):
    """单文件下载，支持 Range 续传。on_bytes(delta)。"""
    import requests
    have = os.path.getsize(path) if os.path.exists(path) else 0
    headers = {"Range": "bytes=%d-" % have} if have else {}
    r = requests.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True)
    if r.status_code == 416:          # 已经下完了
        return
    if r.status_code not in (200, 206):
        raise RuntimeError("HTTP %d：%s" % (r.status_code, url))
    mode = "ab" if (r.status_code == 206 and have) else "wb"
    if mode == "wb" and have:
        have = 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        for chunk in r.iter_content(1024 * 512):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if not chunk:
                continue
            f.write(chunk)
            on_bytes(len(chunk))


def hf_files(repo, endpoint):
    import requests
    r = requests.get("%s/api/models/%s" % (endpoint, repo), timeout=30)
    r.raise_for_status()
    return [s["rfilename"] for s in r.json().get("siblings", [])]


def download_hf(it, endpoint, on_progress, cancel=None):
    """返回落盘目录。on_progress(done_bytes, total_bytes, 说明)。"""
    import requests
    dest = target_path(it)
    os.makedirs(dest, exist_ok=True)
    files = it.get("only") or hf_files(it["repo"], endpoint)

    # 先拿每个文件的大小，算总进度
    sizes = {}
    for fn in files:
        try:
            u = "%s/%s/resolve/main/%s" % (endpoint, it["repo"], fn)
            h = requests.head(u, allow_redirects=True, timeout=30)
            sizes[fn] = int(h.headers.get("Content-Length") or 0)
        except Exception:
            sizes[fn] = 0
    total = sum(sizes.values()) or 1
    done = 0
    for fn in files:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        dst = os.path.join(dest, fn.replace("/", os.sep))
        if os.path.exists(dst) and sizes.get(fn) and os.path.getsize(dst) >= sizes[fn]:
            done += sizes[fn]
            on_progress(done, total, fn)
            continue
        base = done

        def cb(n, _base=base, _fn=fn):
            on_progress(_base + n, total, _fn)

        url = "%s/%s/resolve/main/%s" % (endpoint, it["repo"], fn)
        _download(url, dst, cb, cancel)
        done = base + (os.path.getsize(dst) if os.path.exists(dst) else 0)
        on_progress(done, total, fn)
    return dest


def _find_llama_release():
    """找**带 Windows 二进制**的 release。

    ⚠️ 不能直接用 `/releases/latest`：仓库的 latest 常是 `v0.x` 空壳（只有一个
    nightly-tag.txt），真正的 `bXXXXX` 构建在普通 release 里。
    """
    import requests
    rels = requests.get("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=20",
                        timeout=30).json()
    for rel in rels:
        names = [a["name"] for a in rel.get("assets", [])]
        if any(n.startswith("llama-b") and "win" in n for n in names):
            return rel
    raise RuntimeError("最近 20 个 release 里都没有 Windows 包")


def _pick_llama_assets(assets, cuda):
    """GPU 版要两个包：主程序 + CUDA runtime（cudart 是分开发的，合计约 670MB）。"""
    def first(pred):
        for a in assets:
            if pred(a["name"].lower()):
                return a
        return None

    if not cuda:
        a = first(lambda n: n.startswith("llama-b") and "win-cpu-x64" in n and n.endswith(".zip"))
        return [a] if a else []
    main = first(lambda n: n.startswith("llama-b") and "win-cuda-12" in n and n.endswith(".zip"))
    runtime = first(lambda n: n.startswith("cudart-llama") and "cuda-12" in n and n.endswith(".zip"))
    return [a for a in (main, runtime) if a]


def download_llama(it, on_progress, cancel=None, cuda=True):
    import requests
    dest = target_path(it)
    os.makedirs(dest, exist_ok=True)
    rel = _find_llama_release()
    picks = _pick_llama_assets(rel.get("assets", []), cuda)
    if not picks:
        raise RuntimeError("release %s 里没有可用的 Windows 包" % rel.get("tag_name"))
    total = sum(int(a.get("size") or 0) for a in picks) or 1
    base = 0
    for a in picks:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        zpath = os.path.join(dest, a["name"])
        done = [0]

        def cb(n, _b=base, _a=a):
            done[0] += n
            on_progress(min(total, _b + done[0]), total, _a["name"])

        _download(a["browser_download_url"], zpath, cb, cancel)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(dest)
        try:
            os.remove(zpath)
        except Exception:
            pass
        base += int(a.get("size") or 0)
    return dest


def apply_to_config(it, path):
    """下载完把路径写进 pipeline.local.json。"""
    if it["key"] == "asr_model":
        settings.set_model("asr_model", path)
    elif it["key"] == "model_sakura":
        gguf = None
        for dp, _, fs in os.walk(path):
            for f in fs:
                if f.endswith(".gguf"):
                    gguf = os.path.join(dp, f)
                    break
            if gguf:
                break
        settings.set_model("model_sakura", gguf or path)
    elif it["key"] == "llama_dir":
        exe = _find_exe(path)
        settings.set_model("llama_dir", os.path.dirname(exe) if exe else path)
