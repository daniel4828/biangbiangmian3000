"""Sibling groups for Spanish irregular-verb form cards (#1553).

"caer · Presente", "caer · Imperativo" and the verb "caer" itself are one
sibling group: one card of the group per day, like the three cards of one word.
Chinese / French keep word_id as the group.
"""
import pytest

import database
from database.siblings import sibling_group_key, sibling_group_map, sibling_group_members


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    conn = database.get_db()
    conn.execute("UPDATE deck_presets SET bury_quick_mode='all'")
    conn.commit()
    conn.close()


def _today():
    return database.anki_today().isoformat()


def _es_word(word, form=False):
    row = {"word_zh": word, "lang": "es"}
    if form:
        row.update(note_type="grammar", pos="Verbform", source="irregular-forms", notes="x")
    return database.insert_word(row)


@pytest.fixture
def world(tmp_db):
    day = _today()
    daily, _ = database.get_or_create_daily_deck(day, "es")
    leaves = database.get_or_create_category_decks(daily, day)
    w = {"leaves": leaves}
    caer = _es_word("caer")
    w["caer"] = caer
    w["caer_cards"] = {cat: database.insert_card(caer, cat, leaves[cat], state="new", due=day)
                       for cat in ("listening", "reading", "creating")}
    w["pres_word"] = _es_word("caer · Presente", form=True)
    w["imp_word"] = _es_word("caer · Imperativo", form=True)
    w["pres"] = database.insert_card(w["pres_word"], "creating", leaves["creating"], state="new", due=day)
    w["imp"] = database.insert_card(w["imp_word"], "creating", leaves["creating"], state="new", due=day)
    tener = _es_word("tener · Presente", form=True)
    w["tener_word"] = tener
    w["tener"] = database.insert_card(tener, "creating", leaves["creating"], state="new", due=day)
    return w


def _buried(card_id):
    return database.get_card(card_id)["buried_until"]


def _all_flags(word_id, category):
    database.bury_siblings(word_id, category, bury_new=True, bury_review=True, bury_learning=True)


def test_group_helper(world):
    conn = database.get_db()
    try:
        members = sibling_group_members({world["caer"], world["pres_word"], world["tener_word"]}, conn)
        groups = sibling_group_map({world["caer"], world["pres_word"], world["imp_word"]}, conn)
    finally:
        conn.close()
    caer_group = {world["caer"], world["pres_word"], world["imp_word"]}
    assert members[world["caer"]] == caer_group
    assert members[world["pres_word"]] == caer_group
    assert members[world["tener_word"]] == {world["tener_word"]}
    assert len(set(groups.values())) == 1
    assert sibling_group_key(7, "你好", "zh") == 7
    assert sibling_group_key(8, "manger", "fr") == 8
    assert sibling_group_key(9, "caer · Presente", "es") == sibling_group_key(10, "caer", "es")


def test_two_form_cards_yield_one_due_card(world):
    leaf = world["leaves"]["creating"]
    ids = {c["id"] for c in database.get_due_cards(leaf, "creating")}
    assert {world["pres"], world["imp"]} <= ids  # no suppression: both are due
    suppressed = {c["id"] for c in database.get_due_cards(leaf, "creating", sibling_suppression=True)}
    # caer's listening card (best category rank) owns the group, so caer's form
    # cards are suppressed. (caer's own creating card may stay: the #1395
    # exemption "reading/listening don't bury creating" covers the same entry.)
    assert suppressed & {world["pres"], world["imp"]} == set()
    # ... while tener is a different verb and stays
    assert world["tener"] in suppressed


def test_two_form_cards_without_normal_cards(world):
    conn = database.get_db()
    conn.execute("UPDATE cards SET state='suspended' WHERE word_id=?", (world["caer"],))
    conn.commit()
    conn.close()
    leaf = world["leaves"]["creating"]
    kept = {c["id"] for c in database.get_due_cards(leaf, "creating", sibling_suppression=True)}
    assert len(kept & {world["pres"], world["imp"]}) == 1


def test_suppression_off_keeps_all(world):
    conn = database.get_db()
    conn.execute("UPDATE deck_presets SET bury_quick_mode='none'")
    conn.execute("UPDATE decks SET bury_quick_mode='none'")
    conn.commit()
    conn.close()
    leaf = world["leaves"]["creating"]
    ids = {c["id"] for c in database.get_due_cards(leaf, "creating", sibling_suppression=True)}
    assert {world["pres"], world["imp"], world["tener"]} <= ids


def test_reviewing_form_card_buries_other_form_and_normal_cards(world):
    _all_flags(world["pres_word"], "creating")
    today = _today()
    assert _buried(world["imp"]) == today
    for card in world["caer_cards"].values():
        assert _buried(card) == today
    assert _buried(world["pres"]) is None  # the reviewed card itself
    assert _buried(world["tener"]) is None


def test_reviewing_normal_creating_buries_form_cards(world):
    _all_flags(world["caer"], "creating")
    today = _today()
    assert _buried(world["pres"]) == today
    assert _buried(world["imp"]) == today
    assert _buried(world["caer_cards"]["listening"]) == today
    assert _buried(world["caer_cards"]["creating"]) is None
    assert _buried(world["tener"]) is None


def test_reviewing_normal_listening_buries_form_cards(world):
    _all_flags(world["caer"], "listening")
    today = _today()
    assert _buried(world["pres"]) == today
    assert _buried(world["imp"]) == today
    assert _buried(world["tener"]) is None


def test_get_sibling_cards_includes_group(world):
    sibs = {c["id"] for c in database.get_sibling_cards(world["pres"])}
    assert sibs == {world["imp"], *world["caer_cards"].values()}
    sibs = {c["id"] for c in database.get_sibling_cards(world["caer_cards"]["listening"])}
    assert sibs == {world["caer_cards"]["reading"], world["caer_cards"]["creating"],
                    world["pres"], world["imp"]}


def test_burying_off_does_nothing_new(world):
    database.bury_siblings(world["pres_word"], "creating")  # no flag set
    assert _buried(world["imp"]) is None
    assert _buried(world["caer_cards"]["listening"]) is None


def test_parent_badge_counts_group_once(world):
    pairs = [(world["leaves"]["creating"], "creating")]
    counts = database.count_due_deduped(pairs)
    # caer-group counts once + tener once
    assert counts["new"] == 2


def test_chinese_siblings_stay_per_word(tmp_db):
    day = _today()
    daily, _ = database.get_or_create_daily_deck(day, "zh")
    leaves = database.get_or_create_category_decks(daily, day)
    a = database.insert_word({"word_zh": "你好", "lang": "zh"})
    b = database.insert_word({"word_zh": "谢谢", "lang": "zh"})
    a_l = database.insert_card(a, "listening", leaves["listening"], state="new", due=day)
    a_c = database.insert_card(a, "creating", leaves["creating"], state="new", due=day)
    b_l = database.insert_card(b, "listening", leaves["listening"], state="new", due=day)
    b_c = database.insert_card(b, "creating", leaves["creating"], state="new", due=day)
    conn = database.get_db()
    try:
        assert sibling_group_members({a, b}, conn) == {a: {a}, b: {b}}
    finally:
        conn.close()
    assert {c["id"] for c in database.get_sibling_cards(a_l)} == {a_c}
    _all_flags(a, "listening")
    assert _buried(a_c) == _today()
    assert _buried(b_l) is None and _buried(b_c) is None
    # suppression: a_c is buried, b_c is suppressed by b_l (same word only)
    kept = {c["id"] for c in database.get_due_cards(leaves["creating"], "creating",
                                                    sibling_suppression=True)}
    assert kept == set()
    kept = {c["id"] for c in database.get_due_cards(leaves["listening"], "listening",
                                                    sibling_suppression=True)}
    assert kept == {a_l, b_l}


def test_leaf_badge_counts_verb_once(world):
    """Creating leaf: caer + 2 caer form cards + tener form card -> 2 new (one per verb)."""
    leaf = world["leaves"]["creating"]
    assert database.count_due(leaf, "creating")["new"] == 2
    assert database.cards._count_due_bulk([leaf], "creating")[leaf]["new"] == 2
