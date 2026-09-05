-- SRS Database Schema (multi-language; Chinese-specific tables are unused for other languages)

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- deck_presets
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deck_presets (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    name                    TEXT NOT NULL,

    -- Daily limits
    new_per_day             INTEGER NOT NULL DEFAULT 20,
    reviews_per_day         INTEGER NOT NULL DEFAULT 100,

    -- Learning steps in minutes, space-separated e.g. "1 10"
    learning_steps          TEXT NOT NULL DEFAULT '1 10',

    -- Graduation intervals in days
    graduating_interval     INTEGER NOT NULL DEFAULT 1,
    easy_interval           INTEGER NOT NULL DEFAULT 4,

    -- Relearning steps in minutes, space-separated e.g. "10"
    relearning_steps        TEXT NOT NULL DEFAULT '10',

    -- Review scheduling
    minimum_interval        INTEGER NOT NULL DEFAULT 1,

    -- learned_interval: minimum interval (in days) a card must reach before it
    -- counts as "learned/mature". Reviews of cards below this interval (plus all
    -- relearn cards) are treated as "still learning" in retention stats and deck
    -- badge counts. Does NOT change the SRS state machine or queue order.
    learned_interval        INTEGER NOT NULL DEFAULT 4,

    -- Graduation probation (see cards.probation): when on (1), a card that
    -- finishes its learning/relearn steps does NOT become a review card yet —
    -- it stays learning/relearn until it survives an interval of
    -- >= learned_interval days. Off (0) = classic Anki (graduate immediately).
    enable_probation        INTEGER NOT NULL DEFAULT 1,

    -- ── FSRS scheduling ──────────────────────────────────────────────────────
    -- desired_retention: target recall probability that sets every interval
    -- maximum_interval: hard cap on any computed interval (days)
    -- fsrs_weights: space-separated 19-param vector (NULL = use built-in defaults)
    -- enable_fsrs: 1 = FSRS memory model, 0 = legacy SM-2 (ease-based)
    desired_retention       REAL NOT NULL DEFAULT 0.9,
    maximum_interval        INTEGER NOT NULL DEFAULT 36500,
    fsrs_weights            TEXT,
    enable_fsrs             INTEGER NOT NULL DEFAULT 1,
    -- When 1, a single-step learning/relearn card's "Hard" schedules
    -- learning_hard_days (default 1 day) instead of step×1.5, so a
    -- half-remembered card returns after that many days.
    learning_hard_1d        INTEGER NOT NULL DEFAULT 1,
    -- How many days "Hard" delays a learning/relearn card when learning_hard_1d
    -- is on. Fractional allowed (e.g. 0.5 = half a day).
    learning_hard_days      REAL NOT NULL DEFAULT 1,

    -- New card insertion order (legacy; superseded by new_gather_order)
    insertion_order         TEXT NOT NULL DEFAULT 'sequential'
                                CHECK(insertion_order IN ('sequential', 'random')),

    -- Mark one preset as the default for new decks
    is_default              INTEGER NOT NULL DEFAULT 0,

    -- Bury siblings (legacy; superseded by per-state options below)
    bury_siblings           INTEGER NOT NULL DEFAULT 1,

    -- Randomize word order when generating stories
    randomize_story_order   INTEGER NOT NULL DEFAULT 0,

    -- Leech settings
    -- leech_threshold: review-state lapses before a card is flagged as a leech
    leech_threshold         INTEGER NOT NULL DEFAULT 3,
    -- learning_leech_threshold: Again presses in learning/relearn before flagging
    learning_leech_threshold INTEGER NOT NULL DEFAULT 6,
    -- enable_learning_leech: whether learning/relearn Again presses count toward leech
    enable_learning_leech   INTEGER NOT NULL DEFAULT 1,
    leech_action            TEXT NOT NULL DEFAULT 'suspend'
                                CHECK(leech_action IN ('suspend', 'tag')),

    -- ── Display Order ────────────────────────────────────────────────────────

    new_gather_order        TEXT NOT NULL DEFAULT 'ascending_position'
                                CHECK(new_gather_order IN (
                                    'deck', 'deck_random_notes',
                                    'ascending_position', 'descending_position',
                                    'random_notes', 'random_cards')),

    new_sort_order          TEXT NOT NULL DEFAULT 'card_type_gathered'
                                CHECK(new_sort_order IN (
                                    'card_type_gathered', 'gathered',
                                    'card_type_random', 'random_note_card_type', 'random')),

    new_review_order        TEXT NOT NULL DEFAULT 'mixed'
                                CHECK(new_review_order IN ('mixed', 'new_first', 'reviews_first')),

    interday_learning_review_order TEXT NOT NULL DEFAULT 'mixed'
                                CHECK(interday_learning_review_order IN (
                                    'mixed', 'learning_first', 'reviews_first')),

    review_sort_order       TEXT NOT NULL DEFAULT 'due_random'
                                CHECK(review_sort_order IN (
                                    'due_random', 'due_deck', 'deck_due',
                                    'ascending_intervals', 'descending_intervals',
                                    'ascending_ease', 'descending_ease',
                                    'relative_overdueness')),

    -- ── Burying ──────────────────────────────────────────────────────────────

    bury_new_siblings       INTEGER NOT NULL DEFAULT 0,
    bury_review_siblings    INTEGER NOT NULL DEFAULT 0,
    bury_interday_siblings  INTEGER NOT NULL DEFAULT 0,

    -- Quick-access bury mode overrides the three per-state options above:
    --   'all'    = bury all siblings (default)
    --   'none'   = bury no siblings
    --   'custom' = use bury_new/review/interday_siblings individually
    bury_quick_mode         TEXT NOT NULL DEFAULT 'all'
                                CHECK(bury_quick_mode IN ('all', 'none', 'custom')),

    -- Order of L/R/C category pills shown in the deck list
    category_order          TEXT NOT NULL DEFAULT 'listening,reading,creating',

    -- When 0, the category is fully disabled for decks using this preset: no
    -- L/R/C pill, no due counts, excluded from mixed/unfinished queues. Cards
    -- are kept and come back untouched when re-enabled. reading defaults off
    -- (opt-in feature); listening/creating default on (core categories).
    reading_enabled         INTEGER NOT NULL DEFAULT 0,
    listening_enabled       INTEGER NOT NULL DEFAULT 1,
    creating_enabled        INTEGER NOT NULL DEFAULT 1,

    -- Minimum days between sibling card reviews (R/T/C of the same word)
    sibling_separation      INTEGER NOT NULL DEFAULT 3,

    -- Fraction of current interval applied as additional sibling separation
    -- effective_separation = max(sibling_separation, floor(interval * sibling_factor))
    sibling_factor          REAL NOT NULL DEFAULT 0.2,

    -- Delay (ms) before auto-playing audio when a new card is flipped.
    -- Only affects auto-play; manual replay is never delayed.
    autoplay_delay_ms       INTEGER NOT NULL DEFAULT 1000
);

-- ---------------------------------------------------------------------------
-- decks
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    parent_id   INTEGER REFERENCES decks(id) ON DELETE CASCADE,
    preset_id   INTEGER NOT NULL REFERENCES deck_presets(id),
    -- NULL for parent decks; set for category leaf decks
    category    TEXT CHECK(category IN ('listening', 'reading', 'creating')),
    -- target language of this deck's content (see languages.py registry);
    -- child decks inherit the parent's lang at creation time
    lang        TEXT NOT NULL DEFAULT 'zh',
    -- soft delete: set when moved to trash, hard-deleted after 30 days
    deleted_at  TEXT,
    UNIQUE(name, parent_id)
);

-- ---------------------------------------------------------------------------
-- entries  (formerly 'words' — deck-agnostic vocabulary entries)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- word_zh holds the target-language headword for ALL languages (the _zh
    -- suffix is historical). Unique per (word_zh, lang) — not globally unique
    -- (issue #803): French and Spanish share many identical surface forms
    -- (capital, animal, total, region...), so a global constraint would
    -- either reject a genuinely new word or silently collide with an
    -- unrelated language's entry.
    word_zh         TEXT NOT NULL,
    -- target language of this entry (see languages.py registry)
    lang            TEXT NOT NULL DEFAULT 'zh',
    pinyin          TEXT,
    definition      TEXT,           -- English definition
    pos             TEXT,           -- part of speech
    hsk_level       INTEGER,        -- 1-6, NULL for 超纲 (or CEFR A1-C2 for non-zh langs)
    traditional     TEXT,
    definition_zh   TEXT,
    date_added      TEXT NOT NULL DEFAULT (datetime('now')),
    date_yaml       TEXT,           -- date string from YAML file, e.g. "03/27"
    source          TEXT NOT NULL DEFAULT 'kouyu',
    notes           TEXT,           -- usage notes / explanations from YAML `note` field
    source_sentence TEXT,           -- original source-language sentence (e.g. German) for sentence notes
    grammar_notes   TEXT,           -- grammar explanation (e.g. grammar_de from YAML)
    definition_de   TEXT,           -- German translation / definition
    definition_fr   TEXT,           -- French translation / definition
    note_type       TEXT NOT NULL DEFAULT 'vocabulary',
                        -- vocabulary | sentence | chengyu | expression | grammar
    register        TEXT CHECK(register IN ('spoken', 'written', 'both', 'spoken_colloquial', 'spoken_neutral', 'neutral', 'formal_written', 'literary')),
                        -- language register: spoken=口语, written=书面语, both=通用, spoken_colloquial=口语俚语, spoken_neutral=中性口语, neutral=通用, formal_written=正式书面语, literary=文学语体
    gender          TEXT,           -- 'm' | 'f' | 'mf' | NULL — noun grammatical gender (French/Spanish; #803)
    -- Word origin, German prose (issue #906). Romance languages only: a French
    -- word has no characters to break down, so the review card's right-hand
    -- panel shows this instead of the Chinese "Word Analysis" block. Chinese
    -- entries keep NULL here — their etymology lives per character in
    -- characters.etymology, which is a different thing entirely.
    etymology       TEXT,
    UNIQUE(word_zh, lang)
);

-- ---------------------------------------------------------------------------
-- entry_measure_words  (量词 — classifiers/measure words for a vocabulary entry)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entry_measure_words (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id     INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    measure_zh  TEXT NOT NULL,      -- simplified Chinese, e.g. 种
    pinyin      TEXT,               -- e.g. zhǒng
    meaning     TEXT,               -- English gloss, e.g. "kind, type"
    position    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(word_id, measure_zh)
);

-- ---------------------------------------------------------------------------
-- entry_relations  (synonyms + antonyms — joint table)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entry_relations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id         INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    related_zh      TEXT NOT NULL,      -- simplified Chinese of the related word
    related_pinyin  TEXT,
    related_de      TEXT,               -- German gloss
    relation_type   TEXT NOT NULL CHECK(relation_type IN ('synonym', 'antonym')),
    UNIQUE(word_id, related_zh, relation_type)
);

-- ---------------------------------------------------------------------------
-- entry_conjugations  (verb conjugation forms — generic tense × person grid,
-- used by French (issue #596) and any future conjugating language; person is
-- '' for impersonal forms like participles/infinitives)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entry_conjugations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id     INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    tense       TEXT NOT NULL,      -- e.g. 'présent', 'passé composé', 'participe passé'
    person      TEXT NOT NULL DEFAULT '',  -- e.g. 'je', 'tu', 'il/elle', '' for impersonal
    form        TEXT NOT NULL,      -- the conjugated form, e.g. 'parle'
    position    INTEGER NOT NULL DEFAULT 0, -- preserves the YAML tense/person order
    UNIQUE(word_id, tense, person)
);
-- NOTE (#803): entry_conjugations is kept around but no longer read or
-- written by application code — its rows were migrated into entry_forms
-- below (one-time, see database/core.py's migrated_entry_conjugations
-- marker). entry_forms is the single source of truth going forward.

-- ---------------------------------------------------------------------------
-- entry_forms  (morphological forms — generalizes entry_conjugations to also
-- cover noun/adjective inflection: plural, gender agreement, etc. Two uses,
-- distinguished by `kind` (issue #803, docs/multilang.md has the full model):
--   kind='conjugation': paradigm=tense (e.g. 'présent'), slot=person
--                        (e.g. 'je', '' for impersonal forms)
--   kind='inflection':  paradigm=dimension (e.g. 'nombre', 'genre'),
--                        slot=value (e.g. 'pluriel', 'féminin')
-- idx_entry_forms_form is required for forms_lookup(): the knowledge-base
-- annotator runs "is this surface form one Daniel already knows" hundreds of
-- times per article.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entry_forms (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id  INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL DEFAULT 'conjugation',
    paradigm TEXT NOT NULL,
    slot     TEXT NOT NULL DEFAULT '',
    form     TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    UNIQUE(word_id, paradigm, slot)
);
CREATE INDEX IF NOT EXISTS idx_entry_forms_form ON entry_forms(form);
CREATE INDEX IF NOT EXISTS idx_entry_forms_word ON entry_forms(word_id);

-- ---------------------------------------------------------------------------
-- entry_examples
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entry_examples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id         INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    example_zh      TEXT NOT NULL,
    example_pinyin  TEXT,
    example_en      TEXT,           -- English translation of the example
    example_de      TEXT,
    position        INTEGER NOT NULL,
    example_type    TEXT NOT NULL DEFAULT 'example'
                        CHECK(example_type IN ('example', 'similar'))
                        -- 'example': normal usage example; 'similar': similar sentence (sentence type)
    -- Note: deduplication enforced in application layer (INSERT OR IGNORE check on example_zh)
);

-- ---------------------------------------------------------------------------
-- entry_grammar_structures  (grammar patterns within sentence entries)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entry_grammar_structures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id     INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    structure   TEXT NOT NULL,      -- e.g. "忘记如何 + 动词"
    explanation TEXT,               -- prose explanation
    example_zh  TEXT,               -- short example phrase
    position    INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- characters
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS characters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    char            TEXT NOT NULL UNIQUE,
    traditional     TEXT,
    pinyin          TEXT,
    hsk_level       INTEGER,
    etymology       TEXT,
    other_meanings  TEXT,   -- JSON array
    compounds       TEXT    -- DEPRECATED: use character_compounds table; kept for migration only
);

-- ---------------------------------------------------------------------------
-- character_compounds  (normalised compound rows linked to a character)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS character_compounds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    char_id     INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    compound_zh TEXT NOT NULL,      -- simplified Chinese compound, e.g. 绝望
    pinyin      TEXT,
    meaning     TEXT,               -- English/German gloss
    position    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(char_id, compound_zh)
);

-- ---------------------------------------------------------------------------
-- entry_characters  (junction table — formerly word_characters)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entry_characters (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id             INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    char_id             INTEGER NOT NULL REFERENCES characters(id),
    position            INTEGER NOT NULL,
    meaning_in_context  TEXT,
    UNIQUE(word_id, char_id)
);

-- ---------------------------------------------------------------------------
-- cards  (owns deck_id — one card per entry per category, globally unique)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id     INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    deck_id     INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    category    TEXT NOT NULL CHECK(category IN ('listening', 'reading', 'creating')),
    state       TEXT NOT NULL DEFAULT 'new'
                    CHECK(state IN ('new', 'learning', 'review', 'relearn', 'suspended')),

    -- due: ISO datetime for learning/relearn, ISO date for new/review
    due         TEXT NOT NULL DEFAULT (date('now')),

    step_index  INTEGER NOT NULL DEFAULT 0,
    interval    INTEGER NOT NULL DEFAULT 0,     -- days
    ease        REAL    NOT NULL DEFAULT 2.5,   -- SM-2 legacy; unused under FSRS scheduling
    repetitions INTEGER NOT NULL DEFAULT 0,
    lapses      INTEGER NOT NULL DEFAULT 0,

    -- FSRS memory state (NULL until the card first graduates out of learning)
    stability   REAL,                            -- days-to-90%-retention
    difficulty  REAL,                            -- 1–10 intrinsic hardness
    last_review TEXT,                            -- ISO date of the previous review (for elapsed-days / R)

    -- Again presses while in new/learning/relearn (drives the learning leech check)
    learning_again_count INTEGER NOT NULL DEFAULT 0,

    -- set to 1 when the card was suspended by leech detection (vs. manual suspend)
    is_leech    INTEGER NOT NULL DEFAULT 0,

    -- when the leech flag was set (NULL when not a leech); drives the Leeched
    -- browse sort. Cleared back to NULL whenever the leech flag is cleared.
    leeched_at  TEXT,

    -- Temporary burial: card is hidden until this date (resets automatically next day)
    buried_until TEXT,

    -- soft delete: set when moved to trash, hard-deleted after 30 days
    deleted_at   TEXT,

    -- saved before suspension so it can be restored on unsuspend
    pre_suspend_state TEXT,

    -- free-text note the user leaves for the next time this card comes up
    next_note TEXT,

    -- Graduation probation: 1 while a learning/relearn card has finished its
    -- steps but has not yet survived an interval of >= learned_interval days.
    -- Only surviving such an interval turns the card into a real 'review' card;
    -- failing during probation restarts the steps WITHOUT counting a lapse.
    probation INTEGER NOT NULL DEFAULT 0,

    UNIQUE(word_id, category)
);

-- ---------------------------------------------------------------------------
-- review_log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id         INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    reviewed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    rating          INTEGER NOT NULL CHECK(rating IN (1, 2, 3, 4)),
    user_response   TEXT,       -- what the user typed (creating category)
    ai_score        INTEGER,    -- future: AI evaluation score
    duration_ms     INTEGER,    -- time spent on this review in milliseconds (NULL for legacy rows)
    state           TEXT,       -- card state at review time (new/learning/review/relearn) — NULL for legacy rows
    last_interval   INTEGER     -- the interval (days) the card had when shown, i.e. the interval being tested
                                -- (used to tell "learned/mature" reviews apart) — NULL for legacy rows
);

-- ---------------------------------------------------------------------------
-- stories
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,  -- YYYY-MM-DD
    -- 'again' = sentinel category for single-sentence regenerations triggered by an
    -- Again rating; kept out of the normal per-category story queries (see stories.py).
    category        TEXT NOT NULL CHECK(category IN ('listening', 'reading', 'creating', 'unified', 'again')),
    deck_id         INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    generated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    prompt_text     TEXT,         -- full AI prompt used to generate this story (NULL for legacy rows)
    topic           TEXT,         -- user-specified topic/theme (NULL if none given)
    gen_params      TEXT,         -- JSON of the generation settings (mode/model/grammar/max_hsk/chapter_ids)
                                  -- so the "Again" regeneration can match the deck's style (NULL for legacy rows)
    lang            TEXT          -- target language of this story (issue #436); NULL = legacy row, treated as 'zh'.
                                  -- Needed because an aggregate deck (e.g. "All") can hold stories of several
                                  -- languages for the same (date, category, deck_id) — lang disambiguates them.
    -- NO unique constraint: multiple stories per (date, category, deck) allowed
    -- active story = latest generated_at
    -- stories are NEVER auto-deleted
);

-- ---------------------------------------------------------------------------
-- story_sentences  (formerly 'sentences')
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS story_sentences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id    INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    sentence_zh TEXT NOT NULL,
    sentence_en TEXT NOT NULL DEFAULT '',
    sentence_de TEXT,
    sentence_fr TEXT,
    tokens      TEXT,
    concept_en  TEXT,
    concept_zh  TEXT,
    reasoning_zh TEXT,
    source_url  TEXT,
    context_de  TEXT,
    source_title TEXT,
    source_name TEXT,
    -- starred during review as a good example to learn from when tuning prompts (#692)
    starred     INTEGER NOT NULL DEFAULT 0,
    starred_at  TEXT,
    -- flagged during review as a bad example — grammar mistakes / awkward
    -- phrasing worth fixing in the prompt (#854, the mirror image of starred)
    flagged     INTEGER NOT NULL DEFAULT 0,
    flagged_at  TEXT,
    UNIQUE(story_id, position)
);

-- ---------------------------------------------------------------------------
-- story_sentence_words  (many-to-many: sentences ↔ vocab entries)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS story_sentence_words (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sentence_id INTEGER NOT NULL REFERENCES story_sentences(id) ON DELETE CASCADE,
    word_id     INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    UNIQUE(sentence_id, word_id)
);

-- ---------------------------------------------------------------------------
-- api_call_log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_call_log (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at            TEXT NOT NULL DEFAULT (datetime('now')),
    model                TEXT NOT NULL,
    input_tokens         INTEGER NOT NULL,
    output_tokens        INTEGER NOT NULL,
    purpose              TEXT NOT NULL DEFAULT 'story',
    cached_input_tokens  INTEGER NOT NULL DEFAULT 0,
    action_id            TEXT,
    action_label         TEXT,
    prompt               TEXT,
    response             TEXT
);

-- ---------------------------------------------------------------------------
-- prompt_templates — user-edited story prompt templates (issue #581);
-- no row for a mode = built-in default (ai.DEFAULT_PROMPT_TEMPLATES)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prompt_templates (
    mode        TEXT PRIMARY KEY,
    template    TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- prompt_presets — 每个故事模式可保存多个命名提示词版本（issue #610）；
-- 每个 mode 最多一行 is_active=1；无生效行 = 用 ai.DEFAULT_PROMPT_TEMPLATES
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prompt_presets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mode        TEXT NOT NULL,
    name        TEXT NOT NULL,
    template    TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(mode, name)
);

-- ---------------------------------------------------------------------------
-- entry_components  (formerly note_components — links sentences/chengyu to their component vocabulary)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entry_components (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id     INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    word_id     INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    UNIQUE(note_id, word_id)
);

-- ---------------------------------------------------------------------------
-- structures  (future)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS structures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern     TEXT NOT NULL,
    description TEXT,
    example_zh  TEXT,
    example_en  TEXT
);

-- ---------------------------------------------------------------------------
-- grammar_points  (type: grammar — reference only, no SRS cards)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS grammar_points (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,   -- display name, e.g. "所 (suǒ) – Nominalisierung"
    level           TEXT,                   -- e.g. "5-6"
    structure       TEXT,                   -- e.g. "所 + Verb + 的 (+ Nomen)"
    meaning         TEXT,                   -- short gloss
    usage           TEXT,                   -- long prose explanation
    cultural_note   TEXT,
    date_added      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- grammar_examples
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS grammar_examples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    grammar_id  INTEGER NOT NULL REFERENCES grammar_points(id) ON DELETE CASCADE,
    example_zh  TEXT NOT NULL,
    pinyin      TEXT,
    example_de  TEXT,
    structure   TEXT,                       -- structural annotation, e.g. "我 + 所 + 知道 + 的"
    position    INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- grammar_patterns  (common_patterns in YAML)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS grammar_patterns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    grammar_id  INTEGER NOT NULL REFERENCES grammar_points(id) ON DELETE CASCADE,
    pattern     TEXT NOT NULL,              -- e.g. "所 + V + 的"
    meaning     TEXT,
    example     TEXT,
    position    INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- grammar_comparisons  (comparisons in YAML)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS grammar_comparisons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    grammar_id  INTEGER NOT NULL REFERENCES grammar_points(id) ON DELETE CASCADE,
    title       TEXT,
    explanation TEXT,
    position    INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- grammar_expressions  (fixed_expressions in YAML)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS grammar_expressions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    grammar_id  INTEGER NOT NULL REFERENCES grammar_points(id) ON DELETE CASCADE,
    expression  TEXT NOT NULL,
    meaning     TEXT,
    position    INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- preset_category_overrides
-- Per-category scheduling overrides for a preset.
-- NULL fields mean "use the preset default".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS preset_category_overrides (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    preset_id           INTEGER NOT NULL REFERENCES deck_presets(id) ON DELETE CASCADE,
    category            TEXT NOT NULL CHECK(category IN ('listening', 'reading', 'creating')),
    new_per_day         INTEGER,
    reviews_per_day     INTEGER,
    learning_steps      TEXT,
    graduating_interval INTEGER,
    easy_interval       INTEGER,
    relearning_steps    TEXT,
    minimum_interval    INTEGER,
    leech_threshold     INTEGER,
    learning_leech_threshold INTEGER,
    leech_action        TEXT CHECK(leech_action IN ('suspend', 'tag')),
    UNIQUE(preset_id, category)
);

-- ---------------------------------------------------------------------------
-- Performance indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_cards_deck_cat_state
    ON cards(deck_id, category, state);

CREATE INDEX IF NOT EXISTS idx_cards_word_id
    ON cards(word_id);

CREATE INDEX IF NOT EXISTS idx_cards_due
    ON cards(due);

CREATE INDEX IF NOT EXISTS idx_review_log_card_date
    ON review_log(card_id, reviewed_at);

-- Supports calendar-stats / card-evolution date-range scans over review_log
-- that aren't filtered by a specific card_id (issue #513).
CREATE INDEX IF NOT EXISTS idx_review_log_reviewed_at
    ON review_log(reviewed_at);

-- ---------------------------------------------------------------------------
-- Morning pregen configuration (issue #473): per deck+category story mode the
-- 06:00 pregen uses — independent of whatever was regenerated during the day
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pregen_config (
    deck_id  INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    category TEXT NOT NULL CHECK(category IN ('listening', 'reading', 'creating', 'unified')),
    lang     TEXT NOT NULL DEFAULT 'zh',
    mode     TEXT NOT NULL,
    max_hsk  INTEGER NOT NULL DEFAULT 3,
    PRIMARY KEY (deck_id, category, lang)
);

-- ---------------------------------------------------------------------------
-- Generic key/value app settings (issue #528): global scalar toggles that
-- don't belong to any deck/category. First key: pregen_enabled — the master
-- switch for morning pre-generation (POST /api/pregen-today short-circuits
-- when it's not '1').
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Words Daniel already knows without ever having studied them here (#710).
-- Pure marker: no card, no deck, no scheduling — the only thing this table
-- does is widen zh_annotate's "already known" test, which until now was
-- "exists in entries.word_zh". Everything downstream (inline annotations in
-- both summaries, the HSK word table under an episode) follows from that one
-- test, so a word marked here quietly stops being flagged everywhere at once.
-- ---------------------------------------------------------------------------
-- lang (#803): known_words was PK'd on word_zh alone, which is wrong for the
-- same reason entries.word_zh is — French/Spanish share surface forms, so
-- marking a French word known must not silently mark an identical-looking
-- Spanish word known too.
CREATE TABLE IF NOT EXISTS known_words (
    word_zh  TEXT NOT NULL,
    lang     TEXT NOT NULL DEFAULT 'zh',
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (word_zh, lang)
);

-- ---------------------------------------------------------------------------
-- Podcast crawler (issue #479): discover new episodes from podcast RSS feeds
-- (#497 — replaced the original YouTube-channel source), transcribe them,
-- summarize into German, extract HSK5+ vocabulary, and email a notification.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS podcast_episodes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    -- kind = 'podcast' | 'video' | 'article' | 'newsletter' (#650 stage A;
    -- 'newsletter' added #925 for known email newsletters like F.A.Z.
    -- Frühdenker, ingested via knowledge/newsletter.py + ingest_text()).
    -- The generic columns below are reused across all kinds with
    -- different meanings — see docs/knowledge-base.md for the full mapping:
    --   video_id:    RSS item guid (podcast) | YouTube video id (video) | normalized URL, utm params stripped (article) | "pasted:<hash of body>" (newsletter, same pasted-text dedup key as ingest_text())
    --   channel_id:  source RSS feed URL (podcast) | YouTube channel id if available (video) | site domain (article) | newsletter source name, e.g. "F.A.Z. Frühdenker" (newsletter)
    --   youtube_url: episode webpage link (podcast) | video link (video) | article link (article) | "" (newsletter — no single article URL, the mail body itself is the content)
    --   transcript_zh: transcript (podcast) | caption/subtitle text (video) | article body (article) | newsletter body, boilerplate-stripped (newsletter) — i.e. "source material, any language", not podcast-specific
    kind             TEXT NOT NULL DEFAULT 'podcast',
    video_id         TEXT NOT NULL UNIQUE,  -- RSS item guid (or enclosure URL if no guid), #497; legacy rows used the YouTube video id
    channel_id       TEXT,   -- source RSS feed URL, #497; legacy rows used the YouTube channel id
    title            TEXT NOT NULL,
    title_en         TEXT,   -- English title, AI-translated starting stage B (#650); NULL for stage-A rows
    published_at     TEXT,
    youtube_url      TEXT NOT NULL,  -- episode webpage link (RSS item <link>), #497; column name kept for backward compat
    audio_url        TEXT,   -- RSS enclosure MP3 direct link, #497
    duration_seconds INTEGER, -- parsed itunes:duration, #497 — used as a pre-download guardrail/gate
    spotify_url      TEXT,
    transcript_zh    TEXT,   -- source material full text, any language (podcast transcript | video captions | article body); name kept for backward compat (#650)
    transcript_de    TEXT,   -- JSON array of {"zh","de"} bilingual segment pairs (#553)
    summary_de       TEXT,
    summary_zh       TEXT,   -- short Chinese summary shown before the German one (#631)
    hsk_words        TEXT,   -- JSON array of {word, pinyin, definition_de, hsk}
    detail_level     TEXT,   -- detail_level used for the summary (short|medium|detailed)
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending', 'no_transcript', 'summarized', 'error')),
    transcript_source TEXT,  -- 'tingwu' | 'whisper' | 'notebooklm' | 'youtube_captions' (#651) | 'article' (#652) | 'groq_whisper' (#750, Instagram Reels — 'whisper' also occurs here when the Groq step was skipped/failed and the OpenAI whisper-1 fallback ran instead) | NULL; legacy rows may say 'captions'
    error            TEXT,
    email_sent_at    TEXT,
    -- #960: Message-ID of the mail this row was ingested from (mailbox view),
    -- so the mail list can say "already processed" without downloading bodies.
    -- NULL for every row that didn't come from a mail.
    mail_message_id  TEXT,
    processing_started_at TEXT,  -- set while _process_episode runs, cleared on exit; a leftover value = killed mid-transcription, recovered at startup (#598)
    -- china_critical (#731): set at paste time, read at summarize time. The
    -- API summary fallback normally prefers DeepSeek to save money; for
    -- material critical of China DeepSeek censors/waters down the answer, so
    -- 1 means "skip DeepSeek, go straight to OpenAI". The free NotebookLM
    -- path stays first either way — Google doesn't censor the topic.
    china_critical   INTEGER NOT NULL DEFAULT 0,
    -- #935 (knowledge base overhaul, umbrella #934):
    --   processed_at: when the summary last completed successfully. This is
    --     the unified material list's default sort key ("Bearbeitungsdatum") —
    --     created_at is when it was pasted, published_at is when the source
    --     went out, neither answers "when did this become readable for me".
    --     Regenerating a summary refreshes it: the item really was processed
    --     again.
    --   author:  person/channel/show behind the material. Deliberately NOT
    --     channel_id — that column is already overloaded four ways (RSS feed
    --     URL | YouTube channel id | site domain | pasted-text author), so
    --     filtering by author off it is impossible.
    --   platform: 'youtube' | 'instagram' | 'podcast' | 'web' | 'upload' |
    --     'paste'. Inferred once at ingest time, editable by hand afterwards
    --     (#937).
    --   manual_fields: JSON array of column names Daniel edited by hand
    --     (#937). AI paths (title suggestion, metadata extraction, auto
    --     tagging) must never overwrite what's listed here.
    --   archived_at: NULL = not archived. The default list view hides
    --     archived material (#940); a filter toggle brings it back.
    processed_at     TEXT,
    author           TEXT,
    platform         TEXT,
    manual_fields    TEXT,
    archived_at      TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Per-language reading renditions of a knowledge-base episode's summary
-- (#804). The AI writes summary_de exactly once; every other language's
-- reading view is a Google-translated-then-annotated derivative of it,
-- generated lazily on first request and cached here so it isn't re-translated
-- on every page view. Chinese is NOT stored here — summary_zh is already
-- AI-native and annotated by zh_annotate.py, so it has no rendition row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_renditions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES podcast_episodes(id) ON DELETE CASCADE,
    lang       TEXT NOT NULL,
    summary    TEXT NOT NULL,   -- target-language summary, new words already annotated inline
    new_words  TEXT,            -- JSON array of {word, lemma, definition_de}
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(episode_id, lang)
);

-- ---------------------------------------------------------------------------
-- Full-text reading versions (#972). The untruncated source text of a piece of
-- material, translated into the reading language and annotated exactly like a
-- summary (knowledge/rendition.render_html — one pipeline, same annotation
-- rules for a book page, a summary and a full text).
--
-- Why a separate table instead of a `kind` column on knowledge_renditions:
-- that table carries UNIQUE(episode_id, lang), which would have to be relaxed
-- to hold both a summary and a full text for the same language — and SQLite
-- cannot alter a constraint, only rebuild the table and migrate production
-- data. book_renditions already sets the precedent of a same-shaped table with
-- a different owner (#836).
--
-- Unlike knowledge_renditions, `lang` here CAN be 'zh': a summary has an
-- AI-native Chinese version (summary_zh), a full text has none.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_fulltexts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES podcast_episodes(id) ON DELETE CASCADE,
    lang       TEXT NOT NULL,
    text       TEXT NOT NULL,   -- target-language full text, new words annotated inline
    new_words  TEXT,            -- JSON array of {word, lemma, definition_de}
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(episode_id, lang)
);

-- Tags and user-defined lists over knowledge material (#935, umbrella #934).
-- Tags come from two sources: the AI tagger (#938) and Daniel's own edits
-- (#937). `source` keeps them apart so re-tagging can never clobber a manual
-- tag, and so the UI can show which ones were machine-made.
CREATE TABLE IF NOT EXISTS knowledge_tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Display form as typed. Uniqueness is case-insensitive (see the index
    -- below) so 'Politik' and 'politik' can never become two tags — with a
    -- shared filter bar, near-duplicate tags are worse than no tags.
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_tags_name ON knowledge_tags(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS knowledge_item_tags (
    episode_id INTEGER NOT NULL REFERENCES podcast_episodes(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES knowledge_tags(id) ON DELETE CASCADE,
    source     TEXT NOT NULL DEFAULT 'user' CHECK(source IN ('user', 'ai')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (episode_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_item_tags_tag ON knowledge_item_tags(tag_id);

-- User-defined lists, e.g. "Read Later" (#940). The Read Later row is created
-- by init_db() and protected from deletion (is_builtin=1) — the swipe gesture
-- targets it by name, so it has to exist.
CREATE TABLE IF NOT EXISTS knowledge_lists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    icon       TEXT,
    is_builtin INTEGER NOT NULL DEFAULT 0,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS knowledge_list_items (
    list_id    INTEGER NOT NULL REFERENCES knowledge_lists(id) ON DELETE CASCADE,
    episode_id INTEGER NOT NULL REFERENCES podcast_episodes(id) ON DELETE CASCADE,
    added_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (list_id, episode_id)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_list_items_episode ON knowledge_list_items(episode_id);

-- Full-text search over everything readable in the knowledge base (#939):
-- titles, source transcripts, both AI summaries and every per-language
-- rendition. One row per (episode, field, lang) so a hit can say WHERE it
-- matched. Chinese is stored character-by-character and queried as a phrase —
-- unicode61 does not segment CJK, so a whole paragraph would otherwise be a
-- single token. See database/search.py for the full reasoning.
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    episode_id UNINDEXED,
    field      UNINDEXED,
    lang       UNINDEXED,
    body,
    tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS podcast_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Podcast source list (issue #502): replaces podcast_config.feeds (a plain
-- JSON array with no per-feed settings) with one row per RSS feed, so each
-- source can be toggled between fully-automatic processing and manual
-- (metadata-only ingestion, transcription triggered per-episode from the UI).
CREATE TABLE IF NOT EXISTS podcast_feeds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL UNIQUE,   -- RSS feed URL
    title        TEXT,                   -- RSS channel <title>, fetched on add
    auto_process INTEGER NOT NULL DEFAULT 0,  -- 1 = new episodes are transcribed+summarized automatically
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- In-app AI dictionary (#746): Daniel used to paste words into a chat AI and
-- copy the answer into the add-word box by hand. This table stores each
-- lookup's full structured result (see ai.dictionary_lookup()) so the /dict
-- page can show a searchable history below the query box. Adding a word from
-- a result still goes through /api/add-word-ai (#643's single add pipeline)
-- — this table only remembers what the dictionary said, not any card state.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dict_queries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    query       TEXT NOT NULL,              -- user's raw input, verbatim
    lang        TEXT NOT NULL DEFAULT 'zh', -- target vocabulary language (only 'zh' for now)
    input_lang  TEXT,                       -- AI-detected input language: zh|de|en|fr
    kind        TEXT,                       -- chinese|word|phrase|sentence
    headline    TEXT,                       -- short display title, denormalized so the
                                             -- history list doesn't need to parse result_json for every row
    result_json TEXT NOT NULL,              -- full JSON returned by ai.dictionary_lookup()
    model       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_dict_queries_created ON dict_queries(created_at DESC);

-- ---------------------------------------------------------------------------
-- Book reader (#836): a whole German/English book, read page by page in the
-- language Daniel is studying. Each page is translated with Google Translate
-- and annotated by the same pipeline the knowledge base uses
-- (knowledge/rendition.py), so a book page reads exactly like an episode
-- summary: HSK5+ / unknown words carry their pinyin and German gloss inline.
--
-- Four tables, one job each:
--   books            the uploaded file and how it was cut into pages
--   book_pages       the SOURCE text (German/English), one row per page
--   book_renditions  the translated+annotated view of one page in one language
--   book_progress    where Daniel is, per (book, language)
--
-- Pagination happens exactly once, at upload. Re-cutting an existing book
-- would silently shift every stored rendition and reading position by an
-- unknown amount, so there is deliberately no "re-paginate" path.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS books (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    author      TEXT,
    source_lang TEXT NOT NULL DEFAULT 'de',  -- language of the uploaded file (de|en)
    format      TEXT NOT NULL,               -- epub|pdf
    file_path   TEXT,                        -- original upload under data/books/
    page_count  INTEGER NOT NULL DEFAULT 0,
    char_budget INTEGER NOT NULL DEFAULT 1200,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS book_pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    page_no     INTEGER NOT NULL,            -- 1-based, contiguous
    source_text TEXT NOT NULL,               -- HTML: <p> paragraphs, source language
    ref_label   TEXT,                        -- PDF's real page number / EPUB chapter title
    UNIQUE(book_id, page_no)
);

-- Same shape as knowledge_renditions on purpose: both are the output of the
-- one translate-then-annotate pipeline in knowledge/rendition.py.
CREATE TABLE IF NOT EXISTS book_renditions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id    INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    page_no    INTEGER NOT NULL,
    lang       TEXT NOT NULL,
    text       TEXT NOT NULL,                -- target language, new words annotated inline
    new_words  TEXT,                         -- JSON array of {word, ...} (annotator-specific)
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(book_id, page_no, lang)
);

CREATE TABLE IF NOT EXISTS book_progress (
    book_id    INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    lang       TEXT NOT NULL,
    last_page  INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY(book_id, lang)
);

CREATE INDEX IF NOT EXISTS idx_book_pages_book ON book_pages(book_id, page_no);
CREATE INDEX IF NOT EXISTS idx_book_renditions_lookup ON book_renditions(book_id, page_no, lang);

-- Chapter table of contents + per-chapter Chinese summaries (#864), structured
-- like data/kahneman_chapters.json so the same concept-card UI/prompting can
-- read either source. Unlike book_pages (cut once at upload), chapters are
-- *derived* lazily from book_pages.ref_label the first time they're asked
-- for — an already-uploaded book doesn't need to be re-uploaded to gain a
-- table of contents. EPUB only: PDF's ref_label is the real page number (a
-- different string on every page), not a chapter marker, so grouping it
-- would produce one "chapter" per page. See database/books.py.derive_chapters.
CREATE TABLE IF NOT EXISTS book_chapters (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id       INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    number        INTEGER NOT NULL,        -- 1-based position in the book
    ref_label     TEXT,                    -- book_pages.ref_label this chapter was grouped from
    start_page    INTEGER NOT NULL,
    end_page      INTEGER NOT NULL,
    title_zh      TEXT,
    title_en      TEXT,
    concept_zh    TEXT,                    -- one-sentence core idea (<=60 chars)
    summary_zh    TEXT,                    -- 300-500 char detailed Chinese summary
    examples_zh   TEXT,                    -- JSON array of translated quote sentences
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|summarized|error
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    summarized_at TEXT,
    UNIQUE(book_id, number)
);

CREATE INDEX IF NOT EXISTS idx_book_chapters_book ON book_chapters(book_id, number);

-- ---------------------------------------------------------------------------
-- Per-language reading renditions of a book chapter's summary (#894). Same
-- idea as knowledge_renditions (#804): the AI writes title_zh/concept_zh/
-- summary_zh/examples_zh exactly once, and every other language's chapter
-- view is a Google-translated derivative of those columns, generated lazily
-- on first request and cached here. Chinese is NOT stored here — the _zh
-- columns on book_chapters already are the Chinese reading (no annotation
-- pass, unlike knowledge_renditions: chapter summaries are recall outlines,
-- the actual annotated reading material is the book page itself).
-- Structure deliberately mirrors knowledge_renditions column-for-column.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS book_chapter_renditions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER NOT NULL REFERENCES book_chapters(id) ON DELETE CASCADE,
    lang       TEXT NOT NULL,
    title      TEXT,            -- translation of title_zh (or ref_label if title_zh was empty)
    concept    TEXT,            -- translation of concept_zh
    summary    TEXT,            -- translation of summary_zh
    examples   TEXT,            -- JSON array, translation of examples_zh (same shape)
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(chapter_id, lang)
);

CREATE INDEX IF NOT EXISTS idx_book_chapter_renditions ON book_chapter_renditions(chapter_id, lang);

-- ---------------------------------------------------------------------------
-- Chat about a knowledge item (#945). After reading a podcast/video/article
-- summary Daniel wants to ask follow-up questions about it; before this the
-- only way was copying the text (#943) into another chat app, where the
-- conversation was lost the moment he closed the tab.
--
-- One conversation per knowledge item (episode_id is UNIQUE) — the detail
-- page shows it again on every visit. Deliberately NOT a single JSON column
-- of messages: appending a turn would then be a read-modify-write of the
-- whole blob, and two open tabs would overwrite each other's turns.
--
-- The material itself is NOT copied in here: every request rebuilds the
-- context from podcast_episodes (transcript, else summary), so a regenerated
-- summary is immediately what the AI sees.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_chats (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL UNIQUE REFERENCES podcast_episodes(id) ON DELETE CASCADE,
    model      TEXT,            -- model used for the most recent turn
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS knowledge_chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL REFERENCES knowledge_chats(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,   -- 'user' | 'assistant'
    content    TEXT NOT NULL,
    model      TEXT,            -- which model answered (NULL on user turns)
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_knowledge_chat_messages ON knowledge_chat_messages(chat_id, id);

-- ---------------------------------------------------------------------------
-- Mailbox view (#960): Daniel subscribes to newsletters with his Gmail address
-- and browses everything that arrives in the 📬 Mailbox section, processing
-- the ones he wants by hand — the same mental model as a podcast episode's
-- "process" button. Mail itself is NOT stored here: the list is fetched live
-- from IMAP (envelopes only, no bodies) on every request, so there is no
-- mirror of his inbox in this database that could drift or leak.
--
-- This table holds only the per-sender "process every mail from this address
-- automatically" switch (same idea as podcast_feeds.auto_process). A row
-- exists only for senders Daniel explicitly touched; every other sender is
-- manual, which is the default and the safe direction.
--
-- It also replaces KNOWLEDGE_MAIL_ALLOWED_SENDERS as the cron's source of
-- truth (#960 retires that variable). That variable existed to stop anyone
-- who knows the address from triggering a paid AI call (#655); with manual
-- processing as the default, the switch below IS that gate — but the cron
-- must therefore search ONLY these addresses, never "all unseen mail", or
-- the default would silently flip from "process nobody" to "process
-- everybody".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mail_senders (
    address      TEXT PRIMARY KEY,          -- envelope From address, lowercased
    name         TEXT,                      -- display name as last seen, for the UI
    auto_process INTEGER NOT NULL DEFAULT 0,-- 1 = cron ingests+summarizes every new mail
    -- 1 = hidden from the mail list and never processed (#968). Blocking clears
    -- auto_process rather than coexisting with it: "subscribed AND blocked" is a
    -- contradiction, and letting it be representable means someone eventually has
    -- to explain which one wins.
    blocked      INTEGER NOT NULL DEFAULT 0,
    -- When the automatic switch was last turned ON (#997). The cron only
    -- looks at mail that arrived after this: subscribing is a promise about
    -- future mail, not an order to summarise (and pay for) the archive.
    -- NULL on rows that predate the column and on never-subscribed senders.
    auto_since   TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Audio tracks (#1047 umbrella, this table added by #1048): karaoke-style
-- read-along needs an mp3 plus a time-aligned list of "cues" for whatever
-- text Daniel is reading — a knowledge-base episode's summary/full text
-- today, a book page later on. Four different ways to produce that pair are
-- planned (TTS + word boundaries, text-anchored ASR alignment, cloud ASR,
-- local ASR —
-- see audio/__init__.py), and all four end up with exactly this shape. One
-- table for all of them, not one per source: they are interchangeable once
-- built, and a second parallel implementation is a second place every future
-- bug in "how cues line up with text" has to be fixed (the same reasoning
-- #643 gives for a single add-word entry point and #836 gives for one
-- rendition pipeline shared by books and the knowledge base).
--
-- `variant` is not optional: the same piece of source material has a short
-- summary and a full text, and those are two different pieces of prose that
-- need two different audio files and two different cue lists — collapsing
-- them into one row per (owner_kind, owner_id, lang) would mean regenerating
-- the full-text track silently destroys the summary track, or vice versa.
--
-- cues_json is a JSON array of:
--   {"start_ms": int, "end_ms": int, "text": str,
--    "char_start": int, "char_end": int}
-- char_start/char_end are NOT optional either: they are positions in the
-- SOURCE TEXT that was handed to the aligner (before any inline vocabulary
-- gloss was stripped for speaking, see audio.strip_annotations), not in
-- whatever rendered/annotated HTML Daniel actually reads (that HTML lives in
-- knowledge_renditions / book_renditions, a different table entirely). The
-- frontend's read-along mode has to slice the *annotated* HTML at these
-- boundaries to highlight the right span while still showing glosses and
-- letting words stay tappable — re-matching cue text against the rendered
-- HTML after the fact would silently drift the moment there's markup, a
-- repeated phrase, or a word the aligner had to skip.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audio_tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_kind  TEXT NOT NULL,     -- 'episode' (knowledge base item) | 'book_page' (from phase 3)
    owner_id    INTEGER NOT NULL,
    lang        TEXT NOT NULL,
    variant     TEXT NOT NULL DEFAULT 'fulltext',  -- 'fulltext' | 'summary'
    audio_path  TEXT NOT NULL,     -- relative path under data/audio/
    duration_ms INTEGER,
    cues_json   TEXT NOT NULL,     -- JSON array, see shape above
    source      TEXT NOT NULL,     -- 'tts' | 'anchored' | 'asr_cloud' | 'asr_local'
    voice       TEXT,
    -- The exact plain text handed to the aligner (routes/audio.py's
    -- _resolve_text() return value) — NOT the rendered/annotated HTML Daniel
    -- reads. cue char_start/char_end are offsets into THIS string. The
    -- frontend needs it verbatim to build its own text->DOM mapping (#1049);
    -- rows written before #1049 have NULL here and the frontend falls back
    -- to audio-only playback (no highlight) rather than guessing.
    source_text TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(owner_kind, owner_id, lang, variant)
);

CREATE INDEX IF NOT EXISTS idx_audio_tracks_owner ON audio_tracks(owner_kind, owner_id, lang);

-- ---------------------------------------------------------------------------
-- Local ASR job queue (#1053, phase 4 of the #1047 read-along umbrella):
-- whisper.cpp on this machine's 4 CPU cores (no GPU) runs roughly 1-3x
-- slower than realtime for large-v3, so it cannot run synchronously inside
-- an HTTP request the way audio/asr_cloud.py (Groq, seconds) does. Instead a
-- request that wants a local (free) transcript enqueues a row here and a
-- cron worker (scripts/audio_worker.py) picks it up only while Daniel is not
-- using the server (see main.py's activity timestamp + the worker's own
-- time-of-day window) and only outside the 05:30-09:30 morning pre-gen
-- window scripts/morning_pregen.py already owns.
--
-- One row per requested transcription, not one row per (owner, lang) like
-- audio_tracks — a job is a unit of WORK, not a cache entry; once it
-- finishes, the actual Track is written into audio_tracks exactly like every
-- other alignment path, and this row just records how that write happened
-- (and whether it's still pending).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audio_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_kind    TEXT NOT NULL,
    owner_id      INTEGER NOT NULL,
    lang          TEXT NOT NULL,
    variant       TEXT NOT NULL DEFAULT 'fulltext',
    audio_path    TEXT NOT NULL,     -- the audio to transcribe
    text_hint     TEXT,              -- known-correct text, if any -> anchored path; NULL -> plain ASR
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','running','done','error')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    started_at    TEXT,
    finished_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_audio_jobs_status ON audio_jobs(status, created_at);
