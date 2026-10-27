# -*- coding: utf-8 -*-
r"""
学习语料的第一步：把「人工字幕 + 音频」语料库里的音频全部识别为日文。

和 subtitle/asr.py 的区别：这里按**作品目录**逐个处理，输出分开存放，
避免不同作品的同名音频互相覆盖。

用法：
  python asr_corpus.py [语料根目录] [输出根目录]
    语料根目录 默认 $KIKOERU_ROOT\learning
    输出根目录 默认 $KIKOERU_ROOT\.workbuddy\draft\learning

产出：
  <输出根目录>\<作品目录名>\asr\<音频文件名>.json

已存在的 json 会跳过，可随时中断后续跑。
"""
import os
import sys
import time
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
# 上一层是 <skill>/kikoeru-style-learn，再上一层才是两个 skill 的共同父目录
SUB_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                           "kikoeru-subtitle", "scripts")
assert os.path.isdir(SUB_SCRIPTS), "找不到 subtitle 脚本目录：%s" % SUB_SCRIPTS
# 直接 import 而不是 subprocess：作品目录名常带 ♪ ～ 〔〕 等字符，
# Windows 下 CreateProcess 会报 WinError 267（目录名称无效）。
sys.path.insert(0, SUB_SCRIPTS)
import asr  # noqa: E402
DEFAULT_CORPUS = r"$KIKOERU_ROOT\learning"
DEFAULT_OUT = r"$KIKOERU_ROOT\.workbuddy\draft\learning"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", nargs="?", default=DEFAULT_CORPUS)
    ap.add_argument("out", nargs="?", default=DEFAULT_OUT)
    ap.add_argument("--only", default="", help="只处理名字含该关键字的作品目录")
    ap.add_argument("--skip", default="", help="跳过名字含该关键字的作品目录")
    a = ap.parse_args()

    works = sorted(d for d in os.listdir(a.corpus)
                   if os.path.isdir(os.path.join(a.corpus, d)))
    if a.only:
        works = [w for w in works if a.only in w]
    if a.skip:
        works = [w for w in works if a.skip not in w]
    if not works:
        print("没有作品目录：%s" % a.corpus)
        return

    t_all = time.time()
    print("共 %d 个作品，输出 -> %s\n" % (len(works), a.out), flush=True)
    for i, w in enumerate(works, 1):
        src = os.path.join(a.corpus, w)
        dst = os.path.join(a.out, w, "asr")
        print("\n===== [%d/%d] %s =====" % (i, len(works), w), flush=True)
        t0 = time.time()
        sys.argv = ["asr.py", src, dst, "auto"]
        try:
            asr.main()
            rc = 0
        except SystemExit as e:
            rc = e.code or 0
        except Exception as e:
            print("    [作品失败] %s" % str(e)[:160], flush=True)
            rc = 1
        print("--- %s 用时 %.1f 分钟（退出码 %s）---"
              % (w[:40], (time.time() - t0) / 60, rc), flush=True)
    print("\n全部识别完成，总耗时 %.1f 小时 -> %s"
          % ((time.time() - t_all) / 3600, a.out))


if __name__ == "__main__":
    main()
