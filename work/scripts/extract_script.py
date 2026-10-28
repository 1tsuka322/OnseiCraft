# -*- coding: utf-8 -*-
"""从 asmr.one 作品的台本 PDF 提取日文台词（竖排感知）。

## 为什么不用 pypdf

asmr.one 上的台本 PDF 绝大多数是**縦書き（竖排）**排版，pypdf 按字符流提取会得到
一列单字：

    ト
    ラ
    ッ
    ク
    １

## 解法：按坐标重排

`pdfplumber` 能给每个字符的 (x0, top)。竖排文本的特征是**同一 x、y 递增**，
所以「按列分组（x 降序）、列内按 y 升序」就能还原阅读顺序（日文竖排从右往左读列）。

实测（RJ9000013 台本，41 页）重排后：
    トラック１ イメージ 場所 セリフ //玄関のドアが開く音// あ、先生いらっしゃい！…

## 两种版面

1. **纯竖排**（RJ1715662）：整页一个文本块，全局排序即可。
2. **表格式竖排**（RJ9000013）：每页有「トラック/イメージ/場所/セリフ」表头 + 正文单元格。
   全局排序会把表头混进正文，所以额外做一次「表头词剔除」。

用法：
  python extract_script.py <pdf> [--out txt] [--raw]
"""
import os
import re
import sys
sys.stdout.reconfigure(encoding="utf-8")
import argparse
import collections

# 表头/版式词（不是台词）：出现在台本每页顶部或侧栏
HEADER_WORDS = ("トラック", "イメージ", "場所", "セリフ", "台本", "ページ")
# 舞台指示：//音// 或 （音）或 ※注
SOUND_RE = re.compile(r"//[^/]{1,60}//|\([^)]{0,30}音[^)]{0,20}\)|（[^）]{0,30}音[^）]{0,20}）")
# 台词结束的标点（用于把长段切成句）
SENT_END = "。！？!?…♡♪」』"
# ⚠️ PDF 字体缺 Unicode 映射时会输出 `(cid:9351)` 这种。实测台本里它成对出现，
#    是日文的**繰り返し記号「〳〵」**（重复前词），删掉不影响语义（模型看上下文能懂）。
CID_RE = re.compile(r"\(cid:\d+\)")
# 台本里的时长/页码标记：`６中`（6 分 30 秒的「中」）、纯数字行、`1` `2` 之类
TIME_MARK_RE = re.compile(r"^[０-９0-9]{1,3}\s*[中分秒]?\s*$")


def is_vertical(chars):
    """这页是不是竖排：同一 x 上出现 ≥3 个字符的比例够高。"""
    if len(chars) < 20:
        return False
    by_x = collections.Counter(round(c["x0"]) for c in chars)
    stacked = sum(v for v in by_x.values() if v >= 3)
    return stacked / float(len(chars)) >= 0.6


def page_text(page):
    """按坐标把一页的字符重排成可读文本。

    竖排：列从右往左（x 降序），列内从上往下（y 升序）。
    横排：行从上往下（y 升序），行内从左往右（x 升序）。
    """
    chars = [c for c in page.chars if (c.get("text") or "").strip()]
    if not chars:
        return ""
    if is_vertical(chars):
        # 列分组：x 相近（±4）视为同一列；列间 x 降序
        rows = sorted(chars, key=lambda c: (-round(c["x0"] / 4.0), c["top"]))
    else:
        rows = sorted(chars, key=lambda c: (round(c["top"] / 4.0), c["x0"]))
    return "".join(c["text"] for c in rows)


def clean(text):
    """去掉表头词、把音效标记单独成行、按句末标点断句。"""
    # 字体缺字 → 删（成对出现的是「〳〵」重复记号，删了不影响语义）
    text = CID_RE.sub("", text)
    # ⚠️ `トラック` 既是表头词又是**轨道分隔标记**。先保护成占位符，
    #    否则下面剔表头时会把「トラック１」一起删掉，整本台本就切不开轨了。
    text = re.sub(r"(?:トラック|Track|TRACK)\s*([０-９0-9]{1,2})",
                  lambda m: "\u3010TRACK%s\u3011" % m.group(1), text)
    # 表头词逐个剔除（它们只出现在版式区，不出现在台词里）
    for w in HEADER_WORDS:
        text = text.replace(w, " ")
    # 时长/页码标记：`６中` `８中` 这种，夹在台词里会把句子切碎
    text = re.sub(r"(?<![ぁ-んァ-ヶ])[０-９0-9]{1,3}中", " ", text)
    # 恢复轨道标记
    text = re.sub(r"\u3010TRACK([０-９0-9]{1,2})\u3011", r"トラック\1", text)
    # 音效标记 → 换行包起来，避免和台词粘在一起
    text = SOUND_RE.sub(lambda m: "\n%s\n" % m.group(0), text)
    # 断句：在句末标点后换行（但保留省略号连用）
    text = re.sub(r"(?<=[。！？!?])(?![。！？!?…])", "\n", text)
    text = re.sub(r"(?<=[♡♪])(?=\S)", "\n", text)
    # 合并空白
    lines = []
    for ln in text.splitlines():
        ln = re.sub(r"[ \t\u3000]+", " ", ln).strip()
        if not ln:
            continue
        if TIME_MARK_RE.match(ln):      # 纯页码/时长行
            continue
        lines.append(ln)
    return "\n".join(lines)


def extract(pdf_path, keep_sound=True):
    import pdfplumber
    out = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            t = page_text(page)
            if not t:
                continue
            c = clean(t)
            if not keep_sound:
                c = SOUND_RE.sub("", c)
            if c.strip():
                out.append("### p%d\n%s" % (i, c))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", default="")
    ap.add_argument("--raw", action="store_true", help="不做清洗，只重排")
    ap.add_argument("--no-sound", action="store_true", help="丢掉 //音效// 标记")
    a = ap.parse_args()

    txt = extract(a.pdf, keep_sound=not a.no_sound)
    if a.raw:
        txt = "\n".join(page_text(p) for p in __import__("pdfplumber").open(a.pdf).pages)

    if a.out:
        open(a.out, "w", encoding="utf-8").write(txt)
        print("已写 %s（%d 字符）" % (a.out, len(txt)))
    else:
        print(txt[:3000])
        print("\n... 共 %d 字符" % len(txt))


if __name__ == "__main__":
    main()
