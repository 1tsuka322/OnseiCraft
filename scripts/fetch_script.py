# -*- coding: utf-8 -*-
"""检测并下载 asmr.one 作品的台本（PDF 优先，其次逐轨 txt），自动提取成 txt。

## 台本在哪（2026-10-18 查证）

asmr.one 的 track 树里，台本在**两个**位置（第一次查漏了 `other` 这类）：

| type | 文件名 | 说明 |
|---|---|---|
| `other` | `マゾオス保育園_仮台本.pdf` | **PDF 台本**（整本，竖排） |
| `text`  | `仮台本トラック1.txt` … | **逐轨 txt 台本** |

⚠️ `type=='text'` 里还有 `readme.txt`/`说明.txt`（作品说明，**不是台本**），
必须按文件名筛（含「台本」/「セリフ」/`script`）。

## 为什么 PDF 优先

PDF 覆盖更广（RJ9000013/RJ9000014/RJ9000007 只有 PDF 没 txt）。竖排 PDF 用
`extract_script.py` 按坐标重排就能干净提取，实测 RJ9000013 41 页 → 27k 字符。

用法：
  python fetch_script.py <RJ号> [--draft-root DIR] [--force]
  例：python fetch_script.py 1715662
"""
import os
import re
import sys
import json
import glob
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", "..", "..", ".."))
TMP = os.path.join(ROOT, "data", "tmp")
sys.path.insert(0, TMP)

SCRIPT_WORD = re.compile(r"台本|セリフ|せりふ|script|シナリオ", re.I)
# ⚠️ 2026-10-19 新踩：部分作品把台本**按语言**各放一份
# （`05_別言語(Multilingual)/英語_English` / `簡体字` / `繁体字`），
# 而日文原件在 `03_台本`。不过滤的话会一次下 25 个 PDF、提取出 3 种语言的正文，
# 拿去和日文 ASR 对齐 → 全段不匹配、被当「演出指示」整段丢掉。
NONJA_DIR = re.compile(r"英語|English|簡体|简体|繁体|繁體|中文|中国語|韓国|Korean|"
                       r"別言語|Multilingual", re.I)


def split_lang(items):
    """按路径把台本分成 (日文候选, 非日文候选)。路径里出现「台本」的优先算日文。"""
    ja, other = [], []
    for it in items:
        p = it[2] if len(it) > 2 else ""
        (other if NONJA_DIR.search(p) else ja).append(it)
    return ja, other



def load_api():
    """复用 download.py 的 API 封装（token + UA）。"""
    import download as D
    D.TOKEN = D.get_token()
    return D


def find_scripts(D, wid):
    """返回 (pdfs, txts)：[(title, url, path)]。"""
    tr = D.get(D.API + "/tracks/%d" % wid, 40)
    pdfs, txts = [], []
    for n, _p in D.walk_path(tr):
        t = n.get("type")
        ti = (n.get("title") or "")
        url = n.get("mediaDownloadUrl") or ""
        if not url:
            continue
        lo = ti.lower()
        path = "/".join(_p)
        if t == "other" and lo.endswith(".pdf"):
            pdfs.append((ti, url, path))
        elif t == "text" and lo.endswith(".txt") and SCRIPT_WORD.search(ti):
            txts.append((ti, url, path))
    return pdfs, txts


def fetch(D, url, dst):
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return os.path.getsize(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    cmd = ["curl", "-k", "-s", "-L", "--retry", "3", "--connect-timeout", "30",
           "--max-time", "180", "-A", D.UA_STR]
    if D.TOKEN:
        cmd += ["-H", "Authorization: Bearer " + D.TOKEN]
    cmd += ["-o", dst, url]
    subprocess.run(cmd, capture_output=True)
    return os.path.getsize(dst) if os.path.exists(dst) else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rj", help="RJ 号，可带前导零（RJ01715662 / 1715662）")
    ap.add_argument("--draft-root", default="")
    ap.add_argument("--force", action="store_true", help="已有提取结果也重做")
    a = ap.parse_args()

    m = re.match(r"^(?:RJ)?0*(\d+)$", a.rj.strip(), re.I)
    if not m:
        raise SystemExit("RJ 号格式不对：%s" % a.rj)
    wid = int(m.group(1))
    # ⚠️ 不能 `RJ%07d`：RJ 号有 6/7/8 位（RJ9000013 / RJ1715662 / RJ01234567），
    #    补零会把 6 位的写成 RJ0256650，台本落到一个**不存在的作品目录**，
    #    后面 script_align 找不到 ASR（实测 2026-10-19）。
    rj = "RJ%d" % wid

    draft_root = a.draft_root or os.path.join(ROOT, "data", "draft")
    out_dir = os.path.join(draft_root, rj, "script")
    os.makedirs(out_dir, exist_ok=True)

    D = load_api()
    pdfs, txts = find_scripts(D, wid)
    print("%s：PDF 台本 %d 个 / txt 台本 %d 个" % (rj, len(pdfs), len(txts)))
    if not pdfs and not txts:
        print("→ 该作品**没有台本**，走正常 ASR 流程")
        return 1

    # 多语言台本：只取日文那份（没有日文才退回全部，并明确警告）
    pdfs_ja, pdfs_other = split_lang(pdfs)
    txts_ja, txts_other = split_lang(txts)
    if pdfs_other or txts_other:
        print("   ⚠️ 跳过非日文台本 %d 个（%s…）"
              % (len(pdfs_other) + len(txts_other),
                 ((pdfs_other + txts_other)[0][2] or "")[:28]))
    if pdfs_ja or txts_ja:
        pdfs, txts = pdfs_ja, txts_ja
    else:
        print("   ⚠️ 没找到日文目录的台本，退回全部（对齐很可能失败）")

    # 逐轨 txt 先落地（它们已经是纯文本，最省事）
    for ti, url, _path in txts:
        dst = os.path.join(out_dir, ti)
        sz = fetch(D, url, dst)
        print("  txt  %-34s %7d B" % (ti[:34], sz))

    # PDF 提取成 txt（同名 .txt）
    for ti, url, _path in pdfs:
        pdf_dst = os.path.join(out_dir, ti)
        sz = fetch(D, url, pdf_dst)
        print("  pdf  %-34s %7d B" % (ti[:34], sz))
        if sz <= 0:
            continue
        out_txt = os.path.join(out_dir, os.path.splitext(ti)[0] + ".extracted.txt")
        if os.path.exists(out_txt) and not a.force:
            print("       已有提取结果，跳过")
            continue
        r = subprocess.run([sys.executable, os.path.join(HERE, "extract_script.py"),
                            pdf_dst, "--out", out_txt], capture_output=True, text=True)
        if r.returncode == 0:
            print("       → %s" % os.path.basename(out_txt))
        else:
            print("       提取失败：%s" % (r.stderr or "")[-160:])

    got = glob.glob(os.path.join(out_dir, "*.txt"))
    print("\n台本就绪：%s（%d 个 txt）" % (out_dir, len(got)))
    print("下一步：python script_align.py %s <asr_dir> <asr_dir>" % out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
