"""📬 Mailbox view (#960): listing the inbox, the per-sender automatic
switch, and processing one mail on demand.

The promise these tests exist to protect: browsing Daniel's personal Gmail
from inside the app must change nothing in Gmail and must not download
mail he didn't ask for. Both are easy to break by accident later (one
`RFC822` instead of `BODY.PEEK[]`, one `store()` call) and impossible to
notice from the UI, so they are asserted directly.
"""
import email.message
import re

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
        if command == "MOVE":
            return "OK", [b"moved"]
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

    def moved(self):
        return [args for cmd, args in self.commands if cmd == "MOVE"]

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
    # The range's SINCE comes first, then the OR — both are the server's job
    assert search[1] == "SINCE"
    assert search[3:] == ("OR", "FROM", "faz", "SUBJECT", "faz")


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
# date range, delete, block (#968)
# ---------------------------------------------------------------------------

def test_range_is_translated_into_an_imap_since():
    """Filtering has to happen on the server — fetching everything and
    slicing here would make the setting pointless, since the cost is in
    what crosses the wire."""
    fake = FakeImap([(b"1", _mail())])

    mailbox.list_inbox(range_name="week", imap_factory=lambda: fake)

    search = next(args for cmd, args in fake.commands if cmd == "SEARCH")
    assert search[1] == "SINCE"
    # dd-Mon-yyyy with an English month — strftime("%b") would localise it
    assert re.match(r"^\d{2}-[A-Z][a-z]{2}-\d{4}$", search[2])


def test_range_all_has_no_since_term():
    fake = FakeImap([(b"1", _mail())])

    mailbox.list_inbox(range_name="all", imap_factory=lambda: fake)

    search = next(args for cmd, args in fake.commands if cmd == "SEARCH")
    assert search == (None, "ALL")


def test_week_range_starts_on_monday():
    """"This week" is the calendar week, not a sliding 7 days — that is
    what was asked for, and it keeps the answer stable through the day."""
    since = mailbox._since_date("week")
    assert since.weekday() == 0


def test_delete_moves_to_trash_and_never_expunges():
    """A wrong click has to stay undoable: Gmail keeps trashed mail for 30
    days, an expunge keeps nothing."""
    fake = FakeImap([(b"9", _mail())])

    mailbox.delete_message("9", imap_factory=lambda: fake)

    assert fake.moved() == [("9", "[Gmail]/Trash")]
    assert not any(cmd == "EXPUNGE" for cmd, _ in fake.commands)
    assert fake.store_calls == []


def test_delete_is_the_only_writable_connection():
    """Everything else keeps #960's promise that browsing the mailbox from
    the app changes nothing in Gmail."""
    fake = FakeImap([(b"9", _mail())])
    mailbox.list_inbox(imap_factory=lambda: fake)
    assert fake.selected_readonly is True

    fake2 = FakeImap([(b"9", _mail())])
    mailbox.fetch_message("9", imap_factory=lambda: fake2)
    assert fake2.selected_readonly is True

    fake3 = FakeImap([(b"9", _mail())])
    mailbox.delete_message("9", imap_factory=lambda: fake3)
    assert fake3.selected_readonly is False


def test_blocking_clears_the_subscription():
    """"Subscribed AND blocked" is a contradiction — it must not become
    representable, or something later has to decide which one wins."""
    database.set_mail_sender_auto("news@example.com", True)
    database.set_mail_sender_blocked("news@example.com", True)

    assert "news@example.com" not in database.auto_mail_senders()
    assert "news@example.com" in database.blocked_mail_senders()


def test_subscribing_a_blocked_sender_unblocks_them():
    database.set_mail_sender_blocked("news@example.com", True)
    database.set_mail_sender_auto("news@example.com", True)

    assert database.blocked_mail_senders() == set()
    assert "news@example.com" in database.auto_mail_senders()


def test_blocked_senders_are_hidden_from_the_mail_list():
    database.set_mail_sender_blocked("spam@example.com", True)
    fake = FakeImap([
        (b"1", _mail(sender="Spam <spam@example.com>")),
        (b"2", _mail(sender="Gut <ok@example.com>", subject="behalten")),
    ])

    result = mailbox.list_inbox(imap_factory=lambda: fake)

    assert [m["subject"] for m in result["messages"]] == ["behalten"]
    # reported, not silently swallowed — otherwise the page just looks short
    assert result["hidden"] == 1


def test_blocked_senders_stay_in_the_sender_view():
    """Otherwise blocking a sender would make them impossible to unblock."""
    database.set_mail_sender_blocked("spam@example.com", True)
    fake = FakeBulkImap([(b"1", _mail(sender="Spam <spam@example.com>"))])

    result = mailbox.list_senders(imap_factory=lambda: fake)

    row = next(s for s in result["senders"] if s["address"] == "spam@example.com")
    assert row["blocked"] is True
    # blocked senders sort last
    assert result["senders"][-1]["address"] == "spam@example.com"


def test_deleting_a_sender_row_deletes_no_mail():
    """"Delete sender" is about the stored setting. The mail list is a live
    IMAP view — there is nothing about it in the database to delete."""
    database.set_mail_sender_auto("news@example.com", True)

    assert database.delete_mail_sender("news@example.com") is True
    assert database.delete_mail_sender("news@example.com") is False
    assert database.auto_mail_senders() == {"newsletter@nl.faz.net"}


def test_sender_cache_is_per_range():
    """Sharing one cache slot would serve one range's counts under the
    other range's label."""
    fake = FakeBulkImap([(b"1", _mail())])

    first = mailbox.list_senders(range_name="week", imap_factory=lambda: fake)
    other = mailbox.list_senders(range_name="all", imap_factory=lambda: fake)

    assert first["cached"] is False
    assert other["cached"] is False


# ---------------------------------------------------------------------------
# sender view (#965)
# ---------------------------------------------------------------------------

class FakeBulkImap(FakeImap):
    """Returns every header in one response, the way a real server answers
    `UID FETCH 1:* (...)`."""

    def uid(self, command, *args):
        self.commands.append((command, args))
        # Any UID set — "1:*" for range=all, an explicit "1,2,3" list once a
        # SINCE search narrowed it down (#968).
        if command == "FETCH":
            out = []
            for _uid, msg in self._messages:
                out.append((b"1 (x {1})", msg.as_bytes()))
                out.append(b")")
            return "OK", out
        return super().uid(command, *args)


@pytest.fixture(autouse=True)
def _clear_sender_cache():
    mailbox._SENDER_CACHE.clear()


def test_sender_scan_is_one_fetch_and_never_writes_flags():
    """1600 mails: per-message fetches here would be 1600 round trips, and
    any store() would break the "browsing changes nothing" promise."""
    fake = FakeBulkImap([
        (b"1", _mail(sender="A <a@example.com>")),
        (b"2", _mail(sender="A <a@example.com>")),
        (b"3", _mail(sender="B <b@example.com>")),
    ])

    result = mailbox.list_senders(imap_factory=lambda: fake)

    fetches = [args for cmd, args in fake.commands if cmd == "FETCH"]
    assert len(fetches) == 1
    assert "BODY.PEEK" in fetches[0][1]
    assert fake.store_calls == []
    assert fake.selected_readonly is True

    # Only the scanned senders are asserted on: init_db seeds the F.A.Z.
    # sender (#960), which correctly shows up with count 0 — that is the
    # subject of test_subscribed_senders_appear_even_with_no_mail_left.
    counts = {s["address"]: s["count"] for s in result["senders"] if s["count"]}
    assert counts == {"a@example.com": 2, "b@example.com": 1}


def test_subscribed_senders_appear_even_with_no_mail_left():
    """Otherwise a sender whose mail was all archived could never be
    unsubscribed again."""
    database.set_mail_sender_auto("gone@example.com", True, name="Weg")
    fake = FakeBulkImap([(b"1", _mail(sender="A <a@example.com>"))])

    result = mailbox.list_senders(imap_factory=lambda: fake)

    row = next(s for s in result["senders"] if s["address"] == "gone@example.com")
    assert row["count"] == 0
    assert row["auto_process"] is True
    # subscribed senders sort first
    assert result["senders"][0]["address"] == "gone@example.com"


def test_second_call_is_served_from_cache_and_refresh_bypasses_it():
    fake = FakeBulkImap([(b"1", _mail())])

    first = mailbox.list_senders(imap_factory=lambda: fake)
    second = mailbox.list_senders(imap_factory=lambda: fake)
    assert first["cached"] is False
    assert second["cached"] is True
    assert len([c for c, _ in fake.commands if c == "FETCH"]) == 1

    third = mailbox.list_senders(refresh=True, imap_factory=lambda: fake)
    assert third["cached"] is False
    assert len([c for c, _ in fake.commands if c == "FETCH"]) == 2


def test_subscription_state_survives_a_cached_scan():
    """The scan is cached; the switches are not — subscribing must show up
    immediately instead of waiting out the cache TTL."""
    fake = FakeBulkImap([(b"1", _mail(sender="News <news@example.com>"))])
    mailbox.list_senders(imap_factory=lambda: fake)

    database.set_mail_sender_auto("news@example.com", True)
    result = mailbox.list_senders(imap_factory=lambda: fake)

    assert result["cached"] is True
    assert result["senders"][0]["auto_process"] is True


# ---------------------------------------------------------------------------
# frontend wiring (#960) — the front end has no build step and no test
# runner, so the few things that silently break it are pinned here
# ---------------------------------------------------------------------------

import pathlib

_STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"


def test_mailbox_is_a_knowledge_screen():
    """#988: the mailbox stopped being a top-level view and became one of the
    Knowledge screens, reachable from the Knowledge header. Its own container
    is gone, so anything still rendering into it would write into nothing."""
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8")
    index = (_STATIC / "index.html").read_text(encoding="utf-8")

    assert 'id="view-mailbox"' not in index
    assert "view-mailbox-content" not in app_js
    assert "showView('mailbox')" not in app_js
    assert "_knowledgeScreen = 'mailbox'" in app_js
    assert "onclick=\"openMailbox()\"" in app_js


def test_subscribing_has_exactly_one_home():
    """Daniel could not find where to (un)subscribe because newsletter senders
    and podcast feeds lived in two unrelated places (#988). Both are now tabs
    of the one Subscriptions screen."""
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8")

    assert "function openKnowledgeSubs" in app_js
    assert "openKnowledgeSubs('newsletters')" in app_js
    assert "openKnowledgeSubs('feeds')" in app_js
    # The old standalone feeds entry point still works — hash routes use it.
    assert "function openKnowledgeFeeds" in app_js


def test_mailbox_frontend_talks_only_to_the_mailbox_api():
    """It must not grow a second path into the ingest pipeline (#643's
    single-entry-point rule): everything goes through /api/mailbox."""
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8")
    start = app_js.index("const _MAILBOX_PAGE")
    section = app_js[start:]

    assert "/api/knowledge/add" not in section
    assert "/api/podcast/episodes" not in section


def test_auto_since_stamped_on_subscribe_and_stable_on_reclick():
    """auto_since 是 cron 时间窗的下界（#997）：只在「关 → 开」那一下写入。

    再点一次已经打开的开关就刷新它的话，等于把窗口往前推，中间到的邮件
    就此永远处理不到——而用户看到的只是「我点了一下，什么都没变」。
    """
    database.set_mail_sender_auto("news@example.com", True)
    first = database.auto_mail_sender_windows()["news@example.com"]
    assert first

    database.set_mail_sender_auto("news@example.com", True)
    assert database.auto_mail_sender_windows()["news@example.com"] == first

    # 关掉再打开是一次真正的重新订阅，窗口应该重置到现在
    database.set_mail_sender_auto("news@example.com", False)
    assert "news@example.com" not in database.auto_mail_sender_windows()


def test_blocked_sender_has_no_window():
    """屏蔽会清掉自动开关，所以它也不该出现在 cron 的时间窗表里——两处判断
    只改一处，就等于屏蔽了还在每天花钱。"""
    database.set_mail_sender_auto("news@example.com", True)
    database.set_mail_sender_blocked("news@example.com", True)
    assert "news@example.com" not in database.auto_mail_sender_windows()
