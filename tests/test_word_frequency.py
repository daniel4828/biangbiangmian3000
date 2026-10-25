from test_browse_lang import client, tmp_db, _entry


def test_frequency_is_exact_and_language_scoped(client):
    _entry('的', 'zh')
    _entry('我', 'zh')
    _entry('完全不存在的测试词语', 'zh')
    _entry('你', 'fr')
    words = {w['word_zh']: w for w in client.get('/api/browse-words').json()}
    assert words['的']['frequency_rank'] == 1
    assert words['我']['frequency_rank'] > 1
    assert words['完全不存在的测试词语']['frequency_rank'] is None
    assert words['你']['frequency_rank'] is None


def test_word_detail_carries_frequency_rank(client):
    zh_id = _entry('的', 'zh')
    fr_id = _entry('de', 'fr')
    assert client.get(f'/api/word/{zh_id}').json()['frequency_rank'] == 1
    assert client.get(f'/api/word/{fr_id}').json()['frequency_rank'] is None
