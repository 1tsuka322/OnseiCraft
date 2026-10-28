# -*- coding: utf-8 -*-
"""ASR 层的「拉丁幻觉」清理（2026-10-28 R154，方案：Gemini 建议 + 本项目实测修正）。

## 问题

Whisper 在 ASMR 的非语音段（舔耳/摩擦/近距离呼吸）会**很自信地**认为「这里有人在说话」，
于是解码器找不到日语 Token，就回退到多语言预训练语料里的拉丁水印/随机英文：

```
う・・・ Holmes Meanwhile in came off
はぁ・・・ヒリヒリはぁ・・・ Huh, Ah, no, sorry!
おね、お願いしまーすsolmaseliv
回中 Okay, now I'm going to over this end of the day
```

实测（RJ01654115 轨 04）：**129 段里 7 段**是这种。

## 为什么必须在 01/02 层拦

`no_speech_prob` 对这类段**无效**（实测整轨识别被它砍光过，已证伪），所以不能拿它当必要条件。
更要命的是：**HY-MT 会把这些垃圾翻成通顺中文**——`与此同时，霍姆斯也进来了。`
九项门禁一条都抓不到（没有拉丁、没有假名、时间轴正常、语义看着还顺）。
Sakura 至少原样留着，被 `check` 的拉丁残留抓住。

## 判据（两条，都是确定性的）

| 判据 | 条件 | 处理 |
|---|---|---|
| A 高拉丁占比 | 去标点后 `拉丁字母数 / 总字符数 > 0.4` | 直接判幻觉，**无视 no_speech_prob** |
| B 低占比 + 声学不可信 | 有 ≥4 连续非白名单拉丁，且 `avg_logprob < -0.8` 或 `no_speech_prob > 0.4` | 判幻觉 |

白名单只留真借词（`ASMR BGM LINE SNS APP VIP OK NG AV JK OL SM NTR POV CM TV HP`）。

## 用法

```bash
# 只出报告（默认，不写任何文件）
python work/scripts/asr_latin_halluc.py --asr work/data/draft

# 真清（会先备份，把 text 置空 + 打 halluc_latin 标记）
python work/scripts/asr_latin_halluc.py --asr work/data/draft --apply
```

⚠️ `--apply` 会改 01_asr 的产物 → 下游 03/04 全部失效重跑。**先看报告再决定。**
"""
import argparse
import glob
import json
import asr_words  # text 改了必须同步 words（cue 管线读 words 不读 text）
import os
import re
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

LATIN_OK = {"ASMR", "BGM", "LINE", "SNS", "APP", "VIP", "OK", "NG", "AV", "JK", "OL",
            "SM", "NTR", "POV", "CM", "TV", "HP", "DVD", "SE", "ID", "BBA", "COSPLAY"}
_LATIN = re.compile(r"[A-Za-z]+")
_PUNCT = re.compile(r"[\s、。，,.!?！？…‥~～ー〜「」『』()（）♪♡]")
RATIO_MAX = 0.40        # 判据 A
RUN_MIN = 4             # 判据 B：连续拉丁下限
LP_MAX = -0.8           # 判据 B：avg_logprob 低于此 = 模型在硬猜
NSP_MAX = 0.40          # 判据 B：或 no_speech_prob 高于此


def judge(text, avg_logprob=None, nsp=None):
    """返回 (是否幻觉, 原因, 拉丁占比)。"""
    t = _PUNCT.sub("", text or "")
    if not t:
        return False, "", 0.0
    words = _LATIN.findall(text or "")
    # ⚠️ 占比只算**非白名单**的拉丁字符。第一版算的是全部拉丁字符，
    # 于是 `OK`（占比 1.00）、`LINEしてね`、`これがASMR。` 全被判成幻觉——
    # 那是正常借词，不是幻觉。
    bad = [w for w in words if w.upper() not in LATIN_OK]
    bad_chars = sum(len(w) for w in bad)
    ratio = bad_chars / len(t)
    if ratio > RATIO_MAX:
        return True, "drop:ratio:%.2f" % ratio, ratio
    long_bad = [w for w in bad if len(w) >= RUN_MIN]
    if long_bad:
        lp = avg_logprob if avg_logprob is not None else 0.0
        ns = nsp if nsp is not None else 0.0
        if lp < LP_MAX or ns > NSP_MAX:
            # ⚠️ 低占比 = 正常台词里混进了幻觉词（`あ、白い方?…Viveだからね`）。
            # 整行丢掉会把真台词一起杀了 → 只摘掉那几个拉丁词。
            return True, "strip:%s(lp=%.2f,nsp=%.2f)" % (long_bad[0], lp, ns), ratio
    return False, "", ratio


def scan(asr_root):
    rows = []
    files = sorted(glob.glob(os.path.join(asr_root, "*", "asr", "*.json")))
    for p in files:
        rj = os.path.basename(os.path.dirname(os.path.dirname(p)))
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for s in d.get("segments") or []:
            tx = s.get("text") or ""
            if not tx.strip():
                continue
            hit, why, ratio = judge(tx, s.get("avg_logprob"), s.get("no_speech_prob"))
            if hit:
                rows.append({"rj": rj, "track": d.get("source") or os.path.basename(p),
                             "t": round(float(s.get("start", 0)), 2),
                             "text": tx, "why": why, "ratio": round(ratio, 2),
                             "lp": s.get("avg_logprob"), "nsp": s.get("no_speech_prob")})
    return rows, len(files)


def _strip_latin(text):
    """只摘掉非白名单的拉丁词，保留日文（用于低占比的「台词里混了幻觉词」）。"""
    def rep(m):
        w = m.group(0)
        return "" if w.upper() not in LATIN_OK else w
    return re.sub(r"[A-Za-z]+", rep, text or "").strip()


def apply(asr_root, rows):
    by_file = {}
    for r in rows:
        by_file.setdefault((r["rj"], r["track"]), []).append(r)
    n_drop = n_strip = 0
    for (rj, track), items in by_file.items():
        cands = [p for p in glob.glob(os.path.join(asr_root, rj, "asr", "*.json"))
                 if (json.load(open(p, encoding="utf-8")).get("source") or "") == track]
        if not cands:
            continue
        p = cands[0]
        shutil.copy2(p, p + ".bak_latinhalluc")
        d = json.load(open(p, encoding="utf-8"))
        want = {it["t"]: it for it in items}
        for s in d.get("segments") or []:
            t = round(float(s.get("start", 0)), 2)
            it = want.get(t)
            if not it:
                continue
            old = s.get("text") or ""
            if it["why"].startswith("drop:"):
                s["text"] = ""
                # ⚠️ cue 管线读 words 不读 text：只清 text 等于没清
                #    （实测：`直近でショット` 判了 drop，译文照旧翻出「近距离 快门」）
                asr_words.clear_words(s)
                n_drop += 1
            else:
                s["text"] = _strip_latin(old)
                if not asr_words.strip_latin_words(s, _strip_latin):
                    asr_words.clear_words(s)
                n_strip += 1
            s["halluc_latin"] = {"old": old, "why": it["why"], "action": it["why"].split(":")[0]}
        json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return n_drop, n_strip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asr", default=os.path.join(ROOT, "data", "draft"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    rows, nf = scan(a.asr)
    nd = sum(1 for r in rows if r["why"].startswith("drop:"))
    print("扫描 %d 个 ASR 文件：命中 %d 段（整段丢弃 %d / 只摘拉丁词 %d）"
          % (nf, len(rows), nd, len(rows) - nd))
    for r in rows[:20]:
        print("  %s %.1fs 占比%.2f  %s" % (r["rj"], r["t"], r["ratio"], r["text"][:52]))
    if len(rows) > 20:
        print("  …（--json 看全部）")
    if a.json:
        json.dump(rows, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("→ %s" % a.json)
    if a.apply:
        nd, ns = apply(a.asr, rows)
        print("已清：整段丢弃 %d / 只摘拉丁词 %d（原文存进 halluc_latin，有 .bak_latinhalluc 备份）"
              % (nd, ns))
        print("⚠️ 下游 03/04 需重跑：01_asr 的输入哈希已变")


if __name__ == "__main__":
    main()
