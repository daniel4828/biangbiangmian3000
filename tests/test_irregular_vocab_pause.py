"""Normal creating card of irregular Spanish verbs is hidden by default (#1552)."""
import json

import pytest
from fastapi.testclient import TestClient

import conj_patterns
import database

MARKER = "es_irregular_vocab_paused"


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "vocab.db"))
    database.init_db()
    database.get_or_create_deck("All")
    from database.decks import _invalidate_locked_deck_cache
    _invalidate_locked_deck_cache()


def _active_deck(category):
    parent = database.get_or_create_deck_path("Español::Practice", lang="es")
    return database.get_or_create_category_decks(parent, "Practice")[category]


def _word(word, source=None):
    entry = {"word_zh": word, "lang": "es"}
    if source:
        entry["source"] = source
    return database.insert_word(entry)


def _card(word, category, state="review", saved=False, source=None):
    wid = _word(word, source)
    did = database.get_or_create_saved_deck("es") if saved else _active_deck(category)
    cid = database.insert_card(wid, category, did, state=state,
                               due=database.anki_today().isoformat())
    return cid


def _set(cid, **cols):
    conn = database.get_db()
    conn.execute("UPDATE cards SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?",
                 [*cols.values(), cid])
    conn.commit()
    conn.close()


def _marker():
    return set(json.loads(database.get_app_setting(MARKER) or "[]"))


def _setup():
    """tener (3 cards + form card) and caminar (no form cards)."""
    ids = {
        "creating": _card("tener", "creating"),
        "reading": _card("tener", "reading"),
        "listening": _card("tener", "listening"),
        "form": _card("tener · Presente", "creating", source="irregular-forms"),
        "caminar": _card("caminar", "creating"),
    }
    return ids


def test_hide_suspends_only_the_target_creating_card():
    ids = _setup()
    assert database.sync_irregular_vocab_pause(True) == 1
    c = database.get_card(ids["creating"])
    assert c["state"] == "suspended" and c["pre_suspend_state"] == "review"
    for key in ("reading", "listening", "form", "caminar"):
        assert database.get_card(ids[key])["state"] == "review"
    assert _marker() == {ids["creating"]}
    assert database.sync_irregular_vocab_pause(True) == 0


def test_show_restores_original_state():
    ids = _setup()
    _set(ids["creating"], state="learning")
    database.sync_irregular_vocab_pause(True)
    assert database.get_card(ids["creating"])["state"] == "suspended"
    assert database.sync_irregular_vocab_pause(False) == 1
    c = database.get_card(ids["creating"])
    assert c["state"] == "learning" and c["pre_suspend_state"] is None
    assert _marker() == set()
    assert database.sync_irregular_vocab_pause(False) == 0


def test_manually_suspended_card_is_never_touched():
    ids = _setup()
    _set(ids["creating"], state="suspended", pre_suspend_state="review")
    assert database.sync_irregular_vocab_pause(True) == 0
    assert _marker() == set()
    assert database.sync_irregular_vocab_pause(False) == 0
    c = database.get_card(ids["creating"])
    assert c["state"] == "suspended" and c["pre_suspend_state"] == "review"


def test_card_in_saved_deck_is_not_suspended():
    wid = _word("tener")
    cid = database.insert_card(wid, "creating", database.get_or_create_saved_deck("es"),
                               state="review", due=database.anki_today().isoformat())
    _card("tener · Presente", "creating", source="irregular-forms")
    assert database.sync_irregular_vocab_pause(True) == 0
    assert database.get_card(cid)["state"] == "review"
    assert _marker() == set()


def test_soft_deleted_form_card_makes_verb_a_non_target_and_restores():
    ids = _setup()
    database.sync_irregular_vocab_pause(True)
    assert database.get_card(ids["creating"])["state"] == "suspended"
    assert database.soft_delete_card(ids["form"])
    assert database.sync_irregular_vocab_pause(True) == 1
    assert database.get_card(ids["creating"])["state"] == "review"
    assert _marker() == set()


def test_target_ids_helper():
    ids = _setup()
    assert database.irregular_vocab_creating_card_ids() == {ids["creating"]}


def test_settings_default_and_validation():
    assert conj_patterns.settings()["irregular_vocab_creating_visible"] is False
    assert conj_patterns.vocab_creating_visible() is False
    key = "irregular_vocab_creating_visible"
    assert conj_patterns.validate_settings({key: True}) == {key: True}
    assert conj_patterns.validate_settings({key: 0}) == {key: False}
    for bad in ("yes", None, 2, 1.5):
        with pytest.raises(ValueError):
            conj_patterns.validate_settings({key: bad})
    conj_patterns.save_settings({key: True})
    assert conj_patterns.vocab_creating_visible() is True
    conj_patterns.save_settings({key: False})
    assert conj_patterns.vocab_creating_visible() is False


def test_put_endpoint_restores_card():
    import main
    ids = _setup()
    database.sync_irregular_vocab_pause(True)
    assert database.get_card(ids["creating"])["state"] == "suspended"
    client = TestClient(main.app)
    r = client.put("/api/es/conj-settings", json={"irregular_vocab_creating_visible": True})
    assert r.status_code == 200 and r.json()["irregular_vocab_creating_visible"] is True
    assert database.get_card(ids["creating"])["state"] == "review"
    r = client.put("/api/es/conj-settings", json={"irregular_vocab_creating_visible": False})
    assert r.json()["irregular_vocab_creating_visible"] is False
    assert database.get_card(ids["creating"])["state"] == "suspended"
    assert client.put("/api/es/conj-settings",
                      json={"irregular_vocab_creating_visible": "x"}).status_code == 400


def test_daily_target_skips_hidden_target_card_from_saved():
    database.set_daily_target_enabled(True, "es")
    _card("tener · Presente", "creating", source="irregular-forms")
    saved = _card("tener", "creating", state="suspended", saved=True)
    plain = _card("caminar", "creating", state="suspended", saved=True)
    assert database.ensure_daily_target("es") == 1
    assert database.get_card(saved)["state"] == "suspended"
    assert database.get_card(plain)["state"] == "new"
    # setting on -> the target card is a normal candidate again
    database.set_app_setting(conj_patterns.KEY_VOCAB_VISIBLE, "1")
    assert database.ensure_daily_target("es") == 1
    assert database.get_card(saved)["state"] == "new"
