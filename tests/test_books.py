"""Tests for issue #836: the book reader.

An uploaded German/English EPUB/PDF is cut into fixed-size pages once, then
each page is translated and annotated on demand by the same pipeline the
knowledge base uses (knowledge/rendition.render_html) and cached.

What is worth pinning down here:

  1. Pagination cuts only at paragraph boundaries and labels a page with the
     book marker it *starts* in.
  2. EPUB extraction walks the spine with the stdlib only, and a file with no
     prose fails loudly rather than becoming an empty book.
  3. A rendered page is cached — paging back must not re-translate.
  4. A translation failure is reported and writes nothing (a page of German
     served as Chinese would only be noticed halfway down it).
  5. Reading progress is per (book, language).

Each test gets an isolated temp DB by patching database.core.DB_PATH — never
database.DB_PATH, which is only a wildcard-import copy (#615).
"""
import json
import os
import sys
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import ai
import books
import database
import knowledge.rendition
import main
from books.epub import BookExtractionError
from books.paginate import paginate

client = TestClient(main.app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


@pytest.fixture
def fake_render(monkeypatch):
    """Stand in for translate-then-annotate, counting how often it ran.

    Patched on routes.books, which is where the page endpoint looks it up —
    the module-level import there means patching knowledge.rendition alone
    would silently miss (#615's lesson about patching the name the caller
    actually reads).
    """
    import routes.books as book_routes

    calls = []

    def _render(html, lang, source="de"):
        calls.append((html, lang, source))
        return f"[{lang}]{html}", [{"word": "生态", "definition_de": "Ökologie"}]

    monkeypatch.setattr(book_routes, "render_html", _render)
    return calls


# --- pagination -------------------------------------------------------------

def test_paginate_never_splits_a_paragraph():
    blocks = [{"text": "A" * 400, "ref_label": "c1"},
              {"text": "B" * 400, "ref_label": "c1"},
              {"text": "C" * 400, "ref_label": "c2"}]
    pages = paginate(blocks, char_budget=500)
    assert len(pages) == 3
    for page, letter in zip(pages, "ABC"):
        assert page["source_text"] == f"<p>{letter * 400}</p>"


def test_paginate_labels_a_page_with_the_marker_it_starts_in():
    blocks = [{"text": "A" * 300, "ref_label": "PDF p. 7"},
              {"text": "B" * 300, "ref_label": "PDF p. 8"}]
    pages = paginate(blocks, char_budget=400)
    assert [p["ref_label"] for p in pages] == ["PDF p. 7", "PDF p. 8"]


def test_paginate_escapes_html_in_the_source_text():
    """Source text goes into <p> markup, so a stray < in the book must not
    turn into a tag the translator would then move around."""
    pages = paginate([{"text": "5 < 6 & <b>bold</b>", "ref_label": None}], 500)
    assert "&lt;" in pages[0]["source_text"]
    assert "<b>" not in pages[0]["source_text"]


def test_paginate_splits_an_over_long_paragraph_at_sentence_ends():
    text = "Das ist ein Satz. " * 100          # ~1800 chars, one paragraph
    pages = paginate([{"text": text, "ref_label": None}], char_budget=300)
    assert len(pages) > 1
    assert all(p["source_text"].endswith("</p>") for p in pages)


# --- EPUB extraction --------------------------------------------------------

def _write_epub(path, chapters, title="Testbuch", author="Autorin"):
    """A minimal but structurally real EPUB: container.xml → OPF → spine."""
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
                        f"<html><head><title>x</title><style>p{{}}</style></head>"
                        f"<body>{body}</body></html>")


def _write_epub_with_toc(path, chapters, *, nav_entries=None, ncx_entries=None,
                         title="Testbuch", author="Autorin"):
    """Like _write_epub, but optionally adds an EPUB3 nav.xhtml and/or an
    EPUB2 toc.ncx (#881). `nav_entries`/`ncx_entries` are [(href, title)]
    written into whichever document is requested."""
    manifest_items = [
        f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(len(chapters))]
    spine_attrs = ""
    if nav_entries is not None:
        manifest_items.append(
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
    if ncx_entries is not None:
        manifest_items.append(
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
        spine_attrs = ' toc="ncx"'
    manifest = "".join(manifest_items)
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
    opf = f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>
  </metadata>
  <manifest>{manifest}</manifest>
  <spine{spine_attrs}>{spine}</spine>
</package>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml",
                    '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                    '<rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
        zf.writestr("content.opf", opf)
        for i, body in enumerate(chapters):
            zf.writestr(f"c{i}.xhtml",
                        f"<html><head><title>x</title><style>p{{}}</style></head>"
                        f"<body>{body}</body></html>")
        if nav_entries is not None:
            links = "".join(f'<li><a href="{href}">{txt}</a></li>' for href, txt in nav_entries)
            nav_xhtml = f"""<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>ToC</title></head>
<body><nav epub:type="toc" id="toc"><ol>{links}</ol></nav></body>
</html>"""
            zf.writestr("nav.xhtml", nav_xhtml)
        if ncx_entries is not None:
            points = "".join(
                f'<navPoint id="np{i}" playOrder="{i}">'
                f'<navLabel><text>{txt}</text></navLabel>'
                f'<content src="{href}"/></navPoint>'
                for i, (href, txt) in enumerate(ncx_entries, start=1))
            ncx = f"""<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head></head><docTitle><text>{title}</text></docTitle>
<navMap>{points}</navMap>
</ncx>"""
            zf.writestr("toc.ncx", ncx)


def test_epub_extract_reads_metadata_and_spine_order(tmp_path):
    path = tmp_path / "b.epub"
    _write_epub(path, ["<h1>Erstes Kapitel</h1><p>Ein Satz.</p>",
                       "<h1>Zweites Kapitel</h1><p>Noch einer.</p>"])
    out = books.epub.extract(str(path))
    assert out["title"] == "Testbuch"
    assert out["author"] == "Autorin"
    texts = [b["text"] for b in out["blocks"]]
    assert texts == ["Erstes Kapitel", "Ein Satz.", "Zweites Kapitel", "Noch einer."]
    # The heading becomes the label for everything under it.
    assert out["blocks"][1]["ref_label"] == "Erstes Kapitel"
    assert out["blocks"][3]["ref_label"] == "Zweites Kapitel"


def test_epub_extract_drops_style_and_script_content(tmp_path):
    path = tmp_path / "b.epub"
    _write_epub(path, ["<p>Prosa.</p><script>var x = 1;</script>"])
    texts = [b["text"] for b in books.epub.extract(str(path))["blocks"]]
    assert texts == ["Prosa."]


def test_epub_with_no_prose_fails_instead_of_becoming_an_empty_book(tmp_path):
    path = tmp_path / "b.epub"
    _write_epub(path, ["<p></p>"])
    with pytest.raises(BookExtractionError):
        books.epub.extract(str(path))


def test_non_epub_file_is_rejected(tmp_path):
    path = tmp_path / "notabook.epub"
    path.write_bytes(b"this is not a zip")
    with pytest.raises(BookExtractionError):
        books.epub.extract(str(path))


def test_unsupported_extension_is_rejected():
    with pytest.raises(BookExtractionError):
        books.format_from_filename("roman.mobi")


# --- table of contents (#881) ------------------------------------------------

def test_epub3_nav_xhtml_toc_sets_chapter_titles(tmp_path):
    """Chapter titles come from the ToC, not from any heading tag — these
    spine documents have no h1-h3 at all, exactly the case #881 is about."""
    path = tmp_path / "b.epub"
    _write_epub_with_toc(
        path, ["<p>Erster Absatz.</p>", "<p>Zweiter Absatz.</p>"],
        nav_entries=[("c0.xhtml", "1. Einleitung"), ("c1.xhtml", "2. Hauptteil")])
    out = books.epub.extract(str(path))
    labels = [b["ref_label"] for b in out["blocks"]]
    assert labels == ["1. Einleitung", "2. Hauptteil"]


def test_epub2_ncx_toc_sets_chapter_titles(tmp_path):
    path = tmp_path / "b.epub"
    _write_epub_with_toc(
        path, ["<p>Erster Absatz.</p>", "<p>Zweiter Absatz.</p>"],
        ncx_entries=[("c0.xhtml", "1. Einleitung"), ("c1.xhtml", "2. Hauptteil")])
    out = books.epub.extract(str(path))
    labels = [b["ref_label"] for b in out["blocks"]]
    assert labels == ["1. Einleitung", "2. Hauptteil"]


def test_epub_falls_back_to_spine_document_per_chapter_without_a_toc(tmp_path):
    """No nav.xhtml, no toc.ncx, no h1-h3 anywhere: each spine document still
    becomes its own chapter, labelled by its filename."""
    path = tmp_path / "b.epub"
    _write_epub(path, ["<p>Erster Absatz.</p>", "<p>Zweiter Absatz.</p>"])
    out = books.epub.extract(str(path))
    labels = [b["ref_label"] for b in out["blocks"]]
    assert labels == ["c0", "c1"]
    assert len(set(labels)) == 2   # two distinct chapters, not one


def test_epub_chapter_title_priority_toc_then_h1_then_filename(tmp_path):
    """Three spine documents exercising all three levels of the fallback
    chain at once: a ToC title (which still yields to a real in-document
    heading appearing partway through), a bare h1 with no ToC entry, and a
    document with neither, which falls back to its filename."""
    path = tmp_path / "b.epub"
    _write_epub_with_toc(
        path,
        ["<p>Vor der Überschrift.</p><h1>Echte Überschrift</h1><p>Danach.</p>",
         "<h1>Nur H1</h1><p>Text.</p>",
         "<p>Weder ToC noch H1.</p>"],
        nav_entries=[("c0.xhtml", "ToC-Titel")])
    out = books.epub.extract(str(path))
    by_text = {b["text"]: b["ref_label"] for b in out["blocks"]}
    assert by_text["Vor der Überschrift."] == "ToC-Titel"
    assert by_text["Echte Überschrift"] == "Echte Überschrift"
    assert by_text["Danach."] == "Echte Überschrift"
    assert by_text["Nur H1"] == "Nur H1"
    assert by_text["Text."] == "Nur H1"
    assert by_text["Weder ToC noch H1."] == "c2"


def test_toc_parsing_failure_falls_back_gracefully(tmp_path):
    """A broken nav.xhtml must not sink the whole book — just its ToC."""
    path = tmp_path / "b.epub"
    _write_epub_with_toc(path, ["<h1>Kapitel</h1><p>Text.</p>"],
                         nav_entries=[("c0.xhtml", "Titel")])
    # Corrupt nav.xhtml in place by rewriting the whole archive with garbage
    # in its place, leaving everything else untouched.
    import shutil
    tmp2 = tmp_path / "b2.epub"
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp2, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "nav.xhtml":
                data = b"<not><valid"
            dst.writestr(item, data)
    shutil.move(str(tmp2), str(path))
    out = books.epub.extract(str(path))
    assert out["blocks"][0]["ref_label"] == "Kapitel"   # falls back to the h1


# --- ingest -----------------------------------------------------------------

def test_ingest_stores_pages_and_detects_german(tmp_db, tmp_path):
    path = tmp_path / "b.epub"
    _write_epub(path, ["<h1>Kapitel</h1>" + "".join(
        f"<p>Das ist der Absatz nummer {i} und er ist nicht sehr lang.</p>"
        for i in range(40))])
    result = books.ingest_file(str(path), "b.epub", char_budget=300)

    assert result["source_lang"] == "de"
    assert result["page_count"] > 1
    book = database.get_book(result["book_id"])
    assert book["page_count"] == result["page_count"]
    assert book["title"] == "Testbuch"
    # Page numbering is 1-based and contiguous.
    assert database.get_page(result["book_id"], 1)["source_text"].startswith("<p>")
    assert database.get_page(result["book_id"], result["page_count"]) is not None
    assert database.get_page(result["book_id"], result["page_count"] + 1) is None


def test_ingest_honours_an_explicit_source_lang(tmp_db, tmp_path):
    """Detection is only a default: reading a German book with source_lang=en
    would translate every page from the wrong language."""
    path = tmp_path / "b.epub"
    _write_epub(path, ["<p>Und das ist nicht auf Englisch geschrieben.</p>"])
    result = books.ingest_file(str(path), "b.epub", source_lang="en")
    assert database.get_book(result["book_id"])["source_lang"] == "en"


# --- reading API ------------------------------------------------------------

def _make_book(tmp_path, paragraphs=("Ein Satz.", "Noch ein Satz.")):
    path = tmp_path / "b.epub"
    _write_epub(path, ["".join(f"<p>{p}</p>" for p in paragraphs)])
    return books.ingest_file(str(path), "b.epub", char_budget=20)


def test_page_is_rendered_then_cached(tmp_db, tmp_path, fake_render):
    book = _make_book(tmp_path)
    first = client.get(f"/api/books/{book['book_id']}/page/1?lang=zh")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["cached"] is False
    assert body["text"].startswith("[zh]")
    assert body["new_words"][0]["word"] == "生态"
    assert body["page_count"] == book["page_count"]

    second = client.get(f"/api/books/{book['book_id']}/page/1?lang=zh")
    assert second.json()["cached"] is True
    assert second.json()["text"] == body["text"]
    assert len(fake_render) == 1, "a cached page must not be re-translated"


def test_each_language_gets_its_own_rendition(tmp_db, tmp_path, fake_render):
    book = _make_book(tmp_path)
    assert client.get(f"/api/books/{book['book_id']}/page/1?lang=zh").json()["text"].startswith("[zh]")
    assert client.get(f"/api/books/{book['book_id']}/page/1?lang=fr").json()["text"].startswith("[fr]")
    assert len(fake_render) == 2


def test_source_language_is_passed_to_the_renderer(tmp_db, tmp_path, fake_render):
    path = tmp_path / "b.epub"
    _write_epub(path, ["<p>This is an English sentence.</p>"])
    book = books.ingest_file(str(path), "b.epub", source_lang="en")
    client.get(f"/api/books/{book['book_id']}/page/1?lang=zh")
    assert fake_render[0][2] == "en"


def test_translation_failure_is_reported_and_writes_nothing(tmp_db, tmp_path, monkeypatch):
    import routes.books as book_routes

    book = _make_book(tmp_path)

    def _boom(html, lang, source="de"):
        raise knowledge.rendition.RenditionError("translator unavailable")

    monkeypatch.setattr(book_routes, "render_html", _boom)
    resp = client.get(f"/api/books/{book['book_id']}/page/1?lang=zh")
    assert resp.status_code == 502
    assert "translator unavailable" in resp.json()["detail"]
    assert database.get_book_rendition(book["book_id"], 1, "zh") is None


def test_out_of_range_page_is_a_404(tmp_db, tmp_path, fake_render):
    book = _make_book(tmp_path)
    resp = client.get(f"/api/books/{book['book_id']}/page/999?lang=zh")
    assert resp.status_code == 404
    assert "999" in resp.json()["detail"]


def test_unknown_language_is_a_400(tmp_db, tmp_path, fake_render):
    book = _make_book(tmp_path)
    assert client.get(f"/api/books/{book['book_id']}/page/1?lang=ru").status_code == 400


def test_missing_book_is_a_404(tmp_db, fake_render):
    assert client.get("/api/books/4242/page/1?lang=zh").status_code == 404


# --- progress ---------------------------------------------------------------

def test_progress_is_remembered_per_language(tmp_db, tmp_path, fake_render):
    book = _make_book(tmp_path)
    bid = book["book_id"]
    assert client.post(f"/api/books/{bid}/progress",
                       json={"lang": "zh", "page_no": 2}).status_code == 200
    assert client.post(f"/api/books/{bid}/progress",
                       json={"lang": "fr", "page_no": 1}).status_code == 200
    assert database.get_book_progress(bid, "zh") == 2
    assert database.get_book_progress(bid, "fr") == 1

    listed = client.get("/api/books").json()["books"]
    assert listed[0]["progress"] == {"zh": 2, "fr": 1}


def test_progress_out_of_range_is_rejected(tmp_db, tmp_path, fake_render):
    book = _make_book(tmp_path)
    resp = client.post(f"/api/books/{book['book_id']}/progress",
                       json={"lang": "zh", "page_no": 999})
    assert resp.status_code == 400


# --- deletion ---------------------------------------------------------------

def test_delete_removes_pages_renditions_and_the_file(tmp_db, tmp_path, fake_render, monkeypatch):
    book = _make_book(tmp_path)
    bid = book["book_id"]
    client.get(f"/api/books/{bid}/page/1?lang=zh")
    client.post(f"/api/books/{bid}/progress", json={"lang": "zh", "page_no": 1})
    assert os.path.isfile(database.get_book(bid)["file_path"])
    path = database.get_book(bid)["file_path"]

    assert client.delete(f"/api/books/{bid}").status_code == 200
    assert database.get_book(bid) is None
    assert database.get_page(bid, 1) is None
    assert database.get_book_rendition(bid, 1, "zh") is None
    assert database.get_book_progress(bid, "zh") is None
    assert not os.path.exists(path)


def test_deleting_a_missing_book_is_a_404(tmp_db):
    assert client.delete("/api/books/4242").status_code == 404


# --- render_html (the shared pipeline) --------------------------------------

def test_render_html_refuses_an_unknown_language():
    with pytest.raises(knowledge.rendition.RenditionError):
        knowledge.rendition.render_html("<p>Hallo</p>", "ru")


def test_render_html_raises_when_the_translator_fails(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("no network")

    monkeypatch.setattr(knowledge.rendition, "_translate_html_strict", _boom)
    with pytest.raises(knowledge.rendition.RenditionError):
        knowledge.rendition.render_html("<p>Hallo</p>", "zh")


# --- PDF --------------------------------------------------------------------

def test_scanned_pdf_without_a_text_layer_fails_loudly(tmp_path):
    """No OCR here: a scan yields no prose, and storing it as a book would
    give Daniel a reader full of blank pages with no explanation."""
    from pypdf import PdfWriter

    from books import pdf as book_pdf

    path = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with open(path, "wb") as fh:
        writer.write(fh)

    with pytest.raises(BookExtractionError) as excinfo:
        book_pdf.extract(str(path))
    assert "text layer" in str(excinfo.value)


def test_pdf_paragraphs_rejoin_hard_wrapped_lines():
    from books.pdf import _paragraphs

    text = "Dies ist eine sehr lan-\nge Zeile im Satz.\n\nZweiter Absatz."
    assert _paragraphs(text) == ["Dies ist eine sehr lange Zeile im Satz.",
                                 "Zweiter Absatz."]


# --- chapters (#864) ---------------------------------------------------------

def _make_book_with_chapters(tmp_path, char_budget=20):
    """Two headed chapters, each split across a couple of pages by the small
    char_budget, so derive_chapters has something real to group."""
    path = tmp_path / "b.epub"
    _write_epub(path, [
        "<h1>Erstes Kapitel</h1><p>Ein Satz.</p><p>Noch ein Satz hier.</p>",
        "<h1>Zweites Kapitel</h1><p>Dritter Satz.</p><p>Vierter Satz hier.</p>",
    ])
    return books.ingest_file(str(path), "b.epub", char_budget=char_budget)


def test_derive_chapters_groups_consecutive_same_label(tmp_db, tmp_path):
    book = _make_book_with_chapters(tmp_path)
    bid = book["book_id"]
    chapters = database.derive_chapters(bid)
    assert [c["ref_label"] for c in chapters] == ["Erstes Kapitel", "Zweites Kapitel"]
    assert [c["number"] for c in chapters] == [1, 2]
    # Every page belongs to exactly one chapter, in order, covering the book.
    assert chapters[0]["start_page"] == 1
    assert chapters[1]["start_page"] == chapters[0]["end_page"] + 1
    assert chapters[-1]["end_page"] == book["page_count"]

    # Idempotent: calling again doesn't insert a second set of rows.
    again = database.derive_chapters(bid)
    assert again == chapters


def test_chapters_endpoint_skips_derivation_entirely_for_pdf(tmp_db, tmp_path):
    """A PDF's ref_label is a distinct real page number on every page —
    grouping it would produce one fake chapter per page. The route must
    never even call derive_chapters() for format='pdf', so book_chapters
    stays empty rather than filling up with junk one-page "chapters"."""
    book = _make_book(tmp_path)
    conn = database.core.get_db()
    conn.execute("UPDATE books SET format = 'pdf' WHERE id = ?", (book["book_id"],))
    conn.execute("UPDATE book_pages SET ref_label = 'p. ' || page_no WHERE book_id = ?",
                (book["book_id"],))
    conn.commit()
    conn.close()
    resp = client.get(f"/api/books/{book['book_id']}/chapters")
    assert resp.json()["available"] is False
    assert database.list_chapters(book["book_id"]) == []


def test_derive_chapters_returns_empty_when_all_labels_are_null(tmp_db, tmp_path):
    book = _make_book(tmp_path)  # no <h1>, so ref_label is NULL throughout
    assert database.derive_chapters(book["book_id"]) == []


def test_chapters_endpoint_unavailable_for_pdf(tmp_db, tmp_path):
    book = _make_book(tmp_path)
    database.core.get_db()  # sanity: db reachable
    conn = database.core.get_db()
    conn.execute("UPDATE books SET format = 'pdf' WHERE id = ?", (book["book_id"],))
    conn.commit()
    conn.close()
    resp = client.get(f"/api/books/{book['book_id']}/chapters")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert "EPUB" in body["reason"]
    assert body["chapters"] == []


def test_chapters_endpoint_missing_book_is_404(tmp_db):
    assert client.get("/api/books/4242/chapters").status_code == 404


def _fake_summary(**overrides):
    result = {"title_zh": "第一章", "title_en": "Chapter One",
              "concept_zh": "核心观点一句话。", "summary_zh": "详细摘要" * 20,
              "examples_zh": ["原句一。", "原句二。"]}
    result.update(overrides)
    return result


def test_summarize_chapter_success_writes_all_fields(tmp_db, tmp_path, monkeypatch):
    import routes.books as book_routes

    book = _make_book_with_chapters(tmp_path)
    bid = book["book_id"]
    database.derive_chapters(bid)

    monkeypatch.setattr(book_routes.ai, "summarize_book_chapter",
                        lambda text, **kw: _fake_summary())

    resp = client.post(f"/api/books/{bid}/chapters/1/summarize")
    assert resp.status_code == 200
    assert resp.json() == {"status": "started"}

    for _ in range(50):
        chapter = database.get_chapter(bid, 1)
        if chapter["status"] != "pending":
            break
        time.sleep(0.05)
    assert chapter["status"] == "summarized"
    assert chapter["title_zh"] == "第一章"
    assert chapter["summary_zh"].startswith("详细摘要")
    assert chapter["examples_zh"] == ["原句一。", "原句二。"]
    assert chapter["summarized_at"] is not None

    # The list view (no summary_zh/examples_zh) reflects the new status too.
    listed = client.get(f"/api/books/{bid}/chapters").json()["chapters"]
    assert listed[0]["status"] == "summarized"
    assert listed[0]["title_zh"] == "第一章"


def test_summarize_chapter_unparseable_reply_leaves_no_summary(tmp_db, tmp_path, monkeypatch):
    """_call_api returning garbage must raise ValueError inside ai.py, which
    the route turns into status='error' — and crucially must NOT leave a
    half-written summary_zh behind."""
    import routes.books as book_routes

    book = _make_book_with_chapters(tmp_path)
    bid = book["book_id"]
    database.derive_chapters(bid)

    monkeypatch.setattr("ai._call_api", lambda *a, **kw: "not json at all")
    monkeypatch.setattr(book_routes.ai, "summarize_book_chapter", ai.summarize_book_chapter)

    resp = client.post(f"/api/books/{bid}/chapters/1/summarize")
    assert resp.status_code == 200

    for _ in range(50):
        chapter = database.get_chapter(bid, 1)
        if chapter["status"] != "pending":
            break
        time.sleep(0.05)
    assert chapter["status"] == "error"
    assert chapter["summary_zh"] is None
    assert chapter["error"]


def test_summarize_chapter_missing_chapter_is_404(tmp_db, tmp_path):
    book = _make_book_with_chapters(tmp_path)
    resp = client.post(f"/api/books/{book['book_id']}/chapters/99/summarize")
    assert resp.status_code == 404


def test_summarize_chapter_rejects_concurrent_duplicate_submission(tmp_db, tmp_path, monkeypatch):
    import threading as th

    import routes.books as book_routes

    book = _make_book_with_chapters(tmp_path)
    bid = book["book_id"]
    database.derive_chapters(bid)

    release = th.Event()

    def _slow_summary(text, **kw):
        release.wait(timeout=2)
        return _fake_summary()

    monkeypatch.setattr(book_routes.ai, "summarize_book_chapter", _slow_summary)

    first = client.post(f"/api/books/{bid}/chapters/1/summarize")
    assert first.status_code == 200
    second = client.post(f"/api/books/{bid}/chapters/1/summarize")
    assert second.status_code == 409

    release.set()
    for _ in range(50):
        if database.get_chapter(bid, 1)["status"] != "pending":
            break
        time.sleep(0.05)


def test_ai_summarize_book_chapter_strips_html_and_parses(monkeypatch):
    captured = {}

    def _fake_call_api(model, messages, max_tokens, purpose, thinking=False):
        captured["prompt"] = messages[0]["content"]
        return json.dumps(_fake_summary())

    monkeypatch.setattr(ai, "_call_api", _fake_call_api)
    result = ai.summarize_book_chapter("<p>Hallo <b>Welt</b>.</p>", book_title="Testbuch",
                                       chapter_label="Erstes Kapitel")
    assert result["summary_zh"].startswith("详细摘要")
    assert result["examples_zh"] == ["原句一。", "原句二。"]
    assert "<p>" not in captured["prompt"]
    assert "Hallo" in captured["prompt"] and "Welt" in captured["prompt"]


def test_ai_summarize_book_chapter_raises_on_missing_summary_field(monkeypatch):
    monkeypatch.setattr(ai, "_call_api",
                        lambda *a, **kw: json.dumps({"title_zh": "x", "concept_zh": "y"}))
    with pytest.raises(ValueError):
        ai.summarize_book_chapter("<p>Text.</p>", book_title="T", chapter_label="Ch1")


def test_ai_summarize_book_chapter_raises_on_empty_text():
    with pytest.raises(ValueError):
        ai.summarize_book_chapter("<p></p>", book_title="T", chapter_label="Ch1")


# --- chapter rescan (#881) ---------------------------------------------------

def _make_book_with_toc(tmp_path, char_budget=20):
    path = tmp_path / "b.epub"
    _write_epub_with_toc(
        path,
        ["<p>Ein Satz.</p><p>Noch ein Satz hier.</p>",
         "<p>Dritter Satz.</p><p>Vierter Satz hier.</p>"],
        nav_entries=[("c0.xhtml", "Erstes Kapitel"), ("c1.xhtml", "Zweites Kapitel")])
    return books.ingest_file(str(path), "b.epub", char_budget=char_budget)


def test_rescan_updates_stale_ref_labels_without_touching_pages(tmp_db, tmp_path):
    """Simulates a book uploaded before #881: its pages carry no ref_label
    (pre-#881 code only ever looked at h1-h3, and these documents have
    none). Rescanning re-parses the same file with today's code and picks
    up the ToC titles, without touching source_text or page_count."""
    book = _make_book_with_toc(tmp_path)
    bid = book["book_id"]
    correct = database.get_all_pages(bid)
    correct_labels = [p["ref_label"] for p in correct]
    assert set(correct_labels) == {"Erstes Kapitel", "Zweites Kapitel"}
    assert correct_labels == sorted(correct_labels, key=lambda l: l != "Erstes Kapitel")

    database.update_page_ref_labels(bid, [None] * book["page_count"])
    assert all(p["ref_label"] is None for p in database.get_all_pages(bid))

    resp = client.post(f"/api/books/{bid}/rescan-chapters")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is True
    assert [c["ref_label"] for c in body["chapters"]] == ["Erstes Kapitel", "Zweites Kapitel"]

    after = database.get_all_pages(bid)
    assert [p["ref_label"] for p in after] == [p["ref_label"] for p in correct]
    assert [p["source_text"] for p in after] == [p["source_text"] for p in correct]
    assert database.get_book(bid)["page_count"] == book["page_count"]


def test_rescan_preserves_summarized_chapters(tmp_db, tmp_path):
    book = _make_book_with_toc(tmp_path)
    bid = book["book_id"]
    chapters = database.derive_chapters(bid)
    assert len(chapters) == 2
    database.save_chapter_summary(
        bid, 1, title_zh="第一章", title_en="Chapter One", concept_zh="核心观点",
        summary_zh="摘要内容", examples_zh=["例句一"])
    summarized_before = database.get_chapter(bid, 1)
    assert summarized_before["status"] == "summarized"

    database.update_page_ref_labels(bid, [None] * book["page_count"])

    resp = client.post(f"/api/books/{bid}/rescan-chapters")
    assert resp.status_code == 200, resp.text

    kept = database.get_chapter(bid, 1)
    assert kept["status"] == "summarized"
    assert kept["title_zh"] == "第一章"
    assert kept["summary_zh"] == "摘要内容"
    # A summarized chapter row is never rewritten by a rescan, even though
    # the underlying pages' ref_label changed and back.
    assert kept["ref_label"] == summarized_before["ref_label"]
    assert kept["start_page"] == summarized_before["start_page"]
    assert kept["end_page"] == summarized_before["end_page"]


def test_rescan_rejects_mismatched_pagination_and_changes_nothing(tmp_db, tmp_path):
    book = _make_book_with_toc(tmp_path)
    bid = book["book_id"]
    before_pages = database.get_all_pages(bid)
    before_book = database.get_book(bid)

    # The underlying file changed since upload -- a completely different book.
    _write_epub(before_book["file_path"], ["<p>Ganz andere Datei.</p>"])

    resp = client.post(f"/api/books/{bid}/rescan-chapters")
    assert resp.status_code == 400
    assert "重新上传" in resp.json()["detail"]

    assert database.get_all_pages(bid) == before_pages
    assert database.get_book(bid)["page_count"] == before_book["page_count"]


def test_rescan_missing_file_is_400(tmp_db, tmp_path):
    book = _make_book_with_toc(tmp_path)
    bid = book["book_id"]
    os.remove(database.get_book(bid)["file_path"])
    resp = client.post(f"/api/books/{bid}/rescan-chapters")
    assert resp.status_code == 400
    assert "重新上传" in resp.json()["detail"]


def test_rescan_rejects_pdf_books(tmp_db, tmp_path):
    book = _make_book(tmp_path)
    bid = book["book_id"]
    conn = database.core.get_db()
    conn.execute("UPDATE books SET format = 'pdf' WHERE id = ?", (bid,))
    conn.commit()
    conn.close()
    resp = client.post(f"/api/books/{bid}/rescan-chapters")
    assert resp.status_code == 400


def test_rescan_missing_book_is_404(tmp_db):
    assert client.post("/api/books/4242/rescan-chapters").status_code == 404


# --- editing book metadata (#882) -------------------------------------------

def test_patch_book_updates_title_author_source_lang(tmp_db, tmp_path):
    book = _make_book(tmp_path)
    bid = book["book_id"]
    before = database.get_book(bid)
    resp = client.patch(f"/api/books/{bid}",
                        json={"title": "Neuer Titel", "author": "Neue Autorin", "source_lang": "en"})
    assert resp.status_code == 200, resp.text
    updated = database.get_book(bid)
    assert updated["title"] == "Neuer Titel"
    assert updated["author"] == "Neue Autorin"
    assert updated["source_lang"] == "en"
    # format/page_count/char_budget describe the actual pagination and must
    # never be touched by this endpoint.
    assert updated["format"] == before["format"]
    assert updated["page_count"] == before["page_count"]
    assert updated["char_budget"] == before["char_budget"]


def test_patch_book_rejects_empty_title(tmp_db, tmp_path):
    book = _make_book(tmp_path)
    bid = book["book_id"]
    original_title = database.get_book(bid)["title"]
    resp = client.patch(f"/api/books/{bid}", json={"title": "   "})
    assert resp.status_code == 400
    assert database.get_book(bid)["title"] == original_title


def test_patch_book_rejects_bad_source_lang(tmp_db, tmp_path):
    book = _make_book(tmp_path)
    bid = book["book_id"]
    resp = client.patch(f"/api/books/{bid}", json={"source_lang": "fr"})
    assert resp.status_code == 400
    assert database.get_book(bid)["source_lang"] == book["source_lang"]


def test_patch_missing_book_is_404(tmp_db):
    resp = client.patch("/api/books/4242", json={"title": "x"})
    assert resp.status_code == 404


# --- per-language chapter renditions (#894) ----------------------------------
# AI writes the chapter summary once, in Chinese; every other language's
# chapter view is a cached Google-translated derivative (books/rendition.py),
# structured like knowledge_renditions (#804).

def _summarized_book(tmp_path):
    """A book with one derived, summarized chapter — everything the
    rendition tests need, without going through the background thread."""
    book = _make_book_with_chapters(tmp_path)
    bid = book["book_id"]
    database.derive_chapters(bid)
    database.save_chapter_summary(
        bid, 1, title_zh="第一章", title_en="Chapter One",
        concept_zh="核心观点一句话。", summary_zh="详细摘要内容。",
        examples_zh=["原句一。", "原句二。"])
    return bid


def _fake_translate(monkeypatch, mapping=None):
    """Patch translator.translate_strict (per #894's testing note: patch the
    real function, not something books/rendition.py imports by name — it
    calls translator.translate_strict(...) as an attribute lookup, so this
    is visible to it) and return the list of texts it was called with."""
    calls = []

    def _fn(text, target="en", source="zh-CN"):
        calls.append(text)
        if mapping and text in mapping:
            return mapping[text]
        return f"[{target}]{text}"

    import translator
    monkeypatch.setattr(translator, "translate_strict", _fn)
    return calls


def test_chapter_rendition_lazily_generated_and_cached(tmp_db, tmp_path, monkeypatch):
    bid = _summarized_book(tmp_path)
    calls = _fake_translate(monkeypatch)

    resp = client.get(f"/api/books/{bid}/chapters/1?lang=fr")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title_zh"] == "[fr]第一章"
    assert body["concept_zh"] == "[fr]核心观点一句话。"
    assert body["summary_zh"] == "[fr]详细摘要内容。"
    assert body["examples_zh"] == ["[fr]原句一。", "原句二。"]  # see mismatch test below for why
    assert "rendition_error" not in body
    first_call_count = len(calls)
    assert first_call_count > 0

    chapter_id = database.get_chapter(bid, 1)["id"]
    cached = database.get_chapter_rendition(chapter_id, "fr")
    assert cached is not None

    # Second request must not re-translate anything.
    resp2 = client.get(f"/api/books/{bid}/chapters/1?lang=fr")
    assert resp2.status_code == 200
    assert resp2.json()["summary_zh"] == body["summary_zh"]
    assert len(calls) == first_call_count


def test_chapter_list_only_translates_short_fields_and_caches(tmp_db, tmp_path, monkeypatch):
    bid = _summarized_book(tmp_path)
    calls = _fake_translate(monkeypatch)

    resp = client.get(f"/api/books/{bid}/chapters?lang=fr")
    assert resp.status_code == 200, resp.text
    chapters = resp.json()["chapters"]
    assert chapters[0]["title_zh"] == "[fr]第一章"
    assert chapters[0]["concept_zh"] == "[fr]核心观点一句话。"
    # summary_zh/examples_zh aren't part of the list payload at all — the
    # list view must never pay for translating them.
    assert "summary_zh" not in chapters[0]
    assert calls == ["第一章", "核心观点一句话。"]

    # Second list request is fully cached.
    resp2 = client.get(f"/api/books/{bid}/chapters?lang=fr")
    assert resp2.json()["chapters"][0]["title_zh"] == "[fr]第一章"
    assert len(calls) == 2

    # A later full request only translates the two fields it was missing
    # (summary/examples) — title/concept came from the cached "short" row.
    resp3 = client.get(f"/api/books/{bid}/chapters/1?lang=fr")
    assert resp3.status_code == 200
    assert resp3.json()["title_zh"] == "[fr]第一章"
    assert "详细摘要内容。" in calls  # summary got translated
    assert calls.count("第一章") == 1  # title was NOT re-translated


def test_chapter_lang_zh_never_touches_rendition_table(tmp_db, tmp_path, monkeypatch):
    bid = _summarized_book(tmp_path)
    calls = _fake_translate(monkeypatch)
    chapter_id = database.get_chapter(bid, 1)["id"]

    plain = database.get_chapter(bid, 1)
    resp_default = client.get(f"/api/books/{bid}/chapters/{1}")
    resp_zh = client.get(f"/api/books/{bid}/chapters/1?lang=zh")
    assert resp_default.json()["title_zh"] == plain["title_zh"] == "第一章"
    assert resp_zh.json()["title_zh"] == "第一章"
    assert calls == []
    assert database.get_chapter_rendition(chapter_id, "zh") is None

    list_resp = client.get(f"/api/books/{bid}/chapters")
    assert list_resp.json()["chapters"][0]["title_zh"] == "第一章"
    assert calls == []


def test_chapter_rendition_failure_reports_and_writes_nothing(tmp_db, tmp_path, monkeypatch):
    import translator

    def _boom(text, target="en", source="zh-CN"):
        raise RuntimeError("translate endpoint down")

    monkeypatch.setattr(translator, "translate_strict", _boom)
    bid = _summarized_book(tmp_path)
    chapter_id = database.get_chapter(bid, 1)["id"]

    resp = client.get(f"/api/books/{bid}/chapters/1?lang=fr")
    assert resp.status_code == 200  # reported inline, not a 5xx
    body = resp.json()
    assert body["rendition_error"]
    assert body["title_zh"] == "第一章"  # Chinese original, untouched
    assert body["summary_zh"] == "详细摘要内容。"
    assert database.get_chapter_rendition(chapter_id, "fr") is None

    # Failure on one chapter doesn't take the list down for the others.
    list_resp = client.get(f"/api/books/{bid}/chapters?lang=fr")
    assert list_resp.status_code == 200
    row = list_resp.json()["chapters"][0]
    assert row["rendition_error"]
    assert row["title_zh"] == "第一章"


def test_save_chapter_summary_clears_stale_renditions(tmp_db, tmp_path):
    bid = _summarized_book(tmp_path)
    chapter_id = database.get_chapter(bid, 1)["id"]
    database.save_chapter_rendition(
        chapter_id, "fr", title="Ancien titre", concept="Ancien concept",
        summary="Ancien résumé", examples=["Ancienne phrase"])
    assert database.get_chapter_rendition(chapter_id, "fr") is not None

    database.save_chapter_summary(
        bid, 1, title_zh="第一章（新）", title_en="Chapter One (new)",
        concept_zh="新观点", summary_zh="新摘要", examples_zh=["新例句"])

    assert database.get_chapter_rendition(chapter_id, "fr") is None


def test_chapter_examples_retried_line_by_line_on_count_mismatch(tmp_db, tmp_path, monkeypatch):
    bid = _summarized_book(tmp_path)
    joined = "原句一。\n原句二。"
    mapping = {
        joined: "合并成一行的错误译文",   # simulates Google collapsing two lines into one
        "原句一。": "Première phrase.",
        "原句二。": "Deuxième phrase.",
        "第一章": "Premier chapitre",
        "核心观点一句话。": "Idée centrale.",
        "详细摘要内容。": "Résumé détaillé.",
    }
    calls = _fake_translate(monkeypatch, mapping)

    resp = client.get(f"/api/books/{bid}/chapters/1?lang=fr")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["examples_zh"] == ["Première phrase.", "Deuxième phrase."]
    # The mismatch forced a retry: the joined call plus one call per example.
    assert calls.count(joined) == 1
    assert calls.count("原句一。") == 1
    assert calls.count("原句二。") == 1
