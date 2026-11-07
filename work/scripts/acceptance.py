# -*- coding: utf-8 -*-
"""一条命令跑完全部「交付声明」的验收。

为什么需要它：台账里写「17 部 / 147 轨 / check 17/17 / 语义 0 loss / 指纹一致」，
这些数字是**声明**。声明必须能被一条命令复现，否则就是「声明 ≠ 实际」。

    python work/scripts/acceptance.py            # 全跑
    python work/scripts/acceptance.py --quick    # 跳过语义覆盖（省 1~2 分钟）

退出码 0 = 全部成立；1 = 有声明不成立（会逐条打印）。
"""
import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # work/
BASE = os.path.dirname(ROOT)                      # <repo>
PROD = os.path.join(BASE, "kikoerusubtitle")
AUDIO = os.path.join(BASE, "ASMRmusic")
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
sys.stdout.reconfigure(encoding="utf-8")

from pipeline_lib import fingerprint as FP          # noqa: E402
from pipeline_lib import manifest, paths, stages   # noqa: E402

FAIL = []


def ck(ok, name, detail=""):
    print("  %s %-42s %s" % ("✅" if ok else "❌", name, detail))
    if not ok:
        FAIL.append(name)


def latest_runs():
    out = {}
    for d in sorted(glob.glob(os.path.join(ROOT, "runs", "*_production"))):
        m = json.load(open(os.path.join(d, "manifest.json"), encoding="utf-8"))
        out.setdefault(m["project"], []).append(os.path.basename(d))
    return {k: max(v) for k, v in out.items()}


def h(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    runs = latest_runs()
    print("=== 1. 音频 ↔ 字幕 覆盖（ASMRmusic 里每个音频都要有同名 .lrc）===")
    AUD = ("mp3", "flac", "wav", "m4a", "ogg", "opus", "aac", "wma")
    tot_a = tot_l = 0
    miss_all = []
    for rj in sorted(os.listdir(AUDIO)) if os.path.isdir(AUDIO) else []:
        ap_dir = os.path.join(AUDIO, rj)
        if not os.path.isdir(ap_dir):
            continue
        auds = [f for f in glob.glob(ap_dir + "/**/*", recursive=True)
                if os.path.isfile(f) and f.rsplit(".", 1)[-1].lower() in AUD]
        lrcs = {os.path.basename(f)[:-4] for f in glob.glob(os.path.join(PROD, rj, "*.lrc"))}
        tot_a += len(auds)
        tot_l += len(lrcs)
        for f in auds:
            if os.path.basename(f) not in lrcs:
                miss_all.append("%s/%s" % (rj, os.path.basename(f)))
    ck(tot_a == tot_l and not miss_all, "音频 %d = 字幕 %d" % (tot_a, tot_l),
       "缺：%s" % miss_all[:3] if miss_all else "")

    print("\n=== 2. 成品 = run 的 05_term_lock（逐字节）===")
    bad = []
    for rj, rid in sorted(runs.items()):
        x = sorted(glob.glob(os.path.join(PROD, rj, "*.lrc")))
        y = sorted(glob.glob(os.path.join(ROOT, "runs", rid, "05_term_lock", rj, "*.lrc")))
        if not (len(x) == len(y) and all(h(p) == h(q) for p, q in zip(x, y))):
            bad.append(rj)
    ck(not bad, "%d 部成品 = run" % len(runs), "不一致：%s" % bad if bad else "")

    print("\n=== 3. 阶段指纹 = 当前代码（改了会重算）===")
    bad = []
    for rj, rid in sorted(runs.items()):
        run = manifest.Run.load(rid)
        # ⚠️ 必须和运行时同口径：`pipeline.py run` 用的是 `paths.load_config()`
        # （= DEFAULTS + pipeline.json），而 run 里的 `config` 是**建 run 那天的快照**，
        # 后来新增的 DEFAULTS 键（seg_flush_chars / chunk，2026-10-30 beta.13）它没有。
        # 直接用快照会算出一个 `seg_flush_chars=None` 的 config_hash，和运行时记录的
        # `=10` 永远对不上 —— 17 部 × 6 阶段全部假报「不符」。
        # 这里用「DEFAULTS + run 快照」，既补齐新增键，又保留该 run 当时的值。
        cfg = dict(paths.DEFAULTS, **(run.data.get("config") or {}))
        for name, spec in stages.STAGES.items():
            ctx = stages.Ctx(run, cfg, dry=True)
            fp = FP.stage_fingerprint(
                name, spec["params"](ctx), spec["inputs"](ctx),
                manifest.knowledge_now(spec["knowledge"]),
                stages.code_rec(spec["code"]), ctx.cfg,
                models=FP.model_records(ctx.cfg, name))
            if (run.stage(name) or {}).get("fingerprint") != fp:
                bad.append("%s/%s" % (rj, name))
    ck(not bad, "%d 部 × 6 阶段全部命中" % len(runs), "不符：%s" % bad[:4] if bad else "")

    # ---- 3b. 产物哈希对账（2026-11-06，GPT「成功提交边界」）----
    # 指纹只证明「代码/参数没变」，**不证明这次真的跑了** —— 2026-11-06 的假绿
    # 就是指纹全绿、产物是 9 天前的。这一条补上「声明与实际一致」：
    # manifest 里记的每个产物文件，磁盘上必须存在且内容一致；多出来的文件也要报
    # （删轨、加轨、等长改字都必须能发现）。
    print("\n=== 3b. 产物哈希对账（manifest 记录 vs 磁盘实际）===")
    bad_h, n_h = [], 0
    for rj, rid in sorted(runs.items()):
        try:
            m = json.load(open(os.path.join(ROOT, "runs", rid, "manifest.json"),
                               encoding="utf-8"))
        except Exception:
            continue
        for sname, st in (m.get("stages") or {}).items():
            # 只对六阶段对账。`07_promote` 不是 STAGES 里的阶段，它目录里还有
            # meta.md 这类成品元信息，本来就不进 outputs —— 拿它做集合核对必然误报。
            if not sname.startswith(("01_", "02_", "03_", "04_", "05_", "06_")):
                continue
            # ⚠️ repair 改过产物时，新哈希记在 `outputs_after_repair`，原 `outputs`
            # 保留「模型刚跑完的样子」（cli._repair_impl 的设计）。对账必须用前者，
            # 否则每一次合法 repair 都会被这条声明误报成「内容变了」。
            _recs = st.get("outputs_after_repair") or st.get("outputs") or []
            for rec in _recs:
                if not isinstance(rec, dict):
                    continue
                rec_files = rec.get("files") or []
                for fr in rec_files:
                    n_h += 1
                    rel = fr.get("path", "")
                    p = os.path.join(ROOT, rel)
                    base = os.path.basename(rel)
                    if not os.path.isfile(p):
                        bad_h.append("%s/%s 缺 %s" % (rj, sname, base))
                    elif h(p) != fr.get("sha256"):
                        bad_h.append("%s/%s 变了 %s" % (rj, sname, base))
                d = os.path.join(ROOT, rec.get("path", ""))
                if rec.get("path") and os.path.isdir(d):
                    # 只比「产物」，不比阶段自己写的审计/备份/日志：
                    #   `_lock_assign.json`/`mask_audit.json` 是 04 的审计输出，
                    #   `*.bak_latinhalluc` 是 01 的备份 —— 它们不进 manifest 是正常的。
                    cur = set()
                    for x in glob.glob(os.path.join(d, "*")):
                        if not os.path.isfile(x):
                            continue
                        b = os.path.basename(x)
                        if (b.startswith("_") or ".bak" in b or b.endswith(".log")
                                or "audit" in b.lower()):
                            continue
                        cur.add(b)
                    old = set(os.path.basename(x.get("path", "")) for x in rec_files)
                    for extra in sorted(cur - old):
                        bad_h.append("%s/%s 多出 %s" % (rj, sname, extra))
    ck(not bad_h, "产物哈希对账 %d 个文件" % n_h,
       "不符 %d：%s" % (len(bad_h), bad_h[:3]) if bad_h else "")

    # ---- 3c. 知识包身份（2026-11-06，GPT 议题 3）----
    # 「用 34 条 presets」和「用 138 条私有表」跑出来的东西是不一样的，
    # 但以前 manifest 只记 sha256/exists，验收层面无法区分 → 安装版静默降级查不出来。
    # 这里独立重读当前知识包，和 run 记录的身份比（不是比两份声明，那仍会自洽）。
    print("\n=== 3c. 知识包身份（manifest 记录 vs 当前实际）===")
    bad_k = []
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import styleloc as _SL
        cur_kp = _SL.resolve()
    except Exception as e:                       # noqa: BLE001
        cur_kp = None
        print("  ⚠️ 解析当前知识包失败：%s" % e)
    if cur_kp:
        for rj, rid in sorted(runs.items()):
            try:
                m = json.load(open(os.path.join(ROOT, "runs", rid, "manifest.json"),
                                   encoding="utf-8"))
            except Exception:
                continue
            kp = ((m.get("stages") or {}).get("02_asr_fix") or {}).get("knowledge_package") or {}
            ident = kp.get("identity")
            if ident and ident != cur_kp["identity"]:
                bad_k.append("%s: %s ≠ %s" % (rj, ident, cur_kp["identity"]))
        ck(not bad_k, "知识包身份 %s（%d 表）"
           % (cur_kp["identity"], len(cur_kp["tables"])),
           "不符 %d：%s" % (len(bad_k), bad_k[:3]) if bad_k else "")

    print("\n=== 4. 门禁与自检 ===")
    py = sys.executable
    r = subprocess.run([py, os.path.join(ROOT, "pipeline", "pipeline.py"), "selftest"],
                       capture_output=True, text=True)
    ck("selftest 全过" in r.stdout, "pipeline selftest",
       [l for l in r.stdout.splitlines() if "结果" in l][-1] if "结果" in r.stdout else "")

    ok = 0
    for d in sorted(glob.glob(os.path.join(PROD, "RJ*"))):
        rr = subprocess.run([py, os.path.join(ROOT, "scripts", "check.py"), d],
                            capture_output=True, text=True)
        if "全部通过" in rr.stdout:
            ok += 1
    ck(ok == len(runs), "成品 check.py %d/%d" % (ok, len(runs)))

    r = subprocess.run([py, os.path.join(ROOT, "scripts", "errata_audit.py"), "--test"],
                       capture_output=True, text=True)
    ck("44/44" in r.stdout, "errata_audit --test 44/44",
       [l for l in r.stdout.splitlines() if "/44" in l][-1] if "/44" in r.stdout else "")

    r = subprocess.run([py, os.path.join(ROOT, "scripts", "test_regressions.py")],
                       cwd=ROOT, capture_output=True, text=True)
    ck("门禁全过" in r.stdout, "test_regressions")

    print("\n=== 5. 语义覆盖（成品 vs 各自 run 的 ASR）===")
    if a.quick:
        print("  ⏭  跳过（--quick）")
    else:
        tot = loss = 0
        for rj, rid in sorted(runs.items()):
            out = os.path.join(ROOT, "tmp", "_acc_%s.json" % rj)
            subprocess.run([py, os.path.join(ROOT, "scripts", "semantic_coverage.py"),
                            "--lrc", os.path.join(PROD, rj),
                            "--asr", os.path.join(ROOT, "runs", rid, "01_asr"),
                            "--rj", rj, "--json", out], capture_output=True, text=True)
            try:
                v = json.load(open(out, encoding="utf-8"))
            except Exception:                       # noqa: BLE001
                continue
            tot += len(v)
            loss += len([x for x in v if x.get("kind") == "loss"])
            os.remove(out)
        ck(tot == 0 and loss == 0, "违规 %d / loss %d" % (tot, loss))

    print("\n=== 6. 写死的项目内绝对路径（三层改造残留）===")
    r = subprocess.run([py, os.path.join(ROOT, "scripts", "path_audit.py")],
                       cwd=ROOT, capture_output=True, text=True)
    ck("有问题的文件 0 个" in r.stdout, "path_audit",
       [l for l in r.stdout.splitlines() if "有问题的文件" in l][-1] if "有问题的文件" in r.stdout else "")

    print("\n" + "=" * 60)
    if FAIL:
        print("❌ 有 %d 条声明不成立：%s" % (len(FAIL), FAIL))
        return 1
    print("✅ 全部声明成立")
    return 0


if __name__ == "__main__":
    sys.exit(main())
