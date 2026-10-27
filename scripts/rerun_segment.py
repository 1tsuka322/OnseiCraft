# -*- coding: utf-8 -*-
"""片段级 ASR 重跑（2026-10-20，GITHUB_SURVEY 缺口④）。

场景：某一小段听错了/漏了，不想整轨重跑（10 分钟起），只想修那几秒。

用法：
  # 先看某时间点附近有什么段
  python rerun_segment.py --rj RJ9000001 --track "01.xxx.mp3" --list 120 150
  # 干跑（打印旧段 vs 新段，不落盘）
  python rerun_segment.py --rj RJ9000001 --track "01.xxx.mp3" --start 120 --end 150
  # 落盘：替换 asr json 里的旧段 + 写 asr_rec（供 translate/polish/splice 补回成品）
  python rerun_segment.py ... --start 120 --end 150 --apply

设计要点：
  · 切片两侧各留 --pad 秒（默认 3s）给模型上下文，但**只采纳落在 [start,end] 内的新段**
  · 同一组参数 + 同一套过滤（is_silence_seg / is_hallucination / is_ts_hallucination）
  · 旧 json 备份到 draft/<rj>/asr_bak/<track>.<时间戳>.json
  · 台本打轴产物（script_aligned）**拒绝执行**：重跑会丢掉台本文本，应重跑 script_align
"""
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json
import time
import shutil
import argparse

SCRIPTS = os.path.join(os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "skills", "kikoeru-subtitle", "scripts")
sys.path.insert(0, SCRIPTS)
import asr as A                                   # noqa: E402
from faster_whisper import WhisperModel           # noqa: E402
from faster_whisper.audio import decode_audio     # noqa: E402

ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL = os.environ.get("FW_MODEL", r"D:\fasterwhisperGUI-0.8.5.sfx\models\faster-whisper-large-v3")


def load_json(p):
    return json.load(open(p, encoding="utf-8"))


def find_audio(rj, track):
    import glob
    for p in glob.glob(os.path.join(ROOT, rj, "**", track), recursive=True):
        return p
    return None


def transcribe_slice(model, pcm, t0, t1, pad, freetalk=False):
    """切 [t0-pad, t1+pad] 识别，只返回落在 [t0,t1] 内的段（时间已加回偏移）。"""
    a, b = max(0.0, t0 - pad), min(len(pcm) / 16000.0, t1 + pad)
    segs, _ = model.transcribe(
        pcm[int(a * 16000):int(b * 16000)], language="ja", beam_size=5,
        vad_filter=False, condition_on_previous_text=False,
        word_timestamps=True, initial_prompt="以下是一段日语对话的转写。")
    out = []
    for s in segs:
        if A.is_silence_seg(s) or A.is_ts_hallucination(s, freetalk):
            continue
        txt = (s.text or "").strip()
        if not txt or A.is_hallucination(txt, freetalk):
            continue
        st, en = a + float(s.start), a + float(s.end)
        if st < t0 - 0.4 or st > t1:      # 只采纳目标区间内的段
            continue
        words = [{"s": round(a + float(w.start), 3), "e": round(a + float(w.end), 3),
                  "w": w.word} for w in (s.words or [])
                 if w.start is not None and w.end is not None]
        out.append({"start": round(st, 3), "end": round(en, 3), "text": txt,
                    "words": words,
                    "no_speech_prob": round(float(getattr(s, "no_speech_prob", -1)), 4),
                    "avg_logprob": round(float(getattr(s, "avg_logprob", 0)), 4)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rj", required=True)
    ap.add_argument("--track", required=True, help="音频文件名（含扩展名）")
    ap.add_argument("--start", type=float)
    ap.add_argument("--end", type=float)
    ap.add_argument("--list", nargs=2, type=float, metavar=("T0", "T1"),
                    help="只列出该区间的现有段，不重跑")
    ap.add_argument("--pad", type=float, default=3.0)
    ap.add_argument("--no-keep-bounds", dest="keep_bounds", action="store_false",
                    help="不按旧段边界重分配（默认按旧边界分配，保持成品行结构不变）")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    jp = os.path.join(ROOT, "data", "draft", a.rj, "asr", a.track + ".json")
    if not os.path.exists(jp):
        sys.exit("找不到 ASR：%s" % jp)
    d = load_json(jp)
    if d.get("script_aligned"):
        sys.exit("该轨是台本打轴产物（script_aligned）——重跑会丢掉台本文本。\n"
                 "请改用 script_align.py 重新打轴。")
    segs = d.get("segments") or []

    if a.list:
        t0, t1 = a.list
        for s in segs:
            if t0 - 1 <= s["start"] <= t1 + 1:
                print("%8.2f-%-8.2f %s" % (s["start"], s["end"], s["text"]))
        return
    if a.start is None or a.end is None:
        sys.exit("需要 --start/--end，或 --list T0 T1")

    old = [s for s in segs if s["start"] < a.end and s["end"] > a.start]
    print("旧段 %d 个（%.1f-%.1f 秒）：" % (len(old), a.start, a.end))
    for s in old:
        print("  %8.2f-%-8.2f %s" % (s["start"], s["end"], s["text"]))

    audio = find_audio(a.rj, a.track)
    if not audio:
        sys.exit("找不到音频：%s" % a.track)
    pcm = decode_audio(audio, sampling_rate=16000)
    model = WhisperModel(MODEL, device="cuda", compute_type="float16")
    new = transcribe_slice(model, pcm, a.start, a.end, a.pad)
    print("\n新段 %d 个：" % len(new))
    for s in new:
        print("  %8.2f-%-8.2f %s" % (s["start"], s["end"], s["text"]))
    if not new:
        print("\n⚠️ 新段为空（该区间可能确实没有台词）。用 --apply 会**删掉**旧段，谨慎。")

    # 按旧段边界重分配新文本：带 padding 重跑常把多段并成一段，
    # 直接替换会改变成品行结构 → 用新段的**词级时间**把文字分回旧段区间。
    if a.keep_bounds and old and new:
        words = [(w["s"], w["w"]) for x in new for w in (x.get("words") or [])]
        rebuilt = []
        for o in old:
            txt = "".join(w for st, w in words
                          if o["start"] - 0.25 <= st < o["end"] - 0.05).strip()
            if not txt:
                txt = o["text"]          # 该区间没有新词 → 保留旧文本（宁可不改）
            rebuilt.append(dict(o, text=txt))
        print("\n按旧段边界重分配（%d 段）：" % len(rebuilt))
        for o in rebuilt:
            print("  %8.2f-%-8.2f %s" % (o["start"], o["end"], o["text"]))
        new = rebuilt

    if not a.apply:
        print("\n（干跑，未落盘。加 --apply 应用）")
        return

    # 备份 + 替换
    bakdir = os.path.join(ROOT, "data", "draft", a.rj, "asr_bak")
    os.makedirs(bakdir, exist_ok=True)
    bak = os.path.join(bakdir, "%s.%s.json" % (a.track, time.strftime("%Y%m%d_%H%M%S")))
    shutil.copy2(jp, bak)
    merged = [s for s in segs if not (s["start"] < a.end and s["end"] > a.start)] + new
    merged.sort(key=lambda x: x["start"])
    d["segments"] = merged
    d["segment_rerun"] = d.get("segment_rerun", []) + [
        {"start": a.start, "end": a.end, "replaced": len(old), "new": len(new),
         "at": time.strftime("%Y-%m-%d %H:%M:%S")}]
    json.dump(d, open(jp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n已替换：%s（备份 %s）" % (jp, os.path.basename(bak)))

    # 供补回成品：只含新段的 asr_rec
    recdir = os.path.join(ROOT, "data", "draft", a.rj, "asr_rec")
    os.makedirs(recdir, exist_ok=True)
    rp = os.path.join(recdir, a.track + ".json")
    r = dict(d)
    r["segments"] = new
    r["recovered_from"] = "segment_rerun_%s" % time.strftime("%Y%m%d")
    json.dump(r, open(rp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("已写 asr_rec：%s" % rp)
    print("\n下一步（把新译文补回成品）：")
    print("  1) python serve.py start")
    print("  2) python translate.py %s %s" % (recdir, os.path.join(ROOT, "data", "draft", a.rj, "draft_rec")))
    print("  3) python polish.py %s %s %s" % (recdir,
          os.path.join(ROOT, "data", "draft", a.rj, "draft_rec"),
          os.path.join(ROOT, "kikoerusubtitle_rec", a.rj)))
    print("  4) python recover_splice.py splice --work %s --apply" % a.rj)


if __name__ == "__main__":
    main()
