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
            ja_raw = "".join((sg.get("text") or "") for sg in segs_in).strip()
            seg = segs_in[0] if segs_in else None
            flags, ja_clean = [], ""
            if not ja_raw and text:
                flags.append("cont")      # 承接上一行，日文已在上面出现

            # ---- 01 层：拉丁幻觉 ----
            hl = (seg or {}).get("halluc_latin")
            if hl:
                act = hl.get("action") or "drop"
                flags.append("latin:" + act)
                if act == "strip":
                    ja_clean = hl.get("old") or ""

            # ---- 02 层：errata ----
            fseg = a_fix.get(t) or {}
            fl = fseg.get("fix_log")
            if fl and fl.get("new"):
                flags.append("errata")
                ja_clean = fl["new"]

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
            if jn >= 8 and zh:
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
    ap.add_argument("--format", choices=["md", "tsv"], default="md")
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

    if a.format == "tsv":
        with open(out, "w", encoding="utf-8") as f:
            f.write("track\tidx\tt\tja\tja_clean\tzh\tflags\n")
            for i, r in enumerate(rows, 1):
                f.write("%s\t%d\t%.2f\t%s\t%s\t%s\t%s\n" % (
                    r["track"], i, r["t"], r["ja"].replace("\t", " "),
                    r["ja_clean"].replace("\t", " "), r["zh"].replace("\t", " "),
                    r["flags"]))
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
