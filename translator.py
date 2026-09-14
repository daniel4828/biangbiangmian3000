"""
Source language → target language translation over free, key-less endpoints.

Three transports are tried in order (#1140): Google's mobile page, Google's
translate_a/single JSON endpoint, and Microsoft's key-less Edge translator.
🔴 One transport is not enough: Google blocks datacenter IPs, and when it
starts doing so EVERY translation in the app goes quiet at once — inline word
glosses, the 译 overlay, knowledge renditions, book pages. That is exactly how
this module failed twice now (#890 by User-Agent, #1140 by IP), each time
looking like an application bug rather than a blocked request.

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
import json
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
# Same service, different door: a plain JSON API. Reached when the mobile page
# answers with something that has no result container (a consent page, a
# "Sorry..." block page).
_GOOGLE_JSON_URL = "https://translate.googleapis.com/translate_a/single"
# Microsoft's key-less translator, the one Edge's own page translation uses:
# a short-lived JWT from the first URL authorizes the second. No account, no
# key, and — the reason it is here — a different company's IP policy.
_MS_AUTH_URL = "https://edge.microsoft.com/translate/auth"
# 🔴 The `api-edge` host, not plain `api.` — the latter wants a paid Azure
# subscription key and does not accept the Edge token at all (#1142 shipped the
# wrong one and got HTTP 404 on the server).
_MS_TRANSLATE_URL = "https://api-edge.cognitive.microsofttranslator.com/translate"
# Microsoft serves the auth endpoint to its own browser; a Chrome UA gets a 404.
_EDGE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0")
# The token is good for ~10 minutes; refreshed a little early.
_MS_TOKEN_TTL_SECONDS = 480
_ms_token: dict = {"value": "", "issued_at": 0.0}
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


def _http(url: str, *, data: bytes | None = None, headers: dict | None = None) -> str:
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": _BROWSER_UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_SECONDS) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _google_mobile(text: str, source: str, target: str) -> str:
    """Transport 1: the mobile page, scraped. Longest-serving one, and the
    only one that ever worked from Daniel's laptop behind a VPN in China."""
    params = urllib.parse.urlencode({"sl": source, "tl": target, "q": text})
    body = _http(f"{_GOOGLE_URL}?{params}")
    parser = _ResultParser()
    parser.feed(body)
    parser.close()
    if parser.result is None:
        # Never return the input as if it were a translation: translate_zh
        # deliberately falls back to the original, translate_strict must
        # raise, and both need this to be an error to tell them apart. The
        # page title goes into the message because it is the whole diagnosis:
        # "Sorry..." is Google blocking this IP (#1140), an empty title is
        # the JS-only page of #890.
        raise RuntimeError(f"no result container (page title: {_page_title(body)!r})")
    return parser.result


def _google_json(text: str, source: str, target: str) -> str:
    """Transport 2: translate_a/single, the JSON API the old Google Translate
    widgets use. POST, not GET — a chunk is up to 4500 characters and that is
    past what a URL should carry."""
    params = urllib.parse.urlencode({"client": "gtx", "sl": source, "tl": target, "dt": "t"})
    payload = urllib.parse.urlencode({"q": text}).encode("utf-8")
    body = _http(f"{_GOOGLE_JSON_URL}?{params}", data=payload,
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        data = json.loads(body)
    except ValueError:
        raise RuntimeError(f"not JSON (page title: {_page_title(body)!r})") from None
    # [[[translated, original, …], [translated, original, …], …], …]
    segments = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else None
    if not segments:
        raise RuntimeError("no segments in the JSON reply")
    out = "".join(seg[0] for seg in segments if isinstance(seg, list) and seg and seg[0])
    if not out.strip():
        raise RuntimeError("empty JSON reply")
    return out


def _ms_lang(code: str) -> str:
    """Microsoft's language codes. Chinese is the only one that differs from
    what languages.py stores ("zh-CN" → "zh-Hans")."""
    c = (code or "").lower()
    if c.startswith("zh"):
        return "zh-Hant" if ("tw" in c or "hant" in c or "hk" in c) else "zh-Hans"
    return c.split("-")[0]


def _ms_auth_token() -> str:
    now = time.time()
    if _ms_token["value"] and now - _ms_token["issued_at"] < _MS_TOKEN_TTL_SECONDS:
        return _ms_token["value"]
    try:
        token = _http(_MS_AUTH_URL, headers={"User-Agent": _EDGE_UA}).strip()
    except Exception as e:
        # Which of the two calls failed is the whole diagnosis — a bare
        # "HTTP 404" left #1142 unable to tell a wrong URL from a wrong token.
        raise RuntimeError(f"auth step: {e}") from None
    if not token or "." not in token:
        raise RuntimeError("auth step: no JWT in the reply")
    _ms_token.update(value=token, issued_at=now)
    return token


def _microsoft(text: str, source: str, target: str) -> str:
    """Transport 3: Microsoft's key-less Edge translator.

    🔴 Sends one array element PER LINE and rejoins them with newlines. The batching
    above packs many sentences into one request separated by newlines and
    splits the answer back apart by line, which with the Google transports
    rests on the endpoint preserving line breaks. Here the line structure is
    carried by the protocol itself, so it cannot drift."""
    lines = text.split("\n")
    payload = json.dumps([{"Text": line or " "} for line in lines]).encode("utf-8")
    params = urllib.parse.urlencode({"api-version": "3.0",
                                     "from": _ms_lang(source), "to": _ms_lang(target)})
    token = _ms_auth_token()
    try:
        body = _http(f"{_MS_TRANSLATE_URL}?{params}", data=payload,
                     headers={"Content-Type": "application/json; charset=utf-8",
                              "User-Agent": _EDGE_UA,
                              "Authorization": f"Bearer {token}"})
    except Exception as e:
        raise RuntimeError(f"translate step: {e}") from None
    try:
        data = json.loads(body)
    except ValueError:
        raise RuntimeError(f"not JSON (page title: {_page_title(body)!r})") from None
    if not isinstance(data, list) or len(data) != len(lines):
        raise RuntimeError(f"expected {len(lines)} results, got "
                           f"{len(data) if isinstance(data, list) else type(data).__name__}")
    out = []
    for item in data:
        try:
            out.append(item["translations"][0]["text"])
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("unexpected result shape") from None
    return "\n".join(out)


# Human-readable names for the AI transport's prompt. Anything not listed
# goes in as the raw code — the models understand "fr" perfectly well.
_LANG_NAMES = {"zh-CN": "Chinese", "zh": "Chinese", "de": "German",
               "en": "English", "fr": "French", "es": "Spanish"}


def _deepseek(text: str, source: str, target: str) -> str:
    """Last resort: the app's own AI account (#1144).

    Every transport above is somebody else's free tier, reachable only as long
    as they tolerate this server's IP — and when they stop, ALL reading help
    dies at once (that is #1140/#1142, twice in one day: Google answering 429
    to both of its doors). This one runs on Daniel's own DeepSeek key, so it
    cannot be taken away, and it is only ever reached after the free doors
    have refused: roughly $0.002 for a whole article, logged under
    purpose="translate_fallback" so it shows up in /api/costs like everything
    else.

    Skipped entirely when AI is switched off (offline mode, DISABLE_AI) or no
    key is configured — an offline laptop must not sit here waiting on a
    network call."""
    import os

    try:
        from routes.utils import ai_disabled
        if ai_disabled():
            raise RuntimeError("AI is disabled on this instance")
    except ImportError:
        pass
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("no DEEPSEEK_API_KEY configured")

    import ai

    lines = text.split("\n")
    src = _LANG_NAMES.get(source, source)
    tgt = _LANG_NAMES.get(target, target)
    prompt = (
        f"Translate the following {src} text into {tgt}.\n"
        f"The text has exactly {len(lines)} lines. Reply with exactly {len(lines)} "
        "lines: one translation per input line, in the same order.\n"
        "No numbering, no commentary, no markdown, no blank lines added or removed. "
        "Keep any HTML tags exactly as they are.\n\n"
        f"{text}"
    )
    out = ai._call_api(ai.DEFAULT_MODEL, [{"role": "user", "content": prompt}],
                       4096, purpose="translate_fallback")
    out = (out or "").strip("\n")
    got = out.split("\n")
    if len(got) != len(lines):
        # Never guess which line went where: the batcher above splits the
        # answer by line and would silently pair sentences with the wrong
        # translations. A mismatch is a failure, and its per-item fallback
        # (one line per request) gets it right.
        raise RuntimeError(f"expected {len(lines)} lines, got {len(got)}")
    return out


class _TitleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in = False

    def handle_starttag(self, tag, attrs):
        self._in = tag == "title"

    def handle_endtag(self, tag):
        if tag == "title":
            self._in = False

    def handle_data(self, data):
        if self._in:
            self.title += data


def _page_title(body: str) -> str:
    """The <title> of whatever came back instead of a translation — this one
    string is usually the entire diagnosis, so it travels in the error."""
    try:
        p = _TitleParser()
        p.feed(body[:4000])
        p.close()
        return p.title.strip()[:80]
    except Exception:
        return ""


# Order matters: Google first (it has served this app all along and handles
# Chinese best), Microsoft as the way out when Google refuses this machine.
_TRANSPORTS = (
    ("google-mobile", _google_mobile),
    ("google-json", _google_json),
    ("microsoft-edge", _microsoft),
    ("deepseek", _deepseek),
)

# A door that answered 429/403 is throttling this machine; asking it again a
# second later costs a round trip, gets the same answer, and keeps the counter
# that produced the ban warm. Skipped for a while instead (#1144).
_THROTTLE_COOLDOWN_SECONDS = 900
# name → (skip until, why it was skipped). The reason travels with the
# cooldown so the diagnosis ("Sorry...", "429") survives into every later
# error message — a bare "skipped" would hide exactly what one needs to know.
_cooldowns: dict[str, tuple[float, str]] = {}


def _is_throttled_error(e: Exception) -> bool:
    code = getattr(e, "code", None)
    if code in (403, 429):
        return True
    text = str(e)
    return "429" in text or "Too Many Requests" in text or "Sorry..." in text


def _cooldown_left(name: str) -> tuple[int, str]:
    """(seconds left, reason) for a door being skipped; (0, "") when it is
    free to try again."""
    until, reason = _cooldowns.get(name, (0.0, ""))
    left = int(until - time.time())
    if left > 0:
        return left, reason
    _cooldowns.pop(name, None)
    return 0, ""


class _WebTranslator:
    """Minimal stand-in for deep-translator's GoogleTranslator: one
    `.translate(text)` method, so the timeout wrapper and the batching helpers
    below did not have to change.

    Walks _TRANSPORTS until one answers, and then REMEMBERS which one (#1140):
    when Google is blocking this server, paying two failed requests before
    every single translation would make a reading page take minutes. The
    preference is per language pair and is dropped again the moment its
    transport fails, so a temporary outage does not pin the app to a worse
    endpoint forever."""

    def __init__(self, source: str, target: str):
        self.source = source
        self.target = target
        self.preferred: str | None = None

    def translate(self, text: str) -> str:
        if not text.strip():
            return text
        order = sorted(_TRANSPORTS, key=lambda t: t[0] != self.preferred)
        errors = []
        for name, fn in order:
            left, reason = _cooldown_left(name)
            if left:
                errors.append(f"{name}: skipped for {left}s after — {reason}")
                continue
            try:
                out = fn(text, self.source, self.target)
            except Exception as e:
                errors.append(f"{name}: {e}")
                if _is_throttled_error(e):
                    _cooldowns[name] = (time.time() + _THROTTLE_COOLDOWN_SECONDS, str(e))
                    logger.warning("translator: %s is throttling us, skipping it for %d min",
                                   name, _THROTTLE_COOLDOWN_SECONDS // 60)
                if self.preferred == name:
                    self.preferred = None
                continue
            if out and out.strip():
                if self.preferred != name:
                    logger.info("translator: using %s (source=%s, target=%s)",
                                name, self.source, self.target)
                    self.preferred = name
                return out
            errors.append(f"{name}: empty answer")
        raise RuntimeError(f"every translation transport failed — {'; '.join(errors)}")


# The old name, kept because it reads as "the Google one" in existing logs.
_GoogleWebTranslator = _WebTranslator


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
    _translators[key] = _WebTranslator(source, target)
    logger.info("translator: web translator ready (source=%s, target=%s)", source, target)
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

# How many sentences of a failed chunk may miss in a row before the rest of
# that chunk is given up on — see _translate_chunk's fallback loop.
_ITEM_FALLBACK_MAX_MISSES = 3


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

    # Per-sentence retry, but bounded (#1140). When the chunk failed because
    # the free endpoint is refusing us (rate limit after a burst, a temporary
    # block), retrying every sentence of the chunk one by one fires dozens
    # more requests into exactly that refusal — it deepens the throttling and
    # costs minutes. After a few consecutive misses the rest of the chunk is
    # handed back unchanged, which is translate_batch's documented failure
    # contract anyway. A chunk that failed for a LOCAL reason (one over-long
    # text in it) still gets fully rescued: the other sentences succeed, so
    # the counter never reaches the limit.
    out = []
    misses = 0
    for i, text in enumerate(texts):
        if misses >= _ITEM_FALLBACK_MAX_MISSES:
            logger.warning("translator: giving up on the remaining %d sentences of this "
                           "chunk after %d consecutive failures (source=%s, target=%s)",
                           len(texts) - i, misses, source, target)
            for rest in texts[i:]:
                out.append(rest)
                if on_item:
                    on_item()
            break
        translated = translate_zh(text, target, source)
        # translate_zh returns the input on failure — that is what a miss
        # looks like from here.
        misses = misses + 1 if (translated == text and text.strip()) else 0
        out.append(translated)
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


def selftest(text: str = "你好，世界。", source: str = "zh-CN",
             target: str = "de") -> list[dict]:
    """Ask every transport the same tiny question and report what each said.

    Exists because this module's two outages (#890, #1140) were both invisible
    from the outside: every caller degrades politely, so a blocked endpoint
    looks exactly like an application bug. Three requests, no state touched —
    open /api/translate-selftest and the answer names the broken door.
    """
    results = []
    for name, fn in _TRANSPORTS:
        started = time.time()
        entry = {"transport": name, "ok": False, "ms": 0}
        # The self-test asks the door itself, cooldown or not — its whole job
        # is to report the door's real state.
        cooling, _reason = _cooldown_left(name)
        if cooling:
            entry["cooldown_seconds"] = cooling
        try:
            out = (fn(text, source, target) or "").strip()
            entry["ok"] = bool(out)
            entry["result"] = out[:200]
            if not out:
                entry["error"] = "empty answer"
        except Exception as e:
            entry["error"] = str(e)[:300]
        entry["ms"] = int((time.time() - started) * 1000)
        results.append(entry)
    return results


# Legacy aliases kept for any callers that used the old API
def translate_zh_en(text: str) -> str:
    return translate_zh(text, target="en")
