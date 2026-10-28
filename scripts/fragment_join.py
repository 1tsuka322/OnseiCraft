# -*- coding: utf-8 -*-
"""碎片行合并（P1-c，2026-10-28）：把「只有句末助词」的独立行并回上一行。

背景
----
`REVIEW_SYS` 要求「严格 N 行、不许合并」，所以润色层**永远修不掉**这种行：

```
[02:42.60] 呢。
```

它在结构上是上一句的尾巴（日文 `のよ` / `ね` / 拖音），独立成行读起来是断的。
但合并会**动时间轴**（少一个时间戳），所以本工具**只做高可靠档 + 人工白名单**，
不做全自动（TODO P1-c 的明确约束）。

两档判据（都在日文侧，纯确定性）
------------------------------
| 档 | 判据 | 处理 |
|---|---|---|
| **A** 高可靠 | 日文只有句末助词/拖音（`のよ`/`ね`/`か`/`…`），**且**中文只有 ≤3 字的语气词 | 可进白名单自动合并 |
| **B** 待判 | 日文只有句末助词，但中文是完整短句（`ね?`→`对吧？`） | **不许自动合并**——它独立成行是正常的 |

用法
----
```bash
# 出候选报告（只读）
python scripts/fragment_join.py --list --json data/tmp/fragment_candidates.json

# 按白名单合并（白名单是 A 档里人工点过头的，格式见下）
python scripts/fragment_join.py --apply data/tmp/fragment_whitelist.json
```

白名单格式（**只写 A 档，且必须逐条给理由**）：

```json
[{"rj": "RJ1065777", "track": "Tr.02 ...", "t": 966.9, "why": "ね? 并进上一句「一起玩吧」"}]
```
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
import scan_violations as SV  # noqa: E402

# 日文「只有句末助词/拖音」：整个段没有实词
JA_TAIL_ONLY = re.compile(
    r"^(の|のよ|のね|のか|のさ|か|かな|かしら|ね|よね|よ|な|わ|ぞ|ぜ|"
    r"でしょ|でしょう|じゃん|っけ|んだ|んだけど|だけど|けど|から|し|って)+"
    r"[\s、。,.!?！？…‥~～ー〜っ]*$")
# 中文「只有语气词」：≤3 字且不含实词
ZH_BARE = re.compile(
    r"^[\s、。,.!?！？…‥~～ー〜]*(呢|呐|啊|呀|哦|喔|嘛|啦|吧|哈|嗯|诶|欸|对|是|的)"
    r"[\s、。,.!?！？…‥~～ー〜]*$")


def candidates(lrc_root=None):
    lrc_root = lrc_root or os.path.join(ROOT, "kikoerusubtitle")
    out = []
    for p in sorted(glob.glob(os.path.join(lrc_root, "RJ*", "*.lrc"))):
        rj = os.path.basename(os.path.dirname(p))
        tr = os.path.splitext(os.path.basename(p))[0]
        rows = SV.parse_lrc(p)
        asr, ver = SV.pick_asr(rj, tr, rows)
        if not asr:
            continue
        segs = asr.get("segments") or []
        for k, (t, zh) in enumerate(rows):
            if not zh:
                continue
            t1 = rows[k + 1][0] if k + 1 < len(rows) else t + 3
            ja = SV.ja_in_range(segs, t, t1)
            if not ja or not JA_TAIL_ONLY.match(ja.strip()):
                continue
            prev_zh = rows[k - 1][1] if k > 0 else ""
            tier = "A" if (len(zh.strip()) <= 3 and ZH_BARE.match(zh.strip())) else "B"
            out.append({"rj": rj, "track": tr, "t": round(t, 2), "tier": tier,
                        "ja": ja.strip(), "zh": zh.strip(),
                        "prev_zh": prev_zh, "asr_ver": ver,
                        # A 档才允许自动合并：B 档的中文是完整短句，独立成行是对的
                        "auto_ok": tier == "A" and bool(prev_zh.strip())})
    return out


def apply(plan, lrc_root=None, dry=False):
    """按白名单合并。只删「这一行」，把中文接到上一行；时间戳逐字节不动。"""
    lrc_root = lrc_root or os.path.join(ROOT, "kikoerusubtitle")
    by_file = {}
    for it in plan:
        by_file.setdefault((it["rj"], it["track"]), []).append(it)
    n_ok = n_skip = 0
    for (rj, tr), items in by_file.items():
        p = os.path.join(lrc_root, rj, tr + ".lrc")
        if not os.path.exists(p):
            print("⚠️ 找不到 %s" % p)
            continue
        with open(p, encoding="utf-8", newline="") as f:
            lines = f.readlines()
        drop = set()
        for it in items:
            ts = "[%02d:%05.2f]" % (int(it["t"] // 60), it["t"] % 60)
            idx = [i for i, l in enumerate(lines) if l.startswith(ts)]
            if len(idx) != 1:
                print("  ⚠️ %s %s 时间戳不唯一（%d 处）→ 跳过" % (rj, ts, len(idx)))
                n_skip += 1
                continue
            i = idx[0]
            j = i - 1
            while j >= 0 and not lines[j].split("]", 1)[-1].strip():
                j -= 1                       # 跳过清屏 spacer
            if j < 0:
                print("  ⚠️ %s %s 上一行不存在 → 跳过" % (rj, ts))
                n_skip += 1
                continue
            prev_text = lines[j].split("]", 1)[-1].strip()
            cur = lines[i].split("]", 1)[-1].strip()
            # 合并：去掉上一行的句末标点再拼（「一起玩吧」+「呐？」→「一起玩吧 呐？」）
            merged = prev_text.rstrip("。！？…、，") + " " + cur
            head = lines[j][:lines[j].index("]") + 1]
            eol = lines[j][len(lines[j].rstrip("\r\n")):] or "\n"
            lines[j] = "%s%s%s" % (head, merged, eol)
            drop.add(i)
            n_ok += 1
            print("  ✓ %s %s  %s｜%s → %s" % (rj, ts, prev_text, cur, merged))
        if drop and not dry:
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.writelines(l for k, l in enumerate(lines) if k not in drop)
    print("\n合并 %d 行，跳过 %d 行%s" % (n_ok, n_skip, "（dry-run，未写盘）" if dry else ""))
    return n_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", default=None)
    ap.add_argument("--apply", default=None, metavar="WHITELIST")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--lrc", default=None)
    a = ap.parse_args()

    if a.apply:
        plan = json.load(open(a.apply, encoding="utf-8"))
        bad = [x for x in plan if not x.get("why")]
        if bad:
            print("❌ 白名单每条都必须写 why（%d 条没写）" % len(bad))
            sys.exit(2)
        sys.exit(0 if apply(plan, a.lrc, a.dry) >= 0 else 1)

    rows = candidates(a.lrc)
    na = sum(1 for r in rows if r["tier"] == "A")
    print("碎片行候选 %d 条：A 档（可自动合并）%d 条 / B 档（待人工判）%d 条"
          % (len(rows), na, len(rows) - na))
    print("\n— A 档 —")
    for r in rows:
        if r["tier"] == "A":
            print("  %s %.1f 日[%s] 中[%s] ← 前[%s]"
                  % (r["rj"], r["t"], r["ja"], r["zh"], r["prev_zh"][:26]))
    print("\n— B 档（**不许自动合并**：中文是完整短句，独立成行是正常的）—")
    for r in rows[:20]:
        if r["tier"] == "B":
            print("  %s %.1f 日[%s] 中[%s] ← 前[%s]"
                  % (r["rj"], r["t"], r["ja"], r["zh"], r["prev_zh"][:26]))
    if a.json:
        json.dump(rows, open(a.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("\n→ %s" % a.json)


if __name__ == "__main__":
    main()
