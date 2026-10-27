# -*- coding: utf-8 -*-
"""E1 锁的匹配逻辑（纯函数，2026-10-26，GPT 评审 Q11 的落地）。

## 为什么需要这个

锁现在只按 `文件名|时间戳` 存（3224 条）。**重跑 ASR 后时间戳会变**（分段不同），
实测 4 条锁只命中 2 条 —— 人工修正静默丢失。

## 双 key 方案

1. **主 key**：`(文件, 时间戳)` —— 原样命中，最快最准。
2. **备 key**：`(文件, 规范化日文)` —— 时间戳漂了，但**同一句日文**还在，就认它。
   - 只在该文件内**唯一**时套用；同一句出现多次（ASMR 复读很常见）→ **不猜**，
     记成「锁失配待人工」。
3. 兜底：`(文件, 旧中文)` —— 只在模型这次恰好吐回同一句时有用（历史行为，保留）。

匹配顺序：主 key → 备 key（唯一才用）→ 旧中文。任何一步命中即返回，且返回来源，
方便日志里说清「这行是按时间戳锁的，还是按日文认回来的」。
"""
import re
import unicodedata

# 规范化时去掉的字符：空白 + 中日标点 + 长音符（ASR 对 ー 的取舍很不稳定）
_STRIP = re.compile(r"[\s\u3000。、，．\.…‥・!?！？「」『』（）()\[\]【】〈〉《》"
                    r"〜～ー―—\-－~♪♡♥☆★\"'“”‘’]")
_KEEP = re.compile(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]")


def norm_ja(s):
    """日文规范化：全半角统一 → 去标点空白 → 去长音 → 只留假名/汉字/数字/字母。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = _STRIP.sub("", s)
    s = s.lower()
    s = _KEEP.sub("", s)
    return s


def build_ja_index(lock_map):
    """lock_map: {(file, ts): {"zh":..., "ja":...}} → {(file, norm_ja): [ts, ...]}。"""
    idx = {}
    for (fn, ts), v in lock_map.items():
        ja = (v or {}).get("ja") if isinstance(v, dict) else ""
        k = norm_ja(ja)
        if not k:
            continue
        idx.setdefault((fn, k), []).append(ts)
    return idx


def lookup(lock_map, ja_index, old_map, fn, ts, text, ja=""):
    """返回 (最终文本 or None, 命中来源)。None 表示没有锁管这一行。"""
    v = lock_map.get((fn, ts))
    if isinstance(v, dict) and (v.get("zh") or "").strip():
        return v["zh"], "ts"
    k = norm_ja(ja)
    if k:
        hits = ja_index.get((fn, k)) or []
        if len(hits) == 1:
            z = (lock_map.get((fn, hits[0])) or {}).get("zh")
            if (z or "").strip():
                return z, "ja"
        elif len(hits) > 1:
            return None, "ambiguous"       # 同句多锁 → 不猜
    v2 = (old_map or {}).get((fn, text))
    if (v2 or "").strip():
        return v2, "old"
    return None, "miss"


def selftest():
    ok = []
    lmap = {("a.mp3", "[00:10.00]"): {"zh": "甲", "ja": "おはよう"},
            ("a.mp3", "[00:20.00]"): {"zh": "乙", "ja": "おはよう"},   # 同句两处
            ("a.mp3", "[00:30.00]"): {"zh": "丙", "ja": "こんばんは"}}
    idx = build_ja_index(lmap)
    ok.append(("主 key 命中", lookup(lmap, idx, {}, "a.mp3", "[00:10.00]", "x",
                                    "おはよう")[1] == "ts"))
    ok.append(("时间戳漂了但日文唯一 → 认回",
               lookup(lmap, idx, {}, "a.mp3", "[00:31.00]", "x", "こんばんは")
               == ("丙", "ja")))
    ok.append(("同句多锁 → 不猜",
               lookup(lmap, idx, {}, "a.mp3", "[00:11.00]", "x", "おはよう")[1]
               in ("ambiguous", "ts") and
               lookup(lmap, idx, {}, "a.mp3", "[00:11.00]", "x", "おはよう")[0] is None))
    ok.append(("规范化能吃掉标点/长音",
               norm_ja("おはよう！ー") == norm_ja("おはよう")))
    ok.append(("无关日文不命中",
               lookup(lmap, idx, {}, "a.mp3", "[00:11.00]", "x", "さようなら")
               == (None, "miss")))
    for n, g in ok:
        print("  %s %s" % ("✅" if g else "❌", n))
    n = sum(1 for _, g in ok if g)
    print("%s lock_match selftest %d/%d" % ("✅" if n == len(ok) else "❌", n, len(ok)))
    return 0 if n == len(ok) else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
