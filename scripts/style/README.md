# kikoeru-style-learn 脚本说明

风格学习 skill（skill B）：从「音频 + 人工中文字幕」的语料库，学出翻译风格库
（供 skill A 的润色 prompt 使用）。

> 完整流程与踩坑见上一级 `SKILL.md`。本文只讲**每个脚本是什么、怎么单独调**。

## 数据流（标准流程五步）

```
learning/<作品>/（音频+人工字幕）
   │
   ① asr_corpus.py  →  draft/learning/<作品>/asr/*.json（日文识别）
   ② diarize.py     →  声纹聚类（写 speakers）
      identify.py   →  角色判明（写 roles，需 llama-server）
   ④ build_library.py → style/parallel.json + stats + hard_rules + glossary
   ⑤ contrast.py    →  mt_vs_human.txt + 追加「机译通病」到 hard_rules.txt
```

`_recall.py` / `eval_asr.py` 是诊断工具，不参与主线。

---

## 脚本清单

### asr_corpus.py —— ① 全库识别（最耗时，数小时）
```bash
python asr_corpus.py [语料根目录] [输出根目录] [--only 关键字] [--skip 关键字]
```
- 默认语料根 `$KIKOERU_ROOT\learning`，输出 `$KIKOERU_ROOT\.workbuddy\draft\learning`。
- 已有 json 会跳过，可中断续跑。
- **音频要直接在作品目录下**（若在 `mp3/` 子目录，先上移到作品根目录，否则报「没有找到音频文件」）。

### diarize.py —— ② 声纹聚类
```bash
python diarize.py <asr目录> <音频目录> --corpus [key] [--device cpu]
```
- `--corpus` 遍历整个 asr 目录；末尾 `key` 只处理名字含关键字的作品。
- 写 `speakers` 字段到 asr json。

### identify.py —— ② 角色判明（需起 serve）
```bash
python identify.py <asr目录> <人工字幕目录> --corpus [key] --workers 3
```
- 用本地 llama 把 SPEAKER_00/01 判成具体角色，写 `roles` 字段。
- ⚠️ 判据靠「自称 vs 称呼」；曾把「お兄さん」（称呼）当自称，SYSTEM 里已写黑名单，别删。

### build_library.py —— ④ 建库
```bash
python build_library.py             # 增量累积到旧库
python build_library.py --reset     # 清空重建（--reset 会清掉 contrast 追加的通病节）
python build_library.py --key 关键字 # 只处理含关键字的作品
```
- 产出 `style/parallel.json`（日→中平行句对，100% 带角色标注）。
- **改了 identify.py 的 prompt 后必须 `--reset`**（角色标注只有建库才写进库）。

### contrast.py —— ⑤ 机译对照
```bash
python contrast.py --n 400
```
- 机译 vs 人工对照，归纳「机械译法」通病，追加到 `hard_rules.txt` 末尾。
- **在 build_library 之后补跑**（否则通病节被重写清掉）。

### align_style.py —— 底层对齐引擎
```bash
python align_style.py <人工字幕目录> <ASR目录> [--style DIR] [--tag 名称] [--no-llm]
```
- build_library 调它做「人工字幕 ↔ ASR 日文」按时间戳对齐。
- 支持 `.lrc` / `.vtt` / `.srt` 三种字幕格式（`SUB_EXT`）。
- `GUIDE_PROMPT` 有 8 个占位符，用 `guide_prompt_args()` 拼装，别手写元组。
- 产出 `style_guide.txt` 前会过 `sanitize_guide()`：把与统计/用户规范冲突的行改写成事实。
  句号分支跟随 `user_period_policy()`（读 `user_rules.txt` 的 period/no-period），
  省略号分支跟随 `stats.ellipsis_style`。**别在这两处写死标点形态**（曾硬编码六连点 + 跳过句号改写，
  导致指南与 `user_rules.txt` 冲突，2026-10-13 已修）。

### serve.py —— llama-server 管理（同 skill A 的那个，可复用）
identify / build_library / contrast 都要先起服务。

### 诊断工具（不参与主线）
- **`_recall.py`**：统计每个作品「人工字幕行数 vs 对齐成功行数」的召回率。
- **`eval_asr.py`**：对比人工字幕与 ASR 结果，评估识别质量。