"""影子模式（GPT 第十轮 Q4）。

v2 后检测精度到 4/4，但只证明「留下来的告警准」，**不能证明被排除的候选都没问题**。
本模块把「被排除的东西」全记下来，只写日志、不进任何门禁：

五类 shadow（GPT 第十轮 Q4 指定）：
  s1_socchi_dropped   被撤除的 そっち 规则（原来当告警，现在完全静音）
  s2_kimi_modifier    被过滤的 君/お前 名词修饰候选（チン君 / ドM君 / 庭の君…）
  s3_neg_boundary     否定/ってば 边界不确定候选（呼びかけ / 跨句否定 / 词中ない）
  s4_prag_low         medium/low 的 pragmatics hint（只有 high 进了 prompt 与门禁）
  s5_homograph        被排除的同形异义与呼びかけ候选（僕のお前 / 先生ってば…）

用法：
  python shadow_audit.py --out <dir> <扫描产物目录>     # 扫已有成品
每条记录：原始规则命中原因 / 为何被排除 / 源语证据 / 中文输出 / 作品-轨-cue
导出抽样：`--sample 3` 每类抽 3 条 → shadow_sample.md（给用户看的形态）
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "..", "skills", "kikoeru-subtitle", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS))

import pragmatics_hints as PH     # noqa: E402

SHADOW_KINDS = ("s1_socchi_dropped", "s2_kimi_modifier", "s3_neg_boundary",
                "s4_prag_low", "s5_homograph")

KIND_DESC = {
    "s1_socchi_dropped": "被撤除的 そっち 规则",
    "s2_kimi_modifier": "被过滤的 君/お前 名词修饰候选",
    "s3_neg_boundary": "否定/ってば 边界不确定候选",
    "s4_prag_low": "medium/low 归属 hint（未进 prompt）",
    "s5_homograph": "被排除的同形异义 / 呼びかけ",
}


def _record(kind, rj, track, cue, ja, zh, why_hit, why_excluded, evidence):
    return {"kind": kind, "rj": rj, "track": track, "cue": cue,
            "ja": ja, "zh": zh,
            "why_hit": why_hit, "why_excluded": why_excluded,
            "evidence": evidence}


def scan_one(rj, track, cue, ja, zh):
    """对单条 cue 产出 shadow 记录（只记录，不返回任何告警）。"""
    out = []
    if not ja:
        return out

    # ---- s1：そっち（规则已撤除，但把命中全记下来看漏没漏）----
    if "そっち" in ja:
        zh_has = bool(re.search(r"你|妳|对方|您|那边|那里|那个|它", zh or ""))
        out.append(_record(
            "s1_socchi_dropped", rj, track, cue, ja, zh,
            "ja 含 そっち",
            "规则已撤除（v1 实测 8/8 全误报；语料指人仅 37%）",
            {"zh_has_listener_or_deictic": zh_has}))

    # ---- s2：君/お前 名词修饰候选 ----
    for m in PH.LISTENER_STRONG:
        if m not in ja:
            continue
        person = PH._person_hits((m,), ja)
        if person:
            continue                       # 是人称 → 归主规则，不属于 shadow
        idx = ja.find(m)
        prev = ja[idx - 1] if idx > 0 else ""
        out.append(_record(
            "s2_kimi_modifier", rj, track, cue, ja, zh,
            "ja 含 %s 但被判非人称（前一字=%r）" % (m, prev),
            "名词修饰/敬称后缀（チン君 / ドM君 / 庭の君…）→ 不算第二人称",
            {"marker": m, "prev_char": prev,
             "zh_has_ni": bool(re.search(r"你|妳|对方|您", zh or ""))}))
        break

    # ---- s3：否定/ってば 边界不确定 ----
    if "ってば" in ja or "だって" in ja:
        has_neg = bool(PH.NEG_RE.search(ja))
        voc = PH._tteba_is_vocative(ja)
        negb = PH._neg_before_tteba(ja)
        if voc or (has_neg and not negb):
            reason = "呼びかけ（先生ってば）" if voc else "否定不在 ってば 前同一断言里"
            out.append(_record(
                "s3_neg_boundary", rj, track, cue, ja, zh,
                "ja 含 ってば/だって（has_neg=%s, vocative=%s, neg_before=%s）"
                % (has_neg, voc, negb),
                "边界不确定 → %s，主规则不报" % reason,
                {"shizh_has_neg": bool(re.search(r"不|没|别|无|未|非", zh or ""))}))

    # ---- s4：medium/low hint（未进 prompt、未进门禁）----
    h = PH.cue_hint(ja)
    if h and h.get("confidence") in ("medium", "low"):
        out.append(_record(
            "s4_prag_low", rj, track, cue, ja, zh,
            "act=%s conf=%s ev=%s" % (h.get("act"), h.get("confidence"),
                                      h.get("evidence")),
            "medium/low 只进 audit 与 shadow（GPT 第十轮 Q1：low 不注入）",
            {"act": h.get("act"), "confidence": h.get("confidence")}))

    # ---- s5：同形异义（お前 = 身体前面）----
    # 只认「所有格 + お前」这一种真正会错的形式（僕のお前 / 俺のお前）。
    # ⚠️ 不能把 POSITIONAL_AFTER_ONMAE 的「の方/の側」也算进来——
    #    那是 `この方` / `そっちの側` 之类的常见词，会把 s5 灌成上百条噪声（实测 94 条）。
    if PH.OWNED_ONMAE.search(ja):
        combos = [m.group(0) for m in PH.OWNED_ONMAE.finditer(ja)]
        out.append(_record(
            "s5_homograph", rj, track, cue, ja, zh,
            "ja 含 お前 的所有格方位用法（%s）" % "、".join(combos),
            "同形异义（僕のお前 = 我的身体前面），不是第二人称",
            {"combos": combos}))
    return out


def scan_dir(scan_dir):
    """扫 scan_violations 产出的逐作品 json（若有 ja/zh 对齐则用；否则跳过）。"""
    recs = []
    for jf in sorted(glob.glob(os.path.join(scan_dir, "**", "*.json"), recursive=True)):
        try:
            d = json.load(open(jf, encoding="utf-8"))
        except Exception:
            continue
        rows = d.get("rows") if isinstance(d, dict) else None
        if not rows:
            continue
        for r in rows:
            recs += scan_one(r.get("rj", ""), r.get("track", ""),
                             r.get("cue", r.get("t", "")),
                             r.get("ja") or "", r.get("zh") or "")
    return recs


def export_sample(recs, per_kind, path):
    """每类抽 N 条 → markdown（给用户看的形态，不交付全量）。"""
    L = ["# 影子模式抽样（每类 %d 条）" % per_kind, "",
         "> 这不是翻译正确率评估。目标只有一个：**发现哪种排除规则可能过宽**。",
         "> 每条只需回答「这个排除判断对不对」——不对的话说明规则开得太狠。", ""]
    for k in SHADOW_KINDS:
        sub = [r for r in recs if r["kind"] == k][:per_kind]
        if not sub:
            continue
        L.append("## %s · %s（共 %d 条，抽样 %d）"
                 % (k, KIND_DESC[k], len([r for r in recs if r["kind"] == k]), len(sub)))
        L.append("")
        L.append("| # | 作品/轨 | 日文 | 中文 | 排除理由 | 判断 |")
        L.append("|---|---|---|---|---|---|")
        for i, r in enumerate(sub, 1):
            L.append("| %d | %s / %s | %s | %s | %s |  |"
                     % (i, r["rj"], (r["track"] or "")[:14],
                        r["ja"][:38].replace("|", "｜"),
                        (r["zh"] or "")[:30].replace("|", "｜"),
                        r["why_excluded"][:34].replace("|", "｜")))
        L.append("")
    open(path, "w", encoding="utf-8").write("\n".join(L))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "shadow"))
    ap.add_argument("--sample", type=int, default=3)
    ap.add_argument("src", nargs="?", default="", help="扫描产物目录（含 rows 的 json）")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    recs = scan_dir(a.src) if a.src else []
    if not recs:
        print("⚠️ 没扫到记录（%s）；可用 selftest 验证逻辑" % (a.src or "未给路径"))
        recs = _demo_records()

    path = os.path.join(a.out, "shadow_audit.json")
    json.dump({"total": len(recs),
               "by_kind": {k: sum(1 for r in recs if r["kind"] == k)
                           for k in SHADOW_KINDS},
               "records": recs}, open(path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("影子记录 %d 条 →" % len(recs), path)
    for k in SHADOW_KINDS:
        print("  %-22s %d" % (k, sum(1 for r in recs if r["kind"] == k)))
    md = os.path.join(a.out, "shadow_sample.md")
    export_sample(recs, a.sample, md)
    print("抽样（每类 %d 条）→" % a.sample, md)
    return 0


def _demo_records():
    """无扫描产物时的演示记录（也用于自检）。"""
    cases = [
        ("闇の組織", "T1", "そっちがローター", "那个是跳蛋"),
        ("RJ1", "T1", "僕のチン君が震えて", "我的肉棒在颤"),
        ("RJ1", "T2", "先生ってば、聞いてる?", "老师，你在听吗？"),
        ("RJ1", "T3", "うん、やっぱり嫌いじゃないでしょ？", "嗯，果然还是喜欢的吧？"),
        ("RJ1", "T4", "僕のお前に", "在我前面"),
    ]
    out = []
    for rj, tr, ja, zh in cases:
        out += scan_one(rj, tr, "c001", ja, zh)
    return out


def _selftest():
    recs = _demo_records()
    kinds = {r["kind"] for r in recs}
    want = {"s1_socchi_dropped", "s2_kimi_modifier", "s3_neg_boundary",
            "s5_homograph"}
    bad = 0
    if not want <= kinds:
        bad += 1
        print("  ✗ 缺类别：%s" % (want - kinds))
    # 每条必须有全部六个字段（GPT 要求：命中原因/排除原因/源语证据/中文/定位）
    for r in recs:
        for f in ("why_hit", "why_excluded", "evidence", "zh", "rj", "cue"):
            if f not in r:
                bad += 1
                print("  ✗ 缺字段 %s in %s" % (f, r.get("kind")))
                break
    # s3 必须能覆盖「否定不在ってば前」（否定+ってば 但跨句）与呼びかけ两种
    s3 = [r for r in recs if r["kind"] == "s3_neg_boundary"]
    if not s3:
        bad += 1
        print("  ✗ s3 无记录")
    print("shadow_audit 自检：%d 条记录 / %s" %
          (len(recs), "全过" if not bad else "有 %d 处失败" % bad))
    return bad == 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        sys.exit(0 if _selftest() else 1)
    sys.exit(main())
