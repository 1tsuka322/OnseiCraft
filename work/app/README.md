# OnseiCraft —— 字幕管线的桌面外壳

> 名字取自日语「音声（おんせい）」+ Craft：把音声做成字幕的手艺。

把「拖入音频 → 生成中文字幕」这件事包成一个人能用的软件，**不改变管线本身的任何行为**：
它仍然走 `pipeline_lib` 的六阶段、仍然只通过 `promote` 进成品目录、仍然受十一项门禁约束。
OnseiCraft 只做三件事：收集输入、展示进度、把 ASR 原文留下来给反馈日志用。

```
双击 OnseiCraft.bat
```

---

## 1. 界面与流程

```
首页 ─┬─ 音频文件 ──→ ① 选择音频 → ② 有台本吗 → ③ 生成 → 完成
      ├─ 反馈日志      （拖入/点选）   （拖入 txt/pdf 或跳过）
      ├─ ASR 暂存管理
      └─ 设置（输出目录 + 模型路径）
```

- **① 选择音频**：拖入文件或文件夹，支持 mp3 / wav / flac / m4a / ogg / opus / aac / wma。
  作品号从目录名里的 `RJxxxxxxx` 自动提取，提不到就按时间生成一个，**可以手改**。
- **② 台本**：txt 或 pdf。pdf 用 pypdf 提文本（失败回退 pdfplumber），自动去页眉页脚。
  没有就点「跳过」——不影响生成，只是识别少了时间锚点。
- **③ 生成**：六阶段逐条显示状态，顶部是总进度与剩余时间，下面是实时日志。
  可以「停止」（当前阶段跑完才停，已完成的阶段会保留）。
- **完成**：给出字幕目录并可一键打开；同时问「保留 ASR 原文 / 不保留并删除」。

## 2. 数据流（路径别搞混）

```
用户拖入 ──┬─ 是文件夹 → 直接当 source（只读，不动用户文件）
           └─ 是零散文件 → 归集到 work/data/import/<RJ>/（同盘硬链接，跨盘才复制）
                              ↓
                    work/runs/<run_id>/        沙盒：01_asr … 06_checks
                              ↓ promote（自动备份 + 门禁检查）
                    输出目录/<RJ>/*.lrc        默认 <repo>/kikoerusubtitle
                              ↓ 用户选「保留」
              work/data/asr_cache/<RJ>/<run_id>/    asr / fix / lrc 三份
                              ↓
                  work/data/feedback/*.txt         发给开发者
```

⚠️ 输出目录在 `work/app/app.json` 里，启动时注入 `KIKOERU_PROD` **然后才** import
`pipeline_lib`——`paths.PROD` 是 import 时求值的常量，顺序错了就会「设置里改了、成品还进老地方」。

## 3. 反馈日志

选作品 → 勾轨道 → 写意见（可选填时间点，`00:12.3` 或 `12.3`）→ 导出 txt。文件里每条轨道带：

1. 用户的意见
2. **ASR 识别原文**（模型听到了什么）
3. **修后原文**（errata 之后，管线实际拿去翻译的版本）
4. 当前成品字幕

三份放一起才能判断「错在识别、错在翻译、还是错在润色」。

## 4. 配置

| 文件 | 内容 | 进版本库 |
|---|---|---|
| `work/app/app.json` | 输出目录、暂存目录、实测倍率（ETA 用） | 否 |
| `work/pipeline/pipeline.json` | 公共管线配置（引擎/槽数/门禁清单） | 是 |
| `work/pipeline/pipeline.local.json` | **本机**模型路径（OnseiCraft 设置页写这个） | 否 |

设置页改模型路径只动 `pipeline.local.json`，不会污染公共配置。

## 5. 它不做什么

- **不绕过 promote**：门禁不过就拒绝进生产目录（此时 OnseiCraft 会把字幕直接复制到输出目录并明确告警「未经门禁保护」）。
- **不改成品**：想改某一行仍走 `scripts/fixline.py` / `pipeline.py repair`。
- **不动知识表**：术语/错字表仍走 `knowledge propose → validate → approve`。
- **不并发**：GPU 单卡，管线自带 `gpu.lock`；同时开两个 OnseiCraft 会排队，不会并行。

## 6. 依赖

- PySide6（界面）
- pypdf / pdfplumber（台本 PDF）
- 管线本身的依赖见 `work/requirements.txt`
- 模型（whisper / Sakura gguf / llama-server）在设置页指路径，缺了首页会提示

## 7. 排错

| 现象 | 原因 |
|---|---|
| 进度一直 0% | 已修：改成按阶段时间估算（`stages._tee` 写日志不 flush，读文件读不到） |
| 02_asr_fix 报 `no attribute 'reconfigure'` | 已修：接管 stdout 的 `_Stream` 补了 `reconfigure()` |
| 启动即崩、退出码 127 | 已修：`_Stream` 只捕获跑管线那条线程的写入，否则主线程 print 会递归 |
| 字幕没进输出目录 | 门禁没过。看日志里的「未进生产目录」那行，或 `python work/scripts/acceptance.py` |
| 台本打轴失败 | 台本只有「章」没有「トラックN」时无法自动对应，需要逐轨台本 |

---

## 8. 打包成安装包

```bash
# 1) exe（onedir；onefile 每次启动要解压 1GB 且管线每阶段起一次子进程，不可接受）
python -m PyInstaller --noconfirm --clean \
  --distpath work/tmp/pydist --workpath work/tmp/pybuild work/app/OnseiCraft.spec

# 2) 安装包（Inno Setup 6；装完可自选安装目录、建开始菜单/桌面快捷方式、可从控制面板卸载）
"C:\Users\<你>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" work/app/installer.iss
```

产物：`work/tmp/installer/OnseiCraft-0.1.0-Setup.exe`（约 600MB，装完约 1.2GB）。

**装到哪**：默认 `%LOCALAPPDATA%\Programs\OnseiCraft`（不需要管理员）。不建议装进
`Program Files`——程序要在自己目录下写 `work\runs`、`work\data`，那里不可写。
向导里可以改成 `D:\OnseiCraft` 这类位置。

### 包里有什么、没有什么

| | 内容 |
|---|---|
| **有** | Python 运行时、PySide6、ctranslate2 + cuBLAS/cuDNN（GPU 推理必需）、faster-whisper、onnxruntime、`work/{scripts,pipeline,presets}` |
| **没有** | **模型**（约 8GB）、音频、成品字幕、语料 |

模型不打包是硬约束：whisper large-v3 3.1GB + Sakura-7B 4.2GB + llama.cpp CUDA 0.7GB，塞进去安装包要 8GB 以上。
所以做成**首次启动检测 + 一键下载**：首页会提示缺什么，设置页点「下载」自动下到
安装目录的 `models\` 并写好配置（支持断点续传、可切国内镜像 hf-mirror.com）。

### 三个只有打包后才会暴露的坑

1. **`sys.stdout` 会变成 None**：纯 GUI 程序（`console=False`）没有控制台，而管线是
   `OnseiCraft.exe asr.py ...` + PIPE 抓日志的。解法不是改回 `console=True`
   （那样双击会闪一个黑框、任务栏还留个控制台，看起来就像「启动了个脚本」），
   而是在 `main.py` 里用 `os.fdopen(1)` 把父进程给的管道句柄拿回来，
   拿不到才退到 devnull。
2. **子进程要能执行 .py**：打包后没有 python 了，`paths.python_exe()` 返回的是 exe 本身，
   所以 `main.py` 里要把「第一个参数是 xxx.py」转成 `runpy.run_path(work/scripts/xxx.py)`。
   少了这段，01_asr 会「启动一个 GUI」然后卡住。
3. **`KIKOERU_ROOT` 必须在最早期注入**：`paths.py` / `rootloc.py` 都靠它定位 `work/`，
   读不到就按脚本位置反推——在 `_internal` 布局里会算到安装目录外面去。

---

## 9. 更新（软件内一键，不用重新配置）

设置页 → 关于 → **检查更新**。有新版会直接弹出更新日志，点「下载并安装」后自动：
下载 → 关闭本程序 → 静默安装 → 装完自动打开新版。装完第一次启动还会再弹一次
「本次更新内容」（记在 `work/data/update_pending.json`，读过即删）。

**为什么不用重新配置**：安装包只覆盖这三处 ——

```
{app}/_internal/            程序本体
{app}/work/scripts/         阶段脚本
{app}/work/pipeline/        管线代码 + 公共配置
{app}/work/presets/         默认知识表（术语/错字/允许集）
```

而下面这些**都不在安装包里**，覆盖安装碰不到：

```
{app}/models/                       模型（约 8GB）
{app}/work/pipeline/pipeline.local.json   你的模型路径（iss 里 Excludes 掉了）
{app}/work/data/                    ASR 暂存、锁、反馈日志、运行记录
{app}/work/runs/                    每次执行的沙盒
{app}/kikoerusubtitle/              已生成的字幕
```

⚠️ 一个例外：如果你手改过 `work/presets/` 里的知识表，**更新会被覆盖**。
要保留自己的术语表，请放 `work/data/style/`（优先级高于 `presets/`），那里不会被碰。

---

## 10. 依赖必须显式白名单（血泪教训）

管线脚本不是被 import 的，是**子进程 `OnseiCraft.exe xxx.py` 用 runpy 执行**的。
PyInstaller 静态分析完全看不到它们的 import —— 所以 `OnseiCraft.spec` 里那份
`collect_all(...)` 列表就是**全部真相**，漏一个的后果是：

> GUI 一切正常，跑到 02 阶段才 `ModuleNotFoundError: No module named 'janome'`。

2026-10-31 就这么漏了 7 个（janome / fugashi / unidic_lite / jieba / rapidfuzz /
pypinyin / joblib / zhconv），包从 548MB 涨到 630MB 才补全。其中两个是大头：

| 包 | 用途 | 体积 |
|---|---|---|
| `unidic_lite` | fugashi 的词典（06 词性判断） | 249 MB |
| `janome` | 02 纠错 / scan_violations / term_mine | 202 MB |
| `jieba` | 中文分词 | 42 MB |

**所以发版前必须跑一次自检**（在装好的软件里，不是开发环境）：

```bash
OnseiCraft.exe work/scripts/deps_selftest.py
# 可选：顺带测 PDF 台本提取
OnseiCraft.exe work/scripts/deps_selftest.py D:\某台本.pdf
```

30 项检查：逐个 import + **真实调用**（janome/fugashi 分词、jieba、pypinyin、zhconv、
rapidfuzz 匹配、pdfplumber 提取真实 PDF）+ presets 知识表存在性。全过才发版。

想加新依赖时：先 `pip install`，再把它加进 spec 的 `collect_all` 列表，
重新打包，跑 `deps_selftest.py` 确认，最后才发布。
