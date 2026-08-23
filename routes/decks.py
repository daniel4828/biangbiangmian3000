import logging

import database
import languages
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from offline import LOCAL_MODE, OFFLINE_MODE, is_offline
from .utils import queue_mgr

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flatten(tree: list) -> list:
    result = []
    for node in tree:
        result.append(node)
        result.extend(_flatten(node.get("children", [])))
    return result


def _leaf_pairs(deck: dict) -> list[tuple[int, str]]:
    """All (id, category) tuples for leaf descendants that have a category.

    Leaves are skipped when their preset disables their own category, so
    parent badges and all-suspended flags ignore the hidden category.
    """
    result = []
    for child in deck.get("children", []):
        if not child.get("children"):
            cat = child.get("category")
            if cat and child.get(f"{cat}_enabled", 1):
                result.append((child["id"], cat))
        else:
            result.extend(_leaf_pairs(child))
    return result


def _filter_tree_by_lang(tree: list, lang: str) -> list:
    """Recursively keep decks whose own lang matches, or that have a matching
    descendant (so aggregating parents like 'All' survive when they contain a
    matching child, even though the parent row's own lang column is 'zh')."""
    result = []
    for node in tree:
        children = _filter_tree_by_lang(node.get("children", []), lang)
        own_match = (node.get("lang") or "zh") == lang
        if own_match or children:
            new_node = {**node, "children": children}
            result.append(new_node)
    return result


# The three category switches (issue #898). They live on a deck preset, but the
# aggregating root 'All' is a *single* deck row that every language tab shows
# (see _filter_tree_by_lang above), so its preset's switches would otherwise be
# shared: turning Creating on under Français turned it on under 中文 too.
# Under a language tab they are read from / written to that language's own
# preset instead (#806 gave every non-zh tree one). Deliberately only these
# three: they answer "which review modes does this language have", a view
# question. The scheduling fields stay on the deck's own preset — see #629 in
# CLAUDE.md for what editing the wrong preset costs.
_CATEGORY_SWITCHES = ("reading_enabled", "listening_enabled", "creating_enabled")


def _lang_switch_preset(deck_id: int, lang: str | None, create: bool = False) -> dict | None:
    """Preset owning this deck's category switches while viewed under `lang`.

    None means "the deck's own preset" — either no language tab is in play, or
    the deck already belongs to that language.
    """
    if not lang:
        return None
    if database.get_deck_lang(deck_id) == lang:
        return None
    return database.get_lang_preset(lang, create=create)


def _attach_counts(flat_decks: list) -> None:
    """Compute due counts for all decks using bulk queries (O(1) DB round-trips)."""
    all_counts, susp_flags = database.count_due_all_decks()
    empty = {"new": 0, "learning": 0, "review": 0, "learning_future": 0, "learning_soon": 0}

    for deck in flat_decks:
        if not deck.get("children"):
            cat = deck.get("category")
            if cat:
                deck["counts"] = all_counts.get((deck["id"], cat), dict(empty))
                if cat in ("creating", "listening", "reading"):
                    deck["all_suspended"] = susp_flags.get((deck["id"], cat), False)
            else:
                deck["counts"] = dict(empty)

    for deck in reversed(flat_decks):
        if deck.get("children"):
            pairs = _leaf_pairs(deck)
            if pairs:
                merged: dict = {"new": 0, "learning": 0, "review": 0,
                                "learning_future": 0, "learning_soon": 0}
                for did, cat in pairs:
                    c = all_counts.get((did, cat), empty)
                    for k in merged:
                        merged[k] += c.get(k, 0)
                deck["counts"] = merged
                # all_suspended = every leaf's category is fully suspended
                deck["deck_all_suspended"] = all(
                    susp_flags.get((did, cat), False) for did, cat in pairs
                )
            else:
                deck["counts"] = dict(empty)
                deck["deck_all_suspended"] = False


# ---------------------------------------------------------------------------
# Deck routes
# ---------------------------------------------------------------------------

@router.get("/api/mode")
def get_mode():
    """Runtime flags the frontend needs to know about (issues #612, #625).

    offline=True means no outbound call can be made right now: the UI hides
    everything that would trigger an AI call and shows the offline banner.
    In LOCAL_MODE this flips by itself as the network comes and goes, so the
    frontend re-polls this endpoint periodically.

    local=True marks a laptop instance, which is what makes the sync button
    appear; hard_offline=True additionally means the flag was forced for a
    flight, so no probing happens at all.
    """
    return {"offline": is_offline(), "local": LOCAL_MODE or OFFLINE_MODE,
            "hard_offline": OFFLINE_MODE}


@router.get("/api/langs")
def get_langs(available: bool = False):
    """Distinct languages currently in use across decks. Frontend shows the
    tab bar only when more than one language is returned.

    available=1 returns every language registered in languages.py instead
    (#805). The home tab bar wants "in use" — showing a tab for a language
    with no cards would be noise. The /add and /dict pages want "registered":
    they are where a language *starts*, and a brand-new language has no decks
    yet, so filtering by usage there would make it impossible to add its very
    first word.
    """
    if available:
        return list(languages.LANGUAGES.keys())
    return database.get_available_langs()


@router.get("/api/decks")
def get_decks(unfinished_scope: str = "unfinished", lang: str | None = None):
    tree = database.get_deck_tree()
    if lang:
        tree = _filter_tree_by_lang(tree, lang)
    flat = _flatten(tree)
    # Attach preset-derived fields first — _attach_counts needs the
    # *_enabled flags to skip disabled category leaves in parent aggregation
    presets = {p["id"]: p for p in database.list_presets()}
    lang_preset = database.get_lang_preset(lang) if lang else None
    locked = database.get_locked_deck_ids()
    for deck in flat:
        pid = deck.get("preset_id")
        p = presets.get(pid, {})
        # #898: decks from another language tree than the tab being viewed —
        # in practice the aggregating root 'All' — take their category
        # switches from this language's preset (see _CATEGORY_SWITCHES).
        sp = lang_preset if (lang_preset and (deck.get("lang") or "zh") != lang) else p
        deck["bury_mode"] = deck.get("bury_quick_mode", "all")
        deck["new_review_order_override"] = deck.get("new_review_order_override")
        deck["category_order"] = p.get("category_order", "listening,reading,creating")
        deck["reading_enabled"] = 1 if sp.get("reading_enabled") else 0
        deck["listening_enabled"] = 1 if sp.get("listening_enabled", 1) else 0
        deck["creating_enabled"] = 1 if sp.get("creating_enabled", 1) else 0
    _attach_counts(flat)
    for deck in flat:
        # Future-dated daily decks are locked until their date — flag for the UI.
        if deck.get("id") in locked:
            deck["locked"] = True
            deck["unlock_date"] = locked[deck["id"]]
    unfinished = database.count_unfinished(unfinished_scope, lang=lang)
    if sum(unfinished.values()) > 0:
        tree.insert(0, {
            "id": "unfinished",
            "name": "Unfinished Cards",
            "virtual": True,
            "counts": unfinished,
            "children": [],
        })
    return tree


@router.post("/api/decks")
def create_deck(name: str, parent_id: int | None = None, category: str | None = None,
                lang: str | None = None):
    if lang is not None and not languages.is_valid_lang(lang):
        raise HTTPException(400, f"unknown lang: {lang!r}")
    # Support Anki-style 'Parent::Child' hierarchy in name
    if "::" in name:
        deck_id = database.get_or_create_deck_path(name, lang=lang)
        return database.get_deck(deck_id)
    if parent_id is None:
        parent_id = database.get_all_deck_id()
    preset_id = database.get_preset_for_deck(parent_id)["id"]
    # lang=None inherits the parent deck's language (database._resolve_deck_lang)
    deck_id = database.insert_deck(name, parent_id, preset_id, category, lang=lang)
    return database.get_deck(deck_id)


@router.delete("/api/decks/{deck_id}")
def delete_deck(deck_id: int):
    database.delete_deck(deck_id)
    return {"ok": True}


@router.delete("/api/decks/{deck_id}/cards")
def clear_deck_cards(deck_id: int):
    """Soft-delete all cards in a filtered deck (and its children)."""
    count = database.delete_all_deck_cards(deck_id)
    return {"ok": True, "deleted": count}


@router.get("/api/trash")
def get_trash():
    return {"decks": database.get_trash(), "cards": database.get_trashed_cards()}


@router.post("/api/trash/{deck_id}/restore")
def restore_deck(deck_id: int):
    database.restore_deck(deck_id)
    return {"ok": True}


@router.delete("/api/trash/{deck_id}")
def purge_deck(deck_id: int):
    database.purge_deck(deck_id)
    return {"ok": True}


@router.post("/api/decks/{deck_id}/creating/toggle-suspension")
def toggle_creating_suspension(deck_id: int):
    return database.toggle_category_suspension(deck_id, "creating")


@router.post("/api/decks/{deck_id}/categories/{category}/toggle-suspension")
def toggle_category_suspension(deck_id: int, category: str):
    return database.toggle_category_suspension(deck_id, category)


@router.post("/api/decks/{deck_id}/toggle-all-suspension")
def toggle_deck_all_suspension(deck_id: int):
    return database.toggle_deck_all_suspension(deck_id)


@router.post("/api/trash/cards/{card_id}/restore")
def restore_trashed_card(card_id: int):
    database.restore_card(card_id)
    return {"ok": True}


@router.delete("/api/trash/cards/{card_id}")
def purge_trashed_card(card_id: int):
    database.purge_card(card_id)
    return {"ok": True}


@router.delete("/api/trash/{deck_id}/cards")
def purge_all_cards_from_trash_deck(deck_id: int):
    count = database.purge_all_cards_from_deck(deck_id)
    return {"ok": True, "deleted": count}


@router.delete("/api/trash/{deck_id}/cards/{card_id}")
def purge_card_from_trash_deck(deck_id: int, card_id: int):
    database.purge_card_from_deck(card_id)
    return {"ok": True}


@router.delete("/api/trash")
def empty_trash():
    count = database.purge_all_trash()
    return {"ok": True, "deleted": count}


class DeckUpdate(BaseModel):
    name: str | None = None

@router.put("/api/decks/{deck_id}")
def update_deck(deck_id: int, body: DeckUpdate):
    if body.name:
        database.rename_deck(deck_id, body.name)
    return database.get_deck(deck_id)


# ---------------------------------------------------------------------------
# Preset routes
# ---------------------------------------------------------------------------

@router.get("/api/presets")
def list_presets():
    return database.list_presets()


@router.post("/api/presets")
def create_preset(name: str, clone_from_id: int | None = None):
    src = database.get_preset(clone_from_id) if clone_from_id else database.default_preset()
    src["name"] = name
    src.pop("id", None)
    src.pop("is_default", None)
    src.pop("deck_count", None)
    return database.get_preset(database.insert_preset(src))


@router.delete("/api/presets/{preset_id}")
def delete_preset(preset_id: int):
    try:
        database.delete_preset(preset_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/api/decks/{deck_id}/preset")
def get_deck_preset(deck_id: int, category: str | None = None, lang: str | None = None):
    preset = database.get_preset_for_deck(deck_id, category)
    preset["category_overrides"] = database.get_category_overrides(preset["id"])
    switch_preset = _lang_switch_preset(deck_id, lang)
    if switch_preset:
        preset.update({k: switch_preset.get(k) for k in _CATEGORY_SWITCHES})
    return preset


@router.put("/api/decks/{deck_id}/preset")
def update_deck_preset(deck_id: int, fields: dict, lang: str | None = None):
    deck = database.get_deck(deck_id)
    switch_preset = _lang_switch_preset(deck_id, lang, create=True)
    if switch_preset:
        switches = {k: fields.pop(k) for k in _CATEGORY_SWITCHES if k in fields}
        if switches:
            database.update_preset(switch_preset["id"], switches)
    database.update_preset(deck["preset_id"], fields)
    queue_mgr.invalidate()
    result = database.get_preset(deck["preset_id"])
    if switch_preset:
        result.update({k: database.get_preset(switch_preset["id"]).get(k)
                       for k in _CATEGORY_SWITCHES})
    return result


@router.put("/api/decks/{deck_id}/preset/assign")
def assign_preset_to_deck(deck_id: int, preset_id: int):
    database.assign_preset_to_deck(deck_id, preset_id)
    return database.get_preset(preset_id)


@router.post("/api/decks/{deck_id}/preset/toggle-bury")
def toggle_bury_siblings(deck_id: int):
    deck = database.get_deck(deck_id)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    current = deck.get("bury_quick_mode", "all")
    # Cycle: all → none → custom → all
    next_mode = {"all": "none", "none": "custom", "custom": "all"}.get(current, "all")
    database.set_deck_bury_quick_mode(deck_id, next_mode)
    return {"bury_mode": next_mode}


@router.post("/api/decks/{deck_id}/preset/toggle-mix")
def toggle_new_review_order(deck_id: int):
    deck = database.get_deck(deck_id)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    current = deck.get("new_review_order_override")
    cycle = {None: "mixed", "mixed": "reviews_first", "reviews_first": "new_first", "new_first": None}
    next_order = cycle.get(current, "mixed")
    database.set_deck_new_review_order_override(deck_id, next_order)
    return {"new_review_order_override": next_order}


@router.post("/api/decks/{deck_id}/preset/set-default")
def set_deck_preset_as_default(deck_id: int):
    deck = database.get_deck(deck_id)
    database.set_default_preset(deck["preset_id"])
    return database.get_preset(deck["preset_id"])


# ---------------------------------------------------------------------------
# Category-level scheduling overrides
# ---------------------------------------------------------------------------

VALID_CATEGORIES = {"listening", "reading", "creating"}


@router.get("/api/presets/{preset_id}/categories")
def get_preset_category_overrides(preset_id: int):
    return database.get_category_overrides(preset_id)


@router.get("/api/presets/{preset_id}/categories/{category}")
def get_preset_category_override(preset_id: int, category: str):
    if category not in VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail="Invalid category")
    overrides = database.get_category_overrides(preset_id)
    return overrides.get(category, {})


@router.put("/api/presets/{preset_id}/categories/{category}")
def set_preset_category_override(preset_id: int, category: str, fields: dict):
    if category not in VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail="Invalid category")
    database.set_category_override(preset_id, category, fields)
    queue_mgr.invalidate()
    overrides = database.get_category_overrides(preset_id)
    return overrides.get(category, {})


@router.delete("/api/presets/{preset_id}/categories/{category}")
def delete_preset_category_override(preset_id: int, category: str):
    if category not in VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail="Invalid category")
    database.delete_category_override(preset_id, category)
    queue_mgr.invalidate()
    return {"ok": True}
