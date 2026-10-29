# -*- coding: utf-8 -*-
r"""硬回归测试：把已人工确认的「跨行翻译泄漏」固化成**必须全通过**的检查。

为什么需要它（GPT 第三轮建议）：
  我们正在改「翻译单位」（父 cue 唯一翻译 + 中文切分）。这类改造最容易悄悄引入
  「相邻行重复/泄漏」。所以把**已确认的失败样本**冻成测试，改造后必须零回归。

用法：
  python test_regressions.py            # 跑全部检查，退出码 0=全过
  python test_regressions.py -v         # 打印逐条判定

三道检查：
  1. `detector`   8 个已确认泄漏样本，检测器**必须**全部命中（现在就应通过）
  2. `regressed`  这 8 处**不得在成品里重现**（cue 翻译改造的目标）
  3. `warnings`   全量泄漏告警数（**信息项**，不是门禁——含大量真重复）
"""
import os
import json
import re
import sys
sys.stdout.reconfigure(encoding="utf-8")
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan_violations import (leak_rules, norm, omit_ratio, onomat_translit,  # noqa: E402
                             cue_too_long, moan_drift, ono_guess, zh_collapse,  # noqa: E402
                             is_moan_ja, OMIT_RATIO, person_flip, number_mismatch,
                             neg_possible_exempt,
                             omit_group_ratio, omit_clause_gap, find_dup_blocks,
                             is_filler_only, kekkon_confusion, group_text,
                             RE_Q_JA, RE_NEG_JA, Q_JA_STRIP,
                             NEG_JA_STRIP, NEG_JA_RESTRIP, Q_JA_RESTRIP)

ROOT = os.environ.get("KIKOERU_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 可用 KIKOERU_LRC_ROOT 指向别的成品目录（例如 cue 管线的试点输出）
OUT_ROOT = os.environ.get("KIKOERU_LRC_ROOT", os.path.join(ROOT, "kikoerusubtitle"))
ASR_ROOT = os.path.join(ROOT, "data/draft")

# —— 数据依赖用例（PILOT_TRACKS / KNOWN_LEAKS）需要具体作品的成品目录。
# 公开仓库**不含数据**，作品号也已脱敏成 RJ9xxxxx；真实 ID 的映射放在
# `internal/id_map.json`（gitignored）。
# 没有映射、或目录不存在 → 这些用例**跳过**，不判失败：
# 同一份测试在「有数据」和「clone 下来没数据」两种环境下都成立。
_ID_MAP_PATH = os.path.join(ROOT, "internal", "id_map.json")


def _load_id_map():
    try:
        return json.load(open(_ID_MAP_PATH, encoding="utf-8"))
    except Exception:
        return {}


_ID_MAP = _load_id_map()
SKIPPED = []


def resolve_rj(rj):
    return _ID_MAP.get(rj, rj)


def data_available(rj, track=""):
    """这条用例的数据在不在。不在 → 跳过。"""
    d = os.path.join(OUT_ROOT, resolve_rj(rj))
    if not os.path.isdir(d):
        return False
    if track:
        return bool(glob.glob(os.path.join(d, glob.escape(track) + "*.lrc")))
    return True

# 12 轨 cue 试点（与 A/B 同一批），用于「丢行」硬门禁
PILOT_TRACKS = [
    ("RJ9000002", "トラック1"), ("RJ9000002", "トラック2"),
    ("RJ9000006", "05 好き好き"), ("RJ9000010", "08.【あまあま授乳"),
    ("RJ9000011", "【Track02】"), ("RJ9000012", "【Track02】"),
    ("RJ9000014", "3,取り調べ前"), ("RJ9000014", "10,いうこと聞けなきゃ"),
    ("RJ9000016", "track03"), ("RJ9000001", "01.夜の日課"),
    ("RJ9000010", "10.【アナル交尾3P】"), ("RJ9000007", "04_ 一人になんて"),
]

# —— 已人工确认的 8 处「同段翻译泄漏」，冻结的原文（不要跟着输出改）——
# (rj, track, start, 前一行, 后一行)
KNOWN_LEAKS = [
    ("RJ9000002", "トラック1：私は、あんたみたいに友達がいるわけじゃないから。.mp3", 45.46,
     "你又不是说想见我，别老是做出奇怪的反应。", "别老是做出奇怪的反应。"),
    ("RJ9000002", "トラック2：む、むむ……そんなこと言う奴はこうしてやる.mp3", 467.38,
     "嘎吱嘎吱…嗯～", "嘎吱嘎吱…"),
    ("RJ9000006", "05 好き好き相互連呼でチンポとの相性再確認ゴム有りキスハメセックス.mp3", 803.30,
     "基君 喜欢喜欢喜欢", "喜欢喜欢喜欢"),
    ("RJ9000010", "08.【あまあま授乳&膝裏コキ】リーリア「おっぱい、好きにしていいよ…♡」_SE.mp3", 336.04,
     "一起舒服舒服？啊 老哥也", "一起舒服舒服？"),
    ("RJ9000011", "【Track02】おまんこ丸見えブルマでパイパン耳＆鼻素股♪.mp3", 614.10,
     "这样下去不行 真的不行", "真的不行"),
    ("RJ9000012", "【Track02】爆乳JKのザーメンおねだりちんぽしゃぶり.mp3", 589.34,
     "好疼… 啊啊啊… 好疼…", "好疼… 啊… 啊… 啊… …"),
    ("RJ9000014", "3,取り調べ前にリラックス【催眠導入】.mp3", 447.44,
     "像脱去内衣般 滑落下来。", "滑落下来。"),
    ("RJ9000014", "10,いうこと聞けなきゃ、公務淫行妨害罪.mp3", 507.52,
     "尽情品尝 快感让脑袋融化", "快感让脑袋融化"),
]

# —— 3e：源语驱动的称谓修复（GPT 第八轮 Q4）——
# (中文, 日文, 期望中文, 说明)
TR_CASES = [
    ("这位先生似乎更喜欢对女性的乳头进行刺激，", "違うよね先生はマヤの乳首攻めと",
     "老师似乎更喜欢对女性的乳头进行刺激，", "先生は无指示词→删这位"),
    ("这位先生真厉害！", "この先生はすごい", "这位老师真厉害！", "この先生→留这位"),
    ("考虑到先生一直以来的行为，", "先生が今までしてきたことを考えると",
     "考虑到老师一直以来的行为，", "裸先生→老师"),
    ("我的学生 哈拉玛老师", "教え子ハラマ先生", "我的学生 哈拉玛老师", "专名不动"),
    ("老公 你回来啦", "旦那", "老公 你回来啦", "源语无先生不动"),
]

# —— 3e2：实体账本判据（不得把「只有学生 tag」误判成师生关系）——
# (作品级文本, 期望语境 or None, 说明)
LEDGER_CASES = [
    ("學生JK七草姐妹與家教失職背德H 学生 track02_對學生發情的壞老師",
     "老师", "家教+学生→老师"),
    ("担任教師を寝取る 学生 制服", "老师", "担任教師→老师"),
    ("金髪不良JKアトラちゃん 学生 女仆 cosplay 纯爱", None,
     "只有学生 tag（无师生关系词）→ 不定性"),
    ("プリロリ乳牛物語 萝莉 魔法少女 怀孕", None, "无称谓线索→不定性"),
    ("痴女警官の魅惑 警察 刑事", "警察", "警官→警察"),
]

# D10 回退阶梯：单条重试的回复解析用例（(模型回复, 期望的 why)）
RETRY_CASES = [
    ("[c0]|哈啊…好舒服", "ok"),
    ("c0|哈啊…好舒服", "ok"),
    ("[c0] | 哈啊…好舒服", "ok"),
    ("哈啊…好舒服", "ok"),            # 没按格式 → 整段当译文
    ("はぁ…気持ちいい", "no_parse"),   # 吐回日文 → 必须拒
    ("気持ちいい", "no_parse"),
    ("", "empty"),
]

# D5 定向重译：解析用例（(模型回复, 期望是否拿到译文)）
TARGETED_CASES = [
    ("[c0]|你没问题的吧？", True),
    ("c0|你没问题的吧？", True),
    ("はぁ…大丈夫", False),      # 吐日文 → 拒
    ("", False),
]

# A8 收尾：bad_cell 的占位符守卫用例（(中文, 日文, 期望判坏, 说明)）
BAD_CELL_CASES = [
    ("那些喜欢欺负人的姐姐们 <M1> <M2> 被当作玩物来使用…",
     "いじめる お姉さんたちのおもちゃにされて", True, "跨句漂移的占位符残留"),
    ("<M1> <M2> 真是太巧了…", "まさけな", True, "无 span 句却带占位符"),
    ("嘴里全是みお的口水 真好吃", "みおのよだれの味がする おいしい", False,
     "单个假名人名残留**不**判坏（2026-10-15 修正，不得回归）"),
    ("嘴里全是みおみおみお的口水", "みおみおみおのよだれ", True,
     "连续 ≥4 假名才判坏"),
    ("那些喜欢欺负人的姐姐们被当作玩物来使用…",
     "いじめる お姉さんたちのおもちゃにされて", False, "干净译文不判坏"),
    ("嗯 啊 啊", "ん あ あ", False, "气声译文不判坏"),
    # ---- M2 协议（2026-10-18 默认开启）：`〔〕` 形态的跨句漂移同样要拦 ----
    # 切默认值前 `bad_cell` 只认 `<M\d+>`，`〔哈啊…〕` 漂到别句会直接漏进成品。
    ("〔哈啊…〕真是太巧了", "まさけな", True, "〔〕形态占位符漂到无 span 句"),
    ("那些姐姐们〔哈啊…〕被当作玩物", "いじめる お姉さんたちのおもちゃにされて",
     True, "〔〕形态占位符跨句漂移"),
    ("嗯 啊 啊 哈啊", "ん あ あ はぁ", False, "剥完包装的正常气声译文不判坏"),
    # ---- 元话语泄漏（2026-10-18 实测：提示行被模型当待译内容改写输出）----
    ("“んだ”这个词用于表达某种语气，用来说明是谁处于某种状况中。", "んだ", True,
     "元话语泄漏：模型解释 hint 里的语气说明"),
    ("“でしょ”这个表达其实是在寻求听者的认同，但并不一定属于疑问句。", "でしょ", True,
     "元话语泄漏：模型解释 hint 里的でしょ"),
    ("注意：这句话是针对听者说的。", "君は", True, "元话语泄漏：模型连「注意：」都写出来了"),
    ("对喜欢这个词有反应", "好き", False, "**不误伤**：正常台词也有「这个词」"),
    ("不是流行过青蛙化现象这个词吗？", "蛙化現象", False, "**不误伤**：同上"),
    ("因为能够说出“喜欢”这个词，就会让人感到幸福。", "好き", False, "**不误伤**：同上"),
    ("已经无法抗拒这个词汇", "たまらない", False, "**不误伤**：这个词汇"),
]


def parse_lrc(path):
    rows = []
    for ln in open(path, encoding="utf-8"):
        m = re.match(r"\[(\d+):(\d+\.\d+)\](.*)", ln.strip())
        if m:
            rows.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
    return rows


def check_detector(verbose):
    """8 个已知泄漏必须全部被检测器命中。"""
    bad = []
    for rj, track, start, zh_a, zh_b in KNOWN_LEAKS:
        r = leak_rules(zh_a, zh_b)
        if verbose:
            print("   %s [%.1fs] -> %s" % (rj, start, r or "❌ 未命中"))
        if not r:
            bad.append((rj, start))
    # R42：日文相同的相邻行 = 角色复读，中文相同是忠实翻译 → 必须放过
    for zh, ja in (("想吸吸你的肉棒", "おちんぽーなめたい"),
                   ("腰部不由自主地浮了起来。", "腰が勝手に浮いちゃう")):
        if leak_rules(zh, zh, ja, ja):
            bad.append(("复读豁免失效", zh))
            if verbose:
                print("   ❌ 复读豁免失效：%s / %s" % (zh, ja))
    return bad


def check_regressed(verbose):
    """8 处已知泄漏不得在对应成品的相邻行里重现。"""
    bad = []
    for rj, track, start, zh_a, zh_b in KNOWN_LEAKS:
        if not data_available(rj, track):
            SKIPPED.append((rj, track, "无数据"))
            continue
        rj = resolve_rj(rj)
        f = os.path.join(OUT_ROOT, rj, track + ".lrc")
        if not os.path.exists(f):
            bad.append((rj, track, "文件缺失"))
            continue
        nb = norm(zh_b)
        rows = parse_lrc(f)
        found = False
        for i in range(len(rows) - 1):
            if norm(rows[i + 1][1]) == nb and nb in norm(rows[i][1]):
                found = True
                break
        if verbose:
            print("   %s %s -> %s" % (rj, track[:26], "❌ 仍重现" if found else "✅ 已消失"))
        if found:
            bad.append((rj, track))
    return bad


def check_warnings(verbose):
    hits = []
    for f in sorted(glob.glob(os.path.join(OUT_ROOT, "RJ*", "*.lrc"))):
        rj = os.path.basename(os.path.dirname(f))
        rows = parse_lrc(f)
        for i in range(len(rows) - 1):
            r = leak_rules(rows[i][1], rows[i + 1][1])
            if r:
                hits.append((rj, os.path.basename(f)[:-4], rows[i][0], r, rows[i][1], rows[i + 1][1]))
    if verbose:
        for h in hits[:15]:
            print("   %s [%.1fs] %s：`%s` ⊃ `%s`" % (h[0], h[2], h[3], h[4][:24], h[5][:24]))
        if len(hits) > 15:
            print("   …（另有 %d 处）" % (len(hits) - 15))
    return hits


def check_cues(verbose):
    """cue 构建器验收：确定性 / 词数守恒 / 时间递增且不重叠 / 无「该切未切」的巨 cue。"""
    import glob
    import json
    import cues
    bad = {"det": 0, "cover": 0, "order": 0, "overlap": 0, "giant": 0}
    tot = unsplit = 0
    giant = []
    for jf in sorted(glob.glob(os.path.join(ASR_ROOT, "*", "asr", "*.json"))):
        try:
            d = json.load(open(jf, encoding="utf-8"))
        except Exception:
            continue
        w = cues.flatten_for_cues(d["segments"])
        if not w:
            continue
        c1 = cues.build_semantic_cues(w)
        c2 = cues.build_semantic_cues(w)
        if [x["start"] for x in c1] != [x["start"] for x in c2]:
            bad["det"] += 1
        if sum(len(c["words"]) for c in c1) != len(w):
            bad["cover"] += 1
        tot += len(c1)
        unsplit += sum(1 for c in c1 if c["split_reason"].startswith("max_guard"))
        for c in c1:
            # 「该切未切」：cue 内部存在**段边界**，且边界前的 token 以句末标点收尾
            # → 说明段边界 flush 漏了，两个独立句子被缝成一条（2026-10-14 新问题 ⑥ 的守卫）。
            ws = c.get("words") or []
            bad_here = False
            for j in range(1, len(ws)):
                if ws[j].get("seg") != ws[j - 1].get("seg"):
                    if ws[j - 1]["text"].strip()[-1:] in "。！？":
                        bad_here = True
                        break
            if bad_here:
                bad["giant"] += 1
                giant.append((os.path.basename(jf)[:20], round(c["start"], 1), c["text"][:30]))
        for i in range(len(c1) - 1):
            if c1[i + 1]["start"] < c1[i]["start"]:
                bad["order"] += 1
            if c1[i + 1]["start"] < c1[i]["end"] - 0.05:
                bad["overlap"] += 1
    if verbose:
        print("   cue 总数 %d，unsplittable %d (%.1f%%)" % (tot, unsplit, unsplit / max(1, tot) * 100))
        print("   失败项: %s" % bad)
        for g in giant[:5]:
            print("   ⚠️ 巨 cue: %s [%.1fs] %s" % g)
    return bad


RENDER_CASES = [
    ("那 请稍微张开嘴巴。", 12.0),
    ("你以为他在慢慢品味，其实他在紧紧握着。所以他才会那么用力。", 14.0),
    ("不要 不要这样 我真的不行了 求你停下来吧 我真的受不了了", 20.0),
    ("这个 3 次就好了。", 9.0),
    ("不 要 停 下 来 啊 我 真 的 受 不 了 了 求你", 22.0),
]

# —— 呼称人称反转（2026-10-21，坑 84）：(ja, zh, 期望命中, 说明) ——
# 正例的译文含「我」= **错译形态**（呼称被当成自称）；判据在译文侧。
PERSON_FLIP_CASES = [
    # ---- 正例：坑 84 签名（ja 无明确第一人称代词 + 呼称词 + 译文「我」）----
    ("ボクのこと、ですよ", "就是指我啊", True, "坑 84 原型：呼称被当自称"),
    ("ボクの上でよがってあげる", "我在你上面蹭", True, "坑 84 签名"),
    ("ボクのことはお兄ちゃんと呼んでください", "你要叫我哥哥", True, "ボク 呼称 → 错译成「我」"),
    ("君が一番大事なんだから", "我才是最重要的", True, "君 呼称 → 错译成「我」"),
    ("お前のせいだろうが", "都是我的错吧", True, "お前 呼称 → 错译成「我」"),
    ("あんたさえ良ければ", "只要我愿意", True, "あんた 呼称 → 错译成「我」"),
    # ---- 反例：ja 有明确第一人称代词 → 「我」合法 ----
    ("私が不眠症なの、あんたなら知ってるでしょ", "我有失眠症，你应该知道的。", False, "ja 有私"),
    ("わ、わたしは、あんたに嘘つかない。", "我不会对你撒谎的。", False, "ja 有わたし"),
    ("僕は学生です", "", False, "空译文不判"),
    ("", "我是学生", False, "空日文不判"),
    ("今日はいい天气ですね", "今天天气真好呢", False, "无呼称词不判"),
    ("君が一番大事なんだから", "你才是最重要的", False, "正确译法（你）不判"),
    # ---- 歧义命中（僕/ボク 自称作品）：R24 起按 info 存档，不再当复核项 ----
    ("そんなことないよ僕は", "没那回事 我是", True, "僕 歧义：自称作品合法命中(info)"),
    ("僕は本気だ", "我可是认真的", True, "僕 自称（RJ9000008 84 条实测）"),
    ("僕自身が絵具のように混ざりま", "让我自己像颜料一样融入其中吧。", True, "僕+自分"),
    ("ボクのことが好き?", "你喜欢我吗?", True, "第一人称ぼく作品的合法命中(info)"),
    ("あんたも、してくれるなら、嬉しい。", "你也能做的话，我会很开心。", True, "省略主语正确补出「我」(info)"),
    ("ママボクちゃんとはなれたくないでちゅよ", "妈妈，我舍不得离开你呢", True, "ボクちゃん=你(info)"),
]


# —— 2026-10-14 新增的 3 类问题：检测器必须命中已确认样本 ——
# (rule, ja, zh, dur, 说明)
NEW_RULE_CASES = [
    ("omit_suspect",
     "疲れた…。うん、あんたもお疲れ様。修学旅行一日目、やっと終わったね。",
     "好累…。嗯 你也是。", 5.7,
     "② 漏译「お疲れ様」（旧 cue 过大导致模型压缩）"),
    ("onomat_translit",
     "お兄ちゃんのお顔 キュー",
     "哥哥的脸 基欧。", 3.8,
     "④ 拟声「キュー」被音译成「基欧」（应译「啾」）"),
    ("cue_too_long",
     "はい?じゃなきゃとっくにやめてるでしょ それ、家庭教師じゃないですよね。",
     "是吗？如果不是这样的话，你早就放弃了吧。那，你不是家庭教师吧？", 10.0,
     "⑥ 两句独立话被缝成一条 cue（一行挂 10 秒）"),
    ("moan_drift",
     "お…あ…あ…足…",
     "妈…妈…妈…脚…", 3.3,
     "③ 气声串被前文「妈妈」带跑（应只是「哦…啊…啊…脚…」）"),
    ("moan_drift",
     "もぅ…",
     "已经…", 1.4,
     "⑫ 呻吟串里的 もう 借字面义译成「已经」（应是「嗯…」）"),
    ("ono_guess",
     "そして、僕がチパチパされている間、こっちのお耳にたっぷりと囁いてあげる。",
     "然后在我被电击的时候，给你的耳朵灌满恶作剧般的甜言蜜语。", 8.5,
     "⑭ 日文只是拟声串 チパチパ，中文却脑补出「电击」"),
    ("zh_collapse",
     "ちょっとだけ私の言った通りにしてみてよフリでいいから",
     "上层", 5.0,
     "⑮ 整行只剩 2 字（内容塌缩，RJ9000006 / RJ9000014 实录）"),
]

# zh_collapse 不能误伤的**反例**（短句短译是对的）
COLLAPSE_NEGATIVE = [
    ("先生", "老师"),                 # 2 字日文 → 2 字中文，正确
    ("気持ちいい", "舒服"),            # 同上
    ("おやすみなさい", "晚安。"),       # 公式化寒暄
    ("ぅぅっぅうっぁぅっんっぅぅううッ", "呜呜…"),  # 气声串（日文无 2 个汉字）
    ("あのね先生は出すしかないの", "是吗？"),        # 疑问标记
    # R32：显示层拆分的首行（段组有其他内容）不是塌缩（RJ9000007 freetalk 实录）
    ("ということで、この度今作のヒロインも私が演じさせていただきます", "所以",
     "所以这次这部作品的女主角也由我来饰演"),
]

# 气声判定不能误伤的**反例**（这些含真词，必须返回 False）
MOAN_NEGATIVE = [
    ("ん…わかった…", "明白了"),        # わかった 是真词
    ("ひ…ひらくな…", "开阔"),          # ひらく 是真词
    ("お…明るい…", "明亮"),            # 明るい 是真词
]

# 从句覆盖缺口（2026-10-23 Round 16，坑 103 的落地）
CLAUSE_GAP_CASES = [
    ([(0.0, "好累…。嗯 你也是。")],
     [{"start": 0.0, "end": 6.0,
       "text": "疲れた…。うん、あんたもお疲れ様。修学旅行一日目、やっと終わったね。"}], 0, True),
    ([(0.0, "热水已经煮好了，")],
     [{"start": 0.0, "end": 4.0, "text": "お湯はすでに沸いたものがご用意されているようですね"}], 0, False),
    # ⚠️ 从句必须 ≥2 个实质字：写成「一。二。三。」会被 _clauses 的 2 字门槛滤光 → 0 从句 → 误报
    ([(0.0, "第一个。第二个。第三个。")],
     [{"start": 0.0, "end": 6.0, "text": "一つ目です。二つ目です。三つ目です。"}], 0, False),
]

# 句尾「か」三态（2026-10-22 Round 12，坑 100 的三处修正）
KA_END_CASES = [
    ("いいんですか", "question"),        # 否定疑问，不是推量
    ("ないんですか", "question"),
    ("そうでしょうか", "inference"),     # 推量
    ("いっしょにしましょうか", "question"),   # 邀约，中文要给「吧」
    ("抵抗あるっていうか", "non_question"),  # 接续「或者说」
    ("もう行かない", None),              # 动词否定形，句尾不是疑问助词
    ("食べたい", None),                  # 不涉句尾か
]

# 纯 filler 行（2026-10-22 learningskill 第三条通道，坑 99）
# 这些行没有命题 → 不该参与否定/疑问判定
FILLER_CASES = [
    ("もうなんか", True), ("ほら", True), ("そうですね", True), ("あ、そう", True),
    ("もう行かない", False),      # 有动词 → 有命题
    ("こんなだらしないお人", False),   # 有名词内容
    ("もう無理", False),          # 名词内容
    ("とても気持ちいい", False),
]

# 整块复制（2026-10-22 Round 9，坑 97）
# (rows, 期望块数)
DUP_BLOCK_CASES = [
    # 正例：连续 3 行按同一偏移 +10s 重复
    ([(0.0, "哥哥等等还在休息"), (1.0, "等等等等"), (2.0, "才刚拔出来手"), (3.0, "别挡着"),
      (10.0, "哥哥等等还在休息"), (11.0, "等等等等"), (12.0, "才刚拔出来手"), (13.0, "稳住别张开")], 1),
    # 反例：单行重复是 ASMR 常态（好舒服/喜欢喜欢），不能报
    ([(0.0, "喜欢喜欢"), (5.0, "好舒服好舒服"), (9.0, "喜欢喜欢")], 0),
    # 反例：连续但偏移不一致，是正常重述不是复制
    ([(0.0, "哥哥等等还在休息"), (1.0, "等等等等"),
      (10.0, "哥哥等等还在休息"), (20.0, "等等等等")], 0),
]

# question/neg 的日文侧判据（2026-10-22 Round 8）
# (ja, zh, 期望 question 命中, 期望 neg 命中)
QNEG_CASES = [
    # `かな` 必须锚定：动词否定形里的「かな」不是疑问
    ("もう行かない", "不去了", False, True),
    ("行けるかな", "能去吗", True, False),
    # 句尾「か」但不是疑问的固定说法
    ("そっか", "原来如此", False, False),
    ("抵抗あるっつーか", "或者说有点抵触", False, False),
    ("場を追われていたでしょう", "恐怕是被赶走的吧", False, False),
    ("行けるだろ？", "能去吧？", True, False),
    # い形容词不是否定
    ("こんなだらしないお人", "这么懒散的人", False, False),
    ("この年にもなって情けないわね", "都这岁数了还这么丢人", False, False),
    # 真否定仍要命中
    ("食べられない", "吃不下", False, True),
    # Round 11（语料挖出）：接续用法「というか」= 或者说；申し訳ない = 对不起
    ("ていうかさ、", "或者说", False, False),
    ("申し訳ない", "对不起", False, False),
    # Round 18：`みんないい` 是 みんな+いい，不是否定；`人が来ない` 是真否定
    ("みんないい人たち", "你们都是好人。", False, False),
    ("人が来ない空き教室", "没人的空教室", False, True),
    # Round 19：`とか`（列举）与 `じゃないですか`（断言）都不是疑问
    ("感じようとか", "感觉被压迫之类的", False, False),
    ("立派な大人じゃないですか", "您已经是出色的成年人了", False, False),
    # R38（2026-10-23）：24 条抽样挖出的 4 类误报源
    # ① 惯用语/形容词的**汉字形**（旧表只有かな形，ASR 常写汉字）
    ("もう限界、堪らない", "已经到极限了，受不了", False, False),
    ("切ない気持ちになる", "心里难受", False, False),
    ("たまらない気持ち", "难以自持的心情", False, False),
    # ② 跨词界「ない」：こんな/あんな/そんな/どんな + い形容词
    ("あんないやらしい声出して", "发出那么下流的声音", False, False),
    ("そんなことない", "没那回事", False, True),
    # ③ なんじゃない = なん+じゃない 断言（剥后剩「お見通しか」，句尾 か 被
    #    `(?<!し)か` 的 lookbehind 挡住 → 两侧都不报，正是想要的）
    ("お見通しなんじゃないか", "被你看透了吧", False, False),
    # ④ 句尾「じゃない?」反问断言 / 「ませんか」礼貌邀约（后者是真疑问，不剥）
    ("もう終わりじゃない?", "已经结束了", False, False),
    ("気持ちよく感じませんか?", "能感受到舒服吗", True, False),
    # 真否定不能被新豁免吃掉
    ("気持ちよくない", "不舒服", False, True),
    ("終わらない", "还没结束", False, True),
    # R39（2026-10-23）：20 条抽样挖出的固定说法（日文侧剥除）
    ("大して仲良くもない子たち", "与那些关系一般的孩子们", False, False),
    ("そうですね、かまいません", "是的 可以这样", False, False),
    ("まだエッチないたずらされたいですか", "还是说还想被玩弄", True, False),
    ("ひどいじゃないか旦那様", "太过分了吧 老公", False, False),
    ("いつもより感じちゃうじゃない", "比平时更觉得呢", False, False),
    # ⚠️ 以下两条修的是**中文侧**（只/怎么可能），日文侧的 ない 仍在 → 期望 NEG=True
    ("そのくらい浅い恋心しかなかったんじゃないの?", "你对她的感情就只有这点程度吗？",
     False, True),
    ("のむわけがないでしょう", "这种提议怎么可能接受", False, True),
    # R40（2026-10-23）：非疑问「か」尾巴 + 裸 でしょ/のか/叠音
    ("うわ今までで一番でかいじゃねえか", "这应该是迄今为止最大的一只", False, False),
    ("旦那様なかなか政治が上手いじゃないか", "你政治手腕还真不错", False, False),
    ("傘さすのめんどくさいでしょ", "毕竟撑伞挺麻烦的", False, False),
    ("傘さすのめんどくさいでしょ?", "毕竟撑伞挺麻烦的", True, False),  # 带 ？仍算疑问
    ("つか", "总之…", False, False),
    ("ばーか", "傻瓜", False, False),
    ("じゃあ行こっか", "那就去吧", False, False),
    ("おっぱいふかふか", "胸部软绵绵", False, False),
    ("てっけど出したのか", "最终还是射出来了", False, False),
]
# 或谓豁免（R18）：かもしれない + 中文情态词 → 不算丢否定
POSSIBLE_CASES = [
    ("きついかも知れないから", "可能会有点疼", True),
    ("少し重いかもしれませんが", "那个 可能会有点重", True),
    ("それが真実ではない", "那不是真的", False),      # 真否定，不能豁免
    ("分かりません", "不知道", False),
]

# omit_suspect 段组聚合（2026-10-22 Round 6，坑 94）
# (rows, segs, 行号, 期望「仍判漏译」)
OMIT_GROUP_CASES = [
    # ① 单行只有 1 字，整条 cue 也只译了 1 字 → 真漏译
    ([(0.0, "啊")],
     [{"start": 0.0, "end": 2.0, "text": "これはとても気持ちいいですよ"}], 0, True),
    # ② 单行短但整条 cue 的中文够长（cue 被拆成 2 行）→ 不该判漏译
    ([(0.0, "啊"), (1.0, "好舒服啊真的好舒服")],
     [{"start": 0.0, "end": 2.0, "text": "これはとても気持ちいいですよ"}], 0, False),
]

# number_mismatch 归一化（2026-10-22 Round 4，坑 93）
# 正例：真数量错 → 必须命中；反例：汉字数字译法 / 连数 / 跨行 → 必须放过
NUMBER_POSITIVE = [
    ("2回?", "三次？", "②→③ 数量错"),
    ("10箱以上", "超过二十盒", "十→二十"),
    ("さすがに2人一緒に種付きは", "果然一起射精的话", "2人 漏译（无 ctx 时命中）"),
]
NUMBER_NEGATIVE = [
    ("3階一番奥", "三楼最深处", "汉字数字译法"),
    ("2ヶ月くらい", "大概两个月吧", "汉字数字译法"),
    ("ご褒美に5万個", "五万次", "混合写法 5万=50000"),
    ("んー12345", "嗯～ 1 2 3 4 5", "连数拆分"),
    ("マグワ5本", "MAG3A 5本", "型号里的数字（MAG3A）"),
]

# onomat_translit 不能误报的**反例**（B2 改进②：汉语拟态词/正确译法必须放过）
# B2 扫描 4 条 onomat_translit 里 3 条是这类误报：
#   · ニコニコ→笑咪咪（ABB 拟态）  · ガタガタ→喀哒喀哒（正确拟声字）
# 注意「卡喀啦妹妹」是角色名（カカラ）——结构上恰是 ABB 被放过，方向安全。
ONOMAT_NEGATIVE = [
    ("ニコニコしながら", "笑咪咪地", "ABB 拟态词（笑咪咪）"),
    ("キューキュー", "咕啾咕啾", "ABAB 叠音拟声"),
    ("ガタガタ", "喀哒喀哒", "正确汉语拟声字（哒）"),
    ("リアリア", "湿漉漉的", "ABB 拟态词（湿漉漉）"),
    ("カタカタ", "颤抖抖的", "AAB/ABB 拟态（颤抖抖）"),
    ("では、カカラちゃん", "那么 卡喀啦妹妹", "人称后缀前的角色名（カカラ+ちゃん）"),
]


def check_new_rules(verbose):
    """5 类新问题的检测器必须命中已确认样本；气声/拟态判定不得误伤。"""
    bad = []
    for rule, ja, zh, dur, note in NEW_RULE_CASES:
        if rule == "omit_suspect":
            r = omit_ratio(ja, zh)
            hit = (r is not None and r < OMIT_RATIO)
        elif rule == "onomat_translit":
            hit = onomat_translit(ja, zh)
        elif rule == "moan_drift":
            hit = moan_drift(ja, zh)
        elif rule == "ono_guess":
            hit = bool(ono_guess(ja, zh))
        elif rule == "zh_collapse":
            hit = zh_collapse(ja, zh)
        else:
            hit = cue_too_long(zh, dur)
        if verbose:
            print("   %-16s -> %s  (%s)" % (rule, "✅ 命中" if hit else "❌ 未命中", note))
        if not hit:
            bad.append((rule, note))
    for _c in COLLAPSE_NEGATIVE:
        ja, zh = _c[0], _c[1]
        _zg = _c[2] if len(_c) > 2 else None
        if zh_collapse(ja, zh, _zg):
            bad.append(("zh_collapse_false_positive", ja))
            if verbose:
                print("   %-16s -> ❌ 误判为塌缩：%s / %s" % ("collapse_negative", ja, zh))
    for ja, zh in MOAN_NEGATIVE:
        if is_moan_ja(ja):
            bad.append(("moan_false_positive", ja))
            if verbose:
                print("   %-16s -> ❌ 误判为气声串：%s" % ("moan_negative", ja))
    if verbose:
        print("   气声反例 %d 条未误伤" % len(MOAN_NEGATIVE))
    # 拟态白名单反例：这些**不该**被判音译
    for ja, zh, note in ONOMAT_NEGATIVE:
        if onomat_translit(ja, zh):
            bad.append(("onomat_false_positive", note))
            if verbose:
                print("   %-16s -> ❌ 误判为音译：%s (%s)" % ("onomat_negative", zh, note))
    if verbose:
        print("   拟态反例 %d 条未误伤" % len(ONOMAT_NEGATIVE))
    # 数字比对：正例必须命中，反例必须放过
    for ja, zh, note in NUMBER_POSITIVE:
        if not number_mismatch(ja, zh):
            bad.append(("number_missing_hit", note))
            if verbose:
                print("   %-16s -> ❌ 未命中：%s" % ("number_positive", note))
    for ja, zh, note in NUMBER_NEGATIVE:
        if number_mismatch(ja, zh):
            bad.append(("number_false_positive", note))
            if verbose:
                print("   %-16s -> ❌ 误报：%s (%s)" % ("number_negative", note, zh))
    if verbose:
        print("   数字反例 %d 条未误伤" % len(NUMBER_NEGATIVE))
    # 段组聚合：单行短 + 段组正常 → 不能判漏译
    for rows, segs, i, want in OMIT_GROUP_CASES:
        r = omit_group_ratio(rows, segs, i)
        got = (r is not None and r < OMIT_RATIO)
        if got != want:
            bad.append(("omit_group", rows, r, want))
            if verbose:
                print("   %-16s -> ❌ 段组比 %s（期望 %s）" % ("omit_group", r, want))
    if verbose:
        print("   段组聚合用例 %d 条通过" % len(OMIT_GROUP_CASES))
    # question / neg 的日文侧判据
    for ja, zh, want_q, want_n in QNEG_CASES:
        jq = ja
        for w in Q_JA_STRIP:
            jq = jq.replace(w, "")
        # R38：门禁必须复刻 scan 的**正则剥除**，否则测的不是线上判据
        for rx, rep in Q_JA_RESTRIP:
            jq = rx.sub(rep, jq)
        jn = ja
        for w in NEG_JA_STRIP:
            jn = jn.replace(w, "")
        for rx, rep in NEG_JA_RESTRIP:
            jn = rx.sub(rep, jn)
        # 只测**日文侧**判据（中文侧由上面的 zh 决定，别混进来）
        got_q = bool(RE_Q_JA.search(jq))
        got_n = bool(RE_NEG_JA.search(jn))
        if (got_q, got_n) != (want_q, want_n):
            bad.append(("qneg", ja, (got_q, got_n), (want_q, want_n)))
            if verbose:
                print("   %-16s -> ❌ %r Q=%s NEG=%s（期望 %s/%s）"
                      % ("qneg", ja, got_q, got_n, want_q, want_n))
    if verbose:
        print("   question/neg 用例 %d 条通过" % len(QNEG_CASES))
    for ja, zh, want in POSSIBLE_CASES:
        got = neg_possible_exempt(ja, zh)
        if got != want:
            bad.append(("possible_exempt", ja, got, want))
            if verbose:
                print("   %-16s -> ❌ %s（期望 %s）" % ("possible_exempt", got, want))
    if verbose:
        print("   或谓豁免用例 %d 条通过" % len(POSSIBLE_CASES))
    # 整块复制：正例必须命中，ASMR 常态重复必须放过
    for rows, want in DUP_BLOCK_CASES:
        got = len(find_dup_blocks(rows))
        if got != want:
            bad.append(("dup_block", got, want))
            if verbose:
                print("   %-16s -> ❌ 命中 %d（期望 %d）" % ("dup_block", got, want))
    if verbose:
        print("   整块复制用例 %d 条通过" % len(DUP_BLOCK_CASES))
    # 纯 filler 行豁免
    # ⚠️ 依赖 janome（日语分词）。缺了这些用例**跳过**而不是判失败：
    # 公开仓库的 requirements 里虽然有 janome，但别人可能用最小环境跑。
    try:
        import janome  # noqa: F401
        _has_janome = True
    except Exception:
        _has_janome = False
    if not _has_janome:
        SKIPPED.append(("filler_only", "无 janome", "pip install janome"))
    else:
        for ja, want in FILLER_CASES:
            got = is_filler_only(ja)
            if got != want:
                bad.append(("filler_only", ja, got, want))
                if verbose:
                    print("   %-16s -> ❌ %r（期望 %s）" % ("filler_only", got, want))
        if verbose:
            print("   filler 用例 %d 条通过" % len(FILLER_CASES))
    # 句尾「か」三态分类（learningskill mine_grammar.classify_ka，坑 100 的三处修正）
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts", "style"))
        import mine_grammar as _MG
        for s, want in KA_END_CASES:
            got = _MG.classify_ka(s)
            if got != want:
                bad.append(("ka_end", s, got, want))
                if verbose:
                    print("   %-16s -> ❌ %r → %s（期望 %s）" % ("ka_end", s, got, want))
        if verbose:
            print("   ka_end 用例 %d 条通过" % len(KA_END_CASES))
    except ImportError as e:
        # 缺依赖（多半是 janome）→ 跳过，不判失败
        SKIPPED.append(("ka_end", "缺依赖", str(e)[:40]))
    except Exception as e:
        bad.append(("ka_end_import", str(e)[:40], None, None))

    # 从句覆盖缺口（坑 103 落地）
    for rows, segs, i, want in CLAUSE_GAP_CASES:
        got = omit_clause_gap(rows, segs, i)
        if got != want:
            bad.append(("clause_gap", got, want))
            if verbose:
                print("   %-16s -> ❌ %s（期望 %s）" % ("clause_gap", got, want))
    if verbose:
        print("   从句覆盖用例 %d 条通过" % len(CLAUSE_GAP_CASES))
    # 知识库自检：zh_mood 必须覆盖 pragmatics_hints 手写表的核心项（坑 102）
    # ⚠️ 依赖风格库（data/style/grammar_patterns.json）。公开仓库不含数据，
    # 协作者 clone 下来必然没有 → **缺文件就跳过**，不判失败。
    _gp = os.path.join(ROOT, "data", "style", "grammar_patterns.json")
    if not os.path.exists(_gp):
        SKIPPED.append(("zh_mood", "无风格库", "grammar_patterns.json 不存在"))
    else:
        try:
            _zm = json.load(open(_gp, encoding="utf-8")).get("zh_mood") or {}
            for w in ("吧？", "吗？", "不是", "肯定", "对吧", "应该"):
                if w not in _zm:
                    bad.append(("zh_mood_missing", w, None, None))
            if verbose:
                print("   zh_mood 知识库 %d 项，核心 6 项齐备" % len(_zm))
        except Exception as e:
            bad.append(("zh_mood_load", str(e)[:40], None, None))
    # ---- 呼称人称反转（2026-10-21，坑 84）----
    for ja, zh, want, note in PERSON_FLIP_CASES:
        got = person_flip(ja, zh)
        if verbose:
            print("   %-16s -> %s（期望 %s） (%s)" % ("person_flip",
                  "命中" if got else "未命中", "命中" if want else "不命中", note))
        if got != want:
            bad.append(("person_flip", note))
    return bad


def check_render(verbose):
    """显示切分验收：**只切不重写**（拼接回去必须等于成稿）+ 时间连续不重叠。"""
    import cues
    bad = []
    for zh, dur in RENDER_CASES:
        cue = {"cue_id": "t", "start": 0.0, "end": dur}
        lines = cues.render_display_lines(cue, zh, {})
        joined = "".join(l["text"] for l in lines).replace(" ", "")
        ts = [(l["start"], l["end"]) for l in lines]
        ok_time = (all(ts[i][1] <= ts[i + 1][0] + 1e-6 for i in range(len(ts) - 1))
                   and ts[0][0] >= -1e-6 and ts[-1][1] <= dur + 1e-6)
        if joined != zh.replace(" ", "") or not ok_time:
            bad.append((zh[:16], joined == zh.replace(" ", ""), ok_time))
        if verbose:
            print("   %2.0fs → %d 行  无损=%s 时间=%s" % (dur, len(lines), joined == zh.replace(" ", ""), ok_time))
    return bad


def check_no_line_loss(verbose):
    """**硬门禁：成品行数必须 ≥ 输入 cue 数**（GPT 第六轮 Q5）。

    显示层只会**多切**不会合并，所以 `成品行数 < cue 数` 一定是**丢了内容**。
    丢行 = 用户永远看不到那句，比译错严重得多。

    实测事故（2026-10-15）：RJ9000010 `10.` 的 `みおのよだれの味がする おいしい`
    （11:19.26）因初译留了假名人名 `みお` 被 `bad_cell` 判「日文残留」→ **整行丢弃**
    （该轨 cue 127 → 成品 126）。

    ⚠️ 2026-10-16 修正判据：`成品行数 ≥ 有台词的 cue 数`。无台词 cue（ASR 静音/
    效果音段，text 为空）本就不该产生字幕行，旧判据把它们也算进分母，
    RJ9000009 `04.` 会因 2 个空 cue 假阳性。
    """
    import cues
    import polish as P
    import glob as _g
    bad = []
    for rj, key in PILOT_TRACKS:
        if not os.path.isdir(os.path.join(ASR_ROOT, resolve_rj(rj))):
            SKIPPED.append((rj, key, "无 ASR 数据"))
            continue
        rj = resolve_rj(rj)
        units = []
        for f in _g.glob(os.path.join(ASR_ROOT, rj, "asr", "*.json")):
            d = json.load(open(f, encoding="utf-8"))
            if key in d.get("source", ""):
                units, _ = cues.build_units(d["segments"], P.build_lines)
                break
        if not units:
            continue
        n_spoken = sum(1 for u in units if (u.get("text") or "").strip())
        lrc = None
        for b in (os.path.join(OUT_ROOT, rj), OUT_ROOT):
            for f in _g.glob(os.path.join(b, "*.lrc")):
                if key in os.path.basename(f):
                    lrc = f
                    break
            if lrc:
                break
        if not lrc:
            continue
        n_out = len(parse_lrc(lrc))
        if n_out < n_spoken:
            bad.append((rj, key[:16], n_spoken, n_out))
        if verbose:
            print("   %s %s：有台词 cue %d → 成品 %d %s"
                  % (rj, key[:14], n_spoken, n_out,
                     "✅" if n_out >= n_spoken else "❌ 丢了 %d 行" % (n_spoken - n_out)))
    return bad


def check_mask(verbose):
    """A8：非实义 span 遮蔽器（简短性 + 往返 + 无误遮真词）。"""
    import mask_nonlexical as M
    bad = []
    for ja, want in M.CASES:
        masked, spans = M.mask_ja(ja)
        restored, problems = M.restore_ja(masked, spans)
        if (restored != ja) or problems or bool(spans) != want:
            bad.append((ja, want, bool(spans), problems))
        if verbose:
            print("   %-36s 遮蔽=%-5s 往返=%s" % (ja[:34], bool(spans),
                                                 restored == ja and not problems))
    return bad


# D9-3：纯非实义独立 cue 的确定性直出（(日文, 期望直出值 or None)）
DET_CASES = [
    ("はぁ…", "哈啊"),
    ("んー", "嗯—"),
    ("ピチャピチャ…ふふ…んー", "啪叽啪叽…呵呵…嗯—"),
    ("クルクルクルクル?", "转啊转转啊转？"),   # 问号不能丢
    ("はぁ…もぅ…お兄ちゃん", None),        # 有实义 → 必须过模型
    ("あ、そうなんだ", None),
    ("クルクル", "转啊转"),
]


def check_deterministic(verbose):
    """D9-3：直出判据（有实义的不许直出、问号不许丢）。"""
    import mask_nonlexical as M
    bad = []
    for ja, want in DET_CASES:
        got = M.pure_nonlexical(ja)
        if verbose:
            print("   %-24r → %r" % (ja[:22], got))
        if got != want:
            bad.append((ja[:22], got, want))
    return bad


def check_retry_parse(verbose):
    """D10 回退阶梯第 1 级：单条重试的回复解析。

    用 mock 替换 `polish.call`，覆盖真实会遇到的回复形态：
    标准格式 / 无方括号 / 带空格 / 完全没按格式 / 吐回日文 / 空回复。
    ⚠️ 「吐回日文」必须拒——否则重试反而把日文写进成品。
    """
    import polish as P
    _orig = P.call
    sp = [("〔哈啊…〕", "はぁ")]
    bad = []
    try:
        for reply, want in RETRY_CASES:
            P.call = (lambda r: (lambda msgs, **kw: r))(reply)
            got, why = P._retry_one_cue("sys", "はぁ…", "哈…", "哈啊", sp, True)
            if verbose:
                print("   %-22r → %r (%s)" % (reply[:20], got, why))
            if why != want:
                bad.append((reply[:20], why, want))
    finally:
        P.call = _orig
    return bad


def check_targeted(verbose):
    """D5 定向重译：后检测触发后的单句重写解析（mock 模型回复）。"""
    import polish as P
    _orig = P.call
    bad = []
    try:
        for reply, want in TARGETED_CASES:
            P.call = (lambda r: (lambda msgs, **kw: r))(reply)
            got = P._targeted_rewrite("sys", "君は大丈夫なわけ?", "你没问题吧", "没问题", "ja 含 君，中文无听者指向")
            if verbose:
                print("   %-24r → %r" % (reply[:20], got))
            if bool(got) != want:
                bad.append((reply[:20], bool(got), want))
    finally:
        P.call = _orig
    return bad


def check_bad_cell(verbose):
    """A8 收尾：bad_cell 必须把**占位符残留**判为坏格（跨句漂移的兜底）。

    实测事故（2026-10-16 RJ9000014 Tr.10）：模型把上一句的 `<M1> <M2>`（来自
    `しこしこ、しこしこ`）搬进了下一句（`いじめる お姉さんたちのおもちゃにされて`
    本无 span）→ `restore_zh` 只校验「本句应有的 span」，抓不到跨句漂移 →
    成品里留下 `<M1> <M2>`。`bad_cell` 加占位符守卫后，这类行退回初译。
    """
    import polish as P
    bad = []
    for zh, ja, want, desc in BAD_CELL_CASES:
        got = P.bad_cell(zh, ja)
        if verbose:
            print("   %-30s → bad=%-5s" % (desc[:28], got))
        if got != want:
            bad.append((desc, got, want))
    return bad


def check_term_repair(verbose):
    """3e：源语驱动的称谓修复（GPT 第八轮 Q4）+ 实体账本判据。"""
    bad = []
    import term_repair as TR
    for zh, ja, exp, desc in TR_CASES:
        new, _n, _w = TR.repair_line(zh, ja)
        if verbose:
            print("   %-34s %s" % (desc[:32], "✅" if new == exp else "❌ " + new))
        if new != exp:
            bad.append((desc, new, exp))
    return bad


def check_ledger(verbose):
    """3e2：实体账本不得把「只有学生 tag」误判成师生关系。"""
    bad = []
    import entity_ledger as EL
    for text, want_ctx, desc in LEDGER_CASES:
        ctx, _hits, _clear = EL.infer_sensei_family(text)
        if verbose:
            print("   %-34s → %s" % (desc[:32], ctx))
        if ctx != want_ctx:
            bad.append((desc, ctx, want_ctx))
    return bad


# ---- R23（坑 106）：LRC 清屏 spacer + cue_too_long 收窄 ----
# 根因：LRC 行显示到下一行时间戳为止，cap_holds 的 end 不写盘就失效
# （全库 693 行停留 ≥20s、最严重 123.3s）。spacer_rows 在写盘层补空文本
# 清屏行；cue_too_long 同步收窄为「停留 > max(阅读 cap, 语音剩余) + 8s」。
SPACER_CASES = [
    # (display lines, next_start, 期望行数, 说明)
    ([{"start": 0.0, "end": 3.0, "text": "你好。"}], 10.0, 2, "末行有 gap → 补 spacer"),
    ([{"start": 0.0, "end": 3.0, "text": "你好。"},
      {"start": 3.0, "end": 6.0, "text": "再见。"}], 9.0, 3, "多行 cue：仅末行补"),
    ([{"start": 0.0, "end": 3.0, "text": "你好。"},
      {"start": 3.0, "end": 6.0, "text": "再见。"}], 6.0, 2, "下一 cue 紧接 → 不补"),
    ([{"start": 0.0, "end": 3.0, "text": "你好。"}], None, 1, "轨尾 → 不补"),
    ([{"start": 0.0, "end": 2.0, "text": "嗯。"},
      {"start": 2.0, "end": 4.0, "text": "好。"}], 4.0, 2, "gap 0 → 不补"),
]
LINGER_CASES = [
    # (zh, 停留, 阅读 cap, 该行语音剩余, 期望命中)
    ("什么？你紧紧抓住我的腰？", 84.8, 5.3, 2.8, True),  # RJ9000006 实录：84.8s
    ("要吃吗？好吃吗？", 119.5, 2.0, 0.2, True),        # RJ9000003 实录：挂静音 117s
    ("什么？怎么回事？", 5.9, 2.9, 5.9, False),        # 停留≈语音 → 不报
    ("一直想摸着它。真的好温柔。", 7.5, 5.3, 5.3, False),  # cue 5.3s + gap 假象 → 不报
    ("嗯？什么？", 5.5, 2.9, 5.5, False),              # 短句慢速语音 → 不报
    ("要吃吗？", 119.5, 2.0, 0.2, False),              # 单句（<2 句末标点）不归本规则；由 spacer 修
    ("晚安", 110.1, 2.0, 0.0, False),                 # 幻觉段 0 标点同上；由 spacer 修
    ("嗯！嗯！嗯！", 20.0, 5.5, 1.0, False),           # 叠词豁免
    ("只有一个句号。", 20.0, 5.5, 1.0, False),         # <2 个句末标点 → 不报
]


def check_spacer_linger(verbose):
    import cues as CS
    bad = []
    for lines, nxt, want_n, note in SPACER_CASES:
        rows = CS.spacer_rows(lines, nxt)
        if len(rows) != want_n:
            bad.append(("spacer", note, len(rows), want_n))
            if verbose:
                print("   ✗ spacer %s → %d 行（期望 %d）" % (note, len(rows), want_n))
        elif len(rows) > len(lines) and rows[-1][1] != "":
            bad.append(("spacer_text", note, rows[-1]))
            if verbose:
                print("   ✗ spacer %s → 末行非空 %r" % (note, rows[-1]))
    for zh, dur, cap, left, want in LINGER_CASES:
        got = cue_too_long(zh, dur, cap, left)
        if got != want:
            bad.append(("cue_too_long", zh[:14], got, want))
            if verbose:
                print("   ✗ cue_too_long %r → %s（期望 %s）" % (zh[:16], got, want))
    if verbose and not bad:
        print("   spacer %d 例 / cue_too_long %d 例全过"
              % (len(SPACER_CASES), len(LINGER_CASES)))
    return bad


# ---- R28（坑 107）：说话人名泄漏 5 格式守卫（spk_leak）----
SPK_LEAK_CASES = [
    # (zh, spk, 期望命中)
    ("白", "白", True),                                    # 整行只剩人名
    ("白的思考变得漫无目的。", "白", True),                 # 行首 X的
    ("甘 好不容易获得了赦免。", "甘", True),                # 行首 X+空格
    ("甘：用那种撒娇的声音。", "甘", False),                 # R35：行首 X： 是规范前缀，不算泄漏
    ("白的思考变得漫无目的。", "白", True),                 # R35：不带冒号的行首 X的 仍是泄漏
    ("如果招供就会被抓。人：白", "白", True),               # 行尾 人：X
    ("感觉非常舒服。人：甘", "甘", True),
    ("刚刚遇到的那个女孩…甘", "甘", True),                 # 行尾裸名
    ("是的 白。", "白", True),                             # 行尾 空格+名+句末
    ("恭喜你 白。", "白", True),
    ("如果招供就会被抓。", "白", False),                    # 正常行文
    ("我明白。", "白", False),                             # 白 在词中不判
    ("他说明白。", "白", False),
    ("把脸涂白。", "白", False),                           # 无空格+白。不判
    ("味道真甜。", "甘", False),
    ("", "白", False),                                     # 空译文不判
    ("白", "", False),                                     # 无 spk 不判
    ("关于白天的事情。", "白", False),                      # 白 在词中
]


def check_spk_leak(verbose):
    import polish as PL
    bad = []
    for zh, spk, want in SPK_LEAK_CASES:
        got = PL.spk_leak(zh, spk)
        if got != want:
            bad.append((zh[:20], spk, got, want))
            if verbose:
                print("   ✗ spk_leak(%r, %r) → %s（期望 %s）" % (zh[:20], spk, got, want))
    if verbose and not bad:
        print("   spk_leak %d 例全过" % len(SPK_LEAK_CASES))
    return bad


# ---- R35（A2 乙方案）：strip_spk_leak + 人名前缀规范化 ----
STRIP_LEAK_CASES = [
    # (zh, spk, 期望)
    ("如果招供就会被抓。人：白", "白", "如果招供就会被抓。"),
    ("感觉非常舒服。人：甘", "甘", "感觉非常舒服。"),
    ("刚刚遇到的那个女孩…甘", "甘", "刚刚遇到的那个女孩…"),
    ("是的 白。", "白", "是的。"),
    ("恭喜你 白。", "白", "恭喜你。"),
    ("甘 好不容易获得了赦免。", "甘", "好不容易获得了赦免。"),
    ("甘：用那种撒娇的声音。", "甘", "用那种撒娇的声音。"),
    ("白", "白", ""),                        # 整行只剩人名 → 空
    ("我明白。", "白", "我明白。"),            # 行文不动
    ("把脸涂白。", "白", "把脸涂白。"),
    ("关于白天的事情。", "白", "关于白天的事情。"),
    ("白色的思考变得漫无目的。", "白", "白色的思考变得漫无目的。"),
    # R43（2026-10-23）：第 6 种格式——人名前无标点无空格，但行首已有规范前缀
    ("甘：但是甘", "甘", "但是"),
    ("白：我明白了白", "白", "我明白了"),
    # ⚠️ 无前缀时不能剥（「明白」「不甘」是合法词）
    ("但是我明白", "白", "但是我明白"),
    ("心里不甘", "甘", "心里不甘"),
]


def check_strip_leak(verbose):
    import polish as PL
    bad = []
    for zh, spk, want in STRIP_LEAK_CASES:
        got = PL.strip_spk_leak(zh, spk)
        if got != want:
            bad.append((zh[:18], spk, got, want))
            if verbose:
                print("   ✗ strip_spk_leak(%r, %r) → %r（期望 %r）" % (zh[:18], spk, got, want))
    if verbose and not bad:
        print("   strip_spk_leak %d 例全过" % len(STRIP_LEAK_CASES))
    return bad


# kekkon_confusion（2026-10-25 T6，坑 120：新增/修改规则必须同步进 harness）
# (ja, zh, 期望命中, 说明) —— 結婚/結構/血行 同音混淆（けっこん/けっこう 一音差）。
# 语料实测 73 句「結婚」里 59 句真结婚 → 判据宁漏勿伤：真结婚句（中文有「婚」）必须放过。
KEKKON_CASES = [
    ("私たち結婚、趣味が合うよね", "我们的兴趣还蛮相投的", True,
     "正例1：結構 被 ASR 听成 結婚，中文无「婚」"),
    ("結婚が良くなってきたね", "加快血液循环", True,
     "正例2：血行 被听成 結婚（中文译的是血行）"),
    ("そろそろ君と結婚したいな", "想和你结婚", False,
     "负例1：真结婚句，中文有「婚」→ 不许命中（误伤即翻车）"),
    ("今日はいい天気だね", "今天天气真不错", False,
     "负例2：ja 不含 結婚/結構 → 不许命中"),
]


def check_kekkon(verbose):
    """T6 门禁：kekkon_confusion 正例必中、真结婚句必放。

    另加一条**段组边界**用例（2026-10-26，坑 143）：显示行跨了段组起点时，
    段组中文必须把前一条显示行也算进来，否则「婚」字被切到上一行 → 误报。
    """
    bad = []
    for ja, zh, want, note in KEKKON_CASES:
        got = bool(kekkon_confusion(ja, zh))
        if verbose:
            print("   %-16s -> %s（期望 %s） (%s)" % ("kekkon",
                  "命中" if got else "未命中", "命中" if want else "不命中", note))
        if got != want:
            bad.append(("kekkon_confusion", note))

    # 段组边界用例（真实数据形状，取自 RJ9000007 轨07 80.44–86.62s）
    rows = [(80.44, "太蠢了…如果我真的是在搞婚姻诈骗之类的把戏，"),
            (84.85, "那该怎么办啊？真的…")]
    segs = [{"start": 80.44, "end": 81.78, "text": "バカすぎ"},
            {"start": 81.78, "end": 86.62,
             "text": "アタシが結婚詐欺かなんかだったらどーすんだよ、ほんと"}]
    ja_g, zh_g, idx_g = group_text(rows, segs, 1, bridge_prev=True)
    got = bool(kekkon_confusion(ja_g, zh_g))
    if verbose:
        print("   %-16s -> %s（期望 不命中）(段组边界：婚 落在上一显示行)" %
              ("kekkon/group", "命中" if got else "未命中"))
    if got:
        bad.append(("kekkon_confusion", "段组边界未桥接 → 误报（坑 143）"))
    # 反向自检：不桥接时**必须**命中，否则这条门禁就失去意义（参数被写死成恒真）
    _ja0, _zh0, _ = group_text(rows, segs, 1, bridge_prev=False)
    if not kekkon_confusion(_ja0, _zh0):
        bad.append(("kekkon_confusion", "bridge_prev 参数失效（不桥接也不命中）"))
    return bad


def check_repair_overlay(verbose):
    """T8 3o：repair_overlay 状态机（--add 幂等/--confirm/--check 锁校验）。

    全程 tempfile 临时目录 + monkeypatch 模块路径常量——**绝不写真覆盖层/真成品/真锁**。
    """
    import io
    import tempfile
    import contextlib
    import repair_overlay as RO

    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        prod = os.path.join(tmp, "prod")
        ovl = os.path.join(tmp, "ovl")
        locks = os.path.join(tmp, "locks")
        os.makedirs(os.path.join(prod, "RJTEST"))
        os.makedirs(ovl)
        lrc_name = "01.テスト.mp3.lrc"
        # 3 行内容行：cue2 定位在 12.50（plan start 12.3 在 ±0.6s 容差内）
        with open(os.path.join(prod, "RJTEST", lrc_name), "w", encoding="utf-8") as f:
            f.write("[ti:t]\n[by:b]\n\n[00:05.00]第一行\n[00:12.50]第二行\n[00:20.00]第三行\n")
        plan = [{"rj": "RJTEST", "track": "01.テスト.mp3", "start": 12.3,
                 "zh": "改后的第二行", "why": "T8 门禁", "ja": "てすと"}]
        plan_p = os.path.join(tmp, "plan.json")
        with open(plan_p, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False)

        _old = (RO.OVL, RO.LRC, RO.LOCKS)
        RO.OVL, RO.LRC, RO.LOCKS = ovl, prod, locks
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                RO.cmd_add(plan_p, "low", "pending_listen")
                RO.cmd_add(plan_p, "low", "pending_listen")     # 第二遍必须幂等
            d = RO.load("RJTEST")
            ents = d["entries"]
            if len(ents) != 1:
                bad.append(("add 幂等失败", len(ents)))
            e = ents[0] if ents else {}
            if e.get("id") != "t01-0012":
                bad.append(("id", e.get("id")))
            if e.get("cue") != 2:
                bad.append(("cue", e.get("cue")))
            if abs((e.get("t0") or 0) - 12.5) > 1e-6:
                bad.append(("t0", e.get("t0")))
            if abs((e.get("t1") or 0) - 20.0) > 1e-6:
                bad.append(("t1", e.get("t1")))
            if e.get("proposed_zh") != "改后的第二行":
                bad.append(("proposed_zh", e.get("proposed_zh")))
            if e.get("original_zh") != "":
                bad.append(("original_zh 应留空", e.get("original_zh")))
            if "skip 已存在" not in buf.getvalue():
                bad.append(("幂等提示缺失",))
            if "original_zh 为空" not in buf.getvalue():
                bad.append(("original_zh 空提示缺失",))
            if verbose:
                print("   add 输出：\n     " + buf.getvalue().strip().replace("\n", "\n     "))

            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    RO.cmd_confirm("t01-0012", None)
            except SystemExit as _se:
                bad.append(("confirm 异常退出", str(_se)))
            e = RO.load("RJTEST")["entries"][0] if RO.load("RJTEST")["entries"] else {}
            if e.get("status") != "confirmed":
                bad.append(("confirm", e.get("status")))

            # confirmed 无锁：--check 必须报
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                RO.check()
            if "缺锁" not in buf2.getvalue():
                bad.append(("check 未报缺锁", buf2.getvalue().strip()[:80]))

            # 登锁后：--check 必须干净（key = 干名|时间戳，与 load_locks 同口径；
            # 干名 = track 去掉 .lrc，即 plan 里的「01.テスト.mp3」）
            os.makedirs(locks, exist_ok=True)
            with open(os.path.join(locks, "RJTEST.json"), "w", encoding="utf-8") as f:
                json.dump({"01.テスト.mp3|[00:12.50]": {"zh": "改后的第二行", "old": "第二行"}},
                          f, ensure_ascii=False)
            buf3 = io.StringIO()
            with contextlib.redirect_stdout(buf3):
                RO.check()
            if "缺锁" in buf3.getvalue():
                bad.append(("check 有锁仍报缺锁",))
            if "0 条异常" not in buf3.getvalue():
                bad.append(("check 时间轴应 0 异常", buf3.getvalue().strip()[:80]))
        finally:
            RO.OVL, RO.LRC, RO.LOCKS = _old
    return bad


# ---------------------------------------------------------------- Sakura 兜底判定护栏
# 2026-10-29：拿 29 行「旧成品有译文、新 run 是『…』」给外部模型判，结论是
# `_sakura_bad` 的复读判据把**忠实重复**也吞了（原文重复、译文等量重复 → 判「放大」）。
# 这里把两类都冻住：下面 6 条**必须判干净**，下面 2 条**必须判脏**。
SAKURA_CLEAN = [
    ("有什么要来了 有什么要来了哦", "なんかなんか来ちゃう なんか来ちゃうよ"),
    ("为什么、为什么、为什么、为什么。", "なんで、なんで、なんで、なんで。"),
    ("我是M字小穴 我是M字小穴", "私はマゾマンコです 私はマゾマンコ"),
    ("很好很好，很好很好，很好很好，听我说。", "よしよし、よしよし、よしよーし ね、"),
    ("去了去了去了去了", "行く、行く、行く、行く"),
    ("啊啊、啊啊、啊啊、不行——啊啊——", "あぁ、あぁ、あぁ、ダメー あぁ、"),
    ("OK。", "オッケー。"),          # 片假名外来词译成拉丁，不能判 nolang
    # 混译：内容译出来了、专有名词用了拉丁 → 不能整行兜底成「…」
    # （实测 RJ1234441 Free Talk 6 句 20~40 字的闲聊就这么整句蒸发了）
    ("我这期吃的是薄荷口味的pino。", "今期食べちゃってて チョコミント味のピノ"),
    ("我们家啊，已经很Happy了！", "もううちはね、ハッピーです!"),
    ("我也想创作甜蜜蜜的作品，今后也请各位多多支持天神糖HIYOK",
     "ラブラブな作品もまた作りたいし 今後とも天神"),
    # 长句（≥24 字且 ≥4 汉字）里的重复是原文带来的，不能判 loop
    # （实测 RJ1228193 轨01 19:01.70 一句 80 字的射精描写被整句吞成「…」）
    ("唔，小弟弟又变得更硬了，色色的液体也沾在姐姐的手上，发出下流的声音哦——"
     "黏糊糊、撸撸、咕啾咕啾、撸撸，脑子里一片空白——",
     "ぐぅ、おーちんぽんも、また一段と固くなって、エッチのおしるも、姉上の手に絡んで、"
     "やらしい音してますよー、ぬちゃぬちゃ、しこしこ、ぐちゅぐちゅ、しこしこ、頭の中、真っ白ー。"),
]
SAKURA_DIRTY = [
    ("啊—啊—啊—啊—啊—啊—啊—啊—啊—啊—啊—啊—", "ジャメそしてそしてそして", "loop"),
    ("如果你赢了，我就会对你言听计从，如果你赢了，我就", "今日はいい天気ですね", "loop"),
    ("啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊", "あ゛あ゛あ゛あ゛あ゛あ゛あ゛あ゛あ゛", "loop"),
    # 整行英文（R153 那 115 行的形态）必须继续拦
    ("Okay, now I am going to show you", "はい、じゃあ今から見せてあげるね", "latin"),
]


def check_sakura_guard(verbose):
    """`translate._sakura_bad` 的判定护栏。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import translate as T
    bad = []
    for zh, ja in SAKURA_CLEAN:
        why, _ = T._sakura_bad(zh, ja)
        if why:
            bad.append((ja[:20], "应判干净，实际 %s" % why))
        elif verbose:
            print("   ✅ 干净 %s" % ja[:20])
    for zh, ja, kind in SAKURA_DIRTY:
        why, _ = T._sakura_bad(zh, ja)
        if why.split(":")[0] != kind:
            bad.append((ja[:20], "应判 %s，实际 %s" % (kind, why or "干净")))
        elif verbose:
            print("   ✅ 拦下 %s" % ja[:20])
    return bad


def main():
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    print("=== 0. cue 构建器验收（确定性/词数守恒/时间单调不重叠）===")
    bad0 = check_cues(verbose)
    ok0 = not any(bad0.values())
    print("   %s %s" % ("✅ 全过" if ok0 else "❌", bad0 if not ok0 else ""))

    print("\n=== 1. 检测器自检（8 个已确认泄漏必须全命中）===")
    bad1 = check_detector(verbose)
    print("   %d/%d 命中 %s" % (len(KNOWN_LEAKS) - len(bad1), len(KNOWN_LEAKS),
                              "✅" if not bad1 else "❌ " + str(bad1)))

    print("\n=== 2. 已知泄漏不得重现（cue 翻译改造的目标）===")
    bad2 = check_regressed(verbose)
    print("   %d/%d 已消失 %s" % (len(KNOWN_LEAKS) - len(bad2), len(KNOWN_LEAKS),
                                "✅" if not bad2 else "❌ " + str(bad2)))

    print("\n=== 3. 显示切分无损性（只切不重写）===")
    bad3 = check_render(verbose)
    print("   %d/%d 通过 %s" % (len(RENDER_CASES) - len(bad3), len(RENDER_CASES),
                              "✅" if not bad3 else "❌ " + str(bad3)))

    print("\n=== 3b. 新问题类检测器（漏译/拟声音译/cue 过大/气声跑偏/脑补名词）===")
    bad3b = check_new_rules(verbose)
    print("   %d/%d 命中 %s" % (len(NEW_RULE_CASES) - len(bad3b), len(NEW_RULE_CASES),
                              "✅" if not bad3b else "❌ " + str(bad3b)))

    print("\n=== 3c. 丢行硬门禁（成品行数必须 ≥ 输入 cue 数）===")
    bad3c = check_no_line_loss(verbose)
    print("   %d 轨检查完毕 %s" % (len(PILOT_TRACKS), "✅" if not bad3c else "❌ " + str(bad3c)))

    print("\n=== 3d. 非实义 span 遮蔽器（灵敏度+往返+无误遮）===")
    import mask_nonlexical as MM
    CASES_MASK = MM.CASES
    bad3d = check_mask(verbose)
    print("   %d 个用例 %s" % (len(CASES_MASK), "✅" if not bad3d else "❌ " + str(bad3d[:3])))

    print("\n=== 3d2. bad_cell 占位符守卫（跨句漂移兜底）===")
    bad3d2 = check_bad_cell(verbose)
    print("   %d/%d 通过 %s" % (len(BAD_CELL_CASES) - len(bad3d2), len(BAD_CELL_CASES),
                              "✅" if not bad3d2 else "❌ " + str(bad3d2[:3])))

    print("\n=== 3d2b. D9-3 纯非实义 cue 确定性直出 ===")
    bad3d2b = check_deterministic(verbose)
    print("   %d/%d 通过 %s" % (len(DET_CASES) - len(bad3d2b), len(DET_CASES),
                              "✅" if not bad3d2b else "❌ " + str(bad3d2b[:3])))

    print("\n=== 3d3. D10 回退阶梯：单条重试的解析（mock 模型回复）===")
    bad3d3 = check_retry_parse(verbose)
    print("   %d/%d 通过 %s" % (len(RETRY_CASES) - len(bad3d3), len(RETRY_CASES),
                              "✅" if not bad3d3 else "❌ " + str(bad3d3[:3])))

    print("\n=== 3d4. D5 定向重译：后检测触发后的单句重写 ===")
    bad3d4 = check_targeted(verbose)
    print("   %d/%d 通过 %s" % (len(TARGETED_CASES) - len(bad3d4), len(TARGETED_CASES),
                              "✅" if not bad3d4 else "❌ " + str(bad3d4[:3])))

    print("\n=== 3e. 源语驱动的称谓修复（GPT 第八轮 Q4）===")
    bad3e = check_term_repair(verbose)
    print("   %d/%d 通过 %s" % (len(TR_CASES) - len(bad3e), len(TR_CASES),
                              "✅" if not bad3e else "❌ " + str(bad3e[:2])))

    print("\n=== 3e2. 实体账本判据（作品名+Tag→称谓族）===")
    bad3e2 = check_ledger(verbose)
    print("   %d/%d 通过 %s" % (len(LEDGER_CASES) - len(bad3e2), len(LEDGER_CASES),
                               "✅" if not bad3e2 else "❌ " + str(bad3e2[:2])))

    print("\n=== 3f. 归属结构层 hint + 后检测（GPT 第九轮 Q1）===")
    import pragmatics_hints as PH
    bad3f = []
    for ja, want in PH.CASES:
        h = PH.cue_hint(ja)
        got = h["act"] if h else None
        if got != want:
            bad3f.append(("hint", ja[:26], got, want))
            if verbose:
                print("   ✗ hint %r → %s（期望 %s）" % (ja[:30], got, want))
    for ja, zh, want in PH.PAIR_CASES:
        got = {r[1] for r in PH.check_pair(ja, zh)}
        if got != want:
            bad3f.append(("pair", ja[:26], sorted(got), sorted(want)))
            if verbose:
                print("   ✗ pair %r/%r → %s（期望 %s）" % (ja[:22], zh[:16], sorted(got), sorted(want)))
    for ja in PH.NEGATIVE_JA:
        h = PH.cue_hint(ja)
        if h and h["act"] in ("listener_address", "speaker_denial"):
            bad3f.append(("neg", ja[:26], h["act"], "不报 listener/denial"))
    # P2 注入格式（GPT 第十轮 Q1）
    _fmt = PH._selftest_format()
    if _fmt:
        bad3f.append(("format-P2", "", _fmt, 0))
    _n3f = len(PH.CASES) + len(PH.PAIR_CASES) + len(PH.NEGATIVE_JA) + PH.FORMAT_N
    print("   %d/%d 通过 %s" % (_n3f - len(bad3f), _n3f,
                              "✅" if not bad3f else "❌ " + str(bad3f[:3])))

    print("\n=== 3g. 纯标点丢句（2026-10-19）===")
    # 判据必须两侧同时成立：中文只剩标点 **且** 日文侧有实义字符。
    # 只判中文侧会把合法的「停顿行」也回退（那些行还兼作长静默处的清屏行）。
    import polish as PL
    from scan_violations import punct_only_loss
    PUNCT_CASES = [
        # (中文, 日文, bad_cell 期望, punct_only_loss 期望)
        ("？",   "あら", True, True),            # 实测 RJ9000001：初译「啊？」，成品只剩「？」
        ("…",   "え?誰がエロ魔人だ?", True, True),
        ("…",   "あ…", False, False),            # R33：'あ' 是纯气声 →「…」忠实翻译（docstring 一直承诺的豁免，10-19 老口径）
        ("…",   "…", False, False),             # 日文本身是停顿 → 忠实翻译，不能判坏
        ("…",   "", False, False),
        ("？？", "??", False, False),            # ASR 只有乱码 → 判不出来
        ("哈…", "…", False, False),             # 正常译文
        ("……", "、、うっ。", False, False),      # R33：'うっ' 是用力声（纯气声）→ 忠实翻译
        # R33 新增：纯气声/拟声短串豁免（ばー 呕吐声等，#34 accepted 边界 9 条同型）
        ("…",   "ばー", False, False),
        ("…",   "あぁん", False, False),
        ("…",   "おっ", False, False),
        ("…",   "ぅっ", False, False),
        # 反面：有实义内容的日文译成纯标点仍是丢句
        ("…",   "見ないで", True, True),
        ("…",   "うるさい", True, True),
    ]
    bad3g = []
    for zh, ja, want_bad, want_pol in PUNCT_CASES:
        gb, gp = PL.bad_cell(zh, ja), punct_only_loss(ja, zh)
        if gb != want_bad:
            bad3g.append(("bad_cell", zh, ja, gb, want_bad))
        if gp != want_pol:
            bad3g.append(("punct_only_loss", zh, ja, gp, want_pol))
    print("   %d/%d 通过 %s" % (len(PUNCT_CASES) * 2 - len(bad3g), len(PUNCT_CASES) * 2,
                              "✅" if not bad3g else "❌ " + str(bad3g[:3])))

    print("\n=== 3h. 词级时间戳幻觉三门槛（2026-10-19）===")
    # 判据：幻觉词表/词跨崩塌 **且** 时长够长 **且** 字数很少。四个条件缺一不可，
    # 否则会误杀软语/长音/真实轨尾晚安。全库 22843 段实测只命中已知 21 条。
    import asr as ASR

    class _W:
        def __init__(self, d):
            self.start, self.end, self.word = d["s"], d["e"], d["w"]

    class _S:
        def __init__(self, text, start, end, words):
            self.text, self.start, self.end = text, start, end
            self.no_speech_prob, self.avg_logprob = 0.5, -0.3
            self.words = [_W(w) for w in words]

    def _wds(pairs):
        return [{"s": a, "e": b, "w": ""} for a, b in pairs]

    TS_CASES = [
        # (段, free talk, 期望命中)
        # A 幻觉词：独占 20s、7 字、词跨 15s → 抓
        (_S("おやすみなさい", 100, 120, _wds([(102, 117)])), False, True),
        # 同样的词但只有 1.5s、词跨 1.2s → 真实轨尾晚安，不抓
        (_S("おやすみなさい", 100, 101.5, _wds([(100.2, 101.4)])), False, False),
        # 词跨够但整句 40 字（完整的道晚安长句）→ 不抓
        (_S("今日は姉上の匂いを胸いっぱいに感じながらはいおやすみなさい", 100, 118,
            _wds([(101, 116)])), False, False),
        # B 崩塌：单字跨 25s、4 字 → 抓
        (_S("ん わ か っ", 100, 126, _wds([(100, 125), (125, 125.01),
                                          (125.01, 125.02), (125.02, 125.03)])), False, True),
        # 同样崩塌但只有 0.3s 的气声（零宽占比天然高）→ 不抓
        (_S("んあ", 100, 100.3, _wds([(100, 100.1), (100.1, 100.15)])), False, False),
        # free talk 里的感谢/晚安是真实台词 → A 类不判
        (_S("ご視聴ありがとうございました", 100, 122, _wds([(102, 118)])), True, False),
    ]
    bad3h = []
    for seg, ft, want in TS_CASES:
        got = bool(ASR.is_ts_hallucination(seg, ft))
        if got != want:
            bad3h.append((seg.text[:18], ft, got, want))
    print("   %d/%d 通过 %s" % (len(TS_CASES) - len(bad3h), len(TS_CASES),
                              "✅" if not bad3h else "❌ " + str(bad3h[:3])))

    print("\n=== 3i. LRC 清屏 spacer + cue_too_long 挂静音口径（2026-10-23 R23，坑 106）===")
    bad3i = check_spacer_linger(verbose)
    print("   %d/%d 通过 %s" % (len(SPACER_CASES) + len(LINGER_CASES) - len(bad3i),
                              len(SPACER_CASES) + len(LINGER_CASES),
                              "✅" if not bad3i else "❌ " + str(bad3i[:3])))

    print("\n=== 3k. 说话人名泄漏守卫 spk_leak（2026-10-23 R28，坑 107）===")
    bad3k = check_spk_leak(verbose)
    print("   %d/%d 通过 %s" % (len(SPK_LEAK_CASES) - len(bad3k), len(SPK_LEAK_CASES),
                              "✅" if not bad3k else "❌ " + str(bad3k[:3])))

    print("\n=== 3l. 人名前缀模式 strip_spk_leak（2026-10-23 R35，A2 乙方案）===")
    bad3l = check_strip_leak(verbose)
    print("   %d/%d 通过 %s" % (len(STRIP_LEAK_CASES) - len(bad3l), len(STRIP_LEAK_CASES),
                              "✅" if not bad3l else "❌ " + str(bad3l[:3])))

    print("\n=== 3m. 結婚/結構/血行 同音混淆 kekkon_confusion（2026-10-25 T6；2026-10-26 加段组边界）===")
    bad3m = check_kekkon(verbose)
    _n3m = len(KEKKON_CASES) + 2          # +2 = 段组边界用例及其反向自检
    print("   %d/%d 通过 %s" % (_n3m - len(bad3m), _n3m,
                              "✅" if not bad3m else "❌ " + str(bad3m)))

    print("\n=== 3n. errata 政策自检（2026-10-26 T7）===")
    # 子进程跑 errata_audit --test（全库语料一遍 ~3s）。退出码非 0 或解析不出
    # 「N/N 通过」即失败；失败断言名由 errata_audit 自己打到 stdout，转贴前几条。
    import subprocess
    _ea = os.path.join(os.path.dirname(os.path.abspath(__file__)), "errata_audit.py")
    # ⚠️ 依赖错字表（data/style/asr_errata.json）。公开仓库不含数据 →
    # 表不存在就**跳过**，不判失败（否则协作者 clone 下来一片红）。
    _et = os.path.join(ROOT, "data", "style", "asr_errata.json")
    if not os.path.exists(_et):
        SKIPPED.append(("errata_audit", "无错字表", "asr_errata.json 不存在"))
        print("   跳过（无错字表）")
        bad3n = []
        _n_ok = _n_all = 0
    else:
        _r = subprocess.run([sys.executable, _ea, "--test"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        _n_ok = _n_all = -1
        for _l in (_r.stdout or "").splitlines():
            m = re.match(r"\s*(\d+)/(\d+) 通过", _l)
            if m:
                _n_ok, _n_all = int(m.group(1)), int(m.group(2))
        if "SKIP 语料断言" in (_r.stdout or ""):
            # 语料不全（协作者的包只有一部作品）→ 只跑了结构断言，不算失败
            SKIPPED.append(("errata_audit", "语料不全",
                            [l.strip() for l in (_r.stdout or "").splitlines()
                             if "SKIP" in l][:1]))
            print("   跳过（语料不全，只跑了结构断言 %d/%d）" % (_n_ok, _n_all))
            bad3n = []
        else:
            bad3n = [] if (_r.returncode == 0 and _n_ok == _n_all and _n_all > 0) \
                else [("errata_audit", [l for l in (_r.stdout or "").splitlines() if "❌" in l][:3]
                       or (_r.stderr or "").strip()[-200:])]
            print("   %d/%d 通过 %s" % (_n_ok if _n_ok >= 0 else 0, _n_all if _n_all >= 0 else 0,
                                      "✅" if not bad3n else "❌ " + str(bad3n)))

    print("\n=== 3o. repair_overlay 状态机（2026-10-26 T8）===")
    bad3o = check_repair_overlay(verbose)
    _n3o = 12               # add 幂等/id/cue/t0/t1/proposed_zh/original_zh/两提示/confirm/缺锁/有锁+时间轴
    print("   %d/%d 通过 %s" % (_n3o - len(bad3o), _n3o,
                              "✅" if not bad3o else "❌ " + str(bad3o)))

    print("\n=== 3p. Sakura 兜底判定护栏（2026-10-29）===")
    bad3p = check_sakura_guard(verbose)
    _n3p = len(SAKURA_CLEAN) + len(SAKURA_DIRTY)
    print("   %d/%d 通过 %s" % (_n3p - len(bad3p), _n3p,
                              "✅" if not bad3p else "❌ " + str(bad3p)))

    print("\n=== 4. 全量泄漏告警（信息项）===")
    w = check_warnings(verbose)
    print("   告警 %d 处（含真重复/叠词，仅供参考）" % len(w))

    ok = (ok0 and not bad1 and not bad2 and not bad3 and not bad3b
          and not bad3c and not bad3d and not bad3d2 and not bad3e and not bad3e2
          and not bad3f and not bad3g and not bad3h and not bad3i and not bad3k and not bad3l
          and not bad3m and not bad3n and not bad3o and not bad3p)
    if SKIPPED:
        print("\n跳过 %d 项（缺数据，公开仓库 clone 下来的正常状态）：" % len(SKIPPED))
        for s in SKIPPED[:5]:
            print("   - %s / %s（%s）" % (s[0], s[1], s[2]))
        if len(SKIPPED) > 5:
            print("   … 其余 %d 项同类" % (len(SKIPPED) - 5))
    print("\n结果：%s" % ("✅ 门禁全过" if ok else "⚠️ 门禁未过"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
