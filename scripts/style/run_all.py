# -*- coding: utf-8 -*-
r"""
一键学习：识别音频出日文 -> 与人工字幕对齐 -> 产出/累积风格画像。

用法：
  python run_all.py <音频目录> <人工字幕目录> <标记名(通常=作品RJ号)> [--ext wav|mp3|auto]
                    [--recursive] [--no-llm]

例：
  python run_all.py "$KIKOERU_ROOT/RJ1401520/xxx/02_mp3" \
                    "$KIKOERU_ROOT/kikoerusubtitle/RJ1401520" RJ1401520 --ext mp3

产出（默认 $KIKOERU_ROOT/.workbuddy/style）：
  parallel.json / stats.json / hard_rules.txt / style_guide.txt
多次运行会累积语料，作品越多风格拟合越准。
"""
import os
import sys
import time
import argparse
import subprocess

PY = sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("KIKOERU_ROOT", r"$KIKOERU_ROOT")
DEFAULT_STYLE = os.path.join(ROOT, ".workbuddy", "style")


def run(args, title):
    print("\n" + "=" * 60)
    print("[%s] %s" % (title, " ".join(args)))
    print("=" * 60, flush=True)
    t0 = time.time()
    r = subprocess.run([PY] + args, cwd=HERE)
    print("--- %s 用时 %.1f 分钟 ---" % (title, (time.time() - t0) / 60), flush=True)
    if r.returncode != 0:
        print("步骤失败：%s（退出码 %d）" % (title, r.returncode))
        sys.exit(r.returncode)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("sub_dir")
    ap.add_argument("name")
    ap.add_argument("--ext", default="auto")
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--style", default=DEFAULT_STYLE)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute", default="float16")
    a = ap.parse_args()

    asr_dir = os.path.join(ROOT, ".workbuddy", "draft", a.name, "asr")
    os.makedirs(asr_dir, exist_ok=True)
    os.makedirs(a.style, exist_ok=True)

    print("音频源 : %s" % a.src)
    print("人工字幕: %s" % a.sub_dir)
    print("风格输出: %s" % a.style)

    args = [os.path.join(HERE, "asr.py"), a.src, asr_dir, a.ext,
            "--device", a.device, "--compute", a.compute]
    if a.recursive:
        args.append("--recursive")
    run(args, "识别日文")

    if not a.no_llm:
        run([os.path.join(HERE, "serve.py"), "start"], "启动推理服务")

    args = [os.path.join(HERE, "align_style.py"), a.sub_dir, asr_dir,
            "--style", a.style, "--tag", a.name]
    if a.no_llm:
        args.append("--no-llm")
    run(args, "对齐并归纳风格")

    print("\n完成：风格资料已更新 -> %s" % a.style)


if __name__ == "__main__":
    main()
