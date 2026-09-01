"""Issue #1023 — the per-word fallback sentence must not crash.

#806 pasted a `if lang == "zh" else …` branch into the fallback loop of all
four sentence generators in ai.py, but only generate_podcast_sentences takes a
`lang` argument. In the other three that branch was a NameError, and since the
fallback loop runs whenever the model skips even one word (routinely — see the
production log in #1023), kahneman / news / briefing (paste, contextsummary)
died *after* the whole pipeline had already run and been paid for. The frontend
then re-requested and silently got a plain `story`, which is what Daniel saw as
"Kontextsummary has no context and nothing to do with the source".

Each test hands the generator a reply that covers only the first word, so the
remaining cards go through the fallback path.
"""

import json
from unittest.mock import patch

import ai

CARDS = [
    {"word_id": 1, "word_zh": "担心", "pinyin": "dān xīn", "definition": "to worry"},
    {"word_id": 2, "word_zh": "努力", "pinyin": "nǔ lì", "definition": "to work hard"},
]
ARTICLES = [{"url": "https://example.com/a", "title": "标题", "text": "正文内容"}]
CHAPTER = {"number": 1, "title_zh": "章", "title_en": "Ch", "concept_zh": "概念",
           "concept_en": "concept", "examples_zh": ["例子"]}

# Covers 担心 only — 努力 has to fall back.
ONE_WORD_REPLY = json.dumps([{"sentence_zh": "她很担心考试。", "target_word": "担心",
                              "article_idx": 0}])

FALLBACK_ZH = "我学了努力这个词。"


def _sentence_for(result, word_id):
    return next(s["sentence_zh"] for s in result if word_id in s["word_ids"])


def test_briefing_fallback_does_not_crash():
    with patch("ai._call_api", return_value=ONE_WORD_REPLY), \
         patch("ai.fact_check_briefing", return_value=[]), \
         patch("ai._fill_translations", lambda sentences, **kw: None):
        result = ai.generate_briefing_sentences(CARDS, ARTICLES, model="gpt-5.6-luna")

    assert {s["word_ids"][0] for s in result} == {1, 2}
    assert _sentence_for(result, 2) == FALLBACK_ZH


def test_news_fallback_does_not_crash():
    with patch("ai._call_api", return_value=ONE_WORD_REPLY), \
         patch("ai._fill_translations", lambda sentences, **kw: None):
        result = ai.generate_news_sentences(CARDS, ARTICLES, model="gpt-5.6-luna")

    assert {s["word_ids"][0] for s in result} == {1, 2}
    assert _sentence_for(result, 2) == FALLBACK_ZH


def test_kahneman_fallback_does_not_crash():
    with patch("ai._call_api", return_value=ONE_WORD_REPLY), \
         patch("ai._fill_translations", lambda sentences, **kw: None):
        result = ai.generate_kahneman_sentences(CARDS, CHAPTER, model="deepseek-v4-flash")

    assert {s["word_ids"][0] for s in result} == {1, 2}
    assert _sentence_for(result, 2) == FALLBACK_ZH
