# -*- coding: utf-8 -*-
r"""开头补扫：修 Whisper「首个 30 秒窗被整窗丢弃」导致的开头丢词。

背景（2026-10-13 定位，实测脚本在 .workbuddy/tmp/daily/test_*.py）：
  asr.py 走整轨 model.transcribe()，Whisper 内部按 **30 秒滑窗**切分。
  当第 1 个窗（0-30s）的开头是低能量的慢起句时，模型会把这半窗判成
  低置信度并**整段跳过或编造套话**——音频完好、音量也够（增益实测无效），
  纯粹是滑窗相位的运气问题。

  实测 RJ9000002 トラック2：整轨首段从 27.02s 起，改用 15s 短窗重扫后
  15.68s 的台词全部找回。
  实测 RJ9000002 トラック6：整轨首段 53.98s（丢了 54 秒整段开场戏），
  重扫把 2.36s 起的 6 句全部找回。
  本批 5 部作品 68 轨里 34 轨（50%）开头被吃掉 >=3s。

做法（已验证）：
  只对「开头」做补扫，把 [0, HEAD] 按 **WIN=15s / OVL=5s** 切窗逐窗识别。
  短窗让内容不再独占一个 30s 窗，从而绕开「整窗丢弃」。
  · **core 抽取**：第 k 窗只保留 start 落在它核心区 [k*step, k*step+step) 的段，
    每个时间点只归属一个窗，天然不重复。
  · **两相**：A 相从 0s 起，B 相从 +5s 起；A 为主，B 只补 A 没覆盖的时间段
    （对抗个别窗失败/幻觉）。
  · **边界重复清理**：相邻段若后段开头重复了前段（含近似），合并去重。

用法：
  python rescan_head.py <ASR目录> <音频目录> [扩展名] [--head 60] [--dry]

  python rescan_head.py "$KIKOERU_ROOT/.workbuddy/draft/RJ9000002/asr" \
      "$KIKOERU_ROOT/RJ9000002/mp3" mp3

  --dry 只报告需要补扫的轨，不写盘。
  逐窗原始结果缓存在 .workbuddy/tmp/rescan_head/out，可断点续跑。
"""
import os
import sys
import re
import json
import glob
import time
import difflib
import argparse

HEAD_SEC = 60.0    # 补扫覆盖的开头秒数（按原首段自动延长）
WIN_SEC = 15.0     # 短窗时长（关键：< 30s 才能避开整窗丢弃）
OVL_SEC = 5.0      # 窗间重叠
PHASE_OFF = 5.0    # 第二相偏移
MIN_GAP = 2.0      # 原首段晚于「有声起点」这么多秒才算丢开头
PUNCT = "。、，,.!！?？…♪~〜ー♡ \u3000「」『』（）()"
EOS_CHARS = ("。", "！", "？", "!", "?", "…", "♪")   # 句末标点（截断时优先切在这些字符之后）


RS = 16000   # 统一按 Whisper 的输入采样率处理


def load_audio(path):
    """用 faster_whisper 的 decode_audio 解 PCM。

    ⚠️ **别**自己用 av.to_ndarray() 解码后当浮点用：FLAC/24bit 音轨返回的是
    **原生采样尺度**（实测峰值 595,782,144），不是 [-1,1]；直接拿它写 WAV
    会让样本全被 clip 成 ±1 → 送进去的是方波 → 一个字符都识别不出。
    2026-10-13 就因为这个 bug，RJ9000003 六首 FLAC 一首都没补回来。
    decode_audio 内部做了重采样 + 归一化，与 asr.py 整轨识别吃到的数据一致。
    """
    import numpy as np
    from faster_whisper.audio import decode_audio
    x = np.asarray(decode_audio(path, sampling_rate=RS), dtype=np.float32)
    return x, RS


def write_wav(samples, sr, path):
    import av
    import numpy as np
    o = av.open(path, "w")
    s = o.add_stream("pcm_s16le", rate=sr)
    s.layout = "mono"
    pcm = np.clip(samples, -1, 1)
    f = av.AudioFrame.from_ndarray((pcm * 32767).astype(np.int16).reshape(1, -1),
                                   format="s16", layout="mono")
    f.sample_rate = sr
    for pk in s.encode(f):
        o.mux(pk)
    for pk in s.encode():
        o.mux(pk)
    o.close()


def voice_onset(X, sr, th=0.004, win=0.25):
    """返回首个「有声」时间点（秒），找不到返回 None。"""
    import numpy as np
    n = int(win * sr)
    for i in range(len(X) // n):
        seg = X[i * n:(i + 1) * n]
        r = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2)))
        if r > th:
            return i * win
    return None


def find_audio(name, audio_dir):
    """先按 audio_dir/name 找，找不到就递归搜（各作品音频目录不统一：
    本編(mp3) / 02 FLAC / 01 mp3 / mp3 都有）。"""
    p = os.path.join(audio_dir, name)
    if os.path.exists(p):
        return p
    for dp, dn, fn in os.walk(audio_dir):
        if name in fn:
            return os.path.join(dp, name)
    return None


def _norm(s):
    return "".join(c for c in (s or "") if c not in PUNCT)


def _strip_prefix_dup(prev_text, cur_text):
    """若 cur 开头重复了 prev 的结尾，返回去掉重复后的 cur（空串表示整条重复）。"""
    a, b = _norm(prev_text), _norm(cur_text)
    if not a or not b:
        return cur_text
    best = 0
    for L in range(min(len(a), len(b)), 2, -1):
        if a[-L:] == b[:L]:
            best = L
            break
    if best < 3:
        return cur_text
    cnt, cut = 0, len(cur_text)
    for i, c in enumerate(cur_text):
        if c not in PUNCT:
            cnt += 1
            if cnt == best:
                cut = i + 1
                break
    return cur_text[cut:].lstrip(PUNCT)


def _is_prefix_dup(prev_text, cur_text, th=0.65):
    """cur 的开头是否「近似重复」了 prev（允许 ASR 措辞微差）。"""
    a, b = _norm(prev_text), _norm(cur_text)
    if len(a) < 3 or len(b) < 3:
        return False
    pre = b[:len(a)]
    if len(pre) < 3:
        return False
    return difflib.SequenceMatcher(None, a, pre).ratio() >= th


def _drop_contained(recs):
    """删除「时间与文本都被更早的长段包含」的冗余段。

    core 抽取漏裁 end 的残留：粗段（如 [15.0-22.5]）会包含一个更细的微段
    （如 0.2s 的 `それ、`）。`_dedup` 只比相邻，抓不到隔位的这种包含。
    """
    keep = []
    for i, z in enumerate(recs):
        drop = False
        for p in recs[:i]:
            if (p["start"] <= z["start"] + 0.15 and p["end"] >= z["end"] - 0.15
                    and (p["end"] - p["start"]) >= (z["end"] - z["start"]) * 1.5):
                nz, np = _norm(z["text"]), _norm(p["text"])
                if len(nz) >= 2 and nz in np and len(np) > len(nz):
                    drop = True
                    break
        if not drop:
            keep.append(z)
    return keep


def _dedup(recs):
    """按时间排序后清理重复：时间重叠 + 文本近似 -> 合并/去重。"""
    recs = sorted(recs, key=lambda z: z["start"])
    out = []
    for z in recs:
        if out:
            p = out[-1]
            if z["start"] < p["end"] - 0.2:      # 时间重叠
                if _is_prefix_dup(p["text"], z["text"]) and len(_norm(z["text"])) >= len(_norm(p["text"])):
                    z = dict(z)
                    z["start"] = min(z["start"], p["start"])
                    z["end"] = max(z["end"], p["end"])
                    out[-1] = z
                    continue
                if _is_prefix_dup(z["text"], p["text"]):
                    p = dict(p)
                    p["start"] = min(p["start"], z["start"])
                    p["end"] = max(p["end"], z["end"])
                    out[-1] = p
                    continue
            t = _strip_prefix_dup(p["text"], z["text"])
            if not _norm(t):
                continue
            if t != z["text"]:
                z = dict(z)
                z["text"] = t
                z["words"] = []
        out.append(z)
    return _drop_contained(out)


def _run_phase(model, X, sr, src_dir, tag, off, win, ovl, head, is_hallu):
    """跑一相滑窗，返回 core 抽取后的段（绝对时间）。"""
    step = max(1.0, win - ovl)
    pos, recs = off, []
    while pos < head - 1.0:
        b = min(pos + win, head)
        if b - pos < 2.0:
            break
        seg = X[int(pos * sr):int(b * sr)]
        wavp = os.path.join(src_dir, tag + ".wav")
        write_wav(seg, sr, wavp)
        try:
            got, _ = model.transcribe(
                wavp, language="ja", beam_size=5, vad_filter=False,
                condition_on_previous_text=False, word_timestamps=True,
                initial_prompt="以下是一段日语对话的转写。",
            )
            core_end = min(pos + step, head)
            for s in got:
                if getattr(s, "no_speech_prob", 0) >= 0.85:
                    continue
                a = pos + float(s.start)
                if a >= core_end - 0.05:
                    continue          # 交给下一窗核心
                e = pos + float(s.end)
                txt = (s.text or "").strip()
                if e > core_end + 0.05:
                    # 段尾越界 core：Whisper 常把整窗内容吐成一整段（如 [15.0-29.4]），
                    # 若原样保留，它会吞并后续窗的细粒度段 → 时间重叠 + 文本重复。
                    # 用词级时间戳在 core_end 处截断，越界部分交给下一窗重识别。
                    # ⚠️ Whisper 的词级时间戳偏粗（会把 14s 的整段词均摊），所以优先切在
                    # **句末标点**之后，避免把「あ、も」这种半截词留在行尾。
                    ws = getattr(s, "words", None) or []
                    keep = [w for w in ws if pos + float(w.start) < core_end - 0.05]
                    if keep:
                        cut = len(keep)
                        for idx in range(len(keep) - 1, -1, -1):
                            if (keep[idx].word or "").strip().endswith(EOS_CHARS):
                                cut = idx + 1
                                break
                        keep = keep[:cut]
                    if keep:
                        txt = "".join(w.word for w in keep).strip()
                        e = min(pos + float(keep[-1].end), core_end)
                    else:
                        e = core_end      # 无词级信息，只能硬裁时间
                if not txt or is_hallu(txt):
                    continue
                recs.append({"start": round(a, 3), "end": round(e, 3),
                             "text": txt, "words": []})
        except Exception as e:
            print("    [窗失败] %.0fs: %s" % (pos, str(e)[:100]), flush=True)
        pos += step
    return recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("asr_dir")
    ap.add_argument("audio_dir")
    ap.add_argument("ext", nargs="?", default="mp3")
    ap.add_argument("--head", type=float, default=HEAD_SEC)
    ap.add_argument("--win", type=float, default=WIN_SEC)
    ap.add_argument("--overlap", type=float, default=OVL_SEC)
    ap.add_argument("--dry", action="store_true", help="只报告，不写盘")
    a = ap.parse_args()

    from faster_whisper import WhisperModel
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from asr import is_hallucination, is_freetalk

    MODEL = os.environ.get(
        "FW_MODEL", r"D:\fasterwhisperGUI-0.8.5.sfx\models\faster-whisper-large-v3")
    HERE = os.path.dirname(os.path.abspath(__file__))
    TMP = os.path.abspath(os.path.join(HERE, "..", "..", "..", "tmp", "rescan_head"))
    src_dir = os.path.join(TMP, "src")
    out_dir = os.path.join(TMP, "out")
    os.makedirs(src_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    jfiles = sorted(glob.glob(os.path.join(a.asr_dir, "*.json")))
    if not jfiles:
        print("没有找到 ASR JSON：%s" % a.asr_dir)
        sys.exit(1)

    todo = []
    for jf in jfiles:
        d = json.load(open(jf, encoding="utf-8"))
        if d.get("head_rescanned"):
            print("  [跳过] 已补扫过 %s" % os.path.basename(jf)[:60])
            continue
        segs = d.get("segments", [])
        if not segs:
            continue
        first = segs[0]["start"]
        if first < MIN_GAP:
            continue
        name = d.get("source", "")
        apath = find_audio(name, a.audio_dir)
        if not apath:
            print("  [跳过] 找不到音频 %s" % name)
            continue
        X, sr = load_audio(apath)
        onset = voice_onset(X, sr)
        if onset is None or first <= onset + MIN_GAP:
            continue
        todo.append((jf, d, onset, first))

    print("=== 需补扫 %d / %d 轨 ===" % (len(todo), len(jfiles)))
    for jf, d, onset, first in todo:
        print("  %-58s 首段 %.1fs / 有声起点 %.2fs" %
              (os.path.basename(jf)[:58], first, onset))
    if not todo or a.dry:
        print("（--dry 或无需补扫，结束）")
        return

    print("\n加载模型 large-v3 (cuda / float16) ...", flush=True)
    model = WhisperModel(MODEL, device="cuda", compute_type="float16")
    print("就绪\n", flush=True)

    for jf, d, onset, first_old in todo:
        name = d.get("source", "")
        apath = find_audio(name, a.audio_dir)
        # 缓存按「作品__文件名」命名，避免不同作品同名轨道串味
        rj = os.path.basename(os.path.dirname(os.path.abspath(a.asr_dir)))
        cache = os.path.join(out_dir, rj + "__" + name + ".json")
        ft = is_freetalk(name)
        hallu = lambda t: is_hallucination(t, ft)

        if os.path.exists(cache):
            recs = json.load(open(cache, encoding="utf-8"))["segments"]
        else:
            X, sr = load_audio(apath)
            head = min(len(X) / sr, max(a.head, first_old + 10.0))
            A = _run_phase(model, X, sr, src_dir, "A", 0.0, a.win, a.overlap, head, hallu)
            B = _run_phase(model, X, sr, src_dir, "B", PHASE_OFF, a.win, a.overlap, head, hallu)
            recs = _dedup(A + B)
            json.dump({"source": name, "head_window": a.win, "head_sec": head,
                       "segments": recs},
                      open(cache, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

        # ---- 合并：只取「完全落在原首段之前」的补扫段，接到原段前面 ----
        old = d.get("segments", [])
        add = [z for z in recs if z["end"] <= first_old + 0.3]
        add = _dedup(add)
        if add and old:
            t = _strip_prefix_dup(add[-1]["text"], old[0]["text"])
            if t and _norm(t) != _norm(old[0]["text"]):
                old = [dict(old[0], text=t, words=[])] + old[1:]
        merged = add + old          # 原段本身来自整轨单次识别，无重复，不再整体去重
        d["segments"] = merged
        d["head_rescanned"] = True
        d["head_rescan_added"] = len(add)
        json.dump(d, open(jf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("  %-55s 补入 %d 段，首段 %.2fs -> %.2fs"
              % (name[:55], len(add), first_old,
                 merged[0]["start"] if merged else -1), flush=True)

    print("\n补扫完成。")


if __name__ == "__main__":
    main()
