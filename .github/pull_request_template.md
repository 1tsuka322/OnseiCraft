## 这个 PR 做了什么

<!-- 一句话。为什么改，比改了什么更重要 -->

## 类型

- [ ] 修 bug
- [ ] 新增确定性规则（查表 / 正则）
- [ ] 新增/调整门禁
- [ ] 文档
- [ ] 重构（不改变行为）

## 自检

```bash
python pipeline/pipeline.py selftest          # 13 项
python scripts/empty_check.py --selftest      # 改了门禁时
python -m compileall -q pipeline scripts
```

粘贴输出：

```
（贴这里）
```

## 检查表

- [ ] `selftest` 全过
- [ ] 新增/修改的脚本带自检，且自检输出贴在上面
- [ ] 没有提交 `internal/`、`runs/`、`regset/`、`data/`、`RJ*/` 下的任何文件
- [ ] 代码里没有绝对路径、没有真实作品号（用 `RJ0000001` 占位）、没有露骨台词
- [ ] 影响产物时更新了 `pipeline/VERSION` 与 `pipeline/CHANGELOG.md`
- [ ] 新增规则时给了**反例**（只给正例的规则一定会误杀）

## 关联

Closes #
