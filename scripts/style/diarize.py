# -*- coding: utf-8 -*-
r"""
说话人分离（diarization）：给 ASR 的每一段标上「这是谁的声音」。

用 speechbrain 的 ECAPA-TDNN 声纹模型提取每段语音的 embedding，
再按余弦距离做层次聚类，得到 SPEAKER_00 / 01 / 02 …

**重要局限**：聚的是**音色**，不是角色。
同一声优分饰多个角色时会被聚成同一类（音色相同），
需要再跑 identify.py 结合称呼/自称/口吻把角色细分出来。

用法：
  python diarize.py <ASR目录> <音频目录> [--device cpu|cuda] [--thr 0.30] [--min 0.6]

产出：直接改写 ASR 目录里的 json，每段加 "speaker": "SPEAKER_00"，
     并在 json 顶层写 "speakers": {"SPEAKER_00": {"segments": 123, "seconds": 456.7}, ...}
"""
import os
import sys
import json
import glob
import time
import argparse

import numpy as np
import re

MODEL_ID = "speechbrain/spkrec-ecapa-voxceleb"
SAVEDIR = os.environ.get("SPK_MODEL_DIR", r"$KIKOERU_ROOT\.workbuddy\models\spkrec-ecapa")
# 声纹模型从 HF 拉，国内直连常失败；提前设好镜像
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

MIN_SEC = 1.2      # 短于这个时长的段不提声纹：喘息/呻吟的 embedding 全是噪声
MIN_TEXT = 3       # 文本短于这个长度的也跳过（「あ」「ん」这类纯喘息）
MAX_SEC = 20.0     # 长段只取中间这部分，避免混入太多喘息/静音
THR = 0.50         # 层次聚类的余弦距离阈值，越小类越多。0.30 会碎成 30+ 类
MIN_SHARE = 0.05   # 说话时长占比低于此值的小类并入最相似的大类
MAX_SPK = 4        # 最多保留几个说话人（音声作品一般 1-3 人）


def load_audio(path, sr=16000):
    """解码成 16k 单声道 float32。（没用 av 的 resampler：av 19 的 API 变了，
    而且声纹模型对采样率不敏感，直接用容器原始采样率即可，切片时按实际 sr 算。）"""
    import av
    with av.open(path) as c:
        stream = c.streams.audio[0]
        stream.thread_type = "AUTO"
        real_sr = stream.rate or sr
        out = []
        for frame in c.decode(stream):
            # av 19 的 to_ndarray() 不再收 format 参数，返回形状是
            # (channels, samples)；整数格式要自己归一到 [-1,1]。
            a = np.asarray(frame.to_ndarray())
            if a.ndim == 2:
                ch_axis = 0 if a.shape[0] <= 8 else 1
                a = a.mean(axis=ch_axis)
            if a.dtype.kind in "iu":          # 整数 PCM：归一到 [-1,1]
                a = a.astype(np.float32) / max(1.0, float(np.iinfo(a.dtype).max))
            elif a.dtype != np.float32:       # float64 等浮点：已归一化，只降精度
                a = a.astype(np.float32)
            out.append(a)
    if not out:
        return np.zeros(0, dtype=np.float32), sr
    return np.concatenate(out), real_sr


def cut(y, sr, a, b):
    a = max(0.0, a)
    b = min(len(y) / float(sr), b)
    if b - a <= 0:
        return None
    # 段太长只取中间，两端常带喘息和静音
    if b - a > MAX_SEC:
        mid = (a + b) / 2
        a, b = mid - MAX_SEC / 2, mid + MAX_SEC / 2
    return y[int(a * sr): int(b * sr)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("asr_dir")
    ap.add_argument("audio_dir")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--thr", type=float, default=THR)
    ap.add_argument("--min", dest="min_sec", type=float, default=MIN_SEC)
    ap.add_argument("--force", action="store_true", help="已做过聚类的轨也重算")
    ap.add_argument("--corpus", action="store_true",
                    help="语料库模式：asr_dir 下每个子目录是一个作品的 asr/，"
                         "audio_dir 下同名的子目录放音频。一次加载模型跑完全部")
    ap.add_argument("key", nargs="?", default="")
    a = ap.parse_args()

    jobs = []
    files = []
    if a.corpus:
        for w in sorted(os.listdir(a.asr_dir)):
            ad = os.path.join(a.asr_dir, w, "asr")
            aud = os.path.join(a.audio_dir, w)
            if os.path.isdir(ad) and os.path.isdir(aud):
                jobs.append((w, ad, aud))
        if a.key:
            jobs = [j for j in jobs if a.key in j[0]]
        print("语料库模式：%d 个作品" % len(jobs), flush=True)
        if not jobs:
            return
    else:
        files = sorted(glob.glob(os.path.join(glob.escape(a.asr_dir), "*.json")))
        if a.key:
            files = [f for f in files if a.key in os.path.basename(f)]
        if not files:
            print("没有 ASR json：%s" % a.asr_dir)
            return

    from speechbrain.inference.speaker import EncoderClassifier
    # speechbrain 自己的下载通道不走 HF_ENDPOINT 镜像，国内会下到 0 字节文件然后报
    # "hparams is None"。所以模型文件是手动 curl 下来的，这里直接吃本地目录。
    src = SAVEDIR if os.path.exists(os.path.join(SAVEDIR, "hyperparams.yaml")) else MODEL_ID
    print("加载声纹模型 %s ..." % src, flush=True)
    t0 = time.time()
    clf = EncoderClassifier.from_hparams(source=src, run_opts={"device": a.device})
    print("模型就绪 %.0f 秒\n" % (time.time() - t0), flush=True)

    import torch

    def run_one(jf, audio_dir):
        d = json.load(open(jf, encoding="utf-8"))
        segs = d.get("segments", [])
        if not segs:
            return
        # 已做过聚类就跳过：语料库是分批跑的（ASR 一边跑一边补），
        # 重跑一遍不该把已完成的 100 多轨全部重算一遍。要重算用 --force。
        if (not a.force) and d.get("speakers") and all("speaker" in s for s in segs):
            return
        src = d["source"]
        apath = None
        for ext in ("", ".mp3", ".wav", ".m4a", ".flac"):
            p = os.path.join(audio_dir, src)
            if os.path.exists(p):
                apath = p
                break
        if apath is None:
            cand = glob.glob(os.path.join(glob.escape(audio_dir), "**", src), recursive=True)
            apath = cand[0] if cand else None
        if apath is None:
            print("  找不到音频，跳过 %s" % src[:40], flush=True)
            return

        y, sr = load_audio(apath)
        if len(y) == 0:
            print("  解码失败，跳过 %s" % src[:40], flush=True)
            return

        # 重跑时必须先清掉旧标签：否则上次留下的 speaker 会被"沿用相邻段"
        # 的逻辑当成已有结果保留，新旧混在一起（实测 2 类被污染成 14 类）。
        for s in segs:
            s.pop("speaker", None)

        idx, emb = [], []
        for i, s in enumerate(segs):
            if s["end"] - s["start"] < a.min_sec:
                continue
            # 纯喘息（「あ」「ん」「はぁ」）没有声纹价值，只会污染聚类
            if len(re.sub(r"[\s、。，！？…♪〜~ー]", "", s.get("text", ""))) < MIN_TEXT:
                continue
            w = cut(y, sr, s["start"], s["end"])
            if w is None or len(w) < sr * 0.3:
                continue
            idx.append(i)
            with torch.no_grad():
                e = clf.encode_batch(torch.tensor(w).unsqueeze(0))
            emb.append(e.squeeze().cpu().numpy())

        print("%s : %d 段，提了 %d 段声纹" % (src[:44], len(segs), len(emb)), flush=True)
        if len(emb) < 2:
            for i in idx:
                segs[i]["speaker"] = "SPEAKER_00"
            d["segments"] = segs
            d["speakers"] = {"SPEAKER_00": {"segments": len(idx),
                                            "seconds": round(sum(
                                                segs[i]["end"] - segs[i]["start"]
                                                for i in idx), 1)}}
            json.dump(d, open(jf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            return

        E = np.vstack(emb)
        E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
        # 余弦距离 -> 层次聚类
        from scipy.cluster.hierarchy import linkage, fcluster
        from scipy.spatial.distance import pdist
        try:
            D = pdist(E, metric="cosine")
            Z = linkage(D, method="average")
            lab = fcluster(Z, t=a.thr, criterion="distance")
        except Exception as e:
            print("    聚类失败 %s，全部归为一类" % str(e)[:80], flush=True)
            lab = np.ones(len(E), dtype=int)

        # 合并碎类：占比太小或类数太多时，把最小的类并进余弦最相近的大类。
        # 不做这步的话，单女主作品也会被碎成 30+ 个 SPEAKER（实测）。
        def merge_small(E, lab, dur_of):
            lab = list(lab)
            while True:
                cls = sorted(set(lab), key=lambda c: -dur_of.get(c, 0))
                total = sum(dur_of.values()) or 1
                small = [c for c in cls[1:]
                         if dur_of.get(c, 0) / total < MIN_SHARE or len(cls) > MAX_SPK]
                if not small:
                    break
                c = small[-1]
                cent = {k: E[[i for i in range(len(lab)) if lab[i] == k]].mean(axis=0)
                        for k in set(lab) if k != c}
                if not cent:
                    break
                cx = E[[i for i in range(len(lab)) if lab[i] == c]].mean(axis=0)
                best = max(cent, key=lambda k: float(
                    np.dot(cx, cent[k]) / (np.linalg.norm(cx) * np.linalg.norm(cent[k]) + 1e-9)))
                d = dur_of.pop(c, 0)
                dur_of[best] = dur_of.get(best, 0) + d
                lab = [best if x == c else x for x in lab]
            return lab

        dur = {}
        for k, i in enumerate(idx):
            dur[lab[k]] = dur.get(lab[k], 0) + (segs[i]["end"] - segs[i]["start"])
        n_before = len(set(lab))
        lab = merge_small(E, lab, dur)
        print("    声纹聚类 %d 类 -> 合并碎类后 %d 类" % (n_before, len(set(lab))), flush=True)

        # 按说话总时长排序，让 SPEAKER_00 = 戏份最多的人（通常是女主）
        dur = {}
        for k, i in enumerate(idx):
            dur[lab[k]] = dur.get(lab[k], 0) + (segs[i]["end"] - segs[i]["start"])
        order = sorted(dur, key=lambda c: -dur[c])
        remap = {c: "SPEAKER_%02d" % n for n, c in enumerate(order)}
        for k, i in enumerate(idx):
            segs[i]["speaker"] = remap[lab[k]]
        # 没提声纹的短段：沿用相邻段（多半是同一人在喘息）
        for i, s in enumerate(segs):
            if "speaker" in s:
                continue
            for j in range(i - 1, -1, -1):
                if "speaker" in segs[j]:
                    s["speaker"] = segs[j]["speaker"]
                    break
            else:
                for j in range(i + 1, len(segs)):
                    if "speaker" in segs[j]:
                        s["speaker"] = segs[j]["speaker"]
                        break
                else:
                    s["speaker"] = "SPEAKER_00"

        stat = {}
        for s in segs:
            k = s.get("speaker", "SPEAKER_00")
            stat.setdefault(k, {"segments": 0, "seconds": 0.0})
            stat[k]["segments"] += 1
            stat[k]["seconds"] += s["end"] - s["start"]
        for k in stat:
            stat[k]["seconds"] = round(stat[k]["seconds"], 1)
        d["segments"] = segs
        d["speakers"] = stat
        json.dump(d, open(jf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("    %s" % ", ".join("%s %d段/%.0f秒" % (k, v["segments"], v["seconds"])
                                   for k, v in sorted(stat.items())), flush=True)

    if a.corpus:
        for w, ad, aud in jobs:
            print("\n===== %s =====" % w[:52], flush=True)
            for jf in sorted(glob.glob(os.path.join(glob.escape(ad), "*.json"))):
                run_one(jf, aud)
    else:
        for jf in files:
            run_one(jf, a.audio_dir)
    print("\n完成", flush=True)


if __name__ == "__main__":
    main()
