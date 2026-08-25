"""Known email newsletters that get their own ingestion path (#925).

Why this has to run BEFORE knowledge/mailbox.py's URL branch: a F.A.Z.
Frühdenker mail's body contains dozens of faz.net links (headline links,
"weiterlesen", social share buttons, unsubscribe/imprint footers). Every one
of them sits behind F.A.Z.'s paywall, so knowledge.article.fetch_article()
would fail on each — a guaranteed-failed network round trip per link, on
every poll, for a mail that already contains the real content in its own
body. Newsletters therefore get intercepted by sender address and ingested
as pasted text (knowledge.ingest.ingest_text()) instead of ever reaching the
URL-scanning branch.

Add a new newsletter = add one entry to `_NEWSLETTER_SOURCES` below.
"""
import logging
import re

import knowledge.ingest

logger = logging.getLogger(__name__)

# sender address (lowercase) -> human-readable source name. The name is
# stored as podcast_episodes.channel_id (the "who is this from" column
# every other kind already uses that way — see ingest.py/_store_article).
_NEWSLETTER_SOURCES = {
    "newsletter@nl.faz.net": "F.A.Z. Frühdenker",
}

# Boilerplate lines to drop wholesale. Matched against the WHOLE line
# (case-insensitive substring), not the whole body — a newsletter mixes
# real paragraphs and footer cruft line by line, there's no single
# contiguous "footer block" to slice off. Deliberately narrow: each pattern
# names an actual boilerplate phrase from this newsletter, not a broad
# heuristic, because the body text is the entire point of ingesting this
# mail at all — over-deleting here is worse than under-deleting.
_BOILERPLATE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"zur online-ansicht",
        r"newsletter abbestellen",
        r"abbestellen",
        r"impressum",
        r"datenschutz",
        r"alle rechte vorbehalten",
        r"frankfurter allgemeine zeitung gmbh",
    )
]

# Safety valve for a mail structure we've never actually seen (#925 review):
# if the line-based filter above ends up deleting more than 60% of the body,
# something about this mail's structure broke the "one boilerplate phrase =
# one whole disposable line" assumption — most plausibly the source HTML was
# minified onto one giant line (mailbox._HTMLTextExtractor now inserts
# newlines at block-tag boundaries to prevent exactly that, but a structure
# we haven't seen could still defeat it in some other way). In that failure
# mode clean_body() must NOT trust its own output: returning it would mean
# a mail with real content gets reduced to a near-empty stub, which then
# fails ingest_text()'s length floor and retries forever every 5 minutes
# (this mail can never succeed once truncated, so it's silent forever, not
# just once). Leaving a few boilerplate lines in an otherwise-good body only
# makes the AI summary slightly noisier — completely recoverable, unlike an
# empty body. Better dirty than empty.
_MIN_KEEP_RATIO = 0.4


def source_name(addr: str) -> str | None:
    """Look up a sender address in the known-newsletter registry,
    case-insensitively. None if `addr` isn't a registered newsletter
    sender."""
    if not addr:
        return None
    return _NEWSLETTER_SOURCES.get(addr.strip().lower())


def clean_body(text: str) -> str:
    """Strip newsletter boilerplate lines out of an already-detagged body
    (mailbox.plain_text_body() has already turned the HTML mail into plain
    text — this only removes footer/legal/unsubscribe lines, it does not
    touch markup).

    Deliberately conservative: only whole lines matching a known boilerplate
    phrase are dropped, and consecutive blank lines left behind by removed
    lines are collapsed to one. No line-length heuristics, no "looks like a
    link list" guessing — an over-eager filter here would silently gut the
    one thing this feature exists to capture, the actual newsletter prose.
    """
    if not text:
        return ""
    kept = []
    for line in text.splitlines():
        if any(p.search(line) for p in _BOILERPLATE_PATTERNS):
            continue
        kept.append(line)
    cleaned = "\n".join(kept)
    # Collapse runs of 3+ blank lines (2 blank lines = 1 paragraph break,
    # left alone) left behind by the removed boilerplate lines.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()

    # Bail-out fallback (#925 review, see _MIN_KEEP_RATIO's comment above):
    # if the filter dropped more than 60% of the body, don't trust it —
    # hand back the original, uncleaned text instead of a near-empty stub.
    original_len = len(text.strip())
    if original_len and len(cleaned) < original_len * _MIN_KEEP_RATIO:
        logger.warning(
            "knowledge.newsletter: 样板过滤命中过多（清洗后仅剩 %d/%d 字），"
            "本封放弃清洗，原样返回",
            len(cleaned), original_len,
        )
        return text.strip()

    return cleaned


def ingest_newsletter(sender: str, subject: str, body: str) -> dict:
    """Clean `body` and hand it to knowledge.ingest.ingest_text() as a
    kind='newsletter' row. Same return contract as ingest_text(): either
    {"episode_id": int} (new) or {"status": "already_exists", "episode_id": int}.

    No length check here on purpose — ingest_text() already enforces its
    200-char floor (IngestError if the cleaned body is too short), and
    duplicating that threshold here would be a second place to keep in sync
    with it (the exact trap knowledge/mailbox.py's docstring already warns
    about for its own _MIN_BODY_CHARS gate).
    """
    cleaned = clean_body(body)
    return knowledge.ingest.ingest_text(
        title=subject,
        text=cleaned,
        author=source_name(sender),
        kind="newsletter",
    )
