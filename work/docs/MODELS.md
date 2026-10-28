# 模型准备（本地运行必读）

> **本仓库不含任何模型权重。** 模型有 4~6GB 一个，且各自许可证不同，  
> 不适合塞进 git。这里给出**下载地址 + 放哪 + 怎么配**。
>
> 一句话：把模型下载到任意目录，然后写一个 `work/pipeline/pipeline.local.json` 指过去，  
> **不用改任何代码**。

---

## 1. 需要哪些模型

| 用途       | 模型                          | 大小              | 显存      | 必需     |
| -------- | --------------------------- | --------------- | ------- | ------ |
| 语音识别（01） | `faster-whisper-large-v3`   | ~3GB            | 3~4.5GB | ✅      |
| 初译（03）   | **Sakura-7B**（默认）           | ~4.3GB (IQ4_XS) | ~4.3GB  | ✅      |
| 初译备选（03） | `HY-MT1.5-7B`（翻译专用）         | ~4.3GB (Q4_K_M) | ~4.4GB  | 可选     |
| 润色（04）   | `Qwen2.5-7B-Instruct`       | ~5.4GB (Q5_K_M) | ~5.6GB  | 仅旧路线需要 |
| 推理引擎     | `llama.cpp`（`llama-server`） | —               | —       | ✅      |



> **默认路线不需要 Qwen。** 2026-10-28 起默认是  
> `engine=sakura` + `polish_cleanup_only=true`（04 只做确定性后处理）。  
> 只有回退到旧路线（`engine=hymt` + `polish_cleanup_only=false`）才要 Qwen 做润色。

### 下载地址

| 模型                       | 地址                                                                                              |
| ------------------------ | ----------------------------------------------------------------------------------------------- |
| llama.cpp                | <https://github.com/ggml-org/llama.cpp/releases> （下 `llama-*-bin-win-cuda-x64.zip`，解压即用）        |
| faster-whisper large-v3  | <https://huggingface.co/Systran/faster-whisper-large-v3> （或 `pip install faster-whisper` 首次自动下） |
| Sakura-7B GGUF           | <https://github.com/Hengle/SakuraLLM> （模型列表里有 `sakura-7b-qwen2.5-v1.0-iq4xs.gguf`）              |
| HY-MT1.5-7B GGUF         | <https://modelscope.cn/models/Tencent-Hunyuan/HY-MT1.5-7B-GGUF>                                 |
| Qwen2.5-7B-Instruct GGUF | <https://modelscope.cn/models/Qwen/Qwen2.5-7B-Instruct-GGUF>                                    |

> 国内网络：HuggingFace 可换 `hf-mirror.com`；ModelScope 一般直连即可。

---

## 2. 放哪：写 `pipeline.local.json`

`work/pipeline/pipeline.json` 是**公共配置**（进库，不含任何绝对路径）。  
本机路径写进 **`work/pipeline/pipeline.local.json`**（**已在 .gitignore 里**，不会上传）：

```json
{
 "models": {
  "llama_dir":  "D:/llama.cpp",
  "models_dir": "D:/models",

  "model_sakura": "D:/models/sakura-7b/sakura-7b-qwen2.5-v1.0-iq4xs.gguf",
  "model_hymt":   "D:/models/hy-mt1.5-7b/HY-MT1.5-7B-Q4_K_M.gguf",
  "model_q5":     "D:/models/qwen2.5-7b-instruct-gguf/qwen2.5-7b-instruct-q5_k_m-00001-of-00002.gguf",
  "model_q4":     "D:/models/qwen2.5-7b-instruct-gguf/qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf"
 },
 "asr_model": "D:/models/faster-whisper-large-v3"
}
```

只写你实际用到的就行，缺的项会退回默认值。

### 也可以全用环境变量（优先级更高）

```bash
KIKOERU_LLAMA_DIR=D:/llama.cpp
KIKOERU_MODELS_DIR=D:/models
KIKOERU_MODEL_SAKURA=...
KIKOERU_MODEL_HYMT=...
KIKOERU_MODEL_Q5=...
KIKOERU_ASR_MODEL=...
KIKOERU_HOST=127.0.0.1      # 默认
KIKOERU_PORT=8080           # 默认
```

解析顺序：**环境变量 → `pipeline.local.json` → `pipeline.json` → 内置默认值**。

---

## 3. 8GB 显存怎么排

单卡 8GB **装不下 whisper + 7B 同时跑**，管线是**分阶段串行**的：

```
01_asr  （whisper，约 4.5GB）→ 退出、释放显存
03/04   （7B GGUF，约 5.6GB）→ 退出
```

`serve.py ensure --<engine>` 会**检查端口上真正加载的 gguf**，与声明的引擎不符就重启。  
失败会 `exit 2`——这是刻意的：**声明了什么，就必须验证实际是什么**  
（2026-10-28 有过「声明 hymt、实际跑 Qwen」的事故，全库 115 行英文残留）。

### 显存不够时的调整

| 现象                  | 调法                                          |
| ------------------- | ------------------------------------------- |
| 启动即 OOM             | 换更小量化：Q4_K_M / IQ4_XS                       |
| 上下文不足（报 `n_ctx` 超限） | `pipeline.json` 的 `slots` 从 2 降到 1（每槽上下文翻倍） |
| 想更快                 | `slots=2`（默认，快一倍，但每槽上下文减半）                  |

---

## 4. 自检：确认配置生效

```bash
# 1) 配置读到了吗（会打印实际生效的引擎/槽数）
python work/pipeline/pipeline.py status

# 2) 模型能起来吗（会打印「模型校验通过：<gguf>[<key>]」）
python work/scripts/serve.py ensure --sakura
python work/scripts/serve.py status

# 3) 全链路自检（不需要模型）
python work/pipeline/pipeline.py selftest        # 13 项
python work/scripts/empty_check.py --selftest    # 4 项
python work/scripts/semantic_coverage.py --selftest  # 9 项
```

`serve.py ensure` 的输出里那句 **`模型校验通过：xxx.gguf[sakura]`** 是唯一可信的凭证——  
它来自 `/props` 的 `model_path`，不是配置文件里写的值。

---

## 5. 常见报错

| 报错                         | 原因                       | 处理                                      |
| -------------------------- | ------------------------ | --------------------------------------- |
| `推理服务启动失败（引擎 sakura）`      | llama.cpp 路径不对 / 模型文件不存在 | 检查 `llama_dir` 与 `model_sakura`         |
| `推理服务未通过模型校验（期望引擎 sakura）` | 端口上跑的是别的模型               | `python work/scripts/serve.py stop` 后重试 |
| `⚠️ 配置文件不存在：...`           | `pipeline.json` 路径不对     | 正常应指向 `work/pipeline/pipeline.json`     |
| ASR 报 `模型路径不存在`            | whisper 模型没下             | 设 `KIKOERU_ASR_MODEL` 或 `FW_MODEL`      |

---

## 6. 许可证提醒

各模型许可证不同（Sakura 系列、Qwen 系列、HY-MT 各有条款），**商用前请自行确认**。  
本项目只调用它们，不分发权重。
