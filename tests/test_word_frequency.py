import pytest

import lexicon
from test_browse_lang import client, tmp_db, _entry
from word_frequency import frequency_rank


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


@pytest.fixture
def es_lexicon():
    lexicon.inject('es', {'version': 1,
                          'lemmas': {'tener': {'r': 20}, 'quejar': {'r': 900}},
                          'forms': {}, 'verbs': {}})
    try:
        yield
    finally:
        lexicon.clear_cache()


def test_spanish_rank_uses_lemma_form_cards_and_reflexive_stem(es_lexicon):
    assert frequency_rank('tener', 'es') == 20
    assert frequency_rank('tener · Indefinido', 'es') == 20
    assert frequency_rank('quejarse', 'es') == 900
    assert frequency_rank('desconocidoxyz', 'es') is None
    assert frequency_rank('tener', 'fr') is None


def test_spanish_entry_gets_rank_in_browse_words(client, es_lexicon):
    _entry('tener', 'es')
    _entry('desconocidoxyz', 'es')
    words = {w['word_zh']: w for w in client.get('/api/browse-words').json()}
    assert words['tener']['frequency_rank'] == 20
    assert words['desconocidoxyz']['frequency_rank'] is None
