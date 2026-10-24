"""A leech can restart without erasing the progress of its healthy siblings."""
from datetime import date
import pytest
from fastapi.testclient import TestClient
import database
import main
from test_browse_lang import tmp_db, _entry


def test_set_leech_new_clears_all_scheduling_fields_only_for_leeches(tmp_db, monkeypatch):
    word = _entry('你好', 'zh')
    deck = database.get_or_create_deck('Reset tests')
    leech = database.insert_card(word, 'listening', deck)
    healthy = database.insert_card(word, 'creating', deck)
    conn = database.get_db()
    conn.execute("""UPDATE cards SET state='suspended', is_leech=1, leeched_at='2025-01-01',
        pre_suspend_state='review', due='2099-01-01', interval=80, ease=1.3,
        repetitions=30, lapses=10, learning_again_count=8, probation=1,
        stability=50, difficulty=8, last_review='2025-01-01', step_index=4,
        buried_until='2099-01-01' WHERE id=?""", (leech,))
    conn.execute("UPDATE cards SET state='review', interval=42, stability=40 WHERE id=?", (healthy,))
    before = dict(conn.execute('SELECT * FROM cards WHERE id=?', (healthy,)).fetchone())
    conn.execute('INSERT INTO review_log (card_id, rating) VALUES (?, 1)', (leech,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(database.browse, 'anki_today', lambda: date(2026, 10, 23), raising=False)
    response = TestClient(main.app).post('/api/cards/bulk-leech-new', json={'word_ids': [word]})
    assert response.status_code == 200
    assert response.json()['count'] == 1
    c = database.get_card(leech)
    assert c['state'] == 'new' and c['due'] == '2026-10-23'
    assert c['ease'] == 2.5
    for key in ['interval', 'repetitions', 'lapses', 'learning_again_count', 'probation', 'step_index', 'is_leech']:
        assert c[key] == 0, key
    for key in ['stability', 'difficulty', 'last_review', 'leeched_at', 'pre_suspend_state', 'buried_until']:
        assert c[key] is None, key
    conn = database.get_db()
    assert dict(conn.execute('SELECT * FROM cards WHERE id=?', (healthy,)).fetchone()) == before
    assert conn.execute('SELECT COUNT(*) FROM review_log WHERE card_id=?', (leech,)).fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize('payload', [{}, {'word_ids': 'bad'}, {'word_ids': [True]}, {'word_ids': [0]}, {'word_ids': ['1']}])
def test_set_leech_new_rejects_invalid_ids(tmp_db, payload):
    assert TestClient(main.app).post('/api/cards/bulk-leech-new', json=payload).status_code in (400, 422)


def test_set_leech_new_ignores_deleted_cards(tmp_db):
    word = _entry('谢谢', 'zh')
    deck = database.get_or_create_deck('Reset tests')
    card = database.insert_card(word, 'listening', deck)
    conn = database.get_db()
    conn.execute("UPDATE cards SET is_leech=1, state='suspended', deleted_at='2025-01-01' WHERE id=?", (card,))
    conn.commit()
    conn.close()
    response = TestClient(main.app).post('/api/cards/bulk-leech-new', json={'word_ids': [word]})
    assert response.status_code == 200
    assert response.json()['count'] == 0
    assert database.get_card(card)['state'] == 'suspended'
