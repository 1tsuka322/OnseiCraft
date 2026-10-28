# -*- coding: utf-8 -*-
"""
管理本地 llama-server（Qwen2.5-7B / Qwen3-8B GGUF / GPU）。

用法：
  python serve.py status                 # 查看服务状态与显存
  python serve.py start                  # 启动（默认按显存自动选 Q5/Q4）
  python serve.py start --qwen3          # 用 Qwen3-8B（更强，但需 KV 量化 + 降上下文）
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
QWEN3_DIR = r"D:\fasterwhisperGUI-0.8.5.sfx\models\qwen3-8b-gguf"
Q5 = os.path.join(GGUF_DIR, "qwen2.5-7b-instruct-q5_k_m-00001-of-00002.gguf")
Q4 = os.path.join(GGUF_DIR, "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf")
Q3_8B = os.path.join(QWEN3_DIR, "Qwen3-8B-Q5_K_M.gguf")
SAKURA_DIR = r"D:\fasterwhisperGUI-0.8.5.sfx\models\sakura-7b"
SAKURA = os.path.join(SAKURA_DIR, "sakura-7b-v1.0-iq4xs.gguf")
# HY-MT 1.5（腾讯混元，WMT25 冠军升级版）——**翻译专用**模型，用于「初译」通道
# （KIKOERU_ENGINE=hymt）。官方 GGUF，Q4_K_M 约 4.3GB。它不是聊天模型，
# prompt 必须明确「把下面的文本翻译成中文」，见 translate.py 的 hymt 分支。
HYMT_DIR = r"D:\fasterwhisperGUI-0.8.5.sfx\models\hy-mt1.5-7b"
HYMT = os.path.join(HYMT_DIR, "HY-MT1.5-7B-Q4_K_M.gguf")

# 每个候选模型： (路径, 权重显存MB, 每槽上下文, 并行槽数, 是否 KV 量化, 是否加 --jinja)
# KV cache 的每 token 开销 = 2 * 层数 * KV头数 * head_dim * 2B：
#   Qwen2.5-7B：2*28*4*128*2 = 56KB/token（24k 上下文 ≈ 1.4GB）
#   Qwen3-8B  ：2*36*8*128*2 = 144KB/token（同样 24k 要 3.5GB，8GB 卡装不下）
# 所以 Qwen3 必须降上下文 + KV 量化（q8 后约 72KB/token）。
# 每槽至少 8192 tokens 是硬约束——润色 prompt（硬规则+术语表+例句+待译句）约 4400 tokens。
# ⚠️ 2026-10-16 实测：硬规则/术语表累积增长后，强润色 system prompt 已达 **7220 tokens**
#    （9753 字符）——8192 槽装不下（prompt+待译+输出超限会被 llama.cpp 截断开头）。
#    polish.py 的 WORKERS=2，所以 q5 改 -np 2：每槽 12288 tokens，留足余量。
#    （旧的 -np 3 只是白占一个槽；且 prompt 缩减循环在「静态部分超 SYS_MAX」时
#    已修好终止，见 polish.py main() 的 break 守卫。）
# 另：Qwen3 默认开 thinking，必须 --jinja 才能让请求里的
# chat_template_kwargs={"enable_thinking": false} 生效，否则输出里全是  thinking 块。
MODELS = {
    "q5":     (Q5,     5600, 24576, 2, False, False),
    "q4":     (Q4,     4900, 32768, 2, False, False),
    "qwen3":  (Q3_8B,  5900, 16384, 2, True,  True),
    # SakuraLLM：专为「日→中」训练的 ACGN 微调（Qwen2.5-7B 底模），官方卖点第一条就是
    # 「联系上下文正确使用人称代词」——正对本工程「日文省略主语导致看不懂」的痛点。
    # 来源 hf-mirror（HuggingFace 镜像）：SakuraLLM/Sakura-7B-Qwen2.5-v1.0-GGUF
    # 注意它有自己的 prompt 格式与采样参数（temp 0.1 / top_p 0.3），详见 translate.py 的 sakura 分支。
    "sakura": (SAKURA, 4300, 24576, 3, False, False),
    # HY-MT 1.5 翻译专用：只做初译，风格交给 polish。Q4_K_M ~4.3GB。
    "hymt":   (HYMT,   4400, 24576, 3, False, False),
}
PORT = 8080
API = "http://127.0.0.1:%d" % PORT


def gpu_free_mb():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=20)
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
    if force in MODELS:
        key = force
    else:
        # 默认按显存选：Qwen2.5 的 Q5 需 ~5.6GB，不够就退 Q4
        key = "q5" if (free < 0 or free >= 5800) else "q4"
    gguf, need, ctx, slots, kv_q8, jinja = MODELS[key]
    # KIKOERU_SLOTS（2026-10-25）：评估专用。**并行槽 >1 时 llama.cpp 的批处理
    # 归约顺序不固定，贪心解码也不保证逐字节一致**——实测 temp=0 同配置跑两遍，
    # 20 轨里 18 轨有差异（A/A 噪声底 17.1%，和处理效应 15.6% 同量级，A/B 直接作废）。
    # 设 1 可强制单槽（每槽上下文变成 ctx，显存够），换回确定性。
    _sl = os.environ.get("KIKOERU_SLOTS")
    if _sl:
        try:
            slots = max(1, int(_sl))
            print("KIKOERU_SLOTS=%d（覆盖默认并行槽数，评估用）" % slots)
        except ValueError:
            pass
    if not os.path.exists(gguf):
        print("模型不存在:", gguf)
        sys.exit(1)
    # 关键：-c 是**总**上下文，会被 -np 平分给每个并行槽。
    # 曾经用 -c 10240 -np 3，每槽实际只有 3584 tokens，而润色 prompt 已经涨到
    # 4400+ tokens（硬规则 + 术语表 + 例句 + 16 句待润色），结果 llama-server 直接 400
    # 「exceeds the available context size」。所以按「每槽 ≥ 8192」反推总 -c。
    cmd = [os.path.join(LLAMA_DIR, "llama-server.exe"), "-m", gguf, "-c", str(ctx),
           "-np", str(slots), "-ngl", "99", "-fa", "on", "-t", "8",
           "--host", "127.0.0.1", "--port", str(PORT)]
    if kv_q8:
        cmd += ["--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]
    if jinja:
        cmd += ["--jinja"]
    print("启动:", " ".join(cmd))
    print("（可用显存 %s MB，选用 %s[%s]，权重约 %d MB，每槽上下文 %d tokens%s）"
          % (free, os.path.basename(gguf), key, need, ctx // slots,
             "，KV 已量化 q8" if kv_q8 else ""))
    subprocess.Popen(cmd, cwd=LLAMA_DIR, creationflags=0x00000008,  # DETACHED_PROCESS
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for i in range(90):
        time.sleep(2)
        if alive():
            print("服务就绪，用时约 %d 秒 -> %s" % ((i + 1) * 2, API))
            return
    print("启动超时，请检查 D:\\llama.cpp 下是否有 llama-server.exe 与 CUDA 运行库")


def stop():
    # ⚠️ 必须指定 encoding + errors：`taskkill` 在中文 Windows 上输出 GBK，
    # 默认 utf-8 解码会让 subprocess 的 reader 线程抛 UnicodeDecodeError，
    # 日志里多出一大段 traceback（R154 实测），排障时非常误导。
    subprocess.run(["taskkill", "/F", "/IM", "llama-server.exe"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    time.sleep(2)
    print("已停止" if not alive() else "仍在运行")


def loaded_model():
    """8080 上**真正**加载的模型路径；拿不到返回 ""。"""
    try:
        r = json.loads(urllib.request.urlopen(API + "/props", timeout=8).read())
        return r.get("model_path") or r.get("model_alias") or ""
    except Exception:
        return ""


def ensure(key=None):
    """按引擎对齐模型。**这才是管线该调的命令。**

    2026-10-28 事故（R153）：`_ensure_server` 调的是 `serve.py start` 且不带模型，
    start() 就按显存自动选了 q5（Qwen2.5-7B）；而 translate.py 因为
    KIKOERU_ENGINE=hymt 走的是 HY-MT 分支（裸指令、逐句、temp0.7）——
    等于「拿 HY-MT 的提示词去问 Qwen2.5」。实测同一句日文：HY-MT 英文 0/5，
    Qwen2.5 英文 5/5，还夹带拒答、元话语、日文残留。
    alive() 只探端口，所以「服务已在运行」把上一个错模型原样留下了。
    """
    if key not in MODELS:
        return start(None)                      # 没指定引擎 → 沿用旧行为
    want = os.path.basename(MODELS[key][0])
    if alive():
        got = os.path.basename(loaded_model() or "")
        if got == want:
            print("服务已就绪（引擎 %s，模型匹配）" % key)
            print("模型校验通过：%s[%s]" % (want, key))
            return
        print("⚠️ 端口上跑的是 %s，与引擎 %s 要求的 %s 不符 → 重启"
              % (got or "未知", key, want))
        stop()
    start(key)
    got = os.path.basename(loaded_model() or "")
    if got != want:
        print("❌ 模型校验失败：期望 %s，实际 %s" % (want, got or "未知"))
        sys.exit(2)
    print("模型校验通过：%s[%s]" % (want, key))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        force = None
        for k in MODELS:
            if "--" + k in sys.argv:
                force = k
        start(force)
    elif cmd == "ensure":
        force = None
        for k in MODELS:
            if "--" + k in sys.argv:
                force = k
        ensure(force)
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
