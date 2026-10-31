# kikoeru-subtitle 脚本说明

生成字幕的 skill（skill A）：音频 → 日文识别 → 翻译 → 润色 → 校验 → LRC 成品。

> 完整流程与踩坑见上一级 `SKILL.md`。本文只讲**每个脚本是什么、怎么单独调**。
>
> ⚠️ **2026-10-26 起：生产执行走 `work/pipeline/pipeline.py`，不要再在对话里手串这些脚本。**
> `run_all.py` 也降级为「单步调试用」，不再是交付入口。
> 本目录的 `.py` 是**工具层**（单个脚本仍可独立调用、可被管线当阶段调用）；
> 编排/断点续跑/GPU 互斥/manifest 追溯在 `pipeline_lib/`，说明见 `docs/PIPELINE.md`。

## 依赖关系（数据流）

```
音频 ──asr.py──> asr/*.json（日文+词级时间戳）
                    │
        ┌───────────┴───────────────────────────────┐
   rescan_head.py 补扫开头（整轨丢首窗时）      （可选）clean_hallu.py 清洗套话幻觉
   collapse_rep.py 折叠重复循环幻觉              （可选，二者都改 asr/*.json）
                    │
   translate.py ──> 初译 draft/*.lrc
                    │
   polish.py ──> 成品 lrc（润色/后处理）
                    │
   polish.py 收尾自动跑 term_lock.py（称谓一致性锁定，确定性覆写）
                    │
   check.py / audit.py 校验与审计
```

翻译/润色都要先起 `serve.py`（llama-server）。

> **改了 `asr/*.json` 的脚本（`rescan_head.py` / `collapse_rep.py` / `clean_hallu.py`）
> 之后，必须重跑 `translate.py` + `polish.py`**——行数变了，旧译文会错位。

---

## 脚本清单

### serve.py —— 推理服务管理（前置，先跑）
本地 llama-server 的生命周期管理器。
```bash
python serve.py status          # 看服务状态与显存
python serve.py start           # 默认按显存选 Q5/Q4（Qwen2.5-7B）
python serve.py start --sakura  # ★ 当前默认翻译引擎（日译中专训）
python serve.py start --qwen3   # Qwen3-8B（需 KV 量化）
python serve.py stop
```
- **换翻译引擎只改 `start` 参数**，translate/polish 不用动。
- ⚠️ 服务生命周期只跟**单次 bash 调用**：`start` 和翻译脚本必须写在同一行 `start && 脚本`。
- ⚠️ 长任务不能接 `| head`（SIGPIPE 静默掐断，退出码仍 0）。
- ⚠️ 每槽上下文 = `-c / -np`。**q5/q4 用 `-np 2`（每槽 12288 tokens）**：
  强润色 system prompt 已达 7220 tokens，3 槽的 8192 会被 llama.cpp 截断 prompt 开头
  （2026-10-16 实测，见 SKILL.md 已知坑 54）。polish 的 `WORKERS=2` 也用不满 3 槽。
- ⚠️ 本沙箱里 `serve.py start` 起的 llama-server 会被 Job Object 收割（父 bash 调用一结束
  子进程就被收掉 → 后续全 502）。**正确姿势 = 把 `llama-server.exe -m <gguf> -c 24576
  -np 2 -ngl 99 -fa on -t 8 --host 127.0.0.1 --port 8080` 本身作为后台任务的直接命令**。

### fetch_script.py / extract_script.py / script_align.py —— 台本链路（有台本时替代 ASR 文本）

asmr.one 上部分作品带台本（`type=='other'` 的 PDF、`type=='text'` 的逐轨 txt）。
有台本就用台本打轴：**台本文本准确 + 带说话人标记，ASR 只当时间锚点**。

```bash
# ① 检测 + 下载 + 提取（传 RJ 号即可）
python fetch_script.py 1715662
#    → data/draft/RJ1715662/script/*.txt
#    返回 0 = 有台本；返回 1 = 没有，走正常 ASR 流程

# ② 单独提取竖排 PDF
python extract_script.py <台本.pdf> --out x.txt [--raw] [--no-sound]

# ③ 打轴：台本文本 + ASR 时间轴 → 与 asr.py 同格式的 JSON
python script_align.py <script目录> <asr_raw目录> <asr目录>
```

- **竖排 PDF 必须按坐标重排**：pypdf 直接提取会得到一列单字。`extract_script.py` 用
  pdfplumber 拿 `(x0, top)`，竖排走「列内 y 升序 + **列间 x 降序**」（日文縦書き从右往左读列），
  横排走「y 升序 + x 升序」，`is_vertical()` 自动判。
- **对齐必须字符级**：台本行断是排版断行，句子级 DP 只有 48%；字符级（`difflib`）全量 72–76%。
  ⚠️ **「全量 70%」不是质量指标**：台本 27–28% 的字是拟声台词（`（原句略）`）+ 演出指示，
  ASR 本就不会匹配。`align_stats` 里看 **`dialogue_match_rate`**（实测 100.0%），
  `char_match_rate` 只作参考。
- **演出指示用匹配率剔除**：`（原句略）` 这类不会被说出来 → 字符对齐里是连续未匹配段，
  按「连续 ≥4 个未匹配实字」删（正则猜不准，台词里也有「音」字）。
- **E5 待复核清单（2026-10-21）**：逐句算「保留字里匹配上的比例」，<60% 写
  `review_queue.json`（落在打轴输出目录，review.py 也会找 `draft/<rj>/asr/` 下那份）。
  句内**无汉字**（`うふふ♡`/`あぁ～ん`）记 `kind=nonlex`——拟声/笑声本就不匹配 ASR，
  单独统计；**带汉字台词**才记 `suspect`，review 审阅页打「台本待复核」标、可筛选。
  RJ9000014 实测 74/2900 句 suspect（匹配率照报 87.8%，但可疑句不再混在成品里）。
- **台本排版有 4 种**（整本 PDF / 逐轨 txt / 逐轨 PDF / 按章），解析看**文件名 + 内容标记**：
  `tr01`、`1_….pdf` 都要能取到轨号；「按章」无轨道标记时**直接报错**，不许把整本塞给第 1 轨。
  同一作品常有多份同源台本（PDF 提取 + 全章 + 分章），`pick_txts()` 只取一份。
- **多语言台本**（`別言語/英語|簡体|繁体`）只取日文目录，其余跳过。
- 产物额外带 `segments[].spk`（台本的 `【みう】`/`【ねね】` 说话人标记），polish 已消费。
- ⚠️ **打轴仍要跑 ASR**（时间锚点），台本解决文本准确、不省 ASR 时间。

详见 `docs/PIPELINE.md` 的台本打轴一节。

### asr.py —— 音频识别（faster-whisper large-v3）
```bash
python asr.py <音频目录> <输出目录> auto [--recursive] [--device cuda] [--compute float16]
```
- 音频在子目录时加 `--recursive`（如 `本編(mp3)/`、`mp3/`）。
- VAD 默认**关闭**（音声作品里喘息会被 VAD 并成长段导致丢词，实测关闭更好）。
- 切换模型用环境变量 `FW_MODEL=路径`（如 turbo）。
- **静音过滤（2026-10-19 修）**：`is_silence_seg()` = `no_speech_prob ≥ FW_NO_SPEECH(0.85)`
  **且** `avg_logprob ≤ FW_NO_SPEECH_LP(-1.0)`。⚠️ **不能只看 nsp**：ASMR 有 BGM，
  软语段 nsp 常到 0.86–0.94 而 logprob 只有 -0.2，只看 nsp 会成片删真台词
  （RJ9000002 Tr.06 一轨删掉 27 段 / 0–54 秒开场整段）。
- `FW_NS_THRESHOLD` 会把它传给 `model.transcribe()`——**实测无效果**（2026-10-19 三臂 A/B），
  留着只作复现用，别再靠它治「开头丢 30 秒」。
- **幻觉三门槛（2026-10-20 接）**：`is_ts_hallucination()` 复用 `asr_halluc.scan_one`
  （A 幻觉词独占 ≥8s 且 ≤15 字且词跨 ≥10s / B 词级时间戳崩塌），两个收集循环都接了。
  命中的段写进 JSON 的 `dropped_halluc` 字段（可审计）。free talk 的感谢/晚安不判。
  全库 22843 段实测只命中已知 21 条、零新增误杀。

### asr_fix_scan.py —— ASR 改错阶段（2026-10-24 新增，插在 asr 与 translate 之间）

**为什么**：初译拿到的是带错字的日文，错字会一路带到成品。而错误常常只有「看整轨」才看得出——
RJ9000006 轨01 的「象」应为「お母」，佐证（`（原句略）`）在 **95 秒之后**，
cue 级上下文看不到。

**三条确定性规则**（不依赖模型，可批量）：
- **A1 同读音不同字**：两版按时间中点 ±1.5s 对齐、**按读音**比较（读音同=正字法差异，不是错）。
- **A2 内容不同**：读音也不同（断词/听错/改述）。
- **A3 字符级差异**（2026-10-24 晚新增，坑 129）：整轨拼成字符串做 difflib 字符级对齐，
  差异按累计偏移映射回段。**A1/A2 在两版段数不同时会整体错位**（RJ9000014：台本 254 段 vs ASR 274 段），
  真错字被跳过；A3 修掉这一点（同音错误召回 71.9% → 100%）。
- **B 同轨重复怪词**：同一 token 在一轨出现 ≥2 次且词典外/生僻汉字 → 系统性误识候选。

**已知盲区**：**两版一致 ≠ 正确**。`ゾウ` 在 v1/v2 都出现（`象って…` / `ゾウって…`），
规则 A 抓不到，只能靠 B + 整轨语义复核（Gemini / 人工）。

⚠️ **轨名解析**：两个版本目录的命名不统一（`<track>.mp3.json` vs `<track>.json`），
`_resolve()` 做三级回退；否则整部作品静默 0 候选（坑 126）。
⚠️ **有台本的作品 `asr/` 是台本文本**（script_align 覆盖过），别拿它当「第二版 ASR」（坑 127）。

```bash
# 1) 只出候选（写 asr_fix/<track>.candidates.json + .review.md）
python asr_fix_scan.py --rj RJ9000006 --track "01 xxx.mp3"

# 1b) 整部作品一次扫完（R109 日志化；--jsonl 追加写全部候选）
python asr_fix_scan.py --rj RJ9000014 --jsonl ../regset/r109/candidates.jsonl

# 2) 人工/LLM 确认后落地（写 asr_fix/<track>.json，带 fix_log，不覆盖 asr/）
python asr_fix_scan.py --rj RJ9000006 --track "01 xxx.mp3" \
  --apply-fix "14.7=いやぁ、お邪魔悪いねー。" --apply-fix "58.4=お母さん、誰もいないんだっけ"
```

### asr_fix_scan.py 的 errata 自动替换（`auto_apply_errata`，2026-10-26 R133 改造）

`data/style/asr_errata.json` 是**已核实的 ASR 错字表**，`run_all.py` 在 asr 之后
自动跑一次（写 `draft/<rj>/asr_fix/`，不覆盖 `asr/`）。**不调模型，纯确定性替换。**

| 字段 | 作用 |
|---|---|
| `ja` / `fix` | 错形 → 正形 |
| `mode` | `auto` 直接换 / `guard` **必须命中 `ctx`** / `review` 永不自动换 |
| `ctx` | **正向上下文白名单**（正则列表，任一命中即可）。命中不了 = **新语境** → 不换，落 `new_ctx` |
| `works` | 作用域（RJ 列表）。范围外一律不动，落 `scope_skip` |
| `block_if` | 反例枚举（已知「错形出现在更长合法词里」的正则）→ 命中则不换 |
| `legit` | `0` = 已核实全库出现都是误识（审计工具据此豁免） |
| `all_ok` | `true` = 同段多次出现也全换（默认只换 count==1 的段） |
| `version` / `evidence` | 定档版本 + 证据 |

```bash
# 表内自检（链式替换 / 子串重叠 / guard 缺 ctx / 坏正则）
python -c "import sys;sys.path.insert(0,'.');import asr_fix_scan as A;A.validate_errata(True)"

# 运行时监控（applied / ctx_hit / new_ctx / scope_skip / boundary_skip / multi_skip）
python -c "import sys;sys.path.insert(0,'.');import asr_fix_scan as A;print(A.errata_stats())"

# 离线复刻整库判定（覆盖率 + 新语境样本，改表后先跑这个）
python ../../../tmp/daily/errata_ctx_check.py --json ../../../regset/r133/ctx_check.json
```

⚠️ **三条铁律**（坑 146–148）：
1. **不用排除式负向断言 `(?!…)`**——漏新语境、复合词内部误命中、未来语料静默误改。
2. **反例穷举会判死条目**：语料里出现全是正确用法（`怪我` 5/5、`暴走` 4/4）→ 降 `review`；
   写不出正向条件又删不掉 → 降 `review` 或缩 `works`。
3. **errata 修的是 ASR，成品不会自动变好**——成品是旧 ASR 译的，字面译会留着。
   加条目后必须 `grep -rn "写生\|处世\|战士\|八寸棒" ../../../../kikoerusubtitle --include=*.lrc`。

### adjudicate_asr_fix.py —— 候选裁决（绿/黄/红，2026-10-24 R112 新增）

读 `regset/r109/candidates.jsonl`，只对 A3 候选分三档：

| 档 | 判据 | 动作 |
|---|---|---|
| 🔴 red | 表记差异（同读音 + 一侧假名）、纯漏识/多出 | **禁止自动改** |
| 🟢 green | 同读音 + **双侧含汉字** + 候选写法在同轨别处出现过 | 自动替换 |
| 🟡 yellow | 读音不同 / 双侧汉字但无锚点 | 人工或 Gemini 复核 |

dev 89 轨实测：绿 23 / 黄 2674 / 红 2642；**绿灯人工逐条核验 23/23 全真**（`写生→射精` 20 条）。
输出 `regset/r112/verdicts.jsonl` + `R112.md`。**别放宽绿档**——969 条同读音候选里 94% 是表记差异。

**管线接线**：`run_all.py` 在 translate/polish 前检查 `draft/<RJ>/asr_fix/*.json`，
存在就用它、不存在用 `asr/`，并打印用了哪个目录。`translate.py` / `polish.py` 本身不用改。

**落盘校验口径**：段数、时间轴必须与主版本完全一致（只改 text + 加 fix_log）。

### translate.py —— 机器初译
```bash
KIKOERU_ENGINE=sakura python translate.py <asr目录> <初译输出目录> [文件名关键字]
```
- `KIKOERU_ENGINE=sakura` 走 Sakura 通道（默认是 Qwen 通道）。
- Sakura 用 temp 0.1 / top_p 0.3，**只给正例不给反例**（反例会被照抄）。

### polish.py —— 润色 + 后处理（核心）
```bash
python polish.py <asr目录> <初译目录> <输出目录> [--style DIR] [--system-mode strong|conservative] [文件名关键字]
# Sakura 模式必须加 --cleanup-only（不做 LLM 润色，只做确定性后处理）
```
- `--cleanup-only`：只跑确定性后处理（标点归一、重复压缩、标签剥离），不过 LLM。
  **Sakura 初译必须用这个**，否则 Qwen 润色会整体错位一行。
- **A8 遮蔽器（默认开）**：润色层自动加载 `mask_nonlexical`，把拟声/气声换成 `<M1>`
  占位符防字面翻译（`KIKOERU_MASK_NONLEX=0` 关）。校验失败**单句回退初译**。详见下节。
- `--system-mode conservative`：**保守润色**（B1 实验，2026-10-16）——743 字符的最小改动
  prompt，只修术语/错译/漏主语/日文残留/标点五类，绝不做语气词/叠词/重写/删冗/改语序。
  其余流程（分块/ID 解析/审校/cleanup/门禁）与强模式逐行相同。驱动：
  `tmp/daily/b1_run.py`（9 部 11 轨 × 两臂 → `b1_out/`），对比 `tmp/daily/b1_compare.py`。
- 关键后处理都在 `cleanup()`：句内逗号降级为空格（soften_commas）、重复字符压缩（防无限循环）、
  字段标签剥离、日文长音符清理。
- **纯标点丢句守卫（2026-10-19）**：模型偶发整句只吐「…」/「？」，`bad_cell` 现在会在
  「中文无实义字符 **且** 日文有实义字符」时判坏 → 自动回退该句初译（不丢行）。
  日文本身是 `…` 的不判（合法停顿行）。全库存量 43 行已用 `tmp/daily/fix_punct_lines.py` 清理。
- **标点规范（2026-10-08）**：句末用句号「。」收束，句内短停顿用空格。
  注意 `cleanup()` 里**保留句号**（之前「句末不加句号」已废弃，那会把句号→逗号→空格
  连环抹掉，是机械感元凶）。换翻译引擎时必须重新审视 cleanup 规则。
- ⚠️ system prompt 缩减循环在「静态部分超 SYS_MAX」时有死锁守卫（样例减到 4 即 break+
  警告），见 SKILL.md 已知坑 54；真实容量约束是**每槽 token 数**，不是字符数。
- **说话人名泄漏守卫 `spk_leak()`（2026-10-23 R28，坑 107）**：台本轨的「人：白/甘」
  字段会被模型抄进译文，实测 **5 种格式**（整行/行首/行尾 `人：X`/行尾裸名/行尾 ` X。`），
  10-22 的 `startswith` 守卫只防住 2 种，成品漏了 36 行。命中判坏格 → 退回初译。
  ⚠️ 行文人名不判（「我明白。」「把脸涂白。」「白天」是合法行文）。
- **LRC 清屏 spacer（2026-10-23 R23，坑 106）**：写盘层按 `cues.spacer_rows()` 在
  「本行 end 与下一行 start 有 gap」时补空文本行，让 cap_holds 的「读完即走」真正生效
  （LRC 语义下行显示到下一时间戳为止，不写 end 就失效）。**默认关**
  （`KIKOERU_LRC_END_SPACER=1` 开；全量生效 = 65% 行中位提前 2.4s，待用户听辨）。
- **轨级记忆注入（`KIKOERU_TRACK_MEMORY=1`，默认关，坑 136）**：`load_mem()` 读
  `draft/<rj>/track_memory/<轨名>.json` 压成一段 prompt，内容是整轨背景（时间窗主线 /
  固定译法 / 台本写法 / 已知错字）。2026-10-25 修了三处结构问题：
  ① 记忆文本已进 `cue_sig()` 签名（原来不进 → 改记忆后缓存照命中，改动不生效）；
  ② `extra` 里的顺序改成 称谓锁 → 术语/变体 → 记忆（截断砍尾部，所以记忆先被砍，
     硬约束保住）；③ 记忆的固定译法与 `glossary.json` 对账，冲突项丢弃并打印。
  存量极端尾用 `tmp/daily/fix_lrc_linger.py` 修（1642 行已修，幂等）。

### run_all.py —— 一键全流程
```bash
python run_all.py <音频目录> <输出文件夹名> [--ext auto] [--recursive] [--only asr,translate,polish,check] [--script-dir DIR]
```
- 第 2 参数是**输出文件夹名**（一般用 RJ 号），不是 RJ 号本身。
- `--only` 逗号分隔，识别结果会缓存，重跑翻译不用再识别。
- `--mode retranslate`（2026-10-21）：只跑 `translate → polish`（等价 `--only translate,polish`，
  与 `--only` 互斥）。术语表/风格库/润色 prompt 改动后重出译文用，识别与台本打轴吃缓存不重跑。
- `--script-dir`：给了且目录存在 → 走**台本路径**（`asr → script_align → translate → polish`），
  ASR 原始结果存 `asr_raw/` 只当时间锚点，打轴产物写回 `asr/`（下游零改动）。
  不给 → 原流程。

### term_lock.py —— 称谓/术语一致性锁定（确定性后处理）
```bash
# 预览（不写盘）
python term_lock.py <LRC目录> [--ja-dir <ASR目录>] [--only sensei,kyu]
# 实际覆写
python term_lock.py <LRC目录> --ja-dir <ASR目录> --apply
```
**为什么需要它**：LLM 对「汉语合法词」的二选一会摇摆——同一部作品里同一个人物，
3 秒内「先生」「老师」交替出现（RJ9000016 实测，见 SKILL.md 已知坑 58）。
prompt / 术语表 / hard_rules **三者都在线也挡不住**（已核实注入链路没断），
根因是「先生」在中文里合法，模型不认为它是错的。
→ **一致性只能靠确定性覆写**：全篇数票选锚（票多者胜）→ 其余变体统一改成锚点。
实测 RJ9000016 Tr.03：`先生 8/老师 19` → `先生 1/老师 25`。

- 锁定组：`sensei`（老师/医生/律师/议员）、`kyu`（你）、`danna`（老公）、
  `goshujin`（主人）、`okyakusama`（客人）。
- **语境分化**（`detect_sensei_context`）：全篇共现词投票判「先生」该译什么——
  `生徒/授業/学校` → 老师；`病院/診察/患者/注射` → 医生；`弁護士/裁判` → 律师。
- **三个安全边界**：① 跨组互斥（同一写法不能被两组选为锚）；
  ② 「先生」男性义保护（前面是「那位/这位/一位/某位/老公/丈夫」则不改）；
  ③ 日文侧校验（`--ja-dir`，只有该行日文含触发词才改）。
- **默认 `--dry-run`**，必须 `--apply` 才写盘。
- 已接入 `polish.py` 收尾（自动跑，`--no-term-lock` 关）。
- **C1-3 说话人分桶锚（2026-10-21）**：ASR `segments[].speaker`（diarize 的
  `SPEAKER_xx` 或台本角色名）→ 每行 ±2s 窗按重叠时长取主导 spk → 每锁组**按桶**
  分别数票选锚；≥2 桶才启用、桶内 ≥2 票才给锚、与作品锚相同的桶不记。
  单桶/无 spk → 不分桶（行为同旧版）。防「双角色一部统一成一种称呼」的互覆。
- **测试**：`test_term_lock.py`（26 项断言，含边界）——改 term_lock 必跑。
- **禁用译法表 `BAN_TERMS`（2026-10-25，用户拍板）**：`{"欧派": "胸部"}`，
  **无条件覆写**（不看票、不看日文语境、不看 `--only`），`KIKOERU_NO_BAN=1` 可关（做对照用）。
  ⚠️ **不要**把禁用词加进 `LOCK_TERMS[...]["variants"]`：那组锚是「票多者胜」，
  禁用词一旦在某作品里出现最多，反而会**变成锚**（反向失效）。
  语料里「おっぱい」973 句中有 203 句译成「欧派」（集中在 6 部作品的译者习惯），
  而 `oppai` 组的 variants 只有 `[胸部, 乳房]` → 模型吐「欧派」时旧版**没有任何机制纠正**。
  用户明确：`奶子/奶` **不禁**（"那就粗俗一点"，更粗的作品留口子）。
- **允许集校验 `--check`（2026-10-25，只读）**：读 `style/terms_db.json` 的允许集/禁用集，
  逐轨判 ①行内出现禁用译法（按行）②该术语本轨日文出现 ≥2 次但全轨中文从未用允许译法（按轨）。
  **必须按轨判 `never_used`**（LRC 是显示行，按行判会出 1015 条假阳性），
  并且复用 `scan_violations.pick_asr` 按轨选 ASR 版本（坑 90）。
  ```bash
  python term_lock.py <LRC目录> --check --json out.json
  ```

### test_term_lock.py —— term_lock 边界测试
```bash
python test_term_lock.py
```
覆盖：师生/医生/律师语境判对、「那位先生」男性义保护、日文侧门禁、
跨组互斥、锚点冲突检测、「君→你」、时间戳解析。

### entity_ledger.py —— 作品级实体/称谓账本（源语 mention 级）
```bash
python entity_ledger.py <作品目录> [--meta <meta.md>] [--json]
```
**为什么需要它**（GPT 第八轮判决）：`先生→老师` 是**词级规则**，而
「这 27 次先生都指同一位家庭教师」是**实体级事实**。分块 7B 无法可靠维护后者。
→ 建一层**作品级账本**：从 **作品名 + Tag + 轨名** 推称谓族，
再按**源语 mention** 给每个块注入锁定（不是按中文数票——那可能是错位换来的假一致）。

- **族注册表 `ENTITY_SPECS`**（10 族）：`sensei / onii / onee / senpai / goshujin /
  okyakusama / danna / mama / papa / kyu`。
- **判据收紧**（实测踩坑）：光有 tag「学生」**不足以**推师生关系
  （RJ9000007 有「学生」tag 但无师生关系 → 曾误判成老师）。
  必须命中**强关系词**（家教/家庭教師/担任/教え子/補習/授業/先生，×3），
  弱身份词（学生/JK/制服，×1）。
- **mention 门禁**：源语全篇无该称谓 → 标 `dormant`，不注入（空转约束只制造噪声）。
- ⚠️ **`zh_family` ≠ `to_converge`**：前者含合法礼貌形式（「老师**您**」），
  拿它当收敛池会误删「您」；后者才只放**必须收敛的对立写法**（先生/医师/大夫…）。
- 已接入：`polish.py` 启动 `load_ledger()`，`polish_block` 注入本块命中（实测 108 字符/块）。

### term_repair.py —— 源语驱动的确定性称谓修复（GPT Q4 第一层）
```bash
python term_repair.py <LRC目录> --ledger <账本json> [--only sensei] [--apply]
```
只做**可识别实体错译**的自动修（第一层）：
```
源语「先生」 + 中文命中「X位先生」：
  · 源语无指示词（この/その/あの） → 删「这位/那位」+ 改锚点
  · 源语有指示词                  → 保留指示词 + 改锚点
中文裸「先生」 + 源语有先生 + 非专名 → 改锚点
```
- **专名保护**：`（原句略）`（人名+先生）不动；`name_guard_for(alias)` 动态构造。
- ⚠️ **昵称/爱称必须保护**（用户指出）：**「肛门先生」是调情式爱称**，不是尊称。
  判据 = **引号内** 或 **前缀是身体词/戏称**（`肛门|屁股|变态|抖M|美臀|肉棒…`）→ 不动。
- ⚠️ **变体池为空时正则要守**（`if variant_re:`），`to_converge` 天然可能为空（如 kyu 族）。
- 已接入 `polish.py` 收尾：**先 term_repair（源语修复）→ 再 term_lock（变体收敛）**，
  `--no-term-lock` 同时关两者。
- **测试**：`test_term_repair.py`（22 项：14 单族 + 5 多族 + 3 昵称保护）——改必跑。
- 实测 RJ9000016 Tr.03：`先生 8 / 老师 19` → **`先生 0 / 老师 27`**（源语 27 处逐一对齐）。

### token_budget.py —— 润色 prompt 的 token 预算诊断（GPT Q5）
```bash
python token_budget.py [--slots 2] [--ctx 24576]   # 需 llama-server 在跑
```
用 llama-server 的 **`/tokenize` 端点实测**（不是字符数换算——中文 1 字 ≈ 1–2 token，
日文/符号差异更大）。输出各部分 token 数与每槽余量。

**实测结论（2026-10-16）**：强润色 system = **7866 tokens**（每槽 12288）：
模板正文（规则 1–19）**3387 / 43%** ＞ hard_rules 3278 / 42% ＞ style_guide 441 ＞
**16 组样例 437 / 6%** ＞ 术语表+账本 ~108。
→ **GPT 猜「16 组样例是负担」不成立**（仅 6%）；余量 2422 tokens **充足，当前无需瘦身**。
若将来要瘦身，应优先精简**规则 1–19**（是用户规范，须用户确认），**别删样例**。


### mask_nonlexical.py —— A8 非实义 span 遮蔽器（拟声/气声防字面翻译）
```bash
python mask_nonlexical.py            # 自检（29 用例 + 6 restore_zh 校验）
```
**为什么需要**：prompt 说了「别把拟声/气声按字面译」，但 7B 有时不听——`もぅ`→「已经」、
`ビリビリ`→「电击」。**prompt 是软的，遮蔽是硬的**。
- 机制：`mask_ja(ja)` 把非实义 span 换成 `<M1>…` → 模型译完 → `restore_zh(zh, spans)`
  把占位符换成**中文对应**（不是还原回日文！`もぅ`→「唔」）。
- **接入点 = polish.py 润色层**（**不是**初译层）：初译是 HY-MT 逐句专用模型，遮蔽会破坏输入；
  真正会「按字面译」的是润色层 Qwen。`KIKOERU_MASK_NONLEX=0` 可关掉（A/B 对照）。
- 校验失败处理：**单句回退初译**（`stats_mask_fail` 计数），不再是「整块 return None」——
  后者代价过大（一条丢占位符废掉整块）。汇总会打印「A8 丢占位符回退 N」。
- 优先口径：**宁可漏判、不可误判**（实义内容被遮比放过 10 个拟声更严重）。
  误判收紧教训：`ここ`/`てて`/`どんどん`/`そろそろ`/`（原句略）` 曾被误遮 →
  平假名重复**只对气声字**生效 + `SAFE_REPEAT` 白名单；`（原句略）`（念「零」是台词）→
  `KATA_SAFE` 补数字 + REPEAT **母词守卫**（单元是实义**多字**词则不遮）。
- 实测：18655 cue 里 1049 含遮蔽（4.8%）；B2 效果验证三类拟声违规仅 0.107%（真问题 0.027%）。
- ⚠️ **两个实现坑（2026-10-16 深夜修）**：
  ① **所有 `polish_block` 调用点都要传 `mask=MASK`**——曾只在重试路径传了，主路径（波次并行）
     漏传 → A8 **静默半失效**（日志 `A8丢占位符回退 0` 看着正常）。加开关参数必须 grep 全部调用点。
  ② **跨句占位符漂移**：模型会把别句的 `<M1>` 搬进本句，`restore_zh` 只校验「本句应有的 span」抓不到
     → `bad_cell()` 加占位符守卫（残留 `<M数字>` 判坏格 → 退回该行初译），门禁 `=== 3d2.`。


### asr_latin_halluc.py —— ASR 拉丁幻觉清理（2026-10-28 R154，01 层末尾自动跑）

Whisper 在非语音段（喘息/摩擦声）很自信地认为「有人在说话」，解码器找不到日语 Token
就吐拉丁水印：`う・・・ Holmes Meanwhile in came off`、`はぁ… Huh, Ah, no, sorry!`。

两条确定性判据：
- **A** 非白名单拉丁字符占比 > 0.4 → 整段丢弃
- **B** 有 ≥4 连续非白名单拉丁，且 `avg_logprob < -0.8` 或 `nsp > 0.4` → **只摘拉丁词**保留日文
  （整行丢会把真台词一起杀了：`あ、白い方?…Viveだからね`）

全库 148 文件实测命中 58 段 / 7 部。**默认只出报告，`--apply` 才动数据。**
⚠️ 改 `text` 时必须同步 `words`（见 `asr_words.py`），否则 cue 管线读不到。

### asr_words.py —— `text` 与词级时间戳同步（2026-10-28 R154，**头号 bug 的修复**）

`cues.flatten_for_cues()` 优先用 `seg["words"]`，**根本不看 `seg["text"]`**。
而 01 的拉丁清理、02 的 errata 历史上都只改 `text` → **默认路线下整张错字表等于没生效**
（`性液→精液` 改对了，成品仍是「性液」；`直近でショット` 判了幻觉，译文照旧）。

- `patch_words(seg, ja, fix)`：在词级时间戳上做与 `text.replace` 等价的替换（支持跨词、多处，保住 s/e）
- `clear_words(seg)`：对不齐的直接丢掉，让下游用 text 合成伪词
- `consistent(seg)`：判 text 与 words 是否一致（`flatten_for_cues` 拿它当安全网）

### export_review.py —— 逐行审校表（2026-10-28 R154，给外部模型判）

```bash
python export_review.py --run <run_id> [--only-flagged] [--format txt|md|tsv] [--split]
```

输出六列：`行号 | 时间 | ASR 原文 | 02修正后 | 最终中文 | 标记`。
标记全部可追溯：01 的 `halluc_latin`、02 的 `fix_log`、03/04 的判据现算。

| 标记 | 含义 |
|---|---|
| `latin:drop` / `latin:strip` | 01 层拉丁幻觉：整段丢弃 / 只摘拉丁词 |
| `errata` | 命中错字表 |
| `masked` | 中文是「…」但日文有实字 → 可能误兜底 |
| `short` / `long` | 译文 < 日文 40% / > 200%（日文 ≥10 字才判，短句豁免） |
| `cont` | 承接上一行（一句 ASR 被拆成多行字幕） |
| `dup_cont` | 承接行中文与上一行完全相同 → 显示层没切开 |

⚠️ **格式必须用 `--format txt`**：tsv 的制表符贴进外部模型会被吞掉/当缩进，整表错列。
`--split` 按轨拆文件（每轨一两百行，一次能读完）。

### fill_timeline.py —— 补「整行消失」的时间轴占位（2026-10-29 R155）

一个 cue 里塞两句时，Sakura 只翻前半句、后半句直接丢
（`なんでこんな感じなの` + `フフフフ ママゴジがガギャギャ…` → 只有「为什么会变成这样啊？」）。
写盘 `if not zh: continue` 又不写空行 → **那一秒完全空白**，`empty_check` 直接挂。

```bash
python fill_timeline.py --run <run_id> [--apply]
```

⚠️ 判据必须用**重建 cue 的起点**（`cues.build_semantic_cues(flatten_for_cues(...))`），
不能用 ASR 段起点——05 的行是「显示行」，起点是 cue 起点/中文切点，用段起点会报 96~466 行假缺失。
且必须 `KIKOERU_CUE_PIPELINE=1` 在 import cues **之前**设（`cues.ENABLED` 只读一次）。

### dedup_lines.py —— 删掉「和上一行一字不差」的重复行（2026-10-29 R155）

`render_display_lines` 偶尔把同一句中文贴到两个**不同 cue** 上（全库 193 处，RJ377265 一部 99 处）。
跨 cue 在 `polish.py` 里删不了：那个 cue 会一行不剩，触发 `keep < len(lines)` 硬门禁。

```bash
python dedup_lines.py --run <run_id> [--apply]
```

删行是安全的：LRC 的显示时长 = 到下一行时间戳为止，删掉第二行，第一行自动延长。
⚠️ **纯「…」不能删**——那是「有声音但没译出」的占位，删了 `empty_check` 立刻报「空字幕」。

### check.py —— 成品校验（2026-10-23 R23 起区分「清屏 spacer」与「真空行」）
```bash
python check.py <字幕目录> [--style DIR]
```
检查时间轴递增、空行、日文残留、编号残留、繁体残留（都应 0），并对比人工风格指标。

⚠️ **它抓不到「整句没译出来」**：空文本行只要夹在两条非空行之间，就会被当成合法**清屏 spacer**
（R142 的 19 行空字幕就是这么漏的）。这一类归 `empty_check.py`。

### empty_check.py —— 空字幕检查（2026-10-27 R144，管线硬门禁 `empty`）
```bash
python empty_check.py --lrc <成品目录> --asr <ASR 目录> [--json out.json] [--tol 0.35] [--max-miss 0]
python empty_check.py --selftest        # 4 例自检（正常→0 / 挖空→1 / 删行→1 / 偏移→0）
```
拿 ASR **确定性重建「应有的行」**（复用 `translate.build_lines`，与翻译/润色同一口径），
要求每行起点在成品里有非空文本。这是唯一能抓「整句没译出来」的检查。

三个必须知道的坑：
1. **`KIKOERU_CUE_PIPELINE` 必须为 1**（脚本在 import 前 `setdefault`）。否则退成显示行模式，
   RJ9000002 会重建出 489 行而不是 547 行 → 73 条假缺失。
2. **ASR 版本优先级** `asr_fix > asr > 其他 > asr_raw`（`asr_raw` 没归一化，行数对不上）。
3. **漂移守卫**：单轨缺失率 >3% 且 ≥5 行 → 判 `drift`，只报告不计失败。
   真缺陷是零星几行（全库 19/22800 ≈ 0.08%），版本漂移是整轨成片对不上（存量 5%–51%）。

管线里 `hard` 按 run 动态：`profile == "adopted"` 只作参考项（导入的历史成品本来就不同版）。

### audit.py —— 质量审计（定位系统性问题）
```bash
python audit.py <字幕目录> [--n 30]
```
把可疑行分桶：英文残留/片假名/日文引号/翻译腔五病/超长行/无限循环/无汉字。
**比逐句看快得多，定位系统性问题先跑它。**

### clean_hallu.py —— 清洗 ASR 套话幻觉
```bash
python clean_hallu.py <ASR目录> [--dry]
```
清洗 Whisper 编造的套话（「ご視聴ありがとうございました」等）。

### rescan_head.py —— 补扫开头（整轨丢首窗时用）
```bash
python rescan_head.py <音频目录> <ASR目录> [文件名关键字]
```
整轨识别会把「低能量慢起」的开头 30 秒**整窗丢弃**（faster-whisper 滑窗 skip，详见
`SKILL.md` 已知坑 40）。本脚本对每轨前 60 秒用**短窗**（15s / 重叠 5s）+ **两相位**重扫，
再用 core 抽取保证无重复，把恢复的段插回原 JSON 前面。
- 幂等：json 里打了 `head_rescanned` 标记，重跑会跳过。
- 音频目录支持异构布局（`本編(mp3)` / `02 FLAC` / `01 本編` / `mp3`），会自动递归找同名音频。
- 内置 `asr.py` 的幻觉过滤（`is_hallucination`），不会把「次の動画で…」补进来。
- ⚠️ 内部解码必须用 `faster_whisper.audio.decode_audio()`；用 `av.to_ndarray()` 会因
  FLAC/24bit 的原生采样尺度被 clip 成方波，导致「一条都恢复不出来」。
- ⚠️ **段尾必须裁到 core 边界**（2026-10-13 修复，见 `SKILL.md` 已知坑 42）：core 抽取原来只
  检查段**起点**是否落在 core 内，`end` 原样写盘，于是 Whisper 把整窗吐成一段（如 `[15.0-29.4]`）
  时会**吞并后续窗的细粒度段** → 成品重复行。现在 ① 用词级时间戳在 `core_end` 处截断
  （**优先切在句末标点**，避免留半截词）；② `_dedup` 末尾追加 `_drop_contained()` 删冗余微段。
  已产出的旧数据用 `tmp/daily/fix_rescan_overflow.py <draft根> [--dry]` 回补（不重跑 Whisper）。
- 跑完**必须重跑 translate + polish**（行数变了）。

### collapse_rep.py —— 折叠 ASR 重复循环幻觉
```bash
python collapse_rep.py <ASR目录> [--dry]
```
把 ASR 在长段无语音处的**字符级重复循环**（`ぎゅー`×30、`あ`×100+`う`×127）折成 3–4 个。
规则与 `asr.py` 内置的 `collapse_repetition()` 是**同一份**（importlib 复用，别另写一份）。
- 保守：要求时长 ≥10s 且重复次数够多才动手，**2–7 秒的真实连呼（`好き好き好き`）不动**。
- `--dry` 只看不改。跑完**必须重跑 translate + polish**。

### build_regset.py —— 构建分层回归集（评测的「尺子」）
```bash
python build_regset.py [--out DIR] [--total 300] [--per-stratum 40]
```
从成品 lrc 里抽**高风险 cue**（首分钟/过短/过长/否定/疑问/拟声/近音混淆/已修位置/时间重叠），
按**作品隔离**划分 dev / 冻结回归 / 最终留出三份，产出 `candidates.jsonl` + `review.csv` + `dev.csv`
+ `holdout.csv` + `STATS.md`。标注协议见 `regset/PROTOCOL.md`。
⚠️ **必须按作品分，不能按句随机**——同作品的角色名/术语/近似台词会泄漏，指标虚高。

### score_regset.py —— 回归集打分
```bash
python score_regset.py [--review review.csv] [--compare B.csv] [--out report.md]
```
读人工标注算 **ASR / 忠实度 / 连贯性 / 风格** 四层通过率（只按 `Y+N` 计），
并自动扫全部 lrc 算**交付层**（时间重叠、空 cue、日文残留、后缀重复）——交付层不依赖标注，随时可跑。

### scan_violations.py —— 确定性「源语必要条件 + 跨行泄漏」扫描
```bash
python scan_violations.py [--out DIR]
```
不做「通用自动真值」，只做**可审计的必要条件检查**（漏一条 ≠ 翻错；**违反一条则高度可疑**）。
规则：`neg_missing`（日文有否定中文没否定）、`question_missing`、`number_mismatch`、`confusable`（近音混淆）、
`dup_suffix` / `len_imbalance` / `dup_ngram`（跨行泄漏），以及 2026-10-14 新增的三条：
- `omit_suspect` —— **漏译**：日文内容 ≥12 字但中文 <30%（排除片假名主导、重复串、重复短语）。
- `onomat_translit` —— **拟声被音译**：日文含 2–8 字的连续片假名 run，且中文里**没有**任何拟声/动作字、
  但有 **≥2 个相邻**的音译高危字（如「基欧」「卡雅玛雅」）。本轨高频 run（角色名）与常见外来语黑名单豁免。
- `cue_too_long` —— **cue 过大（挂静音）**：单行停留 ≥5.5s 且行内 ≥2 个句末标点，
  **且停留超过「阅读 cap + 该行语音剩余」8s 以上**（2026-10-23 R23 收窄，坑 106）。
  旧口径 410 条里 294 条是段间静音 gap 假象（cue 自身 <5.5s）；幻觉段只按阅读时间算。
- `moan_drift` —— **气声被上下文带跑**：日文是纯气声串（`お…あ…あ…足…`），
  而中文里出现 ≥2 个非语气词内容字（实测被前文「妈妈」带成「妈…妈…妈…脚…」）。
  气声串判据 = ①「…」≥2 ② 按「…」切出的段 ≥2 ③ **每段 ≤1 字**——
  ⚠️ 第③条别省：`（原句略）`/`（原句略）` 含真词，不是气声。
- `punct_only`（2026-10-19）—— **整行只剩标点**：中文侧无实义字符 **且** 日文侧有实义字符
  （日文本身是 `…` 时不判，那是忠实翻译兼清屏行）。与 `polish.bad_cell` 的同名判据一致。
  ⚠️ 2026-10-23 R33 补两条一直没实现的判据：① **纯气声/拟声短串豁免**
  （`（原句略）` 译「…」是忠实翻译，`_is_pure_sound`，两边同步）；
  ② **段组判据**——显示拆分的首行「…」不是丢句（cue `……、うぅ、見ないで' 拆 3 行实录）。
  ⚠️ 2026-10-23 R25：`asr_suspect` 命中带「幻觉确认·A/B」标签（halluc_hit 分层，
  194 = 179 真台词 + 15 幻觉确认，全部已处置）；空文本 spacer 行参与停留计算但跳过内容规则。
  ⚠️ 2026-10-22 起：ja 段是 `asr_halluc` 判定的幻觉（`（原句略）` 独占 18–25s 一类）时
  **豁免**——那时「…」是 pipeline 的正确处理（幻觉段→清屏行），不是丢句（修后 14→9 条）。
- ⚠️ **ja 配对按轨自动选 ASR 版本**（2026-10-22，坑 90）：成品 LRC 与 `draft/asr`（v2 重跑版）
  **不是一一对应**——16 部成品 12 部时间戳对齐 v1（`asr_v1_20261020`）、4 部对齐 v2，
  RJ9000005 还是逐轨混合。`pick_asr()` 按「LRC 时间戳 vs 词级起始」匹配率逐轨选版本
  （v2 需领先 0.02，平分保留 v1），每条命中记 `asr_ver`。**别再把 ASR 来源写死成 `asr/`**
  （fixline.find_asr 同款故障当天已修）。修复前后对比：总量 2441→2301、
  `listener_marker_unrendered` 10→4、`omit_suspect` 292→193。
- ⚠️ **blocker 级规则的已知误报模式**（2026-10-22 复核 45 条后定）：拟声词真重复
  （しこしこ/ちゅこちゅぱ/好き×5）、呼语重复（`先生好き`/`（原句略）`）、
  `もう→不行·已经够了·真是的`、`と→然后`、`う→呼`、`口→嘴巴`——`ZH_INTERJ` 语气词白名单
  有缺口。**真缺陷的标准**：某行中文含另一条日文话语的译文（跳译/跨行泄漏/ASR 回声）或误译。
产物 `violations.jsonl` + `VIOLATIONS.md`。
- ⚠️ 否定正则**别加 `（原句略）`**——`してしまい`/`はずです`/`いぬ` 会刷出上千条假阳性。
- 残留假阳性主要是**い形容词词尾 `ない`**（切ない/少ない），复核时按「规则误报」处理。
- ⚠️ `number_mismatch` 大量是**扫描器噪声**：中文用汉字数字（「一个月」「三次」「十秒倒计时 九 六」）时对不上阿拉伯数字。

### test_regressions.py —— 硬回归门禁
```bash
python test_regressions.py [-v]
```
0. cue 构建器（确定性/词数守恒/时间单调不重叠/**无「该切未切」巨 cue**）；
① 检测器必须命中 **8 个已确认的跨行泄漏**；② 这 8 处**不得在成品重现**；
③ 显示切分无损性；③b **新 3 类问题检测器**必须命中已确认样本（漏译/拟声音译/cue 过大）；④ 全量告警数（信息项）。
改「翻译单位」后必须跑它——这是防「改 A 坏 B」的闸门。`leak_rules()` 在 `scan_violations.py` 里，两边共用一份口径。

### cues.py —— 语义 cue 层（`KIKOERU_CUE_PIPELINE=1` 时启用）
```bash
KIKOERU_CUE_PIPELINE=1 python translate.py <asr> <draft> [key]
KIKOERU_CUE_PIPELINE=1 python polish.py <asr> <draft> <out> [key]
```
把**翻译单位**从「显示行」提升为「语义 cue」，显示行只在润色后由中文切出 —— 这是消除
**跨行翻译泄漏**的关键（旧链路：`build_lines` 切出显示行 → 逐行翻译 → 模型在 L1 提前补全、
把 L2 的内容也翻进去）。
- `build_semantic_cues(words)`：词流 → cue。边界优先级 = 说话人变化/停顿 ≥0.75s → **段边界**
  （当前 cue 已以句末标点收尾，或 ≥16 字 / ≥3s）→ 句末标点
  （**且 ≥16 日文字；碎片串豁免**）→ 停顿 ≥0.30s（超限时才用）→ 超 10s/70 字退到最近候选点 →
  **兜底保留整段**（标 `max_guard_unsplittable`）。
  优先切**段边界**，**绝不按助词/接续词猜**（日文词 token 是子词级的，必然误伤）。
  ⚠️ 句末标点**必须配字数下限**（`MIN_CUE_JA_CHARS=16`，与旧 `build_lines` 的 `MIN_LINE` 对齐）：
  否则「うん。」这类短句会各自成 cue，显示行跟着变短（实测中位掉到 8.0，旧管线 9.0、人工 13.7）。
- `flatten_for_cues(segments)`：cue 管线的词流入口。**没有词级时间戳的段**（全库 0.5%）用
  `synth_pseudo_words()` 按句末标点切成**伪词**、时间按字数分摊 —— 否则整段变成**一个不可切 token**，
  多句被并成巨 cue，模型压缩漏译（实测漏掉「お疲れ様」）。
  ⚠️ 碎片串判定（`_is_fragment_run`）**只能用「…」密度**：ASR 词 token 本来就是子词级，
  用「平均 token 长度」判会全命中。
  ⚠️ 该碎片规则**默认关闭**（`FRAGMENT_ELLIPSIS_MIN=99`）：消融实测伪词已足够，
  再开碎片规则只会把中位日文字从 14 压到 12。
- 段边界 flush：`SEG_FLUSH_ENABLED`（默认开）。cue 已以句末标点收尾 **或** 字数 ≥ `SEG_FLUSH_CHARS`(16) 即切。
  ⚠️ **不要加时长条件**（实测纯负作用）；代价是 cue 数 +9%（显示行中位 9.0→8.0）。
- `render_display_lines(cue, zh)`：成稿中文 → 显示行。**只切不重写**（拼接回去必须等于成稿）。
  切点只允许在 `。！？…；、，` 或空格后；禁止切在「的地得了着过」旁、数字与单位之间、
  否定词与谓词之间、括号/引号/术语内部，且**任一侧不得少于 4 个可读汉字**。
- 时间按可读字符数比例分配，每行 ≥0.9s。
- **`MAX_HOLD_SEC=5.5`**（2026-10-16 用户盲听裁决定值，`KIKOERU_MAX_HOLD_SEC` 可覆盖；
  旧默认 8.0）——单行停留上限，配合 `cap_holds()` 的自适应缩短（读完即走）。
  `scan_violations.py` 的 `LINE_LONG_SEC` 同步 5.5。副作用扫描见根 README 6.3.12。
- ⚠️ 开关**默认关**，仍走旧 `build_lines`（`legacy` 路径）——可随时逐轨回退。

> **翻译引擎切换**：`KIKOERU_ENGINE=hymt` 走 **HY-MT 1.5**（腾讯混元，翻译专用，逐句翻，
> prompt 必须明确「把下面的文本翻译成中文」）→ 只做**初译**，风格交给 `polish.py`（Qwen + 风格库）。
> 对应 `serve.py start --hymt`。⚠️ 8GB 卡**装不下两个 7B**，HY-MT 初译与 Qwen 润色必须**分两阶段**跑。

### rerun_segment.py —— 片段级 ASR 重跑（2026-10-20）
```bash
python rerun_segment.py --rj RJ9000001 --track "00.xxx.mp3" --list 120 150     # 看该区间现有段
python rerun_segment.py ... --start 120 --end 150                              # 干跑：旧段 vs 新段
python rerun_segment.py ... --start 120 --end 150 --apply                      # 落盘
```
- 只重跑 `[start,end]`（两侧各留 `--pad 3` 秒上下文），不整轨重跑。
- **默认按旧段边界重分配文本**：带 padding 重跑常把多段并成一段，直接用会改变成品行结构；
  用新段的**词级时间**把文字分回旧段区间 → 成品行结构不变。`--no-keep-bounds` 可关。
- `--apply` 会：备份到 `draft/<rj>/asr_bak/`、替换 asr json、写 `asr_rec/`（供
  translate → polish → `recover_splice.py splice` 补回成品）。
- ⚠️ 台本打轴产物（`script_aligned`）**拒绝执行**——重跑会丢台本文本，应重跑 `script_align.py`。

### fixline.py —— 单行重译闭环（2026-10-20，DEFECTS#12）
```bash
python fixline.py --rj RJ9000002 --track "05.xxx.mp3" --list 460 480     # 定位：看该区间成品行
python fixline.py ... --start 469.6                                       # 干跑：重译该行（带前 3 行上下文）
python fixline.py ... --start 469.6 --ja "どうでもいいんじゃないから"      # ASR 不准时给正确日文
python fixline.py ... --start 469.6 --zh "怎么样都无所谓。"                # 已知正确译文直接写
python fixline.py ... --start 469.6 --apply                               # 落盘（自动备份）
```
- **只改文本不动时间戳**；走正式管线同一套函数（`translate_block` + `polish_block`，
  A8 遮蔽/术语策略/实体账本/归属 hint 全部继承），不是另写一套 prompt。
- 落盘前必过：无日文残留 / 非空 / 非拒答 / 非纯标点 / `bad_cell`；备份在
  `data/tmp/lrc_backup_fixline/<rj>/<文件>.<时间戳>.lrc`。
- 新译文过长 → 就地拆成多行（时间按字数分配；窗口 <0.8s 不拆，宁可贵着）。
- `--lrc/--asr` 可直连文件路径（在副本上验证写盘路径而不动成品）。
- ⚠️ **ASR 版本自适应**（2026-10-22，坑 90）：`find_asr` 按 LRC 时间戳匹配率在
  `asr_v1_20261020`/`asr` 间选版本（平分留 v1）——成品 LRC 与 v2 重跑版混存，
  固定读 `asr/` 会在 v1 作品上取不到段（「该行取不到源日文」报错时用 `--ja` 兜底）。
- ⚠️ 「非纯标点」校验会挡掉**清屏行**：ja 窗口=纯停顿（`……、`）时「…」是正确译文
  （项目 punct_only 约定），这种场景需绕过校验直写并自行备份。
- ⚠️ 需要 llama-server（`--zh` 模式不需要）。
- **`--allow-no-ja`（2026-10-26 R135）**：该行 ASR 缺段（时间窗落在 ASR 空洞里）时，
  默认会直接 `sys.exit`（防止盲改）。加这个开关才允许 `--zh` 落盘，日文留空、
  E1 锁只能按时间戳认。用于「覆盖层记了改动但成品没落盘」这类补救
  （实例：RJ9000013 05:01「气筒」，见 SKILL 坑 153）。

### postprocess.py —— 插行/改行后处理规范化（2026-10-20，DEFECTS#13）
```bash
python postprocess.py <lrc文件或目录>            # 干跑：报告长行/幻觉/时间轴问题
python postprocess.py <lrc文件或目录> --apply    # 写盘（先备份 .bak_prepost）
```
- **任何按时间戳插行的工具落盘前必经**：`recover_splice.splice` 已接入、`fixline` 目标行拆分复用它。
  根治两个已踩坑：① splice 绕过 cue 层 → 幻觉词原样插入 / 长句一直挂到下一行（实测 13.6s）。
- 三件事：排幻觉词（ja 命中 `HALLUC_WORDS` 且挂载 ≥8s → 剔除；短晚安不误删）、
  长行拆分（挂 >6s 且 >14 字 → 按标点拆、时间按字数比例分配、迭代到不超标）、
  时间轴单调/重叠检查（只报告）。
- `normalize(rows)` 可编程调用：rows = `[{"t", "zh", "ja", "raw"}]`（ja 可空，空则跳过幻觉判据）。
- 纯标点行（清屏行）绝不拆——删了上一行要挂 20–42 秒。

### nonlex_score.py —— 非词汇发声打分器（2026-10-21，C3 弱监督基线）
```bash
python nonlex_score.py --rj RJ9000016                 # 该作品的试听清单
python nonlex_score.py --all --only-unknown          # 全库只看 A8 表外（C3 目标集）
python nonlex_score.py --rj RJ9000016 --thr 0.9       # 只列 ≥0.9
```
- whisper large-v3 encoder（均值池化）+ 2 层 MLP，用 A8 规则标签做**弱监督**；
  跨作品 **AUC 0.918**（通用 NonverbalVocalization 数据只有 0.543）。
- **只产清单、不做决策**：高阈值端仍有 ~1.7% 误报（breathy 台词被判气声），
  清单头部约 8/10 正确 → 用途是「先听哪几条」，不是自动遮蔽。
- 输出 `data/tmp/nonlex_review/<rj>.md`（按分数排序，含 ja/时间/时长/A8 组）+ 同名 json。
- 需要 GPU + 本地 large-v3；权重 `data/models/nonlex_detector.json`。
- 训练：`tmp/daily/train_domain_detector.py --feat whisper --model mlp`（按作品切分）。

### review.py —— 句级审阅页 + 锁定（2026-10-21，E1）
```bash
python review.py --rj RJ9000014                    # 生成审阅页（表格：时间/日文/中文/可疑标记）
python review.py --rj RJ9000014 --apply patch.json # 写回改动 + 把锁定行记入 locks
```
- 生成 `data/tmp/review/<RJ>.html`：可就地改译文、勾「锁定」、只看可疑/已锁，
  导出 `patch.json`。可疑标记：空行/纯标点/日文残留/长行/重复上行/未对齐/人称可疑。
- **锁定 = 人工修正不被批量重跑冲掉**：`--apply` 写 `data/locks/<RJ>.json`
  （key = `文件.lrc|时间戳`，值 = `{zh, old}`）。**polish 写 LRC 前按锁覆写**
  （key 命中；ts 漂移时按 `old` 原文认领）。`KIKOERU_NO_LOCKS=1` 可关。
- 与 `fixline.py` 分工：fixline 改一处错译（带上下文重译、只改文本不动轴）；
  review 管「批量审 + 锁定保护」。

### polish.py 的 E2 cue 级缓存（2026-10-21）

`polish.py` 默认按**签名**跳过未变的 cue（`data/cache/polish_sig/<作品>.json`）：

- 签名 = 轨名 + ja + 起止时间 + 该条初译 + 模型 + `CHUNK`/`MASK`/`review`/`system_mode`
  + **system prompt 哈希**——任一项变化自动失效，不会用到旧译文。
- 冷跑全量；热跑命中即跳过 LLM。实测：290 cue 冷跑 ~3 分钟 → 热跑 **290/290 命中、
  润色 0.0 秒、成品逐字节一致**；只改 1 行初译 → **289/290 命中、4.7 秒**。
- `--force` 全量重跑（缓存仍按新结果更新）；`KIKOERU_NO_CACHE=1` 关缓存。
- 与其它机制正交：**locks** 在写盘时覆写（锁行永远逐字保留）、**term_lock/term_repair**
  在写盘后跑、`--cleanup-only` 不过 LLM 不走缓存。
- 改了 prompt 模板/CHUNK/检测器等通用机制后，建议带 `--force` 跑一次全量再信任缓存。

---

## 补齐文档的脚本（2026-10-26 整理，之前漏写）

### errata_audit.py —— errata 政策自检（2026-10-26 T7，可复跑的审计 + 门禁）

原来是一次性脚本 `tmp/daily/errata_promote_check.py`，现在固化成工具。

```bash
python errata_audit.py                     # 37 条逐条 verdict（AUTO_OK/AUTO_UNSAFE/GUARD_THIN/REVIEW/CONSUMED）
python errata_audit.py --guard-samples 悪夢 20   # 逐段展示原文 + 「ctx 是否命中」
python errata_audit.py --test              # 门禁：44/44 断言（test_regressions 的 3n 段调它）
                                           # 语料不全时降级为「结构断言 7/7 + SKIP 语料断言」，exit 0
python errata_audit.py --json              # 每条一行 dict
```

- **verdict 口径**：`hits==0` → `CONSUMED`（语料里已被修正消化，既不算安全也不算不安全）；
  带 `legit==0` 的条目豁免「错形是高频词」检查；`guard` 但 ctx 只覆盖 1 段 → `GUARD_THIN`。
- 全量 175,948 段约 **2.3 秒**，零模型。

### repair_overlay.py —— 修复覆盖层（2026-10-26 T8，时间轴不变的人工修复记录）

**没有台本时，「新 ASR 更像话」不是证据**。重跑/推断的结果只能当候选进覆盖层，
听辨确认后才算数。协议见 `docs/errata/REPAIR_OVERLAY_20261026.md`。

```bash
python repair_overlay.py --pending                 # 列出所有待听辨条目
python repair_overlay.py --check                   # 校验时间轴 + confirmed 必须有 E1 锁
python repair_overlay.py --add plan.json --confidence low --status pending_listen
                                                   # 从 fix 计划批量登记（plan 与 fix_lines_batch 同格式）
python repair_overlay.py --fill-original           # 补 original_zh 空值（从 fixline 备份取最早一份）
python repair_overlay.py --confirm t09-0312        # 听辨通过 → confirmed（可选 --zh 覆盖译文）
python repair_overlay.py --reject  t09-0312        # 原译更好 → 经 fixline 回滚 original_zh
```

- 数据在 `data/repair_overlay/<RJ>.json`，每条含
  `original_ja / proposed_ja / original_zh / proposed_zh / audio_range / evidence / confidence / status`。
- ⚠️ `pending_listen` = **已改进成品但没听**，写文档时不许说成「已核实」。
- `--fill-original`（2026-10-26 新增）：`--add` 只认 plan 里的 `original_zh`，漏带就没法 `--reject`
  （R132 有 3 条这样）。该开关从 `data/tmp/lrc_backup_fixline/` 取**最早**一份回填，
  只补空值、来源记进 `original_zh_src`；若该行此前已被别的批次改过，「最早」不等于「紧邻上一版」。
- 听辨材料用 `tmp/daily/make_pending_listen_pack.py` 生成（切音频窗 + 日文/新旧译对照 + index.html），
  产物在 `listen_pending_1026/`。
- `production_zh`（2026-10-26 R135 结案时新增）：确认那一刻**盘上的文本**。`proposed_zh` 会随后续
  批次微调/拆行而过时，别拿它当成品文本引用；两者不一致时 `note` 会写明原因。

### rebase_overlay_by_text.py —— 按**文本**重绑覆盖层时间轴（2026-10-31）

`repair_overlay` 的主键是精确 `t0`（`abs(t-t0) < 0.02`）。流水线一动切分
（`SEG_FLUSH_CHARS` 16→10、VAD、重采样），cue 边界就漂 0.3~1.6 秒，
**所有按时间戳强绑定的条目会瞬间脱靶**——实测 78 条里 54 条失效，且 `--check` 每部 run
都扫全库，一次全库重跑刷出 864 条警告。

这个脚本把寻址方式换成语义锚定：用条目里记录的日文（`proposed_ja`）在当前 ASR 里重新定位。

```bash
python rebase_overlay_by_text.py                  # 只出 diff（默认，不写盘）
python rebase_overlay_by_text.py --rj RJ1352698   # 只跑一个作品
python rebase_overlay_by_text.py --apply          # 改成品 + 重写 t0 + 重打 E1 锁
```

两级寻址：同轨 + 时间窗口（默认 6s）→ 打分 `0.7*sim + 0.3*(1-漂移/窗口)`。

- `sim = max(lcs_ratio, jaccard_2gram)`。**不能用整体相似度**：条目里的 ja 是人工修正过的
  完整句，ASR 是碎片（一句被切成多段），`SequenceMatcher.ratio ≥ 0.85` 只有 12/78；
  最长公共子串能到 22/78。而 lcs 只找连续公共子串，`さくらの胸が` vs `桜の胸が`
  一换写法就断（实测 0.32），所以再加 2-gram Jaccard 兜住。
- 另一条更硬的判据：**2.5 秒内候选唯一** → 直接 auto。漂 0.4s 的条目文本 sim 可能只有 0.4。
- 反过来，文本路径要限 `|漂移| <= 2.0`：短句（`大きくなってる`）一轨出现多次，
  6 秒窗口里 sim 会虚高到 1.00（实测 `t00-0961` 漂 3.06s 就是这样，已被这条拦下）。
- 输出 `tmp/overlay_migration_diff.json`：`already_ok` / `auto` / `manual` 三态，
  每条带 `anchor_ja` / `matched_ja` / `lrc_now` / `proposed_zh`，供人工核对后再 `--apply`。
- ⚠️ `--apply` 会**改成品 LRC**（走 `fixline`，自带备份），只处理 `auto`。

### term_family.py —— 术语族盘点（2026-10-25 T3）

一次跑出「一个概念的 ja 写法分布 + zh 译法分布 + 作品分布」，频次同时报**原始/去重后**。

```bash
python term_family.py --ja 我慢汁,我慢じる,ガマンチレ --json out.json
python term_family.py --zh 肉棒,鸡鸡 --json out.json
```

去重复用 `dedup_corpus.dedup_pairs()`——语料 69% 是重复（多变体轨），不分母的频次会虚高 2~4 倍。

### term_mine.py —— 自建术语库（2026-10-25 R122，无 FastAlign 的近似词对齐）

ja 词元 ↔ 中文 2–4 字 n-gram 的**条件概率 × 专指度（IDF 惩罚「的/了/我」）**，
产出候选后人工筛。不依赖外部二进制，完全可复现。

### refine_glossary.py —— 术语表重构（2026-10-18）

```bash
python refine_glossary.py --report   # 只出报告
python refine_glossary.py --apply    # 写 glossary_v3.json + 备份
python refine_glossary.py --gaps     # 用语料分词找「高频但术语表没有」的词
```

删「子串冗余」（`孫→小穴` 会命中 お孫さん）、单字汉字条目，1491 → 278 条。
⚠️ `glossary_v3.json` **不在 prompt 注入链上**（坑 89），要生效得进 `align_style.GLOSSARY_SEED`。

### mem_conformance.py —— 记忆遵守率报表（2026-10-25 T5-A）

纯读文件算「轨级记忆写了 `ja→zh`，成品里到底有没有 `zh`」，并区分 `memory_only`
（术语表没覆盖，才算记忆的净信号）。**A/B 差异被采样噪声淹没（同配置两遍 33% 行不同），
所以不用模型打分。**

```bash
python mem_conformance.py --out-dir ../../../kikoerusubtitle --json out.json
```

### probe_fictive.py —— 虚构术语探针（2026-10-25 T5-B，四臂对照）

把真实 cue 里的名词换成生造词「ラネク」，四臂只改记忆内容
（correct / counter / irrelevant / none），看探针句是否吐出对应中文。
**这是「模型真的在读记忆」的唯一因果证据**，mem_conformance 只能给相关性。

```bash
python probe_fictive.py --dry-run
python probe_fictive.py --run --out ../../../regset/r115/probe.json
python probe_fictive.py --judge --out ../../../regset/r115/probe.json
```

### trunc_audit.py —— 成品截断审计（2026-10-25，GPT 第七轮 Q4）

只判**机械完整性**，零模型：R1 行尾悬空虚词、R2 引号/括号未闭合、R3 行内 ≥12 字无句末标点。
⚠️ **不判「长度比」**——那是语义判断。

```bash
python trunc_audit.py --dir ../../../kikoerusubtitle --md ../../../docs/terms/TRUNC_AUDIT_<日期>.md
```

（`--md` 目标文件首次运行会新建；默认值已指向 `docs/terms/`。）

### risk_tier.py —— 风险分层（GPT 第五轮 Q1-e）

把 cue 分成 term/neg/long/speaker/memory 等层，**同时保留随机普通样本**，
避免只在「容易改善」的子集报喜。

```bash
python risk_tier.py <asr_dir> [draft_root] [out.json]
```

### shadow_audit.py —— 影子模式（GPT 第十轮 Q4）

把「被排除的候选」全记下来（s1 撤除的 そっち / s2 被过滤的 君 修饰 / s3 否定边界 /
s4 低置信 pragmatics / s5 同形异义），**只写日志、不进任何门禁**——
用来回答「留下来的告警准」之外的另一半：被排除的有没有问题。

```bash
python shadow_audit.py --selftest
python shadow_audit.py --out <dir> <扫描产物目录>
```

### slot_guard.py —— 语义槽守卫（2026-10-25 R113，ChatGPT 第四轮 Q4）

后验比对「初译 vs 润色稿」的五个可自动判的槽（肯否/主体/动作/因果/术语），
槽变了就**回退初译或标黄**。不改进模型，只做确定性校验。

```bash
python slot_guard.py <asr_dir> <draft_dir> <polished_dir> --report out.json
```

### pragmatics_hints.py —— 参与者/话语行为账本（GPT 第九轮 Q1 方案 B）

cue 级表层标记扫描，产出可审计的 hint（标签 + 证据 + 置信度），**不做语义理解**。
只有 high 置信进 prompt 与门禁；`君` 前接汉字/片假名（チン君/ドM君）、
`お前` 作所有格方位（僕のお前）、`ってば` 作呼びかけ —— 都不算人称信号。

```bash
python pragmatics_hints.py     # 跑内置 selftest
```

### dedup_corpus.py —— 语料去重（2026-10-25 T2）

`parallel.json` 约 75% 的 pair 与同作品邻近 pair 时间重叠 >50%（多变体轨重复进语料）。
同作品内按 (s,e) 排序贪心保留。

```bash
python dedup_corpus.py --stats
python dedup_corpus.py --write --term 我慢汁
```

**任何占比/频次类结论都不能拿 88077 当分母**（去重后 26885）。
## 其他工具（未单列）

| 脚本 | 作用 |
|---|---|
| `semantic_coverage.py` | **语义覆盖门禁**：检查日文里的具体物（名词/数量）在译文里是否被泛化掉（`coverage.json` 14 条规则）。`--selftest` 9/9。 |
| `asr_halluc.py` | 早期幻觉清洗（套话/重复循环），已被 `asr_latin_halluc.py` + `collapse_rep.py` 取代，保留做对照。 |
| `fragment_join.py` | 碎片行合并的独立工具（管线内已由 cue 层处理）。 |
| `lock_match.py` / `lock_migrate.py` / `lock_coverage.py` | **E1 句级锁的三件套**（见下方专节）。`test_lock_apply.py` 是边界测试（12/12）。 |
| `lexical_audit.py` | **词汇层审计**：实词脱落 / 音译 / 2 字母拉丁（见下方专节）。`--selftest` 21/21。 |
| `rootloc.py` / `styleloc.py` | **路径解析**：`rootloc` 找仓库根（work 的上一级），`styleloc` 找知识表目录（`KIKOERU_STYLE` → `data/style` → `presets`）。⚠️ 独立脚本**必须**用它们，别自己拼路径（`os.path.join(ROOT, "kikoerusubtitle")` 是 work 里的空目录）。 |
| `test_punct_norm.py` | 标点归一的边界测试。 |

## 词汇层审计：找「看起来是译文、其实是垃圾」的行（2026-10-31，P2）

`check` 查日文残留/标点，`empty` 查空行，`trunc` 查行尾悬空，`semantic` 查具体物被泛化 ——
这四道**全放过**的一类缺陷是**词汇层塌陷**：

```
だーいすきー   → 「哒——！」   整行只剩语气，实词全丢
ハイボク       → 「嗨波克」   音译，中文读者看不懂
DS             → 「DS」       2 字母拉丁残留（check 只抓 ≥3 字母）
```

它们不是漏译（中文里有字）、不是空行、不是日文残留，只有拿
「**日文侧有没有实词** × **中文侧是不是全虚词**」这个交叉判据才抓得到。

| 检测器 | 判据 | 处置 |
|---|---|---|
| `content_drop` | 日文有实词 且 中文分词后全是 `y/e/w/o/x`（语气/叹词/标点/拟声/非语素） | 红，人工核 |
| `ellipsis_only` | 中文整行只有「…」而日文有实词 | 黄，单独计数（那是重试阶梯的兜底，不是虚化） |
| `translit` | 中文里有个**不成词**的 2~4 字片段，其拼音与**片假名串**的罗马字整体相似 ≥0.75 | 黄，人工核 |
| `latin2` | 2 个字母的拉丁串（带词边界 + 复用 `check.LATIN_OK` 白名单） | 黄，人工核 |

**日文侧「有实词」的判定顺序**（顺序就是三次调参的结果，别调回去）：

1. 含汉字 → 有
2. **纯重复**（`ぎゅぎゅぎゅ`/`くるくるくるくる`/`ねえねえ`）→ 无（fugashi 会把它们切成名詞）
3. **短且音素单调**（`あぁーふっ`/`んふ`）→ 无（喘息）
4. **fugashi 分词**（unidic-lite）：`pos1 ∈ {名詞,動詞,形容詞,形状詞,副詞,連体詞,代名詞}`
   且 `pos2 ∉ {非自立,接尾}` → 有；**跑通了且一个实词都没有 → 定论为无**
5. 没装 fugashi 才退到「去母音后 ≥3 假名」

第 4 层是权威的：它把 `よっしょ`/`むむむ`/`ガルガルガルガルうん` 全判成 `感動詞`，
而这些译成「嘿咻」「唔唔唔」是**对的**。第一版让「≥3 假名」先说话，误报占了三分之一。

### 三条判据错过的坑（都靠全库数字打回来的）

1. **`jieba.dt.FREQ` 在 `initialize()` 之前全是 0** —— 「这个词在词表里吗」这道闸门形同虚设，
   任何候选都「不成词」。
2. **不能用「任意 2~4 字切片」**：`妈妈的` 不是词、`妈妈` 是。切片会把 `ママ`→「妈妈」
   （**本来就是正确译法**）全报成音译 —— 实测 326 条里约 250 条是这一类。
   改成「松散 token 串」里取组合，多字真词会**打断**串。
3. **不能逐显示行判**：一条 cue 的译文被按逗号切成 2 行时，第 1 行只有「啊。」，
   整段日文却都归了它 —— 看着像整句丢了，其实下一行就是后半句。
   改成**按 ASR 段聚合**（一个段拥有它时间范围内的所有显示行，中文拼起来再判）。

**存量（46090 行）**：实词脱落 **40**（约 18 真）/ 纯省略 12 / 音译 **5** / 2 字母拉丁 **0**。
审校量从 ~300 条降到 ~40 条。**它仍是审校清单，不是判决书。**

```bash
python lexical_audit.py --selftest                                   # 21 项
python lexical_audit.py --all --lrc ../kikoerusubtitle --asr-root runs
python lexical_audit.py --lrc ../kikoerusubtitle --asr runs/<run_id> --rj RJ1228193
```

06 门禁里的 `lexical` 项就是它（`hard=False`，只出报表）。

## E1 句级锁：文本锚定 + 覆盖度断言（2026-10-31，P0）

**问题**：锁是人工逐句听出来改出来的（全库 4187 条），但它是按 `文件|时间戳` 存的。
重跑一次切分，时间戳漂 0.3~1.6 秒，**锁就静默失效** —— `apply_lock` 匹配不上就返回原文，
日志只写「覆写 N 行」，miss 不报错。2026-10-31 实测 17 部新路线作品 1032 条锁
只有 547 条落地（53%），另一半是这么没的。

**三件套**：

| 脚本 | 职责 |
|---|---|
| `lock_match.py` | **匹配**（纯函数）。`assign_cues()` 把锁分给 cue：`score = 0.7*sim(锁日文, cue日文) + 0.3*(1-漂移/窗口)`，`sim` 取 `max(lcs, jaccard)`。相似度容忍汉字/假名写法差异和 errata 改字，漂移项压住「短句被长文包含」的虚高。 |
| `lock_migrate.py` | **补锚**。给没有日文的锁补 `ja`。⚠️ **只给「已对齐」的锁补**（锁文出现在成品里，或 ±8s 内某行与锁文相似度 ≥0.5），否则会把错位的锁填成「看起来完全一致」的假配对。 |
| `lock_coverage.py` | **门禁**（06 的 `locks` 项）。拿成品 LRC 反查每条锁有没有落地，分 `landed` / `missing`（有锚点却没落地，**红**）/ `no_anchor`（没有日文，**黄**，待人工）/ `unreadable`。 |

**三条硬规则**（都是踩出来的）：

1. **没有日文锚点 → 一律不猜**。曾经有「窗口内时间唯一就认它」的兜底，实测立刻出事：
   一条锁的 ts 早已漂到别的句子上，纯按时间套用把一句不相干的中文盖了上去。
   **错位比丢失更坏** —— 丢失会被覆盖率门禁报出来，错位看起来一切正常。
2. **人工裁定 > 多数票**。`term_lock.py` 是作品级多数票决策，会把锁里的
   「肉棒」按 70:4 改回「鸡鸡」。现在 `term_lock` 读锁表，**当前文本等于某条锁的行整行跳过**
   （连 `BAN_TERMS` 一起跳，出现禁用词是人的选择，该由门禁报而不是被脚本改掉）。
3. **判「落地」用子串不用整行相等**。显示层会把一句长译文按逗号拆成多行，
   整句锁文天然对不上任何单行（用整行相等判过一次，报 45.5% 丢失，全是假阳性）。

