# -*- coding: utf-8 -*-
"""用台本打轴：台本文本 + ASR 时间轴 → asr 兼容 JSON（下游 translate/polish 零改动）。

## 为什么用**字符级**对齐，而不是句子级

台本的行断是 PDF 排版断行，粒度比 ASR 细得多（实测 RJ9000013 轨1：台本 63 句 /
ASR 32 句），句子级 DP 对齐匹配率只有 48%。而且台本会把同一句拆成两行
（`あ、先生いらっしゃい！` / `待ってたよー！`），ASR 又常把两句并成一句。

所以改成：**把两边都拆成字符序列，做序列匹配（difflib 匹配块），
再按台本句子边界取首尾字符的时间**。粒度不匹配的问题自然消失。

## 算法

1. 台本清洗 → 去标点 → 字符序列 S（带「每个字符属于哪一句」的映射）
2. ASR 用 **word 级时间戳** → 字符序列 A（每个字符带 start/end）
3. `difflib.SequenceMatcher` 找匹配块（ASR 的错字/漏字会被跳过，不影响其他位置）
4. 每个台本字符 → 对应 ASR 字符的时间；未匹配的按前后线性插值
5. 台本句子的时间 = 首字符 start 到末字符 end

## 输出

`<out_dir>/<track>.json`，结构与 `asr.py` 完全一致（`segments:[{start,end,text,words}]`），
额外 `script_aligned: true` + `align_stats`。下游零改动。

用法：
  python script_align.py <script_dir> <asr_ref_dir> <out_dir> [--dry-run]
"""
import os
import re
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json
import glob
import difflib
import argparse

# 台本清洗：这些都不是台词
SOUND_BLOCK = re.compile(r"//[^/]{0,80}//")
SOUND_TAIL = re.compile(r"//[^/]*$", re.M)          # 未闭合的 // 到行尾
POS_MARK = re.compile(r"[０-９0-9]{1,2}\s*[近中]")   # `１１近` `７中` 位置/时长标记
PAREN_NOTE = re.compile(r"\([^)]{0,40}\)|（[^）]{0,40}）")
KEEP_RE = re.compile(r"[ぁ-んァ-ヶー一-龥]")        # 保留假名/汉字（去标点符号）

TRACK_RE = re.compile(r"(?:トラック|Track|TRACK)\s*([０-９0-9]{1,2})")
SPK_RE = re.compile(r"【([^】]{1,10})】")


def zen2han(s):
    return s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def clean_script_text(text):
    """清掉音效块、位置标记、括号说明，只留台词。"""
    text = SOUND_BLOCK.sub(" ", text)
    text = SOUND_TAIL.sub(" ", text)
    text = POS_MARK.sub(" ", text)
    text = PAREN_NOTE.sub(" ", text)
    return text


def split_tracks(text):
    """整本台本按「トラックN」切分成 {N: 文本}。"""
    marks = [(m.start(), int(zen2han(m.group(1)))) for m in TRACK_RE.finditer(text)]
    if not marks:
        return {1: text}
    out = {}
    for k, (pos, num) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(text)
        out.setdefault(num, "")
        out[num] += TRACK_RE.sub("", text[pos:end], count=1)
    return out


def script_sentences(text):
    """台本 → [(句子文本, 说话人 or "")]。

    句子边界 = `。`（`！？` 不切：台本里 `！` 常常是同一句的第二个感叹，
    而 ASR 会把它们并成一句）。
    """
    out = []
    cur_spk = ""
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("###"):
            continue
        m = SPK_RE.search(ln)
        if m:
            cur_spk = m.group(1)
            ln = SPK_RE.sub("", ln).strip()
            if not ln:
                continue
        # 轨标题行（`【トラック１：...】`）不是台词
        if re.match(r"^【.*(?:トラック|Track|ＥＸ|EX).*】$", ln):
            continue
        ln = clean_script_text(ln).strip()
        if not ln:
            continue
        # 只按 `。` 切；`！？` 留着（它们是语气，不是句界）
        buf = ""
        for p in re.split(r"(?<=。)(?!。)", ln):
            buf += p
            if buf.endswith("。") and len(buf) >= 4:
                out.append((buf.strip(), cur_spk))
                buf = ""
        if buf.strip() and KEEP_RE.search(buf):
            out.append((buf.strip(), cur_spk))
    return out


def pick_txts(txts):
    """同一个作品常有**多份同源台本**（PDF 提取件 + 全章 txt + 分章 txt），
    全读进来会重复 2–3 遍（实测 RJ1437620：20903 句，正文其实约 7000 句）。

    优先级：
      1. 文件名带轨道号（`Track1`/`トラック1`）→ 逐轨，最干净，**只用这些**；
      2. 有「全章/全編/complete」整本 → 只取它（分章和 PDF 都是它的子集）；
      3. 原生 txt（排除 `.extracted.txt`）；
      4. 只剩 PDF 提取件 → 用它。
    """
    if len(txts) <= 1:
        return txts
    per = [p for p in txts if TRACK_RE.search(os.path.basename(p))]
    if per:
        return per
    whole = [p for p in txts
             if re.search(r"全章|全編|全部|complete|\ball\b", os.path.basename(p), re.I)]
    if whole:
        native = [p for p in whole if not p.endswith(".extracted.txt")]
        return (native or whole)[:1]
    native = [p for p in txts if not p.endswith(".extracted.txt")]
    return native or txts


def load_script(script_dir):
    """读台本目录 → {轨道号: [(句, spk)]}。"""
    txts = sorted(glob.glob(os.path.join(script_dir, "*.txt")))
    if not txts:
        raise SystemExit("台本目录里没有 txt：%s" % script_dir)
    picked = pick_txts(txts)
    if len(picked) < len(txts):
        print("  台本文件 %d 个 → 用 %d 个（去重同源版本）" % (len(txts), len(picked)))
    txts = picked
    per_track, whole = {}, []
    for p in txts:
        t = open(p, encoding="utf-8", errors="ignore").read()
        bn = os.path.basename(p)
        # 逐轨台本有三种命名：`仮台本トラック1.txt` / `台词初稿台本_Track1.txt` /
        # **逐轨 PDF 提取件** `1_”推し活”キス練習手コキ.extracted.txt`（RJ1332651，7 个 PDF）。
        # 统一交给 track_no 判，别再只认 TRACK_RE——否则逐轨 PDF 会被当成整本，
        # 7 轨正文全塞进第 1 轨（实测）。
        n = track_no(bn)
        if n is not None:
            per_track.setdefault(n, "")
            per_track[n] += t
        else:
            whole.append(t)
    if per_track:
        return {n: script_sentences(t) for n, t in per_track.items()}
    merged = "\n".join(whole)
    return {n: script_sentences(t) for n, t in split_tracks(merged).items()}


def track_no(name):
    """从文件名取轨道号：`01_...` `1章...` `トラック1...` `tr01` `EX_...`。"""
    base = os.path.basename(name)
    m = TRACK_RE.search(base)
    if m:
        return int(zen2han(m.group(1)))
    # ⚠️ 2026-10-19 新踩：`tr00.mp3` / `tr01.mp3`（RJ9000008）——`tr` 前缀不是
    #    `Track`，TRACK_RE 不匹配、数字也不在开头 → 整部作品的台本被跳过。
    m = re.match(r"^\s*tr\s*0*(\d{1,2})(?![0-9])", base, re.I)
    if m:
        return int(m.group(1))
    # ⚠️ 2026-10-21：必须含**逗号**（半角 `,` 与全角 `，`）——asmr.one 上大量作品
    #    的轨名是 `1,プロローグ.mp3` 这种「序号,标题」格式（RJ9000014 25 轨全是），
    #    旧正则只认空格/下划线/点/横线/括号 → track_no 全返回 None，
    #    台本与 ASR 对不上号，整部作品的打轴静默失效。
    for pat in (r"^\s*(\d{1,2})[\s_,.，、\-）)]", r"^\s*(\d{1,2})[章話]"):
        m = re.match(pat, base)
        if m:
            return int(m.group(1))
    if re.match(r"^\s*EX", base, re.I):
        return 99
    # ⚠️ `仮台本EXトラック.txt` 的 EX 不在开头，但它是「EX 轨」台本 → 归 99。
    #    不认的话会被丢进 whole 然后被忽略（实测踩到）。
    if re.search(r"(?:^|[^A-Za-z])EX(?:[^A-Za-z]|$)", base, re.I):
        return 99
    return None


def asr_chars(d):
    """ASR JSON → (字符列表, 每字符时间列表)。用 word 级时间戳。"""
    chars, times = [], []
    for seg in (d.get("segments") or []):
        ws = seg.get("words") or []
        if ws:
            for w in ws:
                s, e = w.get("s"), w.get("e")
                for c in (w.get("w") or ""):
                    if KEEP_RE.match(c):
                        chars.append(c)
                        times.append((s, e))
        else:
            txt = seg.get("text") or ""
            ks = [c for c in txt if KEEP_RE.match(c)]
            s, e = seg.get("start") or 0, seg.get("end") or 0
            step = (e - s) / float(max(1, len(ks)))
            for i, c in enumerate(ks):
                chars.append(c)
                times.append((s + i * step, s + (i + 1) * step))
    return chars, times


def align_chars(s_chars, a_chars):
    """字符级序列匹配 → 每个 S 字符对应的 A 索引（None = 没对上）。"""
    sm = difflib.SequenceMatcher(None, s_chars, a_chars, autojunk=False)
    idx = [None] * len(s_chars)
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            idx[blk.a + k] = blk.b + k
    return idx


def fill_char_times(idx, a_times, total_dur):
    """「S 字符 → A 索引」变成「S 字符 → 时间」，没对上的按前后插值。"""
    n = len(idx)
    out = [None] * n
    for k, ai in enumerate(idx):
        if ai is not None and ai < len(a_times):
            t = a_times[ai]
            if t[0] is not None and t[1] is not None:
                out[k] = (float(t[0]), float(t[1]))
    # 未匹配的标成 None，下面按「前后已知时间之间」铺开
    k = 0
    while k < n:
        if out[k] is None:
            j = k
            while j < n and out[j] is None:
                j += 1
            lo = None
            for q in range(k - 1, -1, -1):
                if out[q] is not None:
                    lo = out[q][1]
                    break
            hi = None
            for q in range(j, n):
                if out[q] is not None:
                    hi = out[q][0]
                    break
            lo = 0.0 if lo is None else lo
            hi = (total_dur or lo + 1.0) if hi is None else hi
            cnt = j - k
            if hi <= lo:
                hi = lo + 0.4 * cnt
            step = (hi - lo) / float(cnt)
            for q in range(cnt):
                out[k + q] = (lo + q * step, lo + (q + 1) * step)
            k = j
        else:
            k += 1
    return out


def make_words(text, start, end):
    """把一句台词按字数均分到词级时间戳（下游 cues.py 需要 words）。"""
    ks = [c for c in text if KEEP_RE.match(c)]
    if not ks:
        return []
    step = (end - start) / float(len(ks))
    return [{"s": round(start + i * step, 2), "e": round(start + (i + 1) * step, 2),
             "w": c} for i, c in enumerate(ks)]


CHAPTER_RE = re.compile(r"[●◯○■◇◆]\s*([０-９0-9]{1,2})\s*[章話]")


def script_shape(script_dir):
    """台本有没有「轨道标记」/「章标记」——决定能不能和音轨一一对应。

    实测两种排版：
      · 有 `トラックN` / 文件名 `TrackN` → 直接按号对应（RJ9000013 / RJ9000008）；
      · 只有 `●１章`…`●２１章`（RJ1437620，21 章 / 18 条音轨）→ **无法自动对应**。
        以前会把整本塞给第 1 轨，第 2 轨起全跳过，第 1 轨还拿整本去对齐（静默出错）。
    """
    txts = pick_txts(sorted(glob.glob(os.path.join(script_dir, "*.txt"))))
    blob = ""
    for p in txts:
        blob += open(p, encoding="utf-8", errors="ignore").read()
    return {"tracks": len(TRACK_RE.findall(blob)),
            "chapters": len(CHAPTER_RE.findall(blob))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script_dir")
    ap.add_argument("asr_ref_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    script = load_script(a.script_dir)
    print("台本：%d 轨，句子数 %s" % (len(script),
          {k: len(v) for k, v in sorted(script.items())}))
    refs = sorted(glob.glob(os.path.join(a.asr_ref_dir, "*.json")))
    print("ASR 轨：%d 个" % len(refs))
    if not refs:
        raise SystemExit("ASR 目录为空：%s" % a.asr_ref_dir)
    # ⚠️ 台本只有「章」没有「轨」时**不能猜**：整本塞给第 1 轨会静默产出错轴。
    if len(script) == 1 and 1 in script and len(refs) > 1:
        sh = script_shape(a.script_dir)
        if sh["tracks"] == 0:
            raise SystemExit(
                "台本没有轨道标记（章标记 %d 个），与 %d 条音轨无法自动对应。\n"
                "→ 请提供逐轨台本，或人工给出「章 → 轨」映射后再跑。\n"
                "（强行按整本对齐会把整本塞给第 1 轨，第 2 轨起全部跳过）"
                % (sh["chapters"], len(refs)))
    os.makedirs(a.out_dir, exist_ok=True)

    n_ok = n_miss = 0
    tot_rate = []
    dial_rates = []
    queue = []                # E5：逐句低置信对齐清单（待复核）
    for ref in refs:
        # ⚠️ 2026-10-21：source 必须是**音频名**（`X.mp3`），不能带 `.json`。
        #    原来直接 basename(ref) 把 `X.mp3.json` 写进 source，下游 polish 用它
        #    命名成品 → 产出 `X.mp3.json.lrc`（双扩展名），与 asr.py 直出路径不一致，
        #    check.py/成品目录也会多出一份重复文件（实测 RJ9000014 25 轨全中）。
        base = re.sub(r"\.json$", "", os.path.basename(ref))
        key = os.path.splitext(base)[0]
        no = track_no(base)
        sents = script.get(no) if no is not None else None
        if not sents:
            print("  跳过（台本没有这一轨）：%s" % base[:46])
            n_miss += 1
            continue
        d = json.load(open(ref, encoding="utf-8"))
        a_chars, a_times = asr_chars(d)
        total_dur = d.get("duration") or (a_times[-1][1] if a_times else 0) or 0

        s_chars, owner, pos_in = [], [], []
        for si, (txt, _spk) in enumerate(sents):
            for pi, c in enumerate(txt):
                if KEEP_RE.match(c):
                    s_chars.append(c)
                    owner.append(si)
                    pos_in.append(pi)
        if not s_chars or not a_chars:
            print("  跳过（空文本）：%s" % base[:46])
            n_miss += 1
            continue

        idx = align_chars(s_chars, a_chars)
        ctimes = fill_char_times(idx, a_times, total_dur)
        rate = sum(1 for x in idx if x is not None) / float(len(idx))
        tot_rate.append(rate)

        # 句子级过滤：台本里混着**演出指示**（`位置そのままで` / `顔の方を向いて`），
        # 那些不会被说出来 → 字符匹配率天然很低。用匹配率当过滤器，
        # 比正则猜「哪句是指示」可靠得多（实测台本 3516 字符 vs ASR 2362，差的就是指示）。
        # ---- 字符级剔除音效/指示 ----
        # 台本把音效写在单独一栏，提取后和台词**粘成一行**（`住宅街の環境音 あ、先生いらっしゃい！`）。
        # 这些字不会被说出来 → 在字符对齐里是**连续未匹配段**。按「连续 ≥4 个未匹配实字」
        # 精确剔除，比正则猜「哪段是音效」可靠得多（台词里也有「音」字）。
        drop = set()
        drop_spans = []                 # 逐段留原文，用于区分「拟声」和「演出指示」
        for si in range(len(sents)):
            ks = [k for k, o in enumerate(owner) if o == si]
            run = []
            for k in ks:
                if idx[k] is None:
                    run.append(k)
                else:
                    if len(run) >= 4:
                        for q in run:
                            drop.add((si, pos_in[q]))
                        drop_spans.append("".join(s_chars[q] for q in run))
                    run = []
            if len(run) >= 4:
                for q in run:
                    drop.add((si, pos_in[q]))
                drop_spans.append("".join(s_chars[q] for q in run))

        def rebuild(txt, si):
            keep = [c for pi, c in enumerate(txt) if (si, pi) not in drop]
            return re.sub(r"\s+", " ", "".join(keep)).strip()

        segs = []
        n_drop = 0
        sent_stat = []      # [(si, kept, matched, seg_start_or_None)] 供 review_queue 用
        for si, (txt, spk) in enumerate(sents):
            clean_txt = rebuild(txt, si)
            nk = sum(1 for c in clean_txt if KEEP_RE.match(c))
            if nk < 2:                      # 整句都是音效/指示
                n_drop += 1
                sent_stat.append((si, nk, 0, None))
                continue
            pos = [k for k, o in enumerate(owner) if o == si and (si, pos_in[k]) not in drop]
            if not pos:
                n_drop += 1
                sent_stat.append((si, nk, 0, None))
                continue
            txt = clean_txt
            st = ctimes[pos[0]][0]
            en = ctimes[pos[-1]][1]
            if en <= st:
                en = st + max(0.4, len(txt) * 0.12)
            # 强制单调：字符级时间可能让后句起点早于前句终点（实测 73.32 > 72.92）。
            # 字幕重叠会让播放器跳行，必须修。往后推，不往前拉。
            if segs and st < segs[-1]["end"]:
                st = segs[-1]["end"]
                if en <= st:
                    en = st + max(0.4, len(txt) * 0.12)
            seg = {"start": round(st, 2), "end": round(en, 2),
                   "text": txt, "words": make_words(txt, st, en)}
            if spk:
                # 两个字段都写：`speaker` 是下游 build_units 读的（polish.py:143），
                # `spk` 语义更清楚、留给人看/以后扩展。
                seg["speaker"] = spk
                seg["spk"] = spk
            segs.append(seg)
            sent_stat.append((si, nk, sum(1 for k in pos if idx[k] is not None),
                              round(st, 2)))
        # ---- 两个匹配率：别再看「字符匹配率」判断对齐好坏（2026-10-19）----
        # 旧报告说「字符匹配 70% 偏低」，实测是**分母假象**：台本里 27–28% 的字是
        # 拟声台词（`んちゅっれろれろ`）+ 演出指示（`演技に合わせて` `急激に耳に近づく感じ`），
        # ASR 本来就不会匹配到它们，而管线**已经按「连续 ≥4 未匹配」把它们剔除**。
        # RJ9000013 七轨实测：全量 72.3%，剔掉这些之后**台词匹配 100.0%**（残留未匹配 4 字 / 19742）。
        # 所以报两个数：`char_match_rate`（全量，含拟声/指示）和
        # `dialogue_match_rate`（只算保留的台词）——后者才是「对齐好不好」。
        n_all = len(s_chars)
        n_matched = sum(1 for x in idx if x is not None)
        n_dropped = len(drop)
        # 剔除段分类：汉字占比 <15% 记作拟声（`んちゅ`/`ぐぶ`），否则记作演出指示
        onomat_chars = direction_chars = 0
        for sp in drop_spans:
            nk = sum(1 for c in sp if "\u4e00" <= c <= "\u9fff")
            if nk / float(max(1, len(sp))) < 0.15:
                onomat_chars += len(sp)
            else:
                direction_chars += len(sp)
        n_dialogue = max(1, n_all - n_dropped)
        dialogue_rate = n_matched / float(n_dialogue)

        out = {"source": base, "language": "ja", "duration": total_dur,
               "container_duration": d.get("container_duration") or total_dur,
               "truncated": False, "coverage": 1.0, "segments": segs,
               "script_aligned": True,
               "align_stats": {"script_sents": len(sents),
                               "kept_sents": len(segs),
                               "dropped_directions": n_drop,
                               "char_match_rate": round(rate, 3),
                               "dialogue_match_rate": round(dialogue_rate, 3),
                               "dropped_chars": n_dropped,
                               "dropped_onomat_chars": onomat_chars,
                               "dropped_direction_chars": direction_chars}}
        print("  %-40s 台本%3d 句 → 留 %3d（丢句 %2d）/ 台词匹配 %.0f%%"
              "（全量 %.0f%%：拟声 %d + 指示 %d 字）"
              % (base[:40], len(sents), len(segs), n_drop, dialogue_rate * 100,
                 rate * 100, onomat_chars, direction_chars))
        dial_rates.append(dialogue_rate)
        # ---- E5（2026-10-21）：对不齐的句子标「待复核」，不静默采用 ----
        # 逐句算「保留字里匹配上的比例」：kept≥2 且 <60% → 这句的台本文本对到 ASR
        # 时间轴的置信度低（可能这句位置对错了，或 ASR 漏听了）。写进 review_queue.json，
        # review.py 审阅页会打「台本待复核」标。整体匹配率照旧报，但可疑句不再混在成品里。
        REV_WEAK = 0.6
        for si, nk, nm, st in sent_stat:
            if st is None or nk < 2 or nm / float(nk) >= REV_WEAK:
                continue
            _txt, _spk = sents[si]
            _clean = re.sub(r"\s+", " ", _txt).strip()
            # 分类：**没有汉字**的句子（うふふ♡ / あぁ～ん / ダーメ♡）是拟声/笑声，
            # ASR 本来就不会匹配——不算待复核，单独统计（与 dialogue_match_rate 的
            # 「分母假象」同源：这些字已被管线按连续未匹配剔除）。真有风险的是
            # **带汉字的台词**对不上（位置错/ASR 漏听）。
            _kind = "suspect" if any("\u4e00" <= c <= "\u9fff" for c in _clean) else "nonlex"
            queue.append({"source": base, "track": key, "sentence": si,
                          "start": st, "spk": _spk or "", "kind": _kind,
                          "text": _clean,
                          "kept_chars": nk, "matched_chars": nm,
                          "rate": round(nm / float(nk), 2)})
        if not a.dry_run:
            json.dump(out, open(os.path.join(a.out_dir, key + ".json"), "w",
                                encoding="utf-8"), ensure_ascii=False, indent=1)
        n_ok += 1
    if tot_rate:
        print("\n平均字符匹配率 %.1f%% · 平均台词匹配率 %.1f%%"
              % (100.0 * sum(tot_rate) / len(tot_rate),
                 100.0 * sum(dial_rates) / max(1, len(dial_rates))))
    if queue:
        # ⚠️ 绝不写进 out_dir（标准流程里它就是 asr/ 目录，下游 translate/polish
        #    按 *.json glob 会把它当 ASR 文件吃掉——实测直接崩 KeyError）。
        #    写到 out_dir 的父目录（标准流程即 draft/<rj>/review_queue.json）。
        qdir = os.path.dirname(os.path.normpath(a.out_dir)) or a.out_dir
        qpath = os.path.join(qdir, "review_queue.json")
        n_sus = sum(1 for q in queue if q.get("kind") == "suspect")
        json.dump({"weak_sentence_rate": REV_WEAK,
                   "suspect": n_sus,
                   "nonlex": len(queue) - n_sus,
                   "items": queue},
                  open(qpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("E5 待复核：%d 句对齐置信度 <%.0f%%（汉字台词 %d · 拟声/笑声 %d）→ %s"
              % (len(queue), REV_WEAK * 100, n_sus, len(queue) - n_sus, qpath))
    print("完成 %d 轨 · 跳过 %d 轨 → %s" % (n_ok, n_miss, a.out_dir))


if __name__ == "__main__":
    main()
