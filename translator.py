"""
Source language → target language translation using Google Translate's free
mobile endpoint (https://translate.google.com/m).

The source language is configurable (defaults to Chinese, "zh-CN") so this module
can also translate other learner languages (e.g. French) into German.

Why this talks to the endpoint itself instead of using `deep-translator` (#890):
that library scrapes the very same page, but sends `requests`' default
User-Agent — and Google answers `python-requests/x.y` with a JavaScript-only
page that contains no `div.result-container`. Every single translation
therefore raised TranslationNotFound. Because most callers go through
translate_zh(), whose contract is "return the original on failure", the app
degraded silently for a long time (German text served under a Chinese label);
the book reader was the one caller that hard-fails, which is how it surfaced.
Sending a browser User-Agent fixes it outright, so the transport lives here
where that one crucial header is visible.

Standard library only — no `deep-translator`, no `beautifulsoup4`.
Requires internet access (VPN recommended in China).
"""
import concurrent.futures
import logging
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

_translators: dict[tuple[str, str], object] = {}

# One stalled connection must not hang the calling thread forever — a podcast
# check once froze for 14h this way while holding its run lock (#565). urlopen
# gets the timeout too; the thread deadline below is the backstop for a
# connection that trickles bytes slowly enough to never trip it.
_REQUEST_TIMEOUT_SECONDS = 90

_GOOGLE_URL = "https://translate.google.com/m"
# 🔴 Not a politeness header — the entire feature depends on it. Google serves
# `python-requests/…` a JS-only page with no result container, which is exactly
# what broke every translation in the app (#890). Keep a real browser UA here.
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class _ResultParser(HTMLParser):
    """Pull the text out of <div class="result-container">, the element the
    mobile endpoint puts the translation in. convert_charrefs=True means the
    text arrives already unescaped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.result: str | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if self._depth:
            self._depth += 1
            return
        if tag == "div" and "result-container" in dict(attrs).get("class", "").split():
            self._depth = 1
            self.result = ""

    def handle_endtag(self, tag):
        if self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth:
            self.result = (self.result or "") + data


class _GoogleWebTranslator:
    """Minimal stand-in for deep-translator's GoogleTranslator: one
    `.translate(text)` method, so the timeout wrapper and the batching helpers
    below did not have to change."""

    def __init__(self, source: str, target: str):
        self.source = source
        self.target = target

    def translate(self, text: str) -> str:
        if not text.strip():
            return text
        params = urllib.parse.urlencode({"sl": self.source, "tl": self.target, "q": text})
        req = urllib.request.Request(f"{_GOOGLE_URL}?{params}",
                                     headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        parser = _ResultParser()
        parser.feed(body)
        parser.close()
        if parser.result is None:
            # Never return the input as if it were a translation: translate_zh
            # deliberately falls back to the original, translate_strict must
            # raise, and both need this to be an error to tell them apart.
            raise RuntimeError(
                f"no translation in Google's reply (source={self.source}, target={self.target})")
        return parser.result


def _translate_with_timeout(t, text: str) -> str:
    """Run t.translate(text) with a hard deadline on a throwaway thread. On
    timeout the worker thread is abandoned (it dies whenever its socket does)
    and concurrent.futures.TimeoutError propagates to the caller's existing
    fallback handling."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(t.translate, text).result(timeout=_REQUEST_TIMEOUT_SECONDS)
    finally:
        ex.shutdown(wait=False)


def _load(source: str, target: str) -> object | None:
    """Cached translator for this language pair. Kept as a factory returning
    None on failure because both public functions branch on that: translate_zh
    returns the original text, translate_strict raises."""
    key = (source, target)
    if key in _translators:
        return _translators[key]
    if not source or not target:
        logger.error("translator: missing language (source=%r, target=%r)", source, target)
        _translators[key] = None
        return None
    _translators[key] = _GoogleWebTranslator(source, target)
    logger.info("translator: Google web translator ready (source=%s, target=%s)", source, target)
    return _translators[key]


# The free Google endpoint fails intermittently: the same request that just
# succeeded raises TranslationNotFound (or comes back empty) a moment later —
# measured 2 failures in 5 identical de->fr calls (#895). translate_zh never
# noticed because it swallows and returns the original, but translate_strict
# turns one blip into a whole failed rendition, so it retries first.
_STRICT_ATTEMPTS = 3
_STRICT_RETRY_DELAY_SECONDS = 0.7


def translate_strict(text: str, target: str = "en", source: str = "zh-CN") -> str:
    """Like translate_zh, but raises instead of silently returning the
    original text on failure (#804). translate_zh's swallow-everything
    contract is right for its existing callers (a missing gloss shouldn't
    sink a whole story/episode), but knowledge-base language renditions need
    to tell "translated" apart from "translation failed" so a failure can be
    reported to the frontend instead of being stored as if it were a real
    translation into that language.

    Retries transient endpoint failures (#895) before giving up; an empty
    result counts as a failure too, since the callers that need strictness
    (knowledge/rendition.py, the book reader) reject it anyway and would
    otherwise throw away a perfectly retryable attempt.
    """
    t = _load(source, target)
    if t is None:
        raise RuntimeError(f"translator unavailable (source={source}, target={target})")
    if not text.strip():
        return text
    last_error: Exception | None = None
    for attempt in range(_STRICT_ATTEMPTS):
        try:
            translated = _translate_with_timeout(t, text)
            if translated and translated.strip():
                return translated
            last_error = RuntimeError("translator returned empty text")
        except Exception as e:
            last_error = e
        logger.info("translator: strict attempt %d/%d failed (source=%s, target=%s) — %s",
                    attempt + 1, _STRICT_ATTEMPTS, source, target, last_error)
        if attempt + 1 < _STRICT_ATTEMPTS:
            time.sleep(_STRICT_RETRY_DELAY_SECONDS * (attempt + 1))
    raise last_error


def translate_zh(text: str, target: str = "en", source: str = "zh-CN") -> str:
    """Translate a string from `source` to the target language. Returns original on failure."""
    t = _load(source, target)
    if t is None or not text.strip():
        return text
    try:
        return _translate_with_timeout(t, text) or text
    except Exception as e:
        logger.warning("translator: error (source=%s, target=%s) — %s", source, target, e)
        return text


# The free Google endpoint rejects requests beyond ~5000 characters, so a batch
# is split into chunks below that limit (podcast.py did this at its own call
# site; #756 moved it in here so every caller gets it).
_CHUNK_CHAR_BUDGET = 4500


def _translate_chunk(t, texts: list[str], target: str, source: str,
                     on_item=None) -> list[str]:
    """One HTTP request for the whole chunk; per-sentence retry on failure.
    on_item() is called once per sentence in the slow retry path only — the
    joined request has no interior progress to report."""
    sep = "\n"
    combined = sep.join(text.strip() or " " for text in texts)
    try:
        translated = _translate_with_timeout(t, combined) or combined
        parts = translated.split(sep)
        if len(parts) == len(texts):
            return [p.strip() or orig for p, orig in zip(parts, texts)]
        logger.warning("translator: split count mismatch (%d vs %d), falling back", len(parts), len(texts))
    except Exception as e:
        logger.warning("translator: batch error (source=%s, target=%s) — %s", source, target, e)

    out = []
    for text in texts:
        out.append(translate_zh(text, target, source))
        if on_item:
            on_item()
    return out


def translate_batch(texts: list[str], target: str = "en", source: str = "zh-CN",
                    on_progress=None) -> list[str]:
    """Translate a list of strings from `source`, chunked under the endpoint's
    request-size limit. on_progress(done, total) is called after each chunk (and
    after each sentence of a chunk that had to fall back to one request per
    sentence) so callers can show real progress instead of 0/N → N/N (#756)."""
    t = _load(source, target)
    if t is None:
        return texts
    if not texts:
        return texts

    total = len(texts)
    out: list[str] = []
    done = 0

    def _report(extra: int = 0) -> None:
        """Report progress as `len(out) + extra` — out is the single source of
        truth for how many sentences are finished, so the fast path (whole chunk
        at once) and the per-sentence retry path can share one counter."""
        nonlocal done
        n = len(out) + extra
        if n != done:
            done = n
            if on_progress:
                on_progress(done, total)

    def _translate_and_report(chunk: list[str]) -> None:
        # In the retry path the chunk's own results aren't in `out` yet, so the
        # callback counts them via `extra`.
        pending = {"n": 0}

        def _on_item() -> None:
            pending["n"] += 1
            _report(pending["n"])

        out.extend(_translate_chunk(t, chunk, target, source, on_item=_on_item))
        _report()

    chunk: list[str] = []
    size = 0
    for text in texts:
        if chunk and size + len(text) > _CHUNK_CHAR_BUDGET:
            _translate_and_report(chunk)
            chunk, size = [], 0
        chunk.append(text)
        size += len(text) + 1
    if chunk:
        _translate_and_report(chunk)

    return out


# Legacy aliases kept for any callers that used the old API
def translate_zh_en(text: str) -> str:
    return translate_zh(text, target="en")
