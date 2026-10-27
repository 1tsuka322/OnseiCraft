# kikoeru-subtitle

把**日语音频**做成**中文 LRC 字幕**的确定性流水线。

不是「调个模型翻译一遍」——是六阶段流水线 + 可追溯指纹 + 硬门禁：
每一步的产物都能回溯到「用了哪份参数、哪份知识表、哪版代码」，改一行译文也必须留痕。

> **本仓库只包含代码与文档。**
> 音频、成品字幕、平行语料、术语表、句级锁全部不进版本库（见 `.gitignore`）——
> 它们是从他人作品派生的内容，且属于使用者自己的语言资产。

---

## 它解决什么问题

把 ASR（语音识别）→ 翻译 → 润色 串起来谁都会。难的是**让它可交付**：

| 问题 | 本项目的做法 |
|---|---|
| 模型这次跑和上次跑结果不一样，怎么比？ | 每阶段进**指纹**（参数 + 输入哈希 + 知识表哈希 + **代码哈希**），任一变化就重跑，不会「脚本悄悄改了、产物看起来没变」 |
| 人工改过的句子，重跑一次就被冲掉了 | **句级锁**：重跑时覆写回去，人工修正永不丢 |
| 改了术语表，怎么知道影响了哪些作品？ | 知识表**版本化 + 提案审批**（`propose → validate → approve`），审批后自动快照，失败回滚 |
| 成品「看起来没问题」，其实某句是空的 | **空字幕门禁**：拿 ASR 确定性重建「应有的行」，逐行对成品 |
| 某句为什么被改成这样？ | `pipeline.py explain` 按时间点回溯每个阶段的输入输出 |

## 六阶段

```
01_asr       音频 → 日文分段（faster-whisper，词级时间戳）
02_asr_fix   确定性改错（错字表 / 表记白名单 / 垃圾前缀清理），不调模型
03_translate 初译（专用翻译模型）
04_polish    按目标译者风格润色（本地小模型 + 句级锁 + 拟声遮蔽）
05_term_lock 术语一致性锁定（确定性覆写，允许集校验）
06_checks    门禁：格式 / 空字幕 / 扫描 / 知识表自检 / 回归 / 覆盖层
```

`runs/<run_id>/` 是**沙盒**，永不直接进生产；只有 `promote` 一条路能进成品目录，且自动备份。

## 快速开始

```bash
git clone <this-repo> && cd kikoeru-subtitle
python -m pip install -r requirements.txt

# 自检：不需要任何数据，13 项全过就说明环境没问题
python pipeline/pipeline.py selftest

# 空字幕门禁的自检（4 个用例）
python scripts/empty_check.py --selftest
```

跑一部作品（需要你自己准备音频）：

```bash
P="python pipeline/pipeline.py"

$P run     --project RJ0000001 --source /path/to/audio    # 串六阶段
$P check   --run <run_id>                                 # 只跑门禁
$P promote --run <run_id>                                 # 进生产（门禁不过拒绝）
$P status                                                 # 所有 run 一览
```

没有 GPU 也能跑 `02_asr_fix` / `05_term_lock` / `06_checks` 三个阶段和全部门禁——它们不调模型。

## 目录

| 路径 | 内容 | 进版本库 |
|---|---|---|
| `pipeline/` | 管线主体（CLI + 六阶段 + manifest + 知识表事务 + GPU 锁） | ✅ |
| `scripts/` | 各阶段可独立调用的脚本（51 个）+ `scripts/style/`（风格学习） | ✅ |
| `docs/` | 架构、管线、知识表格式 | ✅ |
| `examples/` | 合成样例（手写假数据，无版权内容） | ✅ |
| `.workbuddy/` | 风格库 / 中间产物 / 句级锁 / 覆盖层 / 模型 | ❌ |
| `kikoerusubtitle/` | 成品字幕 | ❌ |
| `runs/` `regset/` `knowledge/` | 运行沙盒 / 回归集 / 知识表版本 | ❌ |
| `internal/` | 维护者内部台账（含真实作品号与露骨例句） | ❌ |
| `RJ*/` | 原始音频 | ❌ |

数据目录都可用环境变量改：`KIKOERU_ROOT` / `KIKOERU_STYLE` / `KIKOERU_PROD` / `KIKOERU_RUNS`。

## 数据与合规

本仓库**不分发**任何第三方内容。使用者需要自己准备：

- 音频（`.mp3` / `.flac` / `.wav`）
- 可选：官方台本（PDF/txt），有台本时 ASR 只当时间锚点，精度显著更高
- 可选：目标译者的风格库（默认读 `.workbuddy/style/`），没有就用内置默认规则

**请只处理你有权处理的内容。** 生成的字幕属于原作品的衍生内容，
是否分发、分发到哪里，由你自行判断并承担相应责任。

## 文档

| 想了解 | 看 |
|---|---|
| 整体架构与设计取舍 | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| 管线用法、指纹、断点续跑、GPU 串行 | [`docs/PIPELINE.md`](docs/PIPELINE.md) |
| 知识表（错字/术语/允许集）格式与审批 | [`docs/KNOWLEDGE.md`](docs/KNOWLEDGE.md) |
| 怎么参与 | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

## 许可

[MIT](LICENSE)
