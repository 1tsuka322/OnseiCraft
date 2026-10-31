# -*- coding: utf-8 -*-
"""一键导出诊断包：出错时让用户点一下就生成一个能直接发过来的 txt。

原则：**宁可多写，不要漏**。用户不会描述环境，我们也没法远程看，
所以把「版本 / 系统 / 路径 / 模型 / run 的 manifest / 各阶段日志 / 完整 UI 日志」
全塞进一个文件。体积几 MB 无所谓，能定位才重要。
"""
import json
import os
import platform
import sys
import time

from . import __version__, settings


def _sysinfo():
    lines = []
    lines.append("OnseiCraft %s" % __version__)
    lines.append("时间      : %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("系统      : %s %s (%s)" % (platform.system(), platform.release(),
                                            platform.version()))
    lines.append("架构      : %s" % platform.machine())
    lines.append("Python    : %s%s" % (sys.version.split()[0],
                                       "（打包版）" if getattr(sys, "frozen", False) else "（源码运行）"))
    try:
        from . import runner as R
        dev, comp = R.detect_device()
        lines.append("计算设备  : %s / %s" % (dev, comp))
    except Exception as e:
        lines.append("计算设备  : 检测失败 %s" % e)
    return lines


def _paths():
    lines = []
    cfg = settings.load_app()
    lines.append("程序目录  : %s" % settings.BASE)
    lines.append("数据目录  : %s" % settings.ROOT)
    for k in ("prod_dir", "import_dir", "asr_cache_dir", "feedback_dir"):
        lines.append("%-10s: %s" % (k, cfg.get(k, "")))
    lines.append("")
    lines.append("模型配置（pipeline.local.json）：")
    try:
        for k, label, p, ok, hint in settings.check_models():
            lines.append("  %-14s %-6s %s" % (k, "OK" if ok else "缺失", p or "（未设置）"))
    except Exception as e:
        lines.append("  读取失败：%s" % e)
    return lines


def _run_block(run_id):
    """把某个 run 的 manifest + 各阶段日志尾部拼进来。"""
    if not run_id:
        return ["（没有关联的 run）"]
    from . import settings as S
    d = os.path.join(S.ROOT, "runs", run_id)
    if not os.path.isdir(d):
        return ["（run 目录不存在：%s）" % d]
    out = ["run 目录：%s" % d, ""]
    mp = os.path.join(d, "manifest.json")
    if os.path.exists(mp):
        try:
            m = json.load(open(mp, encoding="utf-8"))
            out.append("--- manifest 摘要 ---")
            out.append("project : %s" % m.get("project"))
            out.append("status  : %s" % m.get("status"))
            for name, st in (m.get("stages") or {}).items():
                out.append("  %-14s %-8s %ss  %s" % (
                    name, st.get("status"), st.get("duration_sec"),
                    (st.get("error") or "")[:200]))
            if m.get("stages", {}).get("07_promote"):
                out.append("07_promote: %s" % json.dumps(
                    m["stages"]["07_promote"], ensure_ascii=False)[:1500])
        except Exception as e:
            out.append("manifest 解析失败：%s" % e)

    out.append("")
    out.append("--- 各阶段日志尾部（每份最多 400 行）---")
    for name in sorted(os.listdir(d)):
        sub = os.path.join(d, name)
        if not os.path.isdir(sub):
            continue
        for dp, _, fs in os.walk(sub):
            for f in fs:
                if not f.endswith((".log", ".txt")):
                    continue
                p = os.path.join(dp, f)
                try:
                    txt = open(p, encoding="utf-8", errors="replace").read()
                except Exception:
                    continue
                tail = "\n".join(txt.splitlines()[-400:])
                out.append("")
                out.append("### %s（%d 字节）" % (os.path.relpath(p, d), len(txt)))
                out.append(tail)
    return out


def build(run_id="", error="", logs=None, job=None):
    """拼一份完整的诊断文本。"""
    L = []
    L.append("=" * 70)
    L.append(" OnseiCraft 诊断报告")
    L.append("=" * 70)
    L.extend(_sysinfo())
    L.append("")
    L.append("--- 路径与模型 ---")
    L.extend(_paths())
    L.append("")
    if error:
        L.append("--- 错误 ---")
        L.append(str(error))
        L.append("")
    if job:
        L.append("--- 这次的任务 ---")
        for k in ("project", "source", "n_files", "minutes", "out_dir", "keep_asr"):
            if k in job:
                L.append("%-10s: %s" % (k, job.get(k)))
        L.append("")
    L.append("--- 运行记录 ---")
    L.extend(_run_block(run_id))
    L.append("")
    if logs:
        L.append("--- 界面日志（最近 1500 行）---")
        L.extend(list(logs)[-1500:])
        L.append("")
    L.append("=" * 70)
    L.append(" 报告结束。把这个文件发给开发者即可。")
    return "\n".join(str(x) for x in L)


def save(text, path=None):
    """写到桌面（默认）。返回实际路径。"""
    if not path:
        desk = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.isdir(desk):
            desk = os.path.expanduser("~")
        path = os.path.join(desk, "OnseiCraft-报错日志-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path
