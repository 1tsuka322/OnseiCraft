# -*- coding: utf-8 -*-
"""字幕质量审计：把成品里"可疑"的行挑出来分类，用于定位系统性问题。
用法: python audit.py <字幕目录> [--n 30]
"""
import os
import re
import sys
sys.stdout.reconfigure(encoding="utf-8")
import glob
import argparse

LINE_RE = re.compile(r"\[(\d+:\d+(?:[.,]\d+)?)\]\s*(.*)")


def load(d):
    lines = []
    for f in sorted(glob.glob(os.path.join(glob.escape(d), "*.lrc"))):
        name = os.path.basename(f)
        for raw in open(f, encoding="utf-8"):
            m = LINE_RE.match(raw.strip())
            if m and m.group(2).strip():
                lines.append((name, m.group(1), m.group(2).strip()))
    return lines


# 每条规则: (类别, 正则/谓词, 说明)
def audit(lines):
    buckets = {}

    def put(cat, item):
        buckets.setdefault(cat, []).append(item)

    for name, ts, t in lines:
        # 1) 未翻译的拉丁字母（允许 ♡♪ … 等符号）
        if re.search(r"[A-Za-z]{2,}", t):
            put("英文残留", (name, ts, t))
        # 2) 未翻译的片假名长串（拟声词应译成中文）
        if re.search(r"[\u30a0-\u30ff]{3,}", t):
            put("片假名残留", (name, ts, t))
        # 3) 日文引号残留
        if "「" in t or "」" in t:
            put("日文引号", (name, ts, t))
        # 4) 翻译腔高危（源自《新编日译汉教程》与 VoiceTransl 的「避雷针」清单）
        if re.search(r"关于[^，。？]{0,12}[，]|对于[^，。？]{0,12}[，]", t):
            put("①关于/对于综合症", (name, ts, t))
        if t.count("被") >= 2 or re.search(r"被[^，。？]{0,6}(着|所)", t):
            put("②被字滥用", (name, ts, t))
        if re.search(r"(进行|作出|做出|给予)[了一]?(个|次|下)?[^，。？]", t):
            put("③虚假动词", (name, ts, t))
        if re.search(r"(学生|朋友|人们|大家)们|们(都|在|的)", t):
            put("④复数们", (name, ts, t))
        # ⑤ 过长定语「的」链：一个分句里出现 3 个以上「的」且跨 12 字以上
        if len(re.findall(r"的", t)) >= 3 and len(t) >= 14:
            put("⑤定语的长链", (name, ts, t))
        if re.search(r"的的|了了|是是|在在|被被|们们", t):
            put("叠字错乱", (name, ts, t))
        if re.search(r"进行[了一]?(个|次|下)?[^，。？]", t) or "做出" in t or "给予" in t:
            put("书面腔", (name, ts, t))
        if re.search(r"的话|的事情|的时候|的情况下", t):
            put("冗余名词化", (name, ts, t))
        # 5) 过长行（>24 字，超出单行阅读）
        zh = len(re.sub(r"[^\u4e00-\u9fff]", "", t))
        if zh > 24:
            put("超长行", (name, ts, t))
        # 5.5) 无限循环行（单字符重复 ≥6 次，含带空格的「三 三 三…」）
        #      实测模型把呻吟放大成「哦哦哦…」×1728，这是静默灾难，check.py 查不出
        if re.search(r"(.)\1{5,}", t) or re.search(r"([^\s，。！？…])(?:\s+\1){5,}", t):
            put("无限循环", (name, ts, t))
        # 6) 纯符号/极短噪音
        if zh == 0 and not re.search(r"[A-Za-z]", t):
            put("无汉字", (name, ts, t))
    return buckets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--n", type=int, default=25)
    a = ap.parse_args()
    lines = load(a.dir)
    print("总行数 %d" % len(lines))
    b = audit(lines)
    order = sorted(b, key=lambda k: -len(b[k]))
    for cat in order:
        items = b[cat]
        pct = 100.0 * len(items) / max(1, len(lines))
        print("\n===== %s: %d 行 (%.1f%%) =====" % (cat, len(items), pct))
        for name, ts, t in items[: a.n]:
            print("  [%s] %s  |%s" % (ts, t, name[:18]))
    if not order:
        print("未发现可疑行")


if __name__ == "__main__":
    main()
