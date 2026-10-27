# -*- coding: utf-8 -*-
r"""
阶段二：日文 -> 简体中文 机器初译（本地 Qwen2.5-7B / llama-server）。

用法：
  python translate.py <ASR目录> <初译输出目录> [文件名关键字]

例：
  python translate.py "./asr_out" "$KIKOERU_ROOT/.workbuddy/draft/RJ9000005"

产出：<初译输出目录>\<音频文件名>.lrc（后面交给 polish.py 润色）
"""
import os
import sys
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

CHUNK = 10
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


def translate_block(block, prev_ctx):
    """prev_ctx: 前几句的「日：… 译：…」，用来保持人称/称呼/口吻连贯。"""
    if ENGINE == "hymt":
        # HY-MT 1.5：翻译专用模型，**逐句**翻（它不做编号列表，也不吃系统提示里的风格规则）。
        # 这里只做「初译」——风格/主语补全交给后面的 polish（Qwen + 10.6 万句对风格库）。
        from polish import strip_think
        res = []
        for _, t in block:
            _hint = _hymt_hint(t)
            _prompt = (HYMT_HINT_TEMPLATE % (_hint, t)) if _hint else (HYMT_TEMPLATE % t)
            reply = call_llm([{"role": "user", "content": _prompt}],
                             temperature=HYMT_TEMP, top_p=HYMT_TOP_P, top_k=HYMT_TOP_K,
                             max_tokens=300)
            rows = [x.strip() for x in strip_think(reply).splitlines() if x.strip()]
            res.append(_clean_cell(rows[0]) if rows else "")
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
                return res, reply
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

        out = os.path.join(a.out_dir, src + ".lrc")
        with open(out, "w", encoding="utf-8") as f:
            f.write("[ti:%s]\n[by:%s]\n\n" % (src, MODEL_TAG))
            keep = 0
            for ln, zh in zip(lines, results):
                zh = re.sub(r"\s+", " ", (zh or "")).strip()
                if not zh:
                    continue
                f.write("%s%s\n" % (fmt_lrc(ln["start"]), zh))
                keep += 1
        print("    已写出 %d 行 -> %s" % (keep, out), flush=True)

    print("\n全部完成 ->", a.out_dir)


if __name__ == "__main__":
    main()
