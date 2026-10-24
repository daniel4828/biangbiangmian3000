"""Daily fill must run before direct review and morning story requests (#1281)."""
import database
import pytest
from fastapi.testclient import TestClient
import main
from routes import story
from routes.utils import queue_mgr


@pytest.fixture
def saved_card(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, 'DB_PATH', str(tmp_path / 'entrypoints.db'))
    database.init_db()
    queue_mgr.invalidate()
    root = database.get_or_create_deck_path('Daily', lang='zh')
    database.get_or_create_category_decks(root, 'Daily')
    saved = database.get_or_create_saved_deck('zh')
    word = database.insert_word({'word_zh': '的', 'lang': 'zh', 'definition': 'of'})
    card = database.insert_card(word, 'listening', saved, state='suspended')
    return root, card


@pytest.mark.parametrize('path', [
    '/api/today/{root}/listening?lang=zh',
    '/api/today-mixed/{root}?lang=zh',
    '/api/today-unfinished?scope=all&lang=zh',
    '/api/today-unfinished-decks?scope=all&lang=zh',
])
def test_direct_review_activates_saved_cards(saved_card, path):
    root, card_id = saved_card
    response = TestClient(main.app).get(path.format(root=root))
    assert response.status_code == 200
    assert database.get_card(card_id)['state'] == 'new'
    assert database.get_card(card_id)['deck_id'] != database.get_or_create_saved_deck('zh')
    if 'card' in response.json():
        assert response.json()['card']['id'] == card_id


def test_morning_job_fills_even_when_story_generation_is_off(saved_card):
    _, card_id = saved_card
    database.set_app_setting('pregen_enabled', '0')
    response = TestClient(main.app).post('/api/pregen-today')
    assert response.status_code == 200
    assert response.json()['disabled'] is True
    assert database.get_card(card_id)['state'] == 'new'


def test_manual_story_word_list_includes_daily_fill(saved_card):
    root, card_id = saved_card
    cards = story._get_cards_for_story(root, 'listening', lang='zh')
    assert card_id in {card['id'] for card in cards}


def test_french_review_does_not_activate_chinese_cards(saved_card):
    _, card_id = saved_card
    response = TestClient(main.app).get('/api/today-unfinished?scope=all&lang=fr')
    assert response.status_code == 200
    assert database.get_card(card_id)['state'] == 'suspended'


def test_french_story_request_does_not_activate_chinese_cards(saved_card):
    root, card_id = saved_card
    assert story._get_cards_for_story(root, 'unified', lang='fr') == []
    assert database.get_card(card_id)['state'] == 'suspended'
