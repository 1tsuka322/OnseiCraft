# -*- coding: utf-8 -*-
"""导出「人工/外部模型逐行审校」表格（2026-10-28 R154）。

给外部审校者（人或另一个模型）看的，**不截断**、带**规则标记**，
这样对方能对着真实输入输出抓虫，而不是靠猜。

每行给六列：

| # | 时间 | ASR 原文（完整） | 清洗后日文 | 最终中文 | 标记 |

`清洗后日文` 只在跟原文不同时填（01 层摘拉丁词 / 02 层 errata 改错）。
`标记` 说明这行经历了什么：

| 标记 | 含义 |
|---|---|
| `latin:drop` | 01 层：整段判为 Whisper 拉丁幻觉，日文已置空 |
| `latin:strip` | 01 层：只摘掉了非白名单拉丁词（如 `何だったの ima no…` → `何だったの`） |
| `errata` | 02 层：命中错字表（如 補修→補習） |
| `src_latin` | 源语仍含非白名单拉丁 → 03 层会摘词重译 |
| `nonlex` | 日文侧判为纯非词汇音（拟声/气声）→ 走确定性遮蔽 |
| `masked` | 中文是「…」或空，但日文有内容 → 可能被兜底了，**重点看** |
| `short` | 中文字数 / 日文字数 < 0.4 → 可能漏译，**重点看** |
| `long` | 比例 > 2.0 → 可能注水 |

用法：
```bash
python work/scripts/export_review.py --run 20261028-201043_RJ1058739_production
python work/scripts/export_review.py --run <run_id> --track トラック1 --format tsv
```
"""
import argparse
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import scan_violations as SV  # noqa: E402
import translate as TR        # noqa: E402  （只为复用 _src_latin / _nonlexical 的判据）

RUNS = os.path.join(ROOT, "runs")
STRIP = re.compile(r"[\s、。，,.!?！？…‥~～ー〜]")


def load_lrc(p):
    d = {}
    for ln in open(p, encoding="utf-8"):
        m = re.match(r"^\[(\d+):([\d.]+)\](.*)", ln.rstrip("\n"))
        if m:
            d[round(int(m.group(1)) * 60 + float(m.group(2)), 2)] = m.group(3).strip()
    return d


def load_asr(d):
    """{start_rounded: seg}"""
    out = {}
    for s in (d or {}).get("segments") or []:
        out[round(float(s.get("start", 0)), 2)] = s
    return out


def ja_at(segs, t0, t1):
    """按时间窗拼日文（与 scan_violations.ja_in_range 同口径）。"""
    return SV.ja_in_range(segs, t0, t1)


def find_run(run_id=None, project=None):
    if run_id:
        p = os.path.join(RUNS, run_id)
        if not os.path.isdir(p):
            raise SystemExit("找不到 run：%s" % p)
        return p
    cands = sorted(glob.glob(os.path.join(RUNS, "*_%s_*" % project)))
    if not cands:
        raise SystemExit("找不到 %s 的 run" % project)
    return cands[-1]


def build(run_dir, track_filter=None):
    man = json.load(open(os.path.join(run_dir, "manifest.json"), encoding="utf-8"))
    rj = man["project"]
    final_dir = os.path.join(run_dir, "05_term_lock", rj)
    asr_dir = os.path.join(run_dir, "01_asr", rj, "asr")
    fix_dir = os.path.join(run_dir, "02_asr_fix", rj, "asr_fix")
    if not os.path.isdir(final_dir):
        raise SystemExit("没有 05_term_lock 产物：%s" % final_dir)

    rows_all = []
    for lrc in sorted(glob.glob(os.path.join(final_dir, "*.lrc"))):
        track = os.path.splitext(os.path.basename(lrc))[0]
        if track_filter and track_filter not in track:
            continue
        rows = SV.parse_lrc(lrc)
        zh = load_lrc(lrc)

        a_src = load_asr(json.load(open(os.path.join(asr_dir, track + ".json"),
                                       encoding="utf-8"))
                         if os.path.exists(os.path.join(asr_dir, track + ".json")) else {})
        fixp = os.path.join(fix_dir, track + ".json")
        a_fix = load_asr(json.load(open(fixp, encoding="utf-8"))) if os.path.exists(fixp) else {}
        segs = [s for _, s in sorted(a_src.items())]

        # ⚠️ 空行是**设计上的清屏 spacer**（读完即走），不是缺陷 —— 直接跳过，
        # 否则一张表里 40% 是噪音（实测 1163 行里 484 行是 spacer）。
        # 配对窗口也改成「到下一行**有内容**的行」，否则 spacer 的日文会串到别处。
        keep = [i for i, (_t, x) in enumerate(rows) if x]
        prev_zh = None
        for pos, i in enumerate(keep):
            t, text = rows[i]
            t1 = rows[keep[pos + 1]][0] if pos + 1 < len(keep) else t + 3
            # 只取「在本行时间窗内**开始**」的 ASR 段。
            # 一句 ASR 常被拆成 2~3 行字幕；如果按「重叠」取，同一句日文会重复贴
            # 到每一行上，看起来像漏译（实测 108 行假 short 全是这么来的）。
            # ⚠️ 边界必须留 epsilon：`332.84 < 332.84` 在浮点下可能为真
            # （LRC 时间戳是 float 运算出来的），会把**下一行**的段也吞进来，
            # 造成「上一行日文包含下一行」的假象（外部审校据此误判成 ASR 重影）。
            segs_in = [sg for k, sg in sorted(a_src.items())
                       if t - 0.05 <= k < t1 - 0.005]
            # `ja` 与 `ja_clean` 必须**同粒度**（都是本行时间窗内所有段的拼接）。
            # 以前 ja 用 01 的原文拼接、ja_clean 只取命中段的 fix_log 片段，
            # 结果看起来像「02 层把句子后半截截断了」——实际是列语义不一致
            # （外部审校据此误判，R154）。
            ja_raw = "".join((sg.get("text") or "") for sg in segs_in).strip()
            ja_clean = ""
            seg = segs_in[0] if segs_in else None
            flags = []
            if not ja_raw and text:
                flags.append("cont")      # 承接上一行，日文已在上面出现

            # ---- 01 层：拉丁幻觉（`action` 记在段上）----
            acts = set()
            for k, sg in sorted(a_src.items()):
                if t - 0.05 <= k < t1 - 0.005:
                    hl = sg.get("halluc_latin")
                    if hl:
                        acts.add(hl.get("action") or "drop")
            for act in sorted(acts):
                flags.append("latin:" + act)

            # ---- 02 层：errata（用 02 的 text，同粒度拼接）----
            fixed_parts, hit = [], False
            for k, sg in sorted(a_fix.items()):
                if t - 0.05 <= k < t1 - 0.005:
                    fixed_parts.append(sg.get("text") or "")
                    if sg.get("fix_log"):
                        hit = True
            if hit:
                flags.append("errata")
            ja_fixed = "".join(fixed_parts).strip()
            # 只有真的变了才填 ja_clean（空 = 与 ja 相同，表更干净）
            if ja_fixed and ja_fixed != ja_raw:
                ja_clean = ja_fixed

            # ---- 03/04 层：现算的判据（确定性） ----
            if ja_raw:
                if TR._src_latin(ja_raw):
                    flags.append("src_latin")
                if TR._nonlexical(ja_raw):
                    flags.append("nonlex")
            # 只有「日文有实字、中文却是省略号」才算可疑兜底；
            # 日文本身是 `…` / 拟声 的，出「…」是正确行为（实测 16 条里 12 条是这种）。
            if (text in ("…", "……") and ja_raw
                    and re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", ja_raw)):
                flags.append("masked")
            # 承接行把上一行的整句中文原样复制一遍（实测 1 处：一句日文被拆成
            # 两个时间戳，但中文没跟着切）。这是**显示层**的问题，不是翻译错。
            if prev_zh is not None and text and text == prev_zh and not ja_raw:
                flags.append("dup_cont")
            prev_zh = text
            rows_all.append({"track": track, "t": t, "ja": ja_raw, "n": pos + 1,
                             "ja_clean": ja_clean, "zh": text,
                             "flags": flags})
    # ---- 按「组」算长度比 ----
    # 一句 ASR 常被拆成多行字幕：日文只在第一行，译文分散在后续 cont 行。
    # 逐行比会把这些全判成漏译（实测 81 行假 short 都是这么来的）。
    i = 0
    while i < len(rows_all):
        r = rows_all[i]
        if r["ja"]:
            zh = r["zh"]
            j = i + 1
            while j < len(rows_all) and "cont" in rows_all[j]["flags"]:
                zh += rows_all[j]["zh"]
                j += 1
            jn = len(STRIP.sub("", r["ja"]))
            zn = len(STRIP.sub("", zh))
            # 阈值从 8 提到 10：短句的字符比天然偏低（`だってそうでしょ` 7 假名
            # → 「不是吗？」4 字 = 0.57 就报警），而拟声词意译更是必然偏低
            # （`ぎゅぎゅもにもに` → 「捏捏。」）。这类假阳性会淹没真问题。
            # 纯拟声/气声行豁免：`ぎゅぎゅぎゅ むにむにむにむにむに`（18 假名）
            # → 「揉揉揉 捏捏捏。」（7 字），比例必然低，但意译完全达意。
            if jn >= 10 and zh and not TR._nonlexical(r["ja"]):
                ratio = zn / jn
                if ratio < 0.4:
                    r["flags"].append("short")
                elif ratio > 2.0:
                    r["flags"].append("long")
        i += 1
    for r in rows_all:
        r["flags"] = ",".join(r["flags"])
    return man, rows_all


def esc(s):
    return (s or "").replace("|", "\\|").replace("\n", " ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="")
    ap.add_argument("--project", default="")
    ap.add_argument("--track", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--format", choices=["md", "tsv", "txt"], default="md",
                    help="txt = 竖线分隔纯文本（外部模型最容易读；tsv 的制表符常被吃掉）")
    ap.add_argument("--split", action="store_true",
                    help="按轨拆成多个文件（每轨一两百行，外部模型一次能读完）")
    ap.add_argument("--only-flagged", action="store_true",
                    help="只导出带标记的行（贴给外部模型时体积小得多）")
    a = ap.parse_args()
    run_dir = find_run(a.run or None, a.project or None)
    man, rows = build(run_dir, a.track or None)
    rj = man["project"]
    gates = ((man.get("stages", {}).get("06_checks") or {}).get("gate") or {}).get("detail") or {}
    cfg = man.get("config") or {}

    out = a.out or os.path.join(ROOT, "tmp", "review_%s%s.md" % (
        rj, "_flags" if a.only_flagged else ""))
    if a.format == "tsv":
        out = os.path.splitext(out)[0] + ".tsv"
    elif a.format == "txt":
        out = os.path.splitext(out)[0] + ".txt"

    if a.format == "tsv":
        with open(out, "w", encoding="utf-8") as f:
            f.write("track\tidx\tt\tja\tja_clean\tzh\tflags\n")
            for i, r in enumerate(rows, 1):
                f.write("%s\t%d\t%.2f\t%s\t%s\t%s\t%s\n" % (
                    r["track"], i, r["t"], r["ja"].replace("\t", " "),
                    r["ja_clean"].replace("\t", " "), r["zh"].replace("\t", " "),
                    r["flags"]))

    elif a.format == "txt":
        # 竖线分隔纯文本。**不用 tab**：贴进外部模型时 tab 常被吞掉/当缩进，
        # 整张表会错列（R154 实测 tsv 读不了）。
        def _txt(path, rs, title):
            with open(path, "w", encoding="utf-8") as f:
                if title:
                    f.write("%s\n%s\n\n" % (title, "=" * len(title)))
                f.write("格式：行号 | 时间 | ASR 原文 | 02修正后 | 最终中文 | 标记\n")
                f.write("（02修正后为空 = 与 ASR 原文相同；标记为空 = 无异常）\n\n")
                cur = None
                for r in rs:
                    if r["track"] != cur:
                        cur = r["track"]
                        f.write("\n---- %s ----\n" % cur)
                    f.write("%4d | %02d:%05.2f | %s | %s | %s | %s\n" % (
                        r["n"], int(r["t"] // 60), r["t"] % 60,
                        r["ja"] or "", r["ja_clean"] or "", r["zh"] or "",
                        r["flags"] or ""))
        head = "%s 逐行审校表（%d 行内容，%d 行带标记）" % (
            rj, len(rows), sum(1 for r in rows if r["flags"]))
        if a.split:
            by = {}
            for r in rows:
                by.setdefault(r["track"], []).append(r)
            base = os.path.splitext(out)[0]
            made = []
            for i, (tr, rs) in enumerate(sorted(by.items()), 1):
                fp = "%s_%02d.txt" % (base, i)
                _txt(fp, rs, "%s — 轨 %d/%d" % (head, i, len(by)))
                made.append(fp)
            print("→ 已拆 %d 个文件：%s ..." % (len(made), made[0]))
            return
        _txt(out, rows, head)
        print("→ %s（%d 行，%d 行带标记）" % (out, len(rows), sum(1 for r in rows if r["flags"])))
        return
        print("→ %s（%d 行）" % (out, len(rows)))
        return

    if a.only_flagged:
        rows = [r for r in rows if r["flags"]]
    by_track = {}
    for r in rows:
        by_track.setdefault(r["track"], []).append(r)
    n_flag = sum(1 for r in rows if r["flags"])
    lines = []
    lines.append("# 逐行审校表 · %s\n" % rj)
    lines.append("> run `%s` ｜ 引擎 `%s` ｜ cleanup-only `%s` ｜ 生成 %s\n"
                 % (os.path.basename(run_dir), cfg.get("engine"),
                    cfg.get("polish_cleanup_only"),
                    __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")))
    lines.append("门禁：" + "，".join("%s %s" % (k, "✅" if v.get("ok") else "❌")
                                    for k, v in gates.items()) + "\n")
    lines.append("共 **%d 行内容**（清屏 spacer 已跳过），其中 **%d 行带标记**（带标记的先看）。\n"
                 % (len(rows), n_flag))
    lines.append("""
**列说明**

| 列 | 说明 |
|---|---|
| ASR 原文 | Whisper 的原始输出，**完整未截断** |
| 清洗后日文 | 与原文不同才填（01 层摘拉丁词 / 02 层错字表改过） |
| 最终中文 | 交付成品里的那一行 |
| 标记 | 见下表 |

| 标记 | 含义 |
|---|---|
| `latin:drop` | 整段判为 Whisper 拉丁幻觉，日文已置空（成品多为「…」或空） |
| `latin:strip` | 只摘掉非白名单拉丁词，日文保留 |
| `errata` | 命中错字表（如 補修→補習） |
| `src_latin` | 源语仍含非白名单拉丁 → 翻译时摘词重译 |
| `nonlex` | 日文判为纯拟声/气声 → 走确定性遮蔽 |
| `masked` | 中文是「…」但日文有内容 → **可能误兜底，重点看** |
| `short` | 中文字数 < 日文 40% → **可能漏译，重点看** |
| `long` | 中文字数 > 日文 200% → 可能注水 |
| `cont` | 承接上一行（一句 ASR 被拆成多行字幕），日文见上一行 |
| `dup_cont` | 承接行的中文与上一行**完全相同** → 显示层没把中文切开 |
""")
    for track, rs in by_track.items():
        lines.append("\n## %s\n" % track)
        lines.append("| # | 时间 | ASR 原文 | 清洗后日文 | 最终中文 | 标记 |")
        lines.append("|---|---|---|---|---|---|")
        for r in rs:
            lines.append("| %d | %02d:%05.2f | %s | %s | %s | %s |" % (
                r["n"], int(r["t"] // 60), r["t"] % 60, esc(r["ja"]),
                esc(r["ja_clean"]), esc(r["zh"]), r["flags"]))
    open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("→ %s（%d 行，%d 行带标记）" % (out, len(rows), n_flag))


if __name__ == "__main__":
    main()
