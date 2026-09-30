"""Spotify sharing resolves public RSS audio, never previews or paid APIs."""
import json
import pytest
import database
import database.core
import knowledge.ingest as ingest
EP = '6rqhFgbbKwnb9MLmUQDhG6'
URL = f'https://open.spotify.com/episode/{EP}'
FEED = 'https://example.com/feed.xml'
ENTITY = {'type': 'episode', 'id': EP, 'title': 'An episode', 'subtitle': 'A show', 'releaseDate': {'isoString': '2021-07-30T13:13:00Z'}, 'duration': 2152056, 'audioPreview': {'url': 'https://example.com/preview.mp3'}}
RSS = '''<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>A show</title><item><guid>rss-guid</guid><title>An episode</title><pubDate>Fri, 30 Jul 2021 13:13:00 GMT</pubDate><itunes:duration>35:52</itunes:duration><enclosure url="https://example.com/full.mp3" type="audio/mpeg"/></item></channel></rss>'''
@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, 'DB_PATH', str(tmp_path / 'db.sqlite'))
    database.init_db()
    for feed in database.list_feeds():
        database.delete_feed(feed["id"])
    import ai
    monkeypatch.setattr(ai, '_call_api', lambda *a, **k: pytest.fail('No AI at ingest'))
    monkeypatch.setattr(ingest.knowledge.article, 'fetch_article', lambda *a: pytest.fail('Spotify is not an article'))
def network(monkeypatch, rss=RSS, directory=True):
    import knowledge.spotify as spotify
    def fetch(url, **kwargs):
        if '/embed/episode/' in url:
            return ('<script id="__NEXT_DATA__" type="application/json">' + json.dumps({'props': {'pageProps': {'state': {'data': {'entity': ENTITY}}}}}) + '</script>').encode()
        if url.startswith('https://itunes.apple.com/search?') and directory:
            return json.dumps({'results': [{'collectionName': 'A show', 'feedUrl': FEED}]}).encode()
        if url == FEED:
            return rss.encode()
        pytest.fail(f'Unexpected network: {url}')
    monkeypatch.setattr(spotify, '_fetch', fetch)
def test_ingest_full_audio_and_duplicate_without_network(monkeypatch):
    network(monkeypatch)
    result = ingest.ingest_url(URL + '?si=tracking', china_critical=True)
    row = database.get_episode(result['episode_id'])
    assert (row['video_id'], row['kind'], row['platform'], row['author']) == ('rss-guid', 'podcast', 'spotify', 'A show')
    assert row['audio_url'] == 'https://example.com/full.mp3'
    assert row['spotify_url'] == URL
    assert row['china_critical'] == 1
    assert result['process_required'] is True
    assert database.list_feeds() == []
    import knowledge.spotify as spotify
    monkeypatch.setattr(spotify, '_fetch', lambda *a, **k: pytest.fail('Duplicate fetched metadata'))
    assert ingest.ingest_url(URL)['episode_id'] == row['id']
def test_existing_subscription_guid_dedup(monkeypatch):
    database.create_feed(FEED, 'A show')
    old = database.create_pending_episode('rss-guid', FEED, 'An episode', None, FEED)
    network(monkeypatch, directory=False)
    result = ingest.ingest_url(URL)
    assert result == {'status': 'already_exists', 'episode_id': old, 'process_required': True}
    assert database.get_episode(old)['spotify_url'] == URL
@pytest.mark.parametrize('replacement', [('<title>A show</title>', '<title>Wrong show</title>'), ('<title>An episode</title>', '<title>Other episode</title>'), ('35:52', '05:00'), ('30 Jul 2021', '30 Jul 2020'), ('https://example.com/full.mp3', '')])
def test_refuses_wrong_or_unavailable_full_episode(monkeypatch, replacement):
    network(monkeypatch, RSS.replace(*replacement))
    with pytest.raises(ingest.IngestError, match='RSS|audio|match'):
        ingest.ingest_url(URL)
@pytest.mark.parametrize('path', ['show/' + EP, 'track/' + EP, 'episode/bad'])
def test_non_episode_spotify_is_not_an_article(path):
    with pytest.raises(ingest.IngestError, match='episode'):
        ingest.ingest_url('https://open.spotify.com/' + path)

@pytest.mark.parametrize('path', ['intl-de/episode/', 'embed/episode/'])
def test_localized_and_embed_links_share_identity(monkeypatch, path):
    network(monkeypatch)
    row = database.get_episode(ingest.ingest_url('https://open.spotify.com/' + path + EP)['episode_id'])
    assert row['spotify_url'] == URL


def test_pending_duplicate_already_processing_is_not_restarted(monkeypatch):
    network(monkeypatch)
    result = ingest.ingest_url(URL)
    database.update_episode(result['episode_id'], processing_started_at='2026-09-30T10:00:00')
    assert ingest.ingest_url(URL)['process_required'] is False


def test_ambiguous_feed_is_refused(monkeypatch):
    duplicate = RSS[RSS.index('<item>'):RSS.index('</item>') + 7].replace('rss-guid', 'other-guid')
    network(monkeypatch, RSS.replace('</channel>', duplicate + '</channel>'))
    with pytest.raises(ingest.IngestError, match='ambiguous'):
        ingest.ingest_url(URL)


def test_mail_share_processes_existing_pending_spotify(monkeypatch):
    from test_knowledge_mailbox import FakeImap, _configure_env, _make_plain_message
    import knowledge.mailbox as mailbox
    import podcast
    _configure_env(monkeypatch)
    network(monkeypatch)
    old = ingest.ingest_url(URL)['episode_id']
    def process(episode_id):
        database.update_episode(episode_id, status='summarized', summary_de='Complete')
        return {'status': 'summarized'}
    monkeypatch.setattr(podcast, 'retry_episode', process)
    result = mailbox.check_mailbox(imap_factory=lambda: FakeImap({b'1': _make_plain_message('Episode', URL)}))
    assert result['processed'] == 1
    assert database.get_episode(old)['status'] == 'summarized'


def test_signal_share_processes_existing_pending_spotify(monkeypatch):
    from test_signal_inbox import ACCOUNT, _lines, _envelope_note_to_self, _make_runner
    import knowledge.signal_inbox as inbox
    import podcast
    monkeypatch.setenv('SIGNAL_ACCOUNT', ACCOUNT)
    monkeypatch.setattr(inbox, '_load_retry_queue', lambda: [])
    monkeypatch.setattr(inbox, '_save_retry_queue', lambda queue: None)
    monkeypatch.setattr(inbox, 'send_receipt', lambda lines: None)
    network(monkeypatch)
    old = ingest.ingest_url(URL)['episode_id']
    def process(episode_id):
        database.update_episode(episode_id, status='summarized', summary_de='Complete')
        return {'status': 'summarized'}
    monkeypatch.setattr(podcast, 'retry_episode', process)
    inbox.check_signal_inbox(runner=_make_runner(_lines(_envelope_note_to_self(URL))))
    assert database.get_episode(old)['status'] == 'summarized'


def test_processing_preserves_shared_spotify_link(monkeypatch):
    import podcast
    import routes.utils
    network(monkeypatch)
    episode_id = ingest.ingest_url(URL)['episode_id']
    database.update_episode(episode_id, transcript_zh='This is a transcript of the episode.', transcript_de=[{'zh': '文本', 'de': 'Text'}])
    monkeypatch.setattr(routes.utils, 'ai_disabled', lambda: False)
    monkeypatch.setattr(podcast, 'summarize', lambda *a, **k: {'summary_de': 'Summary', 'words': []})
    monkeypatch.setattr(podcast, 'find_spotify_url', lambda *a: 'https://open.spotify.com/search/wrong')
    monkeypatch.setattr(podcast, 'send_email', lambda *a: False)
    monkeypatch.setattr(podcast, 'send_signal', lambda *a: None)
    monkeypatch.setattr(podcast, '_maybe_prepare_listen', lambda *a: None)
    import ai
    monkeypatch.setattr(ai, '_call_api', lambda *a, **k: '[]')
    result = podcast.retry_episode(episode_id)
    assert result['status'] == 'summarized'
    assert database.get_episode(episode_id)['spotify_url'] == URL


def test_unrelated_subscriptions_do_not_delay_share(monkeypatch):
    database.create_feed('https://unrelated.example/feed', 'Unrelated show')
    network(monkeypatch)
    assert ingest.ingest_url(URL)['episode_id']


def test_short_link_canonicalizes_and_rejects_external_redirect(monkeypatch):
    import urllib.request
    import knowledge.spotify as spotify
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return URL + '?si=abc'
    class Opener:
        def open(self, request, **kwargs): return Response()
    monkeypatch.setattr(spotify.urllib.request, 'build_opener', lambda *a: Opener())
    assert spotify.canonical_url('https://spotify.link/abc') == URL
    with pytest.raises(spotify.SpotifyError, match='outside Spotify'):
        spotify._SpotifyRedirect().redirect_request(urllib.request.Request('https://spotify.link/abc'), None, 302, '', {}, 'https://evil.example/episode/' + EP)


def test_malformed_embed_does_not_use_preview(monkeypatch):
    import knowledge.spotify as spotify
    monkeypatch.setattr(spotify, '_fetch', lambda *a: b'<html>Preview unavailable</html>')
    with pytest.raises(ingest.IngestError, match='metadata'):
        ingest.ingest_url(URL)


def test_http_metadata_read_is_bounded(monkeypatch):
    import knowledge.spotify as spotify
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return b'x' * size
    monkeypatch.setattr(spotify.urllib.request, 'urlopen', lambda *a, **k: Response())
    with pytest.raises(spotify.SpotifyError, match='too large'):
        spotify._fetch(URL)


def test_short_episode_cannot_match_preview_length(monkeypatch):
    monkeypatch.setitem(ENTITY, 'duration', 120000)
    network(monkeypatch, RSS.replace('35:52', '00:30'))
    with pytest.raises(ingest.IngestError, match='RSS'):
        ingest.ingest_url(URL)


def test_renamed_subscription_found_after_empty_directory(monkeypatch):
    import knowledge.spotify as spotify
    database.create_feed(FEED, 'My custom name')
    network(monkeypatch)
    fetch = spotify._fetch
    monkeypatch.setattr(spotify, '_fetch', lambda url: b'{"results": []}' if url.startswith('https://itunes.apple.com/search?') else fetch(url))
    assert database.get_episode(ingest.ingest_url(URL)['episode_id'])['video_id'] == 'rss-guid'
