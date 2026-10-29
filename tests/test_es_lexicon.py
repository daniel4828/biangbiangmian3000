"""
Tests for the bundled Spanish form -> lemma lexicon (#1293): the offline build
script, the lexicon.py lookups, and the romance annotator's use of both.

Nothing here touches the network or the real database: the lexicon is either
built from tiny fixture files in tmp_path or injected as a dict, and the
annotator's database / translation calls are stubbed.
"""

import csv
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

JEHLE_HEADER = ["infinitive", "infinitive_english", "mood", "mood_english", "tense", "tense_english",
                "verb_english", "form_1s", "form_2s", "form_3s", "form_1p", "form_2p", "form_3p",
                "gerund", "gerund_english", "pastparticiple", "pastparticiple_english"]


def _jehle_row(inf, mood, tense, forms, gerund="", part=""):
    return [inf, "to x", mood, "m", tense, "t", "v", *forms, gerund, "g", part, "p"]


JEHLE_ROWS = [
    _jehle_row("hablar", "Indicativo", "Presente", ["hablo", "hablas", "habla", "hablamos", "habláis", "hablan"],
               "hablando", "hablado"),
    _jehle_row("hablar", "Indicativo", "Pretérito", ["hablé", "hablaste", "habló", "hablamos", "hablasteis", "hablaron"],
               "hablando", "hablado"),
    _jehle_row("hablar", "Indicativo", "Presente perfecto", ["he hablado", "has hablado", "ha hablado",
                                                              "hemos hablado", "", ""]),
    _jehle_row("hablar", "Imperativo Negativo", "Presente", ["", "no hables", "no hable", "no hablemos", "", ""]),
    _jehle_row("ir", "Indicativo", "Pretérito", ["fui", "fuiste", "fue", "fuimos", "fuisteis", "fueron"]),
    _jehle_row("tener", "Indicativo", "Pretérito", ["tuve", "tuviste", "tuvo", "tuvimos", "tuvisteis", "tuvieron"]),
]


@pytest.fixture
def sources(tmp_path):
    freq = tmp_path / "frequency.csv"
    freq.write_text(FREQUENCY, encoding="utf-8")
    data = tmp_path / "es-en.data"
    data.write_text(WIKT, encoding="utf-8")
    jehle = tmp_path / "jehle.csv"
    with open(jehle, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(JEHLE_HEADER)
        writer.writerows(JEHLE_ROWS)
    return freq, data, jehle


def test_build_lemmas(sources):
    lex = build_script.build_lexicon(*sources)
    lemmas = lex["lemmas"]
    # Dense rank by first appearance; the second "cuenta" row (pos v) loses.
    assert lemmas["ser"] == {"r": 1, "p": "v"}
    assert lemmas["cuenta"] == {"r": 5, "p": "n", "g": "f", "en": "count, tally; operation; bill"}
    assert lemmas["hablar"]["en"] == "to speak; to talk"
    assert "g" not in lemmas["hablar"]
    # pos "none" rows are dropped entirely.
    assert "basura" not in lemmas and "zzz" not in lex["forms"]
    # Jehle infinitives missing from the frequency file: no rank, pos v.
    assert lemmas["tener"] == {"p": "v"}


def test_gloss_dedupes_and_never_falls_back_to_proper_noun(tmp_path):
    data = tmp_path / "d.data"
    data.write_text(
        "tener\npos: v\n  gloss: to have\n  gloss: to possess\n"
        "pos: v\n  gloss: to have\n  gloss: to possess\n  gloss: to hold\n  gloss: to hold on\n"
        "_____\n"
        "como\npos: prop\n  gloss: Como (a city)\npos: adv\n  gloss: as\n"
        "_____\n"
        "solo\npos: prop\n  gloss: Solo (a place)\n"
        "_____\n"
        "Madrid\npos: prop\n  gloss: capital of Spain\n",
        encoding="utf-8")
    blocks = build_script.parse_wiktionary(data)
    assert build_script._gloss_for(blocks["tener"], "v") == "to have; to possess; to hold"
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
    # Jehle-only form maps to its infinitive.
    assert forms["tuve"] == ["tener"]
    assert forms["cuentas"] == ["cuenta"]
    # Trivial [form] entries are dropped (implied at lookup time) ...
    for trivial in ("ser", "ir", "comer", "hablar"):
        assert trivial not in forms
    # ... and non-words never become keys.
    assert "ca." not in forms


def test_build_verbs(sources):
    verbs = build_script.build_lexicon(*sources)["verbs"]
    assert ["tener", "ind_pret", "1s"] in verbs["tuve"]
    assert verbs["fui"] == [["ir", "ind_pret", "1s"]]
    # Same surface form, two readings, tense order then person order.
    assert verbs["hablamos"] == [["hablar", "ind_pres", "1p"], ["hablar", "ind_pret", "1p"]]
    assert verbs["hablando"] == [["hablar", "ger", ""]]
    assert verbs["hablado"] == [["hablar", "part", ""]]
    # Negative imperative is stored without its "no"; compound tenses are skipped.
    assert verbs["hables"] == [["hablar", "imp_neg", "2s"]]
    assert "he" not in verbs and "hemos" not in verbs and "hablado" in verbs
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
    },
    "forms": {
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
    assert lexicon.lemma_info("cuenta", "es") == {"rank": 5, "pos": "n", "gender": "f", "gloss_en": "count"}
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
