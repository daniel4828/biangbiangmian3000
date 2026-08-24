"""知识/书籍模式的输出语言关卡（issue #912）。

背景：素材几乎从来不是牌组的目标语言（德语书、英语播客），模型会跟着素材
语言走，写出一句德语、只把法语目标词塞进去。`ai._word_match` 只找目标词，
所以这种句子以前直接进库变成卡片。

这里守两件事：
1. `lang_detect.looks_like_language()` 认得出整句漂移，也不误杀正常句子；
2. `ai.generate_podcast_sentences()` 丢弃漂移的句子、把那个词留到补漏轮，
   **绝不把它写进返回的句子里**。
"""
import pytest

import lang_detect


GERMAN_WITH_FRENCH_WORD = [
    "Jonathan Haidt vergleicht das Bewusstsein mit la bourse, nicht mit dem Oval Office.",
    "Der Körper kann zur Seite springen, bevor das Bewusstsein die Gefahr versteht, ohne sauver.",
    "Eine Person muss boire, bevor sie bewusst den herannahenden Bus bemerkt.",
]

REAL_FRENCH = [
    "Jonathan Haidt compare la conscience à la bourse, pas au Bureau ovale.",
    "Le PIB a augmenté de 3 % en 2024 selon Sutherland.",
    "On peut sauter de côté avant que la conscience comprenne le danger.",
    "Une personne doit boire avant de remarquer le bus qui arrive.",
]


@pytest.mark.parametrize("sentence", GERMAN_WITH_FRENCH_WORD)
def test_german_sentence_with_french_word_is_rejected(sentence):
    assert lang_detect.looks_like_language(sentence, "fr") is False


@pytest.mark.parametrize("sentence", REAL_FRENCH)
def test_real_french_is_accepted(sentence):
    assert lang_detect.looks_like_language(sentence, "fr") is True


def test_real_spanish_is_accepted():
    assert lang_detect.looks_like_language(
        "El PIB creció un 3 % en 2024 según Sutherland.", "es") is True


def test_chinese_sentence_rejected_for_french_deck():
    # 中文路径本身不走这道关卡，但判定本身要认得出来（罗曼语句里出现汉字）。
    assert lang_detect.looks_like_language("他把意识比作股市。", "fr") is False


def test_chinese_check_is_script_based():
    assert lang_detect.looks_like_language("他把意识比作股市。", "zh") is True
    assert lang_detect.looks_like_language("Das ist die Börse.", "zh") is False


def test_no_evidence_is_accepted():
    # 短句/专有名词堆：没有功能词证据就不许否决（误杀比误留贵）。
    assert lang_detect.looks_like_language("Sutherland, Haidt, 2024.", "fr") is True
    assert lang_detect.looks_like_language("", "fr") is True


def test_generate_podcast_sentences_drops_drifted_sentence(monkeypatch):
    """整轮都写成德语时，词不算被覆盖 —— 走完补漏轮后落到兜底句，
    而不是把德语句子存下来。"""
    ai = pytest.importorskip("ai", reason="ai deps not installed")

    cards = [{"word_id": 1, "word_zh": "la bourse", "definition_de": "die Börse"}]
    source = {"title": "Alchemy", "kind": "book", "url": "/#book-1",
              "material": "Ein deutscher Text über die Börse."}

    calls = []

    def fake_call_api(model, messages, max_tokens, purpose=None, **kwargs):
        calls.append(messages[0]["content"])
        return ('[{"reasoning_zh": "Fact: Börse", '
                '"sentence_zh": "Jonathan Haidt vergleicht das Bewusstsein mit '
                'la bourse, nicht mit dem Oval Office.", '
                '"target_word": "la bourse"}]')

    monkeypatch.setattr(ai, "_call_api", fake_call_api)
    monkeypatch.setattr(ai, "_fill_translations", lambda *a, **k: None)
    monkeypatch.setattr(ai, "_card_surface_forms", lambda c, lang: [c["word_zh"]])

    sentences, prompt = ai.generate_podcast_sentences(cards, source, lang="fr")

    assert len(sentences) == 1
    # 兜底句（词本身），不是那句德语。
    assert "Bewusstsein" not in sentences[0]["sentence_zh"]
    assert sentences[0]["sentence_zh"] == "la bourse."
    # 补漏轮真的跑过，并且提醒了语言。
    assert len(calls) > 1
    assert "French" in calls[-1]


def test_prompt_puts_output_language_first():
    ai = pytest.importorskip("ai", reason="ai deps not installed")
    assert "1. OUTPUT LANGUAGE" in ai._KNOWLEDGE_PROMPT_NON_ZH
