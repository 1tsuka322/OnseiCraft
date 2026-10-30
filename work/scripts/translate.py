# -*- coding: utf-8 -*-
r"""
阶段二：日文 -> 简体中文 机器初译（本地 Qwen2.5-7B / llama-server）。

用法：
  python translate.py <ASR目录> <初译输出目录> [文件名关键字]

例：
  python translate.py "./asr_out" "$KIKOERU_ROOT/data/draft/RJ9000005"

产出：<初译输出目录>\<音频文件名>.lrc（后面交给 polish.py 润色）
"""
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json
import glob
import time
import re
import argparse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "http://127.0.0.1:8080/v1/chat/completions"
MODEL_TAG = "faster-whisper large-v3 + Qwen2.5-7B-Instruct"

# ---------- 翻译引擎 ----------
# KIKOERU_ENGINE=sakura 切到 SakuraLLM（专为日中翻译训练的 ACGN 微调）。
# Sakura 官方明确要求使用它自己的 prompt 格式与采样参数，用别的 prompt「不保证质量」，
# 所以这里走独立分支，而不是套用上面的 SYSTEM。
ENGINE = os.environ.get("KIKOERU_ENGINE", "qwen")
SAKURA_SYSTEM = (
    "你是一个轻小说翻译模型，可以流畅通顺地以日本轻小说的风格将日文翻译成简体中文，"
    "并联系上下文正确使用人称代词，不擅自添加原文中没有的代词。\n\n"
    "# Additional Requirements:\n"
    "★ 最重要的一条：**日文的主语/宾语几乎全省略，中文必须补出来**——这是「看不看得懂」的关键。\n"
    "  两人对话默认：说话人＝「我」，听话人＝「你」；独白/内心话一律从主角第一人称视角补，主语是「我」。\n"
    "  补主语的正确做法（照这些来）：\n"
    "  ·「家のこと知らないで」 译成「你又不了解我们家的情况」（补出「你」）\n"
    "  ·「お母さんがやってんじゃない?」 译成「这本来是我妈弄的啦」\n"
    "  ·「感謝しろよ」 译成「你得谢谢我哦」\n"
    "  ·「いけないんだ」 译成「（你）不能这样啦」\n"
    "  ·「夏子一人じゃないけど」 译成「不光我自己」（对话中自称名字要改成人称代词）\n"
    "  · 以「〜ないで」「〜してる」这类连用形收尾的句子，补「你」；\n"
    "    授受动词（あげる/くれる/もらう）补「您/我」；\n"
    "    思考感觉动词与感情形容词（思う/感じる/うれしい/こわい/ほしい）补「我」。\n"
    "  ·「〜んじゃない?」「〜じゃない?」是**推测/反问**，译「…吧？」「不是…吗？」。\n"
    "  每句自问「谁对谁做了什么」，答不上来就是没补主语。宁可啰嗦，别让读者猜。\n"
    "2. 句内停顿优先用半角空格（短促连呼、重复、感叹之间），只有真正的从句才用逗号；"
    "一行最多一个逗号；句末用句号「。」收束。\n"
    "3. 节制语气词，不要满篇「呢/啦/哦/呀」；「ええと」「あの」别机械译成「那个」「怎么说呢」。\n"
    "4. 翻译腔五病要避免：「关于/对于」综合症、「被」字滥用、「进行/做出」虚假动词、"
    "无意义的复数「们」、过长的定语「的」链。\n"
    "   另外「とき/こと/場合」别机械译成「的时候/的事情/的情况下」：\n"
    "   ·「気持ちいいこと」 译「舒服的感觉」不译「舒服的事情」\n"
    "   ·「当たり前のこと」 译「理所当然」不译「理所当然的事情」\n"
    "   ·「〜ということもある」 译「有时也会〜」不译「也有〜这样的情况」\n"
    "   · 但「想教你的事情」「不可思议的事情」这种中文本来自然的不必改。\n"
    "5. 拟声拟态词用中文真实说法（叠词复沓），绝不音译假名，省略促音「っ」。\n"
    "6. 长定语句拆成中文小短句，别把修饰语全堆在名词前。\n"
    "7. 译文讲不通时回头怀疑 ASR 听错了近音词（「男子近世」实为「男子禁制」；"
    "房间对话里的「大象」多半是「そう」），按正确词意译，别照着错字硬翻。"
)
SAKURA_TEMP = 0.1
SAKURA_TOP_P = 0.3

# ---------- HY-MT 1.5（腾讯混元，翻译专用模型）----------
# KIKOERU_ENGINE=hymt 切到 Hunyuan-MT 1.5 做**初译**（后续仍由 Qwen + 风格库润色）。
# ⚠️ 它是 instruction-following 的**翻译模型**，不是通用聊天模型：prompt 必须明确
#    「把下面的文本翻译成中文」，不能当 ChatGPT 用；也不做「编号|」列表，所以**逐句翻**。
# 官方推荐参数：top_k=20 / top_p=0.6 / repetition_penalty=1.05 / temperature=0.7。
HYMT_TEMPLATE = "把下面的文本翻译成中文，不要额外解释。\n\n%s"
# 归属提示专用模板（GPT 第九轮 Q1「HY-MT 也要注入，别指望 polish 修上游错译」）。
# ⚠️ HY-MT 是翻译专用模型，不能在它前面堆「你是资深译者…」这类系统提示；
#    只能把提示压成**一行约束 + 原文**，保持「把下面的文本翻译成中文」的主指令不变。
HYMT_HINT_TEMPLATE = ("把下面的文本翻译成中文，不要额外解释。\n"
                      "注意：这句是对听者说的话，%s\n\n%s")
HYMT_TEMP = 0.7
HYMT_TOP_P = 0.6
HYMT_TOP_K = 20

# 归属 hint 的**短句化**版本（HY-MT 吃不下长说明；只在 translate 层用）
# 口径同 pragmatics_hints.ACT_PROMPT：只说「注意归属」，不给固定人称。
HYMT_HINT_SHORT = {
    "listener_address": "「%s」指的是听者（你），别把听者的经历写成「我」",
    "listener_experience_confirmation":
        "「%s」可能指听者那边，判断这句讲的是谁的状况，别默认安到说话人头上",
    "speaker_denial": "「%s」是说话人在强调自己的否认（「我都说了…」），不要翻成对听者提问",
    "recall_question": "「%s」是疑问句，不要翻成陈述",
    "consensus_seeking": "「%s」是在寻求听者认同，但不一定是疑问句",
    "explanation": "「%s」是解释语气，判断说明的是谁的状况",
    "assertion": "「%s」是说话人在陈述/强调，不是疑问",
    "listener_reference_weak": "「%s」可能指「你那边」，也可能只是方位",
}
# ⚠️⚠️ 2026-10-18 **默认关闭**（D4 撤销）。
#     实测事故：HY-MT 是**纯翻译模型**，没有指令跟随能力——它会把这行 hint
#     （`注意：这句是对听者说的话，「んだ」是解释语气…`）**当成待译文本一起翻译**，
#     于是初译层直接产出 `注意：这句话是针对听者说的，"んだ"这个词用于表达某种语气…`
#     这类元话语，一次污染 121 条。
#     归属问题改在 **polish 层**修（Qwen 能理解指令，且盲听 G1 13/14 证明有效），
#     初译层只要忠实直译。要恢复旧行为设 KIKOERU_HYMT_PRAG=1。
HYMT_PRAG_ON = os.environ.get("KIKOERU_HYMT_PRAG", "0") in ("1", "true", "True")
_hymt_hint_fn = [None]


def _hymt_hint(text):
    """给单句日文生成归属提示（无命中返回 ""）。失败静默降级。"""
    if not HYMT_PRAG_ON:
        return ""
    if _hymt_hint_fn[0] is None:
        try:
            import pragmatics_hints as _ph
            _hymt_hint_fn[0] = (_ph.cue_hint, _ph)
        except Exception:
            _hymt_hint_fn[0] = False
    if not _hymt_hint_fn[0]:
        return ""
    fn, mod = _hymt_hint_fn[0]
    h = fn(text)
    if not h:
        return ""
    tpl = HYMT_HINT_SHORT.get(h["act"])
    if not tpl:
        return ""
    ev = (h.get("evidence") or h.get("explicit_listener_marker")
          or h.get("soft_listener_marker") or [h["act"]])
    return tpl % "／".join(ev)

# 每块多少行一起翻。⚠️ `_parse_plain` **只校验行数**，模型把 N 行重排成另外 N 行
# （对调 / 把两句并一句再拆开）时**静默通过**——2026-10-29 实测到三种：
#   对调：`お塩が溜まって…`(15字)→「用沾满爱液的手掌使劲摩擦」(12字)，与下一行互换
#   截断：`姉上をここまで翻弄できるのはあなただけです`(22字)→「姐姐，」(3字)
#   回吐：下一句首词 `ダメ` 挂到上一句句尾（「晚安。不行」）
# **A/B 实锤（RJ256650 05 轨 656.46/660.10）**：CHUNK=10 两行对调；
# CHUNK=3 变成「我要用湿答答的手掌」/「摩擦到盐巴积到喷出来为止」，各归各位。
# 所以默认改成 3。代价：调用次数变多（RJ256650 6 轨 2 分 25 秒，可接受）。
CHUNK = int(os.environ.get("KIKOERU_CHUNK", "3"))
WORKERS = 3
CTX_KEEP = 3

SYSTEM = """你是资深日语字幕翻译，专精日本音声作品/广播剧对白的汉化。
把日语台词翻译为简体中文，遵守以下规则：

★★ 先看这一条——它最影响「看不看得懂」：**日文的主语/宾语几乎全省略，中文必须补出来**。
   两人对话默认：说话人＝「我」，听话人＝「你」。
   ·「家のこと知らないで」 ✗「不了解家里的情况」 ✓「你又不了解我们家的情况」
   ·「お母さんがやってんじゃない?」 ✗「不是我妈弄的？」 ✓「这本来是我妈弄的啦」
   ·「感謝しろよ」 ✗「感谢吧」 ✓「你得谢谢我哦」
   ·「いけないんだ」 ✗「不该这样」 ✓「（你）不能这样啦」
   · 以「〜ないで」「〜してる」这类连用形收尾的句子，省略的主语默认是「你」。
   **每句自问「谁对谁做了什么」，答不上来就是没补主语。宁可啰嗦，别让读者猜。**
   ▸ 以下八类句子几乎一定要补人称：
     敬语（「お待ちしましょう」→「我来」）· 寒暄语（「お待たせしました」→「让您久等了」）
     · 授受动词（あげる/くれる/もらう →「送给您」「朋友给我的」）
     · 思考感觉动词（思う/感じる/気がする → 补「我」）
     · 感情形容词（うれしい/こわい/ほしい/さびしい → 补「我」）
     · 「たい」→「我」；「う・よう」表意志/劝诱 →「我／我们」；命令 → 补「你／您」
     · **对话中自称名字或直呼对方名字 → 改成人称代词**
       （「夏子一人じゃないけど」→「不光我自己」；「夏子のほかに？」→「除你之外？」）
   ⚠️ 但**补主语 ≠ 改人称**——这是两个相反的错误。原文明确说了是谁就是谁，
      别把第三人称改成「我」：
      ·「バレー部の顧問の先生なんだ」 ✓「是排球部的顾问老师」 ✗「我是排球部的顾问老师」
        （「〜なんだ」是在向对方说明**第三方**的事，保持第三人称）
      · 孩子对母亲说的「ママ」 ✓「妈妈」 ✗「我妈」（「我妈」是向别人介绍自己母亲时才用）

1. 只输出译文。每行格式严格为「编号|译文」，编号与输入一致，不要空行、不要解释。
2. 口语化、自然，符合中文日常说话习惯，禁止逐字直译和翻译腔。
   以下对照是同类错误的标准改法（照这种思路译，不要照抄字面）：
   ·「うちは男ばかりなので」✗「我家是男性居多，因为…」✓「家里全是男的嘛」
   ·「いつもお世話になって」✗「一直承蒙照顾」✓「平时多亏你们照顾啦」
   ·「気持ちよかった」✗「感觉很好」✓「好舒服」
   ·「偉いね」✗「做得不错呢」✓「好乖好乖~」
   ·「やばいやばい」✗「糟了糟了」✓「不妙 不妙…」
3. 保留语气词（ね/よ/な/わ/ぞ/かな等）、拟声拟态词、角色称呼（如「お兄ちゃん」译作「老哥」）。
4. 忠实原文，不增不减，不添加解释或注释。
5. 一句对一句，行数必须和输入完全一致，绝不合并、拆分或省略。
6. 单行译文控制在 40 字以内，适合做字幕。
7. **必须参照「前文」保持连贯**：人称（我/你/他）、称呼、情绪强度要与前文衔接，
   同一角色同一场戏口吻前后一致。
8. **禁止机械直译**：按中文口语习惯重新组织句子，不要照搬日语语序。
   日文助词（は/が/を/に/の）不需要译出。
9. 喘息、呻吟、口吃、拖音（あ、ん、え、その…）按中文口语自然表达，不要整句删掉。
   ⚠️ **纯气声/呻吟串**（整句只有单假名 + 省略号，如「お…あ…あ…足…」「お…お…」「ん…」，
   或一串气声假名如「もぅ…もぅ…」「うぇぁぁぁ…」「んん…」）是**没有词义的气声**，
   逐字译成中文语气词即可：あ→啊、お→哦／唔、う→呜、ん→嗯、ひ→咿、もぅ→嗯、うぇぁ→唔啊。
   **不要借它们字面上的词义**——`もぅ`（もう＝已经）在呻吟串里**不是**「已经」，
   `んん` 不是「嗯嗯」以外的任何词。
   **也不要因为前文出现过某个词就往那个词上靠**——实测前文有「妈妈」，
   模型就把「お…あ…あ…足…」译成了「妈…妈…妈…脚…」（应该只是「哦…啊…啊…脚…」）。
10. **疑问句必须译成疑问句**：识别出的日文常常不带「？」，
    所以不能靠原文的问号判断。看句末助词（か/かな/かしら/の/ん/っけ/よね/でしょ/でしょう/
    ですか/ますか）和上下文是否在发问、反问、确认，是疑问句句末就用「？」。
11. **句末用句号「。」收束**；**句内停顿优先用半角空格**，别一遇到停顿就写逗号
    （「主换人了呢 真可爱」「加油 加油 加油」），只有真正的从句才用逗号
    （「今天没戴眼镜，所以亲起来很方便」），一行最多一个逗号；语气延长用三点「…」。
12. **「ね」不要一律译「呢」**：人工译者同一个ね会译成呢/啊/吧/喔 或干脆省略，
    连续几句都用「呢」会非常机械。陈述、叮嘱语气优先「啊/吧/喔」或什么都不加。
13. **拟声拟态词要选中文里真实存在的说法**，绝不音译假名：
    シコシコ→撸撸／套弄（不是"叽叽"）、ずりずり→蹭蹭、ぐちゅぐちゅ→咕啾咕啾、
    くりくり→揉揉转转、ぎゅ→抱紧／捏紧。
    **先分类再选词**：音/声（自然或物的声音）· 情（心理状态）· 容（人的样子）· 态（事物状态）。
    同一个「がらがら」在「車輪ががらがら」是**声**，在「人ががらがら」是**态**。
    **定感情色彩**：同是笑声，からから＝爽朗、にやにや＝偷偷笑、げらげら＝无所顾忌地大笑。
    **注意程度**：「あかあかと」＝闪闪发亮（不是"明亮"）、「キョトンと」＝一时惊呆了（不是"惊奇"）。
    ⚠️ **独立成句的短片假名尤其危险**——没有上下文时最容易按音节拼成汉字：
       キュー／チュー → 「啾」（亲吻声），绝不译「基欧」；ぴゅっ／びゅっ → 「噗／喷」。
       凡「基欧」「卡雅玛雅」「塔科斯」这种按假名逐音节拼出来的汉字，一律是错的。
       **唯一例外是角色名**（リリア→莉亚、マヤ→玛雅），那是固定音译，要全篇一致。
14. **撒娇、拉长音、兴奋时可以用「~」收尾**（好舒服喔~），
    高潮、心动处可以在句尾加「♡」（在里面抖起来了♡）——人工译者这样用，
    但别每句都加，整轨控制在零星几句。
15. **口语小词别按字面硬译**（这是最常翻出"莫名其妙"中文的地方）：
    ·「いや」＝「不是啦／没有啦」，**不是「不行」**；「いや、だって」＝「不是啦，可是…」
    · 句末「〜んじゃない?」「〜じゃない?」是**推测／反问**——译「…吧？」「不是…吗？」，
      **不是「应该做」**：「お母さんがやってんじゃない?」✓「这不是我妈弄的嘛？」
    · 日文没有「应该」这种义务表达；中文里冒出「应该」多半是错译，先怀疑自己。
    ·「お邪魔する」＝「打扰了／来叨扰」，不是「闯入」；「勝手に」＝「擅自」。
    · 原文没有逗号时，按**话语**切分理解，别把两三个短句揉成一坨。
    · ⚠️ **`〜てあげる` 是「替对方做某事」的助动词，不是「给她／给他」**。
      它只表示施恩于对方，宾语往往已经出现在前文，**不要单独译出「给她」**：
      `こすりつけてあげる` ✗「蹭上去给她」 ✓「替你蹭上去」（あげる 只表施恩，宾语已在前文）。
    · ⚠️ **听不懂的拟声串不要编造具体名词**。ASR 常把喘息吐成 `チパチパ`/`ピチャピチャ`
      这类看不懂的拟声——遇到这种，**按拟声处理或淡化**，绝不能脑补成「电击」「鞭打」
      这类具体动作（实测 `チパチパされている間` 被译成「被**电击**的时候」，ASR 里没有电击）。
16. **⭐ 拟声词（A7，GPT 第六轮 Q2：默认策略已改）**
    语料统计（10.6 万人工句对）显示：**人工译者几乎不留纯语气词（0 / 106743）**，
    他们译成**动作/效果**（`ぎゅぎゅ`→「握～握～」、`ぎぎゅ`→「捏 捏 捏」）。
    **所以默认策略按优先级：**
      1. **有可推断的动作或效果** → 译成动作/效果。`ぎゅ`→「捏」/「握」
      2. **孤立、无句法功能、音频本身就是效果声**（如孤立的亲吻声 `キュー`）
         → **才**允许保留中文拟声「啾」。**这是例外，不是默认**
      3. **无法可靠判断且不影响理解** → 不显示（并入语气）
    ⚠️ `もぅ…`/`うぇぁ…`/`んん…` 这类气声假名串只译「嗯／唔啊」等语气词，
       **不借字面义**（`もぅ` 是 `もう`＝已经 的变体，但气声串里**不是**「已经」）。
    ⚠️ `なら` / `ぎゅ` 这类被 ASR 从非语音里编出来的词，**不要**按字面译成
       「如果」/「捏紧」——它们多半不是真台词（见 `asr_suspect` 检测器）。
17. 记住开头那条：**省略的主语/宾语必须补出来**，别输出「不了解家里的情况」这种
    不知道是谁的残句。
17. **译出来出现上下文完全不合逻辑的具体名词，先怀疑 ASR 听错**：
    房间对话里冒出「大象」几乎一定是把「そう」听成了「象」；「男子近世」其实是「男子禁制」。
    回头看原文近音词再译，别照着错字硬翻。
18. **翻译腔五病——看到就改**：
    ·「关于／对于」综合症：✗「关于那件事，我很抱歉」✓「那件事，真的很抱歉」
    ·「被」字滥用：✗「他被大家所讨厌着」✓「大家都讨厌他」
    · 「进行／做出／给予」等虚假动词：✗「作出了拒绝」「进行说明」✓「拒绝了」「解释清楚」
    · 无意义的复数「们」：✗「学生们都看向了我」✓「全班都看向了我」
    · 过长的定语「的」链：✗「带着看到什么难以置信的东西的表情的她…」
      ✓「她神情错愕，像是看到了什么不可思议的东西」
    动作要干脆、少抽象名词（✗「他对我进行了视线的投射」✓「他看向我」）。
19. **节制语气词**：不要满篇「呢、啦、哦、呀、哟」，语气优先靠标点和副词体现。
    也不要把「ええと」「あの」机械译成「那个」「怎么说呢」，按语境化为「呃……」「我说……」或留白。"""

NUM = r"^\s*(\d+)\s*(?:[|｜]|[:：]|[.．、])\s*(.+)$"
# 只认竖线的严格版，用于「剥重复编号」（不会误伤「3.5 倍」这类译文）
NUM_STRICT = r"^\s*(\d+)\s*[|｜]\s*(.+)$"
# 拒译检测：只认**明确的拒绝短语**。
# ⚠️ 绝对不要把裸的「抱歉」放进来——对话译文里「抱歉」「对不起」太常见，
# 会把正常译文整行误杀（实测「あ〜もうごめんごめん」的译文「啊～真是抱歉抱歉。」
# 就是这样被丢掉的，全量一次丢 4 行）。
REFUSE = ("无法翻译", "不能翻译", "作为一个AI", "作为AI", "无法提供", "无法完成",
          "无法处理", "无法协助", "不能协助", "我不能帮你")


def build_lines(segments):
    """字幕行切分——**必须**与 polish.py 用同一套实现。

    曾经两边各写一份，翻译按旧版切成 48 行、润色按新版切成 29 行，
    结果初译对不上（日志显示「已匹配初译 26/29」），那几行等于丢掉参考重翻。
    现在的唯一真相在 polish.py 里（含跨 segment 合并的关键逻辑）。

    `KIKOERU_CUE_PIPELINE=1` 时改走**语义 cue**（cues.py）：翻译单位从「显示行」
    提升为「语义 cue」，显示行只在润色后由中文切出——这是消除跨行翻译泄漏的关键。
    """
    import cues
    if cues.ENABLED:
        return cues.build_semantic_cues(cues.flatten_for_cues(segments))
    from polish import build_lines as _bl
    lines = _bl(segments)
    if lines and "start" not in lines[0]:
        return lines
    return [{"start": l["start"], "end": l["end"], "text": l["text"]} for l in lines]


def call_llm(messages, temperature=0.3, max_tokens=1400, top_p=0.9, top_k=None):
    from polish import strip_think
    payload = {"model": "qwen", "messages": messages, "temperature": temperature,
               "max_tokens": max_tokens, "top_p": top_p, "repetition_penalty": 1.05,
               "chat_template_kwargs": {"enable_thinking": False}}
    if top_k is not None:
        payload["top_k"] = top_k
    req = urllib.request.Request(API, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    for k in range(3):
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                body = json.loads(r.read().decode("utf-8"))
                # 剥掉推理模型的 thinking 块（Qwen3 默认会输出；正常已由
                # enable_thinking:false 关掉，这里是兜底，见 polish.strip_think）
                return strip_think(body["choices"][0]["message"]["content"])
        except Exception:
            if k == 2:
                raise
            time.sleep(2 * (k + 1))
    return ""


def _clean_cell(s):
    """剥掉单元格里**重复出现**的「编号|」前缀。

    Qwen3-8B 实测会输出「1|1|译文」——编号写了两遍，NUM 只吃一层，
    残留的「1|」就进了成品（表现为整块字幕变成 1、2、3…，见 2026-10-07 记录）。
    这里只认竖线分隔，避免误伤「3.5 倍」这类以数字开头的正常译文。
    """
    s = s.strip()
    for _ in range(3):
        m = re.match(NUM_STRICT, s)
        if not m:
            break
        s = m.group(2).strip()
    return s


def parse_reply(reply, n):
    out = {}
    for line in reply.splitlines():
        line = line.strip().strip("`").strip()
        if not line:
            continue
        m = re.match(NUM, line)
        if m:
            idx = int(m.group(1))
            if 1 <= idx <= n and idx not in out:
                out[idx] = _clean_cell(m.group(2))
    if len(out) == n:
        return [out[i] for i in range(1, n + 1)]
    rows = []
    for line in reply.splitlines():
        line = line.strip().strip("`").strip()
        if not line:
            continue
        rows.append(_clean_cell(re.sub(NUM, r"\2", line)))
    rows = [r for r in rows if r]
    return rows if len(rows) == n else None


def _parse_plain(reply, n):
    """Sakura 模式：回复就是 N 行纯译文，没有「编号|」前缀。

    它的原生格式不带编号，行数偶尔会差一行（把两个短句并成一行之类），
    所以调用方要能容忍失败并重试——见 translate_block 的 sakura 分支。
    """
    from polish import strip_think
    rows = [l.strip() for l in strip_think(reply).splitlines()]
    rows = [l for l in rows if l]
    return rows if len(rows) == n else None


def _has_kana(s):
    from polish import JPN
    return bool(JPN.search(s))


# 初译残留检测（2026-10-28 R153）。HY-MT 分支原来只查「非空 + 无拒答词」，
# 所以 Qwen 被当成 HY-MT 用时吐的英文/假名一路进了成品（全库 115 处 / 16 部）。
# 这里只做**降级重试 + 记账**，不做硬失败——硬失败会让整轨卡死。
LATIN_OK = {"AV", "JK", "SM", "VIP", "OK", "NG", "BGM", "SE", "CM", "DVD", "ID",
            "HP", "TV", "OL", "BBA", "NTR", "POV", "ASMR", "cosplay", "Cosplay",
            # 2026-10-28 R154：Sakura 把「アプリ」译成「APP」是正常译法，
            # 白名单漏了 APP → 「睡眠APP……？」整句被判拉丁残留、兜底成「…」。
            # 同类高频借词一起补上，别等下次再踩。
            "APP", "App", "SNS", "LINE", "PC", "USB", "IC", "QR", "AI", "HP",
            "JK", "DK", "OL", "AV", "MV", "PV", "CG", "RPG", "NTR", "BBA"}
_LATIN = re.compile(r"[A-Za-z]{3,}")
RESIDUE = {"latin": 0, "kana": 0, "retried": 0}


def _residue(s):
    """初译里不该出现的字：成串英文 / 假名。返回原因，干净返回 ""。"""
    for w in _LATIN.findall(s or ""):
        if w.upper() not in LATIN_OK and w not in LATIN_OK:
            return "latin:" + w
    if s and _has_kana(s):
        return "kana"
    return ""


# ---------------------------------------------------------------- 方案 A
# Sakura 幻觉兜底（2026-10-28 R154）。
#
# 为什么必须有：Sakura 是「整段文本」微调出来的，遇到**信息熵极低**的输入
# （纯喘息/拟声，如 `んっ…はぁ…ちゅっ…`）会触发基座幻觉 —— 实测三种：
#   ① 整句吐英文：`哈啊…好痛啊哈啊… Huh,Ah,no, sorry!`
#   ② 复读退化：同一分句吐两遍（`如果你赢了…如果你赢了…`）
#   ③ 超长注水：译文长度远超日文（`2.5×` 以上基本是脑补）
# 而且这些**过不了 A8 遮蔽**：`--cleanup-only` 下 mask 被跳过，垃圾原样进成品
# （轨 04 实测 9 处拉丁里 5 处是这种，`Holmes Meanwhile in came off` 都进了 LRC）。
#
# 处理分两路（对应两种病因）：
#   · 日文侧是**非词汇音**（全假名/拖音）→ 这一行本来就该是「…」，直接给兜底文本，
#     不再问模型（问了也是猜）；
#   · 日文侧有实词 → 单行重问一次（温度降到 0），还坏就兜底 + 记 review。
SAKURA_MAX_RATIO = 2.5     # 译文字符数 / 日文字符数 的上限
SAKURA_LOOP = 4            # 同一 3 字片段连续出现次数上限
SAKURA_FALLBACK = "…"
# 短行阈值：日文去掉标点后 < 这么多字，就单独成块翻（防下一行开头渗进来）
SHORT_JA = 8
HALLU = {"latin": 0, "loop": 0, "ratio": 0, "masked": 0, "retried": 0, "left": 0}
_NONLEX = re.compile(r"^[\u3040-\u30ff\s、。，,.!?！？…‥~～ー〜っ]+$")
# ⚠️ 必须含 em dash / en dash / 连字符：`啊—啊—啊—…` 复读时剥不掉破折号，
#    `(.)\1{4,}` 就匹配不到，退化漏判（R154 实测 1 处 long 就是这么漏的）。
_STRIP = re.compile(r"[\s、。，,.!?！？…‥~～ー〜—–\-·]")
# 日文侧去标点**不能剥「ー」**：`コーラ` 剥成 `コラ` 就只剩 2 字，会被「超短促」误判成喘息。
_STRIP_JA = re.compile(r"[\s、。，,.!?！？…‥~～]")
_SRC_LATIN = re.compile(r"[A-Za-z]{3,}")
# 语气/拟声字：只有这些字连续重复才算「复读退化」
FILLER_CHARS = set("嗯啊哈呜唔呼噢哦呃唉呀哎咦诶嘿嘛呐")

# ---- 「这句是不是非词汇音」的三层机械漏斗（2026-10-28 R154）----
# 不用模型（8GB 显存要留给 LLM），纯字符串判断。
# 第一层「全假名无汉字」太粗：`いいよ`/`だめ`/`やめて`/`すき` 都是全假名实词，
# 一律遮蔽会丢剧情（尤其高潮前后的短句）。所以加第二层白名单护栏 + 第三层拟声特征。
LEXICAL_KANA = (
    # 交互 / 否定肯定 / 疑问
    "いい", "だめ", "ダメ", "やだ", "イヤ", "いや", "やめ", "まて", "まって",
    "なに", "なん", "どこ", "ここ", "そこ", "だれ", "いつ", "どう",
    "ほんと", "うそ", "マジ", "まじ", "すごい", "すご", "やば",
    # 接续 / 语气副词：全假名，去重率也低（`だけど` 只有 3 种假名），
    # 不拦就会被「去重率」那条误判成喘息。实测第一版就是这么错的。
    "だけど", "だから", "でも", "けど", "そして", "それで", "それに", "それじゃ",
    "じゃあ", "では", "もう", "まだ", "また", "だって", "なんか", "なんて",
    "やっぱ", "そう", "どうも", "ください", "でしょう", "ですか", "ますか",
    "のよ", "のね", "よね", "かな", "かしら", "けれど", "から", "まで",
    "はい", "うん", "いえ", "ええ", "すみません", "ごめん", "ありがと", "おねがい",
    # 动词 / 状态 / 人称
    "すき", "きらい", "いく", "イク", "いっく", "でる", "でちゃう", "みる", "みて",
    "きいて", "しってる", "わかる", "ちょうだい", "もっと", "ずっと", "ちょっと",
    "ちゃん", "くん", "せんせい", "わたし", "あたし", "きみ", "おまえ", "あんた",
    # 这批语料的高频实词
    "ちんぽ", "ちんこ", "まんこ", "おっぱい", "しご", "しこ", "ピース",
)
# 拟声/气声的高频假名：喘息、舔耳、水声、吞咽
NONLEX_CHARS = set(
    "あいうえおんはひふへほっーぁぃぅぇぉゎ"          # 气声
    "ちゅぱれろぷぴじゅずぐごく"                        # 水声/舔耳
    "アイウエオンハヒフヘホッァィゥェォ"                  # 片假名对应
    "チュパレロプピジュズグゴク"
)


def _src_latin(ja):
    """源语里有没有**非白名单**的拉丁串（Whisper 幻觉）。

    ⚠️ 必须排除白名单：`ここがVIPルーム` 里的 VIP 是正常借词，
    第一版直接查 `[A-Za-z]{3,}` 把它当成了源语污染，整句被兜底成「…」。
    """
    for w in _SRC_LATIN.findall(ja or ""):
        if w.upper() not in LATIN_OK:
            return True
    return False


def _clean_ja(ja):
    """摘掉源语里的**非白名单**拉丁词，返回 (清理后日文, 是否还有实质内容)。

    为什么不能整句兜底：`何だったの ima no…`（ASR 把「今」拼成了罗马字）
    整句会变成「…」，把「刚才那是什么」这句真台词一起丢了。
    摘掉 `ima` 之后还剩「何だったの」，可以正常译。
    """
    t = re.sub(r"[A-Za-z]+",
               lambda m: "" if m.group(0).upper() not in LATIN_OK else m.group(0),
               ja or "")
    # 摘掉英文后常剩一串标点（`はぁ… Huh, Ah, no, sorry!` → `はぁ… , , , !`）
    t = re.sub(r"[、,，]\s*(?=[、,，！？!?…])", "", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t, bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", t))


def _ja_dup(ja):
    """源语自己就在重复（`もう一回もう一回`、`待って待って`）。

    这种句子的译文重复是**对的**，不能判复读退化——实测「等等等等」被判成退化、
    兜底成「…」，把整句台词丢了。
    """
    s = _STRIP_JA.sub("", ja or "")
    if len(s) < 4:
        return False
    for L in (2, 3, 4):
        for i in range(len(s) - L):
            if s.count(s[i:i + L]) >= 2 and L >= 2:
                return True
    return False


def _nonlexical(ja):
    """日文侧是不是「非词汇音」：三层漏斗，任何一层判实词就返回 False。

    1. 含汉字/数字/拉丁 → 实词
    2. 命中全假名实词白名单 → 实词（`いいよ`/`やめて`/`すき` 这些不能遮蔽）
    3. 剩下的看拟声特征：超短促（≤2 字）/ 去重率极低（≤3 种假名）/ 拟声字符集占比 ≥80%
    """
    t = _STRIP_JA.sub("", ja or "")
    if not t:
        return False
    if re.search(r"[\u4e00-\u9fff0-9A-Za-z]", t):
        return False
    if any(w in t for w in LEXICAL_KANA):
        return False
    # 超短促：要求**每个字**都是拟声字。`ほら`/`って`/`コラ` 这类短实词
    # 只要有一个字不在拟声集里就放过（第一版只看长度，把它们全误杀了）。
    if len(t) <= 2:
        return all(ch in NONLEX_CHARS for ch in t)
    # 去重率极低：要求「够长 + 只用 2 种假名」。
    # ⚠️ 第一版写成 `len(set)<=3` 且不限长度，把 `だけど`（だ/け/ど 三种）判成了喘息。
    if len(t) >= 5 and len(set(t)) <= 2:
        return True
    hit = sum(1 for ch in t if ch in NONLEX_CHARS)
    return hit / len(t) >= 0.8


def _rep_level(s):
    """「重复得多厉害」：最大重复次数（单字连排按长度、6 字片段按出现次数）。

    ⚠️ 为什么不能用「译文长度 ≥10 字」判放大（2026-10-29）：
    `なんで、なんで、なんで、なんで。` → 「为什么、为什么、为什么、为什么。」
    是 **4:4 的忠实重复**，但剥完标点有 12 字，旧规则一律判「放大」→ 重问 → 还是重复
    → 兜底成「…」，把整句台词丢了。实测 RJ1530549/1065777/1234441 共 40 条 loop，
    绝大多数是这种「原文自己重复、译文等量重复」的忠实翻译。
    改成**比较次数**：中文重复次数 > 日文 × 2 + 1 才算真的放大。
    """
    s = _STRIP.sub("", s or "")
    best = 1
    if len(s) < 4:
        return best
    for ch in set(s):
        m = re.search(re.escape(ch) + r"{3,}", s)
        if m:
            best = max(best, len(m.group(0)))
    # ⚠️ 必须也数 2/3 字的短单元：`あぁ×4`、`よし×4`、`んで×4` 都不是单字连排，
    # 只数单字连排 + 6 字片段会漏（实测 `あぁ、あぁ、あぁ、ダメー` 就是这样漏的）。
    for L in (2, 3, 4, 6):
        for i in range(len(s) - L + 1):
            c = s.count(s[i:i + L])
            if c > best:
                best = c
    return best


def _ja_is_sentence(ja):
    """日文是不是「一句话」（而不是「一段声音」）：长度 ≥24 且汉字 ≥4。

    呻吟/拟声不会又长又带这么多汉字。这种句子**就算译文重复也是忠实的**——
    原文里本来就有一串拟声：`ぬちゃぬちゃ、しこしこ、ぐちゅぐちゅ、しこしこ`。
    实测 RJ1228193 轨01 19:01.70 一句 80 字的射精描写，被 loop 判据整句吞成「…」。
    ⚠️ 只用于 loop 判据，**不能**拿去放宽 nonlex（长呻吟照样要压「…」）。
    """
    s = _STRIP_JA.sub("", ja or "")
    return len(s) >= 24 and len(re.findall(r"[\u4e00-\u9fff]", s)) >= 4


def _loop4(zh):
    """复读退化：**语气/拟声字**连续 ≥5 次，或 6 字以上片段出现两遍。

    为什么只认语气字：`不行不行不行不行不行`（ダメダメダメ）是**合法**的强调，
    换成「…」反而丢信息；而 `嗯嗯嗯嗯嗯`（んっ…はぁ…）是退化。两者字面都是
    「同一片段重复」，只能靠字本身区分。
    第二种是实测最坑的：`如果你赢了，我就会对你言听计从，如果你赢了，…`
    —— 读起来像人话，比例检测（2.0 < 2.5）也抓不到，但整句是错的。
    """
    s = _STRIP.sub("", zh or "")
    if len(s) < 5:
        return False
    for ch in set(s):
        if ch in FILLER_CHARS and (ch * 5) in s:
            return True
    for i in range(len(s) - 5):
        frag = s[i:i + 6]
        if s.count(frag) >= 2:
            return True
    return False


def _symloop(zh):
    """单个符号/标点连续重复 ≥10 次（`～～ ～～ ～～ …` × 60）。

    ⚠️ `_loop4` 抓不到这种：它的 `_STRIP` 会把 `～`/`—` 剥掉，剥完是空串
    （`len(s) < 5` 直接返回 False）。实测 RJ1623028 Track05 有一行吐了 60 组波浪号。
    """
    return bool(re.search(r"([～~—–…・])\1{9,}", zh or ""))


def _sakura_bad(zh, ja):
    """返回 (原因, 是否非词汇音)。原因空 = 干净。"""
    # 逐字母退化：`K，O，S，h，h，a，n，d…`（Sakura 遇到罗马音垃圾时会把字母一个个吐出来）。
    # ⚠️ 这种单字母不成 `[A-Za-z]{3,}`，_residue 抓不到，必须单独判。
    if re.search(r"(?:[A-Za-z]，){4,}", zh or ""):
        return "latin:letters", _nonlexical(ja)
    why = _residue(zh)
    if why:
        # 混译不算残留：`我这期吃的是薄荷口味的pino。`——内容全译出来了，只是专有名词
        # （ピノ / プレイ / ハッピー / 天神ドロップ）用了拉丁。整行兜底成「…」比留着
        # "pino" 糟得多：实测 RJ1234441 的 Free Talk 有 6 句 20~40 字的生活闲聊
        # 就这么整句蒸发了（`ラブラブな作品もまた作りたいし…` 那句连品牌名一起没了）。
        # ⚠️ 不能因此放宽 R153 的英文残留保护：那种是**整行英文**，中文字数远不到 6，
        # 下面的 nolang 分支照样抓得住。
        if why.startswith("latin:") and \
                len(re.findall(r"[\u4e00-\u9fff]", zh or "")) >= 6:
            return "", False
        return why, _nonlexical(ja)
    # 整行没有一个中日文字（只剩标点/空格）→ 这是模型没产出内容，不是译文
    if zh and not re.search(r"[\u4e00-\u9fff\u3040-\u30ff]", zh):
        # 例外：源语本身就是**片假名外来词**（`オッケー。`），Sakura 译成 `OK。` 是对的，
        # 不能因为「译文里没有中日文字」就判 nolang。实测 RJ1234441 轨02
        # 把 `オッケー。` 兜底成「…」，虽然后面两句译出来了，但开头被吞。
        if re.fullmatch(r"[A-Za-z]{1,6}",
                        _STRIP.sub("", zh or "")) and \
                re.fullmatch(r"[\u30a0-\u30ff]{1,12}", _STRIP_JA.sub("", ja or "")):
            return "", False
        return "nolang", _nonlexical(ja)
    jn = len(_STRIP.sub("", ja or ""))
    zn = len(_STRIP.sub("", zh or ""))
    if jn >= 5 and zn > SAKURA_MAX_RATIO * jn:
        return "ratio:%d/%d" % (zn, jn), _nonlexical(ja)
    if _symloop(zh):
        return "symloop", _nonlexical(ja)
    # 源语自己就在重复（`もう一回もう一回`）→ 译文重复是对的，不判
    if _loop4(zh):
        # 拟声 → 拟声：源语本来就是纯气声（`あ゛あ゛あ゛…`），译文也是纯语气字
        # （`啊啊啊啊…`）。这**不是**忠实翻译，字幕里应该压成「…」。
        # ⚠️ 这条不能靠比重复次数：`あ゛×11` → 「啊×22」算「等量」（22 ≤ 11×2+1），
        # 按次数比会放过它，结果满屏「啊啊啊啊」。
        if _nonlexical(ja):
            zs = _STRIP.sub("", zh or "")
            if zs and all(ch in FILLER_CHARS for ch in zs):
                return "loop", True
        # 原文自己重复时，译文重复是**对的**（`待って待って`→「等等等等」）。
        # 但**放大**就不对了：`ジャメそしてそしてそして`（3 次）被译成
        # `啊—啊—啊—…`（14 次）。
        # 判「放大」必须比**次数**，不能比字数：`なんで×4`→「为什么×4」剥完标点 12 字，
        # 按「≥10 字」判会把忠实重复全吞掉（2026-10-29 修）。
        # 整句级保护：长句（≥24 字且 ≥4 汉字）永不判 loop——那种句子里的重复是原文带来的。
        if not _ja_is_sentence(ja) and \
                (not _ja_dup(ja) or _rep_level(zh) > _rep_level(ja) * 2 + 1):
            return "loop", _nonlexical(ja)
    return "", False


def _sakura_retry_one(ja):
    """单行重问（温度 0）。返回 (译文, 是否干净)。"""
    from polish import strip_think   # 与 hymt 分支同样按需导入（polish 是重模块）
    HALLU["retried"] += 1
    try:
        reply = call_llm([{"role": "system", "content": SAKURA_SYSTEM},
                          {"role": "user", "content": "将下面的日文文本翻译成中文：\n" + ja}],
                         temperature=0.0, top_p=SAKURA_TOP_P, max_tokens=200)
    except Exception:
        return "", False
    rows = [x.strip() for x in strip_think(reply).splitlines() if x.strip()]
    if not rows:
        return "", False
    cand = _clean_cell(rows[0])
    why, _nl = _sakura_bad(cand, ja)
    return cand, (not why)


def _split_short(block):
    """把「很短的句子」从块里拆出来，各自单独成块。

    为什么：Sakura 是**纯翻译模型**，block 里给 N 行、不带编号，
    短行（`おやすみなさい`、`ん?`）的边界它守不住，会把下一行的开头渗进来。
    实测 RJ1058739：`295.56 おやすみなさい` 被译成「晚安。不行」——
    「不行」来自下一段 `ダメ、先生の見回りかも`；625 行里 2 处。

    只拆短的，长的仍成块（保住上下文，不然调用次数暴涨）。
    """
    out, cur = [], []
    for item in block:
        ja = item[1] or ""
        if len(_STRIP_JA.sub("", ja)) < SHORT_JA:
            if cur:
                out.append(cur)
                cur = []
            out.append([item])
        else:
            cur.append(item)
    if cur:
        out.append(cur)
    return out


def translate_block(block, prev_ctx):
    """prev_ctx: 前几句的「日：… 译：…」，用来保持人称/称呼/口吻连贯。"""
    # 短行隔离（见 _split_short）：只在混排时才拆，全短或全长都不动
    if len(block) > 1 and ENGINE == "sakura":
        parts = _split_short(block)
        if len(parts) > 1:
            merged, last = [], ""
            for part in parts:
                r, rep = translate_block(part, prev_ctx)
                if r is None:
                    return None, rep
                merged.extend(r)
                last = rep or last
            return merged, last
    if ENGINE == "hymt":
        # HY-MT 1.5：翻译专用模型，**逐句**翻（它不做编号列表，也不吃系统提示里的风格规则）。
        # 这里只做「初译」——风格/主语补全交给后面的 polish（Qwen + 10.6 万句对风格库）。
        from polish import strip_think
        res = []
        for _, t in block:
            _hint = _hymt_hint(t)
            _prompt = (HYMT_HINT_TEMPLATE % (_hint, t)) if _hint else (HYMT_TEMPLATE % t)
            cand = ""
            for attempt in range(3):
                # 第一次用官方参数；出现英文/假名残留就降温重抽（残留多半是随机漂移）
                temp = HYMT_TEMP if attempt == 0 else 0.2
                reply = call_llm([{"role": "user", "content": _prompt}],
                                 temperature=temp, top_p=HYMT_TOP_P, top_k=HYMT_TOP_K,
                                 max_tokens=300)
                rows = [x.strip() for x in strip_think(reply).splitlines() if x.strip()]
                cand = _clean_cell(rows[0]) if rows else ""
                if cand and not _residue(cand):
                    break
                if cand:
                    RESIDUE["retried"] += 1
            why = _residue(cand)
            if why:
                RESIDUE["latin" if why.startswith("latin") else "kana"] += 1
                print("    ⚠️ 初译残留(%s)：%s -> %s" % (why, t[:26], cand[:34]), flush=True)
            res.append(cand)
        if all(res) and not any(any(x in t for x in REFUSE) for t in res):
            return res, ""
        return None, ""

    if ENGINE == "sakura":
        # Sakura 的原生格式：user 直接给「将下面的日文文本翻译成中文：」+ N 行原文，
        # 它返回 N 行译文。官方推荐 temperature 0.1 / top_p 0.3。
        # 行数对不上就重试（最多 3 次）；返回里有假名（没译干净）也重试。
        body = "\n".join(t for _, t in block)
        user = "将下面的日文文本翻译成中文：\n" + body
        reply = ""
        for _ in range(3):
            reply = call_llm([{"role": "system", "content": SAKURA_SYSTEM},
                              {"role": "user", "content": user}],
                             temperature=SAKURA_TEMP, top_p=SAKURA_TOP_P,
                             max_tokens=160 * len(block) + 200)
            res = _parse_plain(reply, len(block))
            if (res and not any(any(x in t for x in REFUSE) for t in res)
                    and not any(_has_kana(t) for t in res)):
                # 方案 A：逐行兜底（见 _sakura_bad 的注释）
                out = []
                for (_i, ja), zh in zip(block, res):
                    # 源语本身就是垃圾（Whisper 在非语音段幻觉出英文，如
                    # `はぁ…ヒリヒリはぁ… Huh, Ah, no, sorry!`）——这时候**重问没用**，
                    # 因为模型翻译得没错，是输入错了。直接给拟声兜底。
                    # 实测轨 04：7 处含拉丁的 ASR 行里 5 处是这种。
                    if _src_latin(ja):
                        HALLU["src"] = HALLU.get("src", 0) + 1
                        # 摘掉拉丁词后如果还剩实词（`何だったの ima no…`），
                        # 就用清理后的日文重译；什么都不剩（`Okay, now I'm…`）才兜底。
                        cja, has = _clean_ja(ja)
                        if not has:
                            HALLU["masked"] += 1
                            out.append(SAKURA_FALLBACK)
                            continue
                        cand, ok = _sakura_retry_one(cja)
                        if ok:
                            out.append(cand)
                        else:
                            HALLU["left"] += 1
                            HALLU["masked"] += 1
                            out.append(SAKURA_FALLBACK)
                        continue
                    why, nonlex = _sakura_bad(zh, ja)
                    if not why:
                        out.append(zh)
                        continue
                    kind = why.split(":")[0]
                    HALLU[kind] = HALLU.get(kind, 0) + 1
                    if nonlex:
                        # 纯气声/拟声：本来就该是「…」，不再问模型
                        HALLU["masked"] += 1
                        out.append(SAKURA_FALLBACK)
                        continue
                    cand, ok = _sakura_retry_one(ja)
                    if ok:
                        out.append(cand)
                    else:
                        HALLU["left"] += 1
                        print("    ⚠️ Sakura 幻觉(%s)未救回：%s -> %s"
                              % (why, ja[:22], (cand or zh)[:30]), flush=True)
                        out.append(SAKURA_FALLBACK)
                return out, reply
        return None, reply

    body = "\n".join("%d|%s" % (i, t) for i, t in block)
    user = "【待翻译】共 %d 句：\n%s\n\n【输出】%d 行，格式 编号|译文：" % (len(block), body, len(block))
    if prev_ctx:
        user = ("【前文】（已译好的前几句，仅供保持语境与口吻连贯，不要重复翻译、不要输出）\n"
                + "\n".join(prev_ctx) + "\n\n") + user
    reply = call_llm([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
    res = parse_reply(reply, len(block))
    if res and not any(any(x in t for x in REFUSE) for t in res):
        return res, reply
    return None, reply


def fmt_lrc(sec):
    m, s = divmod(max(0.0, sec), 60)
    return "[%02d:%05.2f]" % (int(m), s)


def emit_lines(lines, results):
    """成稿 → [(start, zh)] + 被兜底的行号列表。

    ⚠️ **空译文必须写占位，不能跳过**（2026-10-30 修）。
    旧写法 `if not zh: continue` 会让整行不写 → 时间轴少一行。上游是
    「块解析失败 → 逐句兜底也返回空」，日志只写「136 行 → 已写出 135 行」，
    看不出是哪一句没了；06 的 empty 门禁能抓到，但只能靠 fill_timeline 手工补「…」。
    实测 RJ1530549 轨07 `04:44.10 リリアも気持ちいいよ` 就这样蒸发。

    抽成独立函数是为了能写回归测试（`test_regressions.py` 4b 组）。
    """
    rows, dropped = [], []
    for ln, zh in zip(lines, results):
        zh = re.sub(r"\s+", " ", (zh or "")).strip()
        if not zh:
            dropped.append(ln["start"])
            zh = SAKURA_FALLBACK
        rows.append((ln["start"], zh))
    return rows, dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("asr_dir")
    ap.add_argument("out_dir")
    ap.add_argument("key", nargs="?", default="")
    a = ap.parse_args()

    os.makedirs(a.out_dir, exist_ok=True)
    files = sorted(glob.glob(os.path.join(a.asr_dir, "*.json")))
    if a.key:
        files = [f for f in files if a.key in os.path.basename(f)]
    if not files:
        print("没有待处理文件")
        return
    try:
        urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=10).read()
    except Exception as e:
        print("llama-server 未就绪: %s\n先运行 serve.py start" % e)
        return

    for jf in files:
        d = json.load(open(jf, encoding="utf-8"))
        src = d["source"]
        lines = build_lines(d["segments"])
        n = len(lines)
        blocks = [list(range(i, min(i + CHUNK, n))) for i in range(0, n, CHUNK)]
        results = [None] * n
        print("\n=== %s : %d 行 ===" % (src[:50], n), flush=True)

        def prev_ctx(i):
            out = []
            for j in range(max(0, i - CTX_KEEP), i):
                if results[j]:
                    out.append("日：%s\n译：%s" % (lines[j]["text"], results[j]))
            return out

        t0 = time.time()
        # 按波次推进：波内并行、波间串行，让后一波拿到前一波的译文做上下文
        for w0 in range(0, len(blocks), WORKERS):
            wb = list(range(w0, min(w0 + WORKERS, len(blocks))))
            jobs = []
            for bi in wb:
                idxs = blocks[bi]
                blk = [(k + 1, lines[i]["text"]) for k, i in enumerate(idxs)]
                jobs.append((bi, blk, prev_ctx(idxs[0])))

            def work(job):
                bi, blk, ctx = job
                return bi, translate_block(blk, ctx)

            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                for bi, (res, raw) in ex.map(work, jobs):
                    idxs = blocks[bi]
                    if res is None:
                        print("    块 %d 解析失败，转逐句" % (bi + 1), flush=True)
                        res = [None] * len(idxs)
                    for k, i in enumerate(idxs):
                        results[i] = res[k]
        bad = [i for i, v in enumerate(results) if v is None]
        for i in bad:
            try:
                r, _ = translate_block([(1, lines[i]["text"])], prev_ctx(i))
                results[i] = r[0] if r else ""
            except Exception:
                results[i] = ""
        print("    翻译完成 %.1f 秒（逐句重试 %d）" % (time.time() - t0, len(bad)), flush=True)
        if RESIDUE["retried"]:
            print("    残留重抽 %d 次；仍残留 英文 %d / 假名 %d"
                  % (RESIDUE["retried"], RESIDUE["latin"], RESIDUE["kana"]), flush=True)
        if any(HALLU.values()):
            print("    Sakura 幻觉兜底：拉丁 %d / 复读 %d / 注水 %d → 拟声段直接给「…」%d，"
                  "重问救回 %d，未救回 %d"
                  % (HALLU["latin"], HALLU["loop"], HALLU["ratio"],
                     HALLU["masked"], HALLU["retried"] - HALLU["left"], HALLU["left"]),
                  flush=True)

        out = os.path.join(a.out_dir, src + ".lrc")
        rows, dropped = emit_lines(lines, results)
        with open(out, "w", encoding="utf-8") as f:
            f.write("[ti:%s]\n[by:%s]\n\n" % (src, MODEL_TAG))
            for t, zh in rows:
                f.write("%s%s\n" % (fmt_lrc(t), zh))
        if dropped:
            print("    ⚠️ 空译文 %d 行，已写「%s」占位（时间轴保住，内容待补）：%s"
                  % (len(dropped), SAKURA_FALLBACK,
                     ", ".join("%.2f" % t for t in dropped[:8])
                     + ("…" if len(dropped) > 8 else "")), flush=True)
        print("    已写出 %d 行 -> %s" % (len(rows), out), flush=True)

    print("\n全部完成 ->", a.out_dir)


if __name__ == "__main__":
    main()
