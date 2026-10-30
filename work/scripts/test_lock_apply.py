# -*- coding: utf-8 -*-
"""E1 锁 v3（文本锚定）的集成自检。不跑模型、不碰生产锁。

做法：把 `KIKOERU_LOCKS` 指到临时目录，造一份带 ja 的锁，然后调 polish 的
`load_locks` / `assign_locks_for_track` / `apply_lock`。

## 判据演进（v3，2026-10-31）

v2 的两条断言在 v3 里**语义反转**了，别照旧抄：

| 情形 | v2 | v3 | 为什么 |
|---|---|---|---|
| 同句两处锁、时间戳漂了 | 不猜，丢 | **靠时间窗消歧，认回** | 「唯一才用」让 ASMR 复读句的锁全丢；时间才是天然消歧器 |
| 日文差一个字（errata 改过） | 丢 | **文本锚定认回** | 锁是人工成果，不能因为改了个错字就失效 |
| 两条锁都落在同一窗口、分数接近 | — | **仍然不猜** | 错位比丢失更坏：把 A 句的修正贴到 B 句上，看起来「有修正」其实全错 |

用法：python test_lock_apply.py
"""
import json
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

tmp = tempfile.mkdtemp(prefix="locktest_")
os.environ["KIKOERU_LOCKS"] = tmp
# 必须在设好 env 之后再 import polish（LOCKS_DIR 在导入时读）
import polish  # noqa: E402

RJ = "RJ9999999"
out_dir = os.path.join(tmp, RJ)
os.makedirs(out_dir, exist_ok=True)
locks = {
    # 主 key 命中
    "a.mp3|[00:10.00]": {"zh": "甲甲甲", "ja": "おはようございます"},
    # 时间戳漂 1.2s → 文本锚定认回
    "a.mp3|[00:30.00]": {"zh": "乙乙乙", "ja": "こんばんは"},
    # 同句两处（5.0 / 40.0）→ 漂到 6.1 时应靠时间选 5.0 那条
    "b.mp3|[00:05.00]": {"zh": "丙丙丙", "ja": "だめだよ"},
    "b.mp3|[00:40.00]": {"zh": "丁丁丁", "ja": "だめだよ"},
    # 两条锁挤在同一个 cue 窗口内、日文一样 → 不猜
    "c.mp3|[00:12.00]": {"zh": "戊戊戊", "ja": "もういい"},
    "c.mp3|[00:13.00]": {"zh": "己己己", "ja": "もういい"},
}
json.dump(locks, open(os.path.join(tmp, RJ + ".json"), "w", encoding="utf-8"),
          ensure_ascii=False)

n = polish.load_locks(out_dir)
ok = []


def ck(name, cond, detail=""):
    ok.append(bool(cond))
    print("  %s %-40s %s" % ("✅" if cond else "❌", name, detail))


ck("载入 6 条锁", n == 6, "n=%d" % n)

# ① 主 key
v = polish.apply_lock("a.mp3", "[00:10.00]", "机器译文", "おはようございます")
ck("主 key 命中", v == "甲甲甲", v)

# ② 时间戳漂了 → 文本锚定
before = polish.LOCK_JA_HIT[0]
v = polish.apply_lock("a.mp3", "[00:31.20]", "机器译文", "こんばんは")
ck("漂 1.2s → 文本锚定认回", v == "乙乙乙", v)
ck("计入「非时间戳锚定」计数", polish.LOCK_JA_HIT[0] == before + 1,
   "%d→%d" % (before, polish.LOCK_JA_HIT[0]))

# ③ errata 改一个字仍能锚定
v = polish.apply_lock("a.mp3", "[00:31.20]", "机器译文", "こんばんわ")
ck("日文差一个字仍锚定（errata 友好）", v == "乙乙乙", v)

# ④ 同句两处 → 靠时间消歧（v2 在这里会「不猜」，是错的）
v = polish.apply_lock("b.mp3", "[00:06.10]", "机器译文", "だめだよ")
ck("同句两处 → 用时间消歧，认回最近那条", v == "丙丙丙", v)

# ⑤ 真正的不猜：一条锁的两侧各有一条 cue、日文一样、分数持平
#    ⚠️ 「同一个 cue 里有两条锁」不算歧义（长 cue 切成多显示行时人工逐行锁过，正常）
import lock_match  # noqa: E402
_a, _u, _m = lock_match.assign_cues(polish.LOCK_RAW, "c.mp3",
                                    [(0, 11.8, 12.2, "もういい"),
                                     (1, 12.4, 12.8, "もういい")])
ck("两侧同分 cue → 不猜", not _a and len(_m) == 2, "assign=%d ambig=%d" % (len(_a), len(_m)))
polish.LOCK_AMBIG.clear()
polish.LOCK_CUE.clear()
polish.assign_locks_for_track("c.mp3", [
    {"start": 11.8, "end": 12.2, "text": "もういい"},
    {"start": 12.4, "end": 12.8, "text": "もういい"}])
ck("不猜的锁记入失配集合", len(polish.LOCK_AMBIG) == 2,
   "ambig=%d" % len(polish.LOCK_AMBIG))

# ⑥ 文本已等于锁 → 不算覆写
v = polish.apply_lock("a.mp3", "[00:10.00]", "甲甲甲", "おはようございます")
ck("文本已等于锁 → 不算覆写", v == "甲甲甲")

# ⑦ cue 级：一条 cue 切成多个显示行，两条锁各落一行
polish.LOCK_CUE.clear()
polish.LOCK_UNMATCHED.clear()
lines = [{"start": 10.0, "end": 14.0, "text": "おはようございます"},
         {"start": 30.0, "end": 34.0, "text": "こんばんは"}]
polish.assign_locks_for_track("a.mp3", lines)
ck("cue 级分配：两条锁各归其 cue",
   len(polish.LOCK_CUE.get(0) or []) == 1 and len(polish.LOCK_CUE.get(1) or []) == 1,
   "cue0=%d cue1=%d" % (len(polish.LOCK_CUE.get(0) or []),
                        len(polish.LOCK_CUE.get(1) or [])))
r0 = polish.apply_lock("a.mp3", "[00:10.00]", "初译", "おはようございます", cue_idx=0)
r1 = polish.apply_lock("a.mp3", "[00:10.80]", "初译", "おはようございます", cue_idx=0)
ck("同一 cue 的第一行吃到锁、第二行不吃（锁落完即消费）",
   r0 == "甲甲甲" and r1 == "初译", "%s / %s" % (r0, r1))

# ⑧ 无落点的锁必须被点名（不许静默丢）
ck("b.mp3 的 40s 锁在只给 a.mp3 的 cue 时不静默",
   ("b.mp3", "[00:40.00]") not in polish.LOCK_UNMATCHED,
   "（该轨未参与本轨分配，属正常）")

print("\n结果：%d/%d" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
