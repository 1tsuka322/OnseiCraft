#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""03 阶段的 A/B 评估器（2026-11-08，解决「单次 A/B 不可信」）。

## 为什么需要它

03_translate（Sakura）**本身就不确定**：`KIKOERU_SAKURA_TEMP=0` 下同配置跑两遍，
实测 **74 行里 9 行不同（12.2%）** —— llama.cpp 在近平局 token 上翻牌。
而 `lexical` 的 `content_drop` 在同一配置下会在 **0~10 之间波动**。

所以「跑一次 A、跑一次 B，指标变好 = 有效」这个流程是**错的**。
2026-11-08 实测：D10 两次「脱 4→0 / 10→0」，重复 2 轮后**关掉开关也变 0**。

## 它做什么

1. 每个配置跑 `--repeat` 次（默认 3），收集指标；
2. **同时跑一组 A/A 对照**（同配置换名再跑一遍）→ 得到**噪声地板**；
3. 只有「B 与 A 的中位差」**超出 A/A 的中位差**，才判定为真效果，否则报 `噪声内`。

## 用法

    # 最小用法：验 KIKOERU_PREFIX_ONO 有没有用
    python scripts/ab03.py --run 20261107-200253_RJ1124502_production \\
        --config "A:" --config "B:KIKOERU_PREFIX_ONO=1" --repeat 3

    # 只跑 03（快，但指标只有行数/空行数；不带 lexical）
    python scripts/ab03.py --run <rid> --config "A:" --config "B:KIKOERU_XXX=1" --stages 03_translate

配置写法：`名字:ENV1=V1;ENV2=V2`，冒号后留空表示「沿用当前环境」。

⚠️ 会**覆盖该 run 的 03/04/05/06 产物**（这是 A/B 的代价）。跑完记得用默认配置再跑一次还原。
"""
import argparse
import io
import json
import os
import re
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # work/
PY = sys.executable
TS = re.compile(r'^\[(\d+):(\d+\.\d+)\](.*)$')


def parse_cfg(s):
    """'B:KIKOERU_X=1;KIKOERU_Y=0' → ('B', {'KIKOERU_X':'1','KIKOERU_Y':'0'})"""
    if ':' not in s:
        raise SystemExit('配置要写成 名字:ENV=V;ENV=V，收到 %r' % s)
    name, rest = s.split(':', 1)
    env = {}
    for kv in [x for x in rest.split(';') if x.strip()]:
        if '=' not in kv:
            raise SystemExit('环境变量要写成 K=V，收到 %r' % kv)
        k, v = kv.split('=', 1)
        env[k.strip()] = v.strip()
    return name.strip(), env


def run_once(rid, stages, env_extra):
    """跑一次。返回 (是否成功, 起始时间戳)。

    ⚠️ 起始时间戳用来给 `metrics()` 判断「这份报告是不是这次跑出来的」。
    GPT R2 Q1 指出：原来不管成败都读 `06_checks/lexical_audit.json`，
    一旦本次运行失败，读到的就是**上一次的旧报告**，A/A 地板会被污染。
    """
    env = dict(os.environ)
    env.update(env_extra)
    cmd = [PY, '-u', os.path.join(ROOT, 'pipeline', 'pipeline.py'),
           'run', '--run', rid, '--only', stages, '--force']
    t0 = time.time()
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or '').strip().splitlines()[-3:]
        print('    ❌ 运行失败，尾部输出：%s' % (' | '.join(tail) or '(空)'), flush=True)
    return r.returncode == 0, t0


def metrics(rid, since=0.0):
    """从 06_checks/lexical_audit.json + 03 draft 收集指标。

    `since`：只接受 **mtime >= since** 的文件。小于它的说明是上一轮的旧报告，
    必须丢弃（否则失败的那次会读到上一次的结果，见 run_once 注释）。
    """
    d = os.path.join(ROOT, 'runs', rid)
    out = {}
    lex = os.path.join(d, '06_checks', 'lexical_audit.json')
    if os.path.exists(lex) and os.path.getmtime(lex) + 1 < since:
        print('    ⚠️ lexical_audit.json 是旧报告（mtime 早于本次运行），已丢弃', flush=True)
        lex = ''
    if lex and os.path.exists(lex):
        try:
            s = json.load(open(lex, encoding='utf-8'))['summary']
            out['rows'] = s.get('rows')
            out['drop'] = s.get('content_drop')
            out['ellipsis'] = s.get('ellipsis_only')
            out['translit'] = s.get('translit')
        except Exception:
            pass
    if 'rows' not in out:
        # 没跑 04-06 时，只数 03 draft 的行数与空行数
        n = empty = 0
        rj = rid.split('_')[1]
        for p in __import__('glob').glob(os.path.join(d, '03_translate', rj, 'draft', '*.lrc')):
            for ln in io.open(p, encoding='utf-8'):
                m = TS.match(ln.rstrip('\n'))
                if not m:
                    continue
                n += 1
                if not m.group(3).strip():
                    empty += 1
        out['rows'], out['empty'] = n, empty
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', required=True, help='要 A/B 的 run_id')
    ap.add_argument('--config', action='append', required=True,
                    help='名字:ENV=V;ENV=V（可多次；第一个当基线）')
    ap.add_argument('--repeat', type=int, default=3)
    ap.add_argument('--stages', default='03_translate,04_polish,05_term_lock,06_checks')
    ap.add_argument('--control', action='store_true', default=True,
                    help='额外把基线再跑一组当 A/A 噪声地板（默认开）')
    a = ap.parse_args()

    cfgs = [parse_cfg(x) for x in a.config]
    base_name, base_env = cfgs[0]
    groups = {base_name: []}
    if a.control:
        groups[base_name + "'(A/A)"] = []
    for name, _ in cfgs[1:]:
        groups[name] = []

    for name, env in cfgs:
        print('\n=== 跑配置 %s（%d 次）env=%s' % (name, a.repeat, env or '（默认）'), flush=True)
        for i in range(a.repeat):
            ok, t0 = run_once(a.run, a.stages, env)
            if not ok:
                raise SystemExit('配置 %s 第 %d 次运行失败，中止 A/B（继续跑会污染噪声地板）' % (name, i + 1))
            m = metrics(a.run, since=t0)
            if not m:
                raise SystemExit('配置 %s 第 %d 次没产出新指标，中止' % (name, i + 1))
            groups[name].append(m)
            print('  [%d/%d] ok  %s' % (i + 1, a.repeat,
                                        ' '.join('%s=%s' % (k, v) for k, v in m.items())), flush=True)

    # A/A 对照：用基线配置再跑一组
    if a.control:
        key = base_name + "'(A/A)"
        print('\n=== 跑 A/A 对照（同配置 %d 次）' % a.repeat, flush=True)
        for i in range(a.repeat):
            ok, t0 = run_once(a.run, a.stages, base_env)
            if not ok:
                raise SystemExit('A/A 对照第 %d 次运行失败，中止' % (i + 1))
            m = metrics(a.run, since=t0)
            if not m:
                raise SystemExit('A/A 对照第 %d 次没产出新指标，中止' % (i + 1))
            groups[key].append(m)
            print('  [%d/%d] %s' % (i + 1, a.repeat,
                                    ' '.join('%s=%s' % (k, v) for k, v in m.items())), flush=True)

    # 汇总
    keys = [k for k in (groups[base_name][0] if groups[base_name] else {})]
    print('\n' + '=' * 72)
    print('%-22s %s' % ('配置', ' '.join('%-14s' % k for k in keys)))
    for name, rows in groups.items():
        cells = []
        for k in keys:
            vals = [r.get(k) for r in rows if r.get(k) is not None]
            cells.append('%-14s' % ('中位%.1f[%s]' % (statistics.median(vals), ','.join(str(v) for v in vals))
                                    if vals else '—'))
        print('%-22s %s' % (name, ' '.join(cells)))

    if a.control and len(groups) >= 2:
        ctrl = groups[base_name + "'(A/A)"]
        print('\n--- 判定 ---')
        for k in keys:
            b = [r.get(k) for r in groups[base_name] if r.get(k) is not None]
            c = [r.get(k) for r in ctrl if r.get(k) is not None]
            if not b or not c:
                continue
            noise = abs(statistics.median(c) - statistics.median(b))
            rng = (max(b + c) - min(b + c))
            print('%-14s 噪声地板（A vs A/A）中位差 %.1f，合并极差 %d' % (k, noise, rng))
            for name, rows in groups.items():
                if name in (base_name, base_name + "'(A/A)"):
                    continue
                v = [r.get(k) for r in rows if r.get(k) is not None]
                if not v:
                    continue
                d = statistics.median(v) - statistics.median(b)
                verdict = '**超出噪声**' if abs(d) > max(noise, 0.5) else '噪声内'
                print('    %-18s 中位差 %+.1f → %s' % (name, d, verdict))
    print('\n⚠️ 该 run 的产物已被最后一组配置覆盖。还原请再跑一次默认配置：')
    print('   python pipeline/pipeline.py run --run %s --only %s --force' % (a.run, a.stages))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
