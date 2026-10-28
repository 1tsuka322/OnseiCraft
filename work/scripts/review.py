# -*- coding: utf-8 -*-
"""句级审阅页 + 锁定（2026-10-21，E1，借鉴 ASMR-Dubber 的「表格逐句改 + 锁定」）。

**解决什么**：现在改一处错译要么 `fixline.py` 逐行敲命令（定位靠时间戳），
要么手编 LRC；而且**批量重跑会把人工修过的行冲掉**（术语表一改就全量重跑）。

本工具做两件事：
  1. `review.py --rj RJxxxx` → 生成 `review.html`：表格列出每行的
     `时间 / 日文 / 当前译文 / 可疑标记`，可就地改、可勾「锁定」、可筛选可疑行；
     点「导出 patch.json」。
  2. `review.py --rj RJxxxx --apply patch.json` → 把改动写回成品 LRC，
     并把「锁定」的行记进 `data/locks/<rj>.json`。
     **polish 之后会按锁覆盖**（见 polish.py 的 locks 逻辑），重跑不再冲掉人工修正。

用法：
  python review.py --rj RJ9000014                  # 生成审阅页
  python review.py --rj RJ9000014 --apply patch.json
"""
import os
import re
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json
import glob
import argparse
import datetime

ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rootloc  # noqa: E402  仓库三层结构：work/ 里，成品/音频在上一级
PROD = os.environ.get("KIKOERU_PROD") or rootloc.PROD
DRAFT = os.path.join(ROOT, "data", "draft")
LOCKS = os.path.join(ROOT, "data", "locks")
TS = re.compile(r"^\[(\d+):([\d.]+)\](.*)$")
KANA = re.compile(r"[\u3040-\u30ff]")
PUNCT_ONLY = re.compile(r"^[\s…‥・.．,，、。！？!?~～\-—–♪♡♥☆★（）()\[\]「」『』\"'“”‘’]+$")


def asr_segs(rj, stem):
    p = os.path.join(DRAFT, rj, "asr", stem + ".json")
    if not os.path.exists(p):
        return []
    return json.load(open(p, encoding="utf-8")).get("segments") or []


def load_queue(rj):
    """E5：script_align 写的逐句低置信清单 → [(source_audio_name, start)]。
    只收 kind=="suspect"（带汉字台词）；纯假名拟声/笑声句（nonlex）单独统计、不标待复核。
    ⚠️ 只查 draft/<rj>/review_queue.json——**绝不查 asr/ 目录**：下游 translate/polish
    按 *.json glob asr 目录，队列文件落在那里会被当 ASR 文件吃掉（实测 KeyError）。"""
    p = os.path.join(DRAFT, rj, "review_queue.json")
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
            return [(it.get("source", ""), float(it.get("start") or 0))
                    for it in (d.get("items") or [])
                    if it.get("kind", "suspect") == "suspect"]
        except Exception:
            return []
    return []


def rows_of(rj):
    """→ [{file, idx, ts, t, ja, zh, flags}]"""
    out = []
    queue = load_queue(rj)
    for p in sorted(glob.glob(os.path.join(PROD, rj, "*.lrc"))):
        fn = os.path.basename(p)
        stem = fn[:-4]
        S = asr_segs(rj, stem)
        lines = open(p, encoding="utf-8-sig", errors="replace").read().split("\n")
        rows = []
        for idx, l in enumerate(lines):
            m = TS.match(l.strip())
            if m:
                rows.append([int(m.group(1)) * 60 + float(m.group(2)), idx, m.group(3).strip()])
        prev_zh = ""
        for i, (t, idx, zh) in enumerate(rows):
            t1 = rows[i + 1][0] if i + 1 < len(rows) else t + 6
            # R23（坑 106）：清屏 spacer（空文本行）是「读完即走」的 intentional
            # 清屏，不是丢句——不进人工审阅队列（否则每条 spacer 都要人判一次）。
            if not zh:
                continue
            ja = "".join(s.get("text", "") for s in S
                         if (s.get("end") or 0) > (rows[i - 1][0] if i else t - 4)
                         and (s.get("start") or 0) < t1)
            flags = []
            if PUNCT_ONLY.match(zh):
                flags.append("纯标点")
            if KANA.search(zh):
                flags.append("日文残留")
            if len(zh) > 20:
                flags.append("长行")
            if zh and zh == prev_zh:
                flags.append("重复上行")
            if not ja:
                flags.append("未对齐")
            if re.search(r"ボク|君|お前|あんた", ja) and "我" in zh:
                flags.append("人称可疑")
            # E5：台本对齐低置信句（source 对得上且起点差 <3s）
            if any(fn[:-4] == src and abs(t - st) < 3.0 for src, st in queue):
                flags.append("台本待复核")
            out.append({"file": fn, "idx": idx, "ts": "[%02d:%05.2f]" % (t // 60, t % 60),
                        "t": round(t, 2), "ja": ja, "zh": zh, "flags": flags})
            prev_zh = zh
    return out


HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>审阅：__RJ__</title>
<style>
 body{font-family:system-ui,"Segoe UI",sans-serif;background:#f6f7f9;color:#1c1e21;margin:0;padding:16px}
 .wrap{max-width:1180px;margin:0 auto}
 .bar{position:sticky;top:0;background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:10px 14px;margin-bottom:12px;z-index:5}
 table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e6ea;border-radius:10px;overflow:hidden}
 th,td{padding:7px 8px;border-bottom:1px solid #eef1f4;font-size:13px;vertical-align:top}
 th{background:#f2f5f8;text-align:left;font-weight:600;position:sticky;top:56px}
 td.ts{color:#5b6570;white-space:nowrap;font-variant-numeric:tabular-nums}
 td.ja{color:#3c4650;max-width:330px}
 textarea{width:100%;border:1px solid #dfe4ea;border-radius:6px;padding:5px 6px;font:inherit;resize:vertical;min-height:30px}
 textarea.changed{border-color:#1a73e8;background:#f3f8ff}
 .flag{display:inline-block;background:#fff2e0;color:#8a5a00;border-radius:4px;padding:0 5px;margin:1px 2px 0 0;font-size:11px}
 .flag.hard{background:#ffe3e3;color:#a11}
 button{padding:6px 12px;border-radius:8px;border:1px solid #cfd6dd;background:#fff;cursor:pointer;font-size:13px}
 button.on{background:#1a73e8;color:#fff;border-color:#1a73e8}
 label{font-size:13px;color:#3c4650;margin-right:14px}
 input[type=checkbox]{vertical-align:-2px}
</style></head><body><div class="wrap">
<div class="bar">
  <b>审阅：__RJ__</b>　<span id="stat" style="color:#5b6570;font-size:13px"></span>
  <div style="margin-top:8px">
    <label><input type="checkbox" id="onlyFlag" onchange="render()"> 只看可疑</label>
    <label><input type="checkbox" id="onlyLock" onchange="render()"> 只看已锁定</label>
    <label><input type="checkbox" id="onlyQueue" onchange="render()"> 只看台本待复核</label>
    <button onclick="exportPatch()">导出 patch.json</button>
    <span style="color:#6b7683;font-size:12px;margin-left:8px">改完译文点「导出」，再跑 <code>review.py --apply patch.json</code>。勾「锁定」的行以后重跑不会被覆盖。</span>
  </div>
</div>
<table><thead><tr><th style="width:78px">时间</th><th style="width:300px">日文</th><th>中文（可改）</th><th style="width:56px">锁定</th></tr></thead>
<tbody id="tb"></tbody></table>
</div>
<script>
const DATA = __DATA__, LOCKS = __LOCKS__;
let edits = {};   // key -> new zh
const key = r => r.file + "|" + r.ts;
function render(){
  const onlyFlag = document.getElementById("onlyFlag").checked;
  const onlyLock = document.getElementById("onlyLock").checked;
  const onlyQueue = document.getElementById("onlyQueue").checked;
  const tb = document.getElementById("tb");
  let n = 0, changed = 0;
  tb.innerHTML = "";
  for (const r of DATA){
    const k = key(r);
    const locked = !!LOCKS[k];
    const queued = r.flags.indexOf("台本待复核") >= 0;
    if (onlyFlag && !r.flags.length) continue;
    if (onlyLock && !locked) continue;
    if (onlyQueue && !queued) continue;
    n++;
    const cur = edits[k] !== undefined ? edits[k] : r.zh;
    if (cur !== r.zh) changed++;
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="ts">${r.ts}</td><td class="ja">${(r.ja||"").slice(0,60)}</td>
      <td><textarea rows="1" data-k="${k}">${cur}</textarea>
        <div>${r.flags.map(f=>`<span class="flag ${f==='日文残留'||f==='空行'?'hard':''}">${f}</span>`).join("")}</div></td>
      <td style="text-align:center"><input type="checkbox" data-lk="${k}" ${locked?'checked':''}></td>`;
    tb.appendChild(tr);
  }
  tb.querySelectorAll("textarea").forEach(t=>{
    t.addEventListener("input", e=>{ edits[e.target.dataset.k] = e.target.value;
      e.target.classList.toggle("changed", e.target.value !== (DATA.find(x=>key(x)===e.target.dataset.k)||{}).zh);
      document.getElementById("stat").textContent = statText(); });
  });
  document.getElementById("stat").textContent = statText(n);
}
function statText(n){
  const total = DATA.length, flagged = DATA.filter(r=>r.flags.length).length;
  return `共 ${total} 行 · 可疑 ${flagged} 行 · 已改 ${Object.keys(edits).filter(k=>k.split("|").length).length} 处 · 显示 ${n||total} 行`;
}
function exportPatch(){
  const out = {rj: "__RJ__", at: new Date().toISOString().slice(0,19), rows: [], locks: []};
  document.querySelectorAll("textarea").forEach(t=>{
    const k = t.dataset.k, r = DATA.find(x=>key(x)===k);
    if (t.value !== r.zh) out.rows.push({file:r.file, ts:r.ts, old:r.zh, new:t.value});
  });
  document.querySelectorAll("input[data-lk]").forEach(c=>{ if (c.checked) out.locks.push(c.dataset.lk); });
  const blob = new Blob([JSON.stringify(out,null,1)], {type:"application/json"});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = "patch.json"; a.click();
}
render();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rj", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--apply", default="", help="patch.json 路径")
    a = ap.parse_args()

    os.makedirs(LOCKS, exist_ok=True)
    lock_path = os.path.join(LOCKS, a.rj + ".json")
    locks = json.load(open(lock_path, encoding="utf-8")) if os.path.exists(lock_path) else {}

    if a.apply:
        patch = json.load(open(a.apply, encoding="utf-8"))
        by_file = {}
        for r in patch.get("rows", []):
            by_file.setdefault(r["file"], []).append(r)
        n_changed = 0
        for fn, items in by_file.items():
            p = os.path.join(PROD, a.rj, fn)
            if not os.path.exists(p):
                print("  跳过（无此文件）", fn)
                continue
            lines = open(p, encoding="utf-8-sig", errors="replace").read().split("\n")
            for r in items:
                for idx, l in enumerate(lines):
                    if l.strip().startswith(r["ts"]):
                        lines[idx] = r["ts"] + r["new"]
                        n_changed += 1
                        break
            open(p, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
        for k in patch.get("locks", []):
            fn, ts = k.split("|")
            row = next((r for r in patch.get("rows", [])
                        if r["file"] == fn and r["ts"] == ts), None)
            if row is not None:
                # 改了并勾了锁定 → 记 {zh: 新文本, old: 被锁前的原文}。
                # old 给 polish 重跑时「key 漂移回退匹配」用（显示层切分变化后
                # ts 会移位，靠原文认领）。
                locks[k] = {"zh": row["new"], "old": row["old"]}
            else:
                # 没改但勾了锁定 → 锁当前文本
                p = os.path.join(PROD, a.rj, fn)
                for l in open(p, encoding="utf-8-sig", errors="replace"):
                    if l.strip().startswith(ts):
                        cur = TS.match(l.strip()).group(3).strip()
                        locks[k] = {"zh": cur, "old": cur}
                        break
        json.dump(locks, open(lock_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("应用完成：改 %d 行，锁定 %d 行（%s）" % (n_changed, len(patch.get("locks", [])), lock_path))
        return

    rows = rows_of(a.rj)
    if not rows:
        sys.exit("没读到成品 LRC：%s" % os.path.join(PROD, a.rj))
    outdir = a.out or os.path.join(ROOT, "data", "tmp", "review")
    os.makedirs(outdir, exist_ok=True)
    html = (HTML.replace("__RJ__", a.rj)
                .replace("__DATA__", json.dumps(rows, ensure_ascii=False))
                .replace("__LOCKS__", json.dumps(locks, ensure_ascii=False)))
    path = os.path.join(outdir, a.rj + ".html")
    open(path, "w", encoding="utf-8").write(html)
    flagged = sum(1 for r in rows if r["flags"])
    print("%s：%d 行（可疑 %d，已锁 %d）→ %s"
          % (a.rj, len(rows), flagged, len(locks), path))


if __name__ == "__main__":
    main()
