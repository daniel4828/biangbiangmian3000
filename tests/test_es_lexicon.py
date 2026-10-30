"""
Tests for the bundled Spanish form -> lemma lexicon (#1293): the offline build
script, the lexicon.py lookups, and the romance annotator's use of both.

Nothing here touches the network or the real database: the lexicon is either
built from tiny fixture files in tmp_path or injected as a dict, and the
annotator's database / translation calls are stubbed.
"""

import gzip
import importlib.util
import json
from pathlib import Path

import pytest

import lexicon
from annotate import baseline, romance

ROOT = Path(__file__).resolve().parents[1]


def _load_build_script():
    spec = importlib.util.spec_from_file_location("build_es_lexicon", ROOT / "scripts" / "build_es_lexicon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_script = _load_build_script()


@pytest.fixture(autouse=True)
def _fresh_lexicon_cache():
    lexicon.clear_cache()
    yield
    lexicon.clear_cache()


# ---------------------------------------------------------------------------
# Build script
# ---------------------------------------------------------------------------

FREQUENCY = """count,spanish,pos,flags,usage
100,ser,v,,50:es|40:ser|30:fui|20:fue
90,ir,v,,45:ir|35:fui|25:va
80,comer,v,,40:como|30:comer
70,como,adv,,60:como
60,cuenta,n,,30:cuenta|20:cuentas
50,cuenta,v,,5:cuenta|4:cuentan
40,basura,none,,10:zzz
30,hablar,v,,3:habló|2:ca.
20,tener,v,,2:tengo
"""

WIKT = """cuenta
pos: n
  meta: {{es-noun|f}}
  g: f
  gloss: count, tally
  gloss: operation
    q: mathematics
  gloss: bill
  gloss: account
_____
cuenta atrás
pos: n
  g: f
  gloss: countdown
_____
hablar
pos: v
  gloss: to speak
  gloss: to talk
"""

def _f(form, tags, source="conjugation"):
    item = {"form": form, "tags": tags}
    if source:
        item["source"] = source
    return item


# kaikki.org en.wiktionary Spanish extract, trimmed to what the build reads.
KAIKKI_ES = [
    {"word": "hablar", "pos": "verb", "forms": [
        _f("hablo", ["first-person", "present", "singular"], source=None),   # head template: ignored
        _f("no-table-tags", ["table-tags"]),
        _f("es-conj", ["inflection-template"]),
        _f("hablar", ["infinitive"]),
        _f("hablando", ["gerund"]),
        _f("hablado", ["masculine", "participle", "past", "singular"]),
        _f("hablada", ["feminine", "participle", "past", "singular"]),
        _f("hablados", ["masculine", "participle", "past", "plural"]),
        _f("hablamos", ["first-person", "indicative", "plural", "present"]),
        _f("hablamos", ["first-person", "indicative", "plural", "preterite"]),
        _f("hablaría", ["conditional", "first-person", "indicative", "singular"]),
        _f("hable", ["first-person", "present", "singular", "subjunctive"]),
        _f("hable", ["present", "singular", "subjunctive", "third-person"]),
        _f("hable", ["formal", "imperative", "second-person-semantically", "singular", "third-person"]),
        _f("habla", ["imperative", "second-person", "singular"]),
        _f("no hables", ["imperative", "negative", "second-person", "singular"]),
        _f("hablara", ["first-person", "imperfect", "singular", "subjunctive"]),
        _f("hablase", ["first-person", "imperfect-se", "singular", "subjunctive"]),
        _f("hablás", ["indicative", "present", "second-person", "singular", "vos-form"]),
        _f("háblame", ["combined-form", "imperative", "second-person", "singular"]),
        _f("hablá", ["imperative", "second-person", "singular", "with-vos"]),
    ]},
    # A second etymology of the same word merges into the first.
    {"word": "hablar", "pos": "verb", "forms": [
        _f("hablaré", ["first-person", "future", "indicative", "singular"]),
    ]},
    {"word": "hablar", "pos": "noun", "forms": [_f("hablares", ["plural"])]},
    {"word": "ir", "pos": "verb", "forms": [
        _f("fui", ["first-person", "indicative", "preterite", "singular"]),
    ]},
    {"word": "ser", "pos": "verb", "forms": [
        _f("fui", ["first-person", "indicative", "preterite", "singular"]),
    ]},
    {"word": "tener", "pos": "verb", "forms": [
        _f("tuve", ["first-person", "indicative", "preterite", "singular"]),
        _f("tuviese", ["first-person", "imperfect-se", "singular", "subjunctive"]),
    ]},
    # "cuenta" is a noun first in the frequency file but has a `v` row too.
    {"word": "cuenta", "pos": "verb", "forms": [
        _f("cuentan", ["indicative", "plural", "present", "third-person"]),
    ]},
    # Not in the frequency file: ignored.
    {"word": "andar", "pos": "verb", "forms": [
        _f("anduve", ["first-person", "indicative", "preterite", "singular"]),
    ]},
]

# kaikki.org de.wiktionary Spanish extract.
KAIKKI_DE_ES = [
    {"word": "hablar", "pos": "verb", "lang_code": "es", "translations": [
        {"word": "sprechen", "lang_code": "de"}, {"word": "reden", "lang_code": "de"},
        {"word": "speak", "lang_code": "en"}], "senses": [{"glosses": ["ignored, there are translations"]}]},
    # No translations: the first gloss of each sense.
    {"word": "tener", "pos": "verb", "lang_code": "es", "senses": [
        {"glosses": ["haben"]}, {"glosses": ["besitzen", "nicht das"]}]},
    {"word": "ser", "pos": "verb", "lang_code": "es", "translations": [
        {"word": "sein", "lang_code": "de"}]},
    {"word": "andar", "pos": "verb", "lang_code": "es", "translations": [
        {"word": "gehen", "lang_code": "de"}]},
    # Inflected-form entry + form-of sense next to a real one: never a gloss.
    {"word": "cuenta", "pos": "verb", "lang_code": "es", "senses": [
        {"tags": ["active", "form-of"], "form_of": [{"word": "contar"}],
         "glosses": ["3. Person Singular Indikativ Präsens Aktiv des Verbs contar"]}]},
    {"word": "comer", "pos": "verb", "lang_code": "es", "senses": [
        {"tags": ["form-of"], "glosses": ["Form des Verbs"]},
        {"glosses": ["essen"]}]},
]


def _de(word, *spanish):
    return {"word": word, "pos": "noun", "lang_code": "de",
            "translations": [{"word": s, "lang_code": "es"} for s in spanish]
                            + [{"word": "x", "lang_code": "fr"}]}


# kaikki.org de.wiktionary German extract (inverted by the build).
KAIKKI_DE_DE = [
    _de("sprechen", "hablar"), _de("reden", "hablar"),
    _de("plaudern", "hablar"), _de("plaudern", "hablar"),
    _de("Haben", "tener"), _de("Halten", "tener"), _de("Halten", "tener"), _de("Tragen", "tener"),
    _de("Konto", "cuenta"), _de("Rechnung", "cuenta"), _de("Rechnung", "cuenta"), _de("Rechnung", "cuenta"),
    _de("Sprache", "habla"),               # not a lemma of the frequency file: ignored
]


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def sources(tmp_path):
    freq = tmp_path / "frequency.csv"
    freq.write_text(FREQUENCY, encoding="utf-8")
    data = tmp_path / "es-en.data"
    data.write_text(WIKT, encoding="utf-8")
    return (freq, data,
            _write_jsonl(tmp_path / "kaikki-es.jsonl", KAIKKI_ES),
            _write_jsonl(tmp_path / "kaikki-de-es.jsonl", KAIKKI_DE_ES),
            _write_jsonl(tmp_path / "kaikki-de-de.jsonl", KAIKKI_DE_DE))


def test_build_lemmas(sources):
    lex = build_script.build_lexicon(*sources)
    lemmas = lex["lemmas"]
    # Dense rank by first appearance; the second "cuenta" row (pos v) loses.
    assert lemmas["ser"] == {"r": 1, "p": "v", "de": "sein"}
    assert lemmas["cuenta"] == {"r": 5, "p": "n", "g": "f", "en": "count, tally; operation; bill",
                                "de": "Rechnung, Konto"}
    assert lemmas["hablar"]["en"] == "to speak; to talk"
    assert "g" not in lemmas["hablar"]
    # pos "none" rows are dropped entirely.
    assert "basura" not in lemmas and "zzz" not in lex["forms"]
    # Lemmas outside the frequency file (kaikki-only) are not added.
    assert "andar" not in lemmas
    assert lemmas["tener"] == {"r": 7, "p": "v", "de": "haben, besitzen, Halten"}
    # No German source at all -> no "de" key.
    assert "de" not in lemmas["ir"]


def test_german_gloss_direct_first_then_inverse_by_count(sources):
    lemmas = build_script.build_lexicon(*sources)["lemmas"]
    # Direct translations first, inverse ones (plaudern x2) fill up to three;
    # an inverse duplicate of a direct gloss (sprechen) is not repeated.
    assert lemmas["hablar"]["de"] == "sprechen, reden, plaudern"
    # Direct from sense glosses; "Haben" is a case-insensitive duplicate of "haben".
    assert lemmas["tener"]["de"] == "haben, besitzen, Halten"
    # Inverse only, by count descending; the pure inflection entry adds nothing.
    assert lemmas["cuenta"]["de"] == "Rechnung, Konto"
    # form-of sense skipped, the real sense of the same word kept.
    assert lemmas["comer"]["de"] == "essen"


def test_german_gloss_limits_and_skips_the_lemma():
    gloss = build_script.german_gloss
    assert gloss("x", ["a", "b", "c", "d"], {"e": 9}) == "a, b, c"
    assert gloss("casa", ["Casa", "Haus"], {"CASA": 3, "haus": 1}) == "Haus"
    assert gloss("casa", [], {}) is None
    assert len(gloss("x", ["a" * 200], {})) <= build_script.MAX_GLOSS_CHARS


def test_gloss_dedupes_and_never_falls_back_to_proper_noun(tmp_path):
    data = tmp_path / "d.data"
    data.write_text(
        "tener\npos: v\n  gloss: to have; to possess\n"
        "pos: v\n  gloss: to have; to possess; to be (a condition or quality)\n  gloss: to hold\n"
        "_____\n"
        "como\npos: prop\n  gloss: Como (a city)\npos: adv\n  gloss: as\n"
        "_____\n"
        "solo\npos: prop\n  gloss: Solo (a place)\n"
        "_____\n"
        "Madrid\npos: prop\n  gloss: capital of Spain\n",
        encoding="utf-8")
    blocks = build_script.parse_wiktionary(data)
    assert build_script._gloss_for(blocks["tener"], "v") == "to have; to possess; to be (a condition or quality)"
    # conj has no block of its own -> first non-prop block, not the city.
    assert build_script._gloss_for(blocks["como"], "conj") == "as"
    # Only prop blocks and a non-prop lemma -> no gloss at all.
    assert build_script._gloss_for(blocks["solo"], "adv") is None
    assert build_script._gloss_for(blocks["madrid"], "prop") == "capital of Spain"


def test_build_forms(sources):
    forms = build_script.build_lexicon(*sources)["forms"]
    # Ambiguous form: ordered by lemma rank.
    assert forms["fui"] == ["ser", "ir"]
    # A form that is itself a lemma lists that lemma too.
    assert forms["como"] == ["comer", "como"]
    # Wiktionary-only form maps to its infinitive.
    assert forms["tuve"] == ["tener"]
    assert forms["cuentas"] == ["cuenta"]
    # Untagged variants are still known forms: -se subjunctive, other participles.
    assert forms["hablase"] == ["hablar"] and forms["tuviese"] == ["tener"]
    assert forms["hablada"] == ["hablar"] and forms["hablados"] == ["hablar"]
    # Voseo / clitic-combined forms and head-template forms are skipped entirely.
    for skipped in ("hablás", "hablá", "háblame", "hablo", "anduve", "hablares"):
        assert skipped not in forms
    # Trivial [form] entries are dropped (implied at lookup time) ...
    for trivial in ("ser", "ir", "comer", "hablar"):
        assert trivial not in forms
    # ... and non-words never become keys.
    assert "ca." not in forms


def test_build_verbs(sources):
    verbs = build_script.build_lexicon(*sources)["verbs"]
    assert ["tener", "ind_pret", "1s"] in verbs["tuve"]
    # Two lemmas share a form: ordered by lemma rank (ser 1, ir 2).
    assert verbs["fui"] == [["ser", "ind_pret", "1s"], ["ir", "ind_pret", "1s"]]
    # Same surface form, two readings, tense order then person order.
    assert verbs["hablamos"] == [["hablar", "ind_pres", "1p"], ["hablar", "ind_pret", "1p"]]
    assert verbs["hablando"] == [["hablar", "ger", ""]]
    assert verbs["hablado"] == [["hablar", "part", ""]]
    assert verbs["hablaría"] == [["hablar", "ind_cond", "1s"]]
    assert verbs["hablaré"] == [["hablar", "ind_fut", "1s"]]       # merged second entry
    assert verbs["hablara"] == [["hablar", "subj_impf", "1s"]]
    # Subjunctive and the formal imperative (-> 3s) share "hable".
    assert verbs["hable"] == [["hablar", "subj_pres", "1s"], ["hablar", "subj_pres", "3s"],
                              ["hablar", "imp_aff", "3s"]]
    assert verbs["habla"] == [["hablar", "imp_aff", "2s"]]
    # Negative imperative is stored without its "no".
    assert verbs["hables"] == [["hablar", "imp_neg", "2s"]]
    # A verb whose first frequency row is a noun still gets tags.
    assert verbs["cuentan"] == [["cuenta", "ind_pres", "3p"]]
    # Never tagged: -se variants, feminine/plural participles, voseo, clitics,
    # head-template forms, lemmas outside the frequency file, infinitives.
    for untagged in ("hablase", "tuviese", "hablada", "hablados", "hablás", "hablá",
                     "háblame", "hablo", "anduve", "hablar"):
        assert untagged not in verbs
    assert verbs["tuve"] == [["tener", "ind_pret", "1s"]]
    assert all(" " not in form for form in verbs)


def test_build_output_is_deterministic(sources, tmp_path):
    out_a, out_b = tmp_path / "a.json.gz", tmp_path / "b.json.gz"
    for out in (out_a, out_b):
        build_script.write_lexicon(build_script.build_lexicon(*sources), out)
    assert out_a.read_bytes() == out_b.read_bytes()
    with gzip.open(out_a, "rt", encoding="utf-8") as f:
        assert json.load(f)["version"] == 1


def test_build_cli_writes_file(sources, tmp_path, capsys):
    out = tmp_path / "out" / "lex.json.gz"
    assert build_script.main([*map(str, sources), "--out", str(out)]) == 0
    assert out.exists()
    assert "SHA-256" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# lexicon.py
# ---------------------------------------------------------------------------

LEX = {
    "lemmas": {
        "ser": {"r": 1, "p": "v", "en": "to be"},
        "ir": {"r": 2, "p": "v", "en": "to go"},
        "cuenta": {"r": 5, "p": "n", "g": "f", "en": "count"},
        "contar": {"r": 9, "p": "v", "en": "to count"},
        "hablar": {"r": 6, "p": "v", "en": "to speak"},
        "viajar": {"r": 700, "p": "v", "en": "to travel"},
        "casa": {"r": 3, "p": "n", "g": "f"},
        "comer": {"r": 50, "p": "v", "en": "to eat", "de": "essen"},
    },
    "forms": {
        "comemos": ["comer"],
        "fui": ["ser", "ir"],
        "cuentas": ["cuenta", "contar"],
        "hablamos": ["hablar"],
        "hablando": ["hablar"],
        "viajamos": ["viajar"],
    },
    "verbs": {
        "fui": [["ser", "ind_pret", "1s"], ["ir", "ind_pret", "1s"]],
        "hablamos": [["hablar", "ind_pres", "1p"], ["hablar", "ind_pret", "1p"]],
        "viajamos": [["viajar", "ind_pres", "1p"]],
        "hablando": [["hablar", "ger", ""]],
    },
}


@pytest.fixture
def es_lexicon():
    lexicon.inject("es", LEX)
    return LEX


def test_lemmas_of(es_lexicon):
    assert lexicon.available("es")
    assert lexicon.lemmas_of("Fui", "es") == ["ser", "ir"]
    assert lexicon.lemmas_of("casa", "es") == ["casa"]     # plain lemma, not in `forms`
    assert lexicon.lemmas_of("xyzzy", "es") == []


def test_lemma_info_and_verb_tags(es_lexicon):
    assert lexicon.lemma_info("cuenta", "es") == {"rank": 5, "pos": "n", "gender": "f", "gloss_en": "count",
                                                  "gloss_de": None}
    assert lexicon.lemma_info("comer", "es")["gloss_de"] == "essen"
    assert lexicon.lemma_info("casa", "es")["gloss_en"] is None
    assert lexicon.lemma_info("nada", "es") is None
    assert lexicon.verb_tags("hablamos", "es") == [("hablar", "ind_pres", "1p"), ("hablar", "ind_pret", "1p")]
    assert lexicon.verb_tags("casa", "es") == []


def test_grammar_label(es_lexicon):
    assert lexicon.grammar_label("fui", "es") == "ser/ir · Indefinido · 1. Sg."
    assert lexicon.grammar_label("hablamos", "es") == "hablar · Presente · 1. Pl.; Indefinido · 1. Pl."
    assert lexicon.grammar_label("hablando", "es") == "hablar · Gerundio"
    # Lemma differs, no verb tags: just the lemma list.
    assert lexicon.grammar_label("cuentas", "es") == "cuenta/contar"
    # A plain lemma says nothing.
    assert lexicon.grammar_label("casa", "es") is None
    assert lexicon.grammar_label("xyzzy", "es") is None


def test_missing_file_is_empty_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(lexicon, "_RESOURCE_DIR", tmp_path)
    lexicon.clear_cache()
    assert lexicon.available("es") is False
    assert lexicon.lemmas_of("fui", "es") == []
    assert lexicon.lemma_info("ir", "es") is None
    assert lexicon.verb_tags("fui", "es") == []
    assert lexicon.grammar_label("fui", "es") is None


def test_corrupt_file_is_empty_not_an_error(monkeypatch, tmp_path):
    (tmp_path / "es-lexicon.json.gz").write_bytes(b"this is not gzip")
    monkeypatch.setattr(lexicon, "_RESOURCE_DIR", tmp_path)
    lexicon.clear_cache()
    assert lexicon.available("es") is False
    assert lexicon.lemmas_of("fui", "es") == []


def test_loads_a_built_file(sources, tmp_path, monkeypatch):
    out = tmp_path / "res" / "es-lexicon.json.gz"
    build_script.write_lexicon(build_script.build_lexicon(*sources), out)
    monkeypatch.setattr(lexicon, "_RESOURCE_DIR", out.parent)
    lexicon.clear_cache()
    assert lexicon.lemmas_of("fui", "es") == ["ser", "ir"]
    assert lexicon.grammar_label("tuve", "es") == "tener · Indefinido · 1. Sg."


def test_shipped_lexicon_smoke():
    if not (ROOT / "resources" / "es-lexicon.json.gz").exists():
        pytest.skip("resources/es-lexicon.json.gz not built")
    assert {"ir", "ser"} <= set(lexicon.lemmas_of("fui", "es"))
    assert ("tener", "ind_pret", "1s") in lexicon.verb_tags("tuve", "es")


# ---------------------------------------------------------------------------
# Romance annotator
# ---------------------------------------------------------------------------

@pytest.fixture
def annotator(monkeypatch, es_lexicon):
    """romance with the database and translation stubbed. `state` lets a test
    choose what counts as studied (forms) / marked known / baseline; `calls`
    records the strings handed to the German gloss lookup."""
    state = {"forms": set(), "known": set(), "baseline": frozenset()}
    calls = []
    monkeypatch.setattr(romance.database, "forms_lookup",
                        lambda words, lang: set(words) & state["forms"])
    monkeypatch.setattr(romance.database, "known_words_exists",
                        lambda words, lang: set(words) & state["known"])
    monkeypatch.setattr(baseline, "baseline_words", lambda lang: state["baseline"])

    def fake_glosses(words, lang):
        calls.append(list(words))
        return {w: f"DE:{w}" for w in words}

    monkeypatch.setattr(romance, "_glosses", fake_glosses)
    state["calls"] = calls
    return state


def _new(text, lang="es"):
    return {w["word"]: w for w in romance.annotate_summary(text, lang)[1]}


def test_inflection_of_a_studied_lemma_is_not_new(annotator):
    annotator["forms"] = {"hablar"}          # only the lemma was ever stored
    assert "hablamos" not in _new("Ayer hablamos y viajamos.")
    assert "viajamos" in _new("Ayer hablamos y viajamos.")


def test_inflection_of_a_known_or_baseline_lemma_is_not_new(annotator):
    annotator["known"] = {"hablar"}
    assert "hablamos" not in _new("Ayer hablamos y viajamos.")
    annotator["known"] = set()
    annotator["baseline"] = frozenset({"hablar"})
    assert "hablamos" not in _new("Ayer hablamos y viajamos.")


def test_ambiguous_form_is_known_if_any_lemma_is(annotator):
    assert "cuentas" in _new("Las cuentas.")
    annotator["known"] = {"contar"}
    assert "cuentas" not in _new("Las cuentas.")


def test_unknown_verb_form_carries_lemma_grammar_and_lemma_gloss(annotator):
    word = _new("Ayer viajamos mucho.")["viajamos"]
    assert word["lemma"] == "viajar"
    assert word["lemmas"] == ["viajar"]
    assert word["grammar"] == "viajar · Presente · 1. Pl."
    assert word["definition"] == "to travel"
    # German gloss was requested for the lemma, not the surface form.
    assert word["definition_de"] == "DE:viajar"
    asked = [w for call in annotator["calls"] for w in call]
    assert "viajar" in asked and "viajamos" not in asked


def test_lexicon_german_gloss_skips_google(annotator):
    """#1305: an unambiguous lemma with a German gloss in the lexicon uses it;
    only the remaining queries reach the translator."""
    word = _new("Comemos y viajamos.")["comemos"]
    assert word["lemma"] == "comer"
    assert word["definition_de"] == "essen"
    asked = [w for call in annotator["calls"] for w in call]
    assert "comer" not in asked and "comemos" not in asked and "viajar" in asked
    # Nothing left to translate -> no Google call at all.
    annotator["calls"].clear()
    assert _new("Comemos.")["comemos"]["definition_de"] == "essen"
    assert annotator["calls"] == []


def test_ambiguous_form_still_goes_to_google_despite_lexicon_gloss(annotator):
    LEX["lemmas"]["cuenta"]["de"] = "Rechnung"
    try:
        lexicon.inject("es", LEX)
        word = _new("Las cuentas.")["cuentas"]
        assert word["definition_de"] == "DE:cuentas"
    finally:
        del LEX["lemmas"]["cuenta"]["de"]
        lexicon.inject("es", LEX)


def test_ambiguous_form_keeps_surface_form_for_lemma_and_gloss(annotator):
    word = _new("Las cuentas.")["cuentas"]
    assert word["lemma"] == "cuentas"
    assert word["lemmas"] == ["cuenta", "contar"]
    assert word["definition_de"] == "DE:cuentas"
    assert word["grammar"] == "cuenta/contar"


def test_all_words_is_enriched_too(annotator):
    annotator["forms"] = {"hablar"}
    words = {w["word"]: w for w in romance.all_words("Ayer hablamos y viajamos.", "es")}
    assert words["hablamos"]["lemma"] == "hablar"
    assert words["hablamos"]["definition_de"] == "DE:hablar"
    assert words["viajamos"]["grammar"].startswith("viajar")
    # One batched translate call over the de-duplicated lemma/surface strings.
    assert len(annotator["calls"]) == 1


def test_french_is_unchanged_without_a_lexicon(annotator):
    """No fr lexicon file: no lemma/grammar keys, lemma == word, gloss asked
    for the surface form — the pre-#1293 result exactly."""
    assert lexicon.available("fr") is False
    new = _new("Cela va amoindrir le risque.", lang="fr")
    assert new["amoindrir"] == {"word": "amoindrir", "lemma": "amoindrir", "definition_de": "DE:amoindrir"}
    words = romance.all_words("Cela va amoindrir le risque.", "fr")
    assert all(set(w) == {"word", "lemma", "definition_de"} and w["lemma"] == w["word"] for w in words)


# ---------------------------------------------------------------------------
# routes.knowledge._with_entry_ids
# ---------------------------------------------------------------------------

def test_entry_ids_fall_back_to_the_lemma(monkeypatch):
    from routes import knowledge

    calls = []

    def fake_ids(words, lang):
        calls.append(list(words))
        return {"hablar": 7}

    monkeypatch.setattr(knowledge.database, "entry_ids_for_forms", fake_ids)
    words = [{"word": "hablamos", "lemma": "hablar"},
             {"word": "casa", "lemma": "casa"},
             {"word": "cuentas", "lemma": "cuentas"}]
    out = knowledge._with_entry_ids(words, "es")
    assert out[0]["word_id"] == 7
    assert "word_id" not in out[1] and "word_id" not in out[2]
    assert calls[1] == ["hablar"]      # second batch: only words whose lemma differs
