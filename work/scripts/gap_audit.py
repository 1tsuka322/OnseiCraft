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


def halluc_candidates(run_dir, top=0):
    """纯假名幻觉候选（D6，2026-11-08，依据 GPT G4）。

    背景：01 只抓 `[A-Za-z]{3,}` 的拉丁幻觉；**纯假名幻觉**（`ああああ`、`んーーー`）没有判据，
    因为 `avg_logprob` / `no_speech_prob` **从来没存进 json**，事后无法判断。
    GPT 的判断是：不值得为此重跑 01；用**可观测特征联合排序**出候选、只供试听，不自动删。

    四个特征（都在已有 json 里，零额外成本）：
      ① 纯假名     —— 去掉标点后只剩假名/长音（无汉字、无数字、无拉丁）
      ② 重复文本   —— 同一文本在本轨出现 ≥2 次（幻觉的典型形态：模型卡住复读）
      ③ 密度异常   —— 字/秒 极高（≥8）或时长极短（≤0.4s）
      ④ 时长异常   —— >12 秒却只有几个字（念不完或空转）

    ⚠️ **只输出候选，绝不自动删**。低声耳语/拖音/气声可能既低能量又真实；
       噪声音效也可能高能量。所以这里不做音频侧判断，把判断留给试听。
    """
    rj = os.path.basename(run_dir).split("_")[1] if "_" in os.path.basename(run_dir) else "?"
    d = os.path.join(run_dir, "01_asr", rj, "asr")
    if not os.path.isdir(d):
        return [], []
    KANA = re.compile(r"^[\u3040-\u309f\u30a0-\u30ff\u30fc\u30fb\u3001\u3002…‥・\s!?！？~〜ー—\-]+$")
    halluc, frag = [], []
    for p in sorted(glob.glob(os.path.join(d, "*.json"))):
        try:
            j = json.load(io.open(p, encoding="utf-8"))
        except Exception:
            continue
        segs = j.get("segments") or []
        norm = [re.sub(r"[\s、。…‥・!?！？~〜ー—\-]", "", (s.get("text") or "")) for s in segs]
        track = os.path.basename(p)[:-5]

        # ① 先切出「连续同文段」——这是 01 的分段碎片化，**不是幻觉**（D7 已量化 217 处）
        i = 0
        runs = {}
        while i < len(segs):
            k = i
            while k + 1 < len(segs) and norm[k + 1] == norm[i] and norm[i]:
                k += 1
            n = k - i + 1
            if n >= 3 and norm[i]:
                durs = [float(segs[x].get("end") or 0) - float(segs[x].get("start") or 0)
                        for x in range(i, k + 1)]
                if max(durs) < 0.5:
                    frag.append(dict(rj=rj, track=track, start=round(float(segs[i].get("start") or 0), 2),
                                     end=round(float(segs[k].get("end") or 0), 2), n=n,
                                     text=segs[i].get("text") or ""))
            for x in range(i, k + 1):
                runs[x] = n
            i = k + 1

        cnt = {}
        for t in norm:
            if t:
                cnt[t] = cnt.get(t, 0) + 1
        for idx, (s, t) in enumerate(zip(segs, norm)):
            txt = (s.get("text") or "").strip()
            if not txt or not t or not KANA.match(txt):
                continue
            dur = float(s.get("end") or 0) - float(s.get("start") or 0)
            if dur < 0.3:
                continue                       # 亚秒段是分段碎片，已在 frag 里统计
            dens = len(t) / dur if dur > 0 else 99
            dup = cnt.get(t, 1)
            score, why = 0, []
            if dup >= 2:
                score += min(dup, 5) * 2
                why.append("同文×%d" % dup)
            if dens >= 8:
                score += 3
                why.append("密度%.1f字/秒" % dens)
            if dur > 12 and len(t) <= 8:
                score += 2
                why.append("长%.1fs仅%d字" % (dur, len(t)))
            if score <= 0:
                continue
            halluc.append(dict(rj=rj, track=track, start=round(float(s.get("start") or 0), 2),
                               dur=round(dur, 2), dens=round(dens, 1), dup=dup,
                               text=txt, score=score, why="；".join(why)))
    halluc.sort(key=lambda x: -x["score"])
    return (halluc[:top] if top else halluc), frag


def timing_anomalies(run_dir, top=0):
    """时间结构异常诊断（2026-11-10，GPT R2 Q6）。

    GPT 的原话：「可以尽早标异常，但**不能称为物理上念不完**，更不能按字/秒自动删除」。
    所以这里只输出**证据**，让人自己判断，一个字节都不改。

    五项证据（全部来自已有 json，不用重跑 01）：
      ① 段时长 vs 字数密度 —— 只做**粗排序**，不单独定罪
      ② 首末词与段边界的偏差 —— 词级对齐塌缩会让首词比段晚很多 / 末词比段早很多
      ③ 零时长词比例 —— 时间戳塌缩的直接证据
      ④ 与下一段的重叠 —— 分块接缝重复解码
      ⑤ 纯音效段（可选 --vad 才判）

    输出按「证据条数」降序，最多 `top` 条。
    """
    rj = os.path.basename(run_dir).split("_")[1] if "_" in os.path.basename(run_dir) else "?"
    d = os.path.join(run_dir, "01_asr", rj, "asr")
    if not os.path.isdir(d):
        return []
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*.json"))):
        try:
            j = json.load(io.open(p, encoding="utf-8"))
        except Exception:
            continue
        segs = j.get("segments") or []
        for i, sg in enumerate(segs):
            txt = (sg.get("text") or "").strip()
            if not txt:
                continue
            st = float(sg.get("start") or 0)
            en = float(sg.get("end") or 0)
            dur = en - st
            n = len(re.sub(r"[\s、。…‥・!?！？~〜ー—\-]", "", txt))
            ws = sg.get("words") or []
            ev = []
            # ① 密度（粗排序用，不单独定罪）
            dens = n / dur if dur > 0 else 999
            if dur > 0 and dens >= 8:
                ev.append("密度%.1f字/秒" % dens)
            # ② 首末词偏差
            if ws:
                fst = float(ws[0].get("start") or st)
                lst = float(ws[-1].get("end") or en)
                if fst - st > 1.0:
                    ev.append("首词晚%.1fs" % (fst - st))
                if en - lst > 1.0:
                    ev.append("末词早%.1fs" % (en - lst))
                # ③ 零时长词
                z = sum(1 for w in ws if float(w.get("end") or 0) <= float(w.get("start") or 0))
                if ws and z / len(ws) > 0.3:
                    ev.append("零时长词%d/%d" % (z, len(ws)))
            # ④ 与下一段重叠
            if i + 1 < len(segs):
                ov = en - float(segs[i + 1].get("start") or 0)
                if ov > 0.5:
                    ev.append("与下段重叠%.1fs" % ov)
            if len(ev) >= 2:          # 单条证据不算，要求 ≥2 项互相印证
                out.append(dict(rj=rj, track=os.path.basename(p)[:-5], t=round(st, 2),
                                dur=round(dur, 2), chars=n, dens=round(dens, 1),
                                ev=ev, text=txt))
    out.sort(key=lambda x: -len(x["ev"]))
    return out[:top] if top else out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rj", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--min-gap", type=float, default=20.0)
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--halluc", action="store_true",
                    help="纯假名幻觉候选（D6）：按「重复/密度/时长」排序，只审计不删")
    ap.add_argument("--timing", action="store_true",
                    help="时间结构异常诊断（GPT Q6）：首末词偏差/零时长词/段重叠/密度，只标不改")
    ap.add_argument("--top", type=int, default=0, help="--halluc / --timing 时只列前 N 条")
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

    if a.timing:
        rows = []
        for d in dirs:
            rows += timing_anomalies(d)
        rows.sort(key=lambda x: -len(x["ev"]))
        if a.top:
            rows = rows[:a.top]
        print("=== 时间结构异常（GPT Q6：只标异常，不代表内容有错）===")
        print("%-11s %-26s %7s %6s %6s %5s %s" % ("RJ", "轨", "起(s)", "时长", "字数", "密度", "证据"))
        for r in rows:
            print("%-11s %-26s %7.2f %6.2f %6d %5.1f %s"
                  % (r["rj"], r["track"][:26], r["t"], r["dur"], r["chars"], r["dens"],
                     "；".join(r["ev"])))
            print("      %r" % r["text"][:46])
        print("")
        print("合计 %d 条（要求 ≥2 项证据互相印证）。" % len(rows))
        print("⚠️ 这是**时间戳异常**，不等于内容有错——字幕短但音频里确有完整台词的，属于对齐问题不是幻觉。")
        return

    if a.halluc:
        rows, frag = [], []
        for d in dirs:
            h, f = halluc_candidates(d)
            rows += h
            frag += f
        rows.sort(key=lambda x: -x["score"])
        if a.top:
            rows = rows[:a.top]
        print("=== 纯假名幻觉候选（D6，只审计不删）===")
        print("%-11s %-30s %7s %6s %6s %5s %s"
              % ("RJ", "轨", "起(s)", "时长", "密度", "同文", "特征"))
        for r in rows:
            print("%-11s %-30s %7.2f %6.2f %6.1f %5d %s  %r"
                  % (r["rj"], r["track"][:30], r["start"], r["dur"], r["dens"],
                     r["dup"], r["why"], r["text"][:24]))
        print("\n合计 %d 条幻觉候选。⚠️ 这是**候选清单，不是错误清单**——必须试听后才能定。" % len(rows))
        if frag:
            print("\n--- 另：连续同文段（分段碎片化，**不是幻觉**，归 01/02 处理）%d 处 ---" % len(frag))
            for f in frag[:8]:
                print("  %-11s %-26s %7.2f-%.2f 连续%d段 %r"
                      % (f["rj"], f["track"][:26], f["start"], f["end"], f["n"], f["text"][:16]))
        return

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
