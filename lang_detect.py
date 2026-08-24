"""便宜的「这段文字真的是 X 语吗？」判定（issue #912）。

为什么需要它：knowledge / book 模式把一大段**素材原文**喂给模型，而素材几乎
从来不是牌组的目标语言（德语书、英语播客）。模型会跟着素材语言走，写出一句
德语、只把法语目标词塞进去：

    Jonathan Haidt vergleicht das Bewusstsein mit la bourse, …

下游没有任何东西能发现：`ai._word_match` 只找目标词，找到了就算这句合格，
于是垃圾句直接进库、变成卡片。这个模块就是那道缺失的关卡。

**它不是语言识别器**，也不打算变成一个 —— 不加依赖（同 `zh_annotate` 的姿态），
只用两个免费信号：

1. **字形**：`zh_annotate.cjk_ratio()`（#904 已经用它判 `summary_de` 写没写错
   语言）—— 拉丁字母语言里出现大量汉字，一票否决；
2. **功能词**：每种语言那一小撮封闭词类（冠词/介词/代词/助动词），任何一句话
   都躲不开。目标语言的词表直接复用 `annotate.romance.stopwords()`，**不另存
   一份** —— 同一个"什么算功能词"的判断不许有两个版本。

**倾向于放行**：只有当外语证据明显压过目标语言证据时才否决。误留一句 = 一张
烂卡；误杀一句 = 每句都要多跑一轮 AI，而且句子可能永远补不上，最后落到兜底句。
"""
import logging
import re

import zh_annotate

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# 罗曼语句子里超过这个比例的汉字 = 根本不是这门语言（同 #904 的
# NON_CHINESE_TEXT_MAX_CJK；合法的中文注释占比远低于此）。
_MAX_CJK_IN_LATIN = 0.10

# 少于这么多外语功能词就不否决：一句法语里蹦出一个德语专有名词很正常，
# 凭一个词判死刑是误杀。
_MIN_FOREIGN_HITS = 2

# 德语功能词。素材语言绝大多数是德语（Daniel 的书/播客），这是主要的漂移方向。
_GERMAN = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem",
    "einer", "eines", "und", "oder", "aber", "denn", "sondern", "dass", "weil",
    "wenn", "als", "wie", "was", "wer", "wo", "wann", "warum", "ist", "sind",
    "war", "waren", "wird", "werden", "wurde", "wurden", "hat", "haben", "hatte",
    "hatten", "kann", "können", "konnte", "muss", "müssen", "musste", "soll",
    "sollen", "will", "wollen", "darf", "dürfen", "mit", "nach", "bei", "seit",
    "von", "zu", "zur", "zum", "aus", "außer", "gegen", "ohne", "für", "über",
    "unter", "vor", "hinter", "neben", "zwischen", "durch", "um", "bevor",
    "nachdem", "während", "nicht", "kein", "keine", "keinen", "auch", "noch",
    "schon", "nur", "sehr", "mehr", "immer", "wieder", "dann", "doch", "sich",
    "ich", "du", "er", "sie", "es", "wir", "ihr", "ihn", "ihm", "ihnen", "mir",
    "mich", "dir", "dich", "uns", "euch", "sein", "seine", "seinen", "ihre",
    "ihren", "unser", "diese", "dieser", "dieses", "diesen", "man", "damit",
    "dabei", "dazu", "daran", "darauf",
}

# 英语功能词（播客/文章素材的第二大来源）。
_ENGLISH = {
    "the", "and", "but", "or", "if", "because", "that", "which", "who", "what",
    "when", "where", "while", "is", "are", "was", "were", "be", "been", "being",
    "has", "have", "had", "does", "did", "doing", "can", "could", "should",
    "would", "will", "shall", "may", "might", "must", "of", "to", "in", "into",
    "for", "with", "without", "from", "by", "at", "about", "after", "before",
    "between", "through", "during", "against", "under", "over", "than", "then",
    "there", "their", "they", "them", "these", "those", "this", "his", "her",
    "hers", "its", "our", "ours", "your", "yours", "she", "we", "you", "not",
    "only", "also", "just", "very", "more", "most", "much", "many", "such",
    "each", "every", "any", "some", "how", "why", "himself", "herself",
    "itself", "themselves",
}

_FOREIGN = _GERMAN | _ENGLISH


def _tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in _WORD_RE.finditer(text or "")]


def looks_like_language(text: str, lang: str) -> bool:
    """`text` 看起来是 `lang` 写的吗？判不出来一律返回 True（见模块开头）。

    中文走字形判定；罗曼语先排除汉字，再比功能词证据。任何异常（词表读不到
    等）都返回 True —— 这道关卡的作用是挡住明显的整句漂移，不是当质检员。
    """
    if not (text or "").strip():
        return True
    try:
        if lang == "zh":
            return zh_annotate.is_chinese_text(text)

        if zh_annotate.cjk_ratio(text) >= _MAX_CJK_IN_LATIN:
            return False

        from annotate.romance import stopwords
        target = stopwords(lang)
        if not target:
            return True          # 词表读不到 = 没有证据，别乱杀

        # 目标语言自己就有的词不算"外语证据"：西语的 es、法语的 a/on/en
        # 同时也是德/英功能词，不减掉就会把正常句子判成外语。
        foreign = _FOREIGN - target
        toks = _tokens(text)
        foreign_hits = sum(1 for t in toks if t in foreign)
        if foreign_hits < _MIN_FOREIGN_HITS:
            return True
        target_hits = sum(1 for t in toks if t in target)
        return target_hits >= foreign_hits
    except Exception as e:
        logger.warning("lang_detect: failed for lang=%s — %s", lang, e)
        return True
