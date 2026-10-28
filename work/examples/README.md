# 合成样例（无版权内容）

用来跑通链路、写测试、复现 bug。全部是**手写假数据**，与任何真实作品无关。

| 文件 | 说明 |
|---|---|
| `asr_sample.json` | ASR 输出格式样例：3 段，带词级时间戳 |
| `lrc_sample.lrc` | 成品格式样例：含「读完即走」的清屏 spacer |

## ASR json 必需字段

```json
{
  "source": "sample.mp3",
  "language": "ja",
  "duration": 12.0,
  "segments": [
    {"start": 1.0, "end": 3.2, "text": "おはようございます。",
     "words": [{"s": 1.0, "e": 1.4, "w": "おはよう"}]}
  ]
}
```

- `words` 可以缺省（缺省时按标点合成伪词，精度略降）
- `segments[].text` 是这一段的完整文本；`words[].s/e` 是词级起止

## LRC 格式

```
[00:01.00]早上好。
[00:03.20]
[00:04.00]今天天气真好呢。
```

**空文本行是清屏 spacer**：LRC 语义下，一行显示到下一行时间戳为止；
空行让字幕在「读完」那一刻清掉，而不是一直挂到下一句开始。
所以「空行」不等于「没译出来」——后者要靠 `empty` 门禁单独判。

## 试用

```bash
mkdir -p data/{draft,locks,overlay,tmp,runs,out,style}
cp examples/asr_sample.json data/draft/RJ0000001/asr/sample.json
# 之后可以从 03_translate 起跑（跳过 ASR）
```
