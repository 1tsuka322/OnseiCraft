# 代码层问题

> 判据：**改代码 / 安装包 / 界面能修好它**。
> 出口：改代码 → 该部链路验证 → 全库回归（受影响的阶段）→ commit。
> 编号 `C-###`，只增不减。

| 状态 | 含义 |
|---|---|
| 待复现 | 只知道现象，还没在本机重现 |
| 已定位 | 知道是哪段代码 / 哪个条件触发 |
| 已修 | 有 commit 可查 |
| 已证伪 | 不是代码问题（多半是语料层或用户环境） |
| 不修 | 明确放弃，写理由 |

---

<!-- 新条目追加在下面，最新的在最前 -->

## C-001 · ASR 模型路径写死在代码里（`Invalid model size`）

- **来源**：`processed/OnseiCraft-报错日志-20261101-205154.txt`（RJ911012048，OnseiCraft 0.1.3）
- **现象**：01_asr 退出码 1，`ValueError: Invalid model size 'D:\fasterwhisperGUI-0.8.5.sfx\models\faster-whisper-large-v3'`。
  诊断报告里 `asr_model` 明明是 **OK**（指向 `OnseiCraft\models\faster-whisper-large-v3`）。
- **定位**：`scripts/asr.py` 第 28 行
  `MODEL = os.environ.get("FW_MODEL", r"D:\fasterwhisperGUI-0.8.5.sfx\models\...")`
  —— **开发机绝对路径当默认值**，而且**完全绕过配置**：管线从来不把 `pipeline.local.json` 的
  `asr_model` 传给 asr.py。开发机上两者恰好指向同一目录，所以一直没暴露。
  同类残留还有 `rerun_segment.py` / `rescan_head.py`。
- **处理**：
  1. 共同开发者 PR #1（`e15ab6a`，已合入 main）：`resolve_asr_model()` 按
     `环境变量 → pipeline 配置 → 默认相对目录 → 规格名` 解析，`Ctx` 注入 `FW_MODEL`/`KIKOERU_ASR_MODEL`。
  2. 本机补强（2026-11-05）：
     - `asr.py` 加 `--model`（命令行可覆写）+ `check_asr_model()`：路径形态的模型**先验存在性**，
       缺目录 / 缺 `model.bin` 就用人话退出，不再让 faster-whisper 抛那句天书；
     - 启动日志改成打**实际**模型与来源（原来写死 `加载模型 large-v3`，换模型就撒谎）；
     - `stages.stage_asr` 把模型路径**显式写进命令行**（日志里那行 `$ ... asr.py ...` 是第一现场）；
     - `fingerprint`：whisper 那条记录从「写死 path=None / exists=True」改成 stat 实际路径
       —— 以前**换 ASR 模型不会让 01 失效**，等于指纹替模型打包票；
     - `params.model` 改成取实际目录名（本机与旧值逐字相同，历史 run 的 params 不变）。
- **验证**：RJ1336515 单轨用新代码重跑 01，输出 JSON 与既有 run **逐字节相同**（sha256 一致）。
- **状态**：已修

## C-002 · 台本对不上音轨 → 整部作品一个字都不输出

- **来源**：`processed/OnseiCraft-报错日志-20261101-220920.txt`（RJ911012206）
- **现象**：`01_asr` 门禁 `{"ok": false, "tracks": 0}`，界面只显示「门禁未过」，没有任何字幕。
- **定位**：台本 1 轨 112 句，音频叫 `含效果音.mp3`（**没有轨号**）→ `script_align.track_no()` 返回
  `None` → 「跳过（台本没有这一轨）」→ `asr/` 里 0 个 json。
  `script_align.py` 还把「1 份台本 + 多音轨 + 无章标记」当成 `SystemExit`，
  在管线里直接变成 `RuntimeError` → 整阶段失败。
  **根因是定位错了**：台本是**可选增强**，却被实现成「对不上就全灭」。
- **处理**：
  1. `script_align.py`：单台本 + 单音轨时**不猜轨号，直接配对**（无歧义，日志里写明）；
  2. `stages.py`：`script_align` 非 0 退出不再抛错，改为「⚠️ 台本对齐失败 → 回退 ASR 原版」，
     并把 `script_align_failed` 记进 manifest。
- **验证**：构造「单台本 + 无轨号音轨」用例，配对成功、台词匹配 100%。
- **状态**：已修

## C-003 · 台本只覆盖一部分轨时，其余轨**静默消失**

- **来源**：C-002 排查时发现（同一段代码的另一种表现）
- **现象**：`asr/` 里有 json 就过门禁（`tracks > 0`），被跳过的轨不进 03，
  成品直接少几轨 —— **没人报错**，比 C-002 更危险。
- **定位**：`stage_asr` 里 `asr/` 只由 `script_align` 写；没被对齐的轨不落盘，下游无从得知。
- **处理**：`stages._fill_missing_from_raw()`：逐轨按 **`source`（音频名）** 核对，
  没覆盖（或 `segments` 为空）就用 raw ASR 补上，日志点名 + `extra.script_fallback_tracks` 留痕。
  ⚠️ 必须按 `source` 比，不能按文件名：`asr.py` 写 `X.mp3.json`、`script_align.py` 写 `X.json`，
  按文件名比会「永远没覆盖」→ 每条轨复制两份 → 同一轨翻两遍。
- **验证**：两轨用例（1 轨有台本 / 1 轨没有）→ 只补缺的那轨，已对齐的轨保持台本版。
- **状态**：已修

## C-004 · 成品目录多出一部无 run 的作品（acceptance `check.py 18/17`）

- **来源**：2026-11-05 跑 `acceptance.py`
- **现象**：`kikoerusubtitle/RJ910312304/` 有 6 个 LRC，但 `work/runs/` 里没有它的 run、
  `ASMRmusic/` 里也没有它的音频 → 「成品 18 部 / run 17 部」。
- **定位**：手工放进成品的交付物，未经 `promote`，因此没有指纹、没有门禁记录、无法回溯。
- **处理**：**待用户确认**。三种出口：① 补一个 `adopt` 基线 run（可回溯但仍是 adopt）；
  ② 移出成品目录（它不在「17 部交付基线」里）；③ 接受它并把 acceptance 的口径改成
  「成品 ⊆ 有 run 的作品」。**没有擅自删/移**。
- **状态**：待确认

## C-005 · 换 ASR 模型时 LRC 头部的 `[by:]` 会撒谎

- **来源**：C-001 顺带发现
- **现象**：`scripts/polish.py` 的 `MODEL_TAG` 写死 `faster-whisper large-v3 + <引擎> (人工风格对齐)`。
  共同开发者给的 moss / whisper 两份 LRC **头部完全一样**，无法据此判断是哪套 ASR 产的。
- **处理**：暂不改。改它会变更所有新产出的成品内容（头部字符串），
  而历史成品是旧代码产的 —— 会制造「成品 ≠ run」的新一轮不一致。
  **等真的换 ASR 引擎时一起改**（届时本来就要全库重跑 01）。
- **状态**：不修（有前置条件，见上）
