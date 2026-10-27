# -*- coding: utf-8 -*-
"""
管理本地 llama-server（Qwen2.5-7B-Instruct GGUF / GPU）。

用法：
  python serve.py status            # 查看服务状态与显存
  python serve.py start [--q5|--q4] # 启动（默认按显存自动选 Q5/Q4）
  python serve.py stop

服务地址：http://127.0.0.1:8080 （OpenAI 兼容接口）
"""
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json
import time
import subprocess
import urllib.request
import urllib.error

LLAMA_DIR = r"D:\llama.cpp"
GGUF_DIR = r"D:\fasterwhisperGUI-0.8.5.sfx\models\qwen2.5-7b-instruct-gguf"
Q5 = os.path.join(GGUF_DIR, "qwen2.5-7b-instruct-q5_k_m-00001-of-00002.gguf")
Q4 = os.path.join(GGUF_DIR, "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf")
PORT = 8080
API = "http://127.0.0.1:%d" % PORT


def gpu_free_mb():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20)
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        return -1


def alive():
    try:
        urllib.request.urlopen(API + "/health", timeout=5).read()
        return True
    except Exception:
        return False


def start(force=None):
    if alive():
        print("服务已在运行")
        return
    free = gpu_free_mb()
    if force == "q5":
        gguf = Q5
    elif force == "q4":
        gguf = Q4
    else:
        # Q5 需 ~5.6GB，Q4 需 ~4.9GB
        gguf = Q5 if (free < 0 or free >= 5800) else Q4
    if not os.path.exists(gguf):
        print("模型不存在:", gguf)
        sys.exit(1)
    slots = 3 if gguf == Q5 else 4
    ctx = 10240 if gguf == Q5 else 12288
    cmd = [os.path.join(LLAMA_DIR, "llama-server.exe"), "-m", gguf, "-c", str(ctx),
           "-np", str(slots), "-ngl", "99", "-fa", "on", "-t", "8",
           "--host", "127.0.0.1", "--port", str(PORT)]
    print("启动:", " ".join(cmd))
    print("（可用显存 %s MB，选用 %s）" % (free, os.path.basename(gguf)))
    subprocess.Popen(cmd, cwd=LLAMA_DIR, creationflags=0x00000008,  # DETACHED_PROCESS
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for i in range(90):
        time.sleep(2)
        if alive():
            print("服务就绪，用时约 %d 秒 -> %s" % ((i + 1) * 2, API))
            return
    print("启动超时，请检查 D:\\llama.cpp 下是否有 llama-server.exe 与 CUDA 运行库")


def stop():
    subprocess.run(["taskkill", "/F", "/IM", "llama-server.exe"],
                   capture_output=True, text=True)
    time.sleep(2)
    print("已停止" if not alive() else "仍在运行")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        force = None
        if "--q5" in sys.argv:
            force = "q5"
        if "--q4" in sys.argv:
            force = "q4"
        start(force)
    elif cmd == "stop":
        stop()
    else:
        print("服务状态:", "运行中" if alive() else "未运行")
        print("可用显存: %s MB" % gpu_free_mb())
        if alive():
            try:
                r = json.loads(urllib.request.urlopen(API + "/props", timeout=5).read())
                print("模型:", r.get("model_path", "?"))
            except Exception:
                pass
