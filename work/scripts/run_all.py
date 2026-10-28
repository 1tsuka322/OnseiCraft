# -*- coding: utf-8 -*-
r"""
⚠️ 2026-10-26 起：**生产请用 `pipeline/pipeline.py`**（有 manifest/指纹/断点续跑/门禁）。
本脚本保留作「单步调试 + 快速手跑」，不再作为交付入口（见 docs/PIPELINE.md）。

一键流水线：识别 -> 翻译 -> 风格润色 -> 校验 -> 输出到 kikoerusubtitle/<名称>/

用法：
  python run_all.py <音频目录> <输出文件夹名(通常=作品RJ号)> [--ext wav|mp3|auto]
                    [--recursive] [--style DIR] [--only asr|translate|polish]
                    [--mode retranslate]

例：
  python run_all.py "$KIKOERU_ROOT/RJ9000005/wav" RJ9000005 --ext wav

目录约定：
  中间产物  $KIKOERU_ROOT/data/draft/<名称>/asr      识别日文 JSON
            $KIKOERU_ROOT/data/draft/<名称>/draft    机器初译 LRC
  最终字幕  $KIKOERU_ROOT/kikoerusubtitle/<名称>/<文件名>.lrc
  风格资料  $KIKOERU_ROOT/data/style（由 kikoeru-style-learn 生成）
"""
import os
import sys
import rootloc  # noqa: E402  仓库三层结构：work/ 里，成品/音频在上一级
sys.stdout.reconfigure(encoding="utf-8")
import glob
import time
import argparse
import json
import subprocess

PY = sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("KIKOERU_ROOT", os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import styleloc  # noqa: E402
DEFAULT_STYLE = styleloc.style_dir()


def run(args, title):
    print("\n" + "=" * 60)
    print("[%s] %s" % (title, " ".join(args)))
    print("=" * 60, flush=True)
    t0 = time.time()
    r = subprocess.run([PY] + args, cwd=HERE)
    print("--- %s 用时 %.1f 分钟 ---" % (title, (time.time() - t0) / 60), flush=True)
    if r.returncode != 0:
        print("步骤失败：%s（退出码 %d）" % (title, r.returncode))
        sys.exit(r.returncode)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("name")
    ap.add_argument("--ext", default="auto")
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--style", default=DEFAULT_STYLE)
    ap.add_argument("--only", default="")
    ap.add_argument("--mode", default="", choices=["", "retranslate"],
                    help="retranslate = 只跑 翻译+润色（等价 --only translate,polish）。"
                         "术语表/风格库/润色 prompt 改动后重出译文用；"
                         "识别与台本打轴吃缓存，不重跑。与 --only 互斥。")
    ap.add_argument("--diarize", action="store_true",
                    help="asr 后跑声纹聚类（diarize.py，CPU，与 whisper 不冲突）："
                         "给 segments[].speaker 打 SPEAKER_xx 标签，"
                         "供「换人→人称重置」的归属提示用（LRC 不输出编号）")
    ap.add_argument("--diarize-thr", type=float, default=0.30,
                    help="层次聚类余弦阈值。0.50 实测会把双角色并成 1 类；"
                         "0.30 才能分出 2 类（鬼娘二人 222:55 段）")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute", default="float16")
    ap.add_argument("--script-dir", default="",
                    help="台本目录（有则走「打轴」路径：ASR 只当时间锚点，文本用台本）")
    a = ap.parse_args()

    draft = os.path.join(ROOT, "data", "draft", a.name)
    asr_dir = os.path.join(draft, "asr")
    draft_dir = os.path.join(draft, "draft")
    out_dir = os.path.join(rootloc.PROD, a.name)
    # 有台本时：ASR 原始结果单独放 asr_raw（当时间锚点，保留可复查），
    # 打轴产物写回 asr/ —— 下游 translate/polish 完全不用改。
    has_script = bool(a.script_dir) and os.path.isdir(a.script_dir)
    asr_raw_dir = os.path.join(draft, "asr_raw")
    asr_target = asr_raw_dir if has_script else asr_dir
    for d in (asr_dir, asr_target, draft_dir, out_dir):
        os.makedirs(d, exist_ok=True)

    steps = [s.strip() for s in a.only.split(",") if s.strip()]
    if a.mode == "retranslate":
        if steps:
            ap.error("--mode retranslate 与 --only 互斥（retranslate 已固定为 translate,polish）")
        if a.diarize:
            print("提示：--mode retranslate 跳过声纹聚类（需要 speaker 标签请单独跑 diarize）")
            a.diarize = False
        steps = ["translate", "polish"]
    elif not steps:
        steps = (["asr", "diarize", "script_align", "translate", "polish", "check"] if has_script
                 else ["asr", "translate", "polish", "check"])
    if a.diarize and "diarize" not in steps:
        # 插到 asr（或 script_align）之后、translate 之前
        k = next((i for i, s in enumerate(steps) if s in ("script_align", "asr")), -1)
        steps.insert(k + 1, "diarize")
    elif not a.diarize and "diarize" in steps:
        steps = [s for s in steps if s != "diarize"]
    print("音频源 : %s" % a.src)
    print("输出   : %s" % out_dir)
    print("风格   : %s" % a.style)
    print("台本   : %s" % (a.script_dir if has_script else "（无，走正常 ASR 流程）"))
    print("步骤   : %s" % " -> ".join(steps))
    if a.diarize:
        print("声纹   : THR=%.2f（说话人分桶 → 换人提示；LRC 不输出编号）" % a.diarize_thr)

    if "asr" in steps:
        args = [os.path.join(HERE, "asr.py"), a.src, asr_target, a.ext,
                "--device", a.device, "--compute", a.compute]
        if a.recursive:
            args.append("--recursive")
        run(args, "识别日文" + ("（当时间锚点，文本稍后用台本替换）" if has_script else ""))
    if "script_align" in steps:
        run([os.path.join(HERE, "script_align.py"), a.script_dir,
             asr_raw_dir, asr_dir], "台本打轴（台本文本 + ASR 时间轴）")
    if "diarize" in steps:
        # C1（2026-10-21）：声纹聚类写 segments[].speaker=SPEAKER_xx。
        # 与 whisper 不冲突（纯 CPU，模型已在 data/models/spkrec-ecapa）。
        # 台本作品跳过——script_align 已带真实角色名，聚类只会覆盖它。
        if not has_script:
            diarize_py = os.path.join(ROOT, "data", "skills",
                                      "kikoeru-style-learn", "scripts", "diarize.py")
            run([diarize_py, asr_dir, a.src, "--device", "cpu",
                 "--thr", str(a.diarize_thr)], "声纹聚类（说话人分桶）")
        else:
            print("\n[声纹聚类] 跳过：台本作品已有角色名")
    if "translate" in steps or "polish" in steps:
        run([os.path.join(HERE, "serve.py"), "start"], "启动推理服务")
    # 已知错字表自动落地（2026-10-25）：规则 C 覆盖「两版同错」。
    # A1/A2/A3 全靠两版不一致 —— 两版一致地错时它们全都看不见（`写生`/`処世` 漏了整年）。
    # 只做确定性替换、只在 asr_fix/<track>.json 不存在时生成（人工确认优先）。
    if "translate" in steps or "polish" in steps:
        try:
            sys.path.insert(0, HERE)
            import asr_fix_scan as _AF
            _n = _AF.auto_apply_errata(draft, asr_dir, os.path.join(draft, "asr_fix"))
            if _n:
                print("\n[ASR 改错] 已知错字表自动落地 %d 处（asr_errata.json）" % _n)
            # T8（2026-10-26）：把 auto_apply_errata 的运行时监控打出来——
            # 只报「自动改了几处」等于没有监控：新语境（错形出现但 ctx 白名单没覆盖）
            # 与作用域外跳过必须可见，否则表升级后拦了多少、漏了多少都是黑盒。
            # 新语境只落盘给人工/Gemini 判，**不自动改**（R133 协议）。
            _s = _AF.errata_stats(reset=True)
            if _s["applied"] or _s["new_ctx"] or _s["scope_skip"]:
                print("[ASR 改错] 自动改 %d 处；ctx 命中 %d；新语境 %d（未改，需 review）；"
                      "作用域外 %d；反例拦下 %d"
                      % (_s["applied"], _s["ctx_hit"], len(_s["new_ctx"]),
                         len(_s["scope_skip"]), len(_s["boundary_skip"])))
                if _s["new_ctx"]:
                    _p = os.path.join(draft, "errata_new_ctx.json")
                    json.dump(_s["new_ctx"], open(_p, "w", encoding="utf-8"),
                              ensure_ascii=False, indent=1)
                    print("           新语境已落盘：%s" % _p)
            # 绿灯（同读音+双侧汉字+同轨锚点）：R112 实测 23/23 全真，可自动落
            if os.environ.get("KIKOERU_AUTOFIX_GREEN", "1") == "1":
                _g = _AF.auto_apply_green(draft, asr_dir, os.path.join(draft, "asr_fix"))
                if _g:
                    print("[ASR 改错] 绿灯自动落地 %d 处（同读音+双侧汉字+锚点）" % _g)
        except Exception as _e:
            print("\n[ASR 改错] 错字表自动落地失败（不影响主流程）：%s" % _e)
    # ASR 改错阶段（2026-10-24）：asr_fix/ 里若有本轨修正版，翻译/润色优先用它。
    # 改错由 asr_fix_scan.py 产出（多版本互证 + 同轨重复 + 人工/Gemini 确认），带 fix_log 可审计。
    fix_dir = os.path.join(draft, "asr_fix")
    tr_asr_dir = asr_dir
    if os.path.isdir(fix_dir) and glob.glob(os.path.join(fix_dir, "*.json")):
        tr_asr_dir = fix_dir
        print("\n[ASR 改错] 使用修正版：%s（%d 个文件）"
              % (fix_dir, len(glob.glob(os.path.join(fix_dir, "*.json")))))
    if "translate" in steps:
        run([os.path.join(HERE, "translate.py"), tr_asr_dir, draft_dir], "机器初译")
    if "polish" in steps:
        run([os.path.join(HERE, "polish.py"), tr_asr_dir, draft_dir, out_dir, "--style", a.style],
            "风格润色")
    # 语义槽守卫（2026-10-25，R113/R117）：只读，跑完产一份抽样清单。
    # 为什么放在 check 之后：它查的是「润色层有没有把初译的语义槽洗掉」——
    # 否定/主语/数量/因果/术语 五类，规则实测精度只有 39%（R113b），所以**只报不改**，
    # 报告给人工/Gemini 复核用，绝不自动回退。
    if "polish" in steps and os.environ.get("KIKOERU_SLOT_GUARD", "1") != "0":
        try:
            import slot_guard as _SG
            _rows = _SG.scan_dir(tr_asr_dir, draft_dir, out_dir)
            if _rows:
                _rep = os.path.join(out_dir, "..", "slot_guard_report.json")
                json.dump(_rows, open(_rep, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                print("\n[语义槽守卫] 可疑 %d 条（真缺陷率约 39%%，只作抽样清单）→ %s"
                      % (len(_rows), os.path.normpath(_rep)))
        except Exception as _e:
            print("\n[语义槽守卫] 跳过：%s" % _e)
    if "check" in steps:
        run([os.path.join(HERE, "check.py"), out_dir, "--style", a.style], "质量校验")
        # 语义槽守卫（2026-10-25）：初译 vs 成品，标出「润色把语义洗掉」的可疑行。
        # 只读、不改成品；实测规则精度 39%（Gemini 复核 36 条：14 真缺陷/22 误报），
        # 所以定位是**抽样清单**，不是检测器。默认开，失败不影响主流程。
        if os.environ.get("KIKOERU_SLOT_GUARD", "1") == "1":
            try:
                run([os.path.join(HERE, "slot_guard.py"), tr_asr_dir, draft_dir, out_dir,
                     "--report", os.path.join(out_dir, "slot_report.json")],
                    "语义槽守卫（抽样清单）")
            except SystemExit:
                print("语义槽守卫失败（不影响成品）")

    print("\n完成：%s" % out_dir)


if __name__ == "__main__":
    main()
