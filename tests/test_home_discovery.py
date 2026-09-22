from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import database


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, 'DB_PATH', str(tmp_path / 'home.db'))
    database.init_db()


def item(key, kind='article', source='source', published=None, **fields):
    ident = database.create_pending_episode(key, source, key, published,
                                           'https://example.com/' + key, kind=kind)
    database.update_episode(ident, **{'transcript_zh': '可阅读的内容', **fields})
    return ident


def at(hour, minute=0):
    return datetime(2026, 9, 22, hour, minute, tzinfo=ZoneInfo('Europe/Berlin'))


def test_morning_and_two_hour_boundaries():
    for n in range(9):
        item(str(n), source=str(n % 3))
    assert database.home_discovery(at(9, 59))['recommendations'] == []
    first = database.home_discovery(at(10))['recommendations']
    assert len(first) == 3
    assert first == database.home_discovery(at(11, 59))['recommendations']
    second = database.home_discovery(at(12))['recommendations']
    assert not {x['id'] for x in first} & {x['id'] for x in second}
    assert len({x['channel_id'] for x in first}) == 3
    assert database.home_discovery(at(0))['recommendations'] == []


def test_today_podcast_matches_feed_and_local_publication_date():
    database.create_feed('coffee', title='声动早咖啡')
    item('old', 'podcast', 'coffee', '2026-09-21T00:00:00+00:00')
    today = item('Daily episode', 'podcast', 'coffee', '2026-09-21T23:30:00+00:00')
    item('tomorrow', 'podcast', 'coffee', '2026-09-22T23:30:00+00:00')
    result = database.home_discovery(at(10))
    assert result['podcast']['id'] == today
    assert today not in [x['id'] for x in result['recommendations']]


def test_no_old_podcast_fallback_and_small_library():
    item('声动早咖啡 old', 'podcast', published='2026-09-21')
    good = item('good')
    item('archived', archived_at='2026-09-22')
    item('broken', status='error', transcript_zh=None)
    result = database.home_discovery(at(10))
    assert result['podcast'] is None
    assert good in [x['id'] for x in result['recommendations']]
    assert len(result['recommendations']) == 2


def test_api_validates_timezone_and_returns_compact_payload():
    from fastapi.testclient import TestClient
    import main
    client = TestClient(main.app)
    assert client.get('/api/home-discovery?tz=Invalid/Timezone').status_code == 422
    response = client.get('/api/home-discovery?tz=Europe/Berlin')
    assert response.status_code == 200
    assert response.json()['recommendations'] == []


def test_coffee_without_audio_or_transcript_is_not_ready():
    ident = item('声动早咖啡', 'podcast', published='2026-09-22', transcript_zh=None)
    assert database.home_discovery(at(10))['podcast']['can_listen'] is False
    database.update_episode(ident, transcript_zh='文字', audio_url='https://example.com/test.mp3')
    assert database.home_discovery(at(10))['podcast']['can_listen'] is True
