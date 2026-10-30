"""Build resources/es-lexicon.json.gz — the bundled Spanish form -> lemma
lexicon (#1293, Wiktionary conjugations and German glosses #1305) — from five
downloaded source files.

Usage:
    python scripts/build_es_lexicon.py <frequency.csv> <es-en.data> \
        <kaikki-es.jsonl> <kaikki-de-es.jsonl> <kaikki-de-de.jsonl> \
        [--out resources/es-lexicon.json.gz]

Sources (details, licenses and checksums in resources/README.md):
  * frequency.csv  — doozan/spanish_data, OpenSubtitles frequencies grouped by
                     lemma. Gives the lemma list, its rank/pos and, in the
                     `usage` column, every surface form seen for that lemma.
  * es-en.data     — doozan/spanish_data, en.wiktionary dump. Gives noun gender
                     and a short English gloss.
  * kaikki-es.jsonl — kaikki.org extract of the en.wiktionary Spanish entries
                     (~1.1 GB, one JSON object per line). Its `forms` entries
                     with source "conjugation" give (tense, person) for every
                     verb lemma of the frequency file, not just a few hundred.
  * kaikki-de-es.jsonl — kaikki.org extract of the de.wiktionary Spanish
                     entries (~7k lines). Direct German translations.
                     Inflected-form entries/senses (`form_of` / `form-of`)
                     are skipped: their "gloss" describes the form.
  * kaikki-de-de.jsonl — kaikki.org extract of the de.wiktionary German entries
                     (~3.4 GB). Each German headword lists Spanish
                     translations; inverted, that gives more German glosses.

Output (gzip, mtime=0, sorted keys -> byte-identical on every run):

    {"version": 1,
     "lemmas": {"cuenta": {"r": 812, "p": "n", "g": "f", "en": "count, tally",
                           "de": "Rechnung, Konto"}},
     "forms":  {"fui": ["ser", "ir"]},
     "verbs":  {"tuve": [["tener", "ind_pret", "1s"]]}}

Design notes:
  * Everything is lowercase, lemmas included: the annotator lowercases every
    token before the lookup, so a "Madrid" lemma would never be found.
  * Rank is dense: the n-th distinct lemma in file order gets rank n (the file
    is sorted by count, and the same lemma can appear in several rows for
    different parts of speech — the first row wins for `p`). A lemma counts as
    a verb for the conjugation tables if ANY of its rows has pos `v`: `cuenta`
    is first a noun but is also a form of contar.
  * Only lemmas of the frequency file exist in the output; Wiktionary lemmas
    that are not in it are ignored.
  * `forms` omits entries whose list is exactly [form]: that is the answer
    lexicon.lemmas_of() gives for any lemma anyway, and those entries are most
    of the file.
  * `forms` takes every conjugation form (incl. the -se subjunctive and the
    feminine/plural participles); `verbs` tags only one form per slot.
    lexicon.form_for() returns the first form in file order (sorted keys), so a
    slot must have exactly one tagged form. Hence NOT tagged: the -se variants
    (tuviese; the -ra form is the tagged subj_impf), feminine/plural
    participles, and anything voseo (`vos-form`, `with-vos`) or clitic-combined
    (`combined-form`, dámelo) — those are skipped entirely, neither in forms
    nor in verbs.
  * Compound tenses ("he hablado") are skipped — the reader tokenises single
    words. The negative imperative is stored without its "no" ("no hables" ->
    "hables"): the bare word is what appears in running text, and the "no"
    is a separate token the annotator sees on its own.
  * Anything that is not a plain run of letters is dropped, so a stray
    "ca." or "1,5" can never become a lookup key.
  * The big JSONL files are streamed line by line and cheaply pre-filtered
    with a substring test before json.loads.
"""
import argparse
import csv
import gzip
import hashlib
import json
import sys
from pathlib import Path

VERSION = 1
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "resources" / "es-lexicon.json.gz"

# Our tense codes in output order.
TENSE_ORDER = ["ind_pres", "ind_pret", "ind_impf", "ind_fut", "ind_cond",
               "subj_pres", "subj_impf", "subj_fut", "imp_aff", "imp_neg",
               "ger", "part"]
PERSONS = ["", "1s", "2s", "3s", "1p", "2p", "3p"]

# kaikki tag -> our code.
_INDICATIVE = {"present": "ind_pres", "preterite": "ind_pret",
               "imperfect": "ind_impf", "future": "ind_fut"}
_SUBJUNCTIVE = {"present": "subj_pres", "imperfect": "subj_impf", "future": "subj_fut"}
_PERSON_TAGS = {"first-person": "1", "second-person": "2", "third-person": "3"}
_SKIP_TAGS = {"combined-form", "vos-form", "with-vos", "table-tags", "inflection-template"}

# Frequency-file pos -> the es-en.data pos block that describes it.
GLOSS_POS = {"n": "n", "adj": "adj", "v": "v", "adv": "adv"}
MAX_GLOSSES = 3
MAX_GLOSS_CHARS = 100


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _clean_form(raw: str) -> str | None:
    """Lowercased form, or None when it is not a plain word."""
    form = (raw or "").strip().lower()
    return form if form and form.isalpha() else None


def parse_frequency(path: Path):
    """-> (lemma_order, lemma_pos, form_to_lemmas, verb_lemmas).

    lemma_order is the distinct lemmas in first-appearance order; a lemma with
    several rows (different pos) keeps the first row's pos but contributes the
    forms of every row. verb_lemmas is every lemma with at least one `v` row."""
    order: list[str] = []
    pos_of: dict[str, str] = {}
    forms: dict[str, set[str]] = {}
    verbs: set[str] = set()
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            pos = (row.get("pos") or "").strip()
            lemma = _clean_form(row.get("spanish"))
            if not lemma or pos == "none" or not pos:
                continue
            if lemma not in pos_of:
                pos_of[lemma] = pos
                order.append(lemma)
            if pos == "v":
                verbs.add(lemma)
            for item in (row.get("usage") or "").split("|"):
                _, sep, raw_form = item.partition(":")
                form = _clean_form(raw_form) if sep else None
                if form:
                    forms.setdefault(form, set()).add(lemma)
    return order, pos_of, forms, verbs


def parse_wiktionary(path: Path) -> dict[str, list[dict]]:
    """headword (lowercase) -> list of POS blocks {"pos", "g", "glosses"}.

    The file is a custom text format: records separated by `_____`, first
    line = headword, `pos: x` at column 0 opens a block, `  g:` / `  gloss:`
    are two-space indented; deeper-indented lines (`q:`, `syn:`) belong to the
    previous gloss and are ignored. A headword that appears in several records
    keeps all of its blocks, in file order."""
    blocks: dict[str, list[dict]] = {}
    head: str | None = None
    current: dict | None = None
    expect_head = True
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if line == "_____":
                expect_head = True
                head = None
                current = None
                continue
            if expect_head:
                expect_head = False
                head = line.strip().lower() or None
                current = None
                continue
            if head is None:
                continue
            if line.startswith("pos:"):
                current = {"pos": line[4:].strip(), "g": None, "glosses": []}
                blocks.setdefault(head, []).append(current)
            elif current is not None and line.startswith("  ") and not line.startswith("   "):
                key, _, value = line.strip().partition(":")
                value = value.strip()
                if key == "g" and value and current["g"] is None:
                    current["g"] = value
                elif key == "gloss" and value:
                    current["glosses"].append(value)
    return blocks


def _iter_jsonl(path: Path, needle: str):
    """Parsed objects of the lines that contain `needle`. The substring test is
    what keeps a 3 GB file tolerable: json.loads only runs on candidates."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if needle not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def _conjugation_slot(tags: set[str]):
    """kaikki tag set of one conjugation form -> (tense, person), or None when
    the form is real but gets no tag (-se variants, feminine/plural
    participles, anything unrecognised)."""
    if "participle" in tags:
        if {"masculine", "singular", "past"} <= tags:
            return "part", ""
        return None
    if "gerund" in tags:
        return "ger", ""
    if "imperfect-se" in tags:
        return None
    person = next((n for t, n in _PERSON_TAGS.items() if t in tags), None)
    number = "s" if "singular" in tags else "p" if "plural" in tags else None
    if person is None or number is None:
        return None
    slot = person + number
    if "imperative" in tags:
        return ("imp_neg" if "negative" in tags else "imp_aff"), slot
    if "subjunctive" in tags:
        code = next((c for t, c in _SUBJUNCTIVE.items() if t in tags), None)
    elif "conditional" in tags:
        code = "ind_cond"
    elif "indicative" in tags:
        code = next((c for t, c in _INDICATIVE.items() if t in tags), None)
    else:
        code = None
    return (code, slot) if code else None


def parse_kaikki_es(path: Path, verb_lemmas: set[str]):
    """-> (forms, tags): forms maps every conjugation form -> {lemma}, tags
    maps form -> {(lemma, tense, person)}. Only verb entries whose headword is
    a verb lemma of the frequency file; several entries of one word merge."""
    forms: dict[str, set[str]] = {}
    tags: dict[str, set[tuple[str, str, str]]] = {}
    for entry in _iter_jsonl(path, '"pos": "verb"'):
        if entry.get("pos") != "verb":
            continue
        lemma = _clean_form(entry.get("word"))
        if lemma not in verb_lemmas:
            continue
        for item in entry.get("forms") or []:
            if not isinstance(item, dict) or item.get("source") != "conjugation":
                continue
            tag_set = set(item.get("tags") or [])
            if tag_set & _SKIP_TAGS or "infinitive" in tag_set:
                continue
            slot = _conjugation_slot(tag_set)
            text = (item.get("form") or "").strip().lower()
            if slot and slot[0] == "imp_neg" and text.startswith("no "):
                text = text[3:].strip()
            form = _clean_form(text)  # also rejects anything with a space
            if not form:
                continue
            forms.setdefault(form, set()).add(lemma)
            if slot:
                tags.setdefault(form, set()).add((lemma, slot[0], slot[1]))
    return forms, tags


def parse_de_es(path: Path) -> dict[str, list[str]]:
    """lemma (lowercase) -> direct German glosses, in file order: the entry's
    German translations, or when it has none the first gloss of each sense."""
    out: dict[str, list[str]] = {}
    for entry in _iter_jsonl(path, '"word"'):
        lemma = _clean_form(entry.get("word"))
        if not lemma:
            continue
        # Inflected-form entries ("cuenta", "es", "ama") describe the form
        # ("3. Person Singular ... des Verbs contar"), not a meaning: their
        # form-of senses are skipped, and an entry with only such senses is
        # ignored altogether (translations included).
        senses = [s for s in entry.get("senses") or [] if isinstance(s, dict)]
        real = [s for s in senses if "form_of" not in s and "form-of" not in (s.get("tags") or [])]
        if senses and not real:
            continue
        found = [t["word"].strip() for t in entry.get("translations") or []
                 if isinstance(t, dict) and t.get("lang_code") == "de"
                 and isinstance(t.get("word"), str) and t["word"].strip()]
        if not found:
            for sense in real:
                glosses = sense.get("glosses")
                if glosses and isinstance(glosses[0], str) and glosses[0].strip():
                    found.append(glosses[0].strip())
        if found:
            out.setdefault(lemma, []).extend(found)
    return out


def parse_de_de(path: Path, lemmas) -> dict[str, dict[str, int]]:
    """Spanish lemma (lowercase) -> {German headword: times seen}, inverted
    from the German entries' Spanish translations. Insertion order = first
    seen. Only Spanish words that are in `lemmas`."""
    out: dict[str, dict[str, int]] = {}
    for entry in _iter_jsonl(path, '"lang_code": "es"'):
        german = entry.get("word")
        if not isinstance(german, str) or not german.strip():
            continue
        for t in entry.get("translations") or []:
            if not isinstance(t, dict) or t.get("lang_code") != "es":
                continue
            spanish = (t.get("word") or "").strip().lower() if isinstance(t.get("word"), str) else ""
            if spanish in lemmas:
                bucket = out.setdefault(spanish, {})
                bucket[german.strip()] = bucket.get(german.strip(), 0) + 1
    return out


def german_gloss(lemma: str, direct: list[str], inverse: dict[str, int]) -> str | None:
    """Direct glosses first, then inverse translations by count (ties keep
    first-seen order); case-insensitive dedupe, never the lemma itself."""
    chosen: list[str] = []
    seen = {lemma.lower()}

    def add(word: str):
        key = word.strip().lower()
        if key and key not in seen:
            seen.add(key)
            chosen.append(word.strip())

    for word in direct:
        add(word)
    if len(chosen) < MAX_GLOSSES:
        for word, _ in sorted(inverse.items(), key=lambda kv: -kv[1]):
            add(word)
            if len(chosen) >= MAX_GLOSSES:
                break
    if not chosen:
        return None
    text = ", ".join(chosen[:MAX_GLOSSES])
    return text[:MAX_GLOSS_CHARS].rstrip()


def _gloss_for(blocks: list[dict], pos: str) -> str | None:
    # Frequency-file pos tags (n, v, adj, adv, conj, pron, prep, art, num,
    # determiner, interj, prop) are spelled the same in wiktionary, so the
    # lemma's pos is the block pos. Several blocks of that pos are merged and
    # duplicate gloss lines dropped (tener lists "to have; to possess" twice).
    # Fallback: the first non-proper-noun block — never a `prop` block unless
    # the lemma itself is one (como/conj must not become "Como, a city in
    # Lombardy").
    wanted = GLOSS_POS.get(pos, pos)
    chosen = [b for b in blocks if b["pos"] == wanted]
    if not chosen:
        first = next((b for b in blocks if b["pos"] != "prop"), None)
        chosen = [first] if first else []
    # Gloss lines themselves contain ";"-separated senses ("to have; to
    # possess"), so dedupe and count the parts, not the lines.
    glosses = list(dict.fromkeys(
        part for b in chosen for line in b["glosses"]
        for part in (p.strip() for p in line.split(";")) if part))
    if not glosses:
        return None
    text = "; ".join(glosses[:MAX_GLOSSES])
    return text[:MAX_GLOSS_CHARS].rstrip()


def build_lexicon(freq_path: Path, data_path: Path, kaikki_es_path: Path,
                  kaikki_de_es_path: Path, kaikki_de_de_path: Path) -> dict:
    order, pos_of, forms, verb_lemmas = parse_frequency(freq_path)
    wikt = parse_wiktionary(data_path)
    conj_forms, tags = parse_kaikki_es(kaikki_es_path, verb_lemmas)
    direct_de = parse_de_es(kaikki_de_es_path)
    inverse_de = parse_de_de(kaikki_de_de_path, set(order))

    lemmas: dict[str, dict] = {}
    for rank, lemma in enumerate(order, 1):
        lemmas[lemma] = {"r": rank, "p": pos_of[lemma]}

    # Gender (nouns only), the English gloss (es-en.data) and the German gloss.
    for lemma, entry in lemmas.items():
        blocks = wikt.get(lemma, [])
        if entry["p"] == "n":
            noun = next((b for b in blocks if b["pos"] == "n"), None)
            if noun and noun["g"]:
                entry["g"] = noun["g"]
        gloss = _gloss_for(blocks, entry["p"])
        if gloss:
            entry["en"] = gloss
        gloss_de = german_gloss(lemma, direct_de.get(lemma, []), inverse_de.get(lemma, {}))
        if gloss_de:
            entry["de"] = gloss_de

    def lemma_key(lemma: str):
        rank = lemmas[lemma].get("r")
        return (rank is None, rank if rank is not None else 0, lemma)

    # Merge the conjugation forms in, then make sure a form that is itself a
    # lemma lists that lemma ("como" -> como, comer).
    for form, found in conj_forms.items():
        forms.setdefault(form, set()).update(found)
    for form in forms:
        if form in lemmas:
            forms[form].add(form)

    out_forms = {}
    for form, found in forms.items():
        ordered = sorted(found, key=lemma_key)
        if ordered != [form]:
            out_forms[form] = ordered

    tense_idx = {code: i for i, code in enumerate(TENSE_ORDER)}
    person_idx = {p: i for i, p in enumerate(PERSONS)}
    out_verbs = {}
    for form, entries in tags.items():
        ordered = sorted(entries, key=lambda e: (lemma_key(e[0]), tense_idx[e[1]], person_idx[e[2]]))
        out_verbs[form] = [list(e) for e in ordered]

    return {"version": VERSION, "lemmas": lemmas, "forms": out_forms, "verbs": out_verbs}


def write_lexicon(lexicon: dict, out: Path) -> int:
    payload = json.dumps(lexicon, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 and no embedded filename: the same inputs must give the same bytes.
    with open(out, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
            gz.write(payload)
    return out.stat().st_size


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("frequency", type=Path)
    ap.add_argument("data", type=Path)
    ap.add_argument("kaikki_es", type=Path)
    ap.add_argument("kaikki_de_es", type=Path)
    ap.add_argument("kaikki_de_de", type=Path)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    for label, path in (("frequency.csv", args.frequency), ("es-en.data", args.data),
                        ("kaikki-es.jsonl", args.kaikki_es),
                        ("kaikki-de-es.jsonl", args.kaikki_de_es),
                        ("kaikki-de-de.jsonl", args.kaikki_de_de)):
        print(f"SHA-256 {label}: {_sha256(path)}")

    lexicon = build_lexicon(args.frequency, args.data, args.kaikki_es,
                            args.kaikki_de_es, args.kaikki_de_de)
    size = write_lexicon(lexicon, args.out)
    lemmas = lexicon["lemmas"]
    tagged_lemmas = {t[0] for tags in lexicon["verbs"].values() for t in tags}
    print(f"lemmas: {len(lemmas)} "
          f"(ranked {sum(1 for v in lemmas.values() if 'r' in v)}, "
          f"with gender {sum(1 for v in lemmas.values() if 'g' in v)}, "
          f"with gloss {sum(1 for v in lemmas.values() if 'en' in v)}, "
          f"with German gloss {sum(1 for v in lemmas.values() if 'de' in v)})")
    print(f"forms (non-trivial): {len(lexicon['forms'])}")
    print(f"verb-tagged forms: {len(lexicon['verbs'])} ({len(tagged_lemmas)} verb lemmas with tags)")
    print(f"wrote {args.out} ({size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
