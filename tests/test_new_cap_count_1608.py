"""#1608: Neu/Tag wirkte im gemischten Review scheinbar nicht.

1. Der Kopfzähler (count_due_any_cat) deduplizierte zuerst nach Wort
   (listening gewinnt) und kappte danach. Wurde die Listening-Karte eines
   Worts vom Limit abgeschnitten, verschwand auch seine Creating-Karte aus
   der Zahl — die Warteschlange lieferte sie trotzdem. Die Zahl blieb stehen.
2. "Heute eingeführt" verglich date(reviewed_at) (UTC) mit dem Anki-Tag
   (Berlin, Grenze 4 Uhr): zwischen 1 und 4 Uhr zählte nichts mehr.
"""
from datetime import datetime, timedelta, timezone

import pytest

import database
from database import cards as C
from test_browse_lang import _entry


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, 'DB_PATH', str(tmp_path / 'cap1608.db'))
    database.init_db()
    database.get_or_create_deck("All")
    from database.decks import _invalidate_locked_deck_cache
    _invalidate_locked_deck_cache()


def _root_id():
    conn = database.get_db()
    rid = conn.execute("SELECT id FROM decks WHERE name='All' AND parent_id IS NULL").fetchone()[0]
    conn.close()
    return rid


def _setup_both_categories(n_words=5, limit=3):
    conn = database.get_db()
    conn.execute('UPDATE deck_presets SET new_per_day=?', (limit,))
    conn.commit()
    conn.close()
    today = database.anki_today().isoformat()
    parent = database.get_or_create_deck_path('Español::Tag0', lang='es')
    leaves = database.get_or_create_category_decks(parent, 'Tag0')
    for i in range(n_words):
        wid = _entry(f'palabra{i}', 'es')
        database.insert_card(wid, 'listening', leaves['listening'], state='new', due=today)
        database.insert_card(wid, 'creating', leaves['creating'], state='new', due=today)
    return leaves


def test_header_new_count_matches_queue_when_listening_is_capped():
    _setup_both_categories(n_words=5, limit=3)
    root = _root_id()

    queue_new = [c for c in database.get_due_cards_any_cat(root, lang='es') if c['state'] == 'new']
    counts = database.count_due_any_cat(root, lang='es')

    assert len(queue_new) == 6  # 3 listening + 3 creating
    assert counts['new'] == len(queue_new)


def test_anki_day_bounds_cover_24_hours_from_cutoff():
    today = database.anki_today()
    start, end = C._anki_day_utc_bounds(today.isoformat())
    s = datetime.fromisoformat(start)
    e = datetime.fromisoformat(end)
    assert timedelta(hours=23) <= e - s <= timedelta(hours=25)
    # Start is the cutoff hour in local time, stored as UTC.
    local = s.replace(tzinfo=timezone.utc).astimezone()
    assert local.date() == today
    assert local.hour == database.get_day_cutoff_hour()


def test_review_late_at_night_counts_as_introduced_today():
    """A first review 30 min before the next cutoff (Berlin ~03:30, UTC is
    already the next calendar day) still uses up today's new-card budget."""
    leaves = _setup_both_categories(n_words=1, limit=3)
    today = database.anki_today().isoformat()
    _, end = C._anki_day_utc_bounds(today)
    late = (datetime.fromisoformat(end) - timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')

    conn = database.get_db()
    cid = conn.execute("SELECT id FROM cards WHERE deck_id=? AND category='listening'",
                       (leaves['listening'],)).fetchone()[0]
    conn.execute("UPDATE cards SET state='learning' WHERE id=?", (cid,))
    conn.execute("INSERT INTO review_log(card_id, rating, reviewed_at) VALUES (?,3,?)", (cid, late))
    conn.commit()
    assert C._count_new_introduced_today(conn, leaves['listening'], 'listening', today) == 1
    assert C._count_new_introduced_today_multi(conn, [leaves['listening']], 'listening', today) == 1
    conn.close()


def test_review_from_previous_anki_day_is_not_introduced_today():
    leaves = _setup_both_categories(n_words=1, limit=3)
    today = database.anki_today().isoformat()
    start, _ = C._anki_day_utc_bounds(today)
    before = (datetime.fromisoformat(start) - timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')

    conn = database.get_db()
    cid = conn.execute("SELECT id FROM cards WHERE deck_id=? AND category='listening'",
                       (leaves['listening'],)).fetchone()[0]
    conn.execute("INSERT INTO review_log(card_id, rating, reviewed_at) VALUES (?,3,?)", (cid, before))
    conn.commit()
    assert C._count_new_introduced_today(conn, leaves['listening'], 'listening', today) == 0
    conn.close()
