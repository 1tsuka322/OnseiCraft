# -*- coding: utf-8 -*-
"""把用户反馈 txt 拆成台账条目。

用户从 Studio「反馈日志」导出的 txt 里已经带齐四样东西（意见 / ASR 原文 /
修后原文 / 当前字幕）。这个脚本只做**机械部分**：

1. 解析出「作品号 / 运行 ID / 轨道 / 意见 / 时间点」
2. 按关键词**初判**是代码层还是语料层（人再确认）
3. 生成条目草稿，追加到 `work/feedback/ISSUES_CODE.md` / `ISSUES_CORPUS.md`
4. 原始 txt 移到 `work/feedback/processed/`

用法：
    python work/scripts/feedback_ingest.py            # 干跑，只打印会生成什么
    python work/scripts/feedback_ingest.py --apply    # 写台账 + 移动文件

⚠️ 判类只是**初判**。分类错了比不分类更糟，落完必须人工看一眼。
"""
import argparse
import glob
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rootloc import ROOT  # noqa: E402

FB = os.path.join(ROOT, "feedback")
INBOX = os.path.join(FB, "inbox")
DONE = os.path.join(FB, "processed")
STUDIO_OUT = os.path.join(ROOT, "data", "feedback")

CODE_MD = os.path.join(FB, "ISSUES_CODE.md")
CORPUS_MD = os.path.join(FB, "ISSUES_CORPUS.md")

# 初判关键词。宁可判错让人改，也别漏判成「没问题」。
CODE_WORDS = ("崩溃", "闪退", "报错", "卡住", "卡死", "无响应", "白屏", "乱码", "装不上",
              "安装失败", "进度", "一直 0", "不动", "很慢", "太慢", "打不开", "找不到",
              "路径", "界面", "按钮", "点击没反应", "重启", "内存", "显存", "报错码")
CORPUS_WORDS = ("译", "翻译", "术语", "错字", "识别错", "断句", "漏", "语气", "生硬",
                "不通顺", "字幕", "时间轴", "标点", "人名", "称呼", "前后不一致", "太直",
                "意译", "口语", "拟声", "喘")

PROJ_RE = re.compile(r"作品号\s*[：:]\s*(RJ\d{5,})")
RUN_RE = re.compile(r"运行\s*ID\s*[：:]\s*(\S+)")
TRACK_RE = re.compile(r"^【轨道】\s*(.+?)\s*$", re.M)
OPINION_RE = re.compile(r"我的意见：\s*\n(.*?)(?=\n▶|\n---|\n={10,}|\Z)", re.S)
TS_RE = re.compile(r"标记时间点\s*([\d:.]+)")


def classify(text):
    """返回 (kind, score_note)。code / corpus。"""
    c = sum(1 for w in CODE_WORDS if w in text)
    l = sum(1 for w in CORPUS_WORDS if w in text)
    if c > l:
        return "code", "代码词 %d / 语料词 %d" % (c, l)
    return "corpus", "代码词 %d / 语料词 %d" % (c, l)


def parse(path):
    """一份 txt → [{project, run, track, opinion, ts, raw}]。"""
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return []
    m = PROJ_RE.search(txt)
    proj = m.group(1) if m else ""
    m = RUN_RE.search(txt)
    run = m.group(1) if m else ""
    heads = list(TRACK_RE.finditer(txt))
    out = []
    if not heads:
        # 用户手写的自由格式：整份当一个条目
        out.append({"project": proj, "run": run, "track": "（整份反馈）",
                    "opinion": txt.strip()[:1200], "ts": "", "raw": txt})
        return out
    for i, m in enumerate(heads):
        start = m.end()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(txt)
        body = txt[start:end]
        op = OPINION_RE.search(body)
        opinion = (op.group(1).strip() if op else body.strip())[:1200]
        ts = TS_RE.search(body)
        out.append({"project": proj, "run": run, "track": m.group(1).strip(),
                    "opinion": opinion, "ts": ts.group(1) if ts else "", "raw": body})
    return out


def next_id(md_path, prefix):
    n = 0
    if os.path.exists(md_path):
        for m in re.finditer(r"^## (%s-(\d{3}))" % prefix, open(md_path, encoding="utf-8").read(), re.M):
            n = max(n, int(m.group(2)))
    return "%s-%03d" % (prefix, n + 1)


def render(kind, iid, it, src_name, note):
    lines = []
    lines.append("## %s · %s / %s" % (iid, it["project"] or "（未知作品）", it["track"]))
    lines.append("- **来源**：`processed/%s`" % src_name)
    if it["run"]:
        lines.append("- **运行 ID**：`%s`" % it["run"])
    lines.append("- **用户意见**：%s" % (it["opinion"] or "（未填写）").replace("\n", " "))
    if it["ts"]:
        lines.append("- **时间点**：%s" % it["ts"])
    lines.append("- **初判依据**：%s" % note)
    lines.append("- **定位**：待复现")
    lines.append("- **处理**：")
    lines.append("- **状态**：待处理")
    lines.append("")
    return "\n".join(lines)


def insert(md_path, block):
    s = open(md_path, encoding="utf-8").read()
    marker = "<!-- 新条目追加在下面，最新的在最前 -->"
    s = s.replace("\n_（暂无）_\n", "\n")
    if marker in s:
        i = s.index(marker) + len(marker)
        s = s[:i] + "\n\n" + block + s[i:]
    else:
        s += "\n" + block
    open(md_path, "w", encoding="utf-8").write(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写台账并移动原始文件")
    ap.add_argument("--dir", action="append", default=[], help="额外扫描目录")
    a = ap.parse_args()

    os.makedirs(INBOX, exist_ok=True)
    os.makedirs(DONE, exist_ok=True)

    files = []
    for d in [STUDIO_OUT, INBOX] + a.dir:
        if os.path.isdir(d):
            files.extend(sorted(glob.glob(os.path.join(glob.escape(d), "*.txt"))))
    files = [f for f in files if os.path.basename(f) != "README.txt"]

    if not files:
        print("没有待消化的反馈（扫过：%s）" % "、".join([STUDIO_OUT, INBOX] + a.dir))
        return 0

    print("待消化 %d 份反馈\n" % len(files))
    plan = {"code": [], "corpus": []}
    for f in files:
        items = parse(f)
        if not items:
            print("⚠️ 解析不出内容，跳过：%s" % f)
            continue
        for it in items:
            kind, note = classify(it["opinion"])
            plan[kind].append((f, it, note))

    for kind, label in (("code", "代码层"), ("corpus", "语料层")):
        if not plan[kind]:
            continue
        print("── %s：%d 条 ──" % (label, len(plan[kind])))
        for f, it, note in plan[kind]:
            print("   %s / %s  [%s]" % (it["project"] or "?", it["track"], note))
            print("      意见：%s" % (it["opinion"] or "（空）")[:80])

    if not a.apply:
        print("\n（干跑。加 --apply 才会写台账并移动文件）")
        return 0

    for kind, md, prefix in (("code", CODE_MD, "C"), ("corpus", CORPUS_MD, "L")):
        for f, it, note in plan[kind]:
            iid = next_id(md, prefix)
            insert(md, render(kind, iid, it, os.path.basename(f), note))
            print("+ %s  ← %s / %s" % (iid, it["project"], it["track"]))

    moved = 0
    for f in files:
        dst = os.path.join(DONE, os.path.basename(f))
        if os.path.exists(dst):
            dst = os.path.join(DONE, "%s_%s" % (time.strftime("%H%M%S"), os.path.basename(f)))
        shutil.move(f, dst)
        moved += 1
    print("\n台账已更新；%d 份原始反馈 → %s" % (moved, DONE))
    print("下一步：打开 ISSUES_*.md 人工复核分类与定位。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
