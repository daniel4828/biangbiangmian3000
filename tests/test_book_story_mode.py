"""Tests for issue #865: story generation gains a `book` mode.

Reviewing due words can now be anchored to one summarized book chapter
(#864's book_chapters table) instead of a podcast/video/article item — same
ai.generate_podcast_sentences pipeline (routes/story.py's knowledge branch),
just a different `source` builder (_book_source). Worth pinning down:

  1. Missing/invalid book_chapter_id never silently falls back to a plain
     story — it's a ValueError surfaced as an error dict, same as knowledge
     mode's missing episode_id.
  2. A chapter that hasn't been summarized yet is rejected with a readable
     Chinese message pointing at the fix (生成摘要 button).
  3. A successful generation records book_chapter_id + kind='book' in
     gen_params (so Again-regen and the frontend can reproduce/display it)
     and stamps every sentence's concept_zh with "第N章：<title>" unless the
     model already supplied one.

Isolated temp DB via database.core.DB_PATH (never database.DB_PATH — #615).
AI is stubbed at ai.generate_podcast_sentences, the same seam
test_knowledge_story_mode.py patches.
"""
import zipfile

import pytest
from unittest.mock import patch

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient
import books
import database
import importer
import main
import routes.story as story_routes

client = TestClient(main.app)

ENTRY_你好 = {"type": "vocabulary", "simplified": "你好", "pinyin": "nǐ hǎo",
               "english": "hello", "pos": "intj", "hsk": "1"}


def write_yaml(tmp_path, name, entries):
    import yaml
    d = tmp_path / "Kouyu"
    d.mkdir(exist_ok=True)
    (d / name).write_text(yaml.dump({"entries": entries}, allow_unicode=True))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(database.core, "DB_PATH", str(db_file))
    database.init_db()
    return db_file


@pytest.fixture
def populated_db(tmp_db, tmp_path):
    write_yaml(tmp_path, "words.yaml", [ENTRY_你好])
    importer.import_all(str(tmp_path))
    return next(d["id"] for d in database.get_all_decks() if d["name"] == "Kouyu")


def _write_epub(path, chapters, title="Testbuch", author="Autorin"):
    """A minimal but structurally real EPUB — copied from tests/test_books.py
    so this file can create its own book+chapter fixtures independently."""
    manifest = "".join(
        f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(len(chapters)))
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
    opf = f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>
  </metadata>
  <manifest>{manifest}</manifest>
  <spine>{spine}</spine>
</package>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml",
                    '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                    '<rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
        zf.writestr("content.opf", opf)
        for i, body in enumerate(chapters):
            zf.writestr(f"c{i}.xhtml",
                        f"<html><head><title>x</title></head><body>{body}</body></html>")


def _make_book_with_chapter(tmp_path, summarized=True):
    """A book with two headed chapters (derive_chapters needs >=2 distinct
    ref_labels to produce anything — a single label spanning the whole book
    is treated as "no chapter structure", see database.books.derive_chapters).
    Tests only use the first chapter."""
    path = tmp_path / "b.epub"
    _write_epub(path, [
        "<h1>Erstes Kapitel</h1><p>Ein Satz.</p><p>Noch ein Satz hier.</p>",
        "<h1>Zweites Kapitel</h1><p>Dritter Satz.</p>",
    ])
    book = books.ingest_file(str(path), "b.epub", char_budget=20)
    bid = book["book_id"]
    chapters = database.derive_chapters(bid)
    assert chapters, "fixture must produce at least one chapter"
    number = chapters[0]["number"]
    if summarized:
        database.save_chapter_summary(
            bid, number, title_zh="第一章", title_en="Chapter One",
            concept_zh="核心观点。", summary_zh="这一章讲述了一个故事。" * 5,
            examples_zh=["原句一。"])
    chapter = database.get_chapter(bid, number)
    return bid, chapter["id"]


def _fake_podcast_sentences(cards, source, **kwargs):
    # (sentences, prompt) — see test_knowledge_story_mode.py's twin fixture.
    title = source.get("title") if source else ""
    return [
        {"word_id": c["word_id"], "sentence_zh": f"{c['word_zh']}出现在这一章里。",
         "sentence_en": "", "target_word": c["word_zh"]}
        for c in cards
    ], f"假提示词：{title}"


def test_book_mode_without_chapter_id_errors(populated_db):
    """No book_chapter_id at all → error dict, never a silent plain story."""
    deck_id = populated_db
    r = client.get(f"/api/story/{deck_id}/listening", params={"mode": "book"})
    assert r.status_code == 200
    body = r.json()
    assert body["error"] is True
    assert "chapter" in body["reason"].lower()


def test_book_mode_unsummarized_chapter_errors(populated_db, tmp_path):
    """A chapter that exists but has status != 'summarized' is rejected with
    a readable message, not silently downgraded to a plain story."""
    deck_id = populated_db
    _, chapter_id = _make_book_with_chapter(tmp_path, summarized=False)

    r = client.get(f"/api/story/{deck_id}/listening",
                   params={"mode": "book", "book_chapter_id": chapter_id})
    assert r.status_code == 200
    body = r.json()
    assert body["error"] is True
    assert "摘要" in body["reason"]


def test_book_mode_unknown_chapter_id_errors(populated_db):
    r = client.get(f"/api/story/{populated_db}/listening",
                   params={"mode": "book", "book_chapter_id": 424242})
    assert r.status_code == 200
    body = r.json()
    assert body["error"] is True
    assert "not found" in body["reason"]


def test_book_mode_generates_story_with_chapter_concept(populated_db, tmp_path):
    """Happy path: gen_params round-trips book_chapter_id + kind='book', and
    every sentence gets a "第N章：<title>" concept box (Daniel's #865 ask —
    same UI treatment as kahneman's concept box)."""
    deck_id = populated_db
    book_id, chapter_id = _make_book_with_chapter(tmp_path, summarized=True)

    with patch("ai.generate_podcast_sentences", side_effect=_fake_podcast_sentences) as mock_gen:
        r = client.get(f"/api/story/{deck_id}/listening",
                       params={"mode": "book", "book_chapter_id": chapter_id})

    assert r.status_code == 200
    body = r.json()
    assert body is not None and not body.get("error")
    assert len(body["sentences"]) == 1
    mock_gen.assert_called_once()
    assert body["sentences"][0]["concept_zh"] == "第1章：第一章"

    story = database.get_active_story(database.anki_today().isoformat(), "listening", deck_id)
    gen_params = __import__("json").loads(story["gen_params"])
    assert gen_params["mode"] == "book"
    assert gen_params["book_chapter_id"] == chapter_id
    assert gen_params["kind"] == "book"


def test_book_mode_material_includes_summary_and_source_text(populated_db, tmp_path):
    """The AI call's material leads with the Chinese summary, then the
    chapter's original text — summary first so the model grasps the point
    before the raw detail (#865's ask)."""
    deck_id = populated_db
    book_id, chapter_id = _make_book_with_chapter(tmp_path, summarized=True)

    seen = {}

    def _capturing(cards, source, **kwargs):
        seen["material"] = source.get("material") if source else ""
        seen["kind"] = source.get("kind") if source else ""
        return _fake_podcast_sentences(cards, source, **kwargs)

    with patch("ai.generate_podcast_sentences", side_effect=_capturing) as mock_gen:
        r = client.get(f"/api/story/{deck_id}/listening",
                       params={"mode": "book", "book_chapter_id": chapter_id})
    assert r.status_code == 200 and not r.json().get("error")
    mock_gen.assert_called_once()
    assert seen["kind"] == "book"
    assert seen["material"].startswith("【本章中文摘要】")
    assert "这一章讲述了一个故事。" in seen["material"]
    assert "【本章原文】" in seen["material"]
    assert "Ein Satz" in seen["material"]


def test_book_again_regen_reuses_the_same_chapter(populated_db, tmp_path):
    """Again single-sentence regen (generate_sentence_for_word) rebuilds the
    same chapter's source via _book_source, same pattern as knowledge mode's
    episode_ids re-fetch."""
    deck_id = populated_db
    card = story_routes._get_cards_for_story(deck_id, "listening")[0]
    book_id, chapter_id = _make_book_with_chapter(tmp_path, summarized=True)

    gen_params = {"mode": "book", "book_chapter_id": chapter_id, "max_hsk": 3, "model": None}
    with patch("ai.generate_podcast_sentences", side_effect=_fake_podcast_sentences) as mock_gen:
        result = story_routes.generate_sentence_for_word(card, gen_params)

    assert result is not None
    assert result["sentence_zh"]
    mock_gen.assert_called_once()
