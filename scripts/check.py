# -*- coding: utf-8 -*-
"""
字幕质量校验：时间轴、空行、日文残留、编号残留，并与人工风格指标对比。

用法：
  python check.py <字幕目录> [--style DIR]

输出 0 = 全部通过；1 = 存在问题。
"""
import os
import re
import sys
import glob
import json
import argparse

try:
    import zhconv
except ImportError:
    zhconv = None

JPN = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
NUM = re.compile(r"^\s*\d+\s*[|｜]")
TS = re.compile(r"^\[(\d+):([\d.]+)\]")
# 风格库位置统一由 styleloc 解析：环境变量 → .workbuddy/style → presets（公开预设）
import styleloc  # noqa: E402
DEFAULT_STYLE = styleloc.style_dir()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--style", default=DEFAULT_STYLE)
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(a.dir, "*.lrc")))
    if not files:
        print("没有找到 lrc：", a.dir)
        sys.exit(1)

    stats = {}
    sp = os.path.join(a.style, "stats.json")
    if os.path.exists(sp):
        stats = json.load(open(sp, encoding="utf-8"))

    tot = {"lines": 0, "jp": 0, "num": 0, "empty": 0, "period": 0,
           "ell": 0, "ne": 0, "dun": 0, "len": 0, "trad": 0, "spacer": 0,
           "half": 0}
    bad_files = []
    for p in files:
        # R23（坑 106）：空文本时间戳行 = 清屏 spacer（LRC 语义下行显示到下一行
        # 时间戳为止，spacer 让字幕在「读完即走」时刻清屏）。合法 spacer 必须
        # 夹在两条非空行之间；连续空行/行首行尾空行仍判真空行。
        rs = []
        for l in open(p, encoding="utf-8"):
            l = l.rstrip("\n")
            if not l.startswith("[") or l.startswith("[ti:") or l.startswith("[by:"):
                continue
            m = TS.match(l)
            if not m:
                continue
            rs.append((int(m.group(1)) * 60 + float(m.group(2)),
                       l[m.end():].strip()))
        sp = {i for i, (_, t) in enumerate(rs) if not t
              and 0 < i < len(rs) - 1 and rs[i - 1][1] and rs[i + 1][1]}
        ts, n, jp, num, emp, trad, half = [], 0, 0, 0, 0, 0, 0
        for i, (t0, t) in enumerate(rs):
            ts.append(t0)
            if i in sp:
                continue
            n += 1
            if not t:
                emp += 1
            if JPN.search(t):
                jp += 1
            if NUM.match(t):
                num += 1
            if t.endswith("。"):
                tot["period"] += 1
            # 省略号形态跟随该译者的真实习惯（三点「…」还是六连点「……」）。
            # 早期版本写死只认「……」，导致用三点的译者永远报 0%，是误报。
            if (stats.get("ellipsis_style", "……") in t) or "…" in t:
                tot["ell"] += 1
            if "呢" in t:
                tot["ne"] += 1
            if "、" in t:
                tot["dun"] += 1
            # R46（2026-10-23）：半角标点（? ! , 以及非数字间的 .）不符合中文排版规范。
            # 全库曾残留 29 行（主要是 `吗?` 用半角问号），已统一为全角。
            _b = re.sub(r"^(白|甘)：", "", t)
            if re.search(r"[?!,]|(?<![0-9A-Za-z])\.(?![0-9A-Za-z])", _b):
                half += 1
                tot["half"] += 1
            # ⚠️ 2026-10-21 修口径缺陷：日文引号「」『』被 zhconv 转成中文弯引号 “”，
            # 会被误判成「繁体」——RJ9000014 台本成品 9 轨中招（台词里的引用句）。
            # 先剥掉日文括号/引号再判（真繁体「說/們」照样报）。
            _t = t.translate({0x300C: None, 0x300D: None, 0x300E: None, 0x300F: None,
                              0x3010: None, 0x3011: None, 0xFF08: None, 0xFF09: None})
            if zhconv and _t.strip() and zhconv.convert(_t, "zh-cn") != _t:
                trad += 1
            tot["len"] += len(t)
        ok = all(ts[i] <= ts[i + 1] for i in range(len(ts) - 1))
        flag = "" if (ok and jp == 0 and num == 0 and emp == 0 and trad == 0
                      and half == 0) else "  <-- 需检查"
        if flag:
            bad_files.append(os.path.basename(p))
        print("%-46s %4d 行  时间轴%s  日文%d  编号%d  空行%d 繁体%d  半角%d  清屏%d%s"
              % (os.path.basename(p)[:44], n, "OK" if ok else "异常", jp, num, emp, trad,
                 half, len(sp), flag))
        tot["lines"] += n
        tot["jp"] += jp
        tot["num"] += num
        tot["empty"] += emp
        tot["trad"] += trad
        tot["spacer"] += len(sp)

    n = max(1, tot["lines"])
    print("\n合计 %d 行（清屏 spacer %d 行，不计入风格指标）"
          % (tot["lines"], tot["spacer"]))
    if tot["half"]:
        print("半角标点 %d 行（应统一为全角 ？！，）" % tot["half"])
    if stats:
        def pc(k):
            return 100 * tot[k] / n
        print("\n%-14s %-22s %s" % ("指标", "本次", "人工参考"))
        ell_name = "省略号%s" % stats.get("ellipsis_style", "…")
        rows = [(ell_name, pc("ell"), stats.get("pct_ellipsis", 0)),
                ("「呢」", pc("ne"), stats.get("pct_ne", 0)),
                ("顿号、", pc("dun"), stats.get("pct_dunhao", 0))]
        for name, mine, ref in rows:
            delta = mine - ref
            mark = "OK" if abs(delta) <= max(20, ref * 0.6) else "偏差大"
            print("  %-12s %6.1f%%  %18.1f%%   %s" % (name, mine, ref, mark))
        # 句号已改为「句末句号」规范（2026-10-08），人工字幕是旧「无句号」时代产物，
        # 其 0% 不再作为目标，只显示不判定。
        print("  %-12s %6.1f%%  %18s   %s" % ("句末句号", pc("period"), "（新规范）", "—"))
        print("  %-12s %6.1f 字 %17.1f 字" % ("平均句长", tot["len"] / n, stats.get("avg_zh_len", 0)))

    ok = (not bad_files and tot["jp"] == 0 and tot["num"] == 0
          and tot["empty"] == 0 and tot["trad"] == 0)
    print("\n结论:", "全部通过" if ok else "存在问题：%s" % ", ".join(bad_files))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
