"""Language registry — the single source of truth for per-language behavior.

Every language the app supports gets one entry here. Adding a new language
means adding one dict entry (plus an importer YAML format if it needs one) —
no other module should hard-code language-specific values.

Consumers (wired up across PRs #428–#431, #803):
  - tts.py             → tts_voice / say_voice
  - translator.py      → translator_source
  - routes/story.py    → tokenizer (jieba vs. whitespace)
  - ai.py               → prompt fragments (language_name, learner_level,
                          background_vocab, sentence_limit)
  - static/app.js       → features (which UI elements to show per deck)
  - zh_annotate.py       → annotator (which knowledge-base annotation
                          implementation a language uses; #803)
  - database/entries.py  → features.conjugation / .gender / .inflection
                          (whether entry_forms rows apply to this language)

Language family (issue #803)
-----------------------------
Chinese is structurally unlike anything else the app supports; French and
Spanish are structurally alike (and any future Romance language will be too).
Rather than repeat every shared field per language, each family has a base
dict (`_SINITIC_BASE` / `_ROMANCE_BASE`) that concrete languages spread into
their own entry (`{**_ROMANCE_BASE, "code": "fr", ...}`). `features` is
merged explicitly too — never let a child language's dict silently replace
the whole features block when it only means to override one flag.
"""

DEFAULT_LANG = "zh"

# ---------------------------------------------------------------------------
# Family bases — shared defaults for every language in that family. Concrete
# language entries below spread these in, then override the fields that make
# that specific language distinct (name, voice, level system, ...).
# ---------------------------------------------------------------------------

_SINITIC_BASE = {
    "family": "sinitic",
    # Which knowledge-base annotation implementation this language uses
    # (zh_annotate.py's zero-AI HSK-table + jieba + pypinyin pipeline is
    # Chinese-specific; Romance languages get a different implementation).
    "annotator": "zh",
    "tokenizer": "jieba",
    "features": {
        "pinyin": True,
        "characters": True,        # per-character breakdown (汉字)
        "measure_words": True,     # 量词
        "traditional": True,
        # kahneman/paste/contextsummary story modes are zh-only for now
        "extended_story_modes": True,
        # Knowledge mode (build cards from a saved podcast/video/article
        # source) is language-agnostic — the source material's language
        # doesn't matter, only the prompt's target language does (issue
        # #806). Kept as its own flag, separate from extended_story_modes,
        # so it isn't accidentally hidden alongside kahneman/paste/contextsummary.
        "knowledge_story_mode": True,
        # Morphology (issue #803): whether entry_forms rows apply.
        "conjugation": False,
        "gender": False,
        "inflection": False,
    },
}

_ROMANCE_BASE = {
    "family": "romance",
    "annotator": "romance",
    "tokenizer": "whitespace",
    "features": {
        "pinyin": False,
        "characters": False,
        "measure_words": False,
        "traditional": False,
        "extended_story_modes": False,
        "knowledge_story_mode": True,
        # Morphology (issue #803): Romance languages conjugate verbs, mark
        # noun/adjective gender, and inflect for number/gender — all stored
        # in entry_forms (see docs/multilang.md).
        "conjugation": True,
        "gender": True,
        "inflection": True,
    },
}

LANGUAGES = {
    "zh": {
        **_SINITIC_BASE,
        "code": "zh",
        "name_en": "Mandarin Chinese",     # language name used inside AI prompts
        "name_native": "中文",
        # ── TTS ──
        "tts_voice": "zh-CN-XiaoxiaoNeural",   # edge-tts voice
        "say_voice": "Tingting",               # macOS `say` fallback voice
        # ── Translation (deep-translator source code) ──
        "translator_source": "zh-CN",
        # ── Deck tree root for words added through the UI (issue #726) ──
        # Each language owns a parallel tree under 'All' whose decks carry that
        # lang, because every language filter in the app keys off decks.lang —
        # a French card in a zh deck is invisible under the fr tab and shows up
        # in the Chinese review queue instead.
        "deck_root": "Daily",
        # ── AI prompt fragments ──
        "level_system": "HSK",
        "learner_level": "HSK 4-5",            # the learner's level
        "background_vocab": "HSK 1-2",         # default level cap for non-target words
        "sentence_limit": "15 Chinese characters",
        # ── Language-specific features (drive schema usage + frontend UI) ──
        "features": {**_SINITIC_BASE["features"]},
    },
    "fr": {
        **_ROMANCE_BASE,
        "code": "fr",
        "name_en": "French",
        "name_native": "français",
        "tts_voice": "fr-FR-DeniseNeural",
        "say_voice": "Thomas",
        "translator_source": "fr",
        "deck_root": "Français",
        "level_system": "CEFR",
        "learner_level": "CEFR B1",            # Daniel's French level (2026-07-06)
        "background_vocab": "CEFR A1-A2",
        "sentence_limit": "12 words",
        "features": {**_ROMANCE_BASE["features"]},
    },
    "es": {
        **_ROMANCE_BASE,
        "code": "es",
        "name_en": "Spanish",
        "name_native": "Español",
        "tts_voice": "es-ES-ElviraNeural",
        "say_voice": "Mónica",
        "translator_source": "es",
        "deck_root": "Español",
        "level_system": "CEFR",
        "learner_level": "CEFR A2",
        "background_vocab": "CEFR A1",
        "sentence_limit": "12 words",
        # Definite article by noun gender, shown in front of the headword in the
        # review card back and the word detail page (#1294: "la clima" errors
        # in the placement test). French is deliberately absent: it needs
        # elision (l'eau) and is out of scope for now.
        "articles": {"m": "el", "f": "la"},
        # Verb slot rotation (#1295): story sentences for verb cards must use a
        # specific (tense, person) form. Unlock order: Presente -> Indefinido ->
        # Imperfecto -> Perfecto -> Futuro (append "ind_fut" later), Subjuntivo
        # later still. "perf" is composed (haber + participle). French has no
        # verb_slots -> feature off.
        "verb_slots": {
            "tenses": ["ind_pres", "ind_pret", "ind_impf", "perf"],
            "persons": ["1s", "2s", "3s", "1p", "2p", "3p"],
        },
        "features": {**_ROMANCE_BASE["features"]},
    },
}

# Spanish feminine nouns that begin with a stressed /a/ take "el" in the
# singular ("el agua", but "las aguas" and "la otra agua"). Explicit list on
# purpose: the rule depends on stress, which can't be derived from spelling
# alone (la aguja, la ambulancia are unstressed and keep "la"). Only the
# unambiguous classics are listed; a word missing here just shows "la".
_ES_EL_FEMININE = frozenset({
    "agua", "águila", "alma", "arma", "área", "hambre", "hacha", "aula",
    "ala", "ave", "haba", "hada", "asma", "ancla", "arpa", "alba", "ánima",
})


def get_lang_config(lang: str | None) -> dict:
    """Return the config for `lang`, falling back to the default language.

    Unknown/legacy values fall back to zh so old rows can never crash the app.
    """
    return LANGUAGES.get(lang or DEFAULT_LANG, LANGUAGES[DEFAULT_LANG])


def is_valid_lang(lang: str | None) -> bool:
    return lang in LANGUAGES


def article_for(word: str | None, gender: str | None, lang: str | None) -> str | None:
    """Definite article to show before a noun headword (#1294), or None.

    None when the language has no "articles" table, the gender is empty/unknown
    or the word is empty. "m"/"f" give the matching article; Spanish feminine
    nouns on the _ES_EL_FEMININE list take "el" (el agua). Any gender naming
    both ("mf", "m/f", "mfbysense") gives "el/la". Never raises: this runs on
    every word detail response and must not be able to break it.
    """
    try:
        articles = get_lang_config(lang).get("articles")
        g = (gender or "").strip().lower()
        w = (word or "").strip()
        if not articles or not g or not w:
            return None
        has_m, has_f = "m" in g, "f" in g
        if has_m and has_f:
            return f"{articles['m']}/{articles['f']}"
        if has_m:
            return articles["m"]
        if has_f:
            if (lang == "es" and w.lower() in _ES_EL_FEMININE):
                return articles["m"]
            return articles["f"]
        return None
    except Exception:
        return None


def deck_root(lang: str | None) -> str:
    """Name of the language's top-level deck under 'All' (issue #726).

    zh keeps the historical 'Daily' root, so nothing about existing decks or
    their names changes; every other language gets its own parallel tree.
    """
    return get_lang_config(lang).get("deck_root", "Daily")
