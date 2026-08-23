import json
import logging
import os
import re

import yaml

import database
from languages import is_valid_lang
from yaml_fixer import fix_yaml_content

logger = logging.getLogger(__name__)

# Maps YAML entry `type` → DB `note_type`. Unknown types are skipped.
_VALID_REGISTERS = {
    'spoken', 'written', 'both',
    'spoken_colloquial', 'spoken_neutral', 'neutral',
    'formal_written', 'literary',
}

NOTE_TYPE_MAP = {
    "vocabulary": "vocabulary",
    "word":       "vocabulary",   # new canonical name for vocabulary
    "sentence":   "sentence",
    "chengyu":    "chengyu",
    "expression": "expression",
    "grammar":    "grammar",      # reference only — stored in grammar_points, no cards
}

_HANZI_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')

# Fields compared when detecting component word conflicts
_CONFLICT_FIELDS = ("pinyin", "definition", "traditional")


def _format_yaml_error(e: yaml.YAMLError, filename: str = None) -> dict:
    """Return a structured, human-readable YAML error dict.

    Keys: file, line, column, problem, context, tip
    """
    result: dict = {}
    if filename:
        result["file"] = filename
    if hasattr(e, "problem_mark") and e.problem_mark is not None:
        result["line"] = e.problem_mark.line + 1
        result["column"] = e.problem_mark.column + 1
    if hasattr(e, "problem") and e.problem:
        result["problem"] = e.problem
    if hasattr(e, "context") and e.context:
        ctx = e.context
        if hasattr(e, "context_mark") and e.context_mark is not None:
            ctx += f" (line {e.context_mark.line + 1})"
        result["context"] = ctx

    # Attach a helpful tip for the most common mistake: unescaped quotes
    raw = str(e)
    if "scalar" in raw or "found" in raw:
        result["tip"] = (
            "If a value contains double quotes, wrap the whole value in single quotes. "
            'Example — change:  meaning: "lump meat" (a dish) '
            "→ to:  meaning: '\"lump meat\" (a dish)'"
        )

    result["raw"] = raw
    return result


def import_all(imports_dir: str = "imports") -> dict:
    """Recursively scan imports/<Source>/<optional subdirs>/*.yaml."""
    total_imported = 0
    total_skipped = 0
    total_invalid = 0
    if not os.path.isdir(imports_dir):
        return {"imported": 0, "skipped_duplicate": 0, "skipped_invalid": 0}
    for source_dir in sorted(os.scandir(imports_dir), key=lambda e: e.name):
        if not source_dir.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(source_dir.path):
            dirnames.sort()
            for filename in sorted(f for f in filenames if f.endswith(".yaml")):
                filepath = os.path.join(dirpath, filename)
                rel = os.path.relpath(dirpath, imports_dir)
                deck_path = rel.replace("\\", "/").split("/")
                result = import_yaml_file(filepath, deck_path)
                total_imported += result["imported"]
                total_skipped += result["skipped_duplicate"]
                total_invalid += result["skipped_invalid"]
    return {"imported": total_imported, "skipped_duplicate": total_skipped,
            "skipped_invalid": total_invalid}


def import_kouyu_yaml(filepath: str) -> dict:
    """Kept for backwards compatibility."""
    return import_yaml_file(filepath, ["Kouyu"])


def import_yaml_file(filepath: str, deck_path: list[str]) -> dict:
    """Parse one YAML file. deck_path is the folder hierarchy."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            raw = f.read()
        data = yaml.safe_load(fix_yaml_content(raw))
    except yaml.YAMLError as e:
        logger.error("YAML parse error in %s: %s", filepath, e)
        err = _format_yaml_error(e, filename=os.path.basename(filepath))
        return {"imported": 0, "skipped_duplicate": 0, "skipped_invalid": 0, "yaml_error": err}

    lang = data.get("lang", "zh") if isinstance(data, dict) else "zh"
    if not is_valid_lang(lang):
        logger.warning("import_yaml_file %s: unknown lang %r, falling back to zh", filepath, lang)
        lang = "zh"

    # Root import folders live under 'All' (same semantics as get_or_create_deck_path)
    # so their cards show up in All-deck aggregation and the language tabs.
    parent_id = database.get_all_deck_id()
    for segment in deck_path:
        parent_id = database.get_or_create_deck(segment, parent_id=parent_id, lang=lang)

    leaf_parent = deck_path[-1]
    deck_ids = _make_leaf_decks(leaf_parent, parent_id)

    entries = _get_entries(data)
    source = deck_path[0].lower()
    return _import_entries(entries, deck_ids, source, label=os.path.basename(filepath), lang=lang)


def import_yaml_content(content: str, parent_deck_id: int,
                        resolutions: dict | None = None,
                        card_configs: dict | None = None,
                        custom_fields: dict | None = None,
                        due_offset_days: int = 0) -> dict:
    """Import YAML from a string into an existing parent deck.

    resolutions:   {word_zh: "keep"|"update"|"custom"} for component word conflicts.
    card_configs:  {word_zh: {include, deck_path, suspended, ai_fill}}
    custom_fields: {word_zh: {pinyin, definition, traditional}} merged values for "custom" resolutions.
    due_offset_days: shift every new card's due date by this many days (1 = tomorrow),
                   so a word can be staged for a future daily deck (#636).
    """
    try:
        data = yaml.safe_load(fix_yaml_content(content))
    except yaml.YAMLError as e:
        logger.error("YAML parse error in upload: %s", e)
        return {"imported": 0, "skipped_duplicate": 0, "skipped_invalid": 0,
                "yaml_error": _format_yaml_error(e)}

    lang = data.get("lang") if isinstance(data, dict) else None
    if lang and not is_valid_lang(lang):
        logger.warning("import_yaml_content: unknown lang %r, falling back to zh", lang)
        lang = "zh"
    if not lang:
        lang = database.get_deck_lang(parent_deck_id)

    parent = database.get_deck(parent_deck_id)
    leaf_parent = parent["name"] if parent else "Upload"
    default_deck_ids = _make_leaf_decks(leaf_parent, parent_deck_id)

    entries = _get_entries(data)
    source = leaf_parent.lower()
    return _import_entries(entries, default_deck_ids, source, label="<upload>",
                           resolutions=resolutions or {},
                           card_configs=card_configs or {},
                           custom_fields=custom_fields or {},
                           lang=lang,
                           due_offset_days=due_offset_days)


def preview_yaml_content(content: str) -> dict:
    """Parse YAML and return a preview + conflict list — no DB writes.

    Returns:
        {
          entries:   [{simplified, note_type, status, reason}],
          summary:   {ok, duplicate, invalid, unknown_type},
          conflicts: [{simplified, existing: {…}, incoming: {…}}]
        }
    """
    try:
        data = yaml.safe_load(fix_yaml_content(content))
    except yaml.YAMLError as e:
        return {
            "entries": [], "conflicts": [],
            "summary": {"ok": 0, "duplicate": 0, "invalid": 0, "unknown_type": 0},
            "error": str(e),
            "error_detail": _format_yaml_error(e),
        }

    preview_lang = data.get("lang") if isinstance(data, dict) else None
    if preview_lang and not is_valid_lang(preview_lang):
        preview_lang = "zh"

    entries = _get_entries(data)
    result_entries = []
    conflicts = []
    seen_conflicts = set()
    summary = {"ok": 0, "duplicate": 0, "invalid": 0, "unknown_type": 0}

    for entry in entries:
        if preview_lang and preview_lang != "zh":
            entry = _normalize_romance_entry(entry, preview_lang)

        yaml_type = entry.get("type", "")
        note_type = NOTE_TYPE_MAP.get(yaml_type)

        if note_type is None:
            summary["unknown_type"] += 1
            word_zh = entry.get("simplified", "").strip() or "(no simplified)"
            result_entries.append({
                "simplified": word_zh, "note_type": yaml_type or "(none)",
                "english": entry.get("english", ""),
                "hsk": str(entry.get("hsk", "") or ""),
                "status": "invalid", "reason": f"unknown type: {yaml_type!r}",
                "raw_yaml": yaml.dump(entry, allow_unicode=True, default_flow_style=False, sort_keys=False).strip(),
            })
            continue

        # Grammar entries: show as reference (no duplicate check needed)
        if note_type == "grammar":
            name = (entry.get("name") or "(no name)").strip()
            summary["ok"] += 1
            result_entries.append({
                "simplified": name, "note_type": "grammar",
                "english": entry.get("meaning", ""),
                "hsk": entry.get("level", ""),
                "status": "ok", "reason": None,
                "raw_yaml": yaml.dump(entry, allow_unicode=True, default_flow_style=False, sort_keys=False).strip(),
            })
            continue

        word_zh = entry.get("simplified", "").strip()
        if not word_zh:
            summary["invalid"] += 1
            result_entries.append({
                "simplified": "(empty)", "note_type": note_type,
                "english": "",
                "status": "invalid", "reason": "missing simplified field",
            })
            continue

        if not preview_lang or preview_lang == "zh":
            stripped = _strip_ellipsis(word_zh)
            if stripped != word_zh:
                logger.warning("STRIP preview: ellipsis stripped from %r → %r", word_zh, stripped)
                word_zh = stripped

        english = entry.get("english", "")
        hsk = str(entry.get("hsk", "") or "")
        warning = _validate_entry(word_zh, note_type)
        if warning:
            summary["invalid"] += 1
            result_entries.append({
                "simplified": word_zh, "note_type": note_type,
                "english": english, "hsk": hsk,
                "status": "invalid", "reason": warning,
                "raw_yaml": yaml.dump(entry, allow_unicode=True, default_flow_style=False, sort_keys=False).strip(),
            })
            continue

        raw = yaml.dump(entry, allow_unicode=True, default_flow_style=False, sort_keys=False).strip()
        existing = database.get_word_by_zh(word_zh)
        if existing and database.word_has_cards(existing["id"]):
            summary["duplicate"] += 1
            deck_names = database.get_word_deck_names(existing["id"])
            result_entries.append({
                "simplified": word_zh, "note_type": note_type,
                "english": english, "hsk": hsk,
                "status": "duplicate", "reason": None,
                "raw_yaml": raw,
                "current_decks": deck_names,
            })
        else:
            summary["ok"] += 1
            result_entries.append({
                "simplified": word_zh, "note_type": note_type,
                "english": english, "hsk": hsk,
                "status": "ok", "reason": None,
                "raw_yaml": raw,
            })

        # Check component word_analyses for conflicts (char_only entries never conflict)
        for analysis in (entry.get("word_analyses") or []):
            if analysis.get("char_only"):
                continue
            if analysis.get("type") not in NOTE_TYPE_MAP:
                continue
            comp_zh = analysis.get("simplified", "").strip()
            if not comp_zh or comp_zh in seen_conflicts:
                continue
            comp_existing = database.get_word_by_zh(comp_zh)
            if comp_existing:
                incoming = _build_word_dict(analysis, source="")
                conflict_fields = {
                    f: (comp_existing.get(f), incoming.get(f))
                    for f in _CONFLICT_FIELDS
                    if comp_existing.get(f) != incoming.get(f)
                }
                if conflict_fields:
                    seen_conflicts.add(comp_zh)
                    conflicts.append({
                        "simplified": comp_zh,
                        "existing": {f: comp_existing.get(f) for f in _CONFLICT_FIELDS},
                        "incoming": {f: incoming.get(f) for f in _CONFLICT_FIELDS},
                    })

    return {"entries": result_entries, "summary": summary, "conflicts": conflicts}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_entries(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("entries") or data.get("vocab") or data.get("vocabulary") or []
    return []


def _make_leaf_decks(leaf_parent: str, parent_id: int) -> dict:
    return {
        "listening": database.get_or_create_deck(
            f"{leaf_parent} · Listening", parent_id=parent_id, category="listening"
        ),
        "reading": database.get_or_create_deck(
            f"{leaf_parent} · Reading", parent_id=parent_id, category="reading"
        ),
        "creating": database.get_or_create_deck(
            f"{leaf_parent} · Creating", parent_id=parent_id, category="creating"
        ),
    }


def _strip_ellipsis(word_zh: str) -> str:
    # Replace internal Chinese ellipsis (……) with ASCII ... used by the SRS
    word_zh = word_zh.replace('……', '...').replace('…', '...')
    return word_zh.strip('.')


# CEFR level string → the shared 1-6 integer scale stored in entries.hsk_level
# (A1=1 … C2=6; languages.py `level_system` tells the frontend how to label it).
_CEFR_TO_INT = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6}


def _cefr_to_int(level) -> int | None:
    return _CEFR_TO_INT.get(str(level or "").strip().upper())


_VALID_GENDERS = {"m", "f", "mf"}


def _normalize_gender(raw) -> str | None:
    g = str(raw or "").strip().lower()
    return g if g in _VALID_GENDERS else None


def _normalize_romance_entry(entry: dict, lang: str = "fr") -> dict:
    """Reshape a Romance-language (fr/es) YAML entry into the internal
    (Chinese-era) key layout so all downstream processing (_build_word_dict,
    examples, etc.) stays untouched. Chinese-only modules (characters,
    measure words, word_analyses) are simply absent from this format.

    `lang` picks which example/similar_sentence key holds the target-language
    text ('fr' or 'es') — everything else about the format is shared between
    Romance languages (issue #805).
    """
    normalized = dict(entry)
    normalized["simplified"] = (
        entry.get("word") or entry.get("sentence")
        or entry.get("expression") or entry.get("simplified") or ""
    )
    normalized["examples"] = [
        {
            "zh":     ex.get(lang) or ex.get("fr", ""),
            "english": ex.get("english"),
            "de":     ex.get("german") or ex.get("de"),
            "pinyin": None,
        }
        for ex in (entry.get("examples") or [])
    ]
    # `level: B1` (CEFR string) → shared 1-6 integer level (issue #596)
    normalized["cefr_level"] = _cefr_to_int(entry.get("level"))
    # similar_sentences {fr/es, german} → internal {zh, de} keys (sentence entries)
    normalized["similar_sentences"] = [
        {
            "zh":      ss.get(lang) or ss.get("fr", ""),
            "english": ss.get("english"),
            "de":      ss.get("german") or ss.get("de"),
            "pinyin":  None,
        }
        for ss in (entry.get("similar_sentences") or [])
    ]
    # gender: m|f|mf (#805) — invalid/missing values fall back to None rather
    # than raising, same "don't fail the whole entry over an optional field"
    # posture as register in _build_word_dict.
    normalized["gender"] = _normalize_gender(entry.get("gender"))
    # Romance format doesn't define these Chinese-only fields
    for key in ("hsk", "traditional", "pinyin", "word_analyses",
                "characters", "measure_words"):
        normalized.pop(key, None)
    return normalized


# Backward-compatible alias — existing callers/tests import this name.
_normalize_fr_entry = _normalize_romance_entry


def _build_word_dict(entry: dict, source: str, note_type: str = "vocabulary",
                     lang: str = "zh") -> dict:
    register = entry.get("register")
    if register not in _VALID_REGISTERS:
        if register is not None:
            logger.warning("_build_word_dict: invalid register %r — set to None", register)
        register = None
    simplified = entry.get("simplified", "").strip()
    word_zh = _strip_ellipsis(simplified) if lang == "zh" else simplified
    return {
        "word_zh":         word_zh,
        "lang":            lang,
        "pinyin":          entry.get("pinyin"),
        "definition":      entry.get("english"),
        "definition_de":   entry.get("german"),
        "definition_fr":   entry.get("french"),
        "pos":             entry.get("pos"),
        # non-zh: CEFR level mapped to the same 1-6 scale (see _cefr_to_int)
        "hsk_level":       entry.get("cefr_level") if lang != "zh"
                           else _hsk_to_int(str(entry.get("hsk", ""))),
        "traditional":     entry.get("traditional"),
        "definition_zh":   entry.get("definition_zh"),
        "source":          source,
        "note_type":       note_type,
        "notes":           entry.get("note") or entry.get("explanations"),
        "date_yaml":       entry.get("date"),
        "source_sentence": entry.get("source_de"),
        "grammar_notes":   None,
        "register":        register,
        # Noun grammatical gender (French/Spanish; #803/#805) — normalized to
        # m|f|mf|None in _normalize_romance_entry; absent (zh) stays None.
        "gender":          entry.get("gender"),
        # Entry-level word origin (#906). Romance formats carry a top-level
        # `etymology:` block scalar; the Chinese format has no such key (its
        # etymology is per character, under `word_analyses`), so this stays None.
        "etymology":       entry.get("etymology") if lang != "zh" else None,
    }


def _process_characters(entry: dict, word_id: int) -> None:
    """Insert characters from an entry's `characters` list and link to word."""
    for pos, char_entry in enumerate(entry.get("characters") or []):
        char_text = char_entry.get("char", "").strip()
        if not char_text:
            continue
        detailed = char_entry.get("detailed_analysis", False)
        other_meanings = char_entry.get("other_meanings")
        compounds_raw  = char_entry.get("compounds")
        char_dict = {
            "char":           char_text,
            "traditional":    char_entry.get("traditional"),
            "pinyin":         char_entry.get("pinyin"),
            "hsk_level":      _hsk_to_int(str(char_entry.get("hsk", ""))),
            "etymology":      char_entry.get("etymology") if detailed else None,
            "other_meanings": json.dumps(other_meanings, ensure_ascii=False)
                              if other_meanings else None,
            # compounds: written to character_compounds table below, NOT as JSON
        }
        char_id = database.upsert_character(char_dict)
        database.insert_word_character(
            word_id=word_id,
            char_id=char_id,
            position=pos,
            meaning_in_context=char_entry.get("meaning_in_context") if detailed else None,
        )
        if compounds_raw and isinstance(compounds_raw, list):
            database.upsert_character_compounds(char_id, compounds_raw)


def _process_measure_words(entry: dict, word_id: int) -> None:
    """Insert measure words (量词) from an entry's `measure_word` list."""
    for pos, mw in enumerate(entry.get("measure_word") or []):
        measure_zh = (mw.get("simplified") or "").strip()
        if not measure_zh:
            continue
        database.insert_word_measure_word(
            word_id=word_id,
            measure_zh=measure_zh,
            pinyin=mw.get("pinyin"),
            meaning=mw.get("meaning"),
            position=pos,
        )


def _process_grammar_structures(entry: dict, word_id: int) -> None:
    """Insert grammar_structures from a sentence entry."""
    for pos, gs in enumerate(entry.get("grammar_structures") or []):
        structure = (gs.get("structure") or "").strip()
        if not structure:
            continue
        database.insert_grammar_structure(
            word_id=word_id,
            structure=structure,
            explanation=gs.get("explanation"),
            example_zh=gs.get("example"),
            position=pos,
        )


def _process_word_relations(entry: dict, word_id: int) -> None:
    """Insert synonyms and antonyms from an entry's `synonyms`/`antonyms` lists."""
    for rel_type, key in (("synonym", "synonyms"), ("antonym", "antonyms")):
        for item in (entry.get(key) or []):
            # Accept both `simplified:` (old) and `word:` (new format) as the hanzi field
            related_zh = (item.get("simplified") or item.get("word") or "").strip()
            if not related_zh:
                continue
            database.insert_word_relation(
                word_id=word_id,
                related_zh=related_zh,
                related_pinyin=item.get("pinyin"),
                related_de=item.get("de") or item.get("meaning"),
                relation_type=rel_type,
            )


def _process_conjugations(entry: dict, word_id: int) -> None:
    """Insert verb conjugations (issue #596) from an entry's `conjugations` mapping.

    YAML shape: {tense: {person: form, …}} for personal tenses, or
    {tense: "form"} for impersonal forms (participles, infinitive) — the person
    is stored as '' for those. Insertion order of the mapping is preserved via
    position so the UI shows tenses in the order the YAML defined them.
    """
    conjugations = entry.get("conjugations")
    if not isinstance(conjugations, dict):
        return
    position = 0
    for tense, forms in conjugations.items():
        tense = str(tense).strip()
        if not tense:
            continue
        if isinstance(forms, dict):
            pairs = [(str(p).strip(), str(f).strip()) for p, f in forms.items()]
        else:
            pairs = [("", str(forms).strip())]
        for person, form in pairs:
            if not form:
                continue
            database.insert_word_conjugation(
                word_id=word_id, tense=tense, person=person,
                form=form, position=position,
            )
            position += 1


def _process_forms(entry: dict, word_id: int) -> None:
    """Insert noun/adjective inflected forms (issue #805) from an entry's
    `forms` mapping into entry_forms with kind='inflection'.

    YAML shape mirrors `conjugations`: {dimension: {slot: form, ...}}, e.g.
    {"nombre": {"pluriel": "chats"}, "genre": {"féminin": "verte"}} — dimension
    -> entry_forms.paradigm, slot -> entry_forms.slot (see docs/multilang.md).
    A bare {dimension: "form"} value (single slot, no sub-mapping) is also
    accepted, stored with slot=''.
    """
    forms = entry.get("forms")
    if not isinstance(forms, dict):
        return
    position = 0
    for dimension, slots in forms.items():
        dimension = str(dimension).strip()
        if not dimension:
            continue
        if isinstance(slots, dict):
            pairs = [(str(s).strip(), str(f).strip()) for s, f in slots.items()]
        else:
            pairs = [("", str(slots).strip())]
        for slot, form in pairs:
            if not form:
                continue
            database.insert_word_form(
                word_id=word_id, kind="inflection", paradigm=dimension,
                slot=slot, form=form, position=position,
            )
            position += 1


def _import_grammar_entry(entry: dict, label: str) -> bool:
    """Store a grammar-type entry in grammar_points + sub-tables. Returns True on success."""
    name = (entry.get("name") or "").strip()
    if not name:
        logger.warning("SKIP %s: grammar entry missing 'name' field", label)
        return False

    grammar_id = database.insert_grammar_point(
        name=name,
        level=entry.get("level"),
        structure=entry.get("structure"),
        meaning=entry.get("meaning"),
        usage=entry.get("usage"),
        cultural_note=entry.get("cultural_note"),
    )

    for i, ex in enumerate(entry.get("examples") or []):
        zh = (ex.get("zh") or "").strip()
        if zh:
            database.insert_grammar_example(
                grammar_id=grammar_id,
                example_zh=zh,
                pinyin=ex.get("pinyin"),
                example_de=ex.get("de"),
                structure=ex.get("structure"),
                position=i,
            )

    for i, pat in enumerate(entry.get("common_patterns") or []):
        pattern = (pat.get("pattern") or "").strip()
        if pattern:
            database.insert_grammar_pattern(
                grammar_id=grammar_id,
                pattern=pattern,
                meaning=pat.get("meaning"),
                example=pat.get("example"),
                position=i,
            )

    for i, cmp in enumerate(entry.get("comparisons") or []):
        database.insert_grammar_comparison(
            grammar_id=grammar_id,
            title=cmp.get("title"),
            explanation=cmp.get("explanation"),
            position=i,
        )

    for i, expr in enumerate(entry.get("fixed_expressions") or []):
        expression = (expr.get("expression") or "").strip()
        if expression:
            database.insert_grammar_expression(
                grammar_id=grammar_id,
                expression=expression,
                meaning=expr.get("meaning"),
                position=i,
            )

    return True


def _process_char_only_component(analysis: dict, note_word_id: int,
                                 position: int, source: str) -> None:
    """Store a char_only word_analyses entry as a minimal word and link it."""
    char_text = analysis.get("char_only", "").strip()
    if not char_text:
        return
    comp_word = {
        "word_zh":         char_text,
        "pinyin":          analysis.get("pinyin"),
        "definition":      None,
        "pos":             None,
        "hsk_level":       _hsk_to_int(str(analysis.get("hsk", ""))),
        "traditional":     None,
        "definition_zh":   None,
        "source":          source,
        "note_type":       "vocabulary",
        "source_sentence": None,
        "grammar_notes":   None,
    }
    comp_word_id = database.insert_word(comp_word)
    database.insert_note_component(note_word_id, comp_word_id, position)


def _process_component(analysis: dict, note_word_id: int, position: int,
                       source: str, resolutions: dict,
                       custom_fields: dict | None = None) -> None:
    """Store a word_analyses component word and link it to its parent note."""
    comp_zh = analysis.get("simplified", "").strip()
    if not comp_zh:
        return

    comp_word = _build_word_dict(analysis, source=source, note_type="vocabulary")
    comp_word_id = database.insert_word(comp_word)  # INSERT OR IGNORE

    resolution = resolutions.get(comp_zh, "keep")
    if resolution == "update":
        database.update_word(comp_word_id, comp_word)
    elif resolution == "custom" and custom_fields and comp_zh in custom_fields:
        merged = {**comp_word, **(custom_fields[comp_zh] or {})}
        database.update_word(comp_word_id, merged)

    _process_characters(analysis, comp_word_id)
    _process_measure_words(analysis, comp_word_id)

    # Store examples if present
    for i, ex in enumerate(analysis.get("examples") or []):
        database.insert_word_example(
            word_id=comp_word_id,
            example_zh=ex.get("zh", ""),
            example_pinyin=ex.get("pinyin"),
            example_en=ex.get("english"),
            example_de=ex.get("de"),
            position=i,
        )

    database.insert_note_component(note_word_id, comp_word_id, position)


def _import_entries(entries: list, deck_ids: dict, source: str, label: str,
                    resolutions: dict | None = None,
                    card_configs: dict | None = None,
                    custom_fields: dict | None = None,
                    lang: str = "zh",
                    due_offset_days: int = 0) -> dict:
    if resolutions is None:
        resolutions = {}
    if card_configs is None:
        card_configs = {}
    if custom_fields is None:
        custom_fields = {}

    imported = 0
    skipped_duplicate = 0
    skipped_invalid = 0
    skipped_entries: list[dict] = []
    _deck_path_cache: dict[str, dict] = {}  # deck_path → leaf deck_ids

    for entry in entries:
      try:
        if lang != "zh":
            entry = _normalize_romance_entry(entry, lang)

        yaml_type = entry.get("type", "")
        note_type = NOTE_TYPE_MAP.get(yaml_type)

        if note_type is None:
            logger.debug("SKIP %s: unknown type %r", label, yaml_type)
            continue

        # Grammar entries go to grammar_points table, not entries — no cards created
        if note_type == "grammar":
            if _import_grammar_entry(entry, label):
                imported += 1
            else:
                skipped_invalid += 1
            continue

        word_zh = entry.get("simplified", "").strip()
        if not word_zh:
            skipped_invalid += 1
            continue

        if lang == "zh":
            stripped = _strip_ellipsis(word_zh)
            if stripped != word_zh:
                logger.warning("STRIP %s: ellipsis stripped from %r → %r", label, word_zh, stripped)
                word_zh = stripped

        warning = _validate_entry(word_zh, note_type)
        if warning:
            logger.warning("SKIP %s: %s", label, warning)
            skipped_invalid += 1
            skipped_entries.append({
                "word": word_zh, "reason": warning,
                "raw_yaml": yaml.dump(entry, allow_unicode=True, default_flow_style=False, sort_keys=False).strip(),
            })
            continue

        # Per-card config (frontend overrides)
        card_cfg = card_configs.get(word_zh, {})

        # Respect per-card include flag (defaults to True)
        if not card_cfg.get("include", True):
            logger.debug("SKIP %s: excluded by user config", word_zh)
            continue

        # YAML-level codeword fields (generated by de-zh-bot skill)
        yaml_categories = entry.get("categories")   # e.g. ["creating"] or ["creating", "listening"]
        yaml_deck_hint = entry.get("deck_hint")     # e.g. "B"

        # deck_hint from YAML takes priority over card_cfg.deck_path
        card_deck_path = yaml_deck_hint or card_cfg.get("deck_path")
        if card_deck_path:
            if card_deck_path not in _deck_path_cache:
                try:
                    pid = database.get_or_create_deck_path(card_deck_path)
                    parent = database.get_deck(pid)
                    leaf_name = parent["name"] if parent else card_deck_path.split("::")[-1]
                    _deck_path_cache[card_deck_path] = _make_leaf_decks(leaf_name, pid)
                except Exception as e:
                    logger.warning("deck_path %r failed (%s), using default", card_deck_path, e)
                    _deck_path_cache[card_deck_path] = deck_ids
            target_deck_ids = _deck_path_cache[card_deck_path]
        else:
            target_deck_ids = deck_ids

        word = _build_word_dict(entry, source=source, note_type=note_type, lang=lang)
        word_id = database.insert_word(word)  # INSERT OR IGNORE → always get id

        if database.word_has_cards(word_id):
            dup_action = card_cfg.get("duplicate_action", "skip")
            if dup_action == "reset":
                n = database.reset_card_progress(word_id)
                logger.info("RESET %s: %r — reset %d card(s)", label, word_zh, n)
                imported += 1
            elif dup_action == "move":
                move_target = card_cfg.get("move_target")
                move_cats = card_cfg.get("move_categories") or None
                if move_target:
                    try:
                        pid = database.get_or_create_deck_path(move_target)
                        parent = database.get_deck(pid)
                        leaf_name = parent["name"] if parent else move_target.split("::")[-1]
                        move_deck_ids = _make_leaf_decks(leaf_name, pid)
                    except Exception as e:
                        logger.warning("MOVE %s: %r — deck_path %r failed (%s), skipping",
                                       label, word_zh, move_target, e)
                        skipped_duplicate += 1
                        skipped_entries.append({"word": word_zh, "reason": f"move target failed: {e}"})
                        continue
                    n = database.move_cards_to_deck(word_id, move_deck_ids, move_cats)
                    logger.info("MOVE %s: %r → %r (cats=%r) — moved %d card(s)",
                                label, word_zh, move_target, move_cats, n)
                    imported += 1
                else:
                    logger.warning("MOVE %s: %r — no move_target specified, skipping", label, word_zh)
                    skipped_duplicate += 1
                    skipped_entries.append({"word": word_zh, "reason": "move_target missing"})
            else:
                skipped_duplicate += 1
                skipped_entries.append({"word": word_zh, "reason": "already in deck"})
            # Always process word_analyses (all types) so components stay linked (Chinese-only)
            if lang == "zh":
                for pos, analysis in enumerate(entry.get("word_analyses") or []):
                    if analysis.get("char_only"):
                        _process_char_only_component(analysis, word_id, pos, source)
                    elif analysis.get("type") in NOTE_TYPE_MAP:
                        _process_component(analysis, word_id, pos, source, resolutions, custom_fields)
            continue

        # Examples
        for i, ex in enumerate(entry.get("examples") or []):
            database.insert_word_example(
                word_id=word_id,
                example_zh=ex.get("zh", ""),
                example_pinyin=ex.get("pinyin"),
                example_en=ex.get("english"),
                example_de=ex.get("de"),
                position=i,
                example_type="example",
            )

        # Similar sentences (sentence entries only)
        if note_type == "sentence":
            for i, ss in enumerate(entry.get("similar_sentences") or []):
                zh = (ss.get("zh") or "").strip()
                if not zh:
                    continue
                database.insert_word_example(
                    word_id=word_id,
                    example_zh=zh,
                    example_pinyin=ss.get("pinyin"),
                    example_en=ss.get("english"),
                    example_de=ss.get("de"),
                    position=i,
                    example_type="similar",
                )

        # Synonyms / antonyms — language-neutral (fr synonyms use the same
        # word/meaning keys, issue #596)
        _process_word_relations(entry, word_id)

        # Verb conjugations (fr/es and future conjugating languages, issue #596)
        _process_conjugations(entry, word_id)

        # Noun/adjective inflected forms — plural, gender agreement (issue #805)
        _process_forms(entry, word_id)

        # The following processors handle Chinese-only YAML fields (characters,
        # measure words, grammar structures, word_analyses components).
        if lang == "zh":
            # Legacy: top-level `characters:` on vocabulary entries.
            # New format uses `word_analyses:` for all entry types (handled below).
            if note_type == "vocabulary":
                _process_characters(entry, word_id)

            # Measure words (量词)
            _process_measure_words(entry, word_id)

            # Grammar structures (sentence entries)
            if note_type == "sentence":
                _process_grammar_structures(entry, word_id)

            # Component word_analyses (sentences / chengyu / expressions)
            for pos, analysis in enumerate(entry.get("word_analyses") or []):
                if analysis.get("char_only"):
                    _process_char_only_component(analysis, word_id, pos, source)
                elif analysis.get("type") in NOTE_TYPE_MAP:
                    _process_component(analysis, word_id, pos, source, resolutions)

        # categories field from YAML overrides card_cfg.suspended (frontend has final say)
        if yaml_categories and not card_cfg.get("suspended"):
            all_cats = ("reading", "listening", "creating")
            active = set(yaml_categories)
            suspended_states = {cat: cat not in active for cat in all_cats}
            logger.info("CATEGORIES %s: %r active=%r", label, word_zh, list(active))
        else:
            suspended_states = card_cfg.get("suspended") or None
        _create_cards(word_id, target_deck_ids, suspended_states, word_zh=word_zh,
                      due_offset_days=due_offset_days)
        imported += 1

      except Exception as _entry_exc:
        _entry_word = entry.get("simplified") or entry.get("word_zh") or repr(entry)[:60]
        logger.exception("ENTRY_ERROR %s: %r — %s", label, _entry_word, _entry_exc)
        skipped_invalid += 1
        skipped_entries.append({"word": str(_entry_word), "reason": str(_entry_exc)})

    return {"imported": imported, "skipped_duplicate": skipped_duplicate,
            "skipped_invalid": skipped_invalid, "skipped_entries": skipped_entries}


def _validate_entry(word_zh: str, note_type: str) -> str | None:
    """Return a warning string if the entry is invalid, else None."""
    if note_type == "sentence":
        if '/' in word_zh or '／' in word_zh:
            return f"slash in sentence: {word_zh!r}"
        return None
    # vocabulary / chengyu: strict rules
    if '/' in word_zh or '／' in word_zh:
        return f"slash in word (multiple entries combined): {word_zh!r}"
    if '。' in word_zh or '. ' in word_zh:
        return f"period in word (looks like a sentence): {word_zh!r}"
    return None


# Default per-category suspension: listening/creating active, reading suspended
_DEFAULT_SUSPENDED: dict[str, bool] = {
    "reading": True,
    "listening": False,
    "creating": False,
}


_CATEGORY_DUE_OFFSET: dict[str, int] = {
    "reading": 0,
    "listening": 0,
    "creating": 0,
}


def _create_cards(word_id: int, deck_ids: dict,
                  suspended_states: dict[str, bool] | None = None,
                  word_zh: str = "", due_offset_days: int = 0) -> None:
    if suspended_states is None:
        suspended_states = _DEFAULT_SUSPENDED
    today = database.anki_today()
    from datetime import timedelta
    lines = []
    for category, deck_id in deck_ids.items():
        is_suspended = suspended_states.get(category,
                                            _DEFAULT_SUSPENDED.get(category, False))
        state = "suspended" if is_suspended else "new"
        offset = _CATEGORY_DUE_OFFSET.get(category, 0) + due_offset_days
        due = (today + timedelta(days=offset)).isoformat() if not is_suspended else None
        tag = "  [SUSPENDED]" if is_suspended else ""
        lines.append(f"  {category:<10}  due={due or '(none)'}  (+{offset}d){tag}")
        database.insert_card(word_id, category, deck_id, state=state, due=due)
    logger.debug(
        "[STAGGER INTRO]  「%s」 (word_id=%d)\n%s",
        word_zh or "?", word_id, "\n".join(lines),
    )


def _hsk_to_int(hsk_str: str) -> int | None:
    if not hsk_str:
        return None
    s = str(hsk_str).strip()
    if s in ("超纲", ""):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    # Some older Kouyu entries grade a word across two levels ("4/5"). The
    # documented format is a single digit, but falling through to None would
    # silently import the word with no HSK level at all, so take the higher one.
    if "/" in s:
        levels = [int(part) for part in s.split("/") if part.strip().isdigit()]
        if levels:
            return max(levels)
    return None


# Keep old name as alias
_kouyu_hsk_to_int = _hsk_to_int
