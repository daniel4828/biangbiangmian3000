"""Tests for knowledge/signal_inbox.py (issue #749).

signal-cli is entirely faked: `runner` (injected into check_signal_inbox)
is a plain callable that returns canned JSON-Lines stdout instead of
actually shelling out — CLAUDE.md is explicit that test suites must never
reach real network services / external processes, mirroring how
knowledge/mailbox.py fakes IMAP via `imap_factory`.

knowledge.ingest.ingest_url and podcast.retry_episode/send_signal_text are
monkeypatched too: exercising the real ingest/transcribe/summarize pipeline
is covered elsewhere (tests/test_knowledge_youtube.py,
tests/test_knowledge_article.py, tests/test_podcast*.py).
"""
import json

import pytest

import database
import knowledge.ingest
import knowledge.signal_inbox as signal_inbox
import podcast


ACCOUNT = "+491234567890"
OTHER = "+499999999999"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh temp database — retry-queue state lives in app_settings.
    Patch database.core.DB_PATH, not database.DB_PATH (issue #615: the
    latter is only a name copy, get_db() reads the former)."""
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()


@pytest.fixture(autouse=True)
def _no_real_signal(monkeypatch):
    """Belt-and-suspenders: even if a test forgets to patch send_signal_text
    itself, nothing here should ever shell out to a real signal-cli."""
    monkeypatch.setattr(podcast, "send_signal_text", lambda text, context="signal": True)


def _envelope_note_to_self(message: str, source=ACCOUNT, destination=ACCOUNT) -> dict:
    """A linked-device sync copy of a message the phone sent to Note to
    Self: source == account, sentMessage.destination == account."""
    return {
        "sourceNumber": source,
        "syncMessage": {"sentMessage": {"destinationNumber": destination, "message": message}},
    }


def _envelope_sent_to_other(message: str) -> dict:
    """A sync copy of a message Daniel's phone sent to someone ELSE — same
    source as Note to Self, different destination. Must be ignored."""
    return {
        "sourceNumber": ACCOUNT,
        "syncMessage": {"sentMessage": {"destinationNumber": "+493333333333", "message": message}},
    }


def _envelope_sent_to_group(message: str) -> dict:
    """A sync copy of a message Daniel's phone sent to a GROUP — same
    source as Note to Self, no destinationNumber at all, only groupInfo.
    This is the fail-closed case: a naive "no destination -> allow" check
    would wave this through. Must be ignored."""
    return {
        "sourceNumber": ACCOUNT,
        "syncMessage": {"sentMessage": {"groupInfo": {"groupId": "abc123"}, "message": message}},
    }


def _envelope_sent_message_no_destination(message: str) -> dict:
    """A sentMessage with no destination and no groupInfo either — an
    ambiguous/unrecognized shape. Must fail closed (ignored), not guessed
    at as Note to Self."""
    return {
        "sourceNumber": ACCOUNT,
        "syncMessage": {"sentMessage": {"message": message}},
    }


def _envelope_from_other(message: str) -> dict:
    """An ordinary incoming message from someone who is not Daniel's own
    account. Must be ignored — the security gate under test."""
    return {
        "sourceNumber": OTHER,
        "dataMessage": {"message": message},
    }


def _lines(*envelopes) -> str:
    return "\n".join(json.dumps(e) for e in envelopes)


def _make_runner(stdout: str, calls=None):
    def runner(args):
        if calls is not None:
            calls.append(args)
        return stdout
    return runner


# ---------------------------------------------------------------------------
# SIGNAL_ACCOUNT not configured
# ---------------------------------------------------------------------------

def test_no_account_configured_skips_everything(tmp_db, monkeypatch):
    monkeypatch.delenv("SIGNAL_ACCOUNT", raising=False)
    calls = []
    summary = signal_inbox.check_signal_inbox(runner=_make_runner("", calls))

    assert summary["reason"] == "no_account"
    assert calls == []


def test_receive_is_invoked_with_an_idle_timeout(tmp_db, monkeypatch):
    """#755: without `-t`, `signal-cli receive` never returns — it keeps
    listening for new messages until killed, so the cron one-shot always died
    on the subprocess timeout instead of draining the queue. The flag is the
    entire fix, so it gets a test of its own."""
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    calls = []
    signal_inbox.check_signal_inbox(runner=_make_runner("", calls))

    assert len(calls) == 1
    args = calls[0]
    assert "receive" in args
    assert "-t" in args, "receive must carry an idle timeout or it never returns"
    # The value must follow -t, and be a positive number of seconds.
    assert int(args[args.index("-t") + 1]) > 0


# ---------------------------------------------------------------------------
# Security: only Note-to-Self from the account itself is ingested
# ---------------------------------------------------------------------------

def test_message_from_someone_else_is_skipped(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_from_other("https://example.com/from-stranger"))

    ingest_calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: ingest_calls.append(url) or {"episode_id": 1})

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert summary["skipped"] == 1
    assert summary["checked"] == 1
    assert ingest_calls == []


def test_message_sent_to_someone_else_is_skipped(tmp_db, monkeypatch):
    """Same source (Daniel's own account) as Note to Self, but addressed to
    a different destination — must not be treated as inbox input."""
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_sent_to_other("https://example.com/to-a-friend"))

    ingest_calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: ingest_calls.append(url) or {"episode_id": 1})

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert summary["skipped"] == 1
    assert ingest_calls == []


def test_message_sent_to_a_group_is_skipped(tmp_db, monkeypatch):
    """Same source as Note to Self, but sent to a GROUP (no destination
    field, only groupInfo) — the fail-closed bug this test guards against:
    a naive "no destination -> allow" check would ingest every link Daniel
    ever shares with a group chat, without him knowing."""
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_sent_to_group("https://example.com/group-link"))

    ingest_calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: ingest_calls.append(url) or {"episode_id": 1})

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert summary["skipped"] == 1
    assert ingest_calls == []


def test_sent_message_with_no_destination_is_skipped(tmp_db, monkeypatch):
    """No destination and no groupInfo either — an unrecognized/ambiguous
    shape must fail closed, not be guessed at as Note to Self."""
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_sent_message_no_destination("https://example.com/ambiguous"))

    ingest_calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: ingest_calls.append(url) or {"episode_id": 1})

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert summary["skipped"] == 1
    assert ingest_calls == []


# ---------------------------------------------------------------------------
# Note-to-Self URLs are extracted and ingested
# ---------------------------------------------------------------------------

def test_note_to_self_url_is_ingested(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("Schau dir das an: https://example.com/article-1"))

    ingest_calls = []

    def fake_ingest(url):
        ingest_calls.append(url)
        return {"episode_id": 1}

    monkeypatch.setattr(knowledge.ingest, "ingest_url", fake_ingest)
    # New episode -> processing is triggered too; keep it a no-op here.
    monkeypatch.setattr(podcast, "retry_episode", lambda episode_id: {"status": "summarized", "error": None})
    monkeypatch.setattr(database, "get_episode", lambda episode_id: {"id": episode_id, "title": "Article One"})

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert ingest_calls == ["https://example.com/article-1"]
    assert summary["ingested"] == 1
    assert summary["processed"] == 1
    assert summary["results"][0]["ok"] is True
    assert summary["results"][0]["episode_id"] == 1


def test_dedup_same_url_twice_ingests_once(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(
        _envelope_note_to_self("https://example.com/dup"),
        _envelope_note_to_self("nochmal: https://example.com/dup"),
    )

    ingest_calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: ingest_calls.append(url) or {"episode_id": 1})
    monkeypatch.setattr(podcast, "retry_episode", lambda episode_id: {"status": "summarized", "error": None})
    monkeypatch.setattr(database, "get_episode", lambda episode_id: {"id": episode_id, "title": "Dup"})

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert ingest_calls == ["https://example.com/dup"]
    assert summary["ingested"] == 1


def test_already_existing_episode_is_not_reprocessed(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("https://example.com/already-there"))

    monkeypatch.setattr(knowledge.ingest, "ingest_url",
                         lambda url: {"status": "already_exists", "episode_id": 7})
    process_calls = []
    monkeypatch.setattr(podcast, "retry_episode", lambda episode_id: process_calls.append(episode_id) or {"status": "summarized"})
    monkeypatch.setattr(database, "get_episode", lambda episode_id: {"id": episode_id, "title": "Already There"})

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert process_calls == []
    assert summary["results"][0]["ok"] is True


# ---------------------------------------------------------------------------
# Retry queue for failed ingests
# ---------------------------------------------------------------------------

def test_ingest_failure_goes_to_retry_queue(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("https://example.com/flaky"))

    def failing_ingest(url):
        raise knowledge.ingest.IngestError("temporary failure")

    monkeypatch.setattr(knowledge.ingest, "ingest_url", failing_ingest)

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert summary["failed"] == 0  # not yet given up
    assert len(summary["errors"]) == 1
    queue = signal_inbox._load_retry_queue()
    assert queue == [{"url": "https://example.com/flaky", "attempts": 1}]


def test_first_failure_still_sends_a_receipt_line(tmp_db, monkeypatch):
    """Bug fix: the "📥 已收到…开始处理" notice was being sent even when the
    only outcome was "queued for retry", leaving Daniel with no follow-up
    message at all — no way to tell "still running" from "silently
    retrying" from "died". Every URL that triggers the start notice must
    produce a corresponding line in the final receipt, even on attempt 1."""
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("https://example.com/flaky"))

    def failing_ingest(url):
        raise knowledge.ingest.IngestError("temporary failure")

    monkeypatch.setattr(knowledge.ingest, "ingest_url", failing_ingest)

    sent = []
    monkeypatch.setattr(podcast, "send_signal_text", lambda text, context="signal": sent.append(text) or True)

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert summary["failed"] == 0
    # sent[0] is the "received, starting" notice; sent[1] is the receipt —
    # the receipt must mention the URL even though it isn't a final failure.
    assert len(sent) == 2
    assert "https://example.com/flaky" in sent[1]


def test_retry_queue_is_retried_and_dropped_after_three_attempts(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)

    def failing_ingest(url):
        raise knowledge.ingest.IngestError("still broken")

    monkeypatch.setattr(knowledge.ingest, "ingest_url", failing_ingest)

    # No new messages this run — only the retry queue is processed.
    signal_inbox._save_retry_queue([{"url": "https://example.com/flaky", "attempts": 2}])

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(""))

    assert summary["failed"] == 1
    assert signal_inbox._load_retry_queue() == []
    assert any("已放弃" in line or "flaky" in line for line in summary["errors"])


def test_retry_queue_processed_before_new_messages(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    signal_inbox._save_retry_queue([{"url": "https://example.com/old", "attempts": 1}])
    stdout = _lines(_envelope_note_to_self("https://example.com/new"))

    order = []

    def fake_ingest(url):
        order.append(url)
        return {"episode_id": 1}

    monkeypatch.setattr(knowledge.ingest, "ingest_url", fake_ingest)
    monkeypatch.setattr(podcast, "retry_episode", lambda episode_id: {"status": "summarized"})
    monkeypatch.setattr(database, "get_episode", lambda episode_id: {"id": episode_id, "title": "T"})

    signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert order == ["https://example.com/old", "https://example.com/new"]


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

def test_no_receipt_sent_when_nothing_to_do(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("kein Link hier, nur Text"))

    sent = []
    monkeypatch.setattr(podcast, "send_signal_text", lambda text, context="signal": sent.append(text) or True)

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert summary["skipped"] == 1
    assert sent == []


def test_receipt_sent_when_url_processed(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("https://example.com/one"))

    sent = []
    monkeypatch.setattr(podcast, "send_signal_text", lambda text, context="signal": sent.append(text) or True)
    monkeypatch.setattr(knowledge.ingest, "ingest_url", lambda url: {"episode_id": 1})
    monkeypatch.setattr(podcast, "retry_episode", lambda episode_id: {"status": "summarized"})
    monkeypatch.setattr(database, "get_episode", lambda episode_id: {"id": episode_id, "title": "One"})

    signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    # Two sends: the "received, starting" notice and the final receipt.
    assert len(sent) == 2
    assert "已收到" in sent[0]
    assert "One" in sent[1]


def test_send_receipt_noop_on_empty_lines(monkeypatch):
    called = []
    monkeypatch.setattr(podcast, "send_signal_text", lambda text, context="signal": called.append(text) or True)
    assert signal_inbox.send_receipt([]) is False
    assert called == []


# ---------------------------------------------------------------------------
# "paste the text" messages (#834)
# ---------------------------------------------------------------------------

LONG_BODY = "Das ist der Text eines Artikels hinter einer Bezahlschranke. " * 8
assert len(LONG_BODY) >= 200


def test_parse_pasted_text_recognizes_the_keyword():
    assert signal_inbox.parse_pasted_text("text\nHallo Welt") == "Hallo Welt"
    assert signal_inbox.parse_pasted_text("text:\nHallo Welt") == "Hallo Welt"
    # Phone keyboards capitalise the first word on their own.
    assert signal_inbox.parse_pasted_text("Text\nHallo Welt") == "Hallo Welt"
    assert signal_inbox.parse_pasted_text("文本\n你好") == "你好"


def test_parse_pasted_text_requires_the_keyword_to_stand_alone():
    """A normal message that merely starts with the word must keep going
    down the URL path — the keyword owns the first line or it isn't one."""
    assert signal_inbox.parse_pasted_text("Text von gestern: https://example.com/a") is None
    assert signal_inbox.parse_pasted_text("https://example.com/a") is None
    assert signal_inbox.parse_pasted_text("text") is None      # keyword but no body
    assert signal_inbox.parse_pasted_text("") is None


def test_pasted_text_is_ingested_as_an_article(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self(f"text\n{LONG_BODY}"))

    calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_text",
                        lambda title, text, **kw: calls.append((title, text, kw)) or {"episode_id": 7})
    monkeypatch.setattr(knowledge.ingest, "ingest_url",
                        lambda url: pytest.fail("a pasted body must not go down the URL path"))
    monkeypatch.setattr(podcast, "retry_episode", lambda episode_id: {"status": "summarized"})
    monkeypatch.setattr(database, "get_episode", lambda episode_id: {"id": episode_id, "title": "Bezahlschranke"})

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert summary["ingested"] == 1
    assert len(calls) == 1
    title, text, kw = calls[0]
    # Title/author are left to the server's AI extraction (#833).
    assert title is None
    assert text == LONG_BODY.strip()


def test_pasted_text_keeps_the_first_link_as_source_url(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    body = f"https://spiegel.de/artikel\n{LONG_BODY}"
    stdout = _lines(_envelope_note_to_self(f"text\n{body}"))

    calls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_text",
                        lambda title, text, **kw: calls.append(kw) or {"episode_id": 7})
    monkeypatch.setattr(podcast, "retry_episode", lambda episode_id: {"status": "summarized"})
    monkeypatch.setattr(database, "get_episode", lambda episode_id: {"id": episode_id, "title": "T"})

    signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert calls[0]["source_url"] == "https://spiegel.de/artikel"


def test_pasted_text_is_processed_immediately(tmp_db, monkeypatch):
    """Same "now, not later" semantics as a shared link (#749)."""
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self(f"text\n{LONG_BODY}"))

    processed = []
    monkeypatch.setattr(knowledge.ingest, "ingest_text",
                        lambda title, text, **kw: {"episode_id": 7})
    monkeypatch.setattr(podcast, "retry_episode",
                        lambda episode_id: processed.append(episode_id) or {"status": "summarized"})
    monkeypatch.setattr(database, "get_episode", lambda episode_id: {"id": episode_id, "title": "T"})

    signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert processed == [7]


def test_pasted_text_failure_reports_but_does_not_queue_a_retry(tmp_db, monkeypatch):
    """Bodies never enter the URL retry queue: it lives in app_settings as
    JSON, and "too short" fails identically every time anyway."""
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("text\nzu kurz"))

    def boom(title, text, **kw):
        raise knowledge.ingest.IngestError("pasted text too short (7 chars, need >= 200)")
    monkeypatch.setattr(knowledge.ingest, "ingest_text", boom)

    sent = []
    monkeypatch.setattr(podcast, "send_signal_text",
                        lambda text, context="signal": sent.append(text) or True)

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert summary["failed"] == 1
    assert signal_inbox._load_retry_queue() == []
    receipt = "\n".join(sent)
    assert "too short" in receipt
    # Privacy (#755): the message body itself must never reach a log,
    # an error message or the receipt.
    assert "zu kurz" not in receipt


def test_a_plain_link_message_is_unaffected(tmp_db, monkeypatch):
    """No keyword -> byte-for-byte the pre-#834 URL behaviour."""
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("https://example.com/a"))

    urls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url",
                        lambda url: urls.append(url) or {"episode_id": 1})
    monkeypatch.setattr(knowledge.ingest, "ingest_text",
                        lambda title, text, **kw: pytest.fail("not a pasted body"))
    monkeypatch.setattr(podcast, "retry_episode", lambda episode_id: {"status": "summarized"})
    monkeypatch.setattr(database, "get_episode", lambda episode_id: {"id": episode_id, "title": "T"})

    signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert urls == ["https://example.com/a"]


def test_pasted_text_from_someone_else_is_ignored(tmp_db, monkeypatch):
    """The Note-to-Self gate applies to the new path exactly as it does to
    the URL path — otherwise anyone messaging Daniel could spend AI money."""
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_from_other(f"text\n{LONG_BODY}"))

    monkeypatch.setattr(knowledge.ingest, "ingest_text",
                        lambda title, text, **kw: pytest.fail("must not ingest a stranger's message"))

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))
    assert summary["skipped"] == 1


# ---------------------------------------------------------------------------
# "add these words" via the explicit `word` keyword (#1041)
# ---------------------------------------------------------------------------

def _fake_add_word_to_list(monkeypatch, calls):
    """Patch the one shared add-word core (routes.imports.add_word_to_list)
    the way _add_words() imports it — inside the function, from
    routes.imports, not from knowledge.signal_inbox."""
    def fake(word, lang):
        calls.append((word, lang))
        return {"status": "added", "word_zh": word}
    monkeypatch.setattr("routes.imports.add_word_to_list", fake)
    return fake


def test_word_keyword_message_adds_each_word(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("word\n促进\n默契"))

    calls = []
    _fake_add_word_to_list(monkeypatch, calls)

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert calls == [("促进", "zh"), ("默契", "zh")]
    assert summary["ingested"] == 2


# ---------------------------------------------------------------------------
# Bare word messages with no `word` keyword at all (#1091)
# ---------------------------------------------------------------------------

def test_bare_single_chinese_word_is_added(tmp_db, monkeypatch):
    """The actual real-world case that motivated #1091: Daniel just sends
    the word itself, no keyword."""
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("促进"))

    calls = []
    _fake_add_word_to_list(monkeypatch, calls)

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert calls == [("促进", "zh")]
    assert summary["ingested"] == 1
    assert summary["skipped"] == 0


def test_bare_multiline_chinese_words_are_all_added(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("促进\n默契\n生态"))

    calls = []
    _fake_add_word_to_list(monkeypatch, calls)

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert calls == [("促进", "zh"), ("默契", "zh"), ("生态", "zh")]
    assert summary["ingested"] == 3


def test_bare_comma_separated_words_on_one_line_are_all_added(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("生态，默契"))

    calls = []
    _fake_add_word_to_list(monkeypatch, calls)

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert calls == [("生态", "zh"), ("默契", "zh")]
    assert summary["ingested"] == 2


def test_bare_french_expression_is_tagged_fr(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("se réduire"))

    calls = []
    _fake_add_word_to_list(monkeypatch, calls)

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert calls == [("se réduire", "fr")]
    assert summary["ingested"] == 1


def test_bare_chinese_sentence_is_not_treated_as_a_word(tmp_db, monkeypatch):
    """A real sentence (has sentence-ending punctuation) must fall through
    to skipped, not misfire an AI call for each "word" in it."""
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("今天要记得买菜。"))

    calls = []
    _fake_add_word_to_list(monkeypatch, calls)

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert calls == []
    assert summary["skipped"] == 1


def test_bare_long_multiline_text_is_not_treated_as_a_word_list(tmp_db, monkeypatch):
    """More than 5 non-empty lines -> not a plausible word list, even if
    every individual line happens to look short."""
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("一\n二\n三\n四\n五\n六"))

    calls = []
    _fake_add_word_to_list(monkeypatch, calls)

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert calls == []
    assert summary["skipped"] == 1


def test_bare_long_sentence_is_not_treated_as_a_word_list(tmp_db, monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self(
        "Das ist ein ganz normaler langer Satz, der eindeutig kein einzelnes Wort ist."))

    calls = []
    _fake_add_word_to_list(monkeypatch, calls)

    summary = signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert calls == []
    assert summary["skipped"] == 1


def test_message_with_a_link_still_goes_down_the_url_path_not_bare_words(tmp_db, monkeypatch):
    """A message containing a URL must never be reinterpreted as a bare word
    list, even if it's short."""
    monkeypatch.setenv("SIGNAL_ACCOUNT", ACCOUNT)
    stdout = _lines(_envelope_note_to_self("https://example.com/a"))

    word_calls = []
    _fake_add_word_to_list(monkeypatch, word_calls)
    urls = []
    monkeypatch.setattr(knowledge.ingest, "ingest_url",
                        lambda url: urls.append(url) or {"episode_id": 1})
    monkeypatch.setattr(podcast, "retry_episode", lambda episode_id: {"status": "summarized"})
    monkeypatch.setattr(database, "get_episode", lambda episode_id: {"id": episode_id, "title": "T"})

    signal_inbox.check_signal_inbox(runner=_make_runner(stdout))

    assert word_calls == []
    assert urls == ["https://example.com/a"]


def test_parse_bare_words_unit():
    assert signal_inbox.parse_bare_words("促进") == [("促进", "zh")]
    assert signal_inbox.parse_bare_words("se réduire") == [("se réduire", "fr")]
    assert signal_inbox.parse_bare_words("生态，默契") == [("生态", "zh"), ("默契", "zh")]
    assert signal_inbox.parse_bare_words("今天要记得买菜。") is None
    assert signal_inbox.parse_bare_words("") is None
    assert signal_inbox.parse_bare_words("一\n二\n三\n四\n五\n六") is None
