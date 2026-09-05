import logging
import sqlite3
from .core import get_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------

def insert_word(word: dict) -> int:
    """INSERT OR IGNORE. Returns the word id whether inserted or already existed.
    For existing entries, also backfills notes and date_yaml if previously empty."""
    conn = get_db()
    conn.execute(
        """INSERT OR IGNORE INTO entries
           (word_zh, lang, pinyin, definition, pos, hsk_level,
            traditional, definition_zh, source, note_type,
            notes, date_yaml, source_sentence, grammar_notes, register, definition_de, definition_fr,
            gender, etymology)
           VALUES (:word_zh, :lang, :pinyin, :definition, :pos, :hsk_level,
                   :traditional, :definition_zh, :source, :note_type,
                   :notes, :date_yaml, :source_sentence, :grammar_notes, :register, :definition_de, :definition_fr,
                   :gender, :etymology)""",
        {
            **word,
            "lang":            word.get("lang") or "zh",
            "pinyin":          word.get("pinyin"),
            "definition":      word.get("definition"),
            "pos":             word.get("pos"),
            "hsk_level":       word.get("hsk_level"),
            "traditional":     word.get("traditional"),
            "definition_zh":   word.get("definition_zh"),
            "source":          word.get("source") or "kouyu",
            "note_type":       word.get("note_type", "vocabulary"),
            "notes":           word.get("notes"),
            "date_yaml":       word.get("date_yaml"),
            "source_sentence": word.get("source_sentence"),
            "grammar_notes":   word.get("grammar_notes"),
            "register":        word.get("register"),
            "definition_de":   word.get("definition_de"),
            "definition_fr":   word.get("definition_fr"),
            # Noun grammatical gender (French/Spanish; #803/#805) — 'm'|'f'|'mf'|None
            "gender":          word.get("gender"),
            # Entry-level word origin, Romance languages only (#906)
            "etymology":       word.get("etymology"),
        },
    )
    # Backfill notes / date_yaml for entries that existed before these fields were added
    if word.get("notes"):
        conn.execute(
            "UPDATE entries SET notes = ? WHERE word_zh = ? AND (notes IS NULL OR notes = '')",
            (word["notes"], word["word_zh"]),
        )
    if word.get("date_yaml"):
        conn.execute(
            "UPDATE entries SET date_yaml = ? WHERE word_zh = ? AND date_yaml IS NULL",
            (word["date_yaml"], word["word_zh"]),
        )
    # Same backfill for etymology (#906): Romance entries imported before the
    # column existed carry their origin inside `notes` and would otherwise show
    # an empty Etymology block forever. Scoped by lang — unlike the two
    # backfills above, which predate UNIQUE(word_zh, lang).
    if word.get("etymology"):
        conn.execute(
            "UPDATE entries SET etymology = ? "
            "WHERE word_zh = ? AND lang = ? AND (etymology IS NULL OR etymology = '')",
            (word["etymology"], word["word_zh"], word.get("lang") or "zh"),
        )
    conn.commit()
    row = conn.execute("SELECT id FROM entries WHERE word_zh = ?", (word["word_zh"],)).fetchone()
    conn.close()
    if row is None:
        raise RuntimeError(
            f"insert_word: INSERT OR IGNORE failed (constraint violation?) for word_zh={word['word_zh']!r}"
        )
    return row["id"]


def get_word(word_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (word_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_word_by_zh(word_zh: str) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM entries WHERE word_zh = ?", (word_zh,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_words_in_deck(deck_id: int) -> list[dict]:
    """Words that have at least one card in this deck."""
    conn = get_db()
    rows = conn.execute(
        """SELECT DISTINCT w.* FROM entries w
           JOIN cards c ON c.word_id = w.id
           WHERE c.deck_id = ?""",
        (deck_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_word_full(word_id: int) -> dict | None:
    """Returns word + examples + characters + measure_words + relations + components."""
    word = get_word(word_id)
    if not word:
        return None
    word["examples"] = get_word_examples(word_id)
    word["characters"] = get_word_characters(word_id)
    word["measure_words"] = get_word_measure_words(word_id)
    word["relations"] = get_word_relations(word_id)
    word["components"] = get_note_components(word_id)
    word["conjugations"] = get_word_conjugations(word_id)
    word["inflections"] = get_word_inflections(word_id)
    return word


def word_has_cards(word_id: int) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM cards WHERE word_id = ? AND deleted_at IS NULL LIMIT 1", (word_id,)
    ).fetchone()
    conn.close()
    return row is not None


def insert_note_component(note_id: int, word_id: int, position: int) -> None:
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO entry_components (note_id, word_id, position) VALUES (?, ?, ?)",
        (note_id, word_id, position),
    )
    conn.commit()
    conn.close()


def get_note_components(note_id: int) -> list[dict]:
    """Return component words for a sentence/chengyu note, with their character data."""
    conn = get_db()
    rows = conn.execute(
        """SELECT nc.position, w.*
           FROM entry_components nc
           JOIN entries w ON w.id = nc.word_id
           WHERE nc.note_id = ?
           ORDER BY nc.position""",
        (note_id,),
    ).fetchall()
    conn.close()
    components = []
    for row in rows:
        comp = dict(row)
        comp["characters"] = get_word_characters(comp["id"])
        comp["examples"] = get_word_examples(comp["id"])
        comp["measure_words"] = get_word_measure_words(comp["id"])
        components.append(comp)
    return components


# ---------------------------------------------------------------------------
# Word examples
# ---------------------------------------------------------------------------

def insert_word_example(word_id: int, example_zh: str,
                        example_pinyin: str | None,
                        example_en: str | None,
                        example_de: str | None,
                        position: int,
                        example_type: str = "example") -> int:
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM entry_examples WHERE word_id = ? AND example_zh = ?",
        (word_id, example_zh),
    ).fetchone()
    if existing:
        conn.close()
        return existing["id"]
    cur = conn.execute(
        """INSERT INTO entry_examples
           (word_id, example_zh, example_pinyin, example_en, example_de, position, example_type)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (word_id, example_zh, example_pinyin, example_en, example_de, position, example_type),
    )
    conn.commit()
    ex_id = cur.lastrowid
    conn.close()
    return ex_id


def insert_word_measure_word(word_id: int, measure_zh: str,
                             pinyin: str | None,
                             meaning: str | None,
                             position: int) -> None:
    conn = get_db()
    conn.execute(
        """INSERT OR IGNORE INTO entry_measure_words
           (word_id, measure_zh, pinyin, meaning, position)
           VALUES (?, ?, ?, ?, ?)""",
        (word_id, measure_zh, pinyin, meaning, position),
    )
    conn.commit()
    conn.close()


def insert_word_relation(word_id: int, related_zh: str,
                         related_pinyin: str | None,
                         related_de: str | None,
                         relation_type: str) -> None:
    """relation_type: 'synonym' or 'antonym'"""
    conn = get_db()
    conn.execute(
        """INSERT OR IGNORE INTO entry_relations
           (word_id, related_zh, related_pinyin, related_de, relation_type)
           VALUES (?, ?, ?, ?, ?)""",
        (word_id, related_zh, related_pinyin, related_de, relation_type),
    )
    conn.commit()
    conn.close()


def insert_word_form(word_id: int, kind: str, paradigm: str, slot: str,
                     form: str, position: int) -> None:
    """Generic single-row writer for entry_forms (#805). `kind` is
    'conjugation' (paradigm=tense, slot=person) or 'inflection'
    (paradigm=dimension e.g. 'nombre'/'genre', slot=value e.g.
    'pluriel'/'féminin'). insert_word_conjugation is a thin wrapper over this
    kept for its established call sites/signature.
    """
    conn = get_db()
    conn.execute(
        """INSERT OR IGNORE INTO entry_forms
           (word_id, kind, paradigm, slot, form, position)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (word_id, kind, paradigm, slot, form, position),
    )
    conn.commit()
    conn.close()


def insert_word_conjugation(word_id: int, tense: str, person: str,
                            form: str, position: int) -> None:
    """person is '' for impersonal forms (participles, infinitive).

    Writes into entry_forms, not the legacy entry_conjugations table (#803:
    entry_forms is the single source of truth going forward — see
    docs/multilang.md). tense -> paradigm, person -> slot.
    """
    insert_word_form(word_id, "conjugation", tense, person, form, position)


def get_word_conjugations(word_id: int) -> list[dict]:
    """Conjugation rows for a word, shaped like the old entry_conjugations
    table (tense/person/form/position) for backward compatibility with
    /api/word/{id} and the frontend's renderConjugationSection (#803)."""
    conn = get_db()
    rows = conn.execute(
        """SELECT paradigm AS tense, slot AS person, form, position
           FROM entry_forms WHERE word_id = ? AND kind = 'conjugation'
           ORDER BY position, id""",
        (word_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_word_inflections(word_id: int) -> list[dict]:
    """Inflection rows for a word (noun/adjective forms — plural, gender
    agreement; #805), shaped like get_word_conjugations for the frontend's
    'inflection' collapsible section: [{paradigm, slot, form, position}]."""
    conn = get_db()
    rows = conn.execute(
        """SELECT paradigm, slot, form, position
           FROM entry_forms WHERE word_id = ? AND kind = 'inflection'
           ORDER BY position, id""",
        (word_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# entry_forms (#803) — generalizes entry_conjugations to also cover noun/
# adjective inflection (plural, gender agreement). See docs/multilang.md for
# the full model.
# ---------------------------------------------------------------------------

def set_entry_forms(word_id: int, forms: list[dict]) -> None:
    """Replace all morphological forms for a word.

    Each form dict: {kind, paradigm, slot, form, position}. kind is
    'conjugation' (paradigm=tense, slot=person) or 'inflection'
    (paradigm=dimension e.g. 'nombre'/'genre', slot=value e.g.
    'pluriel'/'féminin'). Full replace, not merge — callers are expected to
    pass the complete current set for the word (AI-generated entries always
    ship a full table, never a partial patch), mirroring how
    delete_word_examples + re-insert works for entry_examples.
    """
    conn = get_db()
    conn.execute("DELETE FROM entry_forms WHERE word_id = ?", (word_id,))
    for f in forms:
        conn.execute(
            """INSERT OR IGNORE INTO entry_forms
               (word_id, kind, paradigm, slot, form, position)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                word_id,
                f.get("kind", "conjugation"),
                f["paradigm"],
                f.get("slot", ""),
                f["form"],
                f.get("position", 0),
            ),
        )
    conn.commit()
    conn.close()


def get_entry_forms(word_id: int) -> dict:
    """All morphological forms for a word, grouped kind -> paradigm -> slot -> form."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM entry_forms WHERE word_id = ? ORDER BY kind, position, id",
        (word_id,),
    ).fetchall()
    conn.close()
    grouped: dict = {}
    for r in rows:
        grouped.setdefault(r["kind"], {}).setdefault(r["paradigm"], {})[r["slot"]] = r["form"]
    return grouped


def surface_forms(word_id: int | None, word: str, lang: str) -> list[str]:
    """Every surface form that counts as "this word" in `lang`.

    For conjugating languages a sentence may use any inflected/conjugated
    form of the word (e.g. the knowledge story prompt explicitly allows the
    model to adapt "réduire" -> "a réduit"), so matching on the dictionary
    headword alone misses most valid sentences. #803 stores the full
    conjugation/inflection table per entry in entry_forms; this returns the
    headword plus everything stored there, longest first so a multi-word
    form wins over a bare headword prefix. Chinese has no forms and skips
    the lookup entirely — used by both the knowledge-mode sentence matcher
    (ai._card_surface_forms) and the review-card cloze UI (issue #903).
    """
    if lang == "zh" or not word_id:
        return [word]
    forms = [word]
    try:
        grouped = get_entry_forms(word_id)
        for paradigm in grouped.values():
            for slots in paradigm.values():
                forms.extend(f for f in slots.values() if f)
    except Exception as e:
        logger.warning("could not load stored forms for word %s — %s", word_id, e)
        return [word]
    return sorted({f for f in forms if f}, key=len, reverse=True)


def forms_lookup(surface_forms: list[str], lang: str) -> set[str]:
    """Which of these surface forms belong to an already-studied entry in `lang`.

    A surface form matches if it's either a stored conjugated/inflected form
    (entry_forms.form) or the dictionary headword itself (entries.word_zh —
    the word's citation/dictionary form also counts as "already learned").
    This is the core query behind knowledge-base annotation for conjugating
    languages (#803): given the tokens of an article, which ones are forms of
    a word Daniel already knows. `lang` is required — the same surface form
    can belong to unrelated words in different languages (French/Spanish
    share many identical forms).
    """
    if not surface_forms:
        return set()
    conn = get_db()
    placeholders = ",".join("?" for _ in surface_forms)
    form_rows = conn.execute(
        f"""SELECT DISTINCT ef.form FROM entry_forms ef
            JOIN entries e ON e.id = ef.word_id
            WHERE e.lang = ? AND ef.form IN ({placeholders})""",
        (lang, *surface_forms),
    ).fetchall()
    zh_rows = conn.execute(
        f"SELECT word_zh FROM entries WHERE lang = ? AND word_zh IN ({placeholders})",
        (lang, *surface_forms),
    ).fetchall()
    conn.close()
    return {r["form"] for r in form_rows} | {r["word_zh"] for r in zh_rows}


def entry_ids_for_forms(surface_forms: list[str], lang: str) -> dict[str, int]:
    """Map each surface form that belongs to a stored entry to that entry's id.

    The batched, id-returning counterpart to get_word_by_form() (#1042): the
    reader needs to know, for the few hundred words of one page, which ones
    Daniel already has an entry for and where that entry lives — so a word he
    has studied is tappable and opens its full detail page. Doing that with
    one get_word_by_form() per word would be hundreds of queries per page.

    Same matching rule as forms_lookup(): the headword itself, plus every
    stored conjugated/inflected form (entry_forms). No stemming, no guessing —
    a wrong match would send him to another word's entry. Headwords win over
    entry_forms rows, so a form that is one word's headword and another's
    inflection resolves to the headword.
    """
    if not surface_forms:
        return {}
    forms = list(dict.fromkeys(f for f in surface_forms if f))
    if not forms:
        return {}
    conn = get_db()
    placeholders = ",".join("?" for _ in forms)
    out: dict[str, int] = {}
    for row in conn.execute(
        f"""SELECT ef.form AS form, MIN(e.id) AS id FROM entry_forms ef
            JOIN entries e ON e.id = ef.word_id
            WHERE e.lang = ? AND ef.form IN ({placeholders})
            GROUP BY ef.form""",
        (lang, *forms),
    ).fetchall():
        out[row["form"]] = row["id"]
    for row in conn.execute(
        f"""SELECT word_zh AS form, MIN(id) AS id FROM entries
            WHERE lang = ? AND word_zh IN ({placeholders})
            GROUP BY word_zh""",
        (lang, *forms),
    ).fetchall():
        out[row["form"]] = row["id"]
    conn.close()
    return out


def get_word_by_form(surface_form: str, lang: str) -> dict | None:
    """Find the entry a single inflected surface form belongs to (#924).

    `manger` is stored once; typing `mangeons` has to reach that same entry
    instead of paying for a second generation and creating a near-duplicate
    headword. Looks at the headword itself first (the common case), then at
    the stored conjugated/inflected forms — the same table the knowledge-base
    annotator matches against, which is why #803 insists every added word
    carries its full form list.

    Returns None for Chinese and for any form that is not stored: no stemming,
    no guessing. A wrong match would silently move another word's cards.
    """
    if not surface_form:
        return None
    conn = get_db()
    row = conn.execute("SELECT * FROM entries WHERE word_zh = ? AND lang = ?",
                       (surface_form, lang)).fetchone()
    if row is None:
        row = conn.execute(
            """SELECT e.* FROM entries e
               JOIN entry_forms ef ON ef.word_id = e.id
               WHERE e.lang = ? AND ef.form = ?
               ORDER BY e.id LIMIT 1""",
            (lang, surface_form),
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_word_examples(word_id: int) -> None:
    conn = get_db()
    conn.execute("DELETE FROM entry_examples WHERE word_id = ?", (word_id,))
    conn.commit()
    conn.close()


def get_word_examples(word_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM entry_examples WHERE word_id = ? ORDER BY position",
        (word_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_word_measure_words(word_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM entry_measure_words WHERE word_id = ? ORDER BY position",
        (word_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_word_relations(word_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM entry_relations WHERE word_id = ? ORDER BY relation_type, id",
        (word_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def insert_grammar_structure(word_id: int, structure: str, explanation: str | None,
                              example_zh: str | None, position: int) -> None:
    conn = get_db()
    conn.execute(
        """INSERT INTO entry_grammar_structures (word_id, structure, explanation, example_zh, position)
           VALUES (?, ?, ?, ?, ?)""",
        (word_id, structure, explanation, example_zh, position),
    )
    conn.commit()
    conn.close()


def get_word_grammar_structures(word_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM entry_grammar_structures WHERE word_id = ? ORDER BY position",
        (word_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Grammar points  (type: grammar — reference only, no SRS cards)
# ---------------------------------------------------------------------------

def insert_grammar_point(name: str, level: str | None, structure: str | None,
                         meaning: str | None, usage: str | None,
                         cultural_note: str | None) -> int:
    """Insert a grammar_points row (INSERT OR IGNORE). Returns the row id."""
    conn = get_db()
    conn.execute(
        """INSERT OR IGNORE INTO grammar_points
           (name, level, structure, meaning, usage, cultural_note)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (name, level, structure, meaning, usage, cultural_note),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM grammar_points WHERE name = ?", (name,)
    ).fetchone()
    conn.close()
    if row is None:
        raise RuntimeError(f"insert_grammar_point: INSERT OR IGNORE failed for name={name!r}")
    return row["id"]


def insert_grammar_example(grammar_id: int, example_zh: str, pinyin: str | None,
                           example_de: str | None, structure: str | None,
                           position: int) -> None:
    conn = get_db()
    conn.execute(
        """INSERT INTO grammar_examples
           (grammar_id, example_zh, pinyin, example_de, structure, position)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (grammar_id, example_zh, pinyin, example_de, structure, position),
    )
    conn.commit()
    conn.close()


def insert_grammar_pattern(grammar_id: int, pattern: str, meaning: str | None,
                           example: str | None, position: int) -> None:
    conn = get_db()
    conn.execute(
        """INSERT INTO grammar_patterns (grammar_id, pattern, meaning, example, position)
           VALUES (?, ?, ?, ?, ?)""",
        (grammar_id, pattern, meaning, example, position),
    )
    conn.commit()
    conn.close()


def insert_grammar_comparison(grammar_id: int, title: str | None,
                              explanation: str | None, position: int) -> None:
    conn = get_db()
    conn.execute(
        """INSERT INTO grammar_comparisons (grammar_id, title, explanation, position)
           VALUES (?, ?, ?, ?)""",
        (grammar_id, title, explanation, position),
    )
    conn.commit()
    conn.close()


def insert_grammar_expression(grammar_id: int, expression: str,
                              meaning: str | None, position: int) -> None:
    conn = get_db()
    conn.execute(
        """INSERT INTO grammar_expressions (grammar_id, expression, meaning, position)
           VALUES (?, ?, ?, ?)""",
        (grammar_id, expression, meaning, position),
    )
    conn.commit()
    conn.close()


def get_grammar_point_by_name(name: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM grammar_points WHERE name = ?", (name,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_grammar_points() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM grammar_points ORDER BY name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Characters
# ---------------------------------------------------------------------------

def upsert_character(char: dict) -> int:
    """Insert or update a character row without deleting it (preserves FK refs). Returns char_id."""
    conn = get_db()
    conn.execute(
        """INSERT INTO characters
           (char, traditional, pinyin, hsk_level, etymology, other_meanings)
           VALUES (:char, :traditional, :pinyin, :hsk_level, :etymology, :other_meanings)
           ON CONFLICT(char) DO UPDATE SET
               traditional    = excluded.traditional,
               pinyin         = excluded.pinyin,
               hsk_level      = excluded.hsk_level,
               etymology      = COALESCE(excluded.etymology, etymology),
               other_meanings = COALESCE(excluded.other_meanings, other_meanings)""",
        char,
    )
    conn.commit()
    row = conn.execute("SELECT id FROM characters WHERE char = ?", (char["char"],)).fetchone()
    conn.close()
    if row is None:
        raise RuntimeError(f"upsert_character: INSERT failed for char={char['char']!r}")
    return row["id"]


def get_character(char: str) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM characters WHERE char = ?", (char,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_character_by_id(char_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM characters WHERE id = ?", (char_id,)).fetchone()
    if not row:
        conn.close()
        return None
    d = dict(row)
    comp_rows = conn.execute(
        "SELECT compound_zh, pinyin, meaning FROM character_compounds WHERE char_id = ? ORDER BY position",
        (char_id,),
    ).fetchall()
    d["compounds"] = [dict(c) for c in comp_rows]
    conn.close()
    return d


def get_all_characters() -> list[dict]:
    """Return all characters sorted by their Unicode code point (natural stroke order proxy)."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM characters ORDER BY char").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_words_for_character(char_id: int) -> list[dict]:
    """Return all words that contain this character."""
    conn = get_db()
    rows = conn.execute(
        """SELECT w.id, w.word_zh, w.pinyin, w.definition, w.pos
           FROM entry_characters wc
           JOIN entries w ON w.id = wc.word_id
           WHERE wc.char_id = ?
           ORDER BY w.word_zh""",
        (char_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_character(char_id: int, fields: dict) -> None:
    allowed = {"pinyin", "etymology", "other_meanings", "traditional", "hsk_level"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k}=:{k}" for k in updates)
    conn = get_db()
    conn.execute(f"UPDATE characters SET {set_clause} WHERE id=:id", {**updates, "id": char_id})
    conn.commit()
    conn.close()


def upsert_character_compounds(char_id: int, compounds: list[dict]) -> None:
    """Insert or update normalised compound rows for a character.

    Each compound dict should have keys: zh (required), pinyin, meaning.
    Existing rows for this char_id are replaced on conflict (zh).
    """
    conn = get_db()
    for pos, c in enumerate(compounds):
        zh = (c.get("simplified") or c.get("zh") or c.get("compound") or "").strip()
        if not zh:
            continue
        conn.execute(
            """INSERT INTO character_compounds (char_id, compound_zh, pinyin, meaning, position)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(char_id, compound_zh) DO UPDATE SET
                   pinyin   = excluded.pinyin,
                   meaning  = excluded.meaning,
                   position = excluded.position""",
            (char_id, zh, c.get("pinyin"), c.get("meaning") or c.get("de"), pos),
        )
    conn.commit()
    conn.close()


def insert_word_character(word_id: int, char_id: int,
                          position: int,
                          meaning_in_context: str | None) -> None:
    conn = get_db()
    conn.execute(
        """INSERT OR IGNORE INTO entry_characters
           (word_id, char_id, position, meaning_in_context)
           VALUES (?, ?, ?, ?)""",
        (word_id, char_id, position, meaning_in_context),
    )
    conn.commit()
    conn.close()


def get_word_characters(word_id: int) -> list[dict]:
    """Returns characters in position order, joined with full character details.
    Compounds are fetched from the character_compounds relational table."""
    conn = get_db()
    rows = conn.execute(
        """SELECT wc.position, wc.meaning_in_context,
                  c.id as char_id, c.char, c.traditional, c.pinyin,
                  c.hsk_level, c.etymology, c.other_meanings
           FROM entry_characters wc
           JOIN characters c ON c.id = wc.char_id
           WHERE wc.word_id = ?
           ORDER BY wc.position""",
        (word_id,),
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        comp_rows = conn.execute(
            """SELECT compound_zh, pinyin, meaning FROM character_compounds
               WHERE char_id = ? ORDER BY position""",
            (d["char_id"],),
        ).fetchall()
        d["compounds"] = [dict(c) for c in comp_rows]
        result.append(d)
    conn.close()
    return result


def get_random_word(exclude_word: str = "") -> str | None:
    conn = get_db()
    row = conn.execute(
        """SELECT word_zh FROM entries
           WHERE note_type = 'vocabulary'
             AND word_zh != ?
             AND length(word_zh) BETWEEN 1 AND 3
           ORDER BY RANDOM() LIMIT 1""",
        (exclude_word,),
    ).fetchone()
    conn.close()
    return row["word_zh"] if row else None


def get_random_words(n: int = 10) -> list[dict]:
    """Return n random lexical entries for the 'daily random words' popup.

    Includes vocabulary / chengyu / expression (things you can actually use);
    excludes full sentences and grammar points. No SRS involvement.
    """
    conn = get_db()
    rows = conn.execute(
        """SELECT word_zh, pinyin, definition, definition_zh
           FROM entries
           WHERE note_type IN ('vocabulary', 'chengyu', 'expression')
             AND word_zh IS NOT NULL AND word_zh != ''
           ORDER BY RANDOM() LIMIT ?""",
        (n,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
