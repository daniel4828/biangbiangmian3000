"""Explicit cost opt-outs must prevent requests even with valid API keys."""
import pytest
import database
import database.core
import podcast
from audio import AudioTrackError, asr_cloud


@pytest.fixture
def policy_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, 'DB_PATH', str(tmp_path / 'policy.db'))
    database.init_db()
    database.set_app_setting('openai_audio_enabled', '0')
    database.set_app_setting('auto_listen_enabled', '0')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')


def test_podcast_whisper_disabled_before_audio_or_client(policy_db, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Disabled transcription must not split or send audio')
    monkeypatch.setattr(podcast, '_split_audio_segments', forbidden)
    assert podcast._transcribe_via_whisper('missing.mp3', 60, 'episode', '/tmp') is None


def test_cloud_openai_disabled_before_probe(policy_db, monkeypatch):
    monkeypatch.setattr(asr_cloud, 'is_offline', lambda: False)
    monkeypatch.setattr(asr_cloud, '_probe_duration_seconds', lambda *a: pytest.fail('No probe'))
    with pytest.raises(AudioTrackError, match='disabled'):
        asr_cloud.build('missing.mp3', provider='openai')


def test_low_level_openai_call_cannot_bypass_policy(policy_db):
    with pytest.raises(AudioTrackError, match='disabled'):
        asr_cloud._call_openai(None, 'missing.mp3')


def test_auto_listen_opt_out_stops_before_loading_episode(policy_db, monkeypatch):
    monkeypatch.setattr(database, 'get_episode', lambda *a: pytest.fail('No auto-listen work'))
    podcast._maybe_prepare_listen(1)


def test_disabling_during_podcast_transcription_stops_next_chunk(policy_db, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import openai

    database.set_app_setting('openai_audio_enabled', '1')
    paths = [tmp_path / 'one.mp3', tmp_path / 'two.mp3']
    for path in paths:
        path.write_bytes(b'audio')
    monkeypatch.setattr(podcast, '_split_audio_segments', lambda *a: list(map(str, paths)))
    sent = []

    def create(**kwargs):
        sent.append(kwargs['file'].name)
        database.set_app_setting('openai_audio_enabled', '0')
        return SimpleNamespace(text='first chunk')

    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
    monkeypatch.setattr(openai, 'OpenAI', lambda: client)
    with pytest.raises(RuntimeError, match='failed twice'):
        podcast._transcribe_via_whisper(str(paths[0]), 600, 'episode', str(tmp_path))
    assert sent == [str(paths[0])]
