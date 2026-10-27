# -*- coding: utf-8 -*-
r"""
把「人工翻译字幕」与「ASR 识别的日文原文」按时间戳对齐，产出平行语料 + 风格画像。

用法：
  python align_style.py <人工字幕目录> <ASR目录> [--style DIR] [--tag 名称] [--no-llm]

例：
  python align_style.py "$KIKOERU_ROOT/kikoerusubtitle/RJ1401520" "./asr_out_1401520" --tag RJ1401520

产出（默认写入 $KIKOERU_ROOT/.workbuddy/style）：
  parallel.json    累积的「日→中」平行句对（多次运行会合并去重）
  stats.json       客观统计（句末句号率、省略号率、语气词率、平均句长、高频词）
  style_guide.txt  大模型归纳的风格指南
  hard_rules.txt   由统计自动生成的硬性约束（比模型归纳可靠）
"""
import os
import re
import sys
import glob
import json
import random
import argparse
import urllib.request

try:
    import zhconv

    def to_zh_cn(t):
        return zhconv.convert(t, "zh-cn")
except ImportError:
    def to_zh_cn(t):
        return t

DEFAULT_STYLE = os.environ.get("KIKOERU_STYLE", r"$KIKOERU_ROOT\.workbuddy\style")
API = "http://127.0.0.1:8080/v1/chat/completions"

VTT_TS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})")
VTT_TS2 = re.compile(r"(\d{1,2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{1,2}):(\d{2})[.,](\d{3})")
LRC_TS = re.compile(r"^\[(\d{1,3}):(\d{1,2}(?:\.\d+)?)\](.*)$")
SUB_EXT = (".vtt", ".srt", ".lrc")


# ---------- 字幕解析 ----------
ENCS = ("utf-8-sig", "utf-8", "gbk", "big5")


def read_text(path):
    """人工字幕的编码很乱：有的是 UTF-8，有的是 GBK（尤其国内字幕组转存的）。
    写死 utf-8 会整篇 UnicodeDecodeError，所以逐个试。"""
    raw = open(path, "rb").read()
    for enc in ENCS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


# 字幕组水印 / 声明 / 制作名单。这类行不是台词，学进去会污染风格
# （实测出现过「laojiang个人汉化，仅在南+平台发布，搬运赚钱死个马先」）。
WM = re.compile(r"(个人汉化|字幕组|汉化组|仅供.{0,4}交流|请勿传播|搬运|转载|"
                r"仅在.{0,6}平台发布|请私信|反馈请|禁止.{0,4}转载|制作名单|"
                r"Staff|staff|翻訳[:：]|翻訳者|字幕[:：].{0,6}提供|"
                r"^\s*[★☆※]{2,}|^[-=]{4,}$)")


def is_watermark(text):
    return bool(WM.search(text or ""))


# free talk（声优谈话环节）的轨名特征
FT_NAME = re.compile(r"(free\s*talk|フリー\s*トーク|フリートーク|おまけ|あとがき|後記)",
                     re.IGNORECASE)


def dominant_speaker(segs, s, e):
    """该时间窗里说话时长占优的 SPEAKER_XX。没有做过 diarize 就返回空。"""
    acc = {}
    for a in segs:
        k = a.get("speaker")
        if not k:
            continue
        ov = min(e, a["end"]) - max(s, a["start"])
        if ov > 0:
            acc[k] = acc.get(k, 0) + ov
    if not acc:
        return ""
    return max(acc, key=lambda k: acc[k])


def read_sub(path):
    """-> [(start, end, text)]"""
    ext = os.path.splitext(path)[1].lower()
    out = []
    if ext == ".lrc":
        rows = []
        for line in read_text(path).splitlines():
            line = line.strip()
            m = LRC_TS.match(line)
            if m and not line.startswith("[ti:") and not line.startswith("[by:"):
                rows.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
        for i, (s, t) in enumerate(rows):
            e = rows[i + 1][0] if i + 1 < len(rows) else s + 3.0
            if t:
                out.append((s, e, t))
        return out
    # vtt / srt
    cur = None
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        m = VTT_TS.match(line)
        if m:
            h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, m.groups())
            cur = (h1 * 3600 + m1 * 60 + s1 + ms1 / 1000, h2 * 3600 + m2 * 60 + s2 + ms2 / 1000)
            continue
        if cur is None:
            m2 = VTT_TS2.match(line)
            if m2:
                m1, s1, ms1, m2_, s2, ms2 = map(int, m2.groups())
                cur = (m1 * 60 + s1 + ms1 / 1000, m2_ * 60 + s2 + ms2 / 1000)
            continue
        if re.match(r"^\d+$", line):
            continue
        out.append((cur[0], cur[1], line))
        cur = None
    return out


# ---------- 对齐 ----------
def _ja_in_window(asr, s, e):
    """取落在 [s,e] 窗口内的日文。优先用词级时间戳精确截取，
    避免把整段 30 秒的识别结果整块塞进一句字幕里。"""
    buf = []
    for a in asr:
        ws = a.get("words") or []
        if ws:
            for w in ws:
                ws_, we_ = w.get("s"), w.get("e")
                if ws_ is None or we_ is None:
                    continue
                if we_ <= s or ws_ >= e:
                    continue
                ov = min(e, we_) - max(s, ws_)
                if ov >= 0.5 * max(0.001, we_ - ws_):
                    buf.append(w["w"])
        else:
            if a["end"] <= s or a["start"] >= e:
                continue
            if min(e, a["end"]) - max(s, a["start"]) > 0:
                buf.append(a["text"])
    return "".join(buf).strip()


def _score_offset(subs, asr, off):
    """某个时间偏移下的对齐质量：命中率 + 长度合理性。"""
    hit, ok = 0, 0
    for s, e, zh in subs:
        ja = _ja_in_window(asr, s + off, e + off)
        if not ja:
            continue
        hit += 1
        # 中文长度应是日文的 0.25~1.6 倍，超出视为错配
        if 0.25 <= len(zh) / max(1, len(ja)) <= 1.6:
            ok += 1
    return ok * 10 + hit


def align(subs, asr):
    """先估计全局时间偏移（人工字幕与识别结果常有整体时移），再逐句对齐。"""
    if not subs or not asr:
        return []
    best_off, best_sc = 0.0, -1
    for i in range(-40, 41):
        off = i * 0.1
        sc = _score_offset(subs, asr, off)
        if sc > best_sc:
            best_sc, best_off = sc, off
    if abs(best_off) >= 0.1:
        print("    时间偏移校正 %+.1f 秒（命中评分 %d）" % (best_off, best_sc), flush=True)

    pairs = []
    for s, e, zh in subs:
        ja = _ja_in_window(asr, s + best_off, e + best_off)
        if not ja:
            continue
        pairs.append({"ja": ja, "zh": zh.strip(), "s": round(s, 2), "e": round(e, 2)})
    return pairs


def align_by_segment(subs, asr, min_ov=0.5, max_seg=18.0, min_ja=4):
    """按 **ASR 段** 聚合字幕行 → (完整日文句, 合并后的中文)。

    与 `align()` 的分工（2026-10-20 新增）：
      - `align()`       按**字幕行**取 ja（窗口内的词）→ ja 是**片段**。
                        适合学风格（标点/句长/措辞），ja 切碎不影响这些统计。
      - `align_by_segment()` 按**ASR 段**取 ja（完整句）→ 适合**术语挖掘**与**评测基准**。
                        术语必须是完整词，评测的输入也必须完整，否则：
                        `パイズリ` 会被切成 `パイズ`（glossary_v3 里就是这么来的）。

    合并规则：字幕行与 ASR 段的时间重叠 ≥ `min_ov`（默认一半）才收，按时间顺序拼 zh。
    跳过：段长 > `max_seg`（多句粘连，语义不单一）、ja 短于 `min_ja`、无对应字幕。
    """
    if not subs or not asr:
        return []
    best_off, best_sc = 0.0, -1
    for i in range(-40, 41):
        off = i * 0.1
        sc = _score_offset(subs, asr, off)
        if sc > best_sc:
            best_sc, best_off = sc, off
    if abs(best_off) >= 0.1:
        print("    时间偏移校正 %+.1f 秒（命中评分 %d）" % (best_off, best_sc), flush=True)

    # 第一步：给每个字幕行找「重叠最大」的 ASR 段（一行只能归一段）。
    # ⚠️ 不能按「重叠 ≥ 一半就都收」——横跨段边界的那一行会被相邻两段各收一次，
    #    导致上一段多出下一句的开头（实测：`我又做错了 明日，`）。
    assign = {}                      # 段索引 -> [(字幕起点, zh)]
    for s, e, zh in subs:
        ss, ee = s + best_off, e + best_off
        best_i, best_ov = -1, 0.0
        for i, a in enumerate(asr):
            s0, e0 = a.get("start"), a.get("end")
            if s0 is None or e0 is None:
                continue
            ov = min(e0, ee) - max(s0, ss)
            if ov > best_ov:
                best_ov, best_i = ov, i
        if best_i < 0 or best_ov <= 0:
            continue
        if best_ov < min_ov * max(0.001, ee - ss):
            continue
        assign.setdefault(best_i, []).append((ss, zh.strip()))

    # 第二步：合并「未成句」的段。whisper 按 30s 窗口/停顿分段，不按句法，
    # 所以单段常常停在「…して」「…の」上。把这类段与紧邻的下一段合并，直到出现句末。
    # 这是 segment 模式相对 line 模式的核心价值：产出**句子级**平行语料。
    SENT_END = re.compile(r"[。．.！!？?…♪♡♥」』）)ー~～っ]$")

    def merged_units():
        buf, ids = None, []
        for i, a in enumerate(asr):
            s0, e0 = a.get("start"), a.get("end")
            ja = (a.get("text") or "").strip()
            if s0 is None or e0 is None or len(ja) < min_ja:
                continue
            if buf is None:
                buf, ids = {"start": s0, "end": e0, "text": ja,
                            "speaker": a.get("speaker") or a.get("spk"),
                            "role": a.get("role")}, [i]
            else:
                gap = s0 - buf["end"]
                if gap > 0.6:                    # 停顿太久 = 另一句，不合并
                    yield buf, ids
                    buf, ids = {"start": s0, "end": e0, "text": ja,
                                "speaker": a.get("speaker") or a.get("spk"),
                                "role": a.get("role")}, [i]
                    continue
                buf["end"] = e0
                buf["text"] += ja
                ids.append(i)
            if SENT_END.search(buf["text"]):
                yield buf, ids
                buf, ids = None, []
        if buf is not None:
            yield buf, ids

    pairs = []
    for u, ids in merged_units():
        if u["end"] - u["start"] > max_seg:
            continue                     # 合并后过长（多句粘连）→ 语义不单一，跳过
        hit = []
        for i in ids:
            hit.extend(assign.get(i) or [])
        if not hit:
            continue
        zh = "".join(t for _, t in sorted(hit))
        if not zh:
            continue
        pairs.append({"ja": u["text"], "zh": zh,
                      "s": round(float(u["start"]), 2), "e": round(float(u["end"]), 2),
                      "lines": len(hit),
                      "spk": u.get("speaker"), "role": u.get("role")})
    return pairs


# ---------- 术语表挖掘 ----------
# (日文词, [候选中文译法])，按共现频率选出该译者真正用的那个
# ---- B6 用户裁定术语（2026-10-21，人工筛 126 条候选的采纳结果）----
# 语义：**用户拍板 > 语料统计**。build_glossary 里命中这里就直接用固定译法，
# 不再按共现投票（否则「赤ちゃん→小宝宝」会被语料多数「婴儿」翻掉）。
# 来源：.workbuddy/tmp/term_review/adopted.json（27 条，含 キス 由「接吻」改判「亲吻」）。
# ⚠️ 改这里之后必须重跑 build_library.py，否则 style/glossary.json 不更新。
# 退役钉法（2026-10-25）：这些词在语料里「钉的那个译法」自己都不到一半
# （glossary 的 rate 字段 0.11–0.46），当「固定译法」注入会误导模型
# （例：わかる→明白，语料只有 11% 这么译，成品实际用「知道」）。
# 判据与原因见 `.workbuddy/style/retired_pins.json`；重建库时跳过它们。
RETIRED_PINS = {"わかる", "出る", "エッチ", "やばい", "もっと", "我慢", "恥ずかしい", "嬉しい"}

USER_TERMS = {
    # 気持ち 一族（用户原话：都表示「舒服」）
    "気持ち": "舒服", "気持ちいい": "舒服", "気持ちよい": "舒服",
    # 身体/性（与 term_lock 既有锁组一致）
    "おっぱい": "胸部", "ちんぽ": "肉棒", "ちんちん": "鸡鸡",
    # B3（2026-10-23 用户裁定）：精子 → 精液（与 ザーメン/精液 同译，语料 84/239 已如此）
    "精子": "精液",
    # R45（2026-10-23）：B4「ちんぽ 族统一肉棒」的**表记变体**——ASR 会写成
    # おちんぽ / チンポ / ちんこ / チンコ，原表只收了 `ちんぽ` 一个形，
    # 变体走不到注入链 → 成品里出现「小鸡」「丁丁」「小弟弟」（R44 实测 3 条）。
    "おちんぽ": "肉棒", "チンポ": "肉棒", "ちんこ": "肉棒", "チンコ": "肉棒",
    # 称谓/人物
    "赤ちゃん": "小宝宝", "ママ": "妈妈", "教え子": "学生", "アイドル": "偶像",
    # 物/动作（名词性，安全）
    "おもちゃ": "玩具", "ローション": "润滑液", "キス": "亲吻", "お仕置き": "惩罚",
    # 形容词/动词（只作 prompt 引导，不做确定性替换——语境会变）
    "かわいい": "可爱", "甘える": "撒娇", "わかる": "明白", "嬉しい": "高兴",
    "大きい": "大", "恥ずかしい": "害羞", "感じる": "感觉", "入れる": "进去",
    "出る": "出来", "女性": "女性", "身体": "身体", "震える": "颤抖",
    "楽しむ": "享受", "とろける": "融化",
}

GLOSSARY_SEED = [
    # ---- B6 新增（2026-10-21，用户裁定；候选表用于 hit/rate 统计）----
    ("気持ちよい", ["舒服", "舒服的", "感觉好"]),
    ("赤ちゃん", ["小宝宝", "婴儿", "宝宝"]),
    ("おもちゃ", ["玩具", "玩物"]),
    ("かわいい", ["可爱", "好可爱"]),
    ("甘える", ["撒娇", "依赖"]),
    ("わかる", ["明白", "知道"]),
    ("嬉しい", ["高兴", "开心"]),
    ("大きい", ["大", "变大", "很大"]),
    ("アイドル", ["偶像", "爱豆"]),
    ("ちんちん", ["鸡鸡", "小弟弟", "肉棒"]),
    # R45：ちんぽ 族的表记变体（同 USER_TERMS，候选表用于 hit/rate 统计）
    ("おちんぽ", ["肉棒", "小鸡", "小弟弟"]),
    ("チンポ", ["肉棒", "小鸡", "小弟弟"]),
    ("ちんこ", ["肉棒", "小鸡", "丁丁"]),
    ("チンコ", ["肉棒", "小鸡", "丁丁"]),
    ("恥ずかしい", ["害羞", "不好意思"]),
    ("教え子", ["学生", "弟子"]),
    ("感じる", ["感觉", "感到"]),
    ("ママ", ["妈妈", "妈咪"]),
    ("入れる", ["进去", "放入"]),
    ("出る", ["出来", "流出"]),
    ("ローション", ["润滑液", "润滑剂", "润滑油"]),
    ("女性", ["女性", "女人"]),
    ("身体", ["身体", "身子"]),
    ("震える", ["颤抖", "发抖"]),
    ("楽しむ", ["享受", "好好享受"]),
    ("とろける", ["融化", "化开"]),
    ("お仕置き", ["惩罚", "处罚"]),
    ("お兄ちゃん", ["老哥", "哥哥", "大哥", "兄长", "欧尼酱"]),
    ("お兄", ["老哥", "哥哥", "大哥", "兄长"]),
    ("兄ちゃん", ["老哥", "哥哥", "大哥"]),
    ("先輩", ["前辈", "学长", "学姐", "前辈"]),
    ("先生", ["老师", "先生"]),
    ("お前", ["你", "你这家伙"]),
    ("あんた", ["你"]),
    ("あなた", ["你"]),
    ("お姉ちゃん", ["姐姐", "老姐"]),
    ("ちんこ", ["鸡鸡", "肉棒", "阴茎", "小弟弟"]),
    ("ちんぽ", ["鸡鸡", "肉棒", "阴茎"]),
    ("おちんちん", ["鸡鸡", "小鸡鸡"]),
    ("ペニス", ["鸡鸡", "肉棒"]),
    ("おまんこ", ["小穴", "蜜穴", "下面", "私处"]),
    ("まんこ", ["小穴", "蜜穴"]),
    ("おっぱい", ["胸部", "胸部", "咪咪", "奶奶"]),
    ("乳首", ["乳头", "奶头"]),
    ("クリ", ["豆豆", "阴蒂"]),
    ("クリトリス", ["阴蒂", "豆豆"]),
    ("勃起", ["勃起", "硬了"]),
    ("射精", ["射精", "射出来"]),
    ("精子", ["精液", "精子"]),
    ("フェラ", ["口交", "吹"]),
    ("手コキ", ["手交", "用手"]),
    ("騎乗位", ["骑乘位", "女上位"]),
    ("バック", ["后入", "背后位"]),
    ("セックス", ["做爱", "性交"]),
    ("エッチ", ["做爱", "亲热"]),
    ("オナニー", ["自慰", "打飞机"]),
    ("オナホ", ["飞机杯", "自慰杯"]),
    ("ローター", ["跳蛋", "按摩棒"]),
    ("コンドーム", ["避孕套", "套套"]),
    ("ゴム", ["套套", "避孕套"]),
    ("キス", ["接吻", "亲"]),
    ("挿入", ["插入", "进去"]),
    ("気持ちいい", ["舒服", "好舒服"]),
    ("気持ち", ["舒服", "感觉"]),
    ("すごい", ["好厉害", "超", "好"]),
    ("好き", ["喜欢", "好喜欢"]),
    ("だめ", ["不行", "不可以"]),
    ("やばい", ["糟了", "不妙", "好棒"]),
    ("痛い", ["痛", "好痛"]),
    ("我慢", ["忍耐", "忍住"]),
    ("もっと", ["再", "更多", "还要"]),
    ("本当", ["真的", "实在"]),
    ("ほんと", ["真的"]),
    ("なんで", ["为什么", "怎么"]),
    ("ありがとう", ["谢谢", "多谢"]),
    ("ごめん", ["对不起", "抱歉"]),
    ("すみません", ["不好意思", "对不起"]),
]


def build_glossary(pairs, min_hit=3, min_ratio=0.2):
    """共现统计：日文词 X 出现时，该译者最常用哪个中文词？
    只保留证据足够多（>=min_hit 次）且占比够高（>=min_ratio）的条目。"""
    out = {}
    for ja_t, cands in GLOSSARY_SEED:
        if ja_t in RETIRED_PINS:          # 退役钉法：不注入、不统计（见文件顶部注释）
            continue
        # B6 用户裁定优先（语料统计只补 hit 数，不参与选词）
        _ut = USER_TERMS.get(ja_t)
        cnt = {c: 0 for c in cands}
        n = 0
        for p in pairs:
            if ja_t in p["ja"]:
                n += 1
                for c in cands:
                    if c in p["zh"]:
                        cnt[c] += 1
        if n < min_hit:
            continue
        if _ut:
            out[ja_t] = {"zh": _ut, "hit": n, "rate": round(cnt.get(_ut, 0) / n, 2),
                         "source": "user_b6"}
            continue
        best, best_n = max(cnt.items(), key=lambda x: x[1])
        if best_n == 0 or best_n / n < min_ratio:
            continue
        out[ja_t] = {"zh": best, "hit": n, "rate": round(best_n / n, 2)}
    # 同义合并：多个日文写法映射到同一中文时都保留，但去掉中文重复的弱证据项
    return dict(sorted(out.items(), key=lambda x: -x[1]["hit"]))


def key_of(path):
    """字幕文件名去掉字幕扩展名 -> 应等于音频文件名（含音频扩展名）"""
    return os.path.basename(path)[: -4] if os.path.splitext(path)[1].lower() == ".vtt" \
        else os.path.splitext(os.path.basename(path))[0]


# ---------- 统计 ----------
TERMS = ("老哥", "哥哥", "前辈", "姐姐", "哦", "呢", "吗", "吧", "啦", "啊", "嗯",
         "鸡鸡", "胸部", "射", "小", "好舒服", "喜欢")
# 省略号：人工稿可能用三点「…」也可能用六连点「……」，两种都要算，
# 否则遇到只用三点的译者会统计出 0%（踩过这个坑）
ELLIPSIS = re.compile(r"…|\.{2,}")


def compute_stats(pairs):
    n = max(1, len(pairs))
    e6 = sum(1 for p in pairs if "……" in p["zh"])
    e3 = sum(1 for p in pairs if ELLIPSIS.search(p["zh"]) and "……" not in p["zh"])
    st = {
        "total": len(pairs),
        "avg_zh_len": round(sum(len(p["zh"]) for p in pairs) / n, 1),
        "avg_ja_len": round(sum(len(p["ja"]) for p in pairs) / n, 1),
        "end_period": sum(1 for p in pairs if p["zh"].rstrip().endswith("。")),
        "cn_ellipsis": e6 + e3,
        "ellipsis6": e6,
        "ellipsis3": e3,
        "comma": sum(1 for p in pairs if "，" in p["zh"]),
        "dunhao": sum(1 for p in pairs if "、" in p["zh"]),
        "excl": sum(1 for p in pairs if "！" in p["zh"]),
        "question": sum(1 for p in pairs if "？" in p["zh"]),
        "ne": sum(1 for p in pairs if "呢" in p["zh"]),
        # 句内用空格分隔（用户规范要求，取代逗号）
        "space": sum(1 for p in pairs if " " in p["zh"]),
        # 声优 free talk 的句子数
        "freetalk": sum(1 for p in pairs if p.get("type") == "freetalk"),
        # 识别出的日文里带问号的比例。这个数远低于中文问号率，
        # 就证明"Whisper 不标问号"是疑问句被译成陈述句的根因。
        "ja_question": sum(1 for p in pairs if "？" in p["ja"] or "?" in p["ja"]),
    }
    st.update({
        "pct_period": round(100 * st["end_period"] / n, 1),
        "pct_ellipsis": round(100 * st["cn_ellipsis"] / n, 1),
        "pct_ne": round(100 * st["ne"] / n, 1),
        "pct_dunhao": round(100 * st["dunhao"] / n, 1),
        "pct_question": round(100 * st["question"] / n, 1),
        "pct_ja_question": round(100 * st["ja_question"] / n, 1),
        "pct_space": round(100 * st["space"] / n, 1),
        "pct_comma": round(100 * st["comma"] / n, 1),
        # 该译者更常用三点还是六连点，决定输出时归一成哪种
        "ellipsis_style": ("……" if e6 >= e3 else "…"),
    })
    st["terms"] = {w: sum(1 for p in pairs if w in p["zh"]) for w in TERMS}
    st["top_terms"] = dict(sorted(((k, v) for k, v in st["terms"].items() if v > 0),
                                  key=lambda x: -x[1])[:10])
    return st


def user_period_policy():
    """从 `user_rules.txt` 读「句末句号」策略：'period' / 'no-period' / None（没规定）。

    用户规范优先级最高（见 style-learn/SKILL.md「语料来源混杂时怎么办」），
    所以统计生成的硬规则必须**让位**给它，不能反过来把用户的规范改掉。
    背景：人工语料是旧「无句号」时代的产物，统计上只有 ~2% 句末带句号；
    但 2026-10-08 用户改判为「句末用句号」（Sakura 带句号更自然），
    若这里仍按统计输出「不加句号」，就会和 user_rules 打架。
    """
    try:
        txt = open(os.path.join(DEFAULT_STYLE, "user_rules.txt"), encoding="utf-8").read()
    except Exception:
        return None
    for line in txt.splitlines():
        if "句末" in line and "句号" in line:
            if any(k in line for k in ("不加", "不要", "不用", "别加", "勿加")):
                return "no-period"
            if any(k in line for k in ("加", "用", "收束")):
                return "period"
    return None


def gen_hard_rules(st):
    """从统计自动生成硬性约束 —— 数字比模型归纳可靠得多。"""
    n = st["total"]
    L = []
    L.append("- **输出一律用简体中文**。人工原稿可能是繁体，语料已统一转简体，不要输出繁体字")
    pol = user_period_policy()
    if pol == "period":
        L.append("- **句末用句号「。」收束**（用户规范，优先于统计）。"
                 "人工译文只有 %.0f%% 以「。」结尾，那是旧「无句号」时代人工稿的产物，**不作为目标**；"
                 "疑问句用「？」、感叹用「！」、拖音用「…」。" % st["pct_period"])
    else:
        L.append("- 句末一律**不加句号**：%d 句人工译文中只有 %d 句（%.0f%%）以「。」结尾"
                 % (n, st["end_period"], st["pct_period"]))
    # 省略号形态跟随该译者的真实习惯（三点还是六连点），不要硬编码成六连点
    mk = st.get("ellipsis_style", "……")
    L.append("- 省略号统一用「%s」（%d 句里 %d 句这样用），不要混用另一种形态"
             % (mk, n, st.get("ellipsis6", 0) if mk == "……" else st.get("ellipsis3", 0)))
    if st["pct_ellipsis"] >= 40:
        L.append("- **句末省略号是该译者最突出的特征**：%d 句里 %d 句（%.0f%%）带省略号。"
                 % (n, st["cn_ellipsis"], st["pct_ellipsis"]))
        L.append("  原文有拖音（ー、〜、…、ぁー、えー）、喘息、犹豫、娇嗔、语塞时，句末必须加「%s」；" % mk)
        L.append("  一般对白若语气延宕也倾向加。不要吝啬使用，但也别每句都加。")
    else:
        # 该译者的省略号大量出现在**句中**停顿处（如「嗯…几乎都客满了」「不过嘛…就算是」），
        # 只说"句末可用"会严重低估使用率（实测只写出 6% vs 人工 33%）。
        L.append("- 省略号使用率 %.0f%%，用于表现拖音、喘息、犹豫、语塞" % st["pct_ellipsis"])
        L.append("  **句中停顿处也要用**：该译者常把「%s」插在句子中间表现语气中断"
                 % st.get("ellipsis_style", "…"))
        L.append("  （例：「嗯%s几乎都客满了」「不过嘛%s就算是」「而且呢，就算不做那种事%s」），"
                 % (mk, mk, mk))
        L.append("  不要只在句末加，但也别每句都加。")
    # 疑问语气：Whisper 输出基本不带「？」，这是疑问句被译成陈述句的根因
    if st.get("pct_question", 0) >= 3:
        L.append("- **疑问句必须译成疑问句，句末用「？」**：人工译文 %.0f%% 带「？」，" % st["pct_question"])
        L.append("  但识别出的日文只有 %.0f%% 带「？」——不能靠原文的问号判断是不是疑问，"
                 % st.get("pct_ja_question", 0))
        L.append("  要看句末助词（か/かな/かしら/の/ん/っけ/よね/でしょ/でしょう/ですか/ますか）")
        L.append("  和上下文是否发问、反问、确认。别把疑问句译成陈述句。")
    # free talk（声优谈话环节）语体和角色台词完全不同，单独给规则
    if st.get("freetalk", 0) >= 30:
        L.append("- **free talk（声优谈话环节）要换成日常对话体**，共 %d 句这类语料："
                 % st["freetalk"])
        L.append("  声优会在里面自称名字、聊录音过程、感谢听众，是真实的日常闲谈，")
        L.append("  不是角色台词。不要用角色口吻（别撒娇、别用性描写的腔调），")
        L.append("  用自然随意的口语，像博主对着麦克风聊天。作品名、角色名照实译。")
    L.append("- 平均句长 %.1f 字，单行控制在 10-22 字，不要过度拆句" % st["avg_zh_len"])
    if st["pct_dunhao"] <= 5:
        # ⚠️ 原先这里写「并列一律用中文逗号」，与文件顶部的**用户规范**
        # （「句内分隔一律用空格，不要用逗号」）直接打架，导致 prompt 自相矛盾。
        # 用户规范优先级最高，这里必须与它保持一致：并列/短促停顿一律用空格。
        L.append("- **几乎不用顿号「、」**（%.0f%%）：并列与短促停顿一律用**半角空格**分隔，"
                 "不要改成逗号（用户规范：句内分隔用空格不用逗号）"
                 % st["pct_dunhao"])
    if st["pct_ne"] <= 20:
        L.append("- **语气词绝不能滥用**：%d 句里只有 %d 句（%.0f%%）用「呢」，全文控制在 %.0f%% 以内。"
                 % (n, st["ne"], st["pct_ne"], min(30, st["pct_ne"] * 2)))
        L.append("  ね 不一定要译成「呢」，优先考虑「啊」「吧」「哦」或直接省略；")
        L.append("  原文句末是 だ/た/です/ます/だった 等陈述收尾时，中文句末**不要加任何语气词**")
    L.append("- 句末语气词**只用一个**，禁止叠加（「哦呢」「啊吧」「呢啊」一律不允许）")
    top = st.get("top_terms", {})
    if top:
        L.append("- 该译者的高频词：%s（模仿他的用词偏好）"
                 % "、".join("%s(%d)" % (k, v) for k, v in list(top.items())[:8]))
    L.append("- 身体部位与性描写用词直白（如 鸡鸡 / 胸部 / 射），不要文雅化或打码")
    L.append("- 语气词用「呢、吧、哦、啊、啦」，避免「呀、嘛、喔」等他不用的写法")
    return "\n".join(L)


def call_llm(messages, max_tokens=1800, temperature=0.4):
    payload = {"model": "qwen", "messages": messages, "temperature": temperature,
               "max_tokens": max_tokens, "top_p": 0.9}
    req = urllib.request.Request(API, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]


GUIDE_PROMPT = """下面是一位译者翻译日语音声作品字幕的真实样例，格式为「原文」与「人工译文」对照，共 %d 组：

%s

下面是**对这位译者全部 %d 句译文做程序统计得到的客观事实**，你的归纳必须与之完全一致，不得相悖：
- 句末以「。」结尾的比例：%.0f%%
- 使用省略号的比例：%.0f%%
- 使用语气词「呢」的比例：%.0f%%
- 使用顿号「、」的比例：%.0f%%
- 平均句长：%.1f 字

请归纳这位译者的翻译风格，写成一份可指导他人模仿的《风格指南》，必须覆盖：
1. 称呼译法（お兄ちゃん/兄/先輩/君/お前 等）
2. 句末语气词（ね/よ/な/わ/ぞ/かな/の/かしら 等）怎么落地成中文
3. 标点习惯：省略号用「……」还是「…」、句末加不加句号、叹号问号用法
   （句末句号一项：**若用户规范已规定，一律照用户规范写**；否则照上面的统计数字写，统计为 0%% 时就写「句末一律不加句号」）
4. 断句与句长：是否把长句拆短、平均句长（照统计数字写）
5. 用词风格：口语/直白程度，身体部位、性描写、拟声词的译法
6. 对原文语气填充词、重复、结巴的处理（保留还是省略）

只输出条目化的风格指南，不要复述样例，不要客套话。"""


def guide_prompt_args(sample, sample_txt, stats):
    """GUIDE_PROMPT 有 8 个占位符（%d/%s/%d + 4 个 %.0f + %.1f），
    少传一个就是 TypeError。两处调用（align_style / build_library）都走这里，
    别再各自手写一遍元组——之前就是两处同时漏传而崩的。"""
    return (len(sample), sample_txt,
            stats.get("total", 0),
            stats.get("pct_period", 0),
            stats.get("pct_ellipsis", 0),
            stats.get("pct_ne", 0),
            stats.get("pct_dunhao", 0),
            stats.get("avg_zh_len", 0))


def sanitize_guide(guide, stats):
    r"""用客观统计校正模型归纳里与事实冲突的行。

    模型常凭"中文写作常识"臆断（例如人工一句句号都没用，它却写"句末加句号"），
    这类结论会写进 prompt 误导后续润色，必须按统计数字强行纠正。
    """
    total = stats.get("total", 0)
    p_period = stats.get("pct_period", 0)
    pol = user_period_policy()
    # 省略号形态：用户规范（user_rules.txt）优先于统计，见该文件「三点「…」不是六连点」。
    mk = stats.get("ellipsis_style", "…")
    out = []
    for line in guide.splitlines():
        s = line.strip()
        # 句号：统计上几乎不用句号，却写成"加句号" → 纠正。
        # 但若用户规范明确要求「句末用句号」，就反过来写「用句号」——不能留半句矛盾。
        if pol == "period" and re.search(r"句末[^，。；]{0,8}(加|使用|帶|带|用|補|补)[^，。；]{0,4}句号|以句号结尾", s):
            indent = line[:len(line) - len(line.lstrip())]
            out.append("%s- 句末一律用句号「。」收束（用户规范，优先于统计；"
                       "疑问句用「？」、感叹用「！」、拖音用「…」）" % indent)
            continue
        if pol != "period" and p_period <= 5 and re.search(r"句末[^，。；]{0,8}(加|加[上個个]|使用|帶|带|用|補|补)[^，。；]{0,4}句号|以句号结尾|句末加。", s):
            indent = line[:len(line) - len(line.lstrip())]
            out.append("%s- 句末一律不加句号（统计：%d 句人工译文中只有 %.0f%% 以「。」结尾）"
                       % (indent, total, p_period))
            continue
        # 省略号：跟随用户规范/统计的真实形态，别硬编码成六连点
        if re.search(r"省略号[^，。；]{0,6}(用|使用|是|为)[^，。；]{0,3}「?[.…]", s):
            indent = line[:len(line) - len(line.lstrip())]
            out.append("%s- 省略号统一用「%s」，不要混用另一种形态（用户规范优先于统计）"
                       % (indent, mk))
            continue
        out.append(line)
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sub_dir")
    ap.add_argument("asr_dir")
    ap.add_argument("--style", default=DEFAULT_STYLE)
    ap.add_argument("--tag", default="")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--mode", choices=("line", "segment"), default="line",
                    help="line=按字幕行对齐（学风格，ja 是片段）；"
                         "segment=按 ASR 段对齐（完整句，用于术语挖掘/评测基准）")
    ap.add_argument("--out", default="", help="输出文件名（默认 parallel.json / parallel_seg.json）")
    a = ap.parse_args()

    os.makedirs(a.style, exist_ok=True)
    P_PARALLEL = os.path.join(a.style, a.out or
                              ("parallel_seg.json" if a.mode == "segment" else "parallel.json"))
    P_STATS = os.path.join(a.style, "stats.json")
    P_GUIDE = os.path.join(a.style, "style_guide.txt")
    P_HARD = os.path.join(a.style, "hard_rules.txt")

    subs = sorted([p for p in glob.glob(os.path.join(a.sub_dir, "*")) if p.lower().endswith(SUB_EXT)])
    asr_files = sorted(glob.glob(os.path.join(a.asr_dir, "*.json")))
    print("人工字幕 %d 个 / 识别结果 %d 个" % (len(subs), len(asr_files)))

    asr_by_name = {}
    for p in asr_files:
        d = json.load(open(p, encoding="utf-8"))
        asr_by_name[os.path.basename(p)[: -len(".json")]] = d
        asr_by_name[os.path.splitext(os.path.basename(p))[0]] = d

    new_pairs = []
    for sp in subs:
        base = key_of(sp)
        d = asr_by_name.get(base)
        if d is None:
            # 退而求其次：按文件名包含关系匹配
            hits = [v for k, v in asr_by_name.items() if base[:8] and base[:8] in k]
            d = hits[0] if len(hits) == 1 else None
        if d is None:
            print("  [跳过] 无对应识别结果: %s" % base[:50])
            continue
        rows = read_sub(sp)
        n_wm = sum(1 for _, _, t in rows if is_watermark(t))
        # 字幕组水印/制作名单不是台词，学进去只会污染风格
        rows = [(s, e, t) for s, e, t in rows if not is_watermark(t)]
        pr = (align_by_segment(rows, d["segments"]) if a.mode == "segment"
              else align(rows, d["segments"]))
        # 声优 free talk（谈话环节）单列：语体是日常对话，和角色台词不是一回事
        is_ft = bool(FT_NAME.search(base))
        for p in pr:
            p["src"] = a.tag or os.path.basename(a.sub_dir)
            if is_ft:
                p["type"] = "freetalk"
            # 这句话是谁说的（声纹聚类 + 角色判明的结果），用于学"某角色的口吻"
            spk = dominant_speaker(d["segments"], p["s"], p["e"])
            if spk:
                p["spk"] = spk
                role = (d.get("roles") or {}).get(spk)
                if role:
                    p["role"] = role.get("name") or role.get("role") or ""
        print("  %-46s 人工 %3d 行(剔水印%d) -> 对齐 %3d 对%s"
              % (base[:44], len(rows) + n_wm, n_wm, len(pr), "  [free talk]" if is_ft else ""))
        new_pairs.extend(pr)

    # 质量过滤
    good, seen = [], set()
    for p in new_pairs:
        # 人工稿可能是繁体（如台译本），统一转简体，避免把繁体习惯带进输出
        ja, zh = p["ja"], to_zh_cn(p["zh"])
        if not ja or not zh or ja in seen:
            continue
        if not (3 <= len(zh) <= 45) or not (3 <= len(ja) <= 80):
            continue
        if re.search(r"[〈<\[\(]", zh):
            continue
        # 长度合理性：中文译文长度应在日文原文的 0.25~1.6 倍之间，
        # 超出说明时间窗对错了（如把整段台词塞给一句字幕）
        r = len(zh) / max(1, len(ja))
        if r < 0.25 or r > 1.6:
            continue
        seen.add(ja)
        good.append(p)
    print("\n本次可用平行句对: %d / %d" % (len(good), len(new_pairs)))

    # 与已有语料合并去重（累积学习）
    old = []
    if os.path.exists(P_PARALLEL):
        old = json.load(open(P_PARALLEL, encoding="utf-8")).get("pairs", [])
    have = set(p["ja"] for p in old)
    merged = old + [p for p in good if p["ja"] not in have]
    print("累积语料: %d 组（新增 %d）" % (len(merged), len(merged) - len(old)))

    stats = compute_stats(merged)
    json.dump({"pairs": merged, "stats": stats}, open(P_PARALLEL, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(stats, open(P_STATS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\n=== 客观统计 ===")
    print("  句对 %d | 平均句长 %.1f 字 | 句末句号 %.0f%% | 省略号 %.0f%% | 「呢」%.0f%% | 顿号 %.0f%%"
          % (stats["total"], stats["avg_zh_len"], stats["pct_period"], stats["pct_ellipsis"],
             stats["pct_ne"], stats["pct_dunhao"]))
    print("  高频词:", stats["top_terms"])

    # 术语表：该译者的固定译法，保证前后一致、不机械
    gloss = build_glossary(merged)
    gp = os.path.join(a.style, "glossary.json")
    json.dump(gloss, open(gp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n=== 术语表（该译者的固定译法，共 %d 条）===" % len(gloss))
    for k, v in list(gloss.items())[:30]:
        print("  %-12s -> %-8s  (%d 次, 占比 %.0f%%)" % (k, v["zh"], v["hit"], v["rate"] * 100))

    hard = gen_hard_rules(stats)
    # 用户指定的统一规范放在最前面：语料来自多位译者，统计出来的数字会互相打架，
    # 只有这份规范是最终标准，统计只能补充它没覆盖的部分。
    p_user = os.path.join(a.style, "user_rules.txt")
    if os.path.exists(p_user):
        ur = open(p_user, encoding="utf-8").read().strip()
        if ur:
            hard = ("# 以下为用户指定的统一规范，优先级最高，与后面的统计结论冲突时以本节为准\n"
                    + ur + "\n\n" + hard)
    if gloss:
        lines = ["- **术语必须统一**（来自真实译文共现统计，同一日文词全篇只能这样译）："]
        for k, v in list(gloss.items())[:40]:
            lines.append("  %s -> %s" % (k, v["zh"]))
        hard = hard + "\n" + "\n".join(lines)
    open(P_HARD, "w", encoding="utf-8").write(hard + "\n")
    print("\n=== 硬性约束（自动从统计生成）===\n" + hard)

    if a.no_llm:
        print("\n(--no-llm) 跳过风格指南归纳")
        return
    try:
        urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=8).read()
    except Exception as e:
        print("\nllama-server 未就绪，跳过风格指南归纳：%s" % e)
        return

    random.seed(11)
    sample = random.sample(merged, min(80, len(merged)))
    sample_txt = "\n".join("%d|%s\n  译：%s" % (i + 1, p["ja"], p["zh"]) for i, p in enumerate(sample))
    print("\n归纳风格指南中 ...", flush=True)
    guide = call_llm([{"role": "user",
                       "content": GUIDE_PROMPT % guide_prompt_args(sample, sample_txt, stats)}])
    print("\n===== 风格指南 =====\n" + guide)
    with open(P_GUIDE, "w", encoding="utf-8") as f:
        f.write("# 人工翻译风格指南\n# 来源：%s\n# 累积 %d 组平行句对\n\n"
                % (a.tag or a.sub_dir, stats["total"]))
        f.write(guide.strip() + "\n")
    print("\n已保存:\n  %s\n  %s\n  %s\n  %s" % (P_PARALLEL, P_STATS, P_HARD, P_GUIDE))


if __name__ == "__main__":
    main()
