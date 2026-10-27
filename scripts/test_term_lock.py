# -*- coding: utf-8 -*-
"""term_lock.py 的边界测试（不依赖 LLM、不依赖真实语料）。

跑法：python test_term_lock.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import term_lock as T  # noqa: E402

FAIL = []


def ck(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAIL.append(msg)


def test_sensei_teacher():
    print("\n[sensei] 师生语境")
    g = T.LOCK_TERMS["sensei"]
    texts = ["老师 您好", "先生 请多指教", "老师 我明白了", "先生 再见"]
    cnt = T.count_votes(texts, g)
    ck(T.pick_anchor(cnt, g) == "老师", "票数平局时优先「老师」(实际 %s)" % cnt)
    ck(T.rewrite_text("先生 请多指教", g, "老师")[0] == "老师 请多指教", "先生→老师")
    ck(T.rewrite_text("老师 您好", g, "老师")[1] == 0, "已是锚点则不改")
    ja = ["生徒が先生を呼んだ", "授業で先生が教える", "学校の先生"]
    ck(T.detect_sensei_context(ja)[0] == "老师", "师生共现 → 老师")


def test_sensei_doctor():
    print("\n[sensei] 医生语境")
    g = T.LOCK_TERMS["sensei"]
    ja = ["先生、診察をお願いします", "病院で先生に注射してもらった", "患者の症状を診る"]
    ck(T.detect_sensei_context(ja)[0] == "医生", "医疗共现 → 医生")
    ck(T.rewrite_text("先生 请给我开药", g, "医生")[0] == "医生 请给我开药", "先生→医生")
    ck(T.rewrite_text("医生 谢谢", g, "医生")[0] == "医生 谢谢", "锚点是医生则不动")


def test_sensei_lawyer():
    print("\n[sensei] 律师语境")
    ja = ["先生、裁判はどうなりますか", "弁護士の先生に相談した", "法廷で訴える"]
    ck(T.detect_sensei_context(ja)[0] == "律师", "法律共现 → 律师")


def test_sensei_noncovative_protected():
    print("\n[sensei] 「那位先生」保护（男性义，不是当面称呼）")
    g = T.LOCK_TERMS["sensei"]
    for t in ["那位先生是来探病的", "这位先生看起来很生气", "一位先生走了进来"]:
        new, n = T.rewrite_text(t, g, "老师")
        ck(n == 0 and new == t, "保留：%s" % t)
    # 但"先生"独立开头（当面称呼）要改
    ck(T.rewrite_text("先生 您来了", g, "老师")[1] == 1, "当面称呼「先生」要改")
    ck(T.rewrite_text("老师", g, "老师")[1] == 0, "无需改")


def test_ja_gate():
    print("\n[日文侧校验] when 不命中则不改")
    g = T.LOCK_TERMS["sensei"]
    # 日文没有「先生」→ 不该动「老师」（这是真·学校老师）
    ck(T.rewrite_text("老师 我的症状", g, "医生", ja_text="教師が来た")[1] == 0,
       "日文无「先生」→ 不动")
    ck(T.rewrite_text("先生 我的症状", g, "医生", ja_text="先生が来た")[1] == 1,
       "日文有「先生」→ 改")


def test_mutual_exclusion():
    print("\n[互斥] 跨组不抢同一写法")
    # danna 的变体里不应含「先生」（否则会抢 sensei 的票）
    ck("先生" not in T.LOCK_TERMS["danna"]["variants"], "danna 变体不含「先生」")
    ck("老师" not in T.LOCK_TERMS["danna"]["variants"], "danna 变体不含「老师」")
    # sensei 的变体池必须覆盖所有语境锚点，否则覆写时收不拢
    for v in ["老师", "先生", "医生", "律师", "议员"]:
        ck(v in T.LOCK_TERMS["sensei"]["variants"], "sensei 变体池含「%s」" % v)


def test_conflict_detect():
    print("\n[冲突检测]")
    ck(T.anchors_conflict({"a": "主人", "b": "老公"}) == [], "无冲突")
    bad = T.anchors_conflict({"a": "主人", "b": "主人"})
    ck(len(bad) == 1 and bad[0][2] == "主人", "检出同锚冲突")


def test_kyu():
    print("\n[kyu] 「君」→「你」")
    g = T.LOCK_TERMS["kyu"]
    ck(T.rewrite_text("樱木君 你好", g, "你")[0] == "樱木你 你好"
       or "君" not in T.rewrite_text("君 你好", g, "你")[0], "君→你")
    ck(T.rewrite_text("你 你好", g, "你")[1] == 0, "已是「你」则不改")


def test_ts():
    print("\n[时间戳]")
    ck(T.ts_to_sec("[01:27.90]") == 87.9, "[01:27.90] → 87.9")
    ck(T.ts_to_sec("[00:03.26]") == 3.26, "[00:03.26] → 3.26")
    ck(T.ts_to_sec("bad") is None, "非法时间戳 → None")


def test_spk_bucket():
    print("\n[C1-3] 说话人分桶锁称谓")
    # spk_at：±2s 窗内按重叠时长取主导；无 spk → None
    segs = [(0.0, 10.0, "こんにちは", "SPK_A"), (10.5, 20.0, "旦那様", "SPK_B"),
            (30.0, 40.0, "セリフ", "")]
    ck(T.spk_at(segs, 5.0) == "SPK_A", "落在 A 段 → SPK_A")
    ck(T.spk_at(segs, 12.0) == "SPK_B", "落在 B 段 → SPK_B")
    ck(T.spk_at(segs, 25.0) is None, "窗口内无段 → None")
    ck(T.spk_at(segs, 35.0) is None, "只有无 spk 的段 → None")
    ck(T.spk_at([], 5.0) is None, "空索引 → None")
    # bucket_anchors_for：单桶 → {}（回归安全：旧行为）
    rows = {"t.lrc": [("[00:01.00]", "主人"), ("[00:02.00]", "主人")]}
    spk_rows = {"t.lrc": ["SPK_A", "SPK_A"]}
    ck(T.bucket_anchors_for(rows, spk_rows, ["goshujin"], {"goshujin": "主人"}) == {},
       "单桶 → 不分桶（行为同旧版）")
    # 双桶分歧：A 用「主人」、B 用「老爷」→ B 桶记 老爷，A 桶同作品锚不记
    rows = {"t.lrc": [("[00:01.00]", "主人"), ("[00:02.00]", "主人"),
                      ("[00:03.00]", "老爷"), ("[00:04.00]", "老爷")]}
    spk_rows = {"t.lrc": ["SPK_A", "SPK_A", "SPK_B", "SPK_B"]}
    ba = T.bucket_anchors_for(rows, spk_rows, ["goshujin"], {"goshujin": "主人"})
    ck(ba == {"goshujin": {"SPK_B": "老爷"}}, "双桶分歧 → 只有 B 桶记「老爷」(%s)" % ba)
    # 桶内票数 <2 → 不记（没统计意义）
    rows2 = {"t.lrc": [("[00:01.00]", "主人"), ("[00:03.00]", "老爷")]}
    spk_rows2 = {"t.lrc": ["SPK_A", "SPK_B"]}
    ba2 = T.bucket_anchors_for(rows2, spk_rows2, ["goshujin"], {"goshujin": "主人"})
    ck(ba2 == {}, "桶内 1 票 → 不锚（%s）" % ba2)
    # 跨组互斥：老爷 属于 goshujin 变体池，danna 组的 count 不受影响
    rows3 = {"t.lrc": [("[00:01.00]", "老公"), ("[00:03.00]", "老爷"),
                       ("[00:05.00]", "老爷")]}
    spk_rows3 = {"t.lrc": ["SPK_A", "SPK_B", "SPK_B"]}
    ba3 = T.bucket_anchors_for(rows3, spk_rows3, ["danna", "goshujin"],
                               {"danna": "老公", "goshujin": "主人"})
    ck(ba3 == {"goshujin": {"SPK_B": "老爷"}}, "danna 组单桶不记，goshujin 正常记（%s）" % ba3)


if __name__ == "__main__":
    test_sensei_teacher()
    test_sensei_doctor()
    test_sensei_lawyer()
    test_sensei_noncovative_protected()
    test_ja_gate()
    test_mutual_exclusion()
    test_conflict_detect()
    test_kyu()
    test_ts()
    test_spk_bucket()
    print("\n" + ("=" * 40))
    if FAIL:
        print("❌ %d 项失败：" % len(FAIL))
        for m in FAIL:
            print("   -", m)
        sys.exit(1)
    print("✅ 全部通过")
