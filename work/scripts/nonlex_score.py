# -*- coding: utf-8 -*-
"""非词汇发声打分器（2026-10-21，C3 弱监督基线）。

**它是什么**：一个域内训练的「呻吟/喘息/气声」似然打分器——
whisper large-v3 encoder（1280 维均值池化）+ 2 层 MLP，用 A8 遮蔽器的规则标签做弱监督。
跨作品 AUC **0.915**（对比：通用 NonverbalVocalization 数据 0.543、log-mel 线性 0.675）。

**它不是什么（重要）**：**不能当自动遮蔽开关**。高阈值端仍有 ~1.7% 误报（breathy 台词
被判成气声），本项目铁律是「宁可漏判不可误判」→ 默认只产出**按分数排序的试听清单**，
由人决定。要真正自动化，需要人工标签（`data/tmp/label_pack/`）再训。

用法：
  python nonlex_score.py --rj RJ9000004              # 单作品 → 试听清单
  python nonlex_score.py --all                       # 全库
  python nonlex_score.py --rj RJ9000004 --thr 0.9    # 只列 ≥0.9 的
  python nonlex_score.py --rj RJ9000004 --only-unknown   # 只看 A8 表外（默认目标集）
  python nonlex_score.py --rj RJ9000004 --c3            # 用 C3 五头模型（moan/breath 排序，2026-10-21）

输出：
  data/tmp/nonlex_review/<rj>.md    按分数排序的试听清单（含 ja/时间/时长/A8 组）
  data/tmp/nonlex_review/<rj>.json  逐段分数（供程序消费）

依赖：GPU（whisper encoder）+ 本地 large-v3；模型权重 `data/models/nonlex_detector.json`。
"""
import os
import re
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json
import glob
import argparse

import numpy as np

ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(ROOT, "data", "skills", "kikoeru-subtitle", "scripts")
MODEL = os.path.join(ROOT, "data", "models", "nonlex_detector.json")
C3_DIR = os.path.join(ROOT, "data", "tmp", "c3_model")   # 五头模型（c3_train.py 产出）
C3_SCRIPTS = os.path.join(ROOT, "data", "tmp", "daily")
if C3_SCRIPTS not in sys.path:
    sys.path.insert(0, C3_SCRIPTS)
LARGE = r"D:\fasterwhisperGUI-0.8.5.sfx\models\faster-whisper-large-v3"
OUTDIR = os.path.join(ROOT, "data", "tmp", "nonlex_review")
SR = 16000


def load_model():
    import torch
    d = json.load(open(MODEL, encoding="utf-8"))
    st = d["state"]
    dim = len(d["mu"])
    net = torch.nn.Sequential(
        torch.nn.Linear(dim, 256), torch.nn.ReLU(), torch.nn.Dropout(0.3),
        torch.nn.Linear(256, 64), torch.nn.ReLU(),
        torch.nn.Linear(64, 1))
    sd = {}
    for k, v in st.items():
        sd[k] = torch.tensor(np.array(v, dtype=np.float32))
    net.load_state_dict(sd)
    net.eval()
    return d, net


def c3_score(rj, thr, only_unknown, top):
    """C3 五头（moan/breath/speech/overlay/other）给 ASR 段打分 → 试听清单。

    与旧模型（A8 规则弱监督）的区别：**人工 500 标签监督**，按作品分组验证；
    moan top20 精度 0.90（可当排序器），speech 0.982（可判「是不是台词」）。
    特征与训练时完全一致：whisper encoder 均值 + 声道 6 维 + 时长（1287 维）。
    """
    import joblib
    import torch
    import ctranslate2
    import c3_features as F3
    from faster_whisper import WhisperModel
    from faster_whisper.feature_extractor import FeatureExtractor

    heads = {}
    for h in ("moan", "breath", "speech", "overlay", "other"):
        p = os.path.join(C3_DIR, "head_%s.joblib" % h)
        if os.path.exists(p):
            heads[h] = joblib.load(p)
    if "moan" not in heads:
        sys.exit("缺 C3 模型：先跑 data/tmp/daily/c3_train.py")

    wm = WhisperModel(LARGE, device="cuda", compute_type="float16")
    fe = FeatureExtractor(feature_size=128)
    amap = F3.audio_map(rj)
    asr_dir = os.path.join(ROOT, "data", "draft", rj, "asr")

    def _decode_track(path):
        """整轨解码一次（16k stereo fltp）——2026-10-24 修：原实现每段重开文件解码，
        1423 段耗时 18 分钟（O(n²)）；整轨缓存后同量级 ~2 分钟。"""
        import av
        c = av.open(path)
        st = next(x for x in c.streams if x.type == "audio")
        res = av.AudioResampler(format="fltp", layout="stereo", rate=SR)
        L, R = [], []
        for frame in c.decode(st):
            for f in res.resample(frame):
                a = f.to_ndarray()
                L.append(a[0])
                R.append(a[1])
        c.close()
        if not L:
            return None, None
        return np.concatenate(L).astype(np.float32), np.concatenate(R).astype(np.float32)

    rows = []
    for jp in sorted(glob.glob(os.path.join(asr_dir, "*.json"))):
        stem = os.path.splitext(os.path.basename(jp))[0]
        ap = amap.get(stem) or amap.get(os.path.splitext(stem)[0])
        if not ap:
            continue
        segs = json.load(open(jp, encoding="utf-8")).get("segments") or []
        if not segs:
            continue
        Lc, Rc = _decode_track(ap)
        if Lc is None:
            continue
        for s in segs:
            dur = float(s["end"]) - float(s["start"])
            if dur < 0.5:
                continue
            i0 = max(0, int((float(s["start"]) - 0.2) * SR))
            i1 = int((float(s["start"]) + dur + 0.2) * SR)
            L, R = Lc[i0:i1], Rc[i0:i1]
            if len(L) < 8000:
                continue
            mono = ((L + R) / 2).astype(np.float32)
            mel = fe(mono)[None, :, :]
            sv = ctranslate2.StorageView.from_array(np.ascontiguousarray(mel))
            emb = np.asarray(wm.model.encode(sv, to_cpu=True))[0].mean(axis=0)
            x = np.concatenate([emb.astype(np.float32), F3.channel_feats(L, R),
                                np.array([dur], dtype=np.float32)])[None, :]
            sc = {}
            for h, m in heads.items():
                sc[h] = float(m["clf"].predict_proba(m["scaler"].transform(x))[0, 1])
            rows.append({"track": stem, "start": round(float(s["start"]), 2),
                         "ja": (s.get("text") or "").strip(), "dur": round(dur, 1), **sc})
        del Lc, Rc
    if not rows:
        print("没有可用段")
        return
    # 排序分：moan 优先，breath 次之，并用 speech 头做闸门（2026-10-24 修：
    # 原 max(moan, 0.9*breath) 会把 speech=1.00 的纯台词排进前 20，实测污染 15%+）
    for r in rows:
        base = max(r.get("moan", 0), r.get("breath", 0) * 0.9)
        r["score_raw"] = base
        r["score"] = base * (1.0 - r.get("speech", 0))
    rows.sort(key=lambda r: -r["score"])
    sel = [r for r in rows if r["score"] >= thr]
    out = os.path.join(OUTDIR, "%s_c3.md" % rj)
    os.makedirs(OUTDIR, exist_ok=True)
    L = ["# C3 试听清单（五头模型）· %s" % rj, "",
         "- 共 %d 段，score ≥ %.2f 的 **%d 条**（按 moan/breath 排序）" % (len(rows), thr, len(sel)),
         "- score = max(moan, 0.9×breath) × (1−speech)；speech 高 = 这是台词，已自动降权", ""]
    for i, r in enumerate(sel[:top], 1):
        L.append("%d. `[%s %d:%05.2f]` moan %.2f · breath %.2f · speech %.2f · %s"
                 % (i, r["track"][:18], int(r["start"] // 60), r["start"] % 60,
                    r.get("moan", 0), r.get("breath", 0), r.get("speech", 0), r["ja"][:44]))
    open(out, "w", encoding="utf-8").write("\n".join(L) + "\n")
    json.dump(rows, open(os.path.join(OUTDIR, "%s_c3.json" % rj), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("→ %s（%d 条）+ %s_c3.json（%d 段）" % (out, min(len(sel), top), rj, len(rows)))


def a8_group(ja, MN):
    _m, spans = MN.mask_ja(ja, literal=False)
    unknown = []
    MN.mask_ja(ja, literal=False, unknown=unknown)
    lex = len(re.findall(r"[\u3040-\u30ff\u4e00-\u9fff]", ja))
    if spans:
        return "A8表内遮蔽"
    if unknown:
        return "A8表外unknown"
    if lex >= 8:
        return "实义台词"
    return "短碎片"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rj", default="")
    ap.add_argument("--c3", action="store_true", help="用 C3 五头模型（人工 500 标签）")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--thr", type=float, default=0.0, help="只列分数 ≥ 此值的段")
    ap.add_argument("--only-unknown", action="store_true",
                    help="只看 A8 表外 unknown（C3 的目标集）")
    ap.add_argument("--max-tracks", type=int, default=0, help="每作品最多几轨（0=全部）")
    a = ap.parse_args()
    if not a.rj and not a.all:
        sys.exit("需要 --rj RJxxxxxxx 或 --all")
    if a.c3:
        for rj in ([a.rj] if a.rj else sorted(os.listdir(os.path.join(
                ROOT, "data", "draft")))):
            if not rj.startswith("RJ"):
                continue
            c3_score(rj, a.thr, a.only_unknown, a.top)
        return

    sys.path.insert(0, SCRIPTS)
    import mask_nonlexical as MN
    import torch
    import ctranslate2
    from faster_whisper import WhisperModel
    from faster_whisper.feature_extractor import FeatureExtractor
    from faster_whisper.audio import decode_audio

    md, net = load_model()
    mu = np.array(md["mu"], dtype=np.float32)
    sd = np.array(md["sd"], dtype=np.float32)
    print("模型：%s / %s / AUC %.3f" % (md["feat"], md["model"], md["auc"]))
    wm = WhisperModel(LARGE, device="cuda", compute_type="float16")
    fe = FeatureExtractor(feature_size=128, sampling_rate=SR, hop_length=160,
                          chunk_length=30, n_fft=400)

    def _prosody(seg):
        """与训练时一致：时长 / RMS / 标准差 / ZCR / 2–8Hz 调制（logmel_feats 尾 4 位）。"""
        import math
        a = np.asarray(seg, dtype=np.float32)
        rms = float(np.sqrt((a ** 2).mean()))
        zcr = float(((a[:-1] * a[1:]) < 0).mean()) if len(a) > 1 else 0.0
        n = max(2, len(a) // 160)
        env = np.sqrt((a[:n * 160] ** 2).reshape(n, 160).mean(axis=1))
        env = env - env.mean()
        mod = np.abs(np.fft.rfft(env * np.hanning(len(env))))
        band = float(mod[2:9].sum() / (mod.sum() + 1e-9))
        return np.array([rms, float(np.sqrt((a ** 2).var())), zcr, band], dtype=np.float32)

    def score(seg):
        f = fe(np.asarray(seg, dtype=np.float32))[None, :, :]
        sv = ctranslate2.StorageView.from_array(np.ascontiguousarray(f))
        v = np.asarray(wm.model.encode(sv, to_cpu=True))[0].mean(axis=0)
        v = np.concatenate([v, _prosody(seg)])
        x = torch.tensor(((v - mu) / sd)[None, :], dtype=torch.float32)
        with torch.no_grad():
            return float(torch.sigmoid(net(x)).item())

    works = [a.rj] if a.rj else sorted(
        os.path.basename(os.path.dirname(os.path.dirname(p)))
        for p in glob.glob(os.path.join(ROOT, "data", "draft", "*", "asr", "*.json")))
    works = sorted(set(works))
    os.makedirs(OUTDIR, exist_ok=True)
    for rj in works:
        js = sorted(glob.glob(os.path.join(ROOT, "data", "draft", rj, "asr", "*.json")))
        if not js:
            continue
        if a.max_tracks:
            js = js[:a.max_tracks]
        rows = []
        for jf in js:
            try:
                d = json.load(open(jf, encoding="utf-8"))
            except Exception:
                continue
            src = d.get("source", "")
            hits = glob.glob(os.path.join(ROOT, rj, "**", src), recursive=True) or \
                glob.glob(os.path.join(ROOT, rj, "**", re.sub(r"\.json$", "", src)),
                          recursive=True)
            if not hits:
                continue
            y = decode_audio(hits[0], sampling_rate=SR)
            for s in d.get("segments") or []:
                t0, t1 = float(s.get("start", 0)), float(s.get("end", 0))
                ja = (s.get("text") or "").strip()
                if not ja or t1 - t0 < 0.4:
                    continue
                g = a8_group(ja, MN)
                if a.only_unknown and g != "A8表外unknown":
                    continue
                seg = y[int(t0 * SR):int(t1 * SR)]
                if len(seg) < SR * 0.4:
                    continue
                rows.append({"track": os.path.basename(src), "t": round(t0, 1),
                             "dur": round(t1 - t0, 2), "ja": ja,
                             "group": g, "score": round(score(seg), 4)})
            print("  %s %s：累计 %d 段" % (rj, os.path.basename(src)[:26], len(rows)),
                  flush=True)
        rows.sort(key=lambda r: -r["score"])
        sel = [r for r in rows if r["score"] >= a.thr]
        md_path = os.path.join(OUTDIR, rj + ".md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("# 非词汇发声试听清单：%s\n\n" % rj)
            f.write("> 模型 %s/%s，跨作品 AUC %.3f。**按分数排序，需人听确认**——\n"
                    "> 高阈值端仍有约 1.7%% 误报，不能当自动开关。\n\n"
                    % (md["feat"], md["model"], md["auc"]))
            f.write("| 分数 | 时间 | 时长 | A8 组 | 日文 | 轨 |\n|---|---|---|---|---|---|\n")
            for r in sel:
                f.write("| %.3f | %.1fs | %.1fs | %s | %s | %s |\n"
                        % (r["score"], r["t"], r["dur"], r["group"],
                           r["ja"][:38].replace("|", "/"), r["track"][:26]))
        json.dump(rows, open(os.path.join(OUTDIR, rj + ".json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        hi = sum(1 for r in rows if r["score"] >= 0.9)
        print("%s：%d 段（≥0.9 的 %d 段）→ %s" % (rj, len(rows), hi, md_path))
    print("\n清单在 %s" % OUTDIR)


if __name__ == "__main__":
    main()
