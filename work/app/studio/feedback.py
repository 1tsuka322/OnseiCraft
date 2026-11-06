# -*- coding: utf-8 -*-
"""反馈日志：用户挑几轨 + 写不满意的地方 → 一份能直接发给开发者的 txt。

必须带的三样东西（用户要求）：
  1. 用户自己的话
  2. **ASR 识别原文**（模型听到了什么）
  3. **修后原文**（errata 改成了什么）
另外附上当前成品字幕，否则开发者没法判断「错在识别、错在翻译、还是错在润色」。
"""
import os
import re
import time

from . import settings, store


def _ts(sec):
    sec = max(0.0, float(sec or 0))
    return "%02d:%05.2f" % (int(sec // 60), sec % 60)


def _near(lines, sec, span=8.0):
    return [(t, s) for t, s in lines if abs(t - sec) <= span]


def _parse_ts(s):
    """接受 00:12.3 / 12.3 / 1:02 三种写法，解析失败返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d+):([\d.]+)$", s)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    try:
        return float(s)
    except Exception:
        return None


def _txt_report(entry, items, full=True, app_version=""):
    """entry: store.list_stores() 的一条；items: [{track, ts, comment}]。"""
    L = []
    bar = "=" * 58
    L.append(bar)
    L.append("kikoeru 字幕反馈")
    L.append(bar)
    L.append("作品号  ：%s" % entry["project"])
    L.append("运行 ID ：%s" % entry["run_id"])
    L.append("生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    if entry.get("source"):
        L.append("音频目录：%s" % entry["source"])
    if app_version:
        L.append("软件版本：%s" % app_version)
    L.append("反馈轨道：%d 条" % len(items))
    L.append("")
    L.append("说明：ASR 原文 = 语音识别直接吐出的日文；修后原文 = 经过确定性错字表")
    L.append("      修正后的日文（管线实际拿去翻译的版本）；字幕 = 最终成品。")
    L.append("")

    for name, its in _group_by_track(items):
        lines_asr = store.track_text(entry["path"], "asr", name)
        lines_fix = store.track_text(entry["path"], "fix", name)
        lines_lrc = store.track_text(entry["path"], "lrc", name)

        L.append(bar)
        L.append("【轨道】%s" % name)
        L.append(bar)
        L.append("我的意见（%d 条）：" % len(its))
        for it in its:
            head = _entry_head(it)
            comment = (it.get("comment") or "").strip().replace("\n", " ")
            L.append("  · %s%s" % (("[" + head + "] ") if head else "",
                                   comment or "（未填写具体意见）"))
        L.append("")

        for it in its:
            sec = _parse_ts(it.get("ts"))
            if sec is None:
                continue
            L.append("▶ 标记时间点 %s 附近：" % _ts(sec))
            for label, lines in (("ASR 原文", lines_asr), ("修后原文", lines_fix),
                                 ("当前字幕", lines_lrc)):
                near = _near(lines, sec, 8.0)
                if near:
                    L.append("  %s：" % label)
                    for t, s in near:
                        L.append("    [%s] %s" % (_ts(t), s))
            L.append("")

        if full:
            for label, lines in (("ASR 识别原文（全轨）", lines_asr),
                                 ("修后原文（全轨，errata 之后）", lines_fix),
                                 ("当前字幕（成品 LRC）", lines_lrc)):
                L.append("--- %s · %d 行 ---" % (label, len(lines)))
                if not lines:
                    L.append("（暂存里没有这一份）")
                for t, s in lines:
                    L.append("[%s] %s" % (_ts(t), s))
                L.append("")
        else:
            # 只给标记点附近 60 秒，避免 txt 过长
            if sec is not None:
                for label, lines in (("ASR 识别原文（±60s）", lines_asr),
                                     ("修后原文（±60s）", lines_fix),
                                     ("当前字幕（±60s）", lines_lrc)):
                    near = _near(lines, sec, 60.0)
                    L.append("--- %s · %d 行 ---" % (label, len(near)))
                    for t, s in near:
                        L.append("[%s] %s" % (_ts(t), s))
                    L.append("")

    L.append(bar)
    L.append("（本文件由 OnseiCraft 生成，可直接发给开发者）")
    return "\n".join(L) + "\n"


def save_report(text, project, out_dir=None, ext="md"):
    d = out_dir or settings.load_app()["feedback_dir"]
    os.makedirs(d, exist_ok=True)
    fn = "feedback_%s_%s.%s" % (project, time.strftime("%Y%m%d_%H%M%S"), ext)
    p = os.path.join(d, fn)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


# ---------------------------------------------------------------- 新格式
# 为什么改：txt 是给人看的，agent 要解析就得猜格式。
# Markdown 人类可读 + LLM 天然理解；JSON 给脚本批量处理。原文一律用围栏包住，
# 免得日文里的 `#`、`*`、`|` 被当成 Markdown 语法。

def _fmt_lines(lines):
    return "\n".join("[%s] %s" % (_ts(t), x) for t, x in lines)


def _group_by_track(items):
    """[{track,ts,kind,comment}] → [(track, [item, ...]), ...]，保持输入顺序。

    一轨可以有多条意见（用户按时间点逐条报），导出时按轨聚合成一节，
    原文只输出一次，避免同一轨重复贴全文。
    """
    groups = []
    for it in items:
        name = it.get("track") or ""
        if groups and groups[-1][0] == name:
            groups[-1][1].append(it)
        else:
            groups.append((name, [it]))
    return groups


def _entry_head(it):
    """一条意见的抬头：`00:34.10 · 人称错`（缺项自动省略）。"""
    sec = _parse_ts(it.get("ts"))
    parts = []
    if sec is not None:
        parts.append(_ts(sec))
    kind = (it.get("kind") or "").strip()
    if kind:
        parts.append(kind)
    return " · ".join(parts)


def _md_report(entry, items, full=True, app_version=""):
    groups = _group_by_track(items)
    L = []
    L.append("# OnseiCraft 字幕反馈")
    L.append("")
    L.append("| 字段 | 值 |")
    L.append("| --- | --- |")
    L.append("| 作品号 | `%s` |" % entry.get("project", ""))
    L.append("| 运行 ID | `%s` |" % entry.get("run_id", ""))
    L.append("| 导出时间 | %s |" % time.strftime("%Y-%m-%d %H:%M:%S"))
    if app_version:
        L.append("| 软件版本 | %s |" % app_version)
    if entry.get("source"):
        L.append("| 音频目录 | `%s` |" % entry["source"])
    L.append("| 反馈轨道 | %d 条 / 意见 %d 条 |" % (len(groups), len(items)))
    L.append("")
    L.append("> **术语**：`ASR 原文` = 语音识别直出的日文；"
             "`修后原文` = 经确定性错字表修正后、管线实际拿去翻译的日文；"
             "`当前字幕` = 最终成品 LRC。")
    L.append("")

    for i, (name, its) in enumerate(groups, 1):
        la = store.track_text(entry["path"], "asr", name)
        lf = store.track_text(entry["path"], "fix", name)
        ll = store.track_text(entry["path"], "lrc", name)
        secs = [s for s in (_parse_ts(x.get("ts")) for x in its) if s is not None]

        L.append("---")
        L.append("")
        L.append("## %d. `%s`" % (i, name))
        L.append("")
        L.append("**我的意见（%d 条）**" % len(its))
        L.append("")
        for it in its:
            head = _entry_head(it)
            comment = (it.get("comment") or "").strip().replace("\n", " ")
            L.append("- %s%s" % (("**%s**：" % head) if head else "", comment or "（未填写具体意见）"))
        L.append("")

        if full:
            pairs = [("ASR 原文（全轨）", la), ("修后原文（全轨）", lf), ("当前字幕（全轨）", ll)]
        else:
            lo = min(secs) if secs else None
            hi = max(secs) if secs else None

            def win(lines):
                if lo is None:
                    return lines
                return [(t, s) for t, s in lines if lo - 60.0 <= t <= hi + 60.0]

            pairs = [("ASR 原文（标记点 ±60s）", win(la)),
                     ("修后原文（标记点 ±60s）", win(lf)),
                     ("当前字幕（标记点 ±60s）", win(ll))]
        for label, lines in pairs:
            L.append("### %s · %d 行" % (label, len(lines)))
            L.append("")
            L.append("```text")
            L.append(_fmt_lines(lines) if lines else "（暂存里没有这一份）")
            L.append("```")
            L.append("")
    return "\n".join(L).rstrip() + "\n"


def _json_report(entry, items, full=True, app_version=""):
    import json
    groups = _group_by_track(items)
    out = {
        "format": "onseicraft-feedback/2",
        "project": entry.get("project", ""),
        "run_id": entry.get("run_id", ""),
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "app_version": app_version or "",
        "source": entry.get("source", ""),
        "tracks": [],
    }
    for name, its in groups:
        la = store.track_text(entry["path"], "asr", name)
        lf = store.track_text(entry["path"], "fix", name)
        ll = store.track_text(entry["path"], "lrc", name)
        secs = [s for s in (_parse_ts(x.get("ts")) for x in its) if s is not None]
        if not full and secs:
            lo, hi = min(secs), max(secs)

            def win(lines):
                return [(t, s) for t, s in lines if lo - 60.0 <= t <= hi + 60.0]

            la, lf, ll = win(la), win(lf), win(ll)
        out["tracks"].append({
            "track": name,
            "entries": [{
                "ts": _ts(_parse_ts(x.get("ts"))) if _parse_ts(x.get("ts")) is not None else "",
                "ts_sec": _parse_ts(x.get("ts")),
                "kind": (x.get("kind") or "").strip(),
                "comment": (x.get("comment") or "").strip(),
            } for x in its],
            "asr": ["[%s] %s" % (_ts(t), x) for t, x in la],
            "fixed": ["[%s] %s" % (_ts(t), x) for t, x in lf],
            "lrc": ["[%s] %s" % (_ts(t), x) for t, x in ll],
        })
    return json.dumps(out, ensure_ascii=False, indent=1)


def build_report(entry, items, full=True, app_version="", fmt="md"):
    """fmt: md（默认，人机都好读） / json（结构化） / txt（旧格式，保留兼容）。"""
    if fmt == "json":
        return _json_report(entry, items, full, app_version)
    if fmt == "txt":
        return _txt_report(entry, items, full, app_version)
    return _md_report(entry, items, full, app_version)
