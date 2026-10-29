"""Build resources/es-lexicon.json.gz — the bundled Spanish form -> lemma
lexicon (#1293) — from three downloaded source files.

Usage:
    python scripts/build_es_lexicon.py <frequency.csv> <es-en.data> \
        <jehle_verb_database.csv> [--out resources/es-lexicon.json.gz]

Sources (details, licenses and checksums in resources/README.md):
  * frequency.csv  — doozan/spanish_data, OpenSubtitles frequencies grouped by
                     lemma. Gives the lemma list, its rank/pos and, in the
                     `usage` column, every surface form seen for that lemma.
  * es-en.data     — doozan/spanish_data, en.wiktionary dump. Gives noun gender
                     and a short English gloss.
  * jehle_verb_database.csv — Fred Jehle's verb tables. Gives (tense, person)
                     for 637 verbs, which the frequency file does not know.

Output (gzip, mtime=0, sorted keys -> byte-identical on every run):

    {"version": 1,
     "lemmas": {"cuenta": {"r": 812, "p": "n", "g": "f", "en": "count, tally"}},
     "forms":  {"fui": ["ser", "ir"]},
     "verbs":  {"tuve": [["tener", "ind_pret", "1s"]]}}

Design notes:
  * Everything is lowercase, lemmas included: the annotator lowercases every
    token before the lookup, so a "Madrid" lemma would never be found.
  * Rank is dense: the n-th distinct lemma in file order gets rank n (the file
    is sorted by count, and the same lemma can appear in several rows for
    different parts of speech — the first row wins).
  * `forms` omits entries whose list is exactly [form]: that is the answer
    lexicon.lemmas_of() gives for any lemma anyway, and those entries are most
    of the file.
  * Compound tenses ("he hablado") are skipped — the reader tokenises single
    words. The negative imperative is stored without its "no" ("no hables" ->
    "hables"): the bare word is what appears in running text, and the "no"
    is a separate token the annotator sees on its own.
  * Anything that is not a plain run of letters is dropped, so a stray
    "ca." or "1,5" can never become a lookup key.
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

# (Jehle mood, Jehle tense) -> our tense code, in output order.
TENSES = [
    (("Indicativo", "Presente"), "ind_pres"),
    (("Indicativo", "Pretérito"), "ind_pret"),
    (("Indicativo", "Imperfecto"), "ind_impf"),
    (("Indicativo", "Futuro"), "ind_fut"),
    (("Indicativo", "Condicional"), "ind_cond"),
    (("Subjuntivo", "Presente"), "subj_pres"),
    (("Subjuntivo", "Imperfecto"), "subj_impf"),
    (("Subjuntivo", "Futuro"), "subj_fut"),
    (("Imperativo Afirmativo", "Presente"), "imp_aff"),
    (("Imperativo Negativo", "Presente"), "imp_neg"),
]
TENSE_BY_KEY = dict(TENSES)
TENSE_ORDER = [code for _, code in TENSES] + ["ger", "part"]
PERSONS = ["", "1s", "2s", "3s", "1p", "2p", "3p"]
PERSON_COLUMNS = ["form_1s", "form_2s", "form_3s", "form_1p", "form_2p", "form_3p"]

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
    """-> (lemma_order, lemma_pos, form_to_lemmas).

    lemma_order is the distinct lemmas in first-appearance order; a lemma with
    several rows (different pos) keeps the first row's pos but contributes the
    forms of every row."""
    order: list[str] = []
    pos_of: dict[str, str] = {}
    forms: dict[str, set[str]] = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            pos = (row.get("pos") or "").strip()
            lemma = _clean_form(row.get("spanish"))
            if not lemma or pos == "none" or not pos:
                continue
            if lemma not in pos_of:
                pos_of[lemma] = pos
                order.append(lemma)
            for item in (row.get("usage") or "").split("|"):
                _, sep, raw_form = item.partition(":")
                form = _clean_form(raw_form) if sep else None
                if form:
                    forms.setdefault(form, set()).add(lemma)
    return order, pos_of, forms


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


def parse_jehle(path: Path):
    """-> (infinitives, tags) where tags maps form -> set of
    (lemma, tense_code, person). Compound (multi-word) forms are skipped."""
    infinitives: list[str] = []
    tags: dict[str, set[tuple[str, str, str]]] = {}

    def add(form_raw: str, lemma: str, tense: str, person: str, strip_no: bool = False):
        text = (form_raw or "").strip().lower()
        if strip_no and text.startswith("no "):
            text = text[3:].strip()
        form = _clean_form(text)  # also rejects anything with a space
        if form:
            tags.setdefault(form, set()).add((lemma, tense, person))

    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            lemma = _clean_form(row.get("infinitive"))
            if not lemma:
                continue
            if lemma not in infinitives:
                infinitives.append(lemma)
            code = TENSE_BY_KEY.get(((row.get("mood") or "").strip(), (row.get("tense") or "").strip()))
            if code:
                for column, person in zip(PERSON_COLUMNS, PERSONS[1:]):
                    add(row.get(column), lemma, code, person, strip_no=(code == "imp_neg"))
            add(row.get("gerund"), lemma, "ger", "")
            add(row.get("pastparticiple"), lemma, "part", "")
    return infinitives, tags


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


def build_lexicon(freq_path: Path, data_path: Path, jehle_path: Path) -> dict:
    order, pos_of, forms = parse_frequency(freq_path)
    wikt = parse_wiktionary(data_path)
    infinitives, tags = parse_jehle(jehle_path)

    lemmas: dict[str, dict] = {}
    for rank, lemma in enumerate(order, 1):
        lemmas[lemma] = {"r": rank, "p": pos_of[lemma]}
    for lemma in infinitives:
        lemmas.setdefault(lemma, {"p": "v"})

    # Gender (nouns only) and the English gloss, from wiktionary.
    for lemma, entry in lemmas.items():
        blocks = wikt.get(lemma, [])
        if entry["p"] == "n":
            noun = next((b for b in blocks if b["pos"] == "n"), None)
            if noun and noun["g"]:
                entry["g"] = noun["g"]
        gloss = _gloss_for(blocks, entry["p"])
        if gloss:
            entry["en"] = gloss

    def lemma_key(lemma: str):
        rank = lemmas[lemma].get("r")
        return (rank is None, rank if rank is not None else 0, lemma)

    # Merge Jehle's forms in, then make sure a form that is itself a lemma
    # lists that lemma ("como" -> como, comer).
    for form, entries in tags.items():
        forms.setdefault(form, set()).update(lemma for lemma, _, _ in entries)
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
    ap.add_argument("jehle", type=Path)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    for label, path in (("frequency.csv", args.frequency), ("es-en.data", args.data),
                        ("jehle_verb_database.csv", args.jehle)):
        print(f"SHA-256 {label}: {_sha256(path)}")

    lexicon = build_lexicon(args.frequency, args.data, args.jehle)
    size = write_lexicon(lexicon, args.out)
    lemmas = lexicon["lemmas"]
    print(f"lemmas: {len(lemmas)} "
          f"(ranked {sum(1 for v in lemmas.values() if 'r' in v)}, "
          f"with gender {sum(1 for v in lemmas.values() if 'g' in v)}, "
          f"with gloss {sum(1 for v in lemmas.values() if 'en' in v)})")
    print(f"forms (non-trivial): {len(lexicon['forms'])}")
    print(f"verb-tagged forms: {len(lexicon['verbs'])}")
    print(f"wrote {args.out} ({size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
