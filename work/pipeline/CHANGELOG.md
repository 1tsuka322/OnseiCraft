# 变更日志（kikoeru-pipeline）

> 版本号规则：**测试版 `vX.Y.Z-beta.N`**，正式版 `vX.Y.Z`。
> 每次发版：改 `VERSION` → 在本文件顶部加一节（**按日期**）→ `pipeline.py selftest` 全过 → 生成一次看板。
> 只记「能影响行为或追溯」的改动，不记纯文案。

---

## v0.1.0-beta.13 — 2026-10-30（切分旋钮上提到 cfg）

`seg_flush_chars` / `chunk` 从「环境变量 + `knobs.py` 默认值」上提为 **cfg 一等公民**：

- `paths.DEFAULTS` 加 `seg_flush_chars: 10` / `chunk: 3`
- `pipeline.json` 正式声明（值不变）
- `Ctx.__init__` 显式注入 `KIKOERU_SEG_FLUSH_CHARS` / `KIKOERU_CHUNK`
  （不注入的话脚本吃 `knobs.py` 的默认值，改 cfg 等于没改 —— R156 的教训）
- `fingerprint.CONFIG_KEYS` 加这两键；`env_whitelist` 与 03 的 `params` 都从 cfg 取

**入口顺序**：`pipeline.json` > 环境变量（临时 A/B 用）> `knobs.py` 默认（只在不经管线时生效）。

验收：值未变（10/3）→ 产物逐字节不变；17 部 × 6 阶段指纹重打后全部命中；
`acceptance.py` 全绿。

---

## v0.1.0-beta.12 — 2026-10-30（DEFAULTS 对齐 + 验收脚本）

### 1. `paths.DEFAULTS` 与交付口径对齐

`engine` 从 `hymt` 改成 `sakura`、`polish_cleanup_only` 从 `False` 改成 `True`。
以前 DEFAULTS 是旧路线、`pipeline.json` 是新路线，**配置文件一旦缺失/改名，
`load_config()` 就静默退回旧路线**——成品看着正常、其实换了引擎（R153 同族）。
改完 `resolved_config()` 对现有 run 输出不变（各 run 的 config 都带显式值），
产物逐字节不受影响，指纹已按 `scripts/restamp.py` 重打。

### 2. 新增两个验收工具（进库，`tmp/` 是 gitignore）

- `scripts/acceptance.py`：一条命令验 6 类交付声明——音频↔字幕覆盖、成品 = run 逐字节、
  17×6 阶段指纹、门禁/自检、语义覆盖、死路径。任何一条不成立退出码 1。
  **台账里的每个数字都应该能由它复现**，这是「声明 == 实际」的可执行形式。
- `scripts/path_audit.py`：扫写死的项目内绝对路径（三层目录改造的典型残留）。
  首次运行抓出 14 个失效的一次性脚本（`probe_*` / `fix_*` / `msg_filter` 等），已删。

### 3. 文档对齐

`README` 的六阶段描述改成实际口径（Sakura 初译 + cleanup-only 后处理，不再写 HY-MT / Qwen）、
`selftest` 13 → 19 项；`work/README` 修正 `asr_latin_halluc.py` → `asr_halluc.py`；
`docs/INDEX.md` 里 6 处三层改造前的旧路径（`.workbuddy/style/*` → `data/style/*`、
`TODO_20261029` → `TODO_20261030`、已删的 `listen_pending_1026` / `showcase_20261026`）改成真实路径。

---

## v0.1.0-beta.11 — 2026-10-30（指纹补全 + 旋钮单一真相）

主题：**「声明 ≠ 实际」第五例——阶段指纹看不见自己真正的依赖**。
链路验证脚本 `tmp/daily/pipeline_verify.py` 量出来的，不是看代码猜的。

### 1. 03 的指纹有 4 个盲区（改了代码不会重算）

| 盲区 | 事实 | 后果 |
|---|---|---|
| `cues.py` 不在 03 的 `code` 里 | 03 只登记了 `translate.py` / `serve.py`，而 `translate.py:277` **在函数体内** `from polish import build_lines`、`:274` `import cues` | 改 cue 切分（本轮核心改动）**不会**让 03 失效，普通 `run` 会报「缓存命中」 |
| `KIKOERU_SEG_FLUSH_CHARS` / `KIKOERU_CHUNK` 不在 `env_whitelist` | 脚本从 `os.environ` 直读，白名单是硬编码的 | 改门槛/块大小同样不触发 |
| sakura gguf 不在 `MODELS_BY_STAGE["03"]` | 只登记 hymt / qwen×2，而默认引擎是 sakura；`server_model` 只记录、不进哈希 | 换模型文件不触发 |
| 04 也读 `SEG_FLUSH_CHARS` | `polish.py` 的 `cues.build_units()` 同样走 `build_semantic_cues` | 只靠 03 的输入间接失效，显式登记更稳 |

修法：03/04/05/06 的 `code` 改成**「本阶段脚本 + import 闭包」**（03 从 3 项变 14 项）；
`env_whitelist(cfg, stage)` 对 03/04 补两个旋钮；`MODELS_BY_STAGE["03"]` 加 `gguf_sakura`。

### 2. `selftest` 新增 S7（防复发）

扫每个阶段 `code` 里脚本的 import（**含函数内**，正则走全文件），
凡是同目录 `scripts/*.py` 就必须登记进 `code`，否则失败。
`test_*.py` 豁免（测试脚本 import 的是「被测对象」，不是产物依赖）。
这一条当场抓出 19 处漏登记（`01_asr→asr_halluc` / `polish→lock_match,styleloc` /
`term_lock→scan_violations` …），全库 `selftest` 13 → **19 项**。

### 3. `scripts/knobs.py`：旋钮默认值的单一真相

`SEG_FLUSH_CHARS` / `CHUNK` / 空译文占位 / 引擎显示名以前各写一份
（`cues.py`、`translate.py`、`stages.py`、`fingerprint.py` 四处），
改一处其余三处就开始撒谎。现在统一从 `knobs.py` 取，
`pipeline_lib` 用 `spec_from_file_location` 加载（不动 `sys.path`）。

### 4. LRC `[by:]` 标签跟随实际引擎

`translate.py` 的 `MODEL_TAG` 写死 `Qwen2.5-7B-Instruct`，跑 Sakura 时草稿头是假的
（R153 正是靠这个标签发现「声明 hymt 实际跑 Qwen」，标签本身不能撒谎）。
现在与 `polish.py` 共用 `knobs.engine_tag()`；已把 147 个 03 草稿的 `[by:]` 同步成真值。
**04 的标签字符串未变**，成品逐字节不受影响。

### 5. `tmp/daily/restamp.py`：带证据的重打指纹

改 `pipeline_lib` 会让 `runner` 哈希变 → 6 个阶段全失效，包括一行没改的 01 ASR。
该工具先验「`code` 闭包里每个脚本的 mtime ≤ 该阶段 `ended_at`」，
通过才写回 manifest，并把理由与证据记进 `restamped` 字段；
证据不足要 `--force --why`（显式留痕）。本轮用它处理 17 部 × 6 阶段。

**验收**：17 部 × 6 阶段指纹与当前代码**全部一致**（102/102）；
04 重跑产物**逐字节一致**；`check.py` 17/17、语义覆盖 0 违规 / 0 loss、
`selftest` 19/19、`test_regressions` 全过、`errata_audit --test` 44/44。

---

## v0.1.0-beta.10 — 2026-10-28（复核看后文 + 语义覆盖门禁）

### 2026-10-30 · 修：cue 段边界门槛 16 → 10（「拟声+实词」混在一条 cue）

`SEG_FLUSH_CHARS` 从 `MIN_CUE_JA_CHARS`(16) 改成 **10**，并支持 `KIKOERU_SEG_FLUSH_CHARS` 覆盖。

**症状**：RJ1530549 轨07 `04:44.10 リリアも気持ちいいよ` 整行丢失（日志 `136 行 → 已写出 135 行`，
旧成品有译文）。根因不是判定层：它和 `おまんこで`(5字) / `上手におちんと`(8字) 三段
**全都低于 16** → 缝成一条 7.8 秒的 cue → Sakura 把「拟声+实词」当一句话翻，整段返回空
→ 再被「空译文跳过」静默丢弃（那一处已单独修，见上一条）。

**全库实测**（17 部 / 24756 个 ASR 段，纯 CPU 调 `build_semantic_cues`）：

| SEG_FLUSH_CHARS | cue 数 | 跨 ≥3 段 | 中位字数 |
|---|---|---|---|
| 16（旧） | 22135 | 691（3.1%） | 14.0 |
| 12 | 22947 | 447（1.9%） | 14.0 |
| **10（采用）** | 23479 | **330（1.4%）** | 13.0 |
| 8 | 24104 | 232（1.0%） | 12.0 |

取 10 而不是 12：用户报的那条正好 10 字，12 切不开它。代价是中位字数 14 → 13。

⚠️ 这条会改**全部作品**的 cue 切分（行数、时间轴都变），所以必须整库重跑，不能只补个别轨。
审计脚本 `work/tmp/daily/cue_span_audit.py`（数每条 cue 跨几个 ASR 段）。

### 2026-10-30 · 修：03 空译文静默丢行 + 04 切点句号优先

**① 03 空译文不再丢行**（`translate.py::emit_lines`，从写盘循环抽成独立函数）

旧写法 `for ln, zh in zip(lines, results): if not zh: continue` —— 整行不写，时间轴少一行。
上游是「块解析失败 → 逐句兜底也返回空」，日志只写「136 行 → 已写出 135 行」，
**看不出是哪一句没了**；06 的 `empty` 门禁能抓到，但只能靠 `fill_timeline` 手工补「…」。
实测 RJ1530549 轨07 `04:44.10 リリアも気持ちいいよ` 就这样蒸发（旧成品有译文）。
现在写 `SAKURA_FALLBACK`（「…」）保住时间轴，并在日志里点名行号。

**② 04 切分点句末标点优先**（`cues.py::_split_by_points` / `split_points`）

旧行为「吸附到最近合法切点」会把下一句首词留在上一行（「回吐」）：
`晚安。不行，老师可能会来巡视。` 15 字切 2 行，理想点 7.5，最近的合法切点是逗号后（6）
→ 切成「晚安。不行」/「老师可能会来巡视。」。
两处改动：
- `_split_by_points` 给非句末切点加 `SPLIT_SENT_BONUS`（4 字符）惩罚，句末标点优先；
- `split_points` 里「单侧 ≥ `MIN_CJK_CHARS` 汉字」这条**对句末标点后豁免**
  （`晚安。` 只有 2 字，原来被挡掉，导致全串只剩一个合法切点）。
  时长仍受 `MIN_LINE_SEC` 约束，`build()` 会把停留过短的行并回下一段。

**实测（17 部）**：行 24873 → 25103（**+0.9%**）、字数 252461 → 252516（**+0.0%**，证明
「只切不重写」没被破坏）、短行（≤2 字）1311 → 1359。`check.py` 17/17，语义覆盖 0 loss。
回归护栏：`test_regressions.py` **3q 组 5 条**。

### 2026-10-29 · 修：`05_term_lock` 先全删再复制 → 撞 safe-delete 上限

`stage_term_lock` 原来是「先删掉 05 里所有 `.lrc`，再从 04 复制」，理由是幂等
（不删旧文件会让上一轮的残留混进来）。问题：**每部要删 N 个文件，N = 轨数**。

实测全库重跑时：RJ377265 25 轨 = 25 次删除，跑到第 3 部就撞上环境的
`safe-delete` 保护（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`，单轮 50 次），
之后每一部都表现为「锁删不掉、阶段起不来」，16 部里 14 部卡在 03/05。
（R155 记过同一个坑的另一种表现：抢锁失败各 4 秒。）

改成「**先复制覆盖，再删这一轮没有的旧文件**」：最终状态完全相同（幂等性不变），
删除数从 N 降到 0~2。实测 RJ426223 全链通过。


### 2026-10-29 · 修：`CHUNK` 10 → 3（长窗口导致模型跨行重排）

`_parse_plain` **只校验行数**：模型把 10 行重排成另外 10 行就静默通过。实测三种形态：
**对调**（相邻两行互换）、**截断**（只译出称呼）、**回吐**（下句首词挂到上句尾）。

**A/B 实锤**（RJ256650 05 轨 656.46/660.10）：

| | 656.46 | 660.10 |
|---|---|---|
| CHUNK=10 | 用沾满爱液的手掌使劲摩擦， | 直到盐巴喷出来为止。 |
| CHUNK=3 | 我要用湿答答的手掌 | 摩擦到盐巴积到喷出来为止 |

ASR 原文分别是「お塩が溜まって吹き出しちゃうまで」/「エッチなお汁で…こすっちゃうね」——
CHUNK=10 时两行**整段对调**，CHUNK=3 各归各位。
`KIKOERU_CHUNK` 环境变量可覆盖；代价是调用次数变多（RJ256650 6 轨 2 分 25 秒）。

### 2026-10-29 · 修：Sakura 幻觉兜底把**忠实翻译**吞成「…」（3 处判据）

拿 29 行「旧成品有译文、新 run 是『…』」给外部模型判，逐条回日志核对后发现三处误杀，
**全都不是「拟声判定太宽」**（含汉字的句子第一层就放行了），而是兜底判据本身：

| 判据 | 症状 | 实测 |
|---|---|---|
| `loop` 用「译文 ≥10 字」判放大 | 原文重复、译文等量重复也判脏 | `なんで×4`→「为什么×4」、`行く×4`→「我要去×4」、`私はマゾマンコです` 全被吞 |
| `nolang` 要求译文含中日文 | 片假名外来词译成拉丁就判「模型没产出」 | `オッケー。`→`OK。` 被吞 |
| `_residue` 见拉丁就判残留 | 专有名词用拉丁，但整句中文译得很好 | Free Talk 6 句 20~40 字闲聊整句蒸发（`ピノ`→pino、`ハッピー`→Happy、`天神ドロップ`→DROP） |

- 新增 `_rep_level()`：**比较重复次数**（单字连排 + 2/3/4/6 字片段），
  `译文次数 > 日文次数 × 2 + 1` 才算放大。原来的「≥10 字」直接删掉。
- 新增「拟声→拟声」判定：源语是纯气声且译文全是语气字 → 仍压成「…」
  （`あ゛×11`→「啊×22」按次数算属「等量」，会被放过，必须单独拦）。
- `nolang` 放行：源语全片假名 + 译文短拉丁 → 不判。
- `latin:` 残留放行：译文中文字数 ≥6 → 视为混译，保留整行。
  ⚠️ 不削弱 R153 的英文残留保护：那种是整行英文，中文字数远不到 6，`nolang` 照样抓。
- `test_regressions.py` 新增 **3p 组（14 条）**：10 条必须放行、4 条必须拦下。

### 2026-10-29 · 修：阶段描述写死模型名（03 一律显示「HY-MT」、04 一律显示「Qwen」）

R154 把默认路线换成 `engine=sakura` + `polish_cleanup_only=true` 之后，
`STAGES[...]["desc"]` 还是字符串常量，于是每次运行都打印
`[03_translate] ▶ 初译（HY-MT）`、`[04_polish] ▶ 风格润色（Qwen2.5-7B + E1 锁 + A8）`——
**而 03 实际跑的是 Sakura、04 根本没调模型**。这正是 R155 在修的「声明 ≠ 实际」，
只是这次错在日志上（会误导排障：R153 的事故就是靠日志判断引擎的）。

- `desc` 允许写成 `callable(ctx)`；`run_stages` 在打印前解析（缓存命中与开始执行两处）。
- 新增 `ENGINE_LABELS` + `_engine_label()`：`sakura→Sakura`、`hymt→HY-MT`、`q5/q4→Qwen2.5-7B`。
- 03 → `初译（Sakura）` / `初译（HY-MT）`；04 → `确定性后处理（cleanup-only，不过 LLM）`
  / `风格润色（Qwen2.5-7B + E1 锁 + A8）`，按 `polish_cleanup_only` 切换。
- **只影响显示**：`desc` 不进指纹，改动不会造成 cache miss。


### 2026-10-28 · 修：`04_polish` 不记录「实际加载的模型」（R153 的断言缺口）

R153 把 `serve.py ensure` 接上了，但**只有 03 把校验结果写进 manifest**，04 没有。
而 04 恰恰是 R153 事故里跑错模型的那一段——「声明 hymt、实际 Qwen」在 04 的产物上依然查不出来。

- `stage_polish` 的 `extra` 增加 `server_model`；`04_polish` 的 `params` 增加 `polish_engine`
  （原来只在 `config` 里有，参数视图看不到）。
- `_ensure_server(..., stage=)`：04 的 serve 日志不再写进 `03_translate/serve.log`。
  实测两段输出混在一个文件里，`tail` 只能看到最后一次，排障时误导。
- 日志新增一行 `[04_polish] 实际加载模型：<gguf>[<key>]（声明引擎 <key>）`。

### 2026-10-28 · 修：02/05/06 的指纹里混进了 gguf

`MODELS_BY_STAGE` 只登记了 01/03/04，其余阶段 `names=None` 会落到「全部模型」分支 →
换一个**根本没被用到**的 gguf，02/05/06 也会全量重跑（假 cache miss）。
现在显式登记为空列表 = 该阶段不调模型，指纹里不出现任何模型文件。

### 2026-10-28 · 修：解释器路径写死且静默回落

`pipeline.json` 与 `paths.DEFAULTS` 里都写死了一条 `C:/Users/.../python.exe`，
两条都指向已不存在的目录；`python_exe()` 静默退回 `sys.executable`，
看起来一切正常，实际换了环境（依赖可能不同）。

- `pipeline.json` 删掉 `python` 键（公开仓库不该带个人绝对路径）；`DEFAULTS["python"]` 改为
  `KIKOERU_PYTHON` 或空 = 用当前解释器。
- 配置了但不存时**明确告警**，不再静默。原则同 R153：**声明了什么，就必须有一条 assert 实际 == 声明**。

### 2026-10-28 · 修：`serve.py stop()` 在中文 Windows 上抛 UnicodeDecodeError

`taskkill` 输出 GBK，`subprocess.run(text=True)` 默认按 utf-8 解码 → reader 线程抛异常，
日志里多出一大段 traceback。加 `encoding="utf-8", errors="replace"`。

### 2026-10-28 · 新：译文里的日文残留归一（`dejapanize`）

复跑 RJ01654115 时 `check` 硬门禁挂了：**9 行日文残留全是同一个根因** —— `茜ヶ崎椿` 的 `ヶ`。
初译把源语原样留在中文里（`あら、茜ヶ崎椿。` → 「啊 茜ヶ崎椿。」），
旧版是润色模型自己改对的，新版没改 → 于是**能不能过门禁取决于模型这次心情**。

这不该赌。`cleanup()` 里加确定性归一（和已有的「删长音符 `ー`」同一类）：

| 规则 | 例子 | 为什么安全 |
|---|---|---|
| 删「漢+ヶ+漢」里的 `ヶ` | `茜ヶ崎椿` → `茜崎椿` | `ヶ` 夹在两个汉字之间不可能是中文；不碰 `ヶ月`/`ヶ所` 这类量词 |
| 术语假名形 → 中文 | `茜ヶ崎つばき` → `茜崎椿` | **只替换 ja 键含假名**的术语；`先生` 这种纯汉字键绝不碰（中文里是合法词） |

用户拍板「ヶ 删掉」（项目规范零假名，且旧版成品已经是「茜崎椿」）。

### 2026-10-28 · 新：复核可以看**后文**（P1-a / P1-b）

`review_block(prev_zh, block)` 只有前文。但日文的 `でも`/`だから`/`これ`/`それ`、反问句、
预设回答，真正需要看的是**后一句**（R153 实测 131/706 段边界切在句中）。

- `review_block(prev_zh, block, next_zh=None)`：后文 2 句，**只读**，明确写进 prompt
  「禁止修改、禁止输出其 ID」。
- `_drop_foreign_ids()`：模型偶尔会顺手把后文也复核一遍输出出来。`parse_reply_id` 对
  「多出来的 ID」是硬失败（整块重试），所以先按 ID 切段丢掉外来段——那是模型多干了活，不是错误。
- **P1-b 高风险分流**：只对结构性高风险块注入后文，其余块保持原 prompt。判据纯确定性
  （`struct_risk`）：连接词开头 6.4% / 句首指示词 5.8% / 助词开头碎片 3.7%，去重后 **14.4%**
  （全库 23651 段实测）。`KIKOERU_REVIEW_LOOKAHEAD` 或 `--review-lookahead auto|all|off` 可切。
- 后文策略进 `cue_sig`（否则切模式后缓存照样命中，改动等于没生效）。
- 每轨日志新增 `后文注入 N/M 块（conn x/demon y/particle z）`。

### 2026-10-28 · 新：语义覆盖门禁 `semantic`（P2）

`scripts/semantic_coverage.py` + `coverage.json`。拦「原文出现的具体物被中文泛化成公文腔」：

```
おしっこ              → 排泄行为
ちんこ出し入れされる    → 每次那个部位进出的时候
セックスしないと       → 不发生关系就
```

- 判据：命中源语 **且** 本行/相邻行都没有 must 词 **且** 本行出现 forbid 词。只漏译不报
  （那是省略，另一类缺陷）；相邻行放行是为了代词承接。
- 条目带 `level`：`loss`（真信息丢失，门禁按这档计数）/ `style`（公文腔）/ `term`（术语未落地）。
- `--fix --apply`：每条一次定向 Qwen 调用 + **更严**复核（must 出现且 forbid 彻底消失），
  仍不过再按 `swap` 表确定性替换。写盘只替换命中行的文本，时间戳与 `[ti:]/[by:]` 原样保留。
- 管线里作**参考项**（`hard=False`）：全库已知 loss 8 条未修，先报不改；
  与 `trunc`/`overlay` 同一条升级路径——清完再提成硬门禁。
- 门禁从 8 项变 **9 项**。

### 2026-10-28 · 新：碎片行合并工具（P1-c，只做高可靠档）

`scripts/fragment_join.py`。`REVIEW_SYS` 要求「严格 N 行、不许合并」，所以润色层
**永远修不掉** `[02:42.60] 呢。` 这种行。合并会动时间轴，所以只做 A 档 + 人工白名单：

| 档 | 判据 | 处理 |
|---|---|---|
| A | 日文只有句末助词/拖音 **且** 中文只有 ≤3 字语气词 | 可进白名单自动合并 |
| B | 日文只有句末助词，但中文是完整短句（`ね?`→`对吧？`） | 不许自动合并 |

实测全库 A 档只有 **3 条**、B 档 30 条 —— 说明 R151 那一轮已经把碎片清得差不多了，
这条待办的实际收益极低，**不建议再投入**。

---

## v0.1.0-beta.9 — 2026-10-28（引擎与模型对齐 + 初译残留兜底）

### 2026-10-28 · 修：`serve.py start` 加载的模型与 `engine` 声明不一致（R153）

**症状**：成品里出现整段英文（`给 classmates 和 the parents who are paying tuition带来麻烦的事情`），
同时夹带整句拒答、元话语（「这个句子翻译成中文是：」）、日文残留、乱译（デカパイ→「比萨」）。
全库 **115 处 / 16 部**。

**根因**：`_ensure_server` 调 `serve.py start` 时**不带模型参数**，serve.py 就按显存自动选了
`q5`（Qwen2.5-7B）；而 `KIKOERU_ENGINE=hymt` 让 `translate.py` 走 HY-MT 分支——
等于**拿 HY-MT 的裸指令（「把下面的文本翻译成中文」+ 单句、无上下文、temp0.7）去问 Qwen2.5**。
`alive()` 只探端口，「服务已在运行」把错模型原样留下，全链路没有一处校验 gguf。

实测同一句日文：HY-MT 英文 **0/5**，Qwen2.5 英文 **5/5**。

**改动**：

1. `serve.py` 新增 `ensure --<model>`：查 `/props` 的 `model_path`，与目标不符就 `stop` + 重启，
   起完再校验一次，对不上 `exit 2`。输出固定一行 `模型校验通过：<gguf>[<key>]` 供上层断言。
2. `stages._ensure_server(ctx, model=None)` 改调 `ensure`，默认取 `cfg["engine"]`；
   校验失败抛 `RuntimeError`；通过的模型名写进 `03_translate` 的 `extra.server_model`。
3. 新增配置 `polish_engine`（默认 `q5`）。**04 润色必须用聊天模型**：它的输入是
   「20 条规则 + 61 条术语 + 编号协议」，纯翻译模型会把规则当待译文本翻出来
   （2026-10-18 污染过 121 条）。改之前 03/04 共用服务，管线实际全程只跑过 Qwen。
4. `translate.py` HY-MT 分支加**初译残留检测**：命中成串拉丁字母（3+，白名单 `AV/JK/VIP` 等）
   或假名 → 降温到 0.2 重抽（≤3 次）；仍残留则打印 `⚠️ 初译残留(<原因>)` 并计入轨尾汇总。

**效果**（RJ01654115 五轨重跑，同一部作品同一批音频）：

| | 内容行 | 英文行 |
|---|---|---|
| 修复前（Qwen 初译） | 1395 | **19** |
| 修复后（HY-MT 初译） | 1445 | **3**（VIP / 一个罗马字 / 一个作品名） |

`check.py` 17 部全过；`selftest` 13/13；`test_regressions` 门禁全过；`errata_audit --test` 44/44。

---

## v0.1.0-beta.8 — 2026-10-27（空字幕门禁：`empty`）

### 2026-10-27 · 新增硬门禁 `empty`（`empty_check.py`）

- 背景：R142/R143 发现 RJ9000013 有 **19 行成品是空字幕**（ASR 前缀污染 `９接` → 翻译模型整句不译）。
  三道现有检查**全部放过**：`check.py` 只管格式/标点（空文本行被当成**清屏 spacer**）、
  `scan_violations.py` 只看日文侧、`trunc_audit.py` 只抓「有字但被截断」。
- 新口径：**不猜空行的角色，直接拿「应该有的行」去对成品**。
  期望行由 ASR 确定性重建，且**复用 `translate.build_lines`**（翻译/润色共用的唯一真相）。
- 实现要点（三个坑，都会静默给出错误结论）：
  1. **必须 `KIKOERU_CUE_PIPELINE=1`**（默认口径 = 交付口径）。不设会退成旧的显示行模式：
     RJ9000002 重建出 489 行而不是 547 行 → 73 条假缺失。
  2. **ASR 版本优先级** `asr_fix > asr > 其他 > asr_raw`。拿 `asr_raw` 当口径同样对不上。
  3. **漂移守卫**：真缺陷是零星几行（全库 19 / 22800 ≈ 0.08%），版本漂移是整轨成片对不上
     （存量 16 部实测 5%–51%）。单轨缺失率 >3% 且 ≥5 行 → 判 `drift`，只报告不计失败。
- `hard` 按 run **动态**判定：`profile != "adopted"` 才算硬门禁。
  adopt 的 run 是「导入的历史成品」，ASR 与成品本来就不同版（cue 边界/时间戳差 0.4–2s），
  逐行重建必然误报 → 只作参考项。16 部基线因此保持全过（`empty` 显示 ⚠️ 参考）。
- 自检 `--selftest`（4 例）：正常→0 / 挖空一行→1 / 删掉一行→1 / 时间戳偏移 0.2s→0。
  另在真实数据上验过：把 RJ9000002 成品挖空一行 → `缺失 1 行（0.18%）`、exit=1。
- 实测：新跑 run（RJ9000002 全流程）**0/547**；存量 16 部（参考项）7 部有稀疏"缺失"，
  逐条查证**全是版本漂移**（译文在同一句的另一行，起点差 0.4–0.6s，或两段被并进一行）。

### 2026-10-27 · 一条被证伪的假设（记下来防重做）

「每条轨最后一句被丢掉」——`probe_tail` 一度报 **19 轨尾部 ≥3s 未译**（最长 22.5s）。
逐条核对后**不成立**：绝大多数是「末 cue 的译文落在更早那一行」（时间戳差 0.4–2s），
属版本漂移；真截断只有个别（如 RJ9000008 tr01 末 cue `（原句略）`
只译了前半），且**已属 `trunc` 门禁的口径**，不是新缺陷类。

---

## v0.1.0-beta.7 — 2026-10-26（半角标点确定性归一）

### 2026-10-26 · 半角 `? ! ,` 不再靠人工修

- 现象：`check.py` 把半角 `? ! ,` 和孤立 `.` 当**硬门禁失败**；而 ASR 原文经常就是半角
  （`（原句略）`），译文照抄 → 今天两次都栽在这上面，只能 `repair` 手工修。
- 修法：`polish.normalize_halfwidth()` 在 `cleanup()` 末尾统一归一
  （`? → ？`、`! → ！`、`, → ，`、`; : ( )` 同理；`.` 只在不夹于 ASCII 字母数字之间时转 `。`）。
  **只在含中文的行上动**，纯英文/版本号（`OK, fine.` / `v1.2.3`）保持原样。
- 坑：`'啊'.isalnum()` 也是 `True`，用 `isalnum()` 判断会把「中文后的句号」漏掉 → 改用 ASCII 正则。
- 自检：`test_punct_norm.py` 9/9；`test_regressions.py` 门禁全过。

---

## v0.1.0-beta.6 — 2026-10-26（16 部基线刷新 + 两个真 bug）

### 2026-10-26 · 用 7 项门禁刷新 16 部基线

全部 **check ✅ / errata 37/37 ✅ / 回归 ✅**；scan 改为 run 级后每部数字终于不一样了
（RJ9000009 13 条 ↔ RJ9000014 137 条）。trunc / slot_guard 作为参考项入库。

| 作品 | scan | trunc | slot_guard |
|---|---|---|---|
| RJ9000002 | 18/5 | 64 | 46 |
| RJ9000001 | 19/6 | 251 | 43 |
| RJ9000003 | 53/6 | 373 | 140 |
| RJ9000004 | 30/8 | 316 | 3 |
| RJ9000005 | 61/8 | 395 | 85 |
| RJ9000006 | 33/7 | 192 | 82 |
| RJ9000007 | 68/10 | 165 | 104 |
| RJ9000008 | 110/8 | 429 | 57 |
| RJ9000009 | 13/6 | 122 | 40 |
| RJ9000010 | 30/10 | 343 | 144 |
| RJ9000011 | 36/9 | 212 | 18 |
| RJ9000012 | 37/6 | 171 | 92 |
| RJ9000013 | 37/7 | 247 | 285 |
| RJ9000014 | 137/25 | 505 | 100 |
| RJ9000015 | 32/5 | 189 | 46 |
| RJ9000016 | 13/4 | 220 | 67 |

### 2026-10-26 · 修：`effective_asr` 把「候选清单」当成 ASR

- 现象：4 部作品的 `asr_fix/` 里放的是 `asr_fix_scan` 的**候选清单**
  （`[{rule,t,base,other,diff}]`），不是修正后的 ASR。旧判定只看「目录里有 json」，
  于是把候选清单当 ASR 喂给下游 —— `slot_guard` 直接 `AttributeError`（4 部全失败）。
- 修法：`effective_asr()` 现在要求至少一个 json 是 `{"segments": [...]}` 形态，否则退回 `01_asr/`。
- 影响：这 4 部的 slot_guard 从「崩」变成真实数字（43 / 82 / 144 / 100）。
  **如果直接 `run --from 03_translate`，之前会拿候选清单当 ASR 翻译** —— 这是个会静默出错的坑。

### 2026-10-26 · 修：`slot_guard` 不认裸列表 ASR

`load_asr` 直接 `.get("segments")`，遇到裸列表就 `AttributeError`。已兼容 list 形态，
并过滤掉没有 `start` 的元素。

---

## v0.1.0-beta.5 — 2026-10-26（promote 原子化 + 旧入口降级）

### 2026-10-26 · promote 改成逐文件原子替换

- 之前 `shutil.copy2(src, dst)` 直接覆盖：中断会留下**写了一半的 LRC**。
- 现在每个文件走 `copy → 校验 sha256 → os.replace`，单文件永不半截；
  复制校验失败立刻中止并删临时文件。
- 实测（沙箱 `KIKOERU_PROD`）：6 文件 / 改动 3 个 / 无 `.new` 残留 / 二次 promote 改动 0（幂等）。
- 跨文件仍非原子（中断可能新旧混合），所以 `promote` 之后应重跑 `check` 兜底 —— 文档已写明。

### 2026-10-26 · 旧入口降级

- `scripts/run_all.py` 顶部加警示：生产走 `pipeline/pipeline.py`，它只作单步调试。
  `scripts/README.md` 同步。

---

## v0.1.0-beta.4 — 2026-10-26（门禁口径修正 + 两个参考项）

### 2026-10-26 · `scan` 改成 run 级口径

- 现象：`scan_violations.py` 只扫生产目录 `kikoerusubtitle/`，于是 **16 个 run 的 scan 数字全是
  同一个全库值（720 条 / 130 轨）** —— 对「这次 run 有没有变差」毫无信息量。
- 修法：扫描器加 `--lrc-root` / `--asr-root` / `--rj`；管线的 scan 门禁改为
  扫 `runs/<id>/05_term_lock` + `runs/<id>/01_asr`，只扫本作品。
- 实测：RJ9000002 从「720 条 / 130 轨」变成 **29 条 / 6 轨**，报告头会注明口径。

### 2026-10-26 · 两个「抽样清单」进 06_checks

- `trunc`（截断审计）与 `slot_guard`（语义槽守卫）作为**参考项**进生产档门禁
  （精度约 39%，只报不改）。摘要直接数 json 条数：本次 **72 条 / 34 条**。
- 生产档门禁从 5 项变 **7 项**：`check / scan / errata_audit / test_regressions / overlay / trunc / slot_guard`。

---

## v0.1.0-beta.3 — 2026-10-26（锁的实测 + 修复可持久）

### 2026-10-26 · 实测双 key（RJ9000002 强制重跑润色）

```
句级锁定：载入 8 条（4 条原有 + 4 条 repair 登记）
轨1 覆写 1 · 轨2 覆写 2 · 轨3 覆写 2（其中 1 行按日文认回）· 轨4 覆写 2
check.py 全部通过 · run ✅
```
**结论**：时间戳漂移的锁能按日文认回；同句多锁仍不猜（无失配告警）。

### 2026-10-26 · 修：暂存修复重跑即丢

- 现象：`repair --target run` 改的是 `04_polish`，下一次 `run --force` 重跑润色就**被模型输出冲掉**
  （实测 4 行修好的内容全回来了，`check.py` 再次报日文残留/半角）。
- 修法：`repair --lock` —— 把修好的行连同**日文**写进 `data/locks/<RJ>.json`。
  锁在 `apply_lock` 里逐字覆写，于是重跑也能复现人工决定。
- 实测：加锁后再 `--force` 重跑，4 行**全部保留**，门禁全过。

### 2026-10-26 · 修：锁计数器全局累加

`LOCK_N` / `LOCK_JA_HIT` / `LOCK_AMBIG` 之前是模块级累加，日志里每轨都报同一个数
（看起来像「每轨覆写 3 行」）。改为**每轨清零**，现在每行的数字是该轨真实值。

---

## v0.1.0-beta.2 — 2026-10-26（E1 锁双 key）

### 2026-10-26 · 锁不再只认时间戳

**问题**：锁按「文件|时间戳」存，重跑 ASR 后分段变了 → 时间戳漂 → 人工修正静默丢失
（实测 RJ9000002 4 条锁只命中 2 条）。

**做法**（GPT 评审 Q11）：
- 新增 `scripts/lock_match.py`（纯函数）：`norm_ja` 规范化 + `build_ja_index` + `lookup`。
- 新增 `scripts/lock_migrate.py`：从 ASR 回填 `ja`/`ja_norm` 到现有锁。
  **实测 3224 条锁补上 3138 条（97.3%）**，86 条补不到（该时间点没有 ASR 段）。备份在 `locks/_backup_20261026/`。
- `polish.apply_lock(fn, ts, text, ja)` 改为双 key：**时间戳 → 规范化日文（仅当该轨内唯一）→ 旧中文**。
  同句多锁一律**不猜**，记 `LOCK_AMBIG`，收尾打印「锁失配待人工 N 处」。
- 日志区分「覆写 N 行」与「其中 M 行按日文认回（时间戳已漂）」。

**验证**：`test_lock_apply.py` 7/7（主 key / 漂移认回 / 多锁不猜 / 已等不算覆写）；
`test_regressions.py` 门禁全过；真实载入 RJ9000002 锁 4 条（带日文 4 条，JA 索引 3 键）。

**行为变化**：重跑 ASR 时人工修正能按日文认回；但**同句重复**的锁不再自动套用（会打印失配），
这是刻意的——宁可报给人，也不把锁套到错误的行。

---

## v0.1.0-beta.1 — 2026-10-26（首个封装版本）

**状态**：测试版。16 部存量已接管为基线（`runs/*_adopted`），RJ9000002 用本管线全流程重跑验证通过。

### 2026-10-26 · 封装与目录收敛

- 代码从仓库根目录收进 **`pipeline/`**：`pipeline.py` + `pipeline_lib/` + `tools/` + `pipeline.json` + `VERSION` + 本文件。
- 新增 `pipeline.py version`（读 `VERSION`）与 `pipeline.py selftest`（13 条 P0 断言，沙箱内跑）。
- 只读看板 `tools/dashboard.py` → `runs/dashboard.html`（无 JS、无外部资源、零写入）。

### 2026-10-26 · 执行层（六阶段 + 追溯）

- 六阶段固化：`01_asr → 02_asr_fix → 03_translate → 04_polish → 05_term_lock → 06_checks`。
- `runs/<run_id>/manifest.json`：每阶段记 params / inputs / outputs / knowledge / **code 哈希** / gate / 耗时 / 重跑原因。
- 阶段指纹 = 参数 + 输入哈希 + 相关知识表哈希 + 代码哈希 + **resolved config 哈希 + `KIKOERU_*` 白名单 + 模型 stat**（`pipeline_lib/fingerprint.py`）。
- 断点续跑：`--from / --only / --force`；失败只重试该阶段；`explain` 回答「这句为何被改」。

### 2026-10-26 · 评审后加固（P0）

- **05 拆独立目录**：`05_term_lock/<RJ>/` 才是最终产物，`04_polish` 永远不可变。
- **锁全部 token 化**：释放只删自己的；过期锁用 `os.replace` 原子夺走；**PID 存活绝不抢**。
  新增 run 锁 `runs/<id>/.run.lock`、生产锁 `tmp/locks/prod_<RJ>.lock`。
- **promote/repair 安全**：作品号断言、`track` 只允许 basename、promote 核对文件集（多余旧 LRC 拒绝）、
  repair 默认 `target=run`（改生产需 `--confirm-prod`）。
- **知识审批事务化**（`knowledge.approve`）：锁 + 幂等 + 去重 + 临时文件自检 + 原子替换 +
  快照失败回滚 + `commit_pending.json`。
- **状态机**：`created / running / ok / gate_failed / failed / interrupted / incomplete / dry_run / imported`。
- **知识归因**：`knowledge_at_creation` 只写一次，续跑只追加 `knowledge_at_resume` + `knowledge_drift`。
- 修掉一个真 bug：`| head` 截断导致 stdout `OSError(22)` 被误判成阶段失败。

### 2026-10-26 · 验证与基线

- `adopt --all`：16 部接管，门禁 16/16 通过（`errata_audit` 37/37、扫描 720 条 / 130 轨）。
- RJ9000002 全流程重跑：ASR 5.8 分 / 初译 1.3 分 / 润色 4.4 分 / 门禁 0.5 分，门禁 5/5 全过。
- 发现并修正「默认口径 ≠ 交付口径」：`cue_pipeline` 默认改 1（交付是 cue 模式）。

### 2026-10-26 · 磁盘整理

- 释放 **1.85 GB**：三包标注音频（标注已归档 800 条到 `tmp/label_archive/`）、
  parallel 旧备份 6 份（留最新 1 份）、已结案的实验产物、`anime-whisper-ct2`（1.5 GB，废弃模型）。
- 逐项清单见维护者内部台账（不入库）。

### 已知缺口（下一版）

- **E1 锁的双 key 只做了一半**：`lock_match.py` + `lock_migrate.py` 已完成（3224 条锁补上日文，覆盖率 97.3%），
  但 `polish.py` 还没接 `lookup()` —— 也就是说**锁仍然只按时间戳命中**。
- `scan_violations.py` 仍是全库口径（缺 `--rj`），run 级 scan 数字是全局的。
- `slot_guard` / `trunc_audit` 还没进 `06_checks`。
- `promote` 逐文件复制，非原子替换（靠备份兜底）。