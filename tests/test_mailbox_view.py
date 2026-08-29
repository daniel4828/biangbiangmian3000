"""📬 Mailbox view (#960): listing the inbox, the per-sender automatic
switch, and processing one mail on demand.

The promise these tests exist to protect: browsing Daniel's personal Gmail
from inside the app must change nothing in Gmail and must not download
mail he didn't ask for. Both are easy to break by accident later (one
`RFC822` instead of `BODY.PEEK[]`, one `store()` call) and impossible to
notice from the UI, so they are asserted directly.
"""
import email.message

import pytest

import database
import database.core
import knowledge.ingest
import knowledge.mailbox as mailbox


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Patch database.core.DB_PATH, never database.DB_PATH — the latter is
    only a wildcard-import copy and patching it writes to the real
    data/srs.db without failing (#615)."""
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


@pytest.fixture(autouse=True)
def _imap_env(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("KNOWLEDGE_IMAP_USER", "kb@example.com")
    monkeypatch.setenv("KNOWLEDGE_IMAP_PASSWORD", "secret")


def _mail(sender="Absender <news@example.com>", subject="Betreff",
          body="x" * 500, message_id="<abc@example.com>"):
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = "Fri, 29 Aug 2026 07:00:00 +0200"
    msg["Message-ID"] = message_id
    msg.set_content(body)
    return msg


class FakeImap:
    """IMAP stand-in that records every UID command, so the tests can
    assert on what was *not* done as much as on what was."""

    def __init__(self, messages):
        # messages: list of (uid: bytes, EmailMessage), oldest first
        self._messages = list(messages)
        self.commands = []
        self.selected_readonly = None
        self.store_calls = []

    def login(self, user, password):
        return "OK", [b"ok"]

    def select(self, mailbox_name, readonly=False):
        self.selected_readonly = readonly
        return "OK", [b"1"]

    def response(self, key):
        return "OK", [b"12345"]

    def uid(self, command, *args):
        self.commands.append((command, args))
        if command == "SEARCH":
            return "OK", [b" ".join(uid for uid, _ in self._messages)]
        if command == "FETCH":
            wanted = args[0]
            wanted = wanted if isinstance(wanted, bytes) else str(wanted).encode()
            for uid, msg in self._messages:
                if uid == wanted:
                    return "OK", [(b"1 (x {1})", msg.as_bytes())]
            return "NO", [None]
        raise AssertionError(f"unexpected UID command: {command}")

    def store(self, *args):
        self.store_calls.append(args)
        return "OK", [b"done"]

    def close(self):
        return "OK", [b"closed"]

    def logout(self):
        return "OK", [b"out"]


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------

def test_list_returns_newest_first_with_envelope_fields():
    fake = FakeImap([(b"1", _mail(subject="alt")), (b"2", _mail(subject="neu"))])

    result = mailbox.list_inbox(imap_factory=lambda: fake)

    assert [m["subject"] for m in result["messages"]] == ["neu", "alt"]
    assert result["total"] == 2
    assert result["messages"][0]["from"] == "news@example.com"


def test_listing_never_downloads_bodies_or_touches_flags():
    """The whole point of the envelope-only fetch: a 1600-mail personal
    inbox is listed on every page view, and none of it may be downloaded
    or marked read."""
    fake = FakeImap([(b"1", _mail())])

    mailbox.list_inbox(imap_factory=lambda: fake)

    fetches = [args[1] for cmd, args in fake.commands if cmd == "FETCH"]
    assert fetches and all("HEADER.FIELDS" in f for f in fetches)
    assert all("BODY.PEEK" in f for f in fetches)
    assert fake.store_calls == []
    assert fake.selected_readonly is True


def test_pagination_slices_the_reversed_list():
    fake = FakeImap([(str(i).encode(), _mail(subject=f"s{i}")) for i in range(1, 6)])

    result = mailbox.list_inbox(offset=1, limit=2, imap_factory=lambda: fake)

    assert [m["subject"] for m in result["messages"]] == ["s4", "s3"]
    assert result["total"] == 5


def test_search_is_delegated_to_the_imap_server():
    """Filtering client-side would mean downloading every header in the
    mailbox to throw almost all of them away."""
    fake = FakeImap([(b"1", _mail())])

    mailbox.list_inbox(query="faz", imap_factory=lambda: fake)

    search = next(args for cmd, args in fake.commands if cmd == "SEARCH")
    assert search == (None, "OR", "FROM", "faz", "SUBJECT", "faz")


def test_processed_mails_are_marked_from_the_message_id(monkeypatch):
    fake = FakeImap([(b"1", _mail(message_id="<known@example.com>"))])
    monkeypatch.setattr(database, "processed_mail_message_ids",
                        lambda ids: {"<known@example.com>": 42})
    monkeypatch.setattr(database, "auto_mail_senders", lambda: set())

    result = mailbox.list_inbox(imap_factory=lambda: fake)

    assert result["messages"][0]["processed"] is True
    assert result["messages"][0]["episode_id"] == 42


# ---------------------------------------------------------------------------
# fetching one mail
# ---------------------------------------------------------------------------

def test_fetch_message_uses_peek_and_changes_nothing():
    fake = FakeImap([(b"7", _mail(subject="Ziel"))])

    msg = mailbox.fetch_message("7", imap_factory=lambda: fake)

    assert msg["Subject"] == "Ziel"
    assert fake.store_calls == []
    assert all("BODY.PEEK" in args[1] for cmd, args in fake.commands if cmd == "FETCH")


def test_stale_uidvalidity_is_an_error_not_a_guess():
    """UIDs are only meaningful within one UIDVALIDITY generation. Acting
    on a stale one would process a different mail than the one Daniel
    clicked."""
    fake = FakeImap([(b"7", _mail())])

    with pytest.raises(mailbox.MailboxError):
        mailbox.fetch_message("7", uidvalidity="99999", imap_factory=lambda: fake)


def test_missing_uid_is_an_error():
    fake = FakeImap([(b"7", _mail())])

    with pytest.raises(mailbox.MailboxError):
        mailbox.fetch_message("404", imap_factory=lambda: fake)


def test_no_credentials_is_an_error_not_an_empty_list(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_IMAP_HOST", raising=False)

    with pytest.raises(mailbox.MailboxError):
        mailbox.list_inbox(imap_factory=lambda: FakeImap([]))


# ---------------------------------------------------------------------------
# ingesting one mail
# ---------------------------------------------------------------------------

def test_ingest_message_records_the_message_id(monkeypatch):
    seen = {}
    monkeypatch.setattr(knowledge.ingest, "ingest_text",
                        lambda *a, **k: {"episode_id": 7})
    monkeypatch.setattr(database, "set_mail_message_id",
                        lambda eid, mid: seen.update({eid: mid}))

    result = mailbox.ingest_message(_mail(body="y" * 500, message_id="<m1@x>"))

    assert result["route"] == "text"
    assert result["episode_id"] == 7
    assert seen == {7: "<m1@x>"}


def test_ingest_message_uses_the_newsletter_route_for_known_senders(monkeypatch):
    calls = []
    monkeypatch.setattr("knowledge.newsletter.ingest_newsletter",
                        lambda addr, subject, body: calls.append(addr) or {"episode_id": 3})
    monkeypatch.setattr(knowledge.ingest, "ingest_url",
                        lambda url: pytest.fail("newsletter must not take the URL route"))
    monkeypatch.setattr(database, "set_mail_message_id", lambda eid, mid: None)

    msg = _mail(sender="newsletter@nl.faz.net",
                body="Zum Weiterlesen https://www.faz.net/artikel-1 " + "z" * 400)
    result = mailbox.ingest_message(msg)

    assert result["route"] == "newsletter"
    assert calls == ["newsletter@nl.faz.net"]


def test_unusable_mail_raises_instead_of_creating_an_empty_entry(monkeypatch):
    monkeypatch.setattr(knowledge.ingest, "ingest_text",
                        lambda *a, **k: pytest.fail("must not ingest an empty mail"))

    with pytest.raises(knowledge.ingest.IngestError):
        mailbox.ingest_message(_mail(body="zu kurz"))


# ---------------------------------------------------------------------------
# the per-sender switch
# ---------------------------------------------------------------------------

def test_sender_switch_round_trips_and_is_lowercased():
    database.set_mail_sender_auto("News@Example.COM", True, name="Beispiel")

    assert "news@example.com" in database.auto_mail_senders()

    database.set_mail_sender_auto("news@example.com", False)

    assert "news@example.com" not in database.auto_mail_senders()
    # the row survives, so the UI still lists the sender with its switch off
    assert any(s["address"] == "news@example.com" for s in database.get_mail_senders())


def test_turning_a_switch_off_keeps_the_display_name():
    """The name is written but never used to match — a sender that changes
    its display name is still the same sender."""
    database.set_mail_sender_auto("zeitung@example.com", True, name="Zeitung")
    database.set_mail_sender_auto("zeitung@example.com", False, name=None)

    row = next(s for s in database.get_mail_senders() if s["address"] == "zeitung@example.com")
    assert row["name"] == "Zeitung"


# ---------------------------------------------------------------------------
# frontend wiring (#960) — the front end has no build step and no test
# runner, so the few things that silently break it are pinned here
# ---------------------------------------------------------------------------

import pathlib

_STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"


def test_mailbox_view_is_registered_in_show_view():
    """A view whose id isn't in showView()'s list is never hidden again: it
    stays visible underneath every other screen."""
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8")
    index = (_STATIC / "index.html").read_text(encoding="utf-8")

    assert "'knowledge', 'books', 'mailbox'" in app_js
    assert 'id="view-mailbox"' in index
    assert 'id="view-mailbox-content"' in index
    assert "onclick=\"openMailbox()\"" in app_js


def test_mailbox_frontend_talks_only_to_the_mailbox_api():
    """It must not grow a second path into the ingest pipeline (#643's
    single-entry-point rule): everything goes through /api/mailbox."""
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8")
    start = app_js.index("const _MAILBOX_PAGE")
    section = app_js[start:]

    assert "/api/knowledge/add" not in section
    assert "/api/podcast/episodes" not in section
