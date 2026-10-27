# -*- coding: utf-8 -*-
r"""源语驱动的实体/称谓确定性修复（GPT 第八轮 Q4 的落地层）。

## 这一层解决什么

`term_lock.py` 只做**同义变体收敛**（全篇数票选锚 → 统一）。它处理不了
「**初译把 `先生は` 错译成「这位先生」**」这类 —— 因为中文侧根本没有
「先生」的同义变体，是**多出了指示词 + 语义偏移**。

GPT 的判据（本轮）：

> 对于 `先生は → 这位先生`：
> - 源语存在 `先生`，且实体为 `tutor_01`；
> - 中文命中 `先生 / 这位先生 / 那位先生` 等候选；
> - 若源语前**没有** `この / その / あの` 等指示词，则替换为「老师」**并删除**
>   中文多出的「这位/那位」；
> - 若源语**确实有**指示词，则**保留**指示词，改为「这位老师/那位老师」。

这是「源语已知实体 → 高置信、可确定性修复」的问题，**不该留给自由润色模型猜**。

## 分层（GPT 明确要求）

```text
可识别的实体/术语错译      → 本脚本自动修（source-aware repair）
中文命中可疑但替换方式不明 → 交给 targeted rewrite（只让 Qwen 改这一 cue）
完全无法由规则识别的错译   → 风险告警 + 试听/人工清单
```

本脚本只做**第一层**：命中判据强、替换无歧义时改；命中不出任何候选中文时
**只告警不强改**。

## 安全性（沿用 term_lock 的边界，且更严）

1. 只处理**当面称呼**的「先生」（尊称一位职业/身份者），不碰「丈夫/男性」义。
2. **专名保护**：`教え子ハラマ先生`、`〇〇先生`（人名 + 先生）里的先生**不动**。
3. 日文侧必须有 `先生` 且（在 ja 校验可用时）该行**确实命中**，才改。
4. 默认 `--dry-run`，`--apply` 才写盘。

用法：
  python term_repair.py <LRC目录> --ja-dir <ASR目录> [--ledger <账本.json>] [--apply]
"""
import os
import re
import sys
import glob
import json
import argparse

sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------
# 「先生」的**尊称用法**：源语有先生，且不是「人名 + 先生」这种专名。
# 专名形如「ハラマ先生」「七草先生」——先生紧跟 2+ 个片假名/汉字人名。
_NAME_BEFORE = re.compile(r"[ァ-ヶー]{2,}先生|先生[はがのにを、]?$")

# 中文侧的指示词（初译多出来的「这位/那位/一位/某位」）
ZH_DET = r"(这|那|一|某)位"
ZH_SENSEI_WITH_DET = re.compile(ZH_DET + r"先生")

# 日文侧真正的指示词（决定中文该不该带指示词）
JA_DET = re.compile(r"この|その|あの|どの|例の")

# 「先生」作「丈夫/男性」义的中文语境——出现即跳过（与 term_lock 一致）
NON_VOCATIVE = re.compile(r"(老公|丈夫|祖|老公公)")

# 绰号保护：这些前缀下「先生」是**绰号/戏称**，不是当面尊称，不能改。
# 实测 RJ9000011「肛门先生」被误改成「肛门老师」（源语 ASR 还听错成
# マックオーナー，但初译猜对了原意）。引号内的同理（「先生」加引号是外号）。
NICKNAME_PREFIX = re.compile(
    r"(肛门|屁股|屁屁|小鸡|肉棒|美臀|大屌|抖M|抖S|变态|色狼|色鬼|淫魔|自慰|撸管|"
    r"马赛克|工口|打屁股|舔屌|口交|颜射|中出|内射)$")
IN_QUOTES = re.compile(r"[「『“\"][^」』”\"]*先生[^」』”\"]*[」』”\"]")


def load_lrc(path):
    out = []
    for ln in open(path, encoding="utf-8"):
        s = ln.rstrip("\n")
        m = re.match(r"^(\[[^\]]*\])(.*)$", s)
        if m:
            out.append([m.group(1), m.group(2)])
    return out


def save_lrc(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for ts, txt in rows:
            f.write(ts + txt + "\n")


LRC_TS = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def ts_to_sec(ts):
    m = LRC_TS.match(ts)
    return None if not m else int(m.group(1)) * 60 + float(m.group(2))


def load_asr_segments(path):
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return []
    segs = d.get("segments") if isinstance(d, dict) else d
    out = []
    for s in segs or []:
        if isinstance(s, dict) and s.get("text"):
            out.append((float(s.get("start", 0)), float(s.get("end", 0)), s["text"]))
    out.sort()
    return out


def ja_at(segs, sec):
    """该时刻附近的日文原文（聚合多条 segment，与 term_lock 同口径）。"""
    if not segs or sec is None:
        return None
    hit = [tx for st, en, tx in segs if st - 2.0 <= sec <= en + 2.0]
    return "".join(hit) if hit else ""


def repair_line(zh, ja, anchor="老师", ja_alias=r"先生",
                zh_variants=None, name_guard=r"[ァ-ヶー]{2,}先生"):
    """对一行做源语驱动的确定性修复（**单族**）。

    返回 (新中文, 修改数, 告警数)。

    ja_alias    源语触发词正则（默认「先生」）
    zh_variants 该族的中文写法池（含锚点）；默认 [anchor, "先生", "医生", ...]
    name_guard  专名保护正则（源语里这种人名+称谓不动）

    规则（以「先生」为例，其它族同理）：
      1) 中文「这位X/那位X」且源语**无**指示词 → 去指示词、X 改锚点。
      2) 中文「这位X…」且源语**有**指示词   → 保留指示词、X 改锚点。
      3) 中文裸 X（前面不是指示词）且源语命中且非专名 → 改锚点。
    """
    if not zh or not ja:
        return zh, 0, 0
    if not re.search(ja_alias, ja):
        return zh, 0, 0
    # 专名保护（如「ハラマ先生」）：该处源语命中**全部**落在专名里 → 不动
    if name_guard:
        try:
            guards = re.findall(name_guard, ja)
            if guards and len(re.findall(ja_alias, ja)) == len(guards):
                return zh, 0, 0
        except re.error:
            pass

    # 该族的中文变体池：除锚点外的所有写法都要收敛到锚点
    # 默认（未指定）时退化为「先生」族的历史行为，保证向后兼容。
    if zh_variants is None:
        zh_variants = [anchor, "先生", "医生", "医师", "大夫", "律师", "议员"]
    head = re.search(r"[^\W\d_]", anchor)
    variant_re = "|".join(re.escape(v) for v in sorted(set(zh_variants), key=len, reverse=True)
                          if v and v != anchor)

    def _nickname_before(text, pos):
        """pos 处是「先生/变体」，前面紧邻是戏称前缀 → 受保护。"""
        pre = text[max(0, pos - 4):pos]
        return bool(NICKNAME_PREFIX.search(pre))

    def _in_quotes(text, pos):
        """pos 落在引号内的「先生」→ 外号，受保护。"""
        lo = text.rfind("「", 0, pos)
        hi = text.rfind("『", 0, pos)
        ld = max(lo, hi)
        if ld == -1:
            return False
        end = min([x for x in (text.find("」", ld), text.find("』", ld)) if x != -1] or [-1])
        return end == -1 or pos < end

    n_chg = 0
    new = zh

    if variant_re:
        # 规则 1/2：带指示词的「X位<变体>」
        pat_det = re.compile(r"(这|那|一|某)位(%s)" % variant_re)

        def _repl_det(m):
            nonlocal n_chg
            if _nickname_before(m.string, m.start(2)) or _in_quotes(m.string, m.start(2)):
                return m.group(0)
            det = m.group(1)
            n_chg += 1
            return ("%s位%s" % (det, anchor)) if JA_DET.search(ja) else anchor
        new = pat_det.sub(_repl_det, new)

        # 规则 3：裸变体（前面不是指示词）
        parts = re.split(r"(%s)" % variant_re, new)
        buf = []
        for i, seg in enumerate(parts):
            if i % 2 == 0:                 # 非捕获组内容
                buf.append(seg)
                continue
            pos = len("".join(buf))
            prev = "".join(buf)[-6:]
            if (_nickname_before(new, pos) or _in_quotes(new, pos)
                    or re.search(r"[這这那某一]位?$", prev) or NON_VOCATIVE.search(prev)):
                buf.append(seg)
            else:
                n_chg += 1
                buf.append(anchor)
        new = "".join(buf)
    return new, n_chg, 0


def _ja_by_file(files, ja_dir):
    """把每个 LRC 对到同作品的 ASR json。"""
    out = {}
    if not ja_dir or not os.path.isdir(ja_dir):
        return out
    for f in files:
        stem = os.path.splitext(os.path.basename(f))[0]      # xxx.mp3
        cands = glob.glob(os.path.join(ja_dir, "**", stem + ".json"), recursive=True)
        if not cands:
            cands = glob.glob(os.path.join(ja_dir, "**", stem + "*.json"), recursive=True)
        if cands:
            out[f] = load_asr_segments(cands[0])
    return out


def _families_from_ledger(led):
    """从账本读所有可用族 → [(key, ja_alias, anchor, to_converge, desc), ...]。

    跳过 dormant 族（源语无 mention → 不注入、不修）。
    ⚠️ 用 `to_converge`（必须收敛的对立写法），**不是** `zh_family`
      ——后者含合法礼貌形式（如「老师您」），收敛它们会误删「您」。
    """
    out = []
    for k, v in (led.get("entities") or {}).items():
        if v.get("confidence") == "dormant":
            continue
        fam = v.get("zh_family") or []
        if not fam:
            continue
        alias = v.get("ja_alias_text") or "|".join(v.get("ja_aliases") or [])
        if not alias:
            continue
        out.append((k, alias, fam[0], list(v.get("to_converge") or []),
                    v.get("desc", "")))
    return out


def main():
    ap = argparse.ArgumentParser(description="源语驱动的称谓确定性修复")
    ap.add_argument("target", help="LRC 目录或单个文件")
    ap.add_argument("--ja-dir", "--asr-dir", dest="ja_dir", required=True,
                    help="ASR json 目录（源语判据必需）")
    ap.add_argument("--ledger", default="", help="实体账本 json（给锚点，可选）")
    ap.add_argument("--only", default="", help="只处理指定族，逗号分隔")
    ap.add_argument("--apply", action="store_true", help="实际写盘")
    a = ap.parse_args()

    families = []
    if a.ledger and os.path.exists(a.ledger):
        try:
            led = json.load(open(a.ledger, encoding="utf-8"))
            families = _families_from_ledger(led)
            print("账本 %s：%d 个可用族" % (led.get("work", "?"), len(families)))
            for k, alias, anchor, fam, desc in families:
                print("  [%-10s] %s → 「%s」" % (k, alias, anchor))
        except Exception as e:
            print("⚠️ 账本读取失败：%s" % e)
    if not families:
        # 兜底：只做 sensei（向后兼容）
        families = [("sensei", r"先生", "老师", ["老师", "老师您", "教师"], "默认")]
        print("（无账本，退化为只处理「先生」）")

    only = [x for x in a.only.split(",") if x]
    if only:
        families = [f for f in families if f[0] in only]

    # 每族的专名保护：人名（片假名 ≥2）+ 该族日文别名
    def name_guard_for(alias):
        # 取别名里最长的字面片段做保护锚
        core = re.sub(r"[\\()?|*+\[\]{}^$]", "", alias)
        core = core.split("|")[0] if core else "先生"
        return r"[ァ-ヶー]{2,}%s" % re.escape(core)

    files = ([a.target] if os.path.isfile(a.target)
             else sorted(glob.glob(os.path.join(a.target, "**", "*.lrc"), recursive=True)))
    if not files:
        print("没有找到 .lrc：%s" % a.target)
        return
    ja_map = _ja_by_file(files, a.ja_dir)
    print("待处理 %d 个 LRC，对到日文 %d 个" % (len(files), len(ja_map)))

    total = 0
    per_fam = {}
    for f in files:
        rows = load_lrc(f)
        segs = ja_map.get(f)
        ch = 0
        for i, (ts, txt) in enumerate(rows):
            ja = ja_at(segs, ts_to_sec(ts)) if segs else None
            if not ja:
                continue
            cur = txt
            for key, alias, anchor, conv, _d in families:
                new, n, _w = repair_line(cur, ja, anchor, alias,
                                         zh_variants=conv,
                                         name_guard=name_guard_for(alias))
                if n and new != cur:
                    cur = new
                    ch += n
                    per_fam[key] = per_fam.get(key, 0) + n
            if cur != txt:
                rows[i][1] = cur
        if ch:
            total += ch
            print("  %s：修复 %d 处" % (os.path.basename(f), ch))
            if a.apply:
                save_lrc(f, rows)
    print("\n合计修复 %d 处。" % total)
    if per_fam:
        print("按族：" + "、".join("%s=%d" % (k, v) for k, v in
                                  sorted(per_fam.items(), key=lambda x: -x[1])))
    if not a.apply:
        print("（预览模式，加 --apply 写盘）")
    else:
        print("已写盘。")


if __name__ == "__main__":
    main()
