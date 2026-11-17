"""Neue Karten kommen FIFO: zuletzt hinzugefügt → zuletzt gezeigt."""
import database
from database import new_card_fifo_key


def test_fifo_key_orders_by_queued_at_then_id():
    cards = [
        {"id": 1, "queued_at": "2026-11-17 10:00:02.000"},  # zurückgesetzt, alte id
        {"id": 5, "queued_at": "2026-11-17 10:00:01.000"},
        {"id": 3, "queued_at": None},                        # Altbestand
    ]
    assert [c["id"] for c in sorted(cards, key=new_card_fifo_key)] == [3, 5, 1]


def test_reset_sets_queued_at_later_than_newer_card():
    database.init_db()
    deck = database.get_or_create_deck("FifoDeck")
    conn = database.get_db()
    w1 = conn.execute("INSERT INTO entries (word_zh) VALUES ('先甲')").lastrowid
    w2 = conn.execute("INSERT INTO entries (word_zh) VALUES ('后乙')").lastrowid
    conn.commit(); conn.close()
    c1 = database.insert_card(w1, "reading", deck)
    c2 = database.insert_card(w2, "reading", deck)
    # w1 (ältere id) wird später erneut hinzugefügt → muss nach w2 kommen
    import time; time.sleep(0.01)
    database.reset_word_to_new(w1, {"reading": deck}, database.anki_today().isoformat())
    conn = database.get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, queued_at FROM cards WHERE id IN (?, ?)", (c1, c2))]
    conn.close()
    assert [r["id"] for r in sorted(rows, key=new_card_fifo_key)] == [c2, c1]
