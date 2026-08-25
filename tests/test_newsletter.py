"""Tests for knowledge/newsletter.py and its integration into
knowledge/mailbox.py (issue #925).

Same faking approach as tests/test_knowledge_mailbox.py: IMAP is a stub
object matching the imaplib.IMAP4_SSL interface, and knowledge.ingest /
podcast are monkeypatched rather than exercising the real network/AI paths.
"""
from email.message import EmailMessage

import pytest

import knowledge.ingest
import knowledge.mailbox as mailbox
import knowledge.newsletter as newsletter
import podcast


# ---------------------------------------------------------------------------
# source_name()
# ---------------------------------------------------------------------------

def test_source_name_hit():
    assert newsletter.source_name("newsletter@nl.faz.net") == "F.A.Z. Frühdenker"


def test_source_name_case_insensitive():
    assert newsletter.source_name("Newsletter@NL.FAZ.NET") == "F.A.Z. Frühdenker"


def test_source_name_miss():
    assert newsletter.source_name("someone@example.com") is None


def test_source_name_empty():
    assert newsletter.source_name("") is None
    assert newsletter.source_name(None) is None


# ---------------------------------------------------------------------------
# clean_body()
# ---------------------------------------------------------------------------

def test_clean_body_drops_boilerplate_lines_keeps_prose():
    body = (
        "Guten Morgen,\n"
        "hier ist Ihr Frühdenker.\n"
        "Zur Online-Ansicht klicken Sie hier.\n"
        "Die Wirtschaft wächst langsamer als erwartet.\n"
        "Newsletter abbestellen\n"
        "Impressum\n"
        "Datenschutz\n"
        "Frankfurter Allgemeine Zeitung GmbH\n"
        "Alle Rechte vorbehalten.\n"
        "Mit besten Grüßen, Ihre Redaktion"
    )
    cleaned = newsletter.clean_body(body)
    assert "Guten Morgen" in cleaned
    assert "hier ist Ihr Frühdenker" in cleaned
    assert "Die Wirtschaft wächst langsamer als erwartet" in cleaned
    assert "Mit besten Grüßen" in cleaned
    assert "Zur Online-Ansicht" not in cleaned
    assert "abbestellen" not in cleaned.lower()
    assert "Impressum" not in cleaned
    assert "Datenschutz" not in cleaned
    assert "Frankfurter Allgemeine Zeitung GmbH" not in cleaned
    assert "Alle Rechte vorbehalten" not in cleaned


def test_clean_body_collapses_blank_lines_left_by_removed_boilerplate():
    body = "Erster Absatz.\n\nZur Online-Ansicht\n\n\n\nZweiter Absatz."
    cleaned = newsletter.clean_body(body)
    assert "\n\n\n" not in cleaned
    assert "Erster Absatz." in cleaned
    assert "Zweiter Absatz." in cleaned


def test_clean_body_empty():
    assert newsletter.clean_body("") == ""
    assert newsletter.clean_body(None) == ""


def test_clean_body_over_deletion_falls_back_to_original(caplog):
    """#925 review fix: if the boilerplate filter would delete more than
    _MIN_KEEP_RATIO of the body (here: the WHOLE body is boilerplate),
    clean_body() must not trust its own output — it hands back the
    original text unchanged rather than an empty/near-empty stub. An
    empty stub would fail ingest_text()'s length floor and retry the same
    mail forever; a few leftover boilerplate lines are harmless."""
    import logging
    caplog.set_level(logging.WARNING, logger="knowledge.newsletter")
    body = "Zur Online-Ansicht\nAbbestellen\nImpressum\nDatenschutz"
    result = newsletter.clean_body(body)
    assert result == body
    assert "样板过滤命中过多" in caplog.text


def test_clean_body_moderate_boilerplate_still_gets_cleaned():
    """Sanity check that the fallback threshold doesn't defeat normal
    cleaning — a body that's mostly real content with a few boilerplate
    lines still gets those lines removed."""
    body = (
        "Guten Morgen,\n"
        "hier ist Ihr Frühdenker.\n"
        "Die Wirtschaft wächst langsamer als erwartet, sagen Ökonomen.\n"
        "Newsletter abbestellen\n"
        "Impressum\n"
    )
    cleaned = newsletter.clean_body(body)
    assert cleaned != body
    assert "abbestellen" not in cleaned.lower()
    assert "Die Wirtschaft wächst langsamer" in cleaned


# ---------------------------------------------------------------------------
# ingest_newsletter() -> knowledge.ingest.ingest_text() with kind='newsletter'
# ---------------------------------------------------------------------------

def test_ingest_newsletter_calls_ingest_text_with_kind_newsletter(monkeypatch):
    calls = []

    def fake_ingest_text(title, text, source_url=None, author=None,
                          china_critical=False, fallback_title=None, kind="article"):
        calls.append({"title": title, "text": text, "author": author, "kind": kind})
        return {"episode_id": 1}

    monkeypatch.setattr(knowledge.ingest, "ingest_text", fake_ingest_text)

    body = "Guten Morgen,\n" + ("Die Wirtschaft entwickelt sich unterschiedlich. " * 10)
    result = newsletter.ingest_newsletter("newsletter@nl.faz.net", "Frühdenker: Heute", body)

    assert result == {"episode_id": 1}
    assert len(calls) == 1
    assert calls[0]["title"] == "Frühdenker: Heute"
    assert calls[0]["author"] == "F.A.Z. Frühdenker"
    assert calls[0]["kind"] == "newsletter"
    # Body handed to ingest_text is the CLEANED body, not the raw one.
    assert "Guten Morgen" in calls[0]["text"]


# ---------------------------------------------------------------------------
# check_mailbox() — newsletter branch takes priority over the URL branch
# ---------------------------------------------------------------------------

def _make_plain_message(subject: str, body: str, sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def _make_html_message(subject: str, html_body: str, sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content("(html only)")
    msg.add_alternative(html_body, subtype="html")
    return msg


def _configure_env(monkeypatch, allowed="newsletter@nl.faz.net"):
    monkeypatch.setenv("KNOWLEDGE_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("KNOWLEDGE_IMAP_USER", "kb@example.com")
    monkeypatch.setenv("KNOWLEDGE_IMAP_PASSWORD", "secret")
    if allowed is not None:
        monkeypatch.setenv("KNOWLEDGE_MAIL_ALLOWED_SENDERS", allowed)


@pytest.fixture(autouse=True)
def _knowledge_mail_env(monkeypatch):
    for var in (
        "KNOWLEDGE_MAIL_ALLOWED_SENDERS", "KNOWLEDGE_IMAP_HOST",
        "KNOWLEDGE_IMAP_PORT", "KNOWLEDGE_IMAP_USER", "KNOWLEDGE_IMAP_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)


class FakeImap:
    def __init__(self, messages):
        self._messages = messages
        self.seen_flagged = []

    def login(self, user, password):
        return "OK", [b"logged in"]

    def select(self, mailbox_name):
        return "OK", [b"1"]

    def search(self, charset, criterion):
        ids = b" ".join(self._messages.keys())
        return "OK", [ids]

    def fetch(self, msg_id, parts):
        msg = self._messages.get(msg_id)
        if msg is None:
            return "NO", [None]
        raw = msg.as_bytes() if hasattr(msg, "as_bytes") else msg
        return "OK", [(b"1 (RFC822 {123})", raw)]

    def store(self, msg_id, flag_set, flags):
        self.seen_flagged.append(msg_id)
        return "OK", [b"done"]

    def close(self):
        return "OK", [b"closed"]

    def logout(self):
        return "OK", [b"logged out"]


_NEWSLETTER_BODY = (
    "Guten Morgen,\n"
    "hier die wichtigsten Themen des Tages:\n"
    "Ein Artikel: https://www.faz.net/aktuell/wirtschaft/artikel-1.html\n"
    "Noch einer: https://www.faz.net/aktuell/politik/artikel-2.html\n"
    "Und ein dritter: https://www.faz.net/aktuell/feuilleton/artikel-3.html\n"
    + ("Die Konjunktur zeigt gemischte Signale in diesem Monat. " * 8)
    + "\nNewsletter abbestellen\nImpressum\n"
)


def test_newsletter_sender_takes_priority_over_url_branch(monkeypatch):
    """This is the most important regression test for #925: a newsletter
    mail whose body is full of (paywalled) faz.net links must be routed to
    knowledge.newsletter.ingest_newsletter() / ingest_text(kind='newsletter'),
    never to ingest_url() — ingest_url() would try (and fail) to fetch every
    one of those links instead of using the content already in the body."""
    _configure_env(monkeypatch, allowed="newsletter@nl.faz.net")
    msg = _make_plain_message(
        "F.A.Z. Frühdenker: Die Lage am Morgen",
        _NEWSLETTER_BODY,
        sender="newsletter@nl.faz.net",
    )
    fake = FakeImap({b"1": msg})

    url_calls = []
    text_calls = []
    retry_calls = []

    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: url_calls.append(url) or {"episode_id": 99})
    monkeypatch.setattr(
        knowledge.ingest, "ingest_text",
        lambda title, text, source_url=None, author=None, china_critical=False,
               fallback_title=None, kind="article":
            text_calls.append({"title": title, "kind": kind, "author": author}) or {"episode_id": 42},
    )
    monkeypatch.setattr(podcast, "retry_episode", lambda episode_id: retry_calls.append(episode_id) or {"status": "summarized"})

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    # The whole point: ingest_url() must NEVER be called for this mail.
    assert url_calls == []
    assert len(text_calls) == 1
    assert text_calls[0]["kind"] == "newsletter"
    assert text_calls[0]["author"] == "F.A.Z. Frühdenker"
    # Immediate synchronous processing (同 signal_inbox.py 的 "早上就要读").
    assert retry_calls == [42]
    assert summary["processed"] == 1
    assert summary["ingested"] == 1
    assert fake.seen_flagged == [b"1"]


def test_newsletter_already_exists_skips_retry_but_still_marks_seen(monkeypatch):
    _configure_env(monkeypatch, allowed="newsletter@nl.faz.net")
    msg = _make_plain_message(
        "F.A.Z. Frühdenker: Nochmal die gleiche Ausgabe",
        _NEWSLETTER_BODY,
        sender="newsletter@nl.faz.net",
    )
    fake = FakeImap({b"1": msg})

    retry_calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: (_ for _ in ()).throw(
        AssertionError("ingest_url must not be called for a newsletter sender")))
    monkeypatch.setattr(
        knowledge.ingest, "ingest_text",
        lambda title, text, source_url=None, author=None, china_critical=False,
               fallback_title=None, kind="article": {"status": "already_exists", "episode_id": 7},
    )
    monkeypatch.setattr(podcast, "retry_episode", lambda episode_id: retry_calls.append(episode_id))

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert retry_calls == []  # already-existing rows are not reprocessed
    assert summary["processed"] == 1
    assert fake.seen_flagged == [b"1"]


def test_newsletter_permanent_ingest_error_marks_seen(monkeypatch):
    """#925 review fix: IngestError (e.g. cleaned body too short) is a
    PERMANENT failure — the same mail will fail identically on every future
    poll, so retrying it forever (every 5 minutes, via cron) accomplishes
    nothing. Unlike the URL/text-fallback branches (which retry on any
    failure), the newsletter branch must mark such a mail \\Seen so it's
    abandoned instead of retried forever."""
    _configure_env(monkeypatch, allowed="newsletter@nl.faz.net")
    msg = _make_plain_message(
        "F.A.Z. Frühdenker", _NEWSLETTER_BODY, sender="newsletter@nl.faz.net",
    )
    fake = FakeImap({b"1": msg})

    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: (_ for _ in ()).throw(
        AssertionError("should not be reached")))

    def failing_ingest_text(title, text, source_url=None, author=None,
                             china_critical=False, fallback_title=None, kind="article"):
        raise knowledge.ingest.IngestError("too short")

    monkeypatch.setattr(knowledge.ingest, "ingest_text", failing_ingest_text)

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert summary["failed"] == 1
    assert summary["processed"] == 0
    assert fake.seen_flagged == [b"1"]  # marked seen — abandoned, not retried


def test_newsletter_transient_error_not_marked_seen(monkeypatch):
    """A non-IngestError failure (network blip, DB hiccup, ...) is
    transient — the mail must stay unread so the next poll retries it,
    exactly like every other branch in this module."""
    _configure_env(monkeypatch, allowed="newsletter@nl.faz.net")
    msg = _make_plain_message(
        "F.A.Z. Frühdenker", _NEWSLETTER_BODY, sender="newsletter@nl.faz.net",
    )
    fake = FakeImap({b"1": msg})

    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: (_ for _ in ()).throw(
        AssertionError("should not be reached")))

    def flaky_ingest_text(title, text, source_url=None, author=None,
                           china_critical=False, fallback_title=None, kind="article"):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(knowledge.ingest, "ingest_text", flaky_ingest_text)

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert summary["failed"] == 1
    assert summary["processed"] == 0
    assert fake.seen_flagged == []  # left unread — retried next poll


def test_non_newsletter_sender_still_uses_url_branch(monkeypatch):
    """Regression guard: an ordinary shared-link mail from a whitelisted,
    non-newsletter sender must be completely unaffected by #925."""
    _configure_env(monkeypatch, allowed="daniel@example.com")
    msg = _make_plain_message(
        "Schau dir das an", "https://example.com/article", sender="daniel@example.com",
    )
    fake = FakeImap({b"1": msg})

    url_calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: url_calls.append(url) or {"episode_id": 1})

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert url_calls == ["https://example.com/article"]
    assert summary["processed"] == 1


# ---------------------------------------------------------------------------
# ingest_text() default kind (regression: existing callers unaffected)
# ---------------------------------------------------------------------------

def test_ingest_text_default_kind_is_article(monkeypatch):
    captured = {}

    def fake_create_pending_episode(**kwargs):
        captured.update(kwargs)
        return 1

    monkeypatch.setattr(knowledge.ingest.database, "create_pending_episode", fake_create_pending_episode)
    monkeypatch.setattr(knowledge.ingest.database, "update_episode", lambda *a, **k: None)
    monkeypatch.setattr(knowledge.ingest, "_existing_episode", lambda video_id: None)

    import ai
    monkeypatch.setattr(ai, "translate_title", lambda title: None)
    monkeypatch.setattr(ai, "extract_article_metadata", lambda text: {})

    text = "Ein ganz normaler eingefügter Artikeltext. " * 10
    result = knowledge.ingest.ingest_text("Titel", text, author="Tester")

    assert result == {"episode_id": 1}
    assert captured["kind"] == "article"


# ---------------------------------------------------------------------------
# _strip_html() block-tag newline insertion (#925 review fix)
# ---------------------------------------------------------------------------

def test_strip_html_inserts_newlines_at_block_tags():
    """Marketing/newsletter HTML is often minified onto one physical line —
    no whitespace at all between tags. Without an inserted newline at each
    block-tag boundary, _strip_html() would collapse the whole mail into one
    giant line, and knowledge.newsletter.clean_body()'s line-based filter
    would then match/drop (or fail to drop) the ENTIRE body as a single
    unit instead of operating paragraph by paragraph."""
    html = "<p>Erster Absatz.</p><p>Zweiter Absatz.</p><div>Dritter Teil.</div>"
    text = mailbox._strip_html(html)
    assert "<" not in text and ">" not in text
    lines = [l for l in text.split("\n") if l.strip()]
    assert lines == ["Erster Absatz.", "Zweiter Absatz.", "Dritter Teil."]


def test_strip_html_br_and_table_tags_insert_newlines_too():
    html = "<table><tr><td>A</td><td>B</td></tr></table><br>Nach dem Umbruch."
    text = mailbox._strip_html(html)
    lines = [l for l in text.split("\n") if l.strip()]
    assert "A" in lines
    assert "B" in lines
    assert "Nach dem Umbruch." in lines


# ---------------------------------------------------------------------------
# End-to-end: minified HTML newsletter must not be wiped out (#925 review)
# ---------------------------------------------------------------------------

_KEY_SENTENCE = "Die Konjunktur zeigt heute gemischte Signale in ganz Europa."

# Deliberately built as ONE physical line (no literal newlines anywhere in
# this Python string) to simulate a minified HTML mail — the exact failure
# mode the reviewer flagged: without newline-insertion at block boundaries,
# the whole body becomes one line, and that one line contains "Abbestellen"
# alongside the real content.
_MINIFIED_HTML_BODY = (
    "<html><body>"
    "<p>Guten Morgen, hier ist Ihr Frühdenker.</p>"
    "<p>" + (_KEY_SENTENCE + " ") * 4 + "</p>"
    "<p>Zur Online-Ansicht klicken Sie <a href=\"https://www.faz.net/x\">hier</a>.</p>"
    "<p>Newsletter abbestellen</p><p>Impressum</p><p>Datenschutz</p>"
    "</body></html>"
)


def test_minified_html_newsletter_keeps_real_content_end_to_end(monkeypatch):
    """The most important regression test for the review round: a
    minified-HTML newsletter (real content + boilerplate all on one
    physical line before the fix) must still land in the database with its
    real content intact, not wiped out to an empty/near-empty stub."""
    _configure_env(monkeypatch, allowed="newsletter@nl.faz.net")
    msg = _make_html_message(
        "F.A.Z. Frühdenker: Minifiziert", _MINIFIED_HTML_BODY,
        sender="newsletter@nl.faz.net",
    )
    fake = FakeImap({b"1": msg})

    captured_text = {}

    def fake_ingest_text(title, text, source_url=None, author=None,
                          china_critical=False, fallback_title=None, kind="article"):
        captured_text["text"] = text
        captured_text["kind"] = kind
        return {"episode_id": 55}

    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: (_ for _ in ()).throw(
        AssertionError("must not fetch faz.net links from a newsletter body")))
    monkeypatch.setattr(knowledge.ingest, "ingest_text", fake_ingest_text)
    monkeypatch.setattr(podcast, "retry_episode", lambda episode_id: {"status": "summarized"})

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert captured_text["kind"] == "newsletter"
    # The real content must have survived cleaning, not been wiped out
    # alongside the "Abbestellen" line it used to share a physical line with.
    assert _KEY_SENTENCE in captured_text["text"]
    assert summary["processed"] == 1
    assert fake.seen_flagged == [b"1"]
