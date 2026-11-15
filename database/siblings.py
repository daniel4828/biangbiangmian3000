"""Sibling groups (#1553): which entries bury / suppress each other.

Until now "siblings" were the cards of one entry (same word_id). Spanish
irregular-verb form cards ("caer · Presente") are separate entries, so one verb
could show up several times a day. A sibling GROUP fixes that:

  - Spanish form-card entry  -> its lemma ("caer · Imperativo" -> "caer")
  - Spanish entry whose word_zh equals that lemma (the verb itself) -> same group
  - everything else (Chinese, French, other Spanish words) -> its own word_id,
    so their behaviour is unchanged.

This module is the ONE place that knows how to compute the group; bury after
review, queue/story suppression and the daily-target estimate all use it.
"""


def sibling_group_key(word_id: int, word_zh: str | None, lang: str | None):
    """Group key of one entry: an int (word_id) or ("es", lemma). Never raises."""
    if lang != "es":
        return word_id
    import verb_slots
    split = verb_slots.split_form_card_word(word_zh)
    if split is not None:
        return ("es", split[0])
    return ("es", (word_zh or "").strip().lower())


def sibling_group_map(word_ids, conn) -> dict:
    """word_id -> group key for the given entries (unknown ids map to themselves)."""
    ids = list(set(word_ids))
    out = {i: i for i in ids}
    if not ids:
        return out
    ph = ",".join("?" * len(ids))
    for r in conn.execute(f"SELECT id, word_zh, lang FROM entries WHERE id IN ({ph})", ids):
        out[r["id"]] = sibling_group_key(r["id"], r["word_zh"], r["lang"])
    return out


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def sibling_group_members(word_ids, conn) -> dict[int, set[int]]:
    """word_id -> every word_id of its sibling group (always includes itself)."""
    keys = sibling_group_map(word_ids, conn)
    members = {i: {i} for i in keys}
    lemmas = sorted({k[1] for k in keys.values() if isinstance(k, tuple)})
    if not lemmas:
        return members
    import verb_slots
    sep = verb_slots._SEP
    clauses, params = [], []
    for lemma in lemmas:
        clauses.append("lower(word_zh) = ? OR lower(word_zh) LIKE ? ESCAPE '\\'")
        params += [lemma, _like_escape(lemma) + _like_escape(sep) + "%"]
    by_key: dict = {}
    for r in conn.execute(
        f"SELECT id, word_zh, lang FROM entries WHERE lang = 'es' AND ({' OR '.join(clauses)})",
        params,
    ):
        by_key.setdefault(sibling_group_key(r["id"], r["word_zh"], r["lang"]), set()).add(r["id"])
    for i, key in keys.items():
        if isinstance(key, tuple):
            members[i] |= by_key.get(key, set())
    return members
