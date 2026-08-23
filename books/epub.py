"""EPUB text extraction (#836), standard library only.

An EPUB is a zip: META-INF/container.xml points at an OPF package file, whose
<manifest> maps ids to documents and whose <spine> lists those ids in reading
order. Walking that is a few dozen lines of zipfile + ElementTree, which is
why this module adds no dependency — `ebooklib`/`beautifulsoup4` would be two
new packages for work the stdlib already does.

trafilatura (already a dependency, used by knowledge/article.py) is not used
here either: it is tuned to find *one* article inside a noisy web page and
routinely drops chapter headings and short paragraphs from clean book XHTML.
A book chapter has no boilerplate to strip, so a plain block-level text walk
is both simpler and more faithful.

Chapter labelling (#881): most EPUBs' chapter titles are not <h1>/<h2>/<h3> —
publishers style them as <div class="chapter-title">, images, or nothing at
all readable as a heading. The reliable source is the EPUB's own table of
contents, which every valid EPUB carries: EPUB3's nav.xhtml (a manifest item
with properties="nav", containing <nav epub:type="toc">) or EPUB2's toc.ncx
(found via the spine's toc= attribute). _parse_toc() reads whichever exists
into {spine document → title}; extract() uses that to seed each spine
document's label, letting an in-document h1-h3 still override it partway
through (rare multi-chapter files), and falling back to the filename only
when a document has neither a ToC entry nor any heading of its own.
"""
import logging
import posixpath
import re
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

_CONTAINER = "META-INF/container.xml"
# Elements whose content is markup/styling, never prose.
_SKIP_CONTENT = {"script", "style", "head", "title"}
# Elements that end the current paragraph.
_BLOCK = {"p", "div", "br", "li", "tr", "td", "blockquote", "section", "article",
          "h1", "h2", "h3", "h4", "h5", "h6", "figcaption", "pre"}
_HEADINGS = {"h1", "h2", "h3"}


class BookExtractionError(Exception):
    """The file could not be turned into readable text. Raised rather than
    storing an empty book: a book that opens to a blank page is a worse
    outcome than a failed upload, and the cause (DRM, no text layer, damaged
    archive) is exactly what Daniel needs told to him."""


class _TextBlocks(HTMLParser):
    """Collect block-level text runs, remembering the most recent heading so
    pages can be labelled with the chapter they start in.

    `initial_heading` seeds the label before any in-document h1-h3 is seen
    (#881: the EPUB's own table of contents, when there is one). A real
    heading tag still overrides it the moment one is encountered — a single
    XHTML document containing several chapters (rare, but it happens) must
    still split at its own headings, not collapse to the ToC's one title."""

    def __init__(self, initial_heading: str | None = None):
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self.heading: str | None = initial_heading
        self._buf: list[str] = []
        self._skip = 0
        self._in_heading = False

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        if not text:
            return
        if self._in_heading:
            self.heading = text
        self.blocks.append({"text": text, "ref_label": self.heading})

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_CONTENT:
            self._skip += 1
        elif tag in _BLOCK:
            self._flush()
            self._in_heading = tag in _HEADINGS

    def handle_endtag(self, tag):
        if tag in _SKIP_CONTENT:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK:
            self._flush()
            self._in_heading = False

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _opf_path(zf: zipfile.ZipFile) -> str:
    try:
        root = ElementTree.fromstring(zf.read(_CONTAINER))
    except (KeyError, ElementTree.ParseError) as e:
        raise BookExtractionError(f"not a valid EPUB (no {_CONTAINER}): {e}") from e
    for el in root.iter():
        if _strip_ns(el.tag) == "rootfile" and el.get("full-path"):
            return el.get("full-path")
    raise BookExtractionError("EPUB container.xml names no rootfile")


def _nav_toc_entries(raw: bytes) -> list[tuple[str, str]]:
    """[(href, title)] in document order from the first
    <nav epub:type="toc"> in this EPUB3 navigation document. `href`s are
    exactly as written (relative to this document) — the caller resolves
    them. [] if the document doesn't parse or has no toc nav (only a
    landmarks/page-list nav, say)."""
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as e:
        logger.warning("books.epub: cannot parse nav document — %s", e)
        return []
    toc_nav = None
    for el in root.iter():
        if _strip_ns(el.tag) != "nav":
            continue
        if any(_strip_ns(k) == "type" and v == "toc" for k, v in el.attrib.items()):
            toc_nav = el
            break
    if toc_nav is None:
        return []
    entries = []
    for a in toc_nav.iter():
        if _strip_ns(a.tag) != "a":
            continue
        href = a.get("href")
        if not href:
            continue
        text = re.sub(r"\s+", " ", "".join(a.itertext())).strip()
        if text:
            entries.append((href, text))
    return entries


def _ncx_toc_entries(raw: bytes) -> list[tuple[str, str]]:
    """[(src, title)] in document order from an EPUB2 toc.ncx's navMap.
    Nested navPoints (sub-chapters) are included too, in document order —
    good enough for grouping spine documents into chapters, which is the
    only thing this is used for."""
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as e:
        logger.warning("books.epub: cannot parse toc.ncx — %s", e)
        return []
    entries = []
    for navpoint in root.iter():
        if _strip_ns(navpoint.tag) != "navPoint":
            continue
        label, src = None, None
        for child in navpoint:
            name = _strip_ns(child.tag)
            if name == "navLabel" and label is None:
                for sub in child.iter():
                    if _strip_ns(sub.tag) == "text" and (sub.text or "").strip():
                        label = sub.text.strip()
                        break
            elif name == "content" and src is None:
                src = child.get("src")
        if label and src:
            entries.append((src, label))
    return entries


def _resolve_toc_entries(entries: list[tuple[str, str]], doc_base: str) -> dict[str, str]:
    """[(href, title)] relative to `doc_base` → {normalized zip path: title},
    fragments stripped. A spine document can be pointed at by more than one
    toc entry (sub-sections of the same chapter) — the first one wins, since
    that is the entry marking where the document/chapter starts."""
    toc: dict[str, str] = {}
    for href, title in entries:
        target = href.split("#", 1)[0]
        if not target:
            continue
        path = posixpath.normpath(posixpath.join(doc_base, target)) if doc_base else target
        toc.setdefault(path, title)
    return toc


def _parse_toc(zf: zipfile.ZipFile, root, base: str) -> dict[str, str]:
    """{"normalized zip path of a spine document": "chapter title"} read from
    the EPUB's own table of contents — EPUB3's nav.xhtml (manifest item with
    properties="nav") if present, else EPUB2's toc.ncx (found via the
    spine's toc= attribute). {} if neither is present or parses cleanly,
    which just means extract() falls back to in-document headings / one
    chapter per spine file (#881)."""
    manifest_items = {}
    for el in root.iter():
        if _strip_ns(el.tag) == "item" and el.get("id") and el.get("href"):
            manifest_items[el.get("id")] = el

    nav_item = next(
        (item for item in manifest_items.values()
         if "nav" in (item.get("properties") or "").split()),
        None)
    if nav_item is not None:
        href = nav_item.get("href")
        nav_path = posixpath.normpath(posixpath.join(base, href)) if base else href
        try:
            raw = zf.read(nav_path)
        except KeyError:
            raw = None
        if raw is not None:
            entries = _nav_toc_entries(raw)
            if entries:
                return _resolve_toc_entries(entries, posixpath.dirname(nav_path))

    spine_el = next((el for el in root.iter() if _strip_ns(el.tag) == "spine"), None)
    toc_id = spine_el.get("toc") if spine_el is not None else None
    if toc_id and toc_id in manifest_items:
        href = manifest_items[toc_id].get("href")
        ncx_path = posixpath.normpath(posixpath.join(base, href)) if base else href
        try:
            raw = zf.read(ncx_path)
        except KeyError:
            raw = None
        if raw is not None:
            entries = _ncx_toc_entries(raw)
            if entries:
                return _resolve_toc_entries(entries, posixpath.dirname(ncx_path))

    return {}


def _metadata(root) -> tuple[str | None, str | None]:
    title = author = None
    for el in root.iter():
        name, text = _strip_ns(el.tag), (el.text or "").strip()
        if not text:
            continue
        if name == "title" and title is None:
            title = text
        elif name == "creator" and author is None:
            author = text
    return title, author


def extract(path: str) -> dict:
    """{"title", "author", "blocks"} for an EPUB file.

    Raises BookExtractionError when the archive is unreadable, DRM-protected
    (its documents decompress to markup with no prose) or simply empty.
    """
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise BookExtractionError(f"cannot open EPUB: {e}") from e

    with zf:
        opf = _opf_path(zf)
        base = posixpath.dirname(opf)
        try:
            root = ElementTree.fromstring(zf.read(opf))
        except (KeyError, ElementTree.ParseError) as e:
            raise BookExtractionError(f"cannot read EPUB package file: {e}") from e

        manifest = {}
        spine: list[str] = []
        for el in root.iter():
            name = _strip_ns(el.tag)
            if name == "item" and el.get("id") and el.get("href"):
                manifest[el.get("id")] = el.get("href")
            elif name == "itemref" and el.get("idref"):
                spine.append(el.get("idref"))
        if not spine:
            # Some hand-made EPUBs have no usable spine; fall back to every
            # XHTML document in the manifest, in manifest order.
            spine = [i for i, href in manifest.items()
                     if href.lower().endswith((".xhtml", ".html", ".htm"))]

        title, author = _metadata(root)
        try:
            toc = _parse_toc(zf, root, base)
        except Exception as e:  # a malformed nav/ncx must not sink the book
            logger.warning("books.epub: cannot read table of contents — %s", e)
            toc = {}

        blocks: list[dict] = []
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            name = posixpath.normpath(posixpath.join(base, href)) if base else href
            try:
                raw = zf.read(name)
            except KeyError:
                logger.warning("books.epub: spine item %s missing from archive", name)
                continue
            # Chapter title priority (#881): ToC entry > first in-document
            # h1-h3 > filename. The ToC title only seeds the label — an
            # h1-h3 encountered partway through the document still starts a
            # new label, so a document holding several chapters still splits.
            toc_title = toc.get(name)
            parser = _TextBlocks(initial_heading=toc_title)
            try:
                parser.feed(raw.decode("utf-8", errors="replace"))
                parser.close()
            except Exception as e:  # a malformed chapter must not sink the book
                logger.warning("books.epub: cannot parse %s — %s", name, e)
                continue
            doc_blocks = parser.blocks
            # No ToC entry and no heading yet → the only distinguishing label
            # left is the filename. This also covers the blocks *before* a
            # document's first heading: leaving them None would merge every
            # document's front matter into one chapter spanning the book,
            # since derive_chapters() groups consecutive equal labels.
            fallback = posixpath.splitext(posixpath.basename(href))[0]
            for b in doc_blocks:
                if b["ref_label"] is None:
                    b["ref_label"] = fallback
                b["src"] = name
            blocks.extend(doc_blocks)

    if not blocks:
        raise BookExtractionError(
            "no readable text found in this EPUB — it may be DRM-protected or image-only")
    return {"title": title, "author": author, "blocks": blocks}
