# -*- coding: utf-8 -*-
"""语义覆盖检查（P2，2026-10-28）：**原文出现的东西，中文里不许丢**。

为什么单独做这一层
------------------
HY-MT 的安全对齐会把露骨内容**泛化成公文**，信息在初译就丢了，下游补不回来：

```
おしっこ                                → 排泄行为
デカパイ押し付けておちんぽこきこきもしないの?   → 然后也不进行任何性行为吗？
ちんこ出し入れされるたびに                 → 每次那个部位进出的时候…
```

频率低（R154 全库抽样 161 命中里真缺陷约 20 条）但**最刺眼**——读者一眼就知道漏了东西。
`term_lock` 管不了（它管「同一个词统一成哪个译法」），`check.py` 也管不了
（它只查日文残留/空行/标点）。所以要一张**源语短语驱动**的覆盖表。

与风格术语表的分工（别合并）
--------------------------
| 表 | 管什么 | 违反的后果 |
|---|---|---|
| `glossary.json` / `pin_sets.json` | 「更偏好怎么说」（肉棒 vs 鸡巴） | 风格不一致，可接受 |
| `coverage.json`（本脚本） | 「原文出现就不能丢什么」 | 信息丢失，不可接受 |

判据（只拦真委婉化，不拦省略）
--------------------------
```
命中 ja  且  本行/相邻行都没有 must  且  本行出现 forbid   →  违规
```
只漏译（中文里干脆不提）**不报**——那是省略类缺陷，混进来会淹掉真问题。
相邻行放行是为了代词承接（「它好硬」承接上一行的「肉棒」）。

用法
----
```bash
# 全库体检（只读）
python scripts/semantic_coverage.py --lrc kikoerusubtitle --asr data/draft

# 单部
python scripts/semantic_coverage.py --lrc kikoerusubtitle --rj RJ1336515 --verbose

# 定向修复（需要 llama-server；每条一次调用，仍不过就确定性替换/告警）
python scripts/semantic_coverage.py --lrc <run 的 05 目录> --asr <run 的 01 目录> --fix

# 自检（不需要语料）
python scripts/semantic_coverage.py --selftest
"""
import argparse
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.environ.get("KIKOERU_ROOT",
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import styleloc  # noqa: E402

# ASR 配对直接复用 scan_violations 的实现（**别在这里另写一套**：
# 版本自动选择 pick_asr 踩过坑 90，两套口径会给出互相矛盾的数字）。
import scan_violations as SV  # noqa: E402

DEFAULT_TABLE = os.path.join(styleloc.style_dir(), "coverage.json")


def load_table(path=None):
    p = path or DEFAULT_TABLE
    if not os.path.exists(p):
        # 本地没有就退回公开预设（刚 clone 下来也能跑）
        p = os.path.join(ROOT, "presets", "coverage.json")
    if not os.path.exists(p):
        return [], p
    d = json.load(open(p, encoding="utf-8"))
    out = []
    for e in d.get("entries") or []:
        out.append({
            "id": e.get("id") or "?",
            "level": e.get("level") or "style",
            "ja": [re.compile(x) for x in e.get("ja") or []],
            "must": [re.compile(x) for x in e.get("must") or []],
            "forbid": [re.compile(x) for x in e.get("forbid") or []],
            "swap": e.get("swap") or {},
            "note": e.get("note") or "",
        })
    return out, p


def check_pair(ja, zh, prev_zh="", next_zh="", table=None):
    """返回违规列表（空 = 通过）。纯确定性，可复现。"""
    if not ja or not zh:
        return []
    table = table if table is not None else load_table()[0]
    out = []
    for e in table:
        if not any(p.search(ja) for p in e["ja"]):
            continue
        # must 在「本行 + 相邻行」任一出现即算表达（代词承接）
        if any(p.search(zh) for p in e["must"]):
            continue
        if any(p.search(prev_zh or "") or p.search(next_zh or "") for p in e["must"]):
            continue
        hit = [p.pattern for p in e["forbid"] if p.search(zh)]
        if not hit:
            continue          # 只漏译、没被泛化 → 不报（省略是另一类缺陷）
        out.append({"id": e["id"], "level": e["level"], "forbid": hit,
                    "ja": ja[:40], "zh": zh[:40], "note": e["note"]})
    return out


# ---------------------------------------------------------------- 修复
def _llm(messages, max_tokens=200, temperature=0.2):
    """复用 polish.call：同一台 llama-server、同一套参数（别在这里另起一套客户端）。"""
    try:
        import polish
        return polish.call(messages, max_tokens=max_tokens,
                           temperature=temperature) or ""
    except Exception as e:
        return "__ERR__:%s" % e


FIX_SYS = ("你是日语音声作品的中文字幕译者。给你一句日文和它现在的中文译文，"
           "译文把一个具体词泛化成了含糊说法，信息丢了。\n"
           "只输出**改好的那一行中文**：不要解释、不要引号、不要日文、不要换行。\n"
           "保留原译的语气和长度感觉，只把丢掉的东西说出来。")


def _strict_ok(zh, entry):
    """修复后的**更严**判据：must 必须出现，且 forbid 必须彻底消失。

    `check_pair` 只要求「must 出现」就放过（因为它允许代词承接）；
    但修复场景下模型很容易「加一个词、原词不动」——
    实测「现在的性行为…做爱的快感」：must 有了、forbid 还在，读起来更糟。
    """
    if not any(p.search(zh) for p in entry["must"]):
        return False
    return not any(p.search(zh) for p in entry["forbid"])


def fix_one(ja, zh, viol, entry):
    """一次定向修复。返回 (新文本, 方式)。失败返回 (None, 原因)。"""
    # 只给前两个候选词，并明确「优先第一个」——不然模型会挑最书面的那个
    # （实测 ちんこ 被改成「阴茎」而不是本工程的规范词「肉棒」）。
    cand = [p.pattern for p in entry["must"][:2]]
    bad = "、".join(viol["forbid"])
    user = ("日文：%s\n现在的译文：%s\n丢掉的词：%s\n"
            "要求：译文必须明确说出「%s」这一类东西，**优先用「%s」**；"
            "改完的句子里**不许再出现**「%s」这些含糊说法。"
            % (ja, zh, bad, "、".join(cand), cand[0], bad))
    r = _llm([{"role": "system", "content": FIX_SYS},
              {"role": "user", "content": user}], max_tokens=120)
    r = (r or "").strip().strip('"').strip("「」")
    r = r.split("\n")[0].strip()
    if not r or r.startswith("__ERR__"):
        return None, r or "空回执"
    if re.search(r"[ぁ-んァ-ヶ]", r):
        return None, "回执含日文"
    if _strict_ok(r, entry):
        return r, "llm"
    # 确定性兜底：把 forbid 词按 swap 表换掉（只在有映射且换完真的干净时采用）
    t = r
    for k, v in (entry["swap"] or {}).items():
        t = t.replace(k, v)
    if t != r and _strict_ok(t, entry):
        return t, "swap"
    return None, "复核仍不过（must 有了但 forbid 没清干净）"


LRC_TS = re.compile(r"^\[(\d+):(\d+\.\d+)\](.*)$")


def parse_raw(path):
    """保留原始行（含 [ti:]/[by:]/空行/行尾符），返回 (lines, idx_map)。

    ⚠️ 修复写盘**必须**按原始行改，不能 `parse_lrc` 之后重排——
    R154 踩过：重排会丢掉 `[ti:]`/`[by:]` 头与空行，整轨 356 行被换成 353 行，
    时间戳格式也从 `[01:07.04]` 变成 `[01:07.04]`（padding 不同）→ 整文件 diff。
    """
    with open(path, encoding="utf-8", newline="") as f:
        lines = f.readlines()
    idx_map = []          # idx_map[row_index] = lines 里的下标
    for i, ln in enumerate(lines):
        if LRC_TS.match(ln.rstrip("\r\n")):
            idx_map.append(i)
    return lines, idx_map


def fix_file(path, asr_data, table, apply=False, verbose=False):
    """就地修一个 LRC（只改命中的那几行文本，时间戳与头信息原样保留）。

    返回 (修好条数, 仍失败条数, 明细)。
    """
    raw, idx_map = parse_raw(path)
    rows = SV.parse_lrc(path)
    segs = (asr_data or {}).get("segments") or []
    texts = [zh for _, zh in rows]
    fixed, failed, detail = 0, 0, []
    for k, (t, zh) in enumerate(rows):
        if not zh:
            continue
        t1 = rows[k + 1][0] if k + 1 < len(rows) else t + 3
        ja = SV.ja_in_range(segs, t, t1)
        if not ja:
            continue
        prev_zh = texts[k - 1] if k > 0 else ""
        next_zh = texts[k + 1] if k + 1 < len(texts) else ""
        viols = check_pair(ja, zh, prev_zh, next_zh, table)
        if not viols:
            continue
        ent = next(e for e in table if e["id"] == viols[0]["id"])
        new, how = fix_one(ja, zh, viols[0], ent)
        if new:
            texts[k] = new
            fixed += 1
            detail.append({"t": round(t, 2), "ja": ja[:40], "old": zh[:40],
                           "new": new[:40], "how": how, "rule": ent["id"]})
            if verbose:
                print("    ✓ %.1fs %s → %s（%s/%s）"
                      % (t, zh[:24], new[:24], how, ent["id"]))
        else:
            failed += 1
            detail.append({"t": round(t, 2), "ja": ja[:40], "old": zh[:40],
                           "new": None, "how": how, "rule": ent["id"]})
            if verbose:
                print("    ✗ %.1fs %s → %s（%s）" % (t, zh[:26], how, ent["id"]))
    if apply and fixed:
        # 只替换文本部分，时间戳前缀逐字节照抄
        for k, (t, zh) in enumerate(rows):
            if texts[k] == zh:
                continue
            i = idx_map[k]
            m = LRC_TS.match(raw[i].rstrip("\r\n"))
            eol = raw[i][len(raw[i].rstrip("\r\n")):] or "\n"
            raw[i] = "%s%s%s" % (raw[i][:m.start(3)], texts[k], eol)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.writelines(raw)
    return fixed, failed, detail


# ---------------------------------------------------------------- 体检
def scan(lrc_root, asr_root=None, rj=None, table=None, verbose=False):
    table = table if table is not None else load_table()[0]
    rows_out = []
    works = [rj] if rj else sorted(d for d in os.listdir(lrc_root)
                                   if os.path.isdir(os.path.join(lrc_root, d)))
    for w in works:
        d = os.path.join(lrc_root, w)
        if not os.path.isdir(d):
            continue
        for p in sorted(glob.glob(os.path.join(d, "*.lrc"))):
            tr = os.path.splitext(os.path.basename(p))[0]
            rows = SV.parse_lrc(p)
            asr, ver = SV.pick_asr(w, tr, rows)
            if not asr:
                continue
            segs = asr.get("segments") or []
            texts = [zh for _, zh in rows]
            for k, (t, zh) in enumerate(rows):
                if not zh:
                    continue
                t1 = rows[k + 1][0] if k + 1 < len(rows) else t + 3
                ja = SV.ja_in_range(segs, t, t1)
                if not ja:
                    continue
                viols = check_pair(ja, zh,
                                   texts[k - 1] if k > 0 else "",
                                   texts[k + 1] if k + 1 < len(texts) else "",
                                   table)
                for v in viols:
                    v.update({"rj": w, "track": tr, "t": round(t, 2),
                              "asr_ver": ver})
                    rows_out.append(v)
                    if verbose:
                        print("%s|%s|%.1f\n  日: %s\n  中: %s\n  禁: %s（%s）"
                              % (w, tr[:20], t, v["ja"], v["zh"],
                                 "/".join(v["forbid"]), v["id"]))
    return rows_out


# ---------------------------------------------------------------- 自检
def _selftest():
    table, path = load_table()
    ok = []

    def t(name, cond):
        ok.append((name, bool(cond)))

    t("表能载入（≥8 条）", len(table) >= 8)
    # ① 真委婉化必须抓到
    v = check_pair("おしっこしたいの", "想要排泄行为吗", table=table)
    t("おしっこ→排泄行为 命中", any(x["id"] == "oshikko" for x in v))
    v = check_pair("ちんこ出し入れされるたびに", "每次那个部位进出的时候", table=table)
    t("ちんこ→那个部位 命中", any(x["id"] == "chinpo" for x in v))
    v = check_pair("セックスしないと出られない部屋", "不发生关系就出不去的房间", table=table)
    t("セックス→发生关系 命中", any(x["id"] == "sex" for x in v))
    # ② 正确译法不许误报
    t("おしっこ→尿 不报", not check_pair("おしっこしたいの", "我想尿尿", table=table))
    t("ちんこ→肉棒 不报", not check_pair("ちんこ出し入れ", "肉棒进进出出", table=table))
    # ③ 只漏译（没有 forbid 词）不报——那是省略，不是委婉化
    t("纯省略不报", not check_pair("おしっこしたいの", "我想去一下", table=table))
    # ④ 代词承接放行（相邻行说了就算表达）
    t("相邻行承接放行", not check_pair("ちんこ", "它好硬", "肉棒", "啊", table=table))
    # ⑤ 源语没命中就绝不报
    t("源语无关不报", not check_pair("今日はいい天気", "发生关系", table=table))
    for name, good in ok:
        print("  %s %s" % ("✅" if good else "❌", name))
    n = sum(1 for _, g in ok if g)
    print("%s semantic_coverage selftest %d/%d（表：%s）"
          % ("✅" if n == len(ok) else "❌", n, len(ok), path))
    return 0 if n == len(ok) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lrc", default=os.path.join(ROOT, "kikoerusubtitle"))
    ap.add_argument("--asr", default=os.path.join(ROOT, "data", "draft"))
    ap.add_argument("--rj", default="")
    ap.add_argument("--table", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--strict", action="store_true", help="有违规就 exit 1（门禁用）")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--fix", action="store_true", help="定向修复（需要 llama-server）")
    ap.add_argument("--apply", action="store_true", help="配合 --fix 真正写盘")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        sys.exit(_selftest())

    table, tpath = load_table(a.table)
    if not table:
        print("❌ 覆盖表为空：%s" % tpath)
        sys.exit(2)
    SV.ASR_ROOT = a.asr

    if a.fix:
        total_f, total_x, details = 0, 0, {}
        works = [a.rj] if a.rj else sorted(d for d in os.listdir(a.lrc)
                                           if os.path.isdir(os.path.join(a.lrc, d)))
        for w in works:
            d = os.path.join(a.lrc, w)
            if not os.path.isdir(d):
                continue
            for p in sorted(glob.glob(os.path.join(d, "*.lrc"))):
                tr = os.path.splitext(os.path.basename(p))[0]
                rows = SV.parse_lrc(p)
                asr, _v = SV.pick_asr(w, tr, rows)
                if not asr:
                    continue
                f, x, det = fix_file(p, asr, table, apply=a.apply, verbose=a.verbose)
                if det:
                    details["%s|%s" % (w, tr)] = det
                total_f += f
                total_x += x
        print("定向修复：改好 %d 条，仍失败 %d 条%s"
              % (total_f, total_x, "（已写盘）" if a.apply else "（未写盘，加 --apply）"))
        if a.json:
            json.dump(details, open(a.json, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            print("→ %s" % a.json)
        sys.exit(0 if total_x == 0 else 1)

    rows = scan(a.lrc, a.asr, a.rj or None, table, a.verbose)
    n_loss = sum(1 for r in rows if r.get("level") == "loss")
    print("\n语义覆盖：违规 %d 条（其中真信息丢失 loss %d 条；表 %s）"
          % (len(rows), n_loss, os.path.basename(tpath)))
    if rows:
        import collections
        c = collections.Counter(r["id"] for r in rows)
        for k, v in c.most_common():
            lv = next((e["level"] for e in table if e["id"] == k), "?")
            print("  %-10s %3d（%s）" % (k, v, lv))
        if not a.verbose:
            for r in rows[:15]:
                print("  %s|%.1f  日:%s  中:%s" % (r["rj"], r["t"], r["ja"], r["zh"]))
            if len(rows) > 15:
                print("  …（--verbose 看全部）")
    if a.json:
        json.dump(rows, open(a.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("→ %s" % a.json)
    # 门禁口径：只有 loss（真信息丢失）算失败；style/term 只报不拦。
    sys.exit(1 if (a.strict and n_loss) else 0)


if __name__ == "__main__":
    main()
