# kikoeru-pipeline（字幕生产管线）

把日语音声做成中文 LRC 的**确定性执行层**：六阶段编排、断点续跑、GPU 互斥、产物追溯、
知识表版本化与变更提案。**Agent 只做监督与例外处理，不在这里跑业务判断。**

```
pipeline/
  pipeline.py            入口（run / check / status / promote / repair / explain /
                         adopt / knowledge / queue / selftest / version）
  pipeline_lib/
    paths.py             路径与配置（唯一出口）
    manifest.py          run 目录 / manifest / 哈希
    fingerprint.py       阶段指纹（配置 + env + 模型 + 输入 + 代码）
    stages.py            六阶段执行器 + 门禁
    knowledge.py         知识表版本 + 提案 + 事务化合并
    queue.py             文件锁（token）+ GPU 串行队列
    cli.py               命令实现
    selftest.py          13 条 P0 断言
  tools/dashboard.py     只读看板 → runs/dashboard.html
  pipeline.json          配置（解释器 / 引擎 / slots / profile）
  VERSION                版本号
  CHANGELOG.md           变更日志（按日期）
```

## 快速开始

```bash
PY="C:/Users/zyk/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
cd $KIKOERU_ROOT

"$PY" pipeline/pipeline.py version          # 看版本
"$PY" pipeline/pipeline.py selftest         # 13 条断言（沙箱内跑，不碰生产）
"$PY" pipeline/pipeline.py status           # 有哪些 run、跑到哪
"$PY" pipeline/pipeline.py run --project RJxxxxxxx --source $KIKOERU_ROOT/RJxxxxxxx/mp3
"$PY" pipeline/pipeline.py check --run <run_id>
"$PY" pipeline/pipeline.py promote --run <run_id> --dry-run
"$PY" pipeline/tools/dashboard.py           # 生成 runs/dashboard.html
```

## 三条硬规矩

1. **`runs/<run_id>/` 从不直接进生产**，只有 `promote`（自动备份、核对文件集）。
2. **知识表只能走提案**：`knowledge propose → validate → approve --by human --confirm`
   （自动快照版本；合并后自检；失败回滚 + `commit_pending`）。
3. **GPU 只有一把锁**，抢不到退出码 2，不排队等；`selftest` 覆盖了锁的 token 语义。

## 版本号规则

- 测试版：`vX.Y.Z-beta.N`（当前 `v0.1.0-beta.1`）
- 正式版：`vX.Y.Z`
- 发版动作：改 `VERSION` → `CHANGELOG.md` 顶部按日期加一节 → `selftest` 全过 → 重新生成看板。

## 完整文档

- 设计与边界：`docs/rules/PIPELINE.md`（为什么这么设计 / 命令 / manifest 字段 / 已知缺口）
- 工作台入口：`docs/WORKBENCH.md`
- 桌面桥（ZCode/Gemini/ChatGPT）：`docs/rules/BRIDGES.md`