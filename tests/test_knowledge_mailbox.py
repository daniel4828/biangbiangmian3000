"""Tests for knowledge/mailbox.py (issue #655).

IMAP is entirely faked (a stub object matching the imaplib.IMAP4_SSL
interface, injected via `imap_factory`) — CLAUDE.md is explicit that test
suites must never reach real network services, and there is no real
mailbox to poll in CI anyway. knowledge.ingest.ingest_url is monkeypatched
too, since exercising the real pipeline (YouTube/article fetch + AI) is
covered by tests/test_knowledge_youtube.py and tests/test_knowledge_article.py.
"""
import email
from email.message import EmailMessage

import pytest

import knowledge.ingest
import knowledge.mailbox as mailbox


# ---------------------------------------------------------------------------
# URL extraction (pure functions, no IMAP involved)
# ---------------------------------------------------------------------------

def test_extract_urls_from_plain_text():
    text = "Schau dir das an: https://example.com/article-123 danke!"
    assert mailbox.extract_urls(text) == ["https://example.com/article-123"]


def test_extract_urls_strips_trailing_punctuation():
    text = "Video hier (https://youtu.be/abc123)."
    assert mailbox.extract_urls(text) == ["https://youtu.be/abc123"]


def test_extract_urls_multiple_and_dedup():
    text = "https://a.com/1 und nochmal https://a.com/1 und https://b.com/2"
    assert mailbox.extract_urls(text) == ["https://a.com/1", "https://b.com/2"]


def test_extract_urls_empty():
    assert mailbox.extract_urls("") == []
    assert mailbox.extract_urls(None) == []


def _make_plain_message(subject: str, body: str, sender: str = "daniel@example.com") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def _make_html_message(subject: str, html_body: str, sender: str = "daniel@example.com") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content("(html only)")
    msg.add_alternative(html_body, subtype="html")
    return msg


def test_extract_urls_from_message_subject_only():
    msg = _make_plain_message("Schau: https://example.com/from-subject", "kein Link im Text")
    assert mailbox.extract_urls_from_message(msg) == ["https://example.com/from-subject"]


def test_extract_urls_from_message_body_only():
    msg = _make_plain_message("Ohne Link", "hier: https://example.com/from-body")
    assert mailbox.extract_urls_from_message(msg) == ["https://example.com/from-body"]


def test_extract_urls_from_message_subject_and_body_multiple():
    msg = _make_plain_message(
        "Zwei Links: https://a.com/1",
        "und noch einer https://b.com/2 und https://c.com/3",
    )
    urls = mailbox.extract_urls_from_message(msg)
    assert urls == ["https://a.com/1", "https://b.com/2", "https://c.com/3"]


def test_extract_urls_from_html_body():
    msg = _make_html_message(
        "Artikel",
        '<html><body><p>Schau mal <a href="https://example.com/html-link">hier</a></p></body></html>',
    )
    assert mailbox.extract_urls_from_message(msg) == ["https://example.com/html-link"]


def test_extract_urls_from_message_none():
    msg = _make_plain_message("kein link", "auch kein link hier")
    assert mailbox.extract_urls_from_message(msg) == []


def test_sender_address_plain():
    msg = _make_plain_message("x", "y", sender="daniel@example.com")
    assert mailbox._sender_address(msg) == "daniel@example.com"


def test_sender_address_with_display_name():
    msg = _make_plain_message("x", "y", sender="Daniel Schreiber <Daniel@Example.COM>")
    assert mailbox._sender_address(msg) == "daniel@example.com"


# ---------------------------------------------------------------------------
# check_mailbox — fake IMAP connection
# ---------------------------------------------------------------------------

class FakeImap:
    """Minimal stand-in for imaplib.IMAP4_SSL covering only what
    check_mailbox() calls."""

    def __init__(self, messages):
        # messages: dict[bytes msg_id] -> EmailMessage (or None to simulate
        # a fetch failure)
        self._messages = messages
        self.seen_flagged = []
        self.logged_in = False

    def login(self, user, password):
        self.logged_in = True
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


@pytest.fixture(autouse=True)
def _knowledge_mail_env(monkeypatch):
    """Ensure a clean env per test — tests set what they need explicitly."""
    for var in (
        "KNOWLEDGE_MAIL_ALLOWED_SENDERS", "KNOWLEDGE_IMAP_HOST",
        "KNOWLEDGE_IMAP_PORT", "KNOWLEDGE_IMAP_USER", "KNOWLEDGE_IMAP_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)


def _configure_env(monkeypatch, allowed="daniel@example.com"):
    monkeypatch.setenv("KNOWLEDGE_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("KNOWLEDGE_IMAP_USER", "kb@example.com")
    monkeypatch.setenv("KNOWLEDGE_IMAP_PASSWORD", "secret")
    if allowed is not None:
        monkeypatch.setenv("KNOWLEDGE_MAIL_ALLOWED_SENDERS", allowed)


def test_no_allowed_senders_processes_nothing(monkeypatch):
    """Security-critical (#655): with the allowlist unset, nothing gets
    read or marked seen — not even an IMAP connection is opened."""
    monkeypatch.setenv("KNOWLEDGE_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("KNOWLEDGE_IMAP_USER", "kb@example.com")
    monkeypatch.setenv("KNOWLEDGE_IMAP_PASSWORD", "secret")
    # KNOWLEDGE_MAIL_ALLOWED_SENDERS deliberately left unset

    called = {"factory": False}

    def factory():
        called["factory"] = True
        raise AssertionError("should never connect when allowlist is empty")

    summary = mailbox.check_mailbox(imap_factory=factory)

    assert called["factory"] is False
    assert summary["reason"] == "no_allowed_senders"
    assert summary["checked"] == 0
    assert summary["processed"] == 0


def test_missing_credentials_skips(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_MAIL_ALLOWED_SENDERS", "daniel@example.com")
    # host/user/password left unset

    summary = mailbox.check_mailbox(imap_factory=lambda: (_ for _ in ()).throw(
        AssertionError("should not connect without credentials")
    ))

    assert summary["reason"] == "no_credentials"


def test_url_in_subject_is_ingested(monkeypatch):
    _configure_env(monkeypatch)
    msg = _make_plain_message("Schau: https://example.com/subject-link", "kein Text-Link")
    fake = FakeImap({b"1": msg})

    calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: calls.append(url) or {"episode_id": 1})

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert calls == ["https://example.com/subject-link"]
    assert summary["processed"] == 1
    assert summary["ingested"] == 1
    assert fake.seen_flagged == [b"1"]


def test_url_in_plain_body_is_ingested(monkeypatch):
    _configure_env(monkeypatch)
    msg = _make_plain_message("Ohne Link im Betreff", "Link: https://example.com/body-link")
    fake = FakeImap({b"1": msg})

    calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: calls.append(url) or {"episode_id": 1})

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert calls == ["https://example.com/body-link"]
    assert summary["processed"] == 1


def test_url_in_html_body_is_ingested(monkeypatch):
    _configure_env(monkeypatch)
    msg = _make_html_message(
        "Artikel geteilt",
        '<a href="https://example.com/html-share">Link</a>',
    )
    fake = FakeImap({b"1": msg})

    calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: calls.append(url) or {"episode_id": 1})

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert calls == ["https://example.com/html-share"]
    assert summary["processed"] == 1


def test_multiple_urls_in_one_mail_all_processed(monkeypatch):
    _configure_env(monkeypatch)
    msg = _make_plain_message(
        "Mehrere Links",
        "https://example.com/one und https://example.com/two",
    )
    fake = FakeImap({b"1": msg})

    calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: calls.append(url) or {"episode_id": len(calls)})

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert calls == ["https://example.com/one", "https://example.com/two"]
    assert summary["ingested"] == 2
    assert summary["processed"] == 1
    assert fake.seen_flagged == [b"1"]


def test_non_whitelisted_sender_is_skipped(monkeypatch):
    _configure_env(monkeypatch, allowed="daniel@example.com")
    msg = _make_plain_message("Spam mit Link", "https://spam.example.com/pitch", sender="stranger@evil.example")
    fake = FakeImap({b"1": msg})

    called = {"n": 0}
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: called.__setitem__("n", called["n"] + 1))

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert called["n"] == 0
    assert summary["skipped"] == 1
    assert summary["processed"] == 0
    assert fake.seen_flagged == []


def test_whitelist_matches_display_name_format_case_insensitive(monkeypatch):
    _configure_env(monkeypatch, allowed="Daniel@Example.com")
    msg = _make_plain_message(
        "Von Handy geteilt",
        "https://example.com/shared",
        sender="Daniel Schreiber <daniel@example.com>",
    )
    fake = FakeImap({b"1": msg})

    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: {"episode_id": 1})

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert summary["processed"] == 1
    assert fake.seen_flagged == [b"1"]


def test_ingest_failure_does_not_mark_seen(monkeypatch):
    """#655 completion criterion: failed processing must not mark the mail
    read, so the next run retries it."""
    _configure_env(monkeypatch)
    msg = _make_plain_message("Kaputter Link", "https://example.com/broken")
    fake = FakeImap({b"1": msg})

    def failing_ingest(url):
        raise RuntimeError("boom")

    monkeypatch.setattr(knowledge.ingest, "ingest_url", failing_ingest)

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert summary["failed"] == 1
    assert summary["processed"] == 0
    assert fake.seen_flagged == []
    assert len(summary["errors"]) == 1


def test_partial_failure_in_multi_url_mail_does_not_mark_seen(monkeypatch):
    _configure_env(monkeypatch)
    msg = _make_plain_message(
        "Ein guter, ein kaputter Link",
        "https://example.com/good https://example.com/bad",
    )
    fake = FakeImap({b"1": msg})

    def ingest(url):
        if url.endswith("/bad"):
            raise RuntimeError("boom")
        return {"episode_id": 1}

    monkeypatch.setattr(knowledge.ingest, "ingest_url", ingest)

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert summary["ingested"] == 1
    assert summary["failed"] == 1
    assert summary["processed"] == 0
    assert fake.seen_flagged == []


def test_mail_without_url_is_skipped_not_marked_seen(monkeypatch):
    """Body under the 200-char threshold: still skipped, not marked seen
    (unchanged from #655; #668 only adds a path for LONGER bodies)."""
    _configure_env(monkeypatch)
    msg = _make_plain_message("Kein Link", "nur Text, kein Link hier")
    fake = FakeImap({b"1": msg})

    called = {"n": 0}
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: called.__setitem__("n", called["n"] + 1))

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert called["n"] == 0
    assert summary["skipped"] == 1
    assert fake.seen_flagged == []


# ---------------------------------------------------------------------------
# HTML tag stripping (#668) — plain_text_body() / _strip_html()
# ---------------------------------------------------------------------------

def test_strip_html_removes_tags_keeps_text():
    html = "<html><body><p>Hallo <b>Welt</b></p><div>zweiter Absatz</div></body></html>"
    text = mailbox._strip_html(html)
    assert "<" not in text
    assert ">" not in text
    assert "Hallo" in text and "Welt" in text and "zweiter Absatz" in text


def test_strip_html_drops_script_and_style_contents():
    html = "<style>.x{color:red}</style><p>echter Text</p><script>alert('x')</script>"
    text = mailbox._strip_html(html)
    assert "color:red" not in text
    assert "alert" not in text
    assert "echter Text" in text


def test_plain_text_body_strips_html_part():
    msg = _make_html_message(
        "Artikel",
        '<html><body><p>Erster Absatz</p><p>Zweiter <a href="https://example.com/x">Absatz</a></p></body></html>',
    )
    body = mailbox.plain_text_body(msg)
    assert "<" not in body and ">" not in body
    assert "Erster Absatz" in body
    assert "Zweiter" in body and "Absatz" in body


def test_plain_text_body_uses_plain_part_as_is():
    msg = _make_plain_message("Artikel", "Erster Absatz.\nZweiter Absatz.")
    body = mailbox.plain_text_body(msg)
    assert body == "Erster Absatz.\nZweiter Absatz."


# ---------------------------------------------------------------------------
# No-URL, body-length-based fallback to ingest_text() (#668)
# ---------------------------------------------------------------------------

_LONG_BODY = "这是一封没有链接、只有正文的分享邮件，用来测试知识库正文投递功能。" * 7
assert len(_LONG_BODY) >= 200


def test_mail_without_url_but_long_body_ingested_as_text(monkeypatch):
    _configure_env(monkeypatch)
    msg = _make_plain_message("付费墙文章标题", _LONG_BODY)
    fake = FakeImap({b"1": msg})

    calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: (_ for _ in ()).throw(
        AssertionError("ingest_url must not be called when there is no URL")
    ))
    monkeypatch.setattr(
        knowledge.ingest, "ingest_text",
        lambda title, text, source_url=None, **kwargs: calls.append((title, text)) or {"episode_id": 1},
    )

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert calls == [("付费墙文章标题", _LONG_BODY)]
    assert summary["ingested"] == 1
    assert summary["processed"] == 1
    assert fake.seen_flagged == [b"1"]


def test_url_present_takes_priority_over_text_fallback(monkeypatch):
    """#668 completion criterion: a mail with BOTH a URL and a long body
    must still go through ingest_url() only — the URL path is untouched."""
    _configure_env(monkeypatch)
    msg = _make_plain_message(
        "Geteilter Artikel",
        f"https://example.com/article\n\n{_LONG_BODY}",
    )
    fake = FakeImap({b"1": msg})

    url_calls = []
    text_calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: url_calls.append(url) or {"episode_id": 1})
    monkeypatch.setattr(
        knowledge.ingest, "ingest_text",
        lambda title, text, source_url=None, **kwargs: text_calls.append(title) or {"episode_id": 2},
    )

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert url_calls == ["https://example.com/article"]
    assert text_calls == []
    assert summary["processed"] == 1
    assert fake.seen_flagged == [b"1"]


def test_text_fallback_ingest_failure_does_not_mark_seen(monkeypatch):
    _configure_env(monkeypatch)
    msg = _make_plain_message("Titel", _LONG_BODY)
    fake = FakeImap({b"1": msg})

    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: (_ for _ in ()).throw(
        AssertionError("should not be reached")
    ))

    def failing_ingest_text(title, text, source_url=None, **kwargs):
        raise knowledge.ingest.IngestError("boom")

    monkeypatch.setattr(knowledge.ingest, "ingest_text", failing_ingest_text)

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert summary["failed"] == 1
    assert summary["processed"] == 0
    assert fake.seen_flagged == []
    assert len(summary["errors"]) == 1


def test_html_only_body_used_for_text_fallback_after_stripping(monkeypatch):
    """A share-to-mail app that sends HTML-only, no URL, long enough body
    after tags are stripped -> still goes through the text fallback, and
    the ingested text has no markup in it."""
    _configure_env(monkeypatch)
    paragraphs = "".join(f"<p>{_LONG_BODY[i:i + 40]}</p>" for i in range(0, len(_LONG_BODY), 40))
    msg = _make_html_message("HTML Artikel", f"<html><body>{paragraphs}</body></html>")
    fake = FakeImap({b"1": msg})

    calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: (_ for _ in ()).throw(
        AssertionError("should not be reached")
    ))
    monkeypatch.setattr(
        knowledge.ingest, "ingest_text",
        lambda title, text, source_url=None, **kwargs: calls.append(text) or {"episode_id": 1},
    )

    summary = mailbox.check_mailbox(imap_factory=lambda: fake)

    assert summary["processed"] == 1
    assert len(calls) == 1
    assert "<" not in calls[0] and ">" not in calls[0]
