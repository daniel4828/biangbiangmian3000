"""Wählbare TTS-Stimme je Sprache (Einstellungen → Voices)."""
import database
import languages
import tts


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "t.db"))
    database.init_db()


def test_default_es_is_dominican(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert tts.voice_for("es") == "es-DO-RamonaNeural"
    assert tts.voice_for("zh") == languages.LANGUAGES["zh"]["tts_voice"]


def test_choice_and_invalid_fallback(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    database.set_app_setting("tts_voice_zh", "zh-TW-HsiaoChenNeural")
    assert tts.voice_for("zh") == "zh-TW-HsiaoChenNeural"
    database.set_app_setting("tts_voice_zh", "kaputt")
    assert tts.voice_for("zh") == languages.LANGUAGES["zh"]["tts_voice"]


def test_api_rejects_unknown_voice(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from fastapi import HTTPException
    from routes.imports import put_tts_voice
    try:
        put_tts_voice({"lang": "es", "voice": "xx"})
        assert False
    except HTTPException as e:
        assert e.status_code == 400
    r = put_tts_voice({"lang": "es", "voice": "es-MX-DaliaNeural"})
    assert r["langs"]["es"]["current"] == "es-MX-DaliaNeural"
