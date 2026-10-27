# -*- coding: utf-8 -*-
r"""errata 政策自检工具（2026-10-26，T7：把凌晨的手工审计 `errata_promote_check.py` 产品化）。

为什么
------
`data/style/asr_errata.json`（auto/guard/review 三档）只看「命中数」不够定档：
  · `行動→公道` 11 段里 10 段是正确用法（自由行動…）→ 只能 review；
  · `感→噛ん` 命中 5 段，但 `感` 全库 3155 次 → 错形是常用词，auto 会毁掉大量正确句；
  · `咲き走り→先走り` 9 段 / 6 部 / 错形不是词 → auto 安全。
这次审计原来是一次性脚本、没有门禁没有回归；本工具把它固化成可复跑的审计 + `--test` 门禁。

数据口径（必须遵守，错一个就与手工基线对不上）
----
- 语料根：`draft/<作品>/asr/*.json` + `draft/learning/<作品>/asr/*.json`（只扫 asr/，不扫多版本目录）。
- 每段取 `text`；**按 (作品, 轨, 起始秒) 去重**——语料是多轮 ASR 的产物，同一时间窗会留多版，
  不去重命中数灌水约 18%。
- `n_hits` = 去重后含错形的段数；`wrong_cf` = 去重段内错形的**出现次数**（一段出现两次计 2，
  所以 cf ≥ hits：写真 169 段 / 172 次）；`n_works` = 命中覆盖作品数。
- `anchor` = 正确写法（fix）在**同一作品**出现过（同作品锚点，多版本互证的语料版）。
- 目录名含 `[]` `♪` `～`：glob 的目录部分必须 `glob.escape()`（这个坑踩过三次）。
- 全库 ~17.6 万段，一次遍历 + 字典统计（不对每条 errata 单独扫一遍）。

verdict 规则（T7b 2026-10-26 用户裁决后）：
  auto   → n_hits==0：CONSUMED（语料已被旧修正消化，既不算安全也不算不安全，
           **不计入 AUTO_UNSAFE**——処世/男子近世/御社/気感坊 一类）
           否则 legit==0（已核实全库出现都是误识，如 写生）→ wrong_cf>=30 豁免；
           否则 wrong_cf >= 30 或 n_works < 2 或 not anchor：AUTO_UNSAFE
  guard  → n_hits < 3 或 n_works < 2，**且 guard 是真正则**（含元字符）：
           GUARD_THIN（样本太少，泛化正则不可信）。纯字面 guard（re.escape 后
           不变，如 処世 的「お処世」）不会过度泛化 → 不判 thin。
  review → n_hits >= 3 且 n_works >= 2 且 anchor 且 wrong_cf < 30：REVIEW_PROMOTABLE
  其余 → OK

用法：
  python errata_audit.py                        # 36 条逐条一行审计
  python errata_audit.py --json                 # 同上，并落盘 JSONL（每条一行 dict）
  python errata_audit.py --guard-samples 悪夢 20 # 某错形的全部命中段原文 + guard 命中标记
  python errata_audit.py --test                 # 门禁（退出码 0 = 全过）
"""
import os
import re
import sys
import glob
import json
import time
import argparse

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import styleloc  # noqa: E402
ERRATA_PATH = styleloc.table("asr_errata.json")
# 16 部成品（draft 根）+ 106 部学习语料（draft/learning）。
CORPUS_ROOTS = [os.path.join(ROOT, "data", "draft"),
                os.path.join(ROOT, "data", "draft", "learning")]


def load_errata(path=ERRATA_PATH):
    """读 errata 表。缺省 mode 按 auto 算——与运行时消费方
    `asr_fix_scan.auto_apply_errata()` 的 `e.get("mode", "auto")` 同口径，
    审计结果才代表真实运行时行为。"""
    if not os.path.exists(path):
        print("错误：errata 表不存在：%s" % path, file=sys.stderr)
        sys.exit(2)
    data = json.load(open(path, encoding="utf-8"))
    entries = [dict(e, mode=e.get("mode") or "auto")
               for e in (data.get("entries") or []) if e.get("ja") and e.get("fix")]
    if not entries:
        print("错误：errata 表没有可用条目", file=sys.stderr)
        sys.exit(2)
    return entries


def iter_segments():
    """一次遍历语料，yield (work, track, start, text)。

    只扫 `<作品>/asr/*.json`；track 用 json 的 `source` 字段（轨名稳定的唯一来源，
    文件名在不同版本目录里有带/不带 .mp3 两种写法）。目录部分 glob.escape。
    """
    for root in CORPUS_ROOTS:
        if not os.path.isdir(root):
            continue
        for wdir in sorted(glob.glob(glob.escape(root) + os.sep + "*")):
            if not os.path.isdir(wdir):
                continue
            work = os.path.basename(wdir)
            pattern = glob.escape(os.path.join(wdir, "asr")) + os.sep + "*.json"
            for jf in sorted(glob.glob(pattern)):
                try:
                    d = json.load(open(jf, encoding="utf-8"))
                except Exception:
                    continue
                track = d.get("source") or os.path.basename(jf)[:-5]
                for s in d.get("segments") or []:
                    t = s.get("text") or ""
                    if t:
                        yield work, track, float(s.get("start") or 0.0), t


def gather(queries, max_samples=400, works_seen=None):
    """一遍语料，统计一组查询串。返回 {q: {hits, works, cf, samples}}。

    · 先按 (work, track, round(start,2)) 去重（多轮 ASR 同窗多版会灌水）；
    · cf = 去重段内出现次数（写真 169 段 / 172 次的口径）；
    · samples 留前 max_samples 条（--guard-samples 逐条看原文用，截断防炸内存）；
    · works_seen：传一个 set 进来，会被填成「语料里出现过的作品名」，
      用来判断语料覆盖是否够跑数值断言（--test 用）。
    """
    stats = {q: {"hits": set(), "works": set(), "cf": 0, "samples": []} for q in queries}
    seen = set()
    for work, track, start, text in iter_segments():
        if works_seen is not None:
            works_seen.add(work)
        key = (work, track, round(start, 2))
        if key in seen:
            continue
        seen.add(key)
        for q in queries:
            c = text.count(q)
            if not c:
                continue
            st = stats[q]
            st["hits"].add(key)
            st["works"].add(work)
            st["cf"] += c
            if len(st["samples"]) < max_samples:
                st["samples"].append((work, track, start, text))
    return stats, len(seen)


def audit_rows(entries, stats):
    """逐条目产出审计行（verdict 规则见模块 docstring，T7b 修订）。"""
    rows = []
    for e in entries:
        ja, fix, mode = e["ja"], e["fix"], e["mode"]
        st = stats.get(ja) or {"hits": set(), "works": set(), "cf": 0, "samples": []}
        n_hits, n_works, cf = len(st["hits"]), len(st["works"]), st["cf"]
        # anchor：正确写法在同一作品出现过（fix 也进 gather 的查询集才能算）
        fst = stats.get(fix) or {"works": set()}
        anchor = bool(st["works"] & fst["works"])
        # T7b ①：auto 且 n_hits==0 → CONSUMED（已被旧修正消化，不算不安全，
        # 也不计入 AUTO_UNSAFE——那批 AUTO_UNSAFE 全是这类，把真风险淹没了）。
        if mode == "auto" and n_hits == 0:
            verdict = "CONSUMED"
        # T7b ②：legit==0（已核实全库出现**都是**误识，如 写生）→ cf 大不构成
        # 威胁，wrong_cf>=30 检查豁免；works/anchor 仍要过（单作品/无锚点照旧危险）。
        elif mode == "auto" and e.get("legit") == 0:
            verdict = "AUTO_UNSAFE" if (n_works < 2 or not anchor) else "OK"
        elif mode == "auto" and (cf >= 30 or n_works < 2 or not anchor):
            verdict = "AUTO_UNSAFE"
        # T7b ③：字面 guard（re.escape 后不变，如 処世 的「お処世」）只会匹配
        # 那一个语境串，不会过度泛化 → 样本少不影响可信度，不判 GUARD_THIN；
        # 带 | (?! (?<! 等元字符的泛化正则仍按 n_hits<3 判 thin（人工 一类）。
        elif (mode == "guard" and (n_hits < 3 or n_works < 2)
              and re.escape(e.get("guard") or "") != (e.get("guard") or "")):
            verdict = "GUARD_THIN"
        elif (mode == "review" and n_hits >= 3 and n_works >= 2
              and anchor and cf < 30):
            verdict = "REVIEW_PROMOTABLE"
        else:
            verdict = "OK"
        rows.append({"ja": ja, "fix": fix, "mode": mode, "n_hits": n_hits,
                     "n_works": n_works, "wrong_cf": cf, "anchor": anchor,
                     "legit0": e.get("legit") == 0, "verdict": verdict})
    return rows


# ---------------------------------------------------------------- --guard-samples
def cmd_guard_samples(ja, n):
    entries = load_errata()
    ent = next((e for e in entries if e["ja"] == ja), None)
    stats, _ = gather({ja})
    st = stats[ja]
    print("%s 命中 %d 段（%d 部）" % (ja, len(st["hits"]), len(st["works"])))
    guard = ent.get("guard") if ent and ent.get("mode") == "guard" else None
    if ent is None:
        tag0 = "[表内无此条]"
    elif guard:
        tag0 = None                      # 逐段动态判
    else:
        tag0 = "[%s]" % ent["mode"]
    for work, track, start, text in sorted(st["samples"])[:n]:
        if tag0:
            tag = tag0
        else:
            tag = "[guard 命中]" if re.search(guard, text) else "[guard 未命中]"
        show = ("…" + text[:75]) if len(text) > 76 else text
        print("  %-26s %-18s %8.1f  %-12s %s" % (work[:26], track[:18], start, tag, show))
    if len(st["samples"]) > n:
        print("  …（另有 %d 段未显示）" % (len(st["samples"]) - n))


# ---------------------------------------------------------------- --test
FULL_WORKS = 16          # 基线语料的作品数（16 部成品）
FULL_SEGS = 20000        # 基线去重段数（实际约 22800）


def structure_checks(entries):
    """**与语料无关**的结构断言：任何 clone 上都应该过。

    这些查的是「表本身合不合法」——mode 取值、ctx 正则可编译、负向断言已退役、
    没有重复键。语料不全也照样能查。
    """
    A = []
    by_ja = {}
    for e in entries:
        by_ja.setdefault(e["ja"], []).append(e)

    def chk(name, cond):
        A.append((name, bool(cond)))

    chk("每条 mode ∈ {auto,guard,review}",
        all(e.get("mode") in ("auto", "guard", "review") for e in entries))
    chk("没有重复的 ja（同键两条会让运行时行为不确定）",
        all(len(v) == 1 for v in by_ja.values()))
    chk("没有 ja == fix 的空条目", all(e["ja"] != e["fix"] for e in entries))

    bad_re = []
    for e in entries:
        for c in (e.get("ctx") or ([e["guard"]] if e.get("guard") else [])):
            try:
                re.compile(c)
            except re.error:
                bad_re.append((e["ja"], c))
    chk("ctx 正则全部可编译", not bad_re)
    chk("guard 条目都有 ctx 白名单（负向断言已退役）",
        all(e.get("ctx") or e.get("guard") for e in entries if e.get("mode") == "guard"))
    chk("没有负向断言 (?!…)",
        not any("(?!" in (c or "") for e in entries for c in (e.get("ctx") or [])))
    chk("works 作用域是字符串列表",
        all(isinstance(e["works"], list) and all(isinstance(x, str) for x in e["works"])
            for e in entries if "works" in e))
    return A


def cmd_test():
    """门禁：期望值来自 2026-10-26 手工基线 + T7b 用户裁决；对不上=口径错了，别改期望值。

    ⚠️ 数值断言（n_hits / n_works / wrong_cf）是**对着完整语料**数出来的
    （16 部成品 + 106 部学习语料）。语料不全时它们必然对不上——那不是表坏了。
    所以按覆盖度分两级：
      · 结构断言：永远跑；
      · 语料断言：只有语料覆盖达到基线才跑，否则 SKIP 并说明差多少。
    """
    entries = load_errata()
    by_ja = {e["ja"]: e for e in entries}
    queries = {e["ja"] for e in entries} | {e["fix"] for e in entries}
    ws = set()
    stats, n_seg = gather(queries, works_seen=ws)

    A = structure_checks(entries)
    full = len(ws) >= FULL_WORKS and n_seg >= FULL_SEGS
    if not full:
        n_pass = sum(1 for _, ok in A if ok)
        for name, ok in A:
            if not ok:
                print("  ❌ %s" % name)
        print("%d/%d 通过（结构断言）" % (n_pass, len(A)))
        print("⚠️ SKIP 语料断言：当前语料 %d 部 / %d 段，基线是 %d 部 / ≥%d 段。"
              % (len(ws), n_seg, FULL_WORKS, FULL_SEGS))
        print("   数值期望（n_hits / n_works / wrong_cf）是对着完整语料数出来的，")
        print("   语料不全时对不上是正常的，不代表错字表坏了。")
        sys.exit(0 if n_pass == len(A) else 1)

    def g(ja):
        st = stats.get(ja) or {"hits": set(), "works": set(), "cf": 0, "samples": []}
        return len(st["hits"]), len(st["works"]), st["cf"]

    rows = {r["ja"]: r for r in audit_rows(entries, stats)}

    def chk(name, cond):
        A.append((name, bool(cond)))

    # ---- 手工基线期望值（6 条：n_hits / n_works / wrong_cf，T7 全数对上）----
    for ja, eh, ew, ec in [("咲き走り", 9, 6, 9), ("古ぼっき", 10, 7, 10),
                           ("悪夢", 12, 8, 12), ("否認", 9, 7, 9),
                           ("行動", 11, 9, 11), ("写真", 169, 54, 172)]:
        h, w, c = g(ja)
        chk("%s n_hits=%d(期望%d)" % (ja, h, eh), h == eh)
        chk("%s n_works=%d(期望%d)" % (ja, w, ew), w == ew)
        chk("%s wrong_cf=%d(期望%d)" % (ja, c, ec), c == ec)

    # ---- T7b 用户裁决的 verdict 期望（2026-10-26 改档/加 legit 后）----
    for ja, want_mode in [("不敬", "review"), ("還元", "review"),
                          ("処世", "guard"), ("行動", "review")]:
        r = rows.get(ja)
        chk("%s %s(%s)" % (ja, "OK", want_mode),
            r and r["verdict"] == "OK" and r["mode"] == want_mode)
    r = rows.get("写生")
    chk("写生 OK(legit=0，cf 194 豁免)", r and r["verdict"] == "OK"
        and by_ja["写生"].get("legit") == 0 and r["legit0"])
    for ja in ("咲き走り", "古ぼっき"):
        r = rows.get(ja)
        chk("%s OK(auto 安全)" % ja, r and r["verdict"] == "OK" and r["mode"] == "auto")
    # CONSUMED：hits==0 的 auto 条目不再算 AUTO_UNSAFE（男子近世 是这类代表）
    r = rows.get("男子近世")
    chk("男子近世 hits==0 → CONSUMED", r and r["n_hits"] == 0 and r["verdict"] == "CONSUMED")
    chk("AUTO_UNSAFE 里没有 hits==0 的条目",
        all(r2["n_hits"] > 0 for r2 in rows.values() if r2["verdict"] == "AUTO_UNSAFE"))
    # 悪夢 guard 样本足仍 OK
    r = rows.get("悪夢")
    chk("悪夢 verdict=OK（guard 样本足）", r and r["verdict"] == "OK" and r["mode"] == "guard")

    # ---- guard 白名单的正反例（2026-10-26 R133：guard 从 `guard` 负向断言改成 `ctx` 正向白名单）
    for ja, hit_txt, miss_txt in [
            ("悪夢", "生悪夢を耳元で聞く", "悪夢を見た"),
            ("否認", "否認もせずに", "ちゃんと否認するんだよ"),
            ("ポッキー", "ポッキーおちんちん", "チンポッキーどんどん")]:
        e = by_ja.get(ja)
        ctx = (e or {}).get("ctx") or ([e["guard"]] if e and e.get("guard") else [])
        chk("%s 有 guard 条目（ctx 白名单）" % ja,
            e and e.get("mode") == "guard" and bool(ctx))
        if ctx:
            chk("%s ctx 正例命中" % ja, any(re.search(c, hit_txt) for c in ctx))
            chk("%s ctx 反例放过" % ja, not any(re.search(c, miss_txt) for c in ctx))

    n_pass = sum(1 for _, ok in A if ok)
    for name, ok in A:
        if not ok:
            print("  ❌ %s" % name)
    print("%d/%d 通过%s" % (n_pass, len(A), "" if n_pass == len(A)
                            else "（失败断言见上）"))
    sys.exit(0 if n_pass == len(A) else 1)


# ---------------------------------------------------------------- 主审计
def main():
    ap = argparse.ArgumentParser(description="errata 政策自检（只读，不改表不改语料）")
    ap.add_argument("--guard-samples", nargs=2, metavar=("JA", "N"), default=None,
                    help="打印该错形命中的段原文（默认最多 N=20 条），标 guard 是否命中")
    ap.add_argument("--test", action="store_true", help="门禁断言（退出码 0=全过）")
    ap.add_argument("--json", nargs="?", const=os.path.join(ROOT, "data", "tmp",
                                                            "errata_audit.jsonl"),
                    default=None, metavar="PATH",
                    help="审计结果另存 JSONL（每条一行 dict；缺省 data/tmp/errata_audit.jsonl）")
    a = ap.parse_args()

    if a.test:
        cmd_test()
    if a.guard_samples:
        ja, n = a.guard_samples[0], max(1, int(a.guard_samples[1]))
        cmd_guard_samples(ja, n)
        return

    entries = load_errata()
    t0 = time.time()
    queries = {e["ja"] for e in entries} | {e["fix"] for e in entries}
    stats, n_seg = gather(queries)
    rows = audit_rows(entries, stats)
    print("errata 政策审计：%s（%d 条）" % (ERRATA_PATH, len(entries)))
    print("语料：%s + learning 的 asr/（%d 段去重后，%.1f 秒）"
          % ("draft", n_seg, time.time() - t0))
    for r in rows:
        print("  %-10s→%-8s [%-6s] hits=%-4d works=%-3d cf=%-5d anchor=%-3s%s %s"
              % (r["ja"], r["fix"], r["mode"], r["n_hits"], r["n_works"],
                 r["wrong_cf"], "Y" if r["anchor"] else "N",
                 " legit=0" if r["legit0"] else "", r["verdict"]))
    from collections import Counter
    cnt = Counter(r["verdict"] for r in rows)
    print("verdict 分布：%s（CONSUMED=已被旧修正消化，不计安全/不安全；"
          "AUTO_UNSAFE/GUARD_THIN 只出建议，改档由人定）" % dict(cnt))
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        with open(a.json, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("已写入：%s（%d 行）" % (a.json, len(rows)))


if __name__ == "__main__":
    main()
