"""
Tests for zh_annotate.py — AI-free vocabulary annotation of podcast summaries
(#638).

No network and no real database: the HSK table, the collection lookup and
Google Translate are all stubbed, so these tests assert the annotation *rules*
(what counts as a new word, where the annotation lands, what happens when a
piece fails) rather than the contents of the shipped word list. jieba does the
real segmentation — it is a pure local dependency and its behavior is what the
module actually relies on.
"""

import pytest

import zh_annotate


@pytest.fixture
def collection():
    """The stubbed contents of Daniel's collection — tests add to it."""
    return set()


@pytest.fixture(autouse=True)
def stub_io(monkeypatch, collection):
    """Stub the two things that reach outside the process: the collection
    lookup (SQLite) and Google Translate. The HSK table is NOT stubbed — it
    ships in this repo (static/hsk_levels.json) and is exactly what the rules
    are supposed to be judged against."""
    monkeypatch.setattr(zh_annotate, "_translation_cache", {})
    monkeypatch.setattr(zh_annotate, "_known_words",
                        lambda words: collection & set(words))
    monkeypatch.setattr(zh_annotate, "_gloss_de", lambda w: f"DE:{w}")


def test_hsk5_word_outside_collection_is_annotated():
    out = zh_annotate.annotate_zh_summary("这集讨论对就业的影响。")
    assert "就业（jiùyè - DE:就业）" in out


def test_word_in_collection_is_not_annotated(collection):
    """就业 is HSK 5, but once it is in Daniel's collection he knows it."""
    collection.add("就业")
    assert zh_annotate.annotate_zh_summary("这集讨论对就业的影响。") == "这集讨论对就业的影响。"


def test_hsk4_and_below_is_not_annotated():
    out = zh_annotate.annotate_zh_summary("这对公司的影响很大。")
    assert out == "这对公司的影响很大。"


def test_unlisted_word_of_basic_characters_is_skipped():
    """"十年" is missing from the word list, but every character of it appears
    in an HSK 1-4 word — Daniel can read it, so it must stay clean. This is the
    main noise filter (#638)."""
    out = zh_annotate.annotate_zh_summary("过去十年的变化。")
    assert out == "过去十年的变化。"


def test_unlisted_word_with_advanced_characters_is_annotated():
    """"硅谷" is in neither the word list nor the collection, and 硅 appears in
    no basic word — exactly the case the annotation exists for."""
    out = zh_annotate.annotate_zh_summary("他在硅谷工作。")
    assert "硅谷（guīgǔ - DE:硅谷）" in out


def test_only_first_occurrence_is_annotated():
    out = zh_annotate.annotate_zh_summary("就业问题。就业问题。")
    assert out.count("jiùyè") == 1


def test_non_annotated_text_is_returned_unchanged():
    """Everything jieba emits is concatenated back, so untouched text must
    survive byte-for-byte, including punctuation and line breaks."""
    text = "他说：“好的。”\n\n第二段，还有 AI 和 2026 年。"
    assert zh_annotate.annotate_zh_summary(text) == text


def test_annotation_falls_back_to_pinyin_when_translation_fails(monkeypatch):
    """Google Translate being down must cost the gloss, not the annotation."""
    monkeypatch.setattr(zh_annotate, "_gloss_de", lambda w: "")
    out = zh_annotate.annotate_zh_summary("对就业的影响。")
    assert "就业（jiùyè）" in out


def test_segmentation_failure_returns_original_text(monkeypatch):
    monkeypatch.setattr(zh_annotate, "_segment", lambda text: [])
    assert zh_annotate.annotate_zh_summary("对就业的影响。") == "对就业的影响。"


def test_person_and_place_names_are_annotated_in_chinese(monkeypatch):
    """Names used to be dropped by their jieba POS tag (nr/ns); since #961 they
    go through the same chain as any other word — Daniel needs the reading of a
    province he cannot pronounce just as much. Stub the segmentation to pin the
    rule down independently of the dictionary."""
    monkeypatch.setattr(zh_annotate, "_segment",
                        lambda text: [("浙江", "ns"), ("的", "uj"), ("瓶颈", "n")])
    out = zh_annotate.annotate_zh_summary("浙江的瓶颈")
    assert "浙江（zhèjiāng - DE:浙江）" in out
    assert "瓶颈（píngjǐng - DE:瓶颈）" in out


# --- German summary: strip every Chinese aside (#979) -----------------------

def test_pinyin_annotation_is_stripped_from_german():
    out = zh_annotate.strip_chinese_annotations(
        "<p>Rezession (jīngjì shuāituì/经济衰退) trifft alle.</p>")
    assert out == "<p>Rezession trifft alle.</p>"


def test_chinese_company_name_is_stripped():
    out = zh_annotate.strip_chinese_annotations("<p>Airbnb (爱彼迎) wächst.</p>")
    assert out == "<p>Airbnb wächst.</p>"


def test_mixed_aside_with_a_latin_name_goes_entirely():
    """The whole group goes, not just its Chinese half: "(Björn Höcke, pinyin/
    汉字)" is an annotation of a name already spelled out right before it."""
    out = zh_annotate.strip_chinese_annotations(
        "<p>AfD-Chef Björn Höcke (Björn Höcke, déguóxuǎnzédǎng/德国选择党) "
        "will stürzen.</p>")
    assert out == "<p>AfD-Chef Björn Höcke will stürzen.</p>"


def test_parentheses_without_chinese_survive():
    """Timestamps (#479) and ordinary German parentheses are not annotations."""
    text = "<p>Das Thema beginnt (ca. 12:30) und dauert (etwa 20 Minuten).</p>"
    assert zh_annotate.strip_chinese_annotations(text) == text


def test_german_text_without_chinese_is_untouched():
    text = "<p><b>Nur Deutsch.</b> Kein Chinesisch hier.</p>"
    assert zh_annotate.strip_chinese_annotations(text) == text


def test_full_width_parentheses_are_stripped_too():
    """Models write both bracket shapes. This is also why the Chinese summary
    must never be passed through here — those annotations are the material."""
    out = zh_annotate.strip_chinese_annotations("<p>Ökologie（生态）zählt.</p>")
    assert out == "<p>Ökologie zählt.</p>"


def test_empty_input_is_passed_through():
    assert zh_annotate.annotate_zh_summary("") == ""
    assert zh_annotate.strip_chinese_annotations("") == ""
    assert zh_annotate.annotate_zh_summary(None) is None


# --- extract_new_words (#650) -----------------------------------------------

def test_extract_new_words_picks_up_new_word():
    out = zh_annotate.extract_new_words("这集讨论对就业的影响。")
    words = [w["word"] for w in out]
    assert "就业" in words
    entry = next(w for w in out if w["word"] == "就业")
    assert entry["pinyin"] == "jiùyè"
    assert entry["definition_de"] == "DE:就业"
    assert entry["hsk"] == 6


def test_extract_new_words_excludes_words_in_collection(collection):
    collection.add("就业")
    out = zh_annotate.extract_new_words("这集讨论对就业的影响。")
    assert "就业" not in [w["word"] for w in out]


def test_extract_new_words_excludes_hsk4_and_below():
    out = zh_annotate.extract_new_words("这对公司的影响很大。")
    assert out == []


def test_extract_new_words_excludes_transparent_compounds():
    """"十年" is built entirely from basic (HSK<=4) characters — same
    transparent-compound filter as annotate_zh_summary."""
    out = zh_annotate.extract_new_words("过去十年的变化。")
    assert out == []


def test_extract_new_words_empty_input_returns_empty_list():
    assert zh_annotate.extract_new_words("") == []
    assert zh_annotate.extract_new_words(None) == []


def test_extract_new_words_deduplicates_in_first_appearance_order():
    out = zh_annotate.extract_new_words("硅谷很大。硅谷也很贵。他去了就业市场。")
    words = [w["word"] for w in out]
    assert words.count("硅谷") == 1
    assert words.index("硅谷") < words.index("就业")


def test_extract_new_words_reads_chinese_embedded_in_html():
    """extract_new_words must not need pre-stripped HTML — the German summary
    ships raw HTML with embedded Chinese runs (#650)."""
    out = zh_annotate.extract_new_words("<p>Beschäftigung (就业) ist wichtig.</p>")
    assert "就业" in [w["word"] for w in out]


def test_extract_new_words_segmentation_failure_returns_empty_list(monkeypatch):
    monkeypatch.setattr(zh_annotate, "_segment", lambda text: [])
    assert zh_annotate.extract_new_words("对就业的影响。") == []
