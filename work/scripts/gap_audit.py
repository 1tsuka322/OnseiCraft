#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ASR 时间空洞审计：空洞里到底有没有声音？

背景（2026-11-08）：
  给 selfcheck 加「ASR 相邻段最大间隔」指标后，全库扫出 **109 处 ≥20 秒空洞**，
  最大 241 秒。但空洞**不能一律当漏听** —— 音声作品有大量纯音效 / 喘息段落，
  whisper 不出文本是正常的。

  所以需要客观判据：**解出空洞区间的音频能量**。
    · 能量接近该轨中位数 → 有人在说话但 ASR 没出 → **可疑漏听**
    · 能量明显低于中位数（静音/底噪）→ 正常空洞
    · 能量很高但无文本（纯喘息/水声）→ 拟声段，正常

用法：
  python scripts/gap_audit.py --rj RJ1289104              # 单部
  python scripts/gap_audit.py --all --min-gap 30          # 全库，只看 ≥30 秒
  python scripts/gap_audit.py --rj RJ1289104 --detail     # 打印每处明细
"""
import argparse
import glob
import io
import json
import os
import re
import sys

import numpy as np

if hasattr(sys, "stdout") and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNS = os.path.join(ROOT, "runs")
AUDIO_EXT = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".aac")


def db(x):
    return 20.0 * np.log10(max(float(x), 1e-9))


def track_rms(path, segs=None):
    """整轨 16k 单声道波形。返回 (波形, 采样率) 或 (None, 0)。"""
    try:
        import av
    except Exception:
        return None, 0
    try:
        buf = []
        with av.open(path) as c:
            rs = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for fr in c.decode(audio=0):
                for rf in rs.resample(fr):
                    buf.append(rf.to_ndarray().reshape(-1))
        if not buf:
            return None, 0
        return np.concatenate(buf).astype(np.float32) / 32768.0, 16000
    except Exception:
        return None, 0


_IDX = {}


def find_audio(run_dir, rj, name):
    """在 ASMRmusic / samples 里找对应音频。

    ⚠️ ASMRmusic 的音频在子目录里（`RJ1289104/01 mp3/xxx.mp3`），不是直接放 RJ 下，
    所以要递归建索引。json 名是 `xxx.mp3.json`，音频是 `xxx.mp3`，按去扩展名的 stem 匹配。
    """
    stem = name[:-5] if name.endswith(".json") else name
    key = os.path.splitext(stem)[0]
    for base in (os.path.join(os.path.dirname(ROOT), "ASMRmusic", rj),
                 os.path.join(ROOT, "samples", rj)):
        if not os.path.isdir(base):
            continue
        if base not in _IDX:
            idx = {}
            for root, _dirs, files in os.walk(base):
                for f in files:
                    if f.lower().endswith(AUDIO_EXT):
                        idx.setdefault(os.path.splitext(f)[0], os.path.join(root, f))
            _IDX[base] = idx
        p = _IDX[base].get(key)
        if p:
            return p
    return None


def vad_speech_ratio(wav, sr=16000):
    """用 Silero VAD 判断一段音频里「语音」占多少比例。

    ⚠️ 为什么不能只用能量判据：2026-11-08 RJ1289104 的 241 秒「可疑空洞」
    （能量 vs 中位 +2.5dB），切出来单独识别只出 1 段且是幻觉
    `次の動画でお会いしましょう` —— 实际是纯音效（深喉段）。
    **能量高 ≠ 有语音。** VAD 才是判「有没有人说话」的正确工具。

    注意：项目**不用 VAD 做整轨识别**（已实测否决），这里只当诊断判据。
    """
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except Exception:
        return None
    try:
        ts = get_speech_timestamps(wav, VadOptions(), sampling_rate=sr)
    except Exception:
        return None
    if not ts:
        return 0.0
    speech = sum(t["end"] - t["start"] for t in ts)
    return round(speech / max(len(wav), 1), 3)


def audit_run(run_dir, min_gap=20.0, detail=False, use_vad=False):
    try:
        m = json.load(io.open(os.path.join(run_dir, "manifest.json"), encoding="utf-8"))
    except Exception:
        return []
    rj = m.get("project")
    if not rj:
        return []
    out = []
    for jp in sorted(glob.glob(os.path.join(run_dir, "01_asr", rj, "asr", "*.json"))):
        try:
            j = json.load(io.open(jp, encoding="utf-8"))
        except Exception:
            continue
        segs = sorted((float(s["start"]), float(s["end"])) for s in (j.get("segments") or []))
        if len(segs) < 2:
            continue
        gaps = [(segs[i][1], segs[i + 1][0]) for i in range(len(segs) - 1)
                if segs[i + 1][0] - segs[i][1] >= min_gap]
        if not gaps:
            continue
        ap = find_audio(run_dir, rj, os.path.basename(jp))
        if not ap:
            out.append({"rj": rj, "track": os.path.basename(jp)[:34], "audio": None,
                        "gaps": [{"start": round(a, 1), "end": round(b, 1),
                                  "len": round(b - a, 1)} for a, b in gaps]})
            continue
        wav, sr = track_rms(ap)
        if wav is None:
            continue
        # 整轨参考电平：按 1 秒窗算 RMS 的中位数（含语音段）
        n = len(wav) // sr
        if n < 2:
            continue
        frame = wav[: n * sr].reshape(n, sr)
        med = db(np.sqrt((frame ** 2).mean(axis=1)).mean())
        rows = []
        for a, b in gaps:
            i0, i1 = int(a * sr), min(int(b * sr), len(wav))
            if i1 - i0 < sr // 2:
                continue
            seg = wav[i0:i1]
            # 用 0.5 秒窗算能量分布：取 90 分位（避开瞬时静音）
            k = sr // 2
            nn = len(seg) // k
            if nn < 1:
                continue
            r = np.sqrt((seg[: nn * k].reshape(nn, k) ** 2).mean(axis=1))
            p90 = db(np.percentile(r, 90))
            row = {"start": round(a, 1), "end": round(b, 1), "len": round(b - a, 1),
                   "p90_db": round(p90, 1), "vs_med": round(p90 - med, 1),
                   # 能量判据：空洞里的 90 分位能量接近整轨中位数 → 可能有声音
                   "loud": bool(p90 - med > -6.0)}
            if use_vad:
                row["speech_ratio"] = vad_speech_ratio(seg, sr)
                # VAD 判据：>30% 时长被判为语音 → 可疑漏听
                row["suspect"] = bool((row["speech_ratio"] or 0) > 0.3)
            else:
                row["suspect"] = row["loud"]
            rows.append(row)
        if rows:
            out.append({"rj": rj, "track": os.path.basename(jp)[:34],
                        "audio": os.path.basename(ap)[:30], "med_db": round(med, 1),
                        "gaps": rows})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rj", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--min-gap", type=float, default=20.0)
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--vad", action="store_true",
                    help="用 Silero VAD 精判（能量判据会误报纯音效段）")
    a = ap.parse_args()

    dirs = []
    for d in sorted(glob.glob(os.path.join(RUNS, "*_production"))):
        try:
            m = json.load(io.open(os.path.join(d, "manifest.json"), encoding="utf-8"))
        except Exception:
            continue
        if a.rj and m.get("project") != a.rj:
            continue
        dirs.append(d)
    if not a.all and not a.rj:
        raise SystemExit("要 --rj 或 --all")

    all_rows = []
    for d in dirs:
        all_rows += audit_run(d, a.min_gap, a.detail, a.vad)

    n_sus = sum(1 for r in all_rows for g in r["gaps"] if g.get("suspect"))
    n_all = sum(len(r["gaps"]) for r in all_rows)
    print("=== ASR 空洞审计（≥%.0f 秒）===" % a.min_gap)
    print("%-11s %-34s %7s %8s %8s %6s %s"
          % ("RJ", "轨", "空洞s", "p90dB", "vs中位", "语音%", "判定"))
    for r in all_rows:
        for g in r["gaps"]:
            if not a.detail and not g.get("suspect"):
                continue
            sr_ = g.get("speech_ratio")
            print("%-11s %-34s %7.1f %8s %8s %6s %s"
                  % (r["rj"], r["track"], g["len"], g.get("p90_db", "—"),
                     g.get("vs_med", "—"), ("%.0f" % (100 * sr_)) if sr_ is not None else "—",
                     "⚠️ 可疑漏听" if g.get("suspect") else "静音/拟声"))
    print("\n合计 %d 处空洞，其中 **可疑漏听 %d 处**（空洞内能量接近整轨中位）" % (n_all, n_sus))


if __name__ == "__main__":
    main()
