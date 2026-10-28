# -*- coding: utf-8 -*-
"""半角标点归一的自检（2026-10-26）。

`check.py` 把「半角 ? ! , 或孤立 .」当**硬门禁失败**；而 ASR 原文经常是半角
（`次、また耳少し引っ張るよ?`），译文照抄就中招。`polish.cleanup()` 里的
`normalize_halfwidth()` 负责兜住这类。

用法：python test_punct_norm.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import polish  # noqa: E402

CASES = [
    # (输入, 期望)  —— stats 用空 dict，省略号走默认「……」
    ("其次 再稍微拉一下耳朵吗?", "其次 再稍微拉一下耳朵吗？"),
    ("也许 你有点痒?", "也许 你有点痒？"),
    ("等下, 别这样!", "等下， 别这样！"),
    ("3.5 倍速, 真的!", "3.5 倍速， 真的！"),
    ("啊...这样啊.", "啊……这样啊。"),
    ("真的吗?！", "真的吗？！"),
    # 纯英文/数字行不动（没有中文就不该改）
    ("OK, fine.", "OK, fine."),
    ("v1.2.3", "v1.2.3"),
    # 括号/冒号也归一
    ("小声(笑)说", "小声（笑）说"),
]


def main():
    ok = 0
    for src, want in CASES:
        got = polish.cleanup(src, {})
        good = got == want
        ok += good
        print("  %s %-24r -> %-26r%s"
              % ("✅" if good else "❌", src, got,
                 "" if good else "  期望 %r" % want))
    print("%s punct_norm %d/%d" % ("✅" if ok == len(CASES) else "❌", ok, len(CASES)))
    return 0 if ok == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
