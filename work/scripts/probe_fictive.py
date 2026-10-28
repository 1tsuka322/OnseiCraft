# -*- coding: utf-8 -*-
r"""虚构术语探针（2026-10-25，T5-B：轨级记忆的**因果**证据，四臂对照）。

它回答什么
----------
mem_conformance.py 只能给相关性（记忆写了 X、成品恰好有 X）——但「おっぱい→胸部」
模型本来就可能这么译。要证明**模型真的在读记忆**，得用**来源文本无法决定的虚构词**：
把一句真实 cue 里的普通名词换成生造词「ラネク」，四臂只改记忆内容：

| 臂           | 记忆内容            | 预期（若模型读记忆）        |
|--------------|---------------------|-----------------------------|
| correct      | ラネク→岚核         | 探针句出「岚核」             |
| counter      | ラネク→澜刻（反事实）| 探针句出「澜刻」             |
| irrelevant   | ゾルミ→索露米（同格式、无关词）| 探针句两词都不出   |
| none         | 无记忆文件          | 探针句两词都不出             |

correct 与 counter 的差 = 记忆的**因果**效应；irrelevant/none 是「本来就不会这么译」
的基线。每臂跑 2 次（T4 修复后同臂应逐字节一致；不一致的 cue 标 unstable 不判读）。
「无关 cue 是否被动」：其余行与 none 臂逐行比，差异应在 0（同温度贪心 + 同输入）。

为什么先 --dry-run：四臂 × 2 跑是 GPU 实验，先把「换词是否落对位置、记忆是否
真能被 load_mem 渲染进 prompt、命令是否拼对」全部自证一遍，人确认了再上 GPU。

用法：
  python probe_fictive.py --dry-run --out $KIKOERU_ROOT/data/tmp/r125
  python probe_fictive.py --run    --out ...（实际执行，会起 llama-server，慎用）
"""
import os
import sys
import glob
import json
import argparse
import subprocess

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DRAFT = os.path.join(ROOT, "data", "draft")
PY = sys.executable

# 虚构词（ja, zh_correct, zh_counter）。上 GPU 前必须全库自检 0 命中——
# 若语料里碰巧出现过，模型「见过这个词」会污染「只有记忆能决定译法」的前提。
FICTIVE = [("ラネク", "岚核", "澜刻"), ("ゾルミ", "索露米", "佐露弥"), ("ハヴェン", "哈文", "哈薇")]
PROBE_JA = "ラネク"           # 注入到 ASR 文本里的虚构词（correct/counter 的记忆都教它）

# 探针轨：(RJ, asr 文件名, 被替换的普通名词)。名词选「词面完整出现在 words[] 里」的，
# 替换后 cue 文本才会真正带上虚构词（跨词边界的名词替换会静默不生效，见 substitute 的守卫）。
TRACKS = [
    ("RJ9000007", "00_プロローグ：虎のようで、捨て猫のような.json", "猫"),
    ("RJ9000001", "00.姉上と山奥生活.mp3.json", "雪"),
]

# 四臂记忆（None = 不写 track_memory，load_mem() 返回空串 = 真·无记忆；
# 写个 terms:[] 的空文件反而会注入一行孤零零的【本轨背景】标题，不是「空」）。
ARMS = [
    ("correct", [(PROBE_JA, FICTIVE[0][1])]),
    ("counter", [(PROBE_JA, FICTIVE[0][2])]),
    ("irrelevant", [(FICTIVE[1][0], FICTIVE[1][1])]),
    ("none", None),
]

EVAL_ENV = ["PYTHONHASHSEED=0", "KIKOERU_SLOTS=1", "KIKOERU_TEMP=0",
            "KIKOERU_TRACK_MEMORY=1", "KIKOERU_CUE_PIPELINE=1"]


def die(msg):
    print("错误：%s" % msg, file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------- 虚构词自检
def fictive_selfcheck():
    """在 parallel.json 与 draft/*/asr*/*.json 里数虚构词命中数（读文件，不写）。

    任何一个 >0 都会污染因果前提（模型可能在语料里见过）→ 直接报错换词，别硬用。
    大文件按块流式读（重叠 = 词长-1），避免整读 60MB 语料。
    """
    words = [w for tup in FICTIVE for w in tup]
    targets = [os.path.join(ROOT, "data", "style", "parallel.json")]
    targets += sorted(glob.glob(os.path.join(DRAFT, "*", "asr*", "*.json")))
    hits = {w: 0 for w in words}
    n_files = 0
    for p in targets:
        n_files += 1
        try:
            f = open(p, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        overlap = max(len(w) for w in words) - 1
        tail = ""
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            buf = tail + chunk
            for w in words:
                hits[w] += buf.count(w)
            tail = buf[-overlap:]
        f.close()
    print("虚构词自检：%d 个文件（parallel.json + draft/*/asr*/*.json）" % n_files)
    bad = {w: n for w, n in hits.items() if n > 0}
    for w in words:
        print("  %-6s 命中 %d" % (w, hits[w]))
    if bad:
        die("虚构词已被语料使用（%s）——换词，别硬用" % bad)


# ---------------------------------------------------------------- 换词
def substitute(d, noun, fictive):
    """把第一条「文本含 noun 且 noun 是完整词面（或无词级数据）」的 segment 换成虚构词。

    只换第一条：全轨只留一个探针点，其余句保持真实文本，正好用来测
    「记忆是否影响无关 cue」（irrelevant/none 臂的对照价值就在这）。
    词面不完整（noun 跨 words[] 边界）就跳过该段找下一条——build_units 按 words
    拼文本，只改 text 不改 words 会静默不生效。
    """
    for s in d.get("segments") or []:
        t = s.get("text") or ""
        if noun not in t:
            continue
        ws = s.get("words") or []
        idx = next((i for i, w in enumerate(ws) if (w.get("w") or "") == noun), None)
        if ws and idx is None:
            continue
        if ws:
            ws[idx]["w"] = fictive
        s["text"] = t.replace(noun, fictive, 1)
        return {"start": s["start"], "end": s["end"], "old": t, "new": s["text"]}
    return None


# ---------------------------------------------------------------- prompt 预览
def mem_snippet(arm_asr_dir, src):
    """用 polish.load_mem 渲染该臂真实注入的【本轨背景】片段（与生产逐字一致）。"""
    os.environ["KIKOERU_TRACK_MEMORY"] = "1"
    try:
        sys.path.insert(0, HERE)
        import polish
        return polish.load_mem(arm_asr_dir, src)
    except Exception as e:
        return "（load_mem 预览失败：%s）" % e


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="虚构术语四臂探针（默认只做准备与自证）")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "tmp", "r125"),
                    help="中间产物根目录（默认 data/tmp/r125）")
    ap.add_argument("--dry-run", action="store_true", help="只做自检/换词/写副本/打印命令，不调 LLM")
    ap.add_argument("--run", action="store_true", help="真正执行（起 llama-server，GPU，慎用）")
    a = ap.parse_args()
    if not a.dry_run and not a.run:
        die("必须指定 --dry-run 或 --run 之一")
    if a.dry_run and a.run:
        die("--dry-run 与 --run 互斥")

    fictive_selfcheck()

    # ---- 1) 换词，生成各轨探针 ASR（四臂共享同一份换词结果，臂间只差记忆） ----
    probes = []
    for rj, fname, noun in TRACKS:
        p = os.path.join(DRAFT, rj, "asr", fname)
        if not os.path.exists(p):
            die("ASR 不存在：%s" % p)
        d = json.load(open(p, encoding="utf-8"))
        rec = substitute(d, noun, PROBE_JA)
        if rec is None:
            die("%s/%s 里没找到「词面完整」的名词 %r，换一个名词" % (rj, fname, noun))
        src = d["source"]
        probes.append({"rj": rj, "file": fname, "src": src, "noun": noun,
                       "start": rec["start"], "end": rec["end"],
                       "old": rec["old"], "new": rec["new"], "data": d})
        print("探针轨 %s：%s" % (rj, src))
        print("  [%.1fs] %s" % (rec["start"], rec["old"]))
        print("      →  %s   （%s → %s）" % (rec["new"], noun, PROBE_JA))

    # ---- 2) 写四臂目录：asr 副本 + track_memory（none 臂不写记忆文件） ----
    manifest = []
    for arm, terms in ARMS:
        arm_dir = os.path.join(a.out, arm)
        asr_dir = os.path.join(arm_dir, "asr")
        os.makedirs(asr_dir, exist_ok=True)
        for pr in probes:
            with open(os.path.join(asr_dir, pr["file"]), "w", encoding="utf-8") as f:
                json.dump(pr["data"], f, ensure_ascii=False)
            if terms is not None:
                tmd = os.path.join(arm_dir, "track_memory")
                os.makedirs(tmd, exist_ok=True)
                # 文件名 = <source>.json（source 含 .mp3）：与生产 track_memory 命名一致，
                # load_mem 的精确路径/glob 兜底两条路都能命中。
                mem = {"rj": pr["rj"], "track": pr["src"], "terms":
                       [{"ja": ja, "zh": zh, "n": 1} for ja, zh in terms]}
                with open(os.path.join(tmd, pr["src"] + ".json"), "w", encoding="utf-8") as f:
                    json.dump(mem, f, ensure_ascii=False, indent=1)
            manifest.append({"arm": arm, "rj": pr["rj"], "file": pr["file"], "src": pr["src"],
                             "terms": terms,
                             "probe": {"start": pr["start"], "end": pr["end"],
                                       "old": pr["old"], "new": pr["new"]}})
        snippet = mem_snippet(asr_dir, probes[0]["src"])
        print("\n臂 %-10s 记忆 terms=%s" % (arm, terms))
        print("  load_mem 注入片段（%s）：\n%s" % (probes[0]["src"][:40], snippet.rstrip() or "  （空 = 不注入）"))

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "probe_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print("\n已写入 %s/probe_manifest.json（判读用：探针位置 + 各臂记忆）" % a.out)

    # ---- 3) 命令（Windows cmd 形式；--run 时由本脚本代跑同样的东西） ----
    env_prefix = "set " + "&& set ".join('"%s"' % v for v in EVAL_ENV) + "&& "
    cmds = []
    cmds.append(("初译引擎", '"%s" "%s" start --hymt' % (PY, os.path.join(HERE, "serve.py"))))
    for arm, _terms in ARMS:
        arm_dir = os.path.join(a.out, arm)
        cmds.append(("初译 %s" % arm,
                     'set "KIKOERU_ENGINE=hymt"&& set "KIKOERU_CUE_PIPELINE=1"&& "%s" "%s" "%s" "%s"'
                     % (PY, os.path.join(HERE, "translate.py"),
                        os.path.join(arm_dir, "asr"), os.path.join(arm_dir, "init"))))
    cmds.append(("切回润色模型", '%s "%s" stop && %s "%s" start' % (PY, os.path.join(HERE, "serve.py"),
                                                                 PY, os.path.join(HERE, "serve.py"))))
    for arm, _terms in ARMS:
        arm_dir = os.path.join(a.out, arm)
        for run in ("run1", "run2"):
            cmds.append(("润色 %s/%s" % (arm, run),
                         '%s"%s" "%s" "%s" "%s" "%s" --force'
                         % (env_prefix, PY, os.path.join(HERE, "polish.py"),
                            os.path.join(arm_dir, "asr"), os.path.join(arm_dir, "init"),
                            os.path.join(arm_dir, "out_" + run))))
    print("\n将要执行的命令（%s）：" % ("dry-run 只打印不执行" if a.dry_run else "即将执行"))
    for tag, c in cmds:
        print("  [%s] %s" % (tag, c))
    judge_cmd = '"%s" "%s" --judge --out %s' % (PY, os.path.abspath(__file__), a.out)
    print("  [判读] %s" % judge_cmd)

    if a.dry_run:
        print("\n--dry-run 完成：未调 LLM、未写 style/。人工核对上面的换词与注入片段后，"
              "去掉 --dry-run 加 --run 再跑。")
        return

    # ---- 4) 真跑（--run）：串行执行上面的命令 ----
    for tag, c in cmds:
        print("\n==== %s ====" % tag, flush=True)
        r = subprocess.run(c, shell=True)
        if r.returncode != 0:
            die("步骤失败（%s），退出码 %d" % (tag, r.returncode))
    r = subprocess.run(judge_cmd, shell=True)
    sys.exit(r.returncode)


# ---------------------------------------------------------------- 判读
def judge(out_root):
    """四臂 ×2 跑完后的判读：探针句译法比例 + 无关 cue 被动量 + run1/run2 稳定性。

    判读口径：
    · 探针行 = out_<run>/<src>.lrc 里时间戳落在 [probe.start-0.3, probe.end+0.3) 的行；
    · correct 臂看含「岚核」比例，counter 看「澜刻」，irrelevant/none 看任一虚构译法（预期 0）；
    · 同臂 run1 vs run2 不一致的行 → unstable，不计入判读（T4 修复后预期 0）；
    · 无关行被动 = 各臂 run1 的非探针行与 none 臂 run1 逐行（按时间戳配对）比不同的行数。
    """
    man = json.load(open(os.path.join(out_root, "probe_manifest.json"), encoding="utf-8"))
    zh_of = {"correct": "岚核", "counter": "澜刻"}

    def read_rows(p):
        import re
        rows = []
        for ln in open(p, encoding="utf-8"):
            m = re.match(r"\[(\d+):(\d+\.\d+)\](.*)", ln.strip())
            if m:
                rows.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
        return rows

    report = {"probes": [], "unstable": {}, "collateral": {}}
    for m in man:
        if m["arm"] != man[0]["arm"]:
            continue
        pr = m["probe"]
        per_arm = {}
        for arm, _t in ARMS:
            hits, probe_rows = {w: 0 for tup in FICTIVE for w in tup[1:]}, []
            unstable_n = 0
            rows_by_run = {}
            for run in ("run1", "run2"):
                lrc = os.path.join(out_root, arm, "out_" + run, m["src"] + ".lrc")
                if not os.path.exists(lrc):
                    die("缺成品：%s（先跑 --run）" % lrc)
                rows_by_run[run] = read_rows(lrc)
            r1, r2 = rows_by_run["run1"], rows_by_run["run2"]
            if len(r1) != len(r2):
                unstable_n = abs(len(r1) - len(r2))
            else:
                unstable_n = sum(1 for x, y in zip(r1, r2) if x != y)
            report["unstable"]["%s/%s" % (arm, m["src"][:24])] = unstable_n
            for t, txt in r1:
                if pr["start"] - 0.3 <= t < pr["end"] + 0.3 and txt:
                    probe_rows.append((t, txt))
                    for w in hits:
                        if w in txt:
                            hits[w] += 1
            key = zh_of.get(arm)
            per_arm[arm] = {
                "probe_lines": len(probe_rows),
                "probe_texts": [x[1] for x in probe_rows][:4],
                "fictive_hits": hits,
                "signal": (hits[key] if key else sum(hits.values())),
            }
        # 无关行被动：各臂 run1 vs none run1（按时间戳配对，排除探针窗）
        base = {t: x for t, x in read_rows(os.path.join(out_root, "none", "out_run1", m["src"] + ".lrc"))}
        for arm, _t in ARMS:
            if arm == "none":
                continue
            rows = read_rows(os.path.join(out_root, arm, "out_run1", m["src"] + ".lrc"))
            diff = sum(1 for t, x in rows if x and not (pr["start"] - 0.3 <= t < pr["end"] + 0.3)
                       and base.get(t, "") != x)
            report["collateral"]["%s/%s" % (arm, m["src"][:24])] = diff
        report["probes"].append({"src": m["src"], "probe": pr, "arms": per_arm})

    print("==== 判读 ====")
    for p in report["probes"]:
        print("探针 %s [%.1fs] %s" % (p["src"][:40], p["probe"]["start"], p["probe"]["new"]))
        for arm, v in p["arms"].items():
            print("  %-10s 探针行 %d · 信号命中 %d · 明细 %s · 样例 %s"
                  % (arm, v["probe_lines"], v["signal"],
                     {k: n for k, n in v["fictive_hits"].items() if n},
                     v["probe_texts"][:1]))
    print("unstable（run1≠run2 行数，预期 0）：%s" % report["unstable"])
    print("无关行被动（vs none 臂，预期 0）：%s" % report["collateral"])
    with open(os.path.join(out_root, "judge_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("已写入 %s/judge_report.json" % out_root)


if __name__ == "__main__":
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--judge", action="store_true")
    _ap.add_argument("--out")
    _ns, _ = _ap.parse_known_args()
    if _ns.judge and _ns.out:
        judge(_ns.out)
    else:
        main()
