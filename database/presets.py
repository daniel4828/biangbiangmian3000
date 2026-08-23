import sqlite3
from .core import get_db


# ---------------------------------------------------------------------------
# Deck presets
# ---------------------------------------------------------------------------

def default_preset() -> dict:
    return {
        "name": "Default",
        "new_per_day": 20,
        "reviews_per_day": 100,
        "learning_steps": "11m 10m",
        "graduating_interval": 1,
        "easy_interval": 4,
        "relearning_steps": "10",
        "minimum_interval": 1,
        "learned_interval": 4,
        "enable_probation": 1,
        "insertion_order": "sequential",
        "bury_siblings": 1,
        "randomize_story_order": 0,
        "leech_threshold": 3,
        "learning_leech_threshold": 6,
        "enable_learning_leech": 1,
        "leech_action": "suspend",
        "desired_retention": 0.9,
        "maximum_interval": 36500,
        "fsrs_weights": None,
        "enable_fsrs": 1,
        "learning_hard_1d": 1,
        "learning_hard_days": 1,
        "new_gather_order": "ascending_position",
        "new_sort_order": "card_type_gathered",
        "new_review_order": "mixed",
        "interday_learning_review_order": "mixed",
        "review_sort_order": "due_random",
        "bury_new_siblings": 0,
        "bury_review_siblings": 0,
        "bury_interday_siblings": 0,
        "bury_quick_mode": "all",
        "category_order": "listening,reading,creating",
        "sibling_separation": 3,
        "sibling_factor": 0.2,
        "reading_enabled": 0,
        "listening_enabled": 1,
        "creating_enabled": 1,
        "autoplay_delay_ms": 1000,
    }


def get_default_preset() -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM deck_presets WHERE is_default = 1 LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None


def set_default_preset(preset_id: int) -> None:
    conn = get_db()
    conn.execute("UPDATE deck_presets SET is_default = 0")
    conn.execute("UPDATE deck_presets SET is_default = 1 WHERE id = ?", (preset_id,))
    conn.commit()
    conn.close()


def list_presets() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT p.*, COUNT(d.id) AS deck_count
           FROM deck_presets p
           LEFT JOIN decks d ON d.preset_id = p.id
           GROUP BY p.id
           ORDER BY p.is_default DESC, p.name"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_preset(preset_id: int) -> None:
    conn = get_db()
    in_use = conn.execute(
        "SELECT COUNT(*) FROM decks WHERE preset_id = ?", (preset_id,)
    ).fetchone()[0]
    if in_use:
        conn.close()
        raise ValueError("Preset is still assigned to one or more decks")
    conn.execute("DELETE FROM deck_presets WHERE id = ?", (preset_id,))
    conn.commit()
    conn.close()


def assign_preset_to_deck(deck_id: int, preset_id: int) -> None:
    conn = get_db()
    conn.execute("UPDATE decks SET preset_id = ? WHERE id = ?", (preset_id, deck_id))
    conn.commit()
    conn.close()


def get_preset(preset_id: int) -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM deck_presets WHERE id = ?", (preset_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_preset_for_deck(deck_id: int, category: str | None = None) -> dict:
    conn = get_db()
    row = conn.execute(
        """SELECT p.*,
                  d.new_review_order_override AS deck_nro_override,
                  d.bury_quick_mode           AS deck_bury_quick_mode
           FROM deck_presets p JOIN decks d ON d.preset_id = p.id WHERE d.id = ?""",
        (deck_id,),
    ).fetchone()
    if not row:
        conn.close()
        return None
    preset = dict(row)
    deck_nro = preset.pop("deck_nro_override", None)
    if deck_nro is not None:
        preset["new_review_order_override"] = deck_nro
    deck_bury = preset.pop("deck_bury_quick_mode", None)
    if deck_bury is not None:
        preset["bury_quick_mode"] = deck_bury

    if category:
        override_row = conn.execute(
            "SELECT * FROM preset_category_overrides WHERE preset_id = ? AND category = ?",
            (preset["id"], category),
        ).fetchone()
        if override_row:
            for key, val in dict(override_row).items():
                if key not in ("id", "preset_id", "category") and val is not None:
                    preset[key] = val

    conn.close()
    return preset


# ---------------------------------------------------------------------------
# Category overrides
# ---------------------------------------------------------------------------

_OVERRIDE_FIELDS = {
    "new_per_day", "reviews_per_day", "learning_steps",
    "graduating_interval", "easy_interval", "relearning_steps",
    "minimum_interval", "leech_threshold", "learning_leech_threshold", "leech_action",
}


def get_category_overrides(preset_id: int) -> dict:
    """Return {category: {field: value, ...}} for all overrides of a preset."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM preset_category_overrides WHERE preset_id = ?", (preset_id,)
    ).fetchall()
    conn.close()
    result = {}
    for r in rows:
        d = dict(r)
        cat = d.pop("category")
        d.pop("id", None)
        d.pop("preset_id", None)
        result[cat] = {k: v for k, v in d.items() if v is not None}
    return result


def set_category_override(preset_id: int, category: str, fields: dict) -> None:
    """Upsert category-level scheduling overrides. Pass None values to clear a field."""
    allowed = {k: fields[k] for k in fields if k in _OVERRIDE_FIELDS}
    if not allowed:
        return
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM preset_category_overrides WHERE preset_id = ? AND category = ?",
        (preset_id, category),
    ).fetchone()
    if existing:
        set_clause = ", ".join(f"{k} = :{k}" for k in allowed)
        allowed["_pid"] = preset_id
        allowed["_cat"] = category
        conn.execute(
            f"UPDATE preset_category_overrides SET {set_clause} WHERE preset_id = :_pid AND category = :_cat",
            allowed,
        )
    else:
        cols = ", ".join(["preset_id", "category"] + list(allowed.keys()))
        placeholders = ", ".join([":preset_id", ":category"] + [f":{k}" for k in allowed])
        allowed["preset_id"] = preset_id
        allowed["category"] = category
        conn.execute(
            f"INSERT INTO preset_category_overrides ({cols}) VALUES ({placeholders})", allowed
        )
    conn.commit()
    conn.close()


def delete_category_override(preset_id: int, category: str) -> None:
    conn = get_db()
    conn.execute(
        "DELETE FROM preset_category_overrides WHERE preset_id = ? AND category = ?",
        (preset_id, category),
    )
    conn.commit()
    conn.close()


def insert_preset(preset: dict) -> int:
    preset.setdefault("desired_retention", 0.9)
    preset.setdefault("maximum_interval", 36500)
    preset.setdefault("fsrs_weights", None)
    preset.setdefault("enable_fsrs", 1)
    preset.setdefault("learning_hard_1d", 1)
    preset.setdefault("learning_hard_days", 1)
    preset.setdefault("learned_interval", 4)
    preset.setdefault("enable_probation", 1)
    preset.setdefault("reading_enabled", 0)
    preset.setdefault("listening_enabled", 1)
    preset.setdefault("creating_enabled", 1)
    preset.setdefault("autoplay_delay_ms", 1000)
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO deck_presets
           (name, new_per_day, reviews_per_day,
            learning_steps, graduating_interval, easy_interval,
            relearning_steps, minimum_interval, learned_interval,
            enable_probation, insertion_order,
            bury_siblings, randomize_story_order, leech_threshold, learning_leech_threshold, enable_learning_leech, leech_action,
            desired_retention, maximum_interval, fsrs_weights, enable_fsrs, learning_hard_1d, learning_hard_days,
            new_gather_order, new_sort_order, new_review_order,
            interday_learning_review_order, review_sort_order,
            bury_new_siblings, bury_review_siblings, bury_interday_siblings,
            bury_quick_mode, category_order, sibling_separation, sibling_factor,
            reading_enabled, listening_enabled, creating_enabled, autoplay_delay_ms)
           VALUES (:name, :new_per_day, :reviews_per_day,
                   :learning_steps, :graduating_interval, :easy_interval,
                   :relearning_steps, :minimum_interval, :learned_interval,
                   :enable_probation, :insertion_order,
                   :bury_siblings, :randomize_story_order, :leech_threshold, :learning_leech_threshold, :enable_learning_leech, :leech_action,
                   :desired_retention, :maximum_interval, :fsrs_weights, :enable_fsrs, :learning_hard_1d, :learning_hard_days,
                   :new_gather_order, :new_sort_order, :new_review_order,
                   :interday_learning_review_order, :review_sort_order,
                   :bury_new_siblings, :bury_review_siblings, :bury_interday_siblings,
                   :bury_quick_mode, :category_order, :sibling_separation, :sibling_factor,
                   :reading_enabled, :listening_enabled, :creating_enabled, :autoplay_delay_ms)""",
        preset,
    )
    conn.commit()
    preset_id = cur.lastrowid
    conn.close()
    return preset_id


def update_preset(preset_id: int, fields: dict) -> None:
    allowed = {
        "name", "new_per_day", "reviews_per_day",
        "learning_steps", "graduating_interval", "easy_interval",
        "relearning_steps", "minimum_interval", "learned_interval",
        "enable_probation", "insertion_order",
        "bury_siblings", "randomize_story_order", "leech_threshold", "learning_leech_threshold", "enable_learning_leech", "leech_action",
        "desired_retention", "maximum_interval", "fsrs_weights", "enable_fsrs", "learning_hard_1d", "learning_hard_days",
        "new_gather_order", "new_sort_order", "new_review_order",
        "interday_learning_review_order", "review_sort_order",
        "bury_new_siblings", "bury_review_siblings", "bury_interday_siblings",
        "bury_quick_mode", "category_order", "new_review_order_override",
        "sibling_separation", "sibling_factor", "reading_enabled",
        "listening_enabled", "creating_enabled",
        "autoplay_delay_ms",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["_id"] = preset_id
    conn = get_db()
    conn.execute(f"UPDATE deck_presets SET {set_clause} WHERE id = :_id", updates)
    conn.commit()
    conn.close()


def get_lang_preset(lang: str | None, create: bool = False) -> dict | None:
    """The preset a language tree's decks are bound to (issue #806).

    Chinese decks hang off the default preset (id=2 in production); every other
    language tree got its own clone, named after its deck root ('Français',
    'Español'). Looked up by name, read-only by default.

    Pass create=True on write paths (#898): silently falling back to the
    default preset there would write one language's settings onto Chinese —
    an unused extra preset row is the cheaper mistake by far.
    """
    from languages import DEFAULT_LANG, deck_root
    from .core import _ensure_lang_preset

    conn = get_db()
    if not lang or lang == DEFAULT_LANG:
        row = conn.execute("SELECT * FROM deck_presets WHERE is_default = 1 LIMIT 1").fetchone()
        conn.close()
        return dict(row) if row else None

    name = deck_root(lang)
    row = conn.execute("SELECT * FROM deck_presets WHERE name = ?", (name,)).fetchone()
    if row is None and create:
        _ensure_lang_preset(conn, lang)
        row = conn.execute("SELECT * FROM deck_presets WHERE name = ?", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None
