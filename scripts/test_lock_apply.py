# -*- coding: utf-8 -*-
"""E1 锁双 key 的集成自检（不跑模型、不碰生产锁）。

做法：把 `KIKOERU_LOCKS` 指到临时目录，造一份带 ja 的锁，然后调 polish 的
`load_locks` / `apply_lock`，验证三种情形：
  1. 时间戳命中 → 用锁；
  2. 时间戳漂了但日文唯一 → 按日文认回（并计入 LOCK_JA_HIT）；
  3. 同句两处锁、时间戳漂了 → **不猜**（记 LOCK_AMBIG，保持原文）。

用法：python test_lock_apply.py
"""
import json
import os
import sys
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
    # 日文唯一 → 时间戳漂了也能认回
    "a.mp3|[00:30.00]": {"zh": "乙乙乙", "ja": "こんばんは"},
    # 同句两处 → 漂了就不猜
    "b.mp3|[00:05.00]": {"zh": "丙丙丙", "ja": "だめだよ"},
    "b.mp3|[00:40.00]": {"zh": "丁丁丁", "ja": "だめだよ"},
}
json.dump(locks, open(os.path.join(tmp, RJ + ".json"), "w", encoding="utf-8"),
          ensure_ascii=False)

n = polish.load_locks(out_dir)
ok = []


def ck(name, cond, detail=""):
    ok.append(bool(cond))
    print("  %s %-40s %s" % ("✅" if cond else "❌", name, detail))


ck("载入 4 条锁", n == 4, "n=%d" % n)
v = polish.apply_lock("a.mp3", "[00:10.00]", "机器译文", "おはようございます")
ck("主 key 命中", v == "甲甲甲", v)

before = polish.LOCK_JA_HIT[0]
v = polish.apply_lock("a.mp3", "[00:31.20]", "机器译文", "こんばんは")
ck("时间戳漂移 → 按日文认回", v == "乙乙乙", v)
ck("计入 ja 命中计数", polish.LOCK_JA_HIT[0] == before + 1)

v = polish.apply_lock("b.mp3", "[00:06.10]", "机器译文", "だめだよ")
ck("同句多锁 → 不猜（保留原文）", v == "机器译文", v)
ck("记入失配集合", ("b.mp3", "[00:06.10]") in polish.LOCK_AMBIG)

v = polish.apply_lock("a.mp3", "[00:10.00]", "甲甲甲", "おはようございます")
ck("文本已等于锁 → 不算覆写", v == "甲甲甲")

print("\n结果：%d/%d" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
