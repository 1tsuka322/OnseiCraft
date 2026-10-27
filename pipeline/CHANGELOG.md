# 变更日志（kikoeru-pipeline）

> 版本号规则：**测试版 `vX.Y.Z-beta.N`**，正式版 `vX.Y.Z`。
> 每次发版：改 `VERSION` → 在本文件顶部加一节（**按日期**）→ `pipeline.py selftest` 全过 → 生成一次看板。
> 只记「能影响行为或追溯」的改动，不记纯文案。

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