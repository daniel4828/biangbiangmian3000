import json as _json
import logging

import ai
import database
import importer
from fastapi import APIRouter, HTTPException
from pypinyin import pinyin as _pinyin, Style
from .utils import queue_mgr as _queue_mgr

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/import")
def trigger_import():
    return importer.import_all("imports")


@router.get("/api/word/{word_id}")
def get_word_detail(word_id: int):
    word = database.get_word_full(word_id)
    if word:
        word["cards"] = database.get_cards_for_word(word_id)
    return word


@router.delete("/api/word/{word_id}")
def delete_word_endpoint(word_id: int):
    """Hard-delete one entry and everything hanging off it (cards, examples,
    forms, characters — all ON DELETE CASCADE), for Browse's per-row 🗑 button
    (#815).

    Unlike a card's soft delete this does not go through the trash: the entry
    itself is removed, which is what "delete this word" means in Browse. A
    missing id is a 404, never a cheerful {"ok": true} for a no-op.
    """
    if not database.get_word(word_id):
        raise HTTPException(status_code=404, detail="Word not found")
    database.delete_word(word_id)
    _queue_mgr.invalidate()
    return {"ok": True, "deleted": word_id}


@router.put("/api/word/{word_id}")
def update_word(word_id: int, body: dict):
    database.update_word(word_id, body)
    return database.get_word_full(word_id)


def _apply_enrich_result(word: dict, characters: list, result: dict) -> None:
    """Apply ai.enrich_word result to a single word and its characters."""
    if result.get("hsk_level") and not word.get("hsk_level"):
        database.update_word(word["id"], {"hsk_level": result["hsk_level"]})
    char_map = {c["char"]: c for c in characters}
    for char_data in result.get("characters", []):
        ch = char_data.get("char")
        if not ch or ch not in char_map:
            continue
        existing = char_map[ch]
        updates = {}
        if char_data.get("etymology") and not existing.get("etymology"):
            updates["etymology"] = char_data["etymology"]
        if char_data.get("other_meanings") and not existing.get("other_meanings"):
            meanings = char_data["other_meanings"]
            if isinstance(meanings, list):
                updates["other_meanings"] = _json.dumps(meanings, ensure_ascii=False)
        if updates:
            database.update_character(existing["char_id"], updates)


def _enrich_chars_with_id(char_results: list, db_chars: list, output: list) -> None:
    """Match AI char results to DB chars by character string, attach char_id, append to output.

    If a character isn't in entry_characters for this word, fall back to looking it up
    directly in the characters table (handles expressions with no entry_characters rows).
    """
    char_map = {c["char"]: c for c in db_chars}
    for char_data in char_results:
        ch = char_data.get("char")
        if not ch:
            continue
        if ch in char_map:
            char_data["char_id"] = char_map[ch]["char_id"]
        else:
            # Fallback: look up by character string in the characters table
            rec = database.get_character(ch)
            if rec:
                char_data["char_id"] = rec["id"]
            else:
                char_data["char_id"] = None  # preview-only, won't be saved
        output.append(char_data)


_TOP_FIELDS = {"notes", "examples", "definition", "definition_zh", "definition_de", "definition_fr", "pos"}
_CHAR_FIELDS = {"etymology", "compounds", "other_meanings"}


def _run_regen_ai(word_id: int, word: dict, fields: list) -> tuple[dict, list]:
    """Run AI field regeneration and return (top_result, all_characters_enriched).

    top_result  — notes/examples/definition/pos for the top-level entry.
    all_characters — list of {char, char_id, etymology?, compounds?} across all components.
    """
    want_char_data = "etymology" in fields or "compounds" in fields
    components = database.get_note_components(word_id)
    top_result: dict = {}
    all_characters: list = []

    if components:
        top_fields = [f for f in fields if f in _TOP_FIELDS]
        if top_fields:
            chars = database.get_word_characters(word_id)
            top_result = ai.regenerate_entry_fields(word, chars, top_fields)

        if want_char_data:
            comp_fields = [f for f in fields if f in ("etymology", "compounds")]
            for comp in components:
                comp_chars = database.get_word_characters(comp["id"])
                if not comp_chars:
                    # No chars linked yet — infer from the component's word_zh
                    seen: set = set()
                    for ch in comp.get("word_zh", ""):
                        if ch in seen:
                            continue
                        seen.add(ch)
                        basic = database.get_character(ch)
                        if basic:
                            full = database.get_character_by_id(basic["id"])
                            if full:
                                comp_chars.append({
                                    "char_id": full["id"], "char": ch,
                                    "pinyin": full.get("pinyin", ""),
                                    "hsk_level": full.get("hsk_level", ""),
                                    "etymology": full.get("etymology", ""),
                                    "compounds": full.get("compounds", []),
                                })
                                continue
                        comp_chars.append({"char_id": None, "char": ch,
                                           "pinyin": "", "hsk_level": "", "etymology": "", "compounds": []})
                result = ai.regenerate_entry_fields(comp, comp_chars, comp_fields)
                _enrich_chars_with_id(result.get("characters", []), comp_chars, all_characters)
    else:
        characters = database.get_word_characters(word_id)
        if want_char_data:
            # Supplement with any characters from word_zh not yet in entry_characters
            existing_in_db = {c["char"] for c in characters}
            for ch in word.get("word_zh", ""):
                if ch not in existing_in_db:
                    basic = database.get_character(ch)
                    full = database.get_character_by_id(basic["id"]) if basic else None
                    if full:
                        characters.append({
                            "char_id": full["id"],
                            "char": ch,
                            "pinyin": full.get("pinyin", ""),
                            "hsk_level": full.get("hsk_level", ""),
                            "etymology": full.get("etymology", ""),
                            "compounds": full.get("compounds", []),
                        })
                    else:
                        characters.append({"char_id": None, "char": ch,
                                           "pinyin": "", "hsk_level": "", "etymology": "", "compounds": []})
                    existing_in_db.add(ch)
        top_result = ai.regenerate_entry_fields(word, characters, fields)
        _enrich_chars_with_id(top_result.get("characters", []), characters, all_characters)

    print(f"[REGEN] FINAL all_characters count={len(all_characters)} chars={[c.get('char') for c in all_characters]}", flush=True)
    return top_result, all_characters


def _save_regen_result(word_id: int, fields: list, top_result: dict, all_characters: list) -> None:
    """Persist regen result to the database."""
    def_updates = {f: top_result[f] for f in ("definition", "definition_zh", "definition_de", "definition_fr", "pos")
                   if f in fields and top_result.get(f)}
    if def_updates:
        database.update_word(word_id, def_updates)

    if "notes" in fields and top_result.get("notes"):
        database.update_word(word_id, {"notes": top_result["notes"]})

    if "examples" in fields and top_result.get("examples"):
        database.delete_word_examples(word_id)
        for i, ex in enumerate(top_result["examples"]):
            if ex.get("zh"):
                database.insert_word_example(
                    word_id,
                    ex["zh"],
                    ex.get("pinyin"),
                    ex.get("english"),
                    ex.get("de"),
                    position=i,
                )

    # Build a map from individual character → component word_id.
    # For a component like '田园', both '田' and '园' map to that component's word_id.
    # Falls back to the parent word_id for characters with no matching component.
    components = database.get_note_components(word_id)
    comp_word_id_by_char: dict[str, int] = {}
    for comp in components:
        for ch in comp["word_zh"]:
            comp_word_id_by_char.setdefault(ch, comp["id"])
    # Cache existing chars per target word to avoid redundant DB reads
    existing_by_word: dict[int, set] = {}

    for i, char_data in enumerate(all_characters):
        char_id = char_data.get("char_id")
        ch = char_data.get("char", "")
        if not char_id:
            if ch:
                rec = database.get_character(ch)
                if rec:
                    char_id = rec["id"]
                elif ch:
                    char_id = database.upsert_character({
                        "char": ch, "traditional": None, "pinyin": None,
                        "hsk_level": None, "etymology": None, "other_meanings": None,
                    })
        if not char_id:
            continue
        target_word_id = comp_word_id_by_char.get(ch, word_id)
        if target_word_id not in existing_by_word:
            existing_by_word[target_word_id] = {
                c["char_id"] for c in database.get_word_characters(target_word_id)
            }
        if char_id not in existing_by_word[target_word_id]:
            database.insert_word_character(target_word_id, char_id, i, None)
            existing_by_word[target_word_id].add(char_id)
        if "etymology" in fields and char_data.get("etymology"):
            database.update_character(char_id, {"etymology": char_data["etymology"]})
        if "compounds" in fields and char_data.get("compounds"):
            database.upsert_character_compounds(char_id, char_data["compounds"])
        if "other_meanings" in fields and char_data.get("other_meanings"):
            meanings = char_data["other_meanings"]
            if isinstance(meanings, list):
                database.update_character(char_id, {"other_meanings": _json.dumps(meanings, ensure_ascii=False)})


@router.post("/api/word/{word_id}/regenerate-fields")
def regenerate_word_fields(word_id: int, body: dict):
    """Regenerate one or more fields for a vocabulary entry using AI.

    body: {"fields": [...], "preview": false}
    When preview=true, returns raw AI result without saving (for the frontend preview modal).
    fields: subset of ["notes", "examples", "etymology", "compounds"]
    """
    fields = body.get("fields", [])
    preview = body.get("preview", False)
    valid = _TOP_FIELDS | _CHAR_FIELDS
    fields = [f for f in fields if f in valid]
    if not fields:
        raise HTTPException(status_code=400, detail=f"fields must be a non-empty subset of {sorted(valid)}")

    word = database.get_word(word_id)
    if not word:
        raise HTTPException(status_code=404, detail="Word not found")

    top_result, all_characters = _run_regen_ai(word_id, word, fields)

    if preview:
        aggregated: dict = {}
        for f in ("pos", "definition", "definition_zh", "definition_de", "definition_fr", "notes", "examples"):
            if top_result.get(f):
                aggregated[f] = top_result[f]
        if all_characters:
            aggregated["characters"] = all_characters
        return {"fields": fields, "result": aggregated}

    _save_regen_result(word_id, fields, top_result, all_characters)
    return database.get_word_full(word_id)


@router.post("/api/word/{word_id}/apply-regen-result")
def apply_regen_result(word_id: int, body: dict):
    """Apply a (possibly user-edited) AI regen result returned by preview mode."""
    fields = body.get("fields", [])
    result = body.get("result", {})
    valid = _TOP_FIELDS | _CHAR_FIELDS
    fields = [f for f in fields if f in valid]
    if not fields:
        raise HTTPException(status_code=400, detail="fields required")

    word = database.get_word(word_id)
    if not word:
        raise HTTPException(status_code=404, detail="Word not found")

    top_keys = ("pos", "definition", "definition_zh", "definition_de", "definition_fr", "notes", "examples")
    top_result = {k: result[k] for k in top_keys if k in result}
    all_characters = result.get("characters", [])
    _save_regen_result(word_id, fields, top_result, all_characters)
    return database.get_word_full(word_id)


@router.post("/api/word/{word_id}/ai-enrich")
def ai_enrich_word(word_id: int):
    word = database.get_word(word_id)
    if not word:
        raise HTTPException(status_code=404, detail="Word not found")

    components = database.get_note_components(word_id)
    if components:
        # Multi-word card (chengyu/sentence/expression): enrich each component separately,
        # because characters are linked to component words, not the top-level note.
        for comp in components:
            comp_chars = database.get_word_characters(comp["id"])
            result = ai.enrich_word(comp, comp_chars)
            _apply_enrich_result(comp, comp_chars, result)
    else:
        characters = database.get_word_characters(word_id)
        result = ai.enrich_word(word, characters)
        _apply_enrich_result(word, characters, result)

    return database.get_word_full(word_id)


@router.get("/api/browse-words")
def browse_words(lang: str | None = None):
    return database.get_words_for_browse(lang)


@router.get("/api/vocab-index")
def vocab_index(lang: str = "zh"):
    """Lightweight word list for `lang` — just the forms, no definitions or card
    state (#850). Used by the listening-hint slider to test "is this word in my
    deck", which needs nothing else. Deliberately not a wrapper around
    /api/browse-words: that payload is several MB and would defeat the point.
    """
    return {"words": database.get_vocab_index(lang)}


@router.get("/api/words/random")
def random_word(exclude: str = ""):
    word = database.get_random_word(exclude)
    return {"word": word}


@router.get("/api/random-words")
def random_words(n: int = 10):
    """Random lexical entries for the daily 'random words' popup (no SRS)."""
    n = max(1, min(n, 50))
    return {"words": database.get_random_words(n)}


@router.get("/api/hanzi")
def get_all_hanzi():
    return database.get_all_characters()


@router.get("/api/hanzi/{char_id}")
def get_hanzi(char_id: int):
    char = database.get_character_by_id(char_id)
    if char:
        char["words"] = database.get_words_for_character(char_id)
    return char


@router.post("/api/hanzi/{char_id}/regenerate")
def regenerate_hanzi_info(char_id: int):
    char = database.get_character_by_id(char_id)
    if not char:
        raise HTTPException(status_code=404, detail="Character not found")
    result = ai.generate_character_info(char["char"], char.get("pinyin") or "")
    updates = {}
    if result["etymology"]:
        updates["etymology"] = result["etymology"]
    if result["translation"]:
        existing = []
        try:
            existing = _json.loads(char.get("other_meanings") or "[]")
        except (ValueError, TypeError):
            existing = []
        merged = [result["translation"]] + [m for m in existing if m != result["translation"]]
        updates["other_meanings"] = _json.dumps(merged, ensure_ascii=False)
    if updates:
        database.update_character(char_id, updates)
    updated = database.get_character_by_id(char_id)
    updated["words"] = database.get_words_for_character(char_id)
    return updated


@router.put("/api/hanzi/{char_id}")
def update_hanzi(char_id: int, body: dict):
    # compounds is handled via character_compounds table, not the JSON column
    compounds_raw = body.pop("compounds", None)
    if body:
        database.update_character(char_id, body)
    if compounds_raw is not None:
        try:
            compounds_list = _json.loads(compounds_raw) if isinstance(compounds_raw, str) else compounds_raw
            if isinstance(compounds_list, list):
                conn = database.get_db()
                conn.execute("DELETE FROM character_compounds WHERE char_id = ?", (char_id,))
                conn.commit()
                conn.close()
                database.upsert_character_compounds(char_id, compounds_list)
        except Exception:
            pass
    char = database.get_character_by_id(char_id)
    char["words"] = database.get_words_for_character(char_id)
    return char


@router.get("/api/search-words")
def search_words(q: str, lang: str | None = None):
    return database.search_words(q, lang)


@router.get("/api/words/{word_id}/cards")
def get_word_cards(word_id: int):
    return database.get_cards_for_word(word_id)


@router.post("/api/cards/{card_id}/suspend")
def toggle_suspend(card_id: int):
    return database.toggle_card_suspension(card_id)


@router.post("/api/cards/{card_id}/leech")
def leech_card_endpoint(card_id: int):
    """Manually flag a card as a leech (suspend + is_leech=1)."""
    database.mark_leech_suspend(card_id)
    return database.get_card(card_id)


@router.post("/api/cards/{card_id}/unleech")
def unleech_card_endpoint(card_id: int):
    """Clear a card's leech flag and restore it from the leech suspension."""
    card = database.clear_leech(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    _queue_mgr.invalidate()
    return card


@router.post("/api/cards/bulk-unleech")
def bulk_unleech(body: dict):
    count = database.bulk_clear_leech_by_words(body.get("word_ids", []))
    _queue_mgr.invalidate()
    return {"ok": True, "count": count}


@router.post("/api/cards/{card_id}/reset")
def reset_card_endpoint(card_id: int):
    return database.reset_card(card_id)


@router.post("/api/cards/{card_id}/bury")
def bury_card_endpoint(card_id: int):
    result = database.bury_card_until_tomorrow(card_id)
    _queue_mgr.invalidate()
    return result


@router.delete("/api/cards/{card_id}")
def delete_card_endpoint(card_id: int):
    card = database.get_card(card_id)
    if card:
        database.delete_word(card["word_id"])
    _queue_mgr.invalidate()
    return {"ok": True}


@router.post("/api/cards/bulk-bury")
def bulk_bury(body: dict):
    count = database.bulk_bury_cards_by_words(body.get("word_ids", []))
    _queue_mgr.invalidate()
    return {"ok": True, "count": count}


@router.post("/api/cards/bulk-suspend")
def bulk_suspend(body: dict):
    count = database.bulk_suspend_cards_by_words(body.get("word_ids", []))
    _queue_mgr.invalidate()
    return {"ok": True, "count": count}


@router.post("/api/cards/bulk-delete")
def bulk_delete(body: dict):
    count = database.bulk_delete_cards_by_words(body.get("word_ids", []))
    _queue_mgr.invalidate()
    return {"ok": True, "count": count}


@router.post("/api/cards/{card_id}/move")
def move_card(card_id: int, body: dict):
    deck_id = body.get("deck_id")
    if not deck_id:
        raise HTTPException(status_code=400, detail="deck_id required")
    ok = database.move_card_to_deck(card_id, deck_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Card not found")
    return {"ok": True}


@router.post("/api/cards/bulk-move")
def bulk_move(body: dict):
    deck_id = body.get("deck_id")
    if not deck_id:
        raise HTTPException(status_code=400, detail="deck_id required")
    count = database.bulk_move_cards_by_words(body.get("word_ids", []), deck_id)
    return {"ok": True, "count": count}


@router.post("/api/entries/{entry_id}/add-to-deck")
def add_entry_to_deck(entry_id: int, body: dict):
    deck_id = body.get("deck_id")
    if not deck_id:
        raise HTTPException(status_code=400, detail="deck_id required")
    result = database.add_entry_to_deck(entry_id, deck_id)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/api/browse")
def browse(deck_id: int | None = None, category: str | None = None,
           state: str | None = None, q: str | None = None, lang: str | None = None):
    return database.get_all_cards_for_browse({
        "deck_id": deck_id,
        "category": category,
        "lang": lang,
        "state": state,
        "search_text": q,
    })


@router.get("/api/stats")
def get_stats(deck_id: int | None = None, lang: str | None = None):
    return database.get_stats(deck_id, lang=lang)


@router.get("/api/retention")
def get_retention(days: int = 30, lang: str | None = None):
    return database.get_retention_bulk(days, lang=lang)


@router.get("/api/calendar-stats")
def get_calendar_stats(days: int = 365, deck_id: int | None = None):
    return database.get_calendar_stats(days, deck_id)


@router.get("/api/card-evolution")
def get_card_evolution(days: int = 365, deck_id: int | None = None, lang: str | None = None):
    return database.get_card_evolution(days, deck_id, lang=lang)


@router.get("/api/costs")
def get_api_costs(limit: int = 100):
    return {**database.get_api_costs(limit=limit), "balances": ai.get_provider_balances()}


@router.get("/api/costs/call/{call_id}")
def get_api_cost_call_details(call_id: int):
    details = database.get_api_call_details(call_id)
    if details is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return {"id": call_id, **details}


@router.get("/api/pinyin")
def get_pinyin(text: str):
    syllables = [item[0] for item in _pinyin(text, style=Style.TONE)]
    return {"syllables": syllables}
