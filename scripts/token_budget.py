# -*- coding: utf-8 -*-
r"""润色 prompt 的 token 预算诊断（GPT 第八轮 Q5）。

## 为什么不能只看字符数

GPT Q5 要求：**用 tokenizer 实测** system + examples + context + cue + output，
不能把 prompt 塞到接近上下文上限。字符数会误导——中文 1 字 ≈ 1–2 token，
日文/符号差异更大。

## 实测结论（2026-10-16）

强润色 system = **7866 tokens**（每槽 12288）。构成：

| 部分 | token | 占比 |
|---|---|---|
| 任务 + 规则 1–19（**模板正文**） | **3387** | **43%** ← 最大 |
| hard_rules（含 user_rules 2017） | 3278 | 42% |
| style_guide | 441 | 6% |
| 16 组译者样例 | 437 | 6% |
| 术语表 | ~108 | 1% |

⚠️ **与 GPT 的猜测相反**：GPT 以为「16 组随机示例」是负担，
实测它只占 437 token（6%）。真正的最大项是**规则 1–19 的模板正文（3387）**。
→ 要瘦身应优先精简模板正文，而不是删样例。

## 用法

  # 需要 llama-server 在跑（用它的 /tokenize 端点）
  python token_budget.py
  python token_budget.py --slots 2      # 指定 -np，算每槽余量
"""
import os
import re
import sys
import json
import argparse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

API = "http://127.0.0.1:8080"


def ntok(text):
    """用 llama-server 的 /tokenize 端点实测 token 数。"""
    req = urllib.request.Request(
        API + "/tokenize",
        data=json.dumps({"content": text}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        return len(json.load(urllib.request.urlopen(req, timeout=60))["tokens"])
    except Exception as e:
        return "ERR:%s" % e


def estimate_by_len(text):
    """服务不可用时的粗估：中文 ≈ 0.75 token/字，日文 ≈ 0.9，ASCII ≈ 0.3。

    只用于没有服务时给个量级，**不要**当实测值。
    """
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]", text))
    other = len(text) - cjk
    return int(cjk * 0.8 + other * 0.3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slots", type=int, default=2, help="llama-server -np 值")
    ap.add_argument("--ctx", type=int, default=24576, help="llama-server -c 值")
    ap.add_argument("--style", default=os.environ.get(
        "KIKOERU_STYLE", os.path.join(os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "style")))
    a = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import polish

    guide, hard, pairs, stats, gloss = polish.load_style(a.style)
    system = polish.build_system(guide, hard, pairs, stats.get("total", 0), gloss)

    parts = {
        "完整强润色 system": system,
        "hard_rules 全文": hard,
        "style_guide": guide,
        "16 组样例": "\n".join("%d|%s\n   译：%s" % (i + 1, p["ja"], p["zh"])
                             for i, p in enumerate(pairs)),
    }
    m = re.search(r"任务：把给出的日文台词.*", system, re.S)
    parts["模板正文（任务+规则1-19）"] = m.group(0) if m else ""

    # 服务可用性
    online = True
    try:
        urllib.request.urlopen(API + "/health", timeout=5).read()
    except Exception:
        online = False

    print("tokenizer 实测：%s" % ("在线（/tokenize）" if online else "❌ 服务不可用，用粗估"))
    print("=" * 62)
    tot = None
    rows = []
    for k, v in parts.items():
        t = ntok(v) if online else estimate_by_len(v)
        if k == "完整强润色 system":
            tot = t
        rows.append((k, t, len(v)))
    for k, t, n in rows:
        pct = ""
        if tot and isinstance(tot, int) and isinstance(t, int) and k != "完整强润色 system":
            pct = "  %5.1f%%" % (100.0 * t / tot)
        print("  %-28s %6s tokens  (%d 字)%s" % (k, t, n, pct))

    print("=" * 62)
    per_slot = a.ctx // a.slots
    print("每槽上下文：%d / %d = %d tokens" % (a.ctx, a.slots, per_slot))
    if isinstance(tot, int):
        reserve = 2000        # 输出 + 待译 cue 的粗略预算
        left = per_slot - tot - reserve
        print("system %d + 预留 %d → 余 %d tokens" % (tot, reserve, left))
        if left < 0:
            print("⚠️ 已超限！llama.cpp 会截断 prompt 开头（系统提示受损）")
        elif left < 1500:
            print("⚠️ 余量偏紧：待译 cue 一多就会超。建议瘦身模板正文。")
        else:
            print("✅ 余量充足")
    print()
    print("结论：最大项是**模板正文（规则 1-19）**，不是样例/账本。"
          "瘦身应优先精简规则条目，别删样例。")


if __name__ == "__main__":
    main()
