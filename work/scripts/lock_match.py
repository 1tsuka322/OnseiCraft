# -*- coding: utf-8 -*-
"""E1 锁的匹配逻辑（纯函数）。

## 演进史（别回退）

- **v1（2026-10-21）**：只按 `文件名|时间戳` 存。重跑 ASR / 改切分后时间戳漂 0.3~1.6s，
  人工修正静默丢失。
- **v2（2026-10-26）**：双 key —— 时间戳 + **全文件唯一的规范化日文**。
  比 v1 好，但仍有两处硬伤：
  1. **要求日文在全文件唯一**。ASMR 复读句极多，一重复就「不猜」→ 直接丢。
  2. **日文要逐字相等**。errata 改一个字（`お写生`→`お射精`）就整条失效。
  3. 完全**不看时间**，只要唯一就套用，理论上能套到 20 分钟外的同句上。
- **v3（2026-10-31，P0）**：**文本锚定 + 时间窗**，算法与 `rebase_overlay_by_text.py`
  同一套（那套已在 78 条 overlay 上验证过）：
  `score = 0.7*sim(锚, cue日文) + 0.3*(1 - 漂移/窗口)`，
  相似度取 `max(lcs_ratio, jaccard)` —— lcs 抓连续长片段，jaccard 容忍汉字/假名写法差异。

## 为什么是 cue 级而不是行级

锁的 key 是**显示行**时间戳，但一条 cue 会被 `render_display_lines` 切成 1~3 个显示行，
切法依赖最终译文 → **写盘前拿不到行清单**。所以先把锁分给 **cue**（cue 的 start/end/ja
在写盘前就确定），再在写盘时把锁落到该 cue 里**时间最近的那个显示行**。

## 判据（阈值都有实测依据，别随手调）

| 参数 | 值 | 依据 |
|---|---|---|
| `WINDOW` | 8.0s | cue 漂移实测 0.3~1.6s；重切分后更大，8s 留余量 |
| `MAX_DRIFT` | 2.0s | 超过 2s 多半撞上相邻的**重复句**（短句 sim 会虚高到 1.0） |
| `MIN_SIM` | 0.55 | 假名/汉字写法差异实测落在 0.55~0.75；0.85 只能捞回 12/78 |
| `MIN_SCORE` | 0.75 | 0.7*0.55 + 0.3*(1-2/8) = 0.61；配 drift 项后 0.75 是「两项都过得去」 |
| `TIE` | 0.06 | 两个候选分差 < 0.06 视为分不清 → **不猜**（宁可漏，不可错位） |

## ⚠️ 没有日文锚点 → 一律不猜（2026-10-31 首次链路验证的教训）

曾经有「窗口内时间唯一就认它」的兜底，实测立刻出事：RJ01654115 的锁
`[05:51.22]` 文案是「啊 不行 不能随便把任何东西交给别人…」，而该时刻 ASR 是
`おまんこおかしくなっちゃう`（小穴要变得奇怪了）—— 锁的 ts 早已和它的句子错位，
纯按时间套用就把一句不相干的中文盖了上去。**错位比丢失更坏**：丢失会被覆盖率
门禁报出来，错位看起来一切正常。所以没有 ja 的锁只认精确时间戳，其余交
`lock_migrate.py` 补锚或人工核。

## 不猜的两种情形（都进 `ambiguous`，交人工）

1. 同一个 cue 有两条锁、分数相近。
2. 一条锁有两个 cue 分数相近。

**错位比丢失更坏**：把 A 句的人工修正贴到 B 句上，成品看起来「有修正」，实际是错的。
"""
import re
import unicodedata

# 规范化时去掉的字符：空白 + 中日标点 + 长音符（ASR 对 ー 的取舍很不稳定）
_STRIP = re.compile(r"[\s\u3000。、，．\.…‥・!?！？「」『』（）()\[\]【】〈〉《》"
                    r"〜～ー―—\-－~♪♡♥☆★\"'“”‘’]")
_KEEP = re.compile(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]")
_TS = re.compile(r"^\[?(\d+):(\d+(?:\.\d+)?)\]?$")

WINDOW = 8.0
MAX_DRIFT = 2.0
MIN_SIM = 0.55
MIN_SCORE = 0.75
TIE = 0.06


def norm_ja(s):
    """日文规范化：全半角统一 → 去标点空白 → 去长音 → 只留假名/汉字/数字/字母。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = _STRIP.sub("", s)
    s = s.lower()
    s = _KEEP.sub("", s)
    return s


def parse_ts(ts):
    """`[01:23.45]` → 83.45。解析不了返回 None。"""
    m = _TS.match(str(ts).strip())
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


def lcs_ratio(a, b):
    """最长公共子串占**较短一方**的比例。短文本被长文本包含时=1.0（所以要配漂移项）。"""
    if not a or not b:
        return 0.0
    # 手写 DP 而不是 difflib：这条在写盘热路径上（每轨几百条锁），
    # difflib 的 SequenceMatcher 建索引开销大一倍以上。
    la, lb = len(a), len(b)
    if la > lb:
        a, b, la, lb = b, a, lb, la
    prev = [0] * (lb + 1)
    best = 0
    for i in range(1, la + 1):
        cur = [0] * (lb + 1)
        ai = a[i - 1]
        for j in range(1, lb + 1):
            if ai == b[j - 1]:
                v = prev[j - 1] + 1
                cur[j] = v
                if v > best:
                    best = v
        prev = cur
    return best / float(min(la, lb))


def jaccard(a, b, n=2):
    """2-gram 集合的 Jaccard。用来兜住**汉字/假名写法差异**：

    锁里是 `さくらの胸が`，ASR 是 `桜の胸が` —— lcs 只找连续公共子串，
    假名与汉字一换就断（实测只有 0.32，人工看明明是同一句）；
    2-gram 有 `の胸が` 这类交集，能到 0.6 以上。
    """
    if len(a) < n or len(b) < n:
        return lcs_ratio(a, b)
    A = {a[i:i + n] for i in range(len(a) - n + 1)}
    B = {b[i:i + n] for i in range(len(b) - n + 1)}
    return len(A & B) / float(len(A | B))


def sim_ratio(a, b):
    """两种指标取大：lcs 抓长片段、jaccard 容忍写法差异。"""
    return max(lcs_ratio(a, b), jaccard(a, b))


def drift_of(t, start, end):
    """时间漂移：落在 [start, end] 内算 0，否则取到两端的较小距离。"""
    if t is None or start is None:
        return None
    if end is None or end < start:
        end = start
    if start <= t <= end:
        return 0.0
    return min(abs(t - start), abs(t - end))


def build_ja_index(lock_map):
    """{(file, ts): {"zh":..., "ja":...}} → {(file, norm_ja): [ts, ...]}（兼容旧调用）。"""
    idx = {}
    for (fn, ts), v in lock_map.items():
        ja = (v or {}).get("ja") if isinstance(v, dict) else ""
        k = norm_ja(ja)
        if not k:
            continue
        idx.setdefault((fn, k), []).append(ts)
    return idx


def _score(lock_ja, cue_ja, drift, window=WINDOW):
    """返回 (score, sim)。两边都有日文才算文本分，否则 sim=0 只靠时间项。"""
    sim = sim_ratio(lock_ja, cue_ja) if (lock_ja and cue_ja) else 0.0
    return 0.7 * sim + 0.3 * (1.0 - min(drift, window) / window), sim


def assign_cues(lock_map, fn, cues, window=WINDOW, min_sim=MIN_SIM,
                min_score=MIN_SCORE, max_drift=MAX_DRIFT, tie=TIE):
    """把某轨的锁分给 cue。

    cues: `[(idx, start, end, ja)]`（idx 是写盘循环里的 cue 序号）

    返回 `(assign, unmatched, ambiguous)`：
      - `assign`: `{cue_idx: [rec, ...]}`，rec = `{"key","zh","how","sim","drift"}`
        （一个 cue 可能收多条锁 —— 长 cue 切成多个显示行时，人工可能逐行锁过）
      - `unmatched`: 找不到任何 cue 的锁 key 列表
      - `ambiguous`: 分数太接近、**不猜**的锁 key 列表

    `how` 取值：`ts`（时间戳精确）/ `text`（文本锚定）/ `near`（时间唯一，无日文时）
    """
    locks = []
    for (f, ts), v in lock_map.items():
        if f != fn or not isinstance(v, dict):
            continue
        zh = (v.get("zh") or "").strip()
        if not zh:
            continue
        t = parse_ts(ts)
        if t is None:
            continue
        locks.append({"key": (f, ts), "ts": ts, "t": t, "zh": zh,
                      "ja": norm_ja(v.get("ja") or "")})
    locks.sort(key=lambda x: x["t"])

    assign = {}
    unmatched, ambiguous = [], []
    # cue 的已用配额：同一 cue 收几条锁由「窗口内候选」决定，不做硬上限
    for lk in locks:
        cands = []
        for idx, start, end, ja in cues:
            d = drift_of(lk["t"], start, end)
            if d is None or d > window:
                continue
            sc, sim = _score(lk["ja"], norm_ja(ja), d, window)
            # 时间戳精确命中：给一个**严格高于**公式上限的分数（1.1 > 0.7+0.3）。
            # 用 1.0 会和「锁 ts 落在 cue 区间内 + 文本全同」的 1.0 打平，
            # 于是被下面的 tie 判据判成歧义 —— 明明有逐字时间戳这条最强证据却被丢掉
            # （2026-10-31 实测：补完 ja 后 2 条本来 ts 命中的锁反而变歧义）。
            if d == 0.0 and parse_ts_ok(lk["ts"], start):
                sc, sim = 1.1, 1.0
            cands.append({"idx": idx, "drift": d, "score": sc, "sim": sim})
        if not cands:
            unmatched.append(lk["key"])
            continue
        cands.sort(key=lambda c: (-c["score"], c["drift"]))
        best = cands[0]
        second = cands[1] if len(cands) > 1 else None

        # ① 文本锚定：相似度和总分都要过线，且漂移不许超过 MAX_DRIFT
        ok_text = (best["sim"] >= min_sim and best["score"] >= min_score
                   and best["drift"] <= max_drift)
        # ② 时间戳精确命中（score 被抬到 1.1，见上）
        ok_ts = (best["score"] > 1.0)
        # ③ **没有日文就不套用**（2026-10-31 首次链路验证的教训）
        #
        # 曾经有过「窗口内时间唯一 → 认它」的 `near` 兜底，实测立刻出事：
        # RJ01654115 的锁 `[05:51.22]` 文案是「啊 不行 不能随便把任何东西交给别人…」，
        # 而该时刻 ASR 是 `おまんこおかしくなっちゃう`（小穴要变得奇怪了）——
        # 锁的 ts 早已和它的句子错位，纯按时间套用就把一句不相干的中文盖了上去。
        # **错位比丢失更坏**：丢失会被覆盖度门禁报出来，错位看起来一切正常。
        # 所以：没有日文锚点 → 一律不猜，留给 `lock_migrate.py` 补 ja 或人工处理。
        if not (ok_text or ok_ts):
            unmatched.append(lk["key"])
            continue
        # 分不清就不猜：**另一条 cue** 的分数与最佳几乎持平 → 交人工。
        # ⚠️ 判据只看「不同 cue」（`cands` 按 cue 去重，所以 second 必然是另一条），
        # 不能再加「漂移差」条件 —— 两条锁对称落在同一个时间点两侧时漂移相等，
        # 那种情况恰恰是最需要人工的（同一句被人工改了两次，改法还不一样）。
        if second is not None and second["score"] >= best["score"] - tie \
                and second["sim"] >= best["sim"] - tie:
            ambiguous.append(lk["key"])
            continue
        how = "ts" if ok_ts else "text"
        assign.setdefault(best["idx"], []).append({
            "key": lk["key"], "zh": lk["zh"], "how": how,
            "sim": round(best["sim"], 3), "drift": round(best["drift"], 2),
            "t": lk["t"]})
    return assign, unmatched, ambiguous


def parse_ts_ok(ts, start):
    """该锁的时间戳是否**逐字**等于 cue 起点（同一秒同一毫秒）。"""
    return start is not None and abs((parse_ts(ts) or -1e9) - float(start)) < 0.005


def lookup(lock_map, ja_index, old_map, fn, ts, text, ja="",
           window=WINDOW, min_sim=MIN_SIM, min_score=MIN_SCORE):
    """单 cue 特例（`cues=[(0, ts, ts, ja)]`）的 `assign_cues`。

    保留这个签名是为了 `test_lock_apply.py` 与旧调用点；新代码请直接用 `assign_cues`
    —— 它能看到整轨，能靠时间消歧，比单行视图准得多。
    返回 `(最终文本 or None, 命中来源)`，来源 ∈ {ts, text, ambiguous, old, miss}。
    """
    t = parse_ts(ts)
    assign, unmatched, ambig = assign_cues(lock_map, fn, [(0, t, t, ja)],
                                           window=window, min_sim=min_sim,
                                           min_score=min_score)
    if ambig:
        return None, "ambiguous"
    recs = assign.get(0) or []
    if recs:
        return recs[0]["zh"], recs[0]["how"]
    # 兜底：旧中文原样匹配（只在模型这次恰好吐回同一句时有用，历史行为）
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
    ok.append(("时间戳漂了但文本锚定 → 认回",
               lookup(lmap, idx, {}, "a.mp3", "[00:31.00]", "x", "こんばんは")
               == ("丙", "text")))
    # 同句两处：靠时间区分，不该「不猜」
    ok.append(("同句两处 → 用时间消歧",
               lookup(lmap, idx, {}, "a.mp3", "[00:20.30]", "x", "おはよう")
               == ("乙", "text")))
    ok.append(("规范化能吃掉标点/长音",
               norm_ja("おはよう！ー") == norm_ja("おはよう")))
    ok.append(("无关日文不命中",
               lookup(lmap, idx, {}, "a.mp3", "[00:11.00]", "x", "さようなら")
               == (None, "miss")))
    ok.append(("errata 改一个字仍能锚定",
               lookup(lmap, idx, {}, "a.mp3", "[00:31.00]", "x", "こんばんわ")
               == ("丙", "text")))
    ok.append(("漂移超 MAX_DRIFT 不认（防撞相邻重复句）",
               lookup(lmap, idx, {}, "a.mp3", "[00:34.00]", "x", "こんばんは")
               == (None, "miss")))
    # cue 级：两条锁落在同一 cue 的不同显示行上，都要收下
    m2 = {("c.mp3", "[00:10.00]"): {"zh": "甲", "ja": "おはよう"},
          ("c.mp3", "[00:20.00]"): {"zh": "乙", "ja": "おはよう"}}
    a2, un2, am2 = assign_cues(m2, "c.mp3", [(0, 10.0, 21.0, "おはよう")])
    ok.append(("一个 cue 收下窗口内的多条锁",
               len(a2.get(0) or []) == 2 and not un2 and not am2))
    for n, g in ok:
        print("  %s %s" % ("✅" if g else "❌", n))
    n = sum(1 for _, g in ok if g)
    print("%s lock_match selftest %d/%d" % ("✅" if n == len(ok) else "❌", n, len(ok)))
    return 0 if n == len(ok) else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
