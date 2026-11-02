"""Home-chart language filter: /api/calendar-stats?lang= only counts that language's decks."""
import pytest

import database
from test_daily_target import card, tmp_db  # noqa: F401  (isolated DB fixture)


def _review(cid):
    conn = database.get_db()
    conn.execute("INSERT INTO review_log (card_id, rating, reviewed_at, state) "
                 "VALUES (?, 3, datetime('now'), 'review')", (cid,))
    conn.commit()
    conn.close()


def _total(stats):
    return sum(d["reviews"] for d in stats["by_date"].values())


def test_calendar_stats_filters_by_lang():
    _review(card('中文词', lang='zh'))
    _review(card('palabra', lang='es'))
    _review(card('otra', lang='es'))
    assert _total(database.get_calendar_stats(30)) == 3
    assert _total(database.get_calendar_stats(30, lang='zh')) == 1
    assert _total(database.get_calendar_stats(30, lang='es')) == 2
    assert _total(database.get_calendar_stats(30, lang='fr')) == 0


def test_future_filtered_by_lang():
    conn = database.get_db()
    for cid in (card('将来', lang='zh'), card('futuro', lang='es')):
        conn.execute("UPDATE cards SET state='review', due=date('now','+3 days') WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    fut = lambda lang=None: sum(f["total"] for f in database.get_calendar_stats(30, lang=lang)["future"].values())
    assert fut() == 2 and fut('zh') == 1 and fut('es') == 1
