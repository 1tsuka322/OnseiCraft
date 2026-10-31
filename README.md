# OnseiCraft

把**日语音频**做成**中文 LRC 字幕**的桌面软件 + 确定性流水线。

不是「调个模型翻译一遍」——是六阶段流水线 + 可追溯指纹 + 硬门禁：
每一步的产物都能回溯到「用了哪份参数、哪份知识表、哪版代码」，改一行译文也必须留痕。

> **本仓库只包含代码与文档。**
> 音频、成品字幕、平行语料、术语表、句级锁都不进版本库。

---

## 下载安装（Windows）

到 [Releases](https://github.com/1tsuka322/OnseiCraft/releases/latest) 下载
`OnseiCraft-x.y.z-Setup.exe`，双击安装即可。

- **可自选安装目录**，不需要管理员权限，装完桌面有快捷方式
- 首次启动会提示缺模型 → 点「下载缺失的模型」，自动下载约 **8GB**
  （Whisper large-v3 3.1GB + Sakura-7B 4.2GB + llama.cpp 0.7GB），支持断点续传、可切国内镜像
- 之后就是：**拖入音频或整个文件夹** → 选台本（可选）→ 等它跑完 → 字幕在输出目录
- 卸载不会删模型和已生成的字幕

安装包**不含模型**（塞进去要 8.5GB 以上），也不含音频/成品/语料。

## 它长什么样

首页三张卡片（音频文件 / 反馈日志 / ASR 暂存管理）→ 三步向导（音频 → 有无台本 → 生成）。
生成时逐条显示六个阶段的状态、总进度和预计剩余时间。

- **反馈日志**：勾出不满意的轨道，导出「你的意见 + ASR 识别原文 + 修后原文 + 当前字幕」，
  直接发给开发者就能定位是识别、翻译还是润色的锅。
- **ASR 暂存管理**：识别中间产物留着还是删掉，由你决定。

---

## 目录（三个文件夹 + 两个文件）

```
kikoeru/
├── README.md             ← 本文件：仓库说明
├── LICENSE               ← MIT（留在根目录，GitHub 只从这里识别许可证）
├── ASMRmusic/            原始音频（不进库）
│   └── RJxxxxxxx/…
├── kikoerusubtitle/      交付成品 LRC（不进库）
│   └── RJxxxxxxx/…
└── work/                 全部工作文件（代码/文档/数据/运行产物/临时）
    ├── README.md         ← **结构树 + 每个目录干什么 + 常用命令**
    ├── app/              ★ OnseiCraft 桌面软件（界面 + 打包配置）
    ├── pipeline/         管线软件（pipeline.py + pipeline_lib）
    ├── scripts/          阶段脚本（ASR/翻译/润色/校验/修复）
    ├── presets/          公开知识包（术语/错字/风格统计）
    ├── docs/             公开文档（架构/用法/知识表/质量）
    ├── examples/         合成样例（可直接被自己的检查器跑）
    ├── data/             知识表/草稿/锁/覆盖层/模型（不进库）
    ├── runs/             每次执行一个沙盒目录（不进库）
    ├── knowledge/        知识表版本与提案（不进库）
    ├── regset/           回归集与转接材料（不进库）
    ├── internal/         内部台账（不进库）
    └── tmp/              临时文件（不进库）
```

**接手先看 [`work/README.md`](work/README.md)**，那里有完整结构树和每层的职责。
许可见 [`LICENSE`](LICENSE)（MIT），参与方式见 [`work/CONTRIBUTING.md`](work/CONTRIBUTING.md)。

## 它解决什么问题

把 ASR（语音识别）→ 翻译 → 润色 串起来谁都会。难的是**让它可交付**：

| 问题 | 本项目的做法 |
| --- | --- |
| 模型这次跑和上次跑结果不一样，怎么比？ | 每阶段进**指纹**（参数 + 输入哈希 + 知识表哈希 + **代码哈希** + **实际加载的模型**），任一变化就重跑，不会「脚本悄悄改了、产物看起来没变」 |
| 人工改过的句子，重跑一次就被冲掉了 | **句级锁**：重跑时覆写回去，人工修正永不丢 |
| 改了术语表，怎么知道影响了哪些作品？ | 知识表**版本化 + 提案审批**（`propose → validate → approve`），审批后自动快照，失败回滚 |
| 成品「看起来没问题」，其实某句是空的 | **空字幕门禁**：拿 ASR 确定性重建「应有的行」，逐行对成品 |
| 翻译模型把具体物「委婉化」成公文腔，信息悄悄没了 | **语义覆盖门禁**：源语短语驱动的表，原文出现的东西必须在中文里出现（`おしっこ` 不许译成「排泄行为」） |
| 某句为什么被改成这样？ | `pipeline.py explain` 按时间点回溯每个阶段的输入输出 |

## 六阶段

```
01_asr       音频 → 日文分段（faster-whisper，词级时间戳）
02_asr_fix   确定性改错（错字表 / 表记白名单 / 垃圾前缀清理），不调模型
03_translate 初译（SakuraLLM 7B，专为日中翻译微调；HY-MT / Qwen 是可回退的旧路线）
04_polish    确定性后处理（默认 cleanup-only：标点/格式归一 + E1 人工锁，**不过 LLM**）
05_term_lock 术语一致性锁定（确定性覆写，允许集校验）
06_checks    十一项门禁：格式 / 空字幕 / 扫描 / 知识表自检 / 回归 / 覆盖层 / 截断 / 槽位 / 语义覆盖 / 锁落地 / 词汇层
```

`work/runs/<run_id>/` 是**沙盒**，永不直接进生产；只有 `promote` 一条路能进成品目录，且自动备份。

## 快速开始

> **本仓库不含模型权重。** 跑之前先按 [`work/docs/MODELS.md`](work/docs/MODELS.md)
> 把 whisper / Sakura / llama.cpp 下好，并在 `work/pipeline/pipeline.local.json` 里指过去
> （该文件已在 .gitignore，不会上传）。不配也能跑完三个不调模型的阶段和全部门禁。

```bash
git clone <this-repo> && cd kikoeru
python -m pip install -r work/requirements.txt

# 自检：不需要任何数据，19 项全过就说明环境没问题
python work/pipeline/pipeline.py selftest

# 空字幕门禁的自检（4 个用例）
python work/scripts/empty_check.py --selftest

# 语义覆盖门禁的自检（9 个用例）：原文出现的东西，中文里不许丢
python work/scripts/semantic_coverage.py --selftest

# 错字表自检：有完整语料时 44/44；语料不全时自动降级为「结构断言」并 SKIP 数值断言
python work/scripts/errata_audit.py --test

# 一条命令验收全部「交付声明」（音频↔字幕 / 成品=run / 阶段指纹 / 门禁 / 语义覆盖 / 死路径）
# 台账里的每个数字都应该能由它复现；任何一条不成立就退出码 1
python work/scripts/acceptance.py
```

跑一部作品（需要你自己准备音频，放进 `ASMRmusic/<RJ号>/`）：

```bash
P="python work/pipeline/pipeline.py"

$P run     --project RJ0000001 --source ASMRmusic/RJ0000001   # 串六阶段
$P status                                                     # 所有 run 一览（先看有哪些 run_id）
$P check   --run <run_id>                                     # 只跑门禁
$P promote --run <run_id>                                     # 进生产（门禁不过拒绝）
```

> `check --run` 需要**本机真实存在**的 run；刚 clone 下来 `work/runs/` 是空的。
> 想先有东西可查：`$P adopt --project <你的作品>` 建一个基线 run，再 `check` 它。

没有 GPU 也能跑 `02_asr_fix` / `05_term_lock` / `06_checks` 三个阶段和全部门禁——它们不调模型。

## 知识表（安装包已内置）

术语怎么统一、ASR 错字怎么改、什么算表记差异、译文该是什么风格——这些**不在代码里，
在知识表里**。安装包已经把公开的那部分（规则、词典、统计量）打进 `work/presets/`，
装完就能用，**不需要自己准备**。

想改？把自己的表放 `work/data/style/`（优先级高于 `presets/`）：
查找顺序是 `KIKOERU_STYLE` → `work/data/style` → `work/presets`，
所以「有私有语料」和「刚 clone」跑的是同一份代码。

详见 [`work/presets/README.md`](work/presets/README.md)。

## 数据与合规

本仓库**不分发**音频、台本、成品字幕或语料。使用者需要自己准备：

- 音频（`.mp3` / `.flac` / `.wav`），放在 `ASMRmusic/<RJ号>/`
- 可选：官方台本（PDF/txt），有台本时 ASR 只当时间锚点，精度显著更高
- 可选：自己的风格库（不提供就用仓库里的 `work/presets/`）

**请只处理你有权处理的内容。** 生成的成品字幕是否分发、分发到哪里，由你自行判断并承担相应责任。

## 文档

| 想了解 | 看 |
| --- | --- |
| **目录结构与每层职责** | [`work/README.md`](work/README.md) |
| **桌面软件（安装 / 使用 / 打包 / 排错）** | [`work/app/README.md`](work/app/README.md) |
| **模型下载 / 放哪 / 怎么配（本地跑必读）** | [`work/docs/MODELS.md`](work/docs/MODELS.md) |
| 整体架构与设计取舍 | [`work/docs/ARCHITECTURE.md`](work/docs/ARCHITECTURE.md) |
| 管线用法、指纹、断点续跑、GPU 串行 | [`work/docs/PIPELINE.md`](work/docs/PIPELINE.md) |
| 知识表（错字/术语/允许集/覆盖表）格式与审批 | [`work/docs/KNOWLEDGE.md`](work/docs/KNOWLEDGE.md) |
| **怎么知道字幕是好的：门禁、缺陷发现、被证伪的判据** | [`work/docs/QUALITY.md`](work/docs/QUALITY.md) |
| 怎么参与 | [`work/CONTRIBUTING.md`](work/CONTRIBUTING.md) |

## 许可

[MIT](LICENSE)
