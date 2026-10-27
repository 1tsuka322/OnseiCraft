# -*- coding: utf-8 -*-
"""空字幕检查：每个「应有字幕的行」在成品里必须真的有字。

为什么需要这个（R142/R143，2026-10-27）：
  RJ9000013 的 ASR 被注入过前缀垃圾（`９接` 等），翻译模型对这类句子**整句不译**，
  于是成品里出现**空文本行**。19 行观众什么都看不到，但三道现有检查全放过：
    - `check.py` 只管格式/标点，空文本行会被当成**清屏 spacer**（夹在两条非空行之间）；
    - `scan_violations.py` 只看日文侧，成品是空的不影响；
    - `trunc_audit.py` 只抓「有字但被截断」。
  所以这里换一个口径：**不猜空行的角色，直接拿「应该有的行」去对成品**。

「应该有的行」由 ASR 确定性重建，且**必须复用 `translate.build_lines`**——
它是翻译与润色共用的唯一真相（cue 模式走 cues.py，旧模式走显示行）。

用法：
  python empty_check.py --lrc <成品目录> --asr <ASR 目录> [--json out.json]
                        [--tol 0.35] [--max-miss 0] [--quiet]

退出码：0 = 通过；1 = 有缺失行（或缺失率超阈值）。
"""
import argparse
import glob
import json
import os
import re
import sys

# ⚠️ 必须在 import translate/cues **之前**定口径：`cues.ENABLED` 在 import 时读一次。
# 交付口径是 cue 模式（cue_pipeline=1）；不设会静默退成旧的「显示行」模式，
# 重建出来的行数与生产对不上（实测 489 vs 547 行 → 73 条假缺失）。
os.environ.setdefault("KIKOERU_CUE_PIPELINE", "1")

TS = re.compile(r"^\[(\d+):([\d.]+)\]")


def read_lrc(p):
    """→ [(秒, 文本)]，跳过 [ti:]/[by:] 等标签行。"""
    rows = []
    for line in open(p, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line.startswith("[") or line.startswith(("[ti:", "[by:", "[ar:", "[al:")):
            continue
        m = TS.match(line)
        if m:
            rows.append((int(m.group(1)) * 60 + float(m.group(2)), line[m.end():].strip()))
    return rows


def _prio(path, base):
    """ASR 版本优先级：asr_fix(0) > asr(1) > 其他(5) > asr_raw(9)。

    与 pipeline_lib.stages.effective_asr 同向：**绝不能拿 asr_raw 当成品口径**，
    它没做过归一化/合并，重建出来的行数与生产对不上（实测 489 vs 547）。
    """
    parts = os.path.relpath(path, base).split(os.sep)
    if "asr_fix" in parts:
        return 0
    if "asr" in parts:
        return 1
    if "asr_raw" in parts:
        return 9
    return 5


def load_asr_index(asr_dir):
    """→ {basename_without_ext: path}，只收 `{"segments": [...]}` 形态的文件。

    ⚠️ `asr_fix/` 里可能混着候选清单（`[{rule,t,base,...}]`），必须按形态过滤
    （与 pipeline_lib.stages.effective_asr 同一判据）。
    """
    idx, best = {}, {}
    for p in glob.glob(os.path.join(asr_dir, "**", "*.json"), recursive=True):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if not (isinstance(d, dict) and d.get("segments")):
            continue
        stem = os.path.basename(p)[:-5]
        pr = _prio(p, asr_dir)
        if stem not in idx or pr < best[stem]:
            idx[stem], best[stem] = p, pr
    return idx


def expected_lines(segments):
    """复用 translate.build_lines（cue 模式走 cues.py，旧模式走显示行）。"""
    import translate
    return translate.build_lines(segments)


def match_asr(lrc_name, idx, asr_dir):
    stem = lrc_name[:-4] if lrc_name.endswith(".lrc") else lrc_name
    if stem in idx:
        return idx[stem]
    # v1 文件名带 .mp3，v2 不带
    for cand in (stem + ".mp3", stem.replace(".mp3", "")):
        if cand in idx:
            return idx[cand]
    return None


def _selftest():
    """自检：合成一对 ASR/LRC，验证「正常→0、挖空→1、删行→1、时间戳偏移→不误判」。

    R139 的教训：评测脚本必须先证明它在**正确输入上给 0**，否则错的口径会一直静默。
    """
    import tempfile
    import translate

    segs = [
        {"start": 1.0, "end": 2.4, "text": "おはようございます。",
         "words": [{"s": 1.0, "e": 2.4, "w": "おはようございます。"}]},
        {"start": 3.0, "end": 4.6, "text": "今日はいい天気ですね。",
         "words": [{"s": 3.0, "e": 4.6, "w": "今日はいい天気ですね。"}]},
        {"start": 6.0, "end": 7.8, "text": "散歩でもしませんか。",
         "words": [{"s": 6.0, "e": 7.8, "w": "散歩でもしませんか。"}]},
    ]
    exp = translate.build_lines(segs)
    if len(exp) < 2:
        print("✗ 自检失败：合成样本只切出 %d 行，样本构造有问题" % len(exp))
        return 1

    def write_lrc(rows):
        buf = ["[ti:selftest]", "[by:empty_check]"]
        for t, txt in rows:
            buf.append("[%02d:%05.2f]%s" % (int(t // 60), t % 60, txt))
        return "\n".join(buf) + "\n"

    def run(rows, tag, want):
        with tempfile.TemporaryDirectory() as td:
            lrc = os.path.join(td, "lrc")
            asr = os.path.join(td, "asr")
            os.makedirs(lrc)
            os.makedirs(asr)
            json.dump({"segments": segs}, open(os.path.join(asr, "t.json"), "w",
                                               encoding="utf-8"), ensure_ascii=False)
            open(os.path.join(lrc, "t.lrc"), "w", encoding="utf-8").write(write_lrc(rows))
            import io
            import contextlib
            buf = io.StringIO()
            old = sys.argv
            try:
                sys.argv = ["empty_check.py", "--lrc", lrc, "--asr", asr,
                            "--json", os.path.join(td, "o.json")]
                with contextlib.redirect_stdout(buf):
                    code = main()
            finally:
                sys.argv = old
            got = json.load(open(os.path.join(td, "o.json"), encoding="utf-8"))
            n = got["summary"]["missing"]
            ok = (n == want) and (code == (0 if want == 0 else 1))
            print("%s %-22s 缺失 %d（期望 %d）exit=%d" % ("✓" if ok else "✗", tag, n, want, code))
            return ok

    good = [(ln["start"], "译文%d" % i) for i, ln in enumerate(exp)]
    # 每条真行后面补一个清屏 spacer（合法空行）
    good_sp = []
    for t, txt in good:
        good_sp.append((t, txt))
        good_sp.append((t + 0.9, ""))
    ok = run(good_sp, "正常输入→0", 0)
    # 挖空第一行（spacer 仍在）→ 必须报 1
    blanked = list(good_sp)
    blanked[0] = (blanked[0][0], "")
    ok &= run(blanked, "挖空一行→1", 1)
    # 整行删掉 → 必须报 1
    ok &= run(good_sp[2:], "删掉一行→1", 1)
    # 时间戳整体偏移 0.2s（容差内）→ 仍应 0
    shifted = [(t + 0.2, txt) for t, txt in good_sp]
    ok &= run(shifted, "偏移0.2s→0", 0)
    print("\n自检:", "通过" if ok else "失败")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lrc", help="成品 LRC 目录")
    ap.add_argument("--asr", help="ASR json 目录（可含子目录）")
    ap.add_argument("--json", default=None, help="把明细写成 json")
    ap.add_argument("--tol", type=float, default=0.35, help="行起点匹配容差（秒）")
    ap.add_argument("--max-miss", type=float, default=0.0,
                    help="允许的缺失率（0 = 一行都不许缺）")
    ap.add_argument("--drift-rate", type=float, default=0.03,
                    help="单轨缺失率超过此值且缺失≥%d 行 → 判为「版本漂移」而非缺陷"
                         % 5)
    ap.add_argument("--drift-min", type=int, default=5,
                    help="判漂移所需的最少缺失行数（防小轨误判）")
    ap.add_argument("--selftest", action="store_true", help="跑自检（合成样本）")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if not (a.lrc and a.asr):
        ap.error("需要 --lrc 与 --asr（或用 --selftest）")

    files = sorted(glob.glob(os.path.join(a.lrc, "*.lrc")))
    if not files:
        print("没有找到 lrc：", a.lrc)
        return 1
    idx = load_asr_index(a.asr)
    if not idx:
        print("ASR 目录里没有可用的 json（需 {\"segments\": [...]} 形态）：", a.asr)
        return 1

    rows_out = []
    tot = {"tracks": 0, "expected": 0, "missing": 0, "no_asr": 0,
           "empty_lines": 0, "text_lines": 0, "drift_tracks": 0, "drift_missing": 0}
    bad = []
    drift = []
    for p in files:
        name = os.path.basename(p)
        ap_ = match_asr(name, idx, a.asr)
        if not ap_:
            tot["no_asr"] += 1
            if not a.quiet:
                print("%-46s  !! 找不到对应 ASR" % name[:44])
            rows_out.append({"track": name, "status": "no_asr"})
            continue
        d = json.load(open(ap_, encoding="utf-8"))
        try:
            exp = expected_lines(d.get("segments") or [])
        except Exception as e:      # 单轨失败不该拖垮整批
            print("%-46s  !! 重建失败：%s" % (name[:44], e))
            rows_out.append({"track": name, "status": "error", "error": str(e)})
            continue
        got = read_lrc(p)
        starts = [t for t, txt in got if txt]
        empty_lines = sum(1 for t, txt in got if not txt)
        miss = []
        for ln in exp:
            t0 = ln.get("start")
            if t0 is None:
                continue
            if not any(abs(t0 - s) <= a.tol for s in starts):
                miss.append({"t": round(float(t0), 2),
                             "ja": (ln.get("text") or "")[:60]})
        tot["tracks"] += 1
        tot["expected"] += len(exp)
        tot["empty_lines"] += empty_lines
        tot["text_lines"] += len(starts)
        # 漂移守卫：真缺陷是零星几行（R142 全库 19 行 / 22800 ≈ 0.08%），
        # 版本漂移是整轨成片对不上（存量 16 部实测 5%–51%）。
        rate_t = len(miss) / max(1, len(exp))
        is_drift = rate_t > a.drift_rate and len(miss) >= a.drift_min
        if is_drift:
            tot["drift_tracks"] += 1
            tot["drift_missing"] += len(miss)
            drift.append(name)
        else:
            tot["missing"] += len(miss)
            if miss:
                bad.append(name)
        if not a.quiet:
            tag = "  <-- 漂移(跳过)" if is_drift else ("  <-- 需检查" if miss else "")
            print("%-46s 应有%4d  实有%4d  空行%4d  缺失%3d%s"
                  % (name[:44], len(exp), len(starts), empty_lines, len(miss), tag))
        rows_out.append({"track": name,
                         "status": "drift" if is_drift else ("missing" if miss else "ok"),
                         "expected": len(exp), "text_lines": len(starts),
                         "empty_lines": empty_lines, "missing": miss})

    rate = tot["missing"] / max(1, tot["expected"])
    print("\n合计 %d 轨：应有 %d 行，缺失 %d 行（%.2f%%）；空文本行 %d（含清屏 spacer）"
          % (tot["tracks"], tot["expected"], tot["missing"], 100 * rate, tot["empty_lines"]))
    if tot["drift_tracks"]:
        print("⚠️ %d 轨判为版本漂移（ASR/代码与成品不同版，%d 行对不上）→ 不计入失败：%s"
              % (tot["drift_tracks"], tot["drift_missing"], ", ".join(drift[:4])))
    if tot["no_asr"]:
        print("⚠️ %d 轨找不到 ASR，未纳入检查" % tot["no_asr"])

    ok = rate <= a.max_miss
    print("结论:", "全部通过" if ok else "存在空字幕：%s" % ", ".join(bad[:6]))
    if a.json:
        json.dump({"summary": tot, "miss_rate": rate, "tracks": rows_out},
                  open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
