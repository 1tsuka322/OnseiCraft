# -*- coding: utf-8 -*-
"""ASR 幻觉检测（2026-10-18，源自盲听 N19 用户实证）。

用户听 N19（RJ9000015 01 @1206.8s）反馈「这段音频完全是舔耳音，根本就没说话」，
但 ASR 出了 `こういうのもいいでしょう`。查词级时间戳发现崩塌：
`の` 一个词跨 25.3 秒、`こう`/`いう`/`も` 零宽 —— 是 Whisper 在无语音段硬凑的幻觉。

两类可判定规则（其余只记录不判定，避免误伤真实的长音/气声）：
  A 幻觉词表 + 时长过长
     `おやすみなさい` 正常 1.5–2s，占 20s 必然是幻觉（Whisper 的经典填充词）。
     同类：ご視聴ありがとうございました / チャンネル登録 / 最後までご視聴。
  B 词级时间戳崩塌
     单个 word 跨度 > 15s，或零宽词（时长 < 0.05s）占比 > 60%，且总字数 < 15。
     → 内容可能是幻觉，也可能只是时间戳坏了，标 `review` 交人工/能量复核。

用法：
  python asr_halluc.py --scan            # 全库扫描出报告
  python asr_halluc.py --scan --rj RJ9000015
"""
import glob
import json
import os
import re
import sys
sys.stdout.reconfigure(encoding="utf-8")
import collections

DRAFT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "..", "..", "..", "draft"))

# Whisper 在静音/纯音效段的经典填充词（YouTube 字幕训练数据的残留）
HALLUC_WORDS = [
    "おやすみなさい", "おやすみ", "ご視聴ありがとうございました",
    "ご視聴ありがとうございます", "チャンネル登録", "最後までご視聴",
    "ありがとうございました", "字幕", "ご覧いただき",
]
HALLUC_MIN_DUR = 8.0        # 这些词超过 8 秒必是幻觉
HALLUC_MAX_CHARS = 15       # 且整句字数 ≤ 15（长句里的"晚安"是真实台词，别误杀）
# ⚠️ 还要看**词级时间戳**：真实的轨尾"晚安"说话只占 1.5–3s，cue 长是因为后面有静音；
#    幻觉则把整段静音都算进这个词（span 13–23s）。实测 span≤9 的全是真实台词。
HALLUC_MIN_SPAN = 10.0
TS_COLLAPSE_SPAN = 15.0     # 单个词跨度阈值
TS_ZERO_RATIO = 0.6
TS_MIN_DUR = 10.0          # 崩塌判据的最小时长（短促气声不算）
TS_MAX_CHARS = 15


def n_chars(t):
    return len(re.sub(r"[^\u3040-\u30ff\u4e00-\u9fff]", "", t))


def scan_one(seg):
    t = (seg.get("text") or "").strip()
    if not t:
        return None
    dur = (seg.get("end") or 0) - (seg.get("start") or 0)
    n = n_chars(t)
    ws = seg.get("words") or []
    max_span = 0.0
    zero = 0
    for w in ws:
        sp = (w.get("e", 0) or 0) - (w.get("s", 0) or 0)
        max_span = max(max_span, sp)
        if sp < 0.05:
            zero += 1
    zr = (zero / len(ws)) if ws else 0.0

    for hw in HALLUC_WORDS:
        # ⚠️ 必须同时满足「时长过长」+「整句很短」：
        #    `おやすみなさい` 独占 20s = 幻觉；
        #    `今日は、姉上の匂いを胸いっぱいに感じながら、はい、おやすみなさい。`
        #    占 16s 是**真实台词**（语速慢但内容完整），不能杀。
        if (hw in t and dur >= HALLUC_MIN_DUR and n <= HALLUC_MAX_CHARS
                and max_span >= HALLUC_MIN_SPAN):
            return {"kind": "A_halluc_word", "dur": round(dur, 1), "n": n,
                    "max_span": round(max_span, 1), "zero_ratio": round(zr, 2),
                    "why": "幻觉词「%s」独占 %.1fs（%d 字 / 词跨 %.1fs）" % (hw, dur, n, max_span),
                    "text": t}
    # ⚠️ 必须加最小时长：`ん`/`あ` 这类 0.2s 的短促气声，零宽词占比天然高，
    # 但那不是崩塌。只有「时长够长却几乎没内容」才是时间戳崩塌。
    if dur >= TS_MIN_DUR and n < TS_MAX_CHARS \
            and (max_span >= TS_COLLAPSE_SPAN or zr >= TS_ZERO_RATIO):
        return {"kind": "B_ts_collapse", "dur": round(dur, 1), "n": n,
                "max_span": round(max_span, 1), "zero_ratio": round(zr, 2),
                "why": "词级时间戳崩塌（单字跨 %.1fs / 零宽 %.0f%%）"
                       % (max_span, zr * 100), "text": t}
    return None


def scan(rj_filter=None):
    recs = []
    for p in sorted(glob.glob(os.path.join(DRAFT, "*", "asr", "*.json"))):
        rj = os.path.basename(os.path.dirname(os.path.dirname(p)))
        if rj_filter and rj != rj_filter:
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for seg in (d.get("segments") or d.get("cues") or []):
            r = scan_one(seg)
            if r:
                r.update(rj=rj, track=os.path.basename(p)[:-5],
                         start=round(seg.get("start") or 0, 1))
                recs.append(r)
    return recs


def main():
    args = sys.argv[1:]
    rj = None
    if "--rj" in args:
        rj = args[args.index("--rj") + 1]
    recs = scan(rj)
    c = collections.Counter(r["kind"] for r in recs)
    print("扫描完成：疑似 ASR 幻觉 %d 条" % len(recs))
    for k, v in c.most_common():
        print("  %-16s %d" % (k, v))
    print()
    for k in ("A_halluc_word", "B_ts_collapse"):
        rs = [r for r in recs if r["kind"] == k]
        if not rs:
            continue
        print("=== %s（%d 条）===" % (k, len(rs)))
        for r in rs:
            print("  %s / %s @%.1fs  dur=%.1fs  %s" %
                  (r["rj"], r["track"][:34], r["start"], r["dur"], r["why"]))
            print("      %r" % r["text"][:60])
        print()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "..", "..", "tmp", "daily", "asr_halluc.json")
    out = os.path.normpath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(recs, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("明细 → %s" % out)


if __name__ == "__main__":
    main()
