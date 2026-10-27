# 管线（`pipeline/pipeline.py`）

## 命令

```bash
P="python pipeline/pipeline.py"

$P run     --project RJ0000001 --source /path/to/audio   # 跑一部（自动串六阶段）
$P run     --run <run_id> --from 03_translate           # 断点续跑
$P run     --run <run_id> --only 06_checks              # 只跑某阶段
$P check   --run <run_id>                               # 只跑门禁
$P status                                               # 所有 run 一览
$P promote --run <run_id>                               # 进生产（门禁不过拒绝，自动备份）
$P repair  --run <run_id> --plan repairs.json --apply    # 按计划改成品（走 fixline，留痕）
$P explain --run <run_id> --track <轨名> --start 346.5   # 这句为什么被改成这样
$P knowledge status                                     # 知识表版本 + 未登记变更告警
$P queue status                                         # GPU 串行锁
$P selftest                                             # 13 项自检，不需要数据
```

## 目录布局

```
data/runs/<run_id>/
  manifest.json          配置快照 + 各阶段状态/指纹 + 门禁结果
  01_asr/<project>/asr/  日文分段 json（含词级时间戳）
  02_asr_fix/            修正后的 ASR + 改错统计
  03_translate/draft/    初译 LRC（不可变）
  04_polish/             润色结果（不可变）
  05_term_lock/          **最终产物**（唯一被 promote 的目录）
  06_checks/             gates.json + 每个门禁的日志 + 扫描报告
```

`04_polish` 与 `05_term_lock` 必须是不同目录——术语锁每次从 04 重建，
放一起会让上一轮残留混进来。

## 状态机

```
created → running → ok
                 ├→ gate_failed   硬门禁不过（内容缺陷）
                 ├→ failed        程序异常
                 ├→ interrupted   被杀/断电
                 └→ incomplete    计划内的阶段没跑完
```

「只跑了 06_checks 的 run」不会被判 ok：计划阶段在创建时固化进 manifest。

## 指纹与续跑

阶段指纹 = `参数 + 输入哈希 + 相关知识表哈希 + 代码哈希`。
指纹没变 → 跳过；变了 → 重跑，且下游因为输入变了自然重跑。

断点续跑用 `--from <阶段>`。**注意**：改了初译口径后必须从 `03_translate` 起跑，
不能只重跑 `04_polish`——润色读的是初译产物，只跑润色会静默沿用旧译文。

## GPU 串行

一张卡同时跑 ASR 和 LLM 会互相拖死。所以：

- 单一锁文件 `data/tmp/pipeline_queue/gpu.lock`，内容 `{pid, job, since}`
- 抢不到**直接退出**（退出码 2）并打印占用者，不排队等——避免两个长任务互相饿死
- 持有者崩溃（pid 不存活）→ 自动清理；超过 24h 视为过期

## 门禁

| 门禁 | 类型 | 抓什么 |
|---|---|---|
| `check` | 硬 | 时间轴递增、空行、日文残留、编号残留、繁体、半角标点 |
| `empty` | 硬 | **空字幕**：用 ASR 重建「应有的行」，逐行对成品 |
| `errata_audit` | 硬 | 错字表自身正例/反例是否仍成立 |
| `test_regressions` | 硬 | 历史缺陷回归 |
| `scan` | 参考 | 源语必要条件 + 跨行泄漏的启发式扫描 |
| `trunc` | 参考 | 有字但被截断的行 |
| `slot_guard` | 参考 | 语义槽位（主体/否定/因果）丢失 |
| `overlay` | 参考 | 修复覆盖层与盘上文本是否一致 |

### 空字幕门禁为什么必须存在

空文本行天然有歧义：既可能是「读完即走」的清屏点，也可能是「这句没译出来」。
格式检查会把它当清屏点放过，扫描只看源语侧，截断审计只看「有字但被截」。

所以换口径：**不猜空行的角色，直接拿「应该有的行」去对成品**。
「应该有的行」由 ASR 确定性重建，复用与生产同一套切分函数。

⚠️ 两个静默陷阱：
1. 切分口径必须与生产一致（同一份配置、同一套代码），否则重建出的行数对不上；
2. 历史产物与现存 ASR 常常不是同一版（时间戳差 0.4–2 秒），
   所以要区分「零星缺失」（真缺陷）和「整轨成片对不上」（版本漂移）。

## 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| `gate_failed` on `check` | 半角标点 / 日文残留 | `repair` 修那几行，别 `--force` |
| `gate_failed` on `empty` | 某句没译出来 | 先看 `06_checks/empty_check.json` 的 `missing` 列表 |
| 阶段 `failed` 但日志没报错 | stdout 被截断导致管道错误 | 已在代码里修；旧版本会误判 |
| 续跑后行数变了 | 只重跑了润色，没重跑初译 | 从 `03_translate` 起跑 |
