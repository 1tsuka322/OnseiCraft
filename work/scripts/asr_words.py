"""词级时间戳与段文本的同步（2026-10-28 R154）。

⚠️ **为什么需要这个模块**

cue 管线（`cues.flatten_for_cues`）**优先用 `seg["words"]`，根本不看 `seg["text"]`**。
而 01 层的拉丁幻觉清理（`asr_latin_halluc.py`）和 02 层的 errata（`asr_fix_scan.py`）
历史上**都只改 `text`、没动 `words`**。

后果（实测 RJ1623028）：
- 02 把 `性液中毒症` 改成 `精液中毒症`，`text` 对了、`words` 还是旧的
  → cue 管线拿 `words` 去翻译 → 成品仍是「性液中毒症」
- 01 把 `直近でショット` 判为幻觉、`text` 清空，`words` 还在
  → 译文照旧翻出「近距离 快门」

也就是说：**默认路线（cue_pipeline=1）下，错字表整体失效。**
这跟 `pipeline.json` 路径写错、`RELOAD_SEC` 被当块大小是同一类——
「声明改了一处，实际生效的是另一处」。

修法分两层：
1. 01/02 层改 `text` 时**同步改 `words`**（本模块 `patch_words` / `clear_words`），保住词级时间戳；
2. `cues.flatten_for_cues` 加**安全网**：`words` 拼起来和 `text` 不一致就丢掉 `words`，
   改用 `text` 合成伪词（`consistent()`）。宁可 cue 边界粗一点，也不能译错。
"""

import re

_WS = re.compile(r"[\s\u3000]+")


def _norm(s):
    return _WS.sub("", s or "")


def words_text(seg):
    """words 拼起来的文本。"""
    return "".join((w.get("w") or "") for w in (seg.get("words") or []))


def consistent(seg):
    """`text` 与 `words` 是否一致（忽略空白）。`words` 缺失视为一致。"""
    if not seg.get("words"):
        return True
    return _norm(words_text(seg)) == _norm(seg.get("text") or "")


def clear_words(seg):
    """丢掉词级时间戳（下游会用 text 合成伪词）。"""
    seg.pop("words", None)


def patch_words(seg, ja, fix):
    """在词级时间戳上做与 `text.replace(ja, fix)` 等价的替换。

    成功返回 True（`seg["words"]` 就地更新）；无法安全对齐返回 False，
    此时**不会**改动 words —— 调用方应当 `clear_words()` 兜底，
    让下游退回 text，避免出现「text 改了、words 没改」的不一致。
    """
    words = seg.get("words")
    if not words or not ja:
        return False
    full = "".join((w.get("w") or "") for w in words)
    if ja not in full:
        # words 里本来就没有这个错形（可能在别的段）→ 不算失败，保持原样
        return True

    # 每个 word 在 full 里的字符区间
    spans, pos = [], 0
    for w in words:
        n = len(w.get("w") or "")
        spans.append((pos, pos + n, w))
        pos += n

    guard = 0
    while ja in full:
        guard += 1
        if guard > 50:                    # 防御：异常输入不至于死循环
            return False
        i = full.find(ja)
        j = i + len(ja)
        hit = [(a, b, w) for a, b, w in spans if b > i and a < j]
        if not hit:
            return False
        first, last = hit[0], hit[-1]
        head = (first[2].get("w") or "")[:i - first[0]]
        tail = (last[2].get("w") or "")[j - last[0]:]
        merged = dict(first[2])
        merged["w"] = head + fix + tail
        merged["e"] = last[2].get("e", first[2].get("e"))
        # 区间内的其余词被并入 merged
        out = [w for a, b, w in spans if b <= i]
        out.append(merged)
        out += [w for a, b, w in spans if a >= j]
        spans, pos = [], 0
        for w in out:
            n = len(w.get("w") or "")
            spans.append((pos, pos + n, w))
            pos += n
        full = "".join((w.get("w") or "") for _, _, w in spans)

    seg["words"] = [w for _, _, w in spans]
    return True


def strip_latin_words(seg, keep):
    """按 `keep(text)` 的结果重建 words：把被删掉的拉丁词对应的 word 一起删。

    `keep` 是「这段文本清洗后还剩什么」的函数（见 asr_latin_halluc._strip_latin）。
    实现上不追求字符级对齐——只要 words 里含拉丁串的 word 被删掉、
    且拼起来和 `keep(text)` 一致即可；不一致就返回 False（调用方清空 words）。
    """
    words = seg.get("words")
    if not words:
        return False
    old = seg.get("text") or ""
    new = keep(old)
    # 逐词判断：含 3+ 连续拉丁的 word 视为幻觉词，删掉
    out = [w for w in words if not re.search(r"[A-Za-z]{3,}", w.get("w") or "")]
    if _norm("".join((w.get("w") or "") for w in out)) != _norm(new):
        return False
    seg["words"] = out
    return True
