"""Irregular-verb form cards: plan, migration and creation for new verbs (#1539).

A verb gets one form card ("traer · Presente") for every tense in which it has
an irregular form, judged by pronunciation (verb_slots.irregular_card_plan()).
Tenses: Presente, Indefinido, Imperfecto, Futuro, Imperativo, Participio,
Gerundio. The card rotates over the irregular persons only.

New cards inherit their DECK and DUE DATE from a card the learner already has
(the earliest old form card of the verb, else the verb's own creating card), so
a new card is not suddenly due "today" in a deck of its own. Cards parked in
the Saved deck / suspended stay parked.

Old cards are never hard-deleted: review_log has ON DELETE CASCADE, so the
card is only soft-deleted (deleted_at) and the entry stays.

All database access goes through the database package.
"""
import json
import logging

import database
import lexicon
import verb_slots

logger = logging.getLogger(__name__)

FORM_SOURCE = "irregular-forms"
_LANG = "es"
# Old tense codes that continue as another one: the old Perfecto card becomes
# the Participio card (the participle stands for the perfect).
_TENSE_SUCCESSOR = {"perf": "part"}


def _gate_marker() -> set[int]:
    """Card ids the tense-of-the-day gate paused (database.FORM_PAUSE_KEY)."""
    try:
        raw = database.get_app_setting(database.FORM_PAUSE_KEY)
        return {int(i) for i in json.loads(raw)} if raw else set()
    except (ValueError, TypeError):
        logger.warning("form card pause marker is broken, ignoring it")
        return set()


def _day(due) -> str:
    return (due or "")[:10]


def _is_parked(card: dict, marker: set[int]) -> bool:
    """A card the learner keeps out of the queues on purpose: in the Saved deck
    or suspended. A card paused only by the tense gate does not count — the
    gate decides again for the new card."""
    if card.get("deck_name") == "Saved":
        return True
    return card.get("state") == "suspended" and card.get("card_id") not in marker


def _brief(row: dict) -> dict:
    return {"word_id": row["word_id"], "word_zh": row["word_zh"],
            "card_id": row["card_id"], "deck": row["deck_name"],
            "due": _day(row["due"]), "state": row["state"]}


def _create_form_card(item: dict, deck_id: int, due: str, state: str) -> int:
    """Insert the entry and its single creating card (no AI: headword, gloss
    and note come from the lexicon and verb_slots)."""
    info = lexicon.lemma_info(item["lemma"], _LANG) or {}
    entry_id = database.insert_word({
        "word_zh": item["word_zh"],
        "lang": _LANG,
        "note_type": "grammar",
        "pos": "Verbform",
        "definition": info.get("gloss_en") or "",
        "definition_de": info.get("gloss_de") or "",
        "notes": item["note_de"],
        "source": FORM_SOURCE,
    })
    database.insert_card(entry_id, "creating", deck_id, state=state, due=due)
    return entry_id


def plan_migration(today: str) -> dict:
    """Compare the form cards in the database with what irregular_card_plan()
    wants and return what to keep / rename / delete / create (JSON-serialisable;
    nothing is written). `today` (YYYY-MM-DD) is the due date of last resort."""
    verbs = {v["word_zh"]: v for v in database.es_verb_creating_cards()}
    forms = database.es_form_cards()
    marker = _gate_marker()

    old_by_lemma: dict[str, list[dict]] = {}
    plan = {"keep": [], "rename": [], "delete": [], "create": [], "skipped": []}
    for row in forms:
        split = verb_slots.split_form_card_word(row["word_zh"])
        if split is None:
            plan["skipped"].append({"word_zh": row["word_zh"],
                                    "reason": "headword is not a form card name"})
            continue
        old_by_lemma.setdefault(split[0], []).append({**row, "tense": split[1]})

    for lemma in sorted(set(verbs) | set(old_by_lemma)):
        desired = {p["tense"]: p for p in verb_slots.irregular_card_plan(lemma, _LANG)}
        olds = old_by_lemma.get(lemma, [])
        covered: set[str] = set()  # desired tenses that keep an existing card

        # exact tense first, so a "Participio" card wins over an old "Perfecto" one
        for row in sorted(olds, key=lambda r: r["tense"] in _TENSE_SUCCESSOR):
            tense = _TENSE_SUCCESSOR.get(row["tense"], row["tense"])
            want = desired.get(tense)
            if want is None or tense in covered:
                plan["delete"].append({"lemma": lemma, **_brief(row)})
                continue
            covered.add(tense)
            if tense == row["tense"]:
                plan["keep"].append({"lemma": lemma, **_brief(row),
                                     "note_de": want["note_de"]})
            else:
                plan["rename"].append({"lemma": lemma, **_brief(row),
                                       "new_word_zh": want["word_zh"],
                                       "note_de": want["note_de"]})

        missing = [t for t in desired if t not in covered]
        if not missing:
            continue
        refs = [r for r in olds if r["card_id"] is not None]
        reference = min(refs, key=lambda r: (_day(r["due"]), r["card_id"])) if refs else None
        if reference is None:
            verb = verbs.get(lemma)
            reference = verb if verb and verb["card_id"] is not None else None
        if reference is None:
            for t in missing:
                plan["skipped"].append({
                    "lemma": lemma, "word_zh": desired[t]["word_zh"],
                    "reason": "no old form card and no creating card of the verb to copy from"})
            continue
        state = "suspended" if _is_parked(reference, marker) else "new"
        for t in missing:
            want = desired[t]
            plan["create"].append({
                "lemma": lemma, "tense": t, "word_zh": want["word_zh"],
                "persons": want["persons"], "note_de": want["note_de"],
                "deck_id": reference["deck_id"], "deck": reference["deck_name"],
                "due": _day(reference["due"]) or today, "state": state,
                "reference": reference["word_zh"]})
    return plan


def apply_migration(plan: dict) -> dict:
    """Execute a plan from plan_migration(). Every item is attempted on its own;
    failures are collected in "errors" (never swallowed). Returns
    {"kept", "renamed", "deleted", "created", "skipped", "created_words": [str],
    "errors": [str]}."""
    counts = {"kept": 0, "renamed": 0, "deleted": 0, "created": 0,
              "skipped": len(plan.get("skipped", [])), "created_words": [], "errors": []}

    def attempt(label: str, action) -> bool:
        try:
            action()
            return True
        except Exception as e:
            logger.exception("irregular form card migration: %s failed: %s", label, e)
            counts["errors"].append(f"{label}: {e}")
            return False

    for it in plan.get("delete", []):
        if attempt(f"delete {it['word_zh']}",
                   lambda it=it: database.soft_delete_card(it["card_id"])):
            counts["deleted"] += 1
    for it in plan.get("rename", []):
        if attempt(f"rename {it['word_zh']}",
                   lambda it=it: database.rename_entry(
                       it["word_id"], it["new_word_zh"], it["note_de"])):
            counts["renamed"] += 1
    for it in plan.get("keep", []):
        if attempt(f"notes of {it['word_zh']}",
                   lambda it=it: database.update_word(it["word_id"], {"notes": it["note_de"]})):
            counts["kept"] += 1
    for it in plan.get("create", []):
        if database.get_word_by_zh(it["word_zh"], _LANG):
            # The entry exists but its card was deleted earlier: inserting a
            # card would be ignored (UNIQUE) — say so instead of pretending.
            counts["errors"].append(
                f"create {it['word_zh']}: entry exists without a live card")
            continue
        if attempt(f"create {it['word_zh']}",
                   lambda it=it: _create_form_card(it, it["deck_id"], it["due"], it["state"])):
            counts["created"] += 1
            counts["created_words"].append(it["word_zh"])
    return counts


def create_cards_for_verb(word_id: int) -> list[str]:
    """Create the missing form cards of a newly added Spanish verb, mirroring
    the verb's own creating card (same deck, same due date; suspended when that
    card is parked in Saved / suspended). Returns the created headwords. Never
    raises: failures are logged and what was created so far is returned."""
    created: list[str] = []
    try:
        entry = database.get_word(word_id)
        if not entry or (entry.get("lang") or "zh") != _LANG:
            return created
        if verb_slots.split_form_card_word(entry["word_zh"]):
            return created
        plan = verb_slots.irregular_card_plan(entry["word_zh"], _LANG)
        if not plan:
            return created
        card = database.live_creating_card(word_id)
        if not card:
            logger.warning("no creating card for verb %r — form cards not created",
                           entry["word_zh"])
            return created
        state = "suspended" if _is_parked(card, _gate_marker()) else "new"
        due = _day(card["due"]) or database.anki_today().isoformat()
        for item in plan:
            if database.get_word_by_zh(item["word_zh"], _LANG):
                continue
            try:
                _create_form_card(item, card["deck_id"], due, state)
                created.append(item["word_zh"])
            except Exception as e:
                logger.exception("form card %r failed: %s", item["word_zh"], e)
    except Exception as e:
        logger.exception("create_cards_for_verb(%s) failed: %s", word_id, e)
    return created


def promote_form_cards(lemma: str, leaf_creating_deck_id: int, saved_deck_id: int,
                       due: str) -> list[str]:
    """When a verb is promoted out of Saved, promote its form cards that are
    still parked there into the same `creating` leaf deck (fresh new cards).
    Returns the promoted headwords. Never raises."""
    promoted: list[str] = []
    try:
        lemma = (lemma or "").strip().lower()
        if not lemma or verb_slots.split_form_card_word(lemma):
            return promoted
        for row in database.es_form_cards():
            split = verb_slots.split_form_card_word(row["word_zh"])
            if not split or split[0] != lemma or row["deck_id"] != saved_deck_id:
                continue
            if database.promote_saved_word(
                    row["word_id"], {"creating": leaf_creating_deck_id}, saved_deck_id, due):
                promoted.append(row["word_zh"])
    except Exception as e:
        logger.exception("promote_form_cards(%r) failed: %s", lemma, e)
    return promoted
