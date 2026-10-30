# work/ —— 工作文件总目录

> 仓库根只有三样东西：`ASMRmusic/`（音频）、`kikoerusubtitle/`（成品）、`work/`（本目录）。
> **所有代码、文档、数据、运行产物、临时文件都在这里。**
> 许可文件 `LICENSE` 留在**仓库根**（GitHub 只从根目录识别许可证），不在本目录。
> 换机器接手：先读本页 → `internal/TODO_*.md`（最新那份的 §1）→ `internal/docs/WORKBENCH.md`。

---

## 1. 结构树

```
work/
├── README.md                ← 本文件（结构树 + 每层职责 + 常用命令）
├── requirements.txt         依赖（pip install -r）
├── CONTRIBUTING.md          参与方式
│
├── pipeline/                ★ 管线软件（唯一执行入口）
│   ├── pipeline.py          23 行薄壳，真正的实现在 pipeline_lib/
│   ├── pipeline_lib/
│   │   ├── cli.py           run / check / status / promote / repair / explain / adopt / knowledge / queue
│   │   ├── stages.py        六阶段执行器 + 九项门禁定义
│   │   ├── manifest.py      每阶段产物/指纹/状态（runs/<id>/manifest.json）
│   │   ├── fingerprint.py   参数+输入+知识表+代码+模型 → 阶段指纹
│   │   ├── knowledge.py     知识表版本化与提案审批（propose/validate/approve）
│   │   ├── queue.py         GPU 串行锁（data/tmp/pipeline_queue/gpu.lock）
│   │   ├── paths.py         ★ 路径唯一出口（三层结构的换算点）
│   │   └── selftest.py      19 项自检（不需要任何数据；S7 扫阶段 code 的 import 闭包）
│   ├── tools/dashboard.py   只读看板（manifest → runs/dashboard.html）
│   ├── pipeline.json        配置（引擎/槽数/cue 口径/门禁清单）
│   ├── VERSION / CHANGELOG.md
│   └── README.md
│
├── scripts/                 ★ 阶段脚本（被 pipeline 调用，也可单独跑）
│   ├── asr.py               01 语音识别（faster-whisper large-v3）
│   ├── asr_fix_scan.py      02 确定性改错（errata / 表记白名单）
│   ├── translate.py         03 初译（默认 Sakura；HY-MT / Qwen 仅回退路线）
│   ├── polish.py            04 后处理（默认 cleanup-only：标点/格式归一 + E1 锁，不过 LLM）
│   ├── term_lock.py         05 术语锁（允许集校验 + 确定性覆写）
│   ├── check.py / empty_check.py / scan_violations.py / semantic_coverage.py
│   │                        06 门禁
│   ├── asr_halluc.py        01 层末尾：清 Whisper 在非语音段吐的拉丁幻觉
│   ├── knobs.py             ★ 旋钮默认值的单一真相（SEG_FLUSH_CHARS / CHUNK / 引擎标签）
│   ├── asr_words.py         ★ text 改了必须同步 words（cue 管线读 words 不读 text）
│   ├── export_review.py     逐行审校表（给外部模型判；用 --format txt）
│   ├── fill_timeline.py     补「整行消失」的时间轴占位（cue 内漏译的兜底）
│   ├── dedup_lines.py       删「和上一行一字不差」的重复行（跨 cue 那 193 处）
│   ├── errata_audit.py      errata 自检（44 条断言）
│   ├── acceptance.py        ★ 一条命令验收全部交付声明（音频↔字幕 / 成品=run / 指纹 / 门禁 / 语义 / 路径）
│   ├── path_audit.py        扫写死的项目内绝对路径（三层目录改造的残留）
│   ├── test_regressions.py  回归门禁
│   ├── repair_overlay.py    修复覆盖层（时间轴不变的人工听辨记录）
│   ├── fixline.py           单行重译（--list → --zh → --apply）
│   ├── fragment_join.py     碎片行合并（只做高可靠档 + 白名单）
│   ├── serve.py             llama-server 管理（ensure 会校验实际加载的 gguf）
│   ├── rootloc.py           ★ 给独立脚本用的路径换算（成品/音频在上一级）
│   ├── styleloc.py          风格库定位（env → data/style → presets）
│   ├── style/               风格学习（语料统计、术语挖掘、风格库生成）
│   └── README.md            每个脚本的用法与判据
│
├── presets/                 公开知识包（能公开的那部分，不含语料）
│   ├── glossary.json        术语 → 固定译法
│   ├── coverage.json        语义覆盖表（原文出现就不能丢什么）
│   ├── asr_errata.json      已核实错字表
│   ├── do_not_change.json   表记差异白名单
│   ├── pin_sets.json        允许集
│   ├── user_rules.txt       标点与译法规范（优先级最高）
│   ├── hard_rules.txt       硬约束（带统计百分比）
│   ├── stats.json / style_model.json / style_guide.txt   人工风格统计
│   ├── parallel.json        few-shot 例句（建议换成自己的）
│   └── README.md
│
├── docs/                    公开文档
│   ├── MODELS.md            ★ 模型准备（下载地址 / 放哪 / 怎么配 / 显存怎么排）
│   ├── ARCHITECTURE.md      设计取舍
│   ├── PIPELINE.md          用法/指纹/断点续跑/GPU 串行
│   ├── KNOWLEDGE.md         知识表格式与审批流程
│   └── QUALITY.md           九项门禁 + 缺陷发现 + 被证伪的判据
│
├── examples/                合成样例（CI 会拿自己的检查器跑它）
│
├── data/                    ★ 运行时数据（不进库）
│   ├── style/               生产知识表（唯一真相；glossary/errata/coverage/pin_sets…）
│   ├── draft/<RJ>/          ASR 与草稿（asr/ asr_v1_*/ draft/ track_memory/）
│   ├── locks/<RJ>.json      E1 句级锁（人工修正，重跑不冲掉）
│   ├── repair_overlay/      修复覆盖层
│   ├── models/              本地小模型（拟声检测/声纹/VAD）
│   ├── cache/               润色 cue 级缓存
│   └── tmp/                 管线队列与临时态（gpu.lock 在这）
│
├── runs/<run_id>/           ★ 执行沙盒：每次 run 一个目录 + manifest.json
│   ├── 01_asr/  02_asr_fix/  03_translate/  04_polish/  05_term_lock/  06_checks/
│   └── manifest.json        每阶段的参数/指纹/产物/门禁结果/实际模型
│
├── knowledge/               知识表版本快照 + 提案 + 验证记录 + 备份
├── regset/                  回归集、差分复核池、转接任务与回执
├── internal/                内部台账（SKILL 坑位、规则台账、逐轮记录、ID 映射）
├── kikoerusubtitle_rec/     成品回收站（promote 前的自动备份）
├── listen_*/                听辨包（人工听音频用，带新旧译对照）
├── learning/                风格学习产出
├── assets/                  语料导出（不随仓库分发）
└── tmp/                     ★ 临时文件：一次性脚本、抓取中间态、备份
    ├── daily/               日常小工具（单行修复批处理、抽样包生成…）
    └── backup_pre_git/      建 git 之前的脚本备份（可删）
```

★ = 改之前必读的入口。

---

## 2. 三条路径是怎么换算的

```
<repo>/ASMRmusic/        原始音频        ← KIKOERU_AUDIO
<repo>/kikoerusubtitle/  交付成品        ← KIKOERU_PROD
<repo>/work/             全部工作文件     ← KIKOERU_ROOT（ROOT）
```

- 管线内部：`pipeline/pipeline_lib/paths.py` 是**唯一出口**（`ROOT` / `BASE` / `PROD` / `AUDIO`）。
- 单独跑的脚本：`scripts/rootloc.py` 提供同一套换算，别自己拼 `ROOT + "kikoerusubtitle"`。
  （2026-10-28 目录重构时有 8 个脚本都是这么写的，改漏一个就是「找不到目录」的静默错。）

---

## 2.5 默认路线（2026-10-28 R154 起）

`work/pipeline/pipeline.json` 里这两项决定整条路线：

```json
"engine": "sakura",              // 初译用 Sakura-7B（懂 ACGN 词汇，不会把 赤点 译成「红点」）
"polish_cleanup_only": true      // 04 只做确定性后处理，**不过 Qwen**
```

为什么 04 不能再用 Qwen：Sakura 的输出已经是口语/ACGN 风格，
Qwen 拿那 20 条「为 HY-MT 写的规则」去改，只会改坏（实测把
`君のザコザコガキチンポ` 改成「让你那结实有力的成年男性生殖器…」）。

**回退旧路线**（HY-MT + Qwen）：把这两项改成 `"hymt"` / `false`。

⚠️ 换路线时 **01/02 不用重跑**（它们不调模型），从 03 起跑就行：

```bash
python work/pipeline/pipeline.py run --run <run_id> --from 03_translate
```

## 3. 常用命令

```bash
# 自检（不需要数据、不需要 GPU）
python work/pipeline/pipeline.py selftest                 # 13 项
python work/scripts/empty_check.py --selftest             # 4 项
python work/scripts/semantic_coverage.py --selftest       # 9 项
python work/scripts/errata_audit.py --test                # 44 项（无语料时自动 SKIP 数值断言）

# 修正轮（改完知识表 / 补行 / 去重之后）
python work/scripts/fill_timeline.py --run <run_id>        # 只报告，--apply 才写
python work/scripts/dedup_lines.py  --run <run_id>         # 同上
python work/scripts/export_review.py --run <run_id> --format txt --only-flagged --split

# 跑一部作品
P="python work/pipeline/pipeline.py"
$P run     --project RJ0000001 --source ASMRmusic/RJ0000001
$P status
$P check   --run <run_id>
$P promote --run <run_id>

# 全库体检（成品目录）
python work/scripts/check.py kikoerusubtitle/RJ0000001
python work/scripts/semantic_coverage.py --lrc kikoerusubtitle --asr work/data/draft --verbose

# 溯源某一句
$P explain --run <run_id> --track <轨名> --start 00:01:23

# 改知识表（术语/错字）：提案 → 验证 → 人工批准
$P knowledge propose work/tmp/my_terms.json
$P knowledge validate <proposal_id>
$P knowledge approve <proposal_id> --by human --confirm
```

---

## 4. 清理约定

| 目录 | 能删吗 |
|---|---|
| `tmp/` | 能（一次性脚本与中间态；`tmp/daily/` 里的日常工具建议留） |
| `tmp/backup_pre_git/` | 能（git 之前的备份，已有版本库） |
| `runs/` | 能（沙盒产物；但 `promote` 之前别删） |
| `data/draft/` | **不能**（重跑 ASR 要 GPU 时间；草稿是初译的唯一副本） |
| `data/locks/` `data/style/` | **绝对不能**（人工修正与生产知识表） |
| `kikoerusubtitle/` `ASMRmusic/` | **绝对不能**（成品与原始素材） |
