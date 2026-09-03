// ── Markdown renderer (notes field) ─────────────────────────────────────────
function renderMarkdown(text) {
  if (!text) return '';
  // Escape HTML first
  let html = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  // Bold: **text**
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // Italic: *text* (single asterisk, not matched by bold)
  html = html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
  // Split into lines for block-level processing
  const lines = html.split('\n');
  const out = [];
  let inList = false;
  for (const line of lines) {
    const li = line.match(/^[-*]\s+(.*)/);
    if (li) {
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push(`<li>${li[1]}</li>`);
    } else {
      if (inList) { out.push('</ul>'); inList = false; }
      if (line.trim() === '') {
        out.push('<br>');
      } else {
        out.push(`<p>${line}</p>`);
      }
    }
  }
  if (inList) out.push('</ul>');
  return out.join('');
}

// ── State ──────────────────────────────────────────────────────────────────
let deckId      = null;
let rootDeckId      = null;   // set when studying all categories (mixed mode)
let unfinishedMode  = false;  // set when studying the "Unfinished Cards" virtual deck
// Unfinished-deck options. Scope persists; story mode is re-chosen each session.
let _unfinishedScope     = localStorage.getItem('unfinishedScope') || 'unfinished'; // 'unfinished' | 'all'
let _unfinishedStoryMode = localStorage.getItem('unfinishedStoryMode') || 'existing'; // 'existing' | 'new'
let quickMode       = false;  // set when reviewing without AI story generation
let deckName    = '';
let category    = '';
let card        = null;   // current card dict from API
let story       = null;   // story dict with sentences[]
let sentence    = null;   // current sentence from story (may be null)
let wordDetails = null;   // full word data: examples + characters
let _currentWordId = null; // word ID open in word-detail view
let _currentHanziId = null;   // #1009: nav snapshots need the hanzi-detail id

let _prevView = null;      // view we came from before opening word-detail
let _sessionReviewedCount = 0; // cards rated this session (for clap animation)
let _sessionReviewedIds = [];  // card ids reviewed this session (for summary graph)
let userInput   = '';     // creating category: what the user typed
let clozeExtraWord = ''; // extra word blanked in cloze front (revealed on back)
let wordBankTokens = [];  // [{char, num}] shuffled non-target tokens
let wordBankOrder  = [];  // [{type:'char'|'target', char?, word?, num?}] original order
let browseWords  = [];   // all words from /api/browse-words
let browseAll    = [];   // kept for legacy (unused by new browse)
// Newest first is the default (#846): the words Daniel just added via ＋ / ★ List
// are the ones he comes to Browse for; alphabetical order buries them.
const DEFAULT_BROWSE_SORT = 'newest';
let _browseSort  = DEFAULT_BROWSE_SORT;
let _browseSelected = new Set();  // selected word IDs (multiselect)
let _browseDecks = [];            // flat deck list for move dropdown
let _browseDeckTree = [];         // top-level user decks (children of All) for sidebar tree
let optDeckId    = null; // deck whose options modal is open
const collapsed  = new Set(JSON.parse(localStorage.getItem('collapsedDecks') || '[]'));  // parent deck IDs that are collapsed
let _retentionData = null;  // cached result from GET /api/retention
let _cachedDecks = null;       // last fetched deck tree (for toggle re-renders)
let _deckLangById = {};        // deckId → 'zh'|'fr', rebuilt whenever decks load (flatten(decks))
let _availableLangs = ['zh'];  // distinct langs in use, from GET /api/langs — tab bar shows only when > 1
let _offlineMode = false;      // GET /api/mode — no outbound calls possible right now (#612)
let _localMode = false;        // GET /api/mode — laptop instance; shows the sync button (#625)
let _currentView = 'loading';  // last name passed to showView(), so the mode poll can re-apply it
let _modePollTimer = null;     // LOCAL_MODE only: re-checks connectivity every 60s (#625)
let _syncPollTimer = null;     // active /api/sync/progress poll (#625)

// The main-page language tab bar (issue #436) is the single source of truth for
// "what language am I studying right now" on the home page: deck list, All-deck
// aggregation, unfinished cards, and the stats charts all read this. Persisted
// so it survives reloads; defaults to 'zh' so pure-Chinese users see no change.
function activeLang() { return localStorage.getItem('activeLang') || 'zh'; }
// Query-string fragment for the active tab's lang — empty when only one
// language is in use, so pure-Chinese installs send no lang param at all
// (byte-identical to pre-#436 requests). Use `?${_langQ()}` when the URL has
// no query string yet, or `&${_langQ()}` when appending to an existing one
// (both are safe no-ops — trailing '?'/'&' with nothing after them — when
// _langQ() is empty, but callers still guard with `${_langQ() ? '&...' : ''}`
// style where a stray separator would look odd).
function _langQ() { return _availableLangs.length > 1 ? `lang=${activeLang()}` : ''; }
// Convenience: '?lang=fr' / '&lang=fr' / '' depending on separator + whether a tab bar is active.
function _langQP(sep) { const q = _langQ(); return q ? `${sep}${q}` : ''; }
function setActiveLang(lang) {
  if (lang === activeLang()) return;
  localStorage.setItem('activeLang', lang);
  _applyLangTheme();  // recolour immediately, don't wait for the reload (#824)
  invalidateHomeEvolution();
  // #804: a knowledge-base item detail view open at the moment of the switch
  // shows a per-language rendition of the summary — re-fetch it in the new
  // language instead of leaving the old language's text on screen under the
  // now-active tab. Only re-renders; doesn't change which view is showing.
  if (_knowledgeDetailId != null) openKnowledgeItem(_knowledgeDetailId);
  // #819: switching tabs while browsing reloads Browse in the new language
  // instead of dropping the user back on the deck list. loadDecks still runs,
  // because Browse reads _deckLangById / _cachedDecks for its sidebar — but it
  // must keep the view (#822): its parameter is an object, and a bare `true`
  // silently falls back to keepView=false, racing openBrowse() for the view.
  if (_currentView === 'browse') { loadDecks({ keepView: true }); openBrowse(); return; }
  loadDecks();
}

// Resolve the language of the card currently being reviewed. Prefers the
// card's own deck_id (set per-card in unfinished/mixed mode); falls back to
// the review view's current deckId. Defaults to 'zh' when unknown (e.g. decks
// not loaded yet), which keeps the existing Chinese-only affordances working.
function currentCardLang() {
  const id = (typeof card !== 'undefined' && card?.deck_id) ? card.deck_id
    : (typeof deckId !== 'undefined' ? deckId : null);
  if (id == null) return 'zh';
  return _deckLangById[id] || 'zh';
}

// Language of the story about to be generated (#908). Mirrors the server's
// rule — the `lang` parameter wins, the deck's own lang is only the fallback
// (see _langQ(), which sends that parameter under exactly this condition).
// Reading the deck's own lang was wrong for the aggregating root deck 'All':
// it is lang='zh' in the database yet reviews every language under the tab
// bar, so a French session rendered as Chinese — the editable Chinese prompt
// template button appeared (and edits there do nothing: non-zh generation
// goes through ai._KNOWLEDGE_PROMPT_NON_ZH, #806), the difficulty slider read
// "HSK", and the zh-only modes stayed selectable until the backend rejected
// them. currentCardLang() above deliberately keeps using the card's own deck
// (#726): the word being added comes from that card, not from the tab.
function setupLang() {
  return _availableLangs.length > 1 ? activeLang() : (_deckLangById[deckId] || 'zh');
}

// Shared 1-6 difficulty value → per-language label (issue #596):
// zh uses HSK levels, every other language the CEFR scale (A1=1 … C2=6).
const CEFR_LABELS = { 1: 'A1', 2: 'A2', 3: 'B1', 4: 'B2', 5: 'C1', 6: 'C2' };
function levelLabel(lang, lvl) {
  return lang === 'zh' ? `HSK ${lvl}` : (CEFR_LABELS[lvl] || `${lvl}`);
}

// — Customizable review shortcuts (#856 expands this to cover almost every
// hardcoded shortcut in the app, not just the original review-view 10) —
// Each action maps to one key. Defaults below are the active bindings, and
// match byte-for-byte what used to be hardcoded — this is a pure
// configurability upgrade, not a behavior change.
// User overrides persist in localStorage('reviewKeymap'). Rating keys 1-4,
// Escape, Enter and Tab stay fixed (see KEYMAP_RESERVED).
//
// `scope` says which views/contexts an action's shortcut applies in, and is
// used only for conflict detection in the settings UI when the user tries to
// rebind a key — it does NOT gate where the keydown handler's own code runs
// (that's still whatever view checks the handler already has).
const KEYMAP_SCOPES = {
  // NOTE: `global` deliberately excludes `story`. The story-modal keydown
  // branch runs first and returns early whenever the modal is open, before
  // the global-nav branch is ever reached — so a `global` binding never
  // actually competes with a `story` binding for the same keypress, even
  // though nav-back/story-next share the default key 'd'. See the keydown
  // handler around line ~10880 (search for "storyOverlay").
  global:        ['review', 'word-detail', 'home'],
  review:        ['review'],
  shared:        ['review', 'word-detail'],
  'word-detail': ['word-detail'],
  home:          ['home'],
  story:         ['story'],
};
function _scopeSet(scope) { return new Set(KEYMAP_SCOPES[scope] || [scope]); }
// Display order + labels for the grouped settings-page rendering (#856).
const KEYMAP_SCOPE_GROUPS = [
  { scope: 'review',       name: 'Review' },
  { scope: 'shared',       name: 'Review + word detail' },
  { scope: 'word-detail',  name: 'Word detail' },
  { scope: 'story',        name: 'Story player' },
  { scope: 'global',       name: 'Navigation' },
  { scope: 'home',         name: 'Home' },
];
function _scopeDisplayName(scope) {
  return (KEYMAP_SCOPE_GROUPS.find(g => g.scope === scope) || {}).name || scope;
}

const KEYMAP_DEFAULTS = {
  reveal:         ' ',
  replay:         'q',
  pinyin:         'p',
  translation:    't',
  worddef:        'k',
  'new-sentence': '5',
  undo:           'z',
  'hint-minus':   'a',
  'hint-plus':    's',
  'story-modal':  'x',
  // shared (review card-back + word-detail page)
  examples:       'e',
  notes:          'n',
  'word-analysis':'w',
  'regen-all':    'C',
  // review-only
  'suspend-reading':   'f',
  'suspend-listening': 'v',
  'suspend-creating':  'c',
  'delete-card':       'D',
  'delete-card-alt':   '7',
  leech:               'L',
  'deck-options':      'o',
  reasoning:           'g',
  'restart-server':    'R',
  'star-sentence':     'F',
  'flag-sentence':     'G',
  'fsrs-inspector':    'S',
  // word-detail only
  relations:      'r',
  // story modal
  'story-play':   ' ',
  'story-prev':   'a',
  'story-repeat': 's',
  'story-next':   'd',
  // global navigation
  'nav-back':      'd',
  'nav-browse':    'b',
  'nav-add-card':  'a',
  // #927: ⌘A has always opened the add-word modal (#788), but a Cmd combo can't
  // be shown in the keymap list (single keys only), so the action was invisible
  // there. Listed with no default binding — ⌘A keeps working either way, so
  // this is a pure discoverability fix, not a behavior change.
  'add-word':      null,
  // home (decks view)
  'home-listening': 'l',
  'home-creating':  'c',
};
const KEYMAP_ACTIONS = [
  { id: 'reveal',       label: 'Reveal answer',                scope: 'review' },
  { id: 'replay',       label: 'Replay audio',                 scope: 'review' },
  { id: 'pinyin',       label: 'Toggle pinyin',                scope: 'review' },
  { id: 'translation',  label: 'Toggle translation',           scope: 'review' },
  { id: 'worddef',      label: 'Toggle word definition',       scope: 'review' },
  { id: 'new-sentence', label: 'New sentence (regenerate)',    scope: 'review' },
  { id: 'undo',         label: 'Undo last review',             scope: 'review' },
  { id: 'hint-minus',   label: 'Listening hint −',             scope: 'review' },
  { id: 'hint-plus',    label: 'Listening hint +',             scope: 'review' },
  { id: 'story-modal',  label: 'Open summary (full story)',    scope: 'review' },

  { id: 'examples',       label: 'Toggle examples',            scope: 'shared' },
  { id: 'notes',           label: 'Toggle notes',               scope: 'shared' },
  { id: 'word-analysis',   label: 'Toggle word analysis / etymology', scope: 'shared' },
  { id: 'regen-all',       label: 'Regenerate all fields',      scope: 'shared' },

  { id: 'suspend-reading',   label: 'Suspend/resume reading',     scope: 'review' },
  { id: 'suspend-listening', label: 'Suspend/resume listening',   scope: 'review' },
  { id: 'suspend-creating',  label: 'Suspend/resume creating',    scope: 'review' },
  { id: 'delete-card',       label: 'Delete card',                scope: 'review' },
  { id: 'delete-card-alt',   label: 'Delete card (alt key)',      scope: 'review' },
  { id: 'leech',             label: 'Mark as leech',              scope: 'review' },
  { id: 'deck-options',      label: 'Deck options',               scope: 'review' },
  { id: 'reasoning',         label: 'Background popup / context language', scope: 'review' },
  { id: 'restart-server',    label: 'Restart server',             scope: 'review' },
  { id: 'star-sentence',     label: 'Star this sentence',         scope: 'review' },
  { id: 'flag-sentence',     label: 'Flag this sentence',         scope: 'review' },
  { id: 'fsrs-inspector',    label: 'FSRS scheduler inspector',   scope: 'review' },

  { id: 'relations',    label: 'Toggle relations',             scope: 'word-detail' },

  { id: 'story-play',   label: 'Play / pause full story',      scope: 'story' },
  { id: 'story-prev',   label: 'Previous sentence',            scope: 'story' },
  { id: 'story-repeat', label: 'Repeat sentence',               scope: 'story' },
  { id: 'story-next',   label: 'Next sentence',                scope: 'story' },

  { id: 'nav-back',      label: 'Back',                        scope: 'global' },
  { id: 'nav-browse',    label: 'Open Browse',                 scope: 'global' },
  { id: 'nav-add-card',  label: 'New card',                    scope: 'global' },
  { id: 'add-word',      label: 'Add a new word (also ⌘A)',    scope: 'global' },

  { id: 'home-listening', label: 'All deck · Listening',       scope: 'home' },
  { id: 'home-creating',  label: 'All deck · Creating',        scope: 'home' },
];
// Keys that can never be reassigned to: rating keys, and the fixed
// navigation/editing keys used throughout the app.
const KEYMAP_RESERVED = ['1','2','3','4','Enter','Tab','Escape'];
// Modifier keys fire a keydown of their own before the key they modify (#885);
// the rebinding capture must skip those instead of storing them as the binding.
const KEYMAP_MODIFIER_KEYS = ['Shift', 'Control', 'Alt', 'Meta', 'CapsLock', 'AltGraph'];
function _loadKeymap() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem('reviewKeymap') || '{}'); } catch (e) {}
  return { ...KEYMAP_DEFAULTS, ...saved };
}
let _keymap = _loadKeymap();
function _key(id) { return _keymap[id]; }
function _saveKeymap() {
  localStorage.setItem('reviewKeymap', JSON.stringify(_keymap));
  if (typeof _syncShortcutTitles === 'function') _syncShortcutTitles();
}
// A single uppercase letter as e.key means Shift was held (#856) — spell that
// out, otherwise the settings page shows a bare "F" for what you press as
// Shift+F. Digits and symbols are shown as-is.
function _keyLabel(k) {
  if (k == null) return 'None';
  if (k === ' ') return 'Space';
  if (k.length !== 1) return k;
  if (k >= 'A' && k <= 'Z') return `Shift+${k}`;
  return k.toUpperCase();
}

// Keeps the static `title="… (X)"` shortcut hints in index.html in sync with
// the current keymap (#856) — elements opt in via data-shortcut-action (the
// KEYMAP_ACTIONS id) + data-shortcut-title (the hint text without the key).
// An optional data-shortcut-suffix is appended after the key, inside the
// parens (used by the FSRS inspector's "Close (S / Esc)").
// Unbound actions drop the parenthetical entirely rather than show "(None)".
function _syncShortcutTitles() {
  document.querySelectorAll('[data-shortcut-action]').forEach(el => {
    const base = el.dataset.shortcutTitle || '';
    const k = _key(el.dataset.shortcutAction);
    if (k == null) { el.title = base; return; }
    const suffix = el.dataset.shortcutSuffix || '';
    el.title = `${base} (${_keyLabel(k)}${suffix})`;
  });
}

let _timerInterval = null;
let _timerStart = null;
const _TIMER_CAP_MS = 40000;  // beyond this the user is likely doing something else
let _sessionTotalMs = 0;
let _sessionRatedCount = 0;

// ── Card schedule calendar ───────────────────────────────────────────────────
let _calData     = null;   // {history, future} from API
let _calYear     = null;
let _calMonth    = null;   // 0-based
let _calCategory = null;   // current card's category — shown on today even if not in dues
let _calTimeline = null;   // {cards} from /api/cards/{id}/timeline — for per-day state borders
let _calFocusCat = null;   // focus category — its chips stay full, other categories fade

// Fade level for non-focus category chips — user-adjustable, persisted.
let _calFade = (() => {
  const v = parseFloat(localStorage.getItem('calFade'));
  return v >= 0.15 && v <= 1 ? v : 0.3;
})();
function _calFadeApply() { document.documentElement.style.setProperty('--cal-fade', _calFade); }
function setCalFade(v) {
  _calFade = parseFloat(v);
  localStorage.setItem('calFade', _calFade);
  _calFadeApply();
  document.querySelectorAll('.cal-fade-input').forEach(el => {
    if (parseFloat(el.value) !== _calFade) el.value = _calFade;
  });
}
function _calFadeSliderHtml() {
  return `<label class="cal-fade-ctl" title="Other-category opacity">
    <span>Fade</span>
    <input type="range" class="cal-fade-input" min="0.15" max="1" step="0.05"
           value="${_calFade}" oninput="setCalFade(this.value)">
  </label>`;
}

const _RATING_CLASS = { 1: 'again', 2: 'hard', 3: 'good', 4: 'easy' };
const _CAT_CLASS    = { listening: 'listening', reading: 'reading', creating: 'creating' };

function _calKey(dateStr) { return dateStr; }  // "YYYY-MM-DD"

const _CAT_LETTER = { listening: '听', reading: '读', creating: '创' };

function _buildCalDayMap() {
  // Deduplicate: per (date, category) keep only the last review
  const histByKey = {};
  for (const h of (_calData?.history || [])) {
    histByKey[`${h.date}|${h.category}`] = h;
  }
  const dueByKey = {};
  for (const f of (_calData?.future || [])) {
    dueByKey[`${f.due}|${f.category}`] = f;
  }

  const map = {};
  for (const h of Object.values(histByKey)) {
    if (!map[h.date]) map[h.date] = { ratings: [], dues: [] };
    map[h.date].ratings.push({ rating: h.rating, category: h.category });
  }
  for (const f of Object.values(dueByKey)) {
    if (!map[f.due]) map[f.due] = { ratings: [], dues: [] };
    map[f.due].dues.push({ category: f.category, state: f.state });
  }
  return map;
}

function _renderCal(timelineId = 'cal-timeline', panelId = 'review-cal-panel') {
  const timelineEl = document.getElementById(timelineId);
  if (!timelineEl) return;
  _calFadeApply();

  // Per-(category, date) card state, for the colored chip borders. Built from
  // the timeline data so each review chip shows the state the card was in then.
  const stateByCatDate = {};
  for (const card of (_calTimeline?.cards || [])) {
    const m = stateByCatDate[card.category] = {};
    for (const p of (card.points || [])) m[p.at.slice(0, 10)] = p.state;
  }

  const today = new Date();
  const todayStr = today.toISOString().slice(0, 10);
  const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const dayMap = _buildCalDayMap();

  // Range: first history date → today + 3 months
  const allDates = [
    ...(_calData?.history || []).map(h => h.date),
    ...(_calData?.future  || []).map(f => f.due),
  ];
  let startDate = today;
  if (allDates.length) {
    const minStr = allDates.reduce((a, b) => a < b ? a : b);
    const parsed = new Date(minStr);
    if (!isNaN(parsed)) startDate = parsed;
  }
  const endDate = new Date(today.getFullYear(), today.getMonth() + 4, 0); // last day of today+3 months

  // Find first review date to scroll to on open
  const histDates = (_calData?.history || []).map(h => h.date).filter(Boolean).sort();
  let firstMonthId = null;
  if (histDates.length) {
    const firstParsed = new Date(histDates[0]);
    if (!isNaN(firstParsed)) {
      firstMonthId = `cal-month-${firstParsed.getFullYear()}-${firstParsed.getMonth()}`;
    }
  }

  let html = '';
  let yr = startDate.getFullYear(), mo = startDate.getMonth();
  const endYr = endDate.getFullYear(), endMo = endDate.getMonth();
  let todayMonthId = null;

  while (yr < endYr || (yr === endYr && mo <= endMo)) {
    const monthId = `cal-month-${yr}-${mo}`;
    if (yr === today.getFullYear() && mo === today.getMonth()) todayMonthId = monthId;

    html += `<div class="cal-month-block" id="${monthId}">`;
    html += `<div class="cal-month-heading">${monthNames[mo]} ${yr}</div>`;
    html += `<div class="cal-weekdays"><span>Mo</span><span>Tu</span><span>We</span><span>Th</span><span>Fr</span><span>Sa</span><span>Su</span></div>`;
    html += `<div class="cal-grid">`;

    const firstDay = new Date(yr, mo, 1);
    let startOffset = firstDay.getDay() - 1;
    if (startOffset < 0) startOffset = 6;
    for (let i = 0; i < startOffset; i++) html += '<div class="cal-cell cal-empty"></div>';

    const daysInMonth = new Date(yr, mo + 1, 0).getDate();
    for (let d = 1; d <= daysInMonth; d++) {
      const mm = String(mo + 1).padStart(2, '0');
      const dd = String(d).padStart(2, '0');
      const dateStr = `${yr}-${mm}-${dd}`;
      const isToday = dateStr === todayStr;
      const info = dayMap[dateStr];

      // The current category's chip is suppressed (we're already reviewing it),
      // so only ratings + other-category dues count as visible content. A date
      // whose only due is the current category must render like an empty day:
      // no grey "has-future" background, and its day number must still show.
      const ratings     = info?.ratings || [];
      const visibleDues = (info?.dues || []).filter(f => f.category !== _calCategory);
      const hasVisible   = ratings.length > 0 || visibleDues.length > 0;
      const hasFutureDue = dateStr > todayStr && visibleDues.length > 0;
      html += `<div class="cal-cell${isToday ? ' cal-today' : ''}${hasFutureDue ? ' cal-has-future' : ''}">`;
      if (hasVisible) {
        html += '<div class="cal-chips">';
        for (const r of ratings) {
          const rCls = _RATING_CLASS[r.rating] || 'good';
          const st = stateByCatDate[r.category]?.[dateStr];
          const faded = (_calFocusCat && r.category !== _calFocusCat) ? ' cal-chip-faded' : '';
          const cls = `cal-chip cal-chip-${rCls}${st ? ' cal-chip-state' : ''}${faded}`;
          const style = st ? ` style="border-color:${_STATE_COLOR[st]}"` : '';
          const stTip = st ? ` · ${_CGRAPH_LABEL[st] || st}` : '';
          // No category glyph: the active category shows full colour, others fade.
          html += `<span class="${cls}"${style} title="${r.category}: ${rCls}${stTip}"></span>`;
        }
        for (const f of visibleDues) {
          const cCls = _CAT_CLASS[f.category] || '';
          const faded = (_calFocusCat && f.category !== _calFocusCat) ? ' cal-chip-faded' : '';
          html += `<span class="cal-chip cal-chip-due cal-chip-due-${cCls}${faded}" title="${f.category} due"></span>`;
        }
        html += '</div>';
      } else if (isToday && _calCategory) {
        const cCls = _CAT_CLASS[_calCategory] || '';
        html += `<div class="cal-chips"><span class="cal-chip cal-chip-due cal-chip-due-${cCls}" title="${_calCategory} today"></span></div>`;
      } else {
        html += `<span class="cal-day-num${isToday ? ' cal-day-num-today' : ''}">${d}</span>`;
      }
      html += '</div>';
    }

    html += '</div></div>'; // close cal-grid + cal-month-block

    mo++;
    if (mo > 11) { mo = 0; yr++; }
  }

  timelineEl.innerHTML = html;

  // Scroll to first reviewed month (or today if no history)
  const scrollTargetId = firstMonthId || todayMonthId;
  if (scrollTargetId && panelId) {
    requestAnimationFrame(() => {
      const panel = document.getElementById(panelId);
      const el    = document.getElementById(scrollTargetId);
      if (panel && el) {
        const panelRect = panel.getBoundingClientRect();
        const elRect    = el.getBoundingClientRect();
        panel.scrollTop += elRect.top - panelRect.top;
      }
    });
  }
}

async function _loadCardTile(cardId, category) {
  const panel = document.getElementById('review-cal-panel');
  _calData     = null;
  _ctlData     = null;
  _calCategory = category || null;
  _ctlCategory = category || null;
  if (panel) panel.style.display = 'none';
  try {
    const [cal, tl] = await Promise.all([
      api('GET', `/api/cards/${cardId}/calendar`),
      api('GET', `/api/cards/${cardId}/timeline`).catch(() => null),
    ]);
    if (!cal && !tl) return;
    _calData = cal;
    _ctlData = tl;
    const today = new Date();
    _calYear  = today.getFullYear();
    _calMonth = today.getMonth();
    _renderCardTile();
    if (panel) panel.style.display = '';
  } catch (e) { /* silently skip if unavailable */ }
}

// ── Card interval graph + calendar (issue #323) — graph on top, calendar below
let _ctlData     = null;   // {cards} from /api/cards/{id}/timeline (review view)
let _ctlCategory = null;   // category of the card being reviewed

// Colorblind-safe card-state palette (Okabe-Ito). Tuned for Daniel's red-green
// CB: no green, no red, and — crucially — no black, since black vs dark-blue is
// the pair he could not tell apart on thin lines. The four states now sit on
// hues he reads reliably: light sky blue, orange, dark blue, magenta. The two
// blues are kept far apart in lightness; orange↔magenta differ on the blue↔
// yellow axis. Every pair is distinguishable under deuteranopia/protanopia.
const _STATE_COLOR = {
  new:      '#56B4E9',  // sky blue (light)
  learning: '#E69F00',  // orange
  review:   '#0072B2',  // blue (dark)
  relearn:  '#CC79A7',  // magenta / reddish purple
};
const _CGRAPH_COLOR = _STATE_COLOR;

// Chinese label + colour shown when a card's state changes during review.
// 'suspended' here always means a leech (review can only suspend via leech).
const _STATE_ANIM = {
  new:       { text: '新词',     color: _STATE_COLOR.new },
  learning:  { text: '学习中',   color: _STATE_COLOR.learning },
  review:    { text: '学会了',   color: _STATE_COLOR.review },
  relearn:   { text: '重新学习', color: _STATE_COLOR.relearn },
  suspended: { text: '难词！',   color: '#b45309' },
};

// Floating Chinese state-change cue: fades + scales in, drifts up, removes itself.
function showStateChangeAnim(transition) {
  let key = transition?.to;
  // Graduating to 'review' but below learned_interval isn't "learned" yet —
  // show the learning cue instead of "学会了".
  if (key === 'review' && transition?.learned === false) key = 'learning';
  const info = _STATE_ANIM[key];
  if (!info) return;
  const el = document.createElement('div');
  el.className = 'state-anim';
  el.textContent = info.text;
  el.style.color = info.color;
  document.body.appendChild(el);
  el.addEventListener('animationend', () => el.remove(), { once: true });
  // Safety net in case animationend never fires (e.g. reduced-motion)
  setTimeout(() => el.remove(), 2000);
}
const _CGRAPH_LABEL = { new: 'New', learning: 'Learning', review: 'Learnt', relearn: 'Relearn' };
const _CGRAPH_RATING = { 1: 'Again', 2: 'Hard', 3: 'Good', 4: 'Easy' };

// Review-view tile: interval graph stacked above the calendar
function _renderCardTile() {
  const g = document.getElementById('card-graph');
  if (g) {
    const cards = _ctlData?.cards || [];
    const card = cards.find(k => k.category === _ctlCategory) || cards[0];
    g.innerHTML = _cardGraphHtml(card);
  }
  // Scroll the calendar's own container (not the outer panel) so the graph
  // above it stays visible instead of being pushed out of view.
  const fadeRow = document.getElementById('cal-fade-row');
  if (fadeRow) fadeRow.innerHTML = _calFadeSliderHtml();
  _calTimeline = _ctlData;
  _calFocusCat = _ctlCategory;
  if (_calData) _renderCal('cal-timeline', 'card-calendar');
}

// Format a scheduled interval (in days) for tooltips: sub-day → min/h, else days.
function _fmtIval(days) {
  if (days >= 1) return `${Math.round(days)}d`;
  const mins = Math.round(days * 1440);
  if (mins < 60) return `${mins}m`;
  return `${Math.round(mins / 60)}h`;
}

// Shared SVG renderer: x = time, y = interval (days), colored by card state
function _cardGraphHtml(card) {
  const pts = (card?.points || []).slice();
  if (card?.scheduled) pts.push({ ...card.scheduled, scheduled: true });
  if (!pts.length) return '<div class="cgraph-empty">No reviews yet.</div>';

  const W = 340, H = 150, PT = 8, PB = 6, PL = 6, PR = 8;
  const t = s => new Date(s.replace(' ', 'T')).getTime();
  const t0 = t(pts[0].at);
  let t1 = t(pts[pts.length - 1].at);
  if (t1 <= t0) t1 = t0 + 86400000;
  let ymax = Math.max(1, ...pts.map(p => p.gap));
  ymax = ymax <= 5 ? Math.ceil(ymax) : ymax <= 30 ? Math.ceil(ymax / 5) * 5 : Math.ceil(ymax / 10) * 10;

  const x = p => PL + (t(p.at) - t0) / (t1 - t0) * (W - PL - PR);
  const y = p => PT + (1 - p.gap / ymax) * (H - PT - PB);

  let svg = [0.5, 1].map(f => {
    const gy = (PT + (1 - f) * (H - PT - PB)).toFixed(1);
    return `<line x1="${PL}" y1="${gy}" x2="${W - PR}" y2="${gy}" stroke="var(--border)" stroke-width="0.6"/>`;
  }).join('');
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1], b = pts[i];
    const color = _CGRAPH_COLOR[b.state] || 'var(--muted)';
    svg += `<line x1="${x(a).toFixed(1)}" y1="${y(a).toFixed(1)}" x2="${x(b).toFixed(1)}" y2="${y(b).toFixed(1)}"
              stroke="${color}" stroke-width="2.8" stroke-linecap="round"${b.scheduled ? ' stroke-dasharray="5 4"' : ''}/>`;
  }
  // Small tick under every data point so the x-axis marks the days with data
  const baseY = (H - PB).toFixed(1);
  svg += pts.map(p =>
    `<line x1="${x(p).toFixed(1)}" y1="${baseY}" x2="${x(p).toFixed(1)}" y2="${(H - PB + 3).toFixed(1)}"
           stroke="var(--muted)" stroke-width="0.8" opacity="0.7"/>`).join('');
  svg += pts.map(p => {
    const color = _CGRAPH_COLOR[p.state] || 'var(--muted)';
    const day = p.at.slice(0, 10);
    const tip = p.scheduled
      ? `${day} · due · interval ${_fmtIval(p.gap)}`
      : `${day} · interval ${_fmtIval(p.gap)} · ${_CGRAPH_RATING[p.rating] || ''} · ${_CGRAPH_LABEL[p.state] || p.state}`;
    return `<circle cx="${x(p).toFixed(1)}" cy="${y(p).toFixed(1)}" r="4"
              fill="${p.scheduled ? 'var(--card)' : color}" stroke="${color}" stroke-width="2.2"><title>${tip}</title></circle>`;
  }).join('');

  const legend = Object.keys(_CGRAPH_LABEL).map(k =>
    `<span class="evo-leg"><span class="hcal-leg-sw" style="background:${_CGRAPH_COLOR[k]}"></span>${_CGRAPH_LABEL[k]}</span>`).join('');
  const fmtD = s => { const [m, d] = s.slice(5, 10).split('-'); return `${+m}/${+d}`; };

  // Label each data point's date along the x-axis, thinning so labels never
  // overlap (keep one only if far enough from the last kept), always keeping
  // the first and last point.
  const MIN_GAP = 30;  // viewBox units between adjacent labels
  const keep = [];
  let lastX = -Infinity;
  for (const p of pts) {
    const px = x(p);
    if (px - lastX >= MIN_GAP) { keep.push({ p, px }); lastX = px; }
  }
  const lastPt = pts[pts.length - 1];
  if (!keep.length || keep[keep.length - 1].p !== lastPt) {
    const lpx = x(lastPt);
    if (keep.length && lpx - keep[keep.length - 1].px < MIN_GAP) keep.pop();
    keep.push({ p: lastPt, px: lpx });
  }
  const xlabels = keep.map(({ p, px }) => {
    const pct = Math.min(97.5, Math.max(2.5, px / W * 100));  // clamp so edge labels aren't clipped
    return `<span class="cgraph-xlabel" style="left:${pct.toFixed(2)}%">${fmtD(p.at)}</span>`;
  }).join('');

  return `
    <div class="cgraph-wrap">
      <span class="cgraph-ymax">${ymax}d</span>
      <svg class="cgraph-svg" viewBox="0 0 ${W} ${H}">${svg}</svg>
      <div class="cgraph-xaxis">${xlabels}</div>
      <div class="cgraph-legend">${legend}<span class="evo-leg">╌╌ scheduled</span></div>
    </div>`;
}

// ── Session summary graph (issue #337) ───────────────────────────────────────
// Overlays every reviewed card's interval timeline. Hover a line → its word;
// click → open that card's browse (word detail).

// Distinct line colours, cycled per card. Chosen to stay separable for Daniel's
// red-green CB (no red/green pairs adjacent; spans blue↔orange↔purple).
const _SUMMARY_PALETTE = [
  '#0072B2', '#E69F00', '#CC79A7', '#56B4E9',
  '#9467bd', '#8c564b', '#117733', '#882255',
];

async function openSessionSummary() {
  const ids = [...new Set(_sessionReviewedIds)];
  const body = document.getElementById('session-summary-body');
  document.getElementById('session-summary-overlay').style.display = 'block';
  document.getElementById('session-summary-modal').style.display = 'block';
  if (!ids.length) {
    body.innerHTML = '<div class="cgraph-empty">No cards reviewed yet this session.</div>';
    return;
  }
  body.innerHTML = '<div class="cgraph-empty">Loading…</div>';
  try {
    const data = await api('POST', '/api/session-timelines', { ids });
    body.innerHTML = _sessionSummaryHtml(data.cards || []);
  } catch (e) {
    body.innerHTML = `<div class="cgraph-empty">Failed to load: ${e.message}</div>`;
  }
}

function closeSessionSummary() {
  document.getElementById('session-summary-overlay').style.display = 'none';
  document.getElementById('session-summary-modal').style.display = 'none';
}

function sumLineClick(wordId) {
  closeSessionSummary();
  openWordDetail(wordId);
}

function _sessionSummaryHtml(cards) {
  // Keep only cards that actually have a line to draw
  const drawn = cards.filter(c => (c.points || []).length);
  if (!drawn.length) return '<div class="cgraph-empty">No interval history yet for these cards.</div>';

  const W = 600, H = 340, PT = 14, PB = 20, PL = 30, PR = 14;
  const t = s => new Date(s.replace(' ', 'T')).getTime();

  // Each card's full point list (reviews + the scheduled due point)
  const series = drawn.map(c => {
    const pts = (c.points || []).slice();
    if (c.scheduled) pts.push({ ...c.scheduled, scheduled: true });
    return { card: c, pts };
  });

  const allPts = series.flatMap(s => s.pts);
  let t0 = Math.min(...allPts.map(p => t(p.at)));
  let t1 = Math.max(...allPts.map(p => t(p.at)));
  if (t1 <= t0) t1 = t0 + 86400000;
  const ymax = Math.max(1, ...allPts.map(p => p.gap));

  const x = p => PL + (t(p.at) - t0) / (t1 - t0) * (W - PL - PR);
  // sqrt scale on y so short and long intervals are both legible
  const y = p => PT + (1 - Math.sqrt(p.gap) / Math.sqrt(ymax)) * (H - PT - PB);

  // Horizontal gridlines + interval labels at a few sqrt-spaced levels
  let svg = '';
  const yTicks = [0, ymax * 0.25, ymax * 0.5, ymax].map(v => Math.round(v));
  for (const v of [...new Set(yTicks)]) {
    const gy = (PT + (1 - Math.sqrt(v) / Math.sqrt(ymax)) * (H - PT - PB)).toFixed(1);
    svg += `<line x1="${PL}" y1="${gy}" x2="${W - PR}" y2="${gy}" stroke="var(--border)" stroke-width="0.6"/>`;
    svg += `<text x="${PL - 4}" y="${(+gy + 3).toFixed(1)}" text-anchor="end" font-size="9" fill="var(--muted)">${v}d</text>`;
  }

  // One group per card: polyline (+dashed scheduled tail), hover shows the word
  series.forEach((s, i) => {
    const color = _SUMMARY_PALETTE[i % _SUMMARY_PALETTE.length];
    const real = s.card.points.map(p => `${x(p).toFixed(1)},${y(p).toFixed(1)}`).join(' ');
    let body = `<polyline points="${real}" fill="none" stroke="${color}"/>`;
    if (s.card.scheduled && s.card.points.length) {
      const a = s.card.points[s.card.points.length - 1], b = s.card.scheduled;
      body += `<line x1="${x(a).toFixed(1)}" y1="${y(a).toFixed(1)}" x2="${x(b).toFixed(1)}" y2="${y(b).toFixed(1)}"
                 stroke="${color}" stroke-dasharray="4 3"/>`;
    }
    body += s.pts.map(p => `<circle cx="${x(p).toFixed(1)}" cy="${y(p).toFixed(1)}" r="2.5" fill="${color}"/>`).join('');
    const label = `${s.card.word_zh}${s.card.pinyin ? ' ' + s.card.pinyin : ''} · ${_CGRAPH_LABEL[s.card.state] || s.card.state}`;
    svg += `<g class="sum-card" onclick="sumLineClick(${s.card.word_id})"><title>${label}</title>${body}</g>`;
  });

  const fmtD = s => { const [m, d] = s.slice(5, 10).split('-'); return `${+m}/${+d}`; };
  const d0 = new Date(t0), d1 = new Date(t1);
  const iso = dt => dt.toISOString().slice(0, 10);

  return `
    <div class="session-summary-info">${drawn.length} cards · y = scheduled interval (days) · hover a line to see the word, click to open it</div>
    <div class="cgraph-wrap">
      <svg class="session-summary-svg" viewBox="0 0 ${W} ${H}">${svg}</svg>
      <div class="hcal-graph-axis"><span>${fmtD(iso(d0))}</span><span>${fmtD(iso(d1))}</span></div>
    </div>`;
}

// ── Word-detail tile (browse) ────────────────────────────────────────────────
let _wdTlData  = null;
let _wdCalData = null;
let _wdCat     = null;

function _wdLoadCardTile(cards) {
  const el = document.getElementById('wd-card-tile-section');
  if (!el) return;
  _wdTlData = null;
  _wdCalData = null;
  const withId = (cards || []).filter(c => c.id);
  if (!withId.length) { el.innerHTML = ''; return; }
  _wdCat = withId[0].category;
  el.innerHTML = '<div class="hcal-loading">Loading schedule…</div>';
  Promise.all([
    api('GET', `/api/cards/${withId[0].id}/timeline`),
    api('GET', `/api/cards/${withId[0].id}/calendar`),
  ]).then(([tl, cal]) => {
    _wdTlData = tl;
    _wdCalData = cal;
    const withPts = (tl?.cards || []).find(c => c.points.length);
    if (withPts) _wdCat = withPts.category;
    _wdRenderCardTile();
  }).catch(() => { el.innerHTML = ''; });
}

function wdSetTileCat(cat) { _wdCat = cat; _wdRenderCardTile(); }

function _wdRenderCardTile() {
  const el = document.getElementById('wd-card-tile-section');
  if (!el || !_wdTlData) return;
  const catBtns = (_wdTlData.cards || []).map(c =>
    `<button class="hcal-seg-btn ${c.category === _wdCat ? 'active' : ''}"
             onclick="wdSetTileCat('${c.category}')">${_CAT_LETTER[c.category] || c.category}</button>`).join('');
  const cards = _wdTlData.cards || [];
  const card = cards.find(c => c.category === _wdCat) || cards[0];
  el.innerHTML = `
    <div class="section-label">Schedule</div>
    <div class="wd-card-tile">
      <div class="card-tile-head">
        <div class="hcal-seg">${catBtns}</div>
        ${_calFadeSliderHtml()}
      </div>
      <div id="wd-tile-graph">${_cardGraphHtml(card)}</div>
      <div class="card-calendar wd-cal-scroll" id="wd-cal-scroll"><div id="wd-cal-timeline"></div></div>
    </div>`;
  if (_wdCalData) {
    const saved = _calData, savedCat = _calCategory, savedTl = _calTimeline, savedFocus = _calFocusCat;
    _calData = _wdCalData;
    _calCategory = null;
    _calTimeline = _wdTlData;
    _calFocusCat = _wdCat;
    _renderCal('wd-cal-timeline', 'wd-cal-scroll');
    _calData = saved;
    _calCategory = savedCat;
    _calTimeline = savedTl;
    _calFocusCat = savedFocus;
  }
}

// ── Card timer ──────────────────────────────────────────────────────────────
function _startTimer() {
  _stopTimer();
  _timerStart = Date.now();
  const el = document.getElementById('card-timer');
  el.classList.remove('card-timer-capped');
  el.textContent = '0s';
  el.style.display = 'block';
  _timerInterval = setInterval(() => {
    const ms = Date.now() - _timerStart;
    if (ms >= _TIMER_CAP_MS) {
      // Freeze at the cap — the time past 40s won't count toward the average.
      el.textContent = '40s';
      el.classList.add('card-timer-capped');
      clearInterval(_timerInterval); _timerInterval = null;
      return;
    }
    const s = Math.floor(ms / 1000);
    el.textContent = s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${s % 60}s`;
  }, 1000);
}
function _stopTimer() {
  if (_timerInterval) { clearInterval(_timerInterval); _timerInterval = null; }
  document.getElementById('card-timer').style.display = 'none';
}
function _updateAvgTimeBadge() {
  const el = document.getElementById('avg-time-badge');
  if (_sessionRatedCount === 0) { el.style.display = 'none'; return; }
  const avgS = Math.round(_sessionTotalMs / _sessionRatedCount / 1000);
  const label = avgS < 60 ? `${avgS}s` : `${Math.floor(avgS / 60)}m${avgS % 60}s`;
  el.textContent = `avg ${label}/card`;
  el.style.display = 'inline';
}

// ── Story info row (Sentence x/y · Topic) ───────────────────────────────────
function _updateStoryInfoRow() {
  const row = document.getElementById('story-info-row');
  if (sentence && story?.sentences?.length) {
    const pos = `Sentence ${sentence.position + 1} / ${story.sentences.length}`;
    // Kontextsummary / paste / kahneman / …: show the mode name + story date to the
    // right of the counter (issue #452). Plain stories keep the topic.
    // podcast kept alongside knowledge (issue #654 renamed the mode identifier
    // going forward, but historical mode='podcast' stories still display fine).
    const modeName = { kahneman: 'Kahneman', news: 'News', briefing: 'News flow', contextsummary: 'Kontextsummary', paste: 'Paste', podcast: 'Podcast', knowledge: 'Knowledge', book: 'Book' }[_activeStoryMode()];
    const parts = [pos];
    if (modeName) {
      const date = String(story.date || story.generated_at || '').slice(0, 10);
      parts.push(date ? `${modeName} · ${date}` : modeName);
    } else if (story.topic) {
      parts.push(story.topic);
    }
    row.innerHTML = `<span class="story-info-label">${parts.join('  ·  ')}</span><button class="story-regen-btn" onclick="event.stopPropagation();regenerateStory()" title="Regenerate story">↺</button>`;
    row.style.display = 'flex';
  } else {
    row.style.display = 'none';
  }
}

// ── Prompt modal ────────────────────────────────────────────────────────────
let _promptResolve = null;
function showPrompt(title, defaultValue = '') {
  return new Promise(resolve => {
    _promptResolve = resolve;
    document.getElementById('prompt-modal-title').textContent = title;
    const input = document.getElementById('prompt-modal-input');
    input.value = defaultValue;
    document.getElementById('prompt-modal-overlay').style.display = '';
    document.getElementById('prompt-modal').style.display = '';
    setTimeout(() => { input.focus(); input.select(); }, 50);
  });
}
function confirmPromptModal() {
  const input = document.getElementById('prompt-modal-input');
  const val = input.style.display === 'none' ? true : input.value;
  const resolve = _promptResolve;
  _resetPromptModal();
  closePromptModal();
  if (resolve) resolve(val);
}
function cancelPromptModal() {
  const resolve = _promptResolve;
  _resetPromptModal();
  closePromptModal();
  if (resolve) resolve(null);
}
function _resetPromptModal() {
  const input = document.getElementById('prompt-modal-input');
  input.style.display = '';
  const btn = document.getElementById('prompt-modal-confirm-btn');
  btn.textContent = 'OK';
  btn.style.color = 'var(--primary)';
  btn.style.borderColor = 'var(--primary)';
}
function closePromptModal() {
  document.getElementById('prompt-modal-overlay').style.display = 'none';
  document.getElementById('prompt-modal').style.display = 'none';
  _promptResolve = null;
}
function showConfirm(message) {
  return new Promise(resolve => {
    _promptResolve = resolve;
    document.getElementById('prompt-modal-title').textContent = message;
    document.getElementById('prompt-modal-input').style.display = 'none';
    document.getElementById('prompt-modal-confirm-btn').textContent = 'Delete';
    document.getElementById('prompt-modal-confirm-btn').style.color = '#e53e3e';
    document.getElementById('prompt-modal-confirm-btn').style.borderColor = '#e53e3e';
    document.getElementById('prompt-modal-overlay').style.display = '';
    document.getElementById('prompt-modal').style.display = '';
  });
}

// ── API helper ─────────────────────────────────────────────────────────────
// api() and addWordViaAi() live in shared.js — the standalone /add page (#668)
// needs both without loading this file.

// ── View switcher ──────────────────────────────────────────────────────────
function _triggerClapAnimation() {
  const emojis = ['👏', '👏', '👏', '⭐', '✨', '🌟'];
  const count = 18;
  for (let i = 0; i < count; i++) {
    setTimeout(() => {
      const el = document.createElement('span');
      el.className = 'clap-particle';
      el.textContent = emojis[Math.floor(Math.random() * emojis.length)];
      const x = 5 + Math.random() * 90;
      const rise = 55 + Math.random() * 35;
      const dur = 1.4 + Math.random() * 0.8;
      const tilt = (Math.random() - 0.5) * 30;
      el.style.cssText = `left:${x}vw;--rise:-${rise}vh;--dur:${dur}s;--tilt:${tilt}deg`;
      document.body.appendChild(el);
      el.addEventListener('animationend', () => el.remove());
    }, i * 80);
  }
}

// Last counts payload seen from the review API — the only thing that knows how
// many Again cards are still pending when the queue runs dry (#844).
let _lastCounts = null;

function _renderDoneHint() {
  const el = document.getElementById('done-soon-hint');
  if (!el) return;
  const n = _lastCounts?.learning_soon || 0;
  el.style.display = n > 0 ? '' : 'none';
  el.textContent = n > 0
    ? `还有 ${n} 张卡在学习步骤里，稍后会回来。`
    : '';
}

function showView(name) {
  _currentView = name;
  if (name === 'done') _renderDoneHint();
  if (name === 'done' && _sessionReviewedCount > 0) _triggerClapAnimation();
  // Leaving the knowledge view (#502, generalized #653): stop the episode-list
  // "processing" poll loop — it has no reason to keep firing once the view
  // isn't visible.
  if (name !== 'knowledge' && typeof _clearPodcastPoll === 'function') _clearPodcastPoll();
  // #929: the source buttons belong to one story generation. Leaving the
  // loading screen ends that run, so they must not survive into the next
  // unrelated setLoading() ("Loading audio…", opening a knowledge item, …).
  if (name !== 'loading') _storyLoadingSources = [];
  ['loading', 'decks', 'review', 'done', 'browse', 'word-detail', 'hanzi-detail', 'stats', 'settings', 'knowledge', 'books', 'archive'].forEach(v => {
    document.getElementById(`view-${v}`).style.display = 'none';
  });
  document.getElementById(`view-${name}`).style.display =
    name === 'browse' ? 'flex' : 'block';
  document.querySelector('main').classList.toggle('browse-open', name === 'browse');
  document.querySelector('main').classList.toggle('review-open', name === 'review');
  const countsRow = document.getElementById('counts-row');
  if (countsRow) countsRow.style.display = name === 'review' ? 'flex' : 'none';
  // The header always answers "which language am I in" (#896) — the tinted rule
  // alone (#824) doesn't, and Knowledge/Books/Stats read identically in every
  // language. Switchable tabs everywhere except review: there the header's right
  // half is the due counts, and swapping language mid-card is meaningless, so
  // that view gets a read-only chip naming the current language instead.
  // Re-render rather than just unhiding — some paths reach these views without
  // going through renderDecks() (error fallbacks), and the tabs must still show.
  _renderHeaderLangTabs(name === 'review');
  _updateBackBtn();
  document.getElementById('header-title').textContent =
    name === 'review'       ? deckName :
    name === 'browse'       ? 'Browse' :
    name === 'word-detail'  ? 'Word Detail' :
    name === 'hanzi-detail' ? 'Hanzi Detail' :
    name === 'stats'        ? 'Stats' :
    name === 'settings'     ? 'Settings' :
    name === 'knowledge'    ? 'Knowledge' :
    name === 'books'        ? 'Books' :
    name === 'archive'      ? 'Archive' : 'biangbiangmian3000';
  if (name === 'decks') quickMode = false;
  // ＋ in every view (#829 review-only, widened in #958). Hidden offline for
  // the same reason as ↺: the whole entry generation is an AI call, so it can
  // only fail there (#612).
  const headerAddBtn = document.getElementById('header-add-btn');
  if (headerAddBtn) headerAddBtn.style.display = _offlineMode ? 'none' : '';
  const headerRegenBtn = document.getElementById('header-regen-btn');
  // Offline mode hides both regenerate affordances — they can only fail (#612).
  if (headerRegenBtn) headerRegenBtn.style.display =
    (name === 'review' && !unfinishedMode && !quickMode && !_offlineMode) ? '' : 'none';
  if (name === 'review') {
    const regenBtn = document.querySelector('.regen-btn');
    if (regenBtn) regenBtn.style.display = (unfinishedMode || quickMode || _offlineMode) ? 'none' : '';
  }
}

// Show the loading view. Pass useProgress=true for story/audio generation to show the progress bar.
function setLoading(msg, useProgress = false) {
  document.getElementById('loading-msg').textContent = msg || 'Loading…';
  const wrap = document.getElementById('loading-progress-wrap');
  const bar  = document.getElementById('loading-progress-bar');
  const sub  = document.getElementById('loading-sub');
  const spinner = document.getElementById('loading-spinner');
  if (useProgress) {
    wrap.style.display = 'block';
    bar.style.width = '0%';
    bar.className = '';
  } else {
    wrap.style.display = 'none';
  }
  if (sub) { sub.textContent = ''; sub.className = ''; }
  const arts = document.getElementById('loading-articles');
  if (arts) { arts.innerHTML = ''; arts.style.display = 'none'; }
  if (spinner) spinner.style.visibility = '';
  _renderLoadingSources();
  showView('loading');
}

// ── Source buttons on the story loading screen (#929) ───────────────────────
// Knowledge mode can generate from several source items at once (#752), and the
// generation takes minutes. Reading the material Daniel is about to be quizzed
// on is exactly what that wait is good for — so each selected item gets a button
// here, opening its summary in a popup and dropping the user right back on this
// screen when it closes.
//
// Set once in confirmStorySetup() (and restored from the resume context when a
// background story is re-opened); every _doStartReview*/regenerate variant goes
// through there, so there is no per-flow plumbing. [{id, title, kind}].
let _storyLoadingSources = [];

const _KNOWLEDGE_KIND_ICON = { podcast: '\u{1F399}\uFE0F', video: '\u{1F4FA}', article: '\u{1F4C4}', newsletter: '\u{1F4F0}', kahneman: '\u{1F4A1}' };

function _renderLoadingSources() {
  const el = document.getElementById('loading-sources');
  if (!el) return;
  el.innerHTML = '';
  if (!_storyLoadingSources.length) { el.style.display = 'none'; return; }
  const label = document.createElement('div');
  label.className = 'loading-sources-label';
  label.textContent = 'Material — tap to read it while you wait:';
  el.appendChild(label);
  for (const src of _storyLoadingSources) {
    const btn = document.createElement('button');
    btn.className = 'loading-source-btn';
    // textContent, never innerHTML: these titles come from podcast feeds,
    // YouTube and arbitrary web pages.
    btn.textContent = `${_KNOWLEDGE_KIND_ICON[src.kind] || '\u{1F4C4}'} ${src.title || '(untitled)'}`;
    btn.title = src.title || '';
    // Kahneman chapters (#980) open the chapter modal that the concept box
    // opens during review — the same shared modal knowledge items use, so
    // closing it lands back on this loading screen either way.
    btn.onclick = src.kind === 'kahneman'
      ? () => openKahnemanExamples(src.id, src.title)
      : () => openKnowledgeSummaryPopup(src.id, src.title);
    el.appendChild(btn);
  }
  el.style.display = 'block';
}

// id -> fetched episode, keyed by language too (a non-zh rendition is a
// different payload). Keeps re-opening a summary instant, and re-opening it is
// the point — Daniel reads one, closes it, reads the next.
let _knowledgeSummaryCache = {};

// Popup showing one knowledge item's summary, reusing the kahneman modal (Esc,
// ✕ and the overlay all already close it, and closing leaves the loading
// screen exactly as it was — the generation never stopped).
async function openKnowledgeSummaryPopup(id, title) {
  const overlay = document.getElementById('kahneman-examples-overlay');
  const modal   = document.getElementById('kahneman-examples-modal');
  const titleEl = document.getElementById('kahneman-examples-title');
  const bodyEl  = document.getElementById('kahneman-examples-body');
  titleEl.textContent = title || '';
  bodyEl.innerHTML = '<div class="kahneman-examples-loading">Loading\u2026</div>';
  overlay.style.display = '';
  modal.style.display = '';

  const lang = activeLang();
  const key = `${id}:${lang}`;
  let ep = _knowledgeSummaryCache[key];
  if (!ep) {
    try {
      ep = await api('GET', `/api/podcast/episodes/${id}?lang=${encodeURIComponent(lang)}`);
      _knowledgeSummaryCache[key] = ep;
    } catch (e) {
      bodyEl.innerHTML = '';
      bodyEl.appendChild(document.createTextNode('Failed to load: ' + (e.message || 'error')));
      return;
    }
  }
  if (modal.style.display === 'none') return;   // closed while loading
  titleEl.textContent = ep.title || title || '';
  const summary = _knowledgeSummaryHtml(ep);
  bodyEl.innerHTML = summary.trim()
    ? summary
    : `<p class="keymap-hint">${_escHtml('No summary yet for this item.')}</p>`;
}

// Update progress bar and status text during a multi-step loading operation.
// percent: 0–100; msg: main heading (optional); sub: detail line (optional)
function setLoadingStep(percent, msg, sub) {
  const bar   = document.getElementById('loading-progress-bar');
  const msgEl = document.getElementById('loading-msg');
  const subEl = document.getElementById('loading-sub');
  if (bar)   { bar.style.width = percent + '%'; bar.className = ''; }
  if (msgEl && msg) msgEl.textContent = msg;
  if (subEl) { subEl.textContent = sub || ''; subEl.className = ''; }
}

// Slowly advance the progress bar from `from` → `to` percent over `durationMs`.
// Returns a cancel function. Does NOT set the bar above `to`.
let _fakeProgressTimer = null;
function _startFakeProgress(from, to, durationMs) {
  _stopFakeProgress();
  const steps = Math.ceil(durationMs / 250);
  const inc   = (to - from) / steps;
  let current = from;
  _fakeProgressTimer = setInterval(() => {
    current = Math.min(current + inc, to);
    const bar = document.getElementById('loading-progress-bar');
    if (bar && parseFloat(bar.style.width) < current) bar.style.width = current + '%';
  }, 250);
}
function _stopFakeProgress() {
  if (_fakeProgressTimer) { clearInterval(_fakeProgressTimer); _fakeProgressTimer = null; }
}

// Poll /api/story-progress/{deckId}/{cat} and update the loading sub-text + progress bar.
// Handles warning phase (retry): resets bar to 5% and restarts fake progress.
let _storyProgressPoll = null;
function _startStoryProgressPoll(deckId, cat) {
  _stopStoryProgressPoll();
  _storyProgressPoll = setInterval(async () => {
    try {
      const p = await fetch(`/api/story-progress/${deckId}/${cat}${_langQP('?')}`).then(r => r.json());
      if (!p || p.phase === 'idle') return;
      const subEl = document.getElementById('loading-sub');
      const bar   = document.getElementById('loading-progress-bar');
      if (p.translate_warn) {
        if (subEl) { subEl.textContent = p.translate_warn; subEl.className = 'warn'; }
        return;
      }
      if (p.phase === 'warning') {
        _stopFakeProgress();
        if (bar) { bar.style.width = '5%'; bar.className = 'warn'; }
        _startFakeProgress(5, 50, 30000);
        if (subEl) { subEl.textContent = p.msg; subEl.className = 'warn'; }
      } else if (p.msg) {
        // Briefing pipeline (issue #407): append the overall word counter and drive the
        // bar with the backend's real percent (never move it backwards).
        let txt = p.msg;
        if (p.words_total) txt += ` · 生词 ${p.words_done ?? 0}/${p.words_total}`;
        if (subEl) { subEl.textContent = txt; subEl.className = ''; }
        if (bar && p.phase !== 'ai_done') bar.className = '';
        if (bar && p.words_total && typeof p.percent === 'number') {
          const cur = parseFloat(bar.style.width) || 0;
          if (p.percent > cur) bar.style.width = p.percent + '%';
        }
      }
      // Headlines currently being summarized (news flow) — plain textContent
      // per line, the titles come from external news sources.
      const artEl = document.getElementById('loading-articles');
      if (artEl) {
        const titles = Array.isArray(p.articles) ? p.articles : [];
        const key = titles.join('');
        if (artEl.dataset.key !== key) {
          artEl.dataset.key = key;
          artEl.innerHTML = '';
          for (const t of titles) {
            const line = document.createElement('div');
            line.textContent = `📰 ${t}`;
            artEl.appendChild(line);
          }
        }
        artEl.style.display = titles.length ? 'block' : 'none';
      }
      // Material buttons (#980): the backend reports the chapters/items this run
      // actually uses, which for kahneman's "none selected → random 5" is the
      // only place they exist. Compared by key so the buttons are not rebuilt
      // (and their click handlers dropped) every 400 ms.
      if (Array.isArray(p.sources) && p.sources.length) {
        const srcKey = p.sources.map(x => `${x.kind}:${x.id}`).join('|');
        const curKey = _storyLoadingSources.map(x => `${x.kind}:${x.id}`).join('|');
        if (srcKey !== curKey) { _storyLoadingSources = p.sources; _renderLoadingSources(); }
      }
      // Generation log (issue #642): cumulative backend lines, appended only
      // (re-rendering the whole list every 400ms would fight the scrollbar).
      // Auto-scrolls to the newest line unless the user scrolled up to read.
      const logEl = document.getElementById('loading-log');
      if (logEl) {
        const lines = Array.isArray(p.log) ? p.log : [];
        const shown = logEl.childElementCount;
        if (lines.length < shown) logEl.innerHTML = '';   // new run reset the log
        const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 20;
        for (const line of lines.slice(logEl.childElementCount)) {
          const div = document.createElement('div');
          div.textContent = line;      // backend text, never HTML
          logEl.appendChild(div);
        }
        logEl.style.display = lines.length ? 'block' : 'none';
        if (atBottom) logEl.scrollTop = logEl.scrollHeight;
      }
    } catch (_) {}
  }, 400);
}
function _stopStoryProgressPoll() {
  if (_storyProgressPoll) { clearInterval(_storyProgressPoll); _storyProgressPoll = null; }
}

// Preload TTS for a session while polling per-sentence progress.
// deckId/cat → used to build the API URL and progress-poll key.
// onProgress(done, total) called whenever progress updates.
async function _preloadWithProgress(deckId, cat, onProgress) {
  let finished = false;
  const preloadDone = fetch(`/api/preload-session/${deckId}/${cat}${_langQP('?')}`, { method: 'POST' })
    .then(() => { finished = true; })
    .catch(() => { finished = true; });

  // Poll progress endpoint until preload completes
  while (!finished) {
    await new Promise(r => setTimeout(r, 350));
    if (finished) break;
    try {
      const p = await fetch(`/api/tts-progress/${deckId}/${cat}${_langQP('?')}`).then(r => r.json());
      if (p.total > 0) onProgress(p.done, p.total);
      if (p.error) {
        const subEl = document.getElementById('loading-sub');
        if (subEl) { subEl.textContent = p.error; subEl.className = 'warn'; }
      }
    } catch (_) {}
  }
  await preloadDone;
}

function _showLoadingSuccess(msg) {
  const bar   = document.getElementById('loading-progress-bar');
  const msgEl = document.getElementById('loading-msg');
  const subEl = document.getElementById('loading-sub');
  const spinner = document.getElementById('loading-spinner');
  if (bar)    { bar.style.width = '100%'; bar.className = 'success'; }
  if (msgEl)  msgEl.textContent = msg || 'Done!';
  if (subEl)  { subEl.textContent = ''; subEl.className = ''; }
  if (spinner) spinner.style.visibility = 'hidden';
}

function _showLoadingError(headline, detail) {
  const bar   = document.getElementById('loading-progress-bar');
  const msgEl = document.getElementById('loading-msg');
  const subEl = document.getElementById('loading-sub');
  const spinner = document.getElementById('loading-spinner');
  if (bar)    { bar.className = 'error'; }
  if (msgEl)  msgEl.textContent = headline || 'Failed';
  if (subEl)  { subEl.textContent = detail || ''; subEl.className = detail ? 'error' : ''; }
  if (spinner) spinner.style.visibility = 'hidden';
}

function _resetLoadingSpinner() {
  const spinner = document.getElementById('loading-spinner');
  if (spinner) spinner.style.visibility = '';
}

function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

function showError(msg) {
  const el = document.getElementById('error-banner');
  el.classList.remove('notice');
  el.textContent = msg;
  el.style.display = 'block';
  setTimeout(() => { el.style.display = 'none'; }, 6000);
}

// Non-alarming amber notice (reuses the error banner). Used e.g. when a story
// fails to generate and we silently fall back to words-only quick mode.
function showNotice(msg) {
  const el = document.getElementById('error-banner');
  el.classList.add('notice');
  el.textContent = msg;
  el.style.display = 'block';
  setTimeout(() => { el.style.display = 'none'; el.classList.remove('notice'); }, 6000);
}

// Persistent reminder that this instance can't reach the network, so an empty
// story or a silent sentence is expected rather than a bug (issue #612).
// Dismissal is per browser session only — a permanently hidden banner would
// leave Daniel wondering why regenerate is missing days later (#621).
// In LOCAL_MODE the banner comes and goes with the Wi-Fi, so it is re-rendered
// on every mode poll rather than built once (#625).
function _renderOfflineBanner() {
  const existing = document.getElementById('offline-banner');
  if (!_offlineMode || sessionStorage.getItem('offlineBannerDismissed')) {
    if (existing) existing.remove();
    return;
  }
  if (existing) return;
  const el = document.createElement('div');
  el.id = 'offline-banner';
  const text = document.createElement('span');
  text.textContent = _localMode
    ? '✈️ No network — stories and audio are read-only until the connection is back'
    : '✈️ Offline mode — stories and audio are read-only from the last sync';
  const close = document.createElement('button');
  close.id = 'offline-banner-close';
  close.type = 'button';
  close.title = 'Hide until next browser session';
  close.setAttribute('aria-label', 'Dismiss offline notice');
  close.textContent = '×';
  close.onclick = () => {
    sessionStorage.setItem('offlineBannerDismissed', '1');
    el.remove();
  };
  el.append(text, close);
  document.body.prepend(el);
}



// ── Background-task indicator (#821) ────────────────────────────────────────
// "Continue in background" on the story loading screen dropped Daniel on a deck
// list that looked completely idle while the AI call, the translations and the
// TTS preload kept running for another minute — and adding a word or processing
// a knowledge item is just as invisible. GET /api/tasks aggregates whatever the
// server is actually doing; this renders it in the header.

let _tasksTimer = null;
let _tasksPanelOpen = false;
let _tasksCount = 0;

// Poll fast while something runs (the detail text changes every few seconds),
// slowly while idle — every open tab runs this poll, so an idle install must
// not hammer the server.
const _TASKS_POLL_BUSY = 3000;
const _TASKS_POLL_IDLE = 15000;

function _startTasksPolling() {
  if (_tasksTimer) return;
  const tick = async () => {
    await _refreshTasks();
    _tasksTimer = setTimeout(tick,
      (_tasksCount > 0 || _tasksPanelOpen) ? _TASKS_POLL_BUSY : _TASKS_POLL_IDLE);
  };
  tick();
}

async function _refreshTasks() {
  // A failed poll must not clear the indicator: a dropped request is not
  // evidence that the work stopped. Keep showing the last known state.
  const r = await api('GET', '/api/tasks').catch(() => null);
  if (!r || !Array.isArray(r.tasks)) return;
  _tasksCount = r.tasks.length;
  _renderTasks(r.tasks);
}

function _renderTasks(tasks) {
  const wrap = document.getElementById('tasks-wrap');
  if (!wrap) return;
  if (tasks.length === 0) {
    wrap.style.display = 'none';
    _tasksPanelOpen = false;
    document.getElementById('tasks-panel').style.display = 'none';
    return;
  }
  wrap.style.display = '';
  document.getElementById('tasks-count').textContent = String(tasks.length);
  document.getElementById('tasks-btn').title =
    tasks.map(t => `${t.icon} ${t.label}`).join('\n');

  const list = document.getElementById('tasks-list');
  list.textContent = '';
  for (const t of tasks) {
    const row = document.createElement('div');
    row.className = 'task-row';

    const head = document.createElement('div');
    head.className = 'task-row-head';
    const label = document.createElement('span');
    label.className = 'task-row-label';
    label.textContent = `${t.icon || '⚙'} ${t.label || ''}`;
    head.appendChild(label);
    const age = _taskAge(t.started_at);
    if (age) {
      const ageEl = document.createElement('span');
      ageEl.className = 'task-row-age';
      ageEl.textContent = age;
      head.appendChild(ageEl);
    }
    // ✕ only where the server says a cancel actually does something (#877):
    // most task kinds have no interruption point, and a button that silently
    // does nothing is worse than none — he would stop trusting the panel.
    if (t.cancellable) {
      const cancel = document.createElement('button');
      cancel.className = 'task-cancel-btn';
      cancel.textContent = '✕';
      cancel.title = 'Cancel this task';
      cancel.onclick = (e) => { e.stopPropagation(); _cancelTask(t.id, cancel, row); };
      head.appendChild(cancel);
    }
    row.appendChild(head);

    if (t.detail) {
      const detail = document.createElement('div');
      detail.className = 'task-row-detail';
      detail.textContent = t.detail;
      row.appendChild(detail);
    }
    if (typeof t.percent === 'number') {
      const bar = document.createElement('div');
      bar.className = 'task-bar';
      const fill = document.createElement('div');
      fill.style.width = Math.max(0, Math.min(100, t.percent)) + '%';
      bar.appendChild(fill);
      row.appendChild(bar);
    }
    list.appendChild(row);
  }
}

async function _cancelTask(taskId, btn, row) {
  btn.disabled = true;
  row.classList.add('task-row-cancelling');
  try {
    await api('POST', '/api/tasks/cancel', { id: taskId });
    // The run clears itself on its next progress check; refresh so the row
    // disappears rather than sitting there greyed out until the next poll.
    await _refreshTasks();
  } catch (e) {
    // Re-enable: the task is still running, and pretending otherwise is exactly
    // what the server-side 400/404 is there to prevent.
    btn.disabled = false;
    row.classList.remove('task-row-cancelling');
    showError('Could not cancel: ' + e.message);
  }
}

// started_at is a server-side epoch in seconds; only the jobs that have no
// progress bar of their own carry it.
function _taskAge(startedAt) {
  if (!startedAt) return '';
  const secs = Math.max(0, Math.round(Date.now() / 1000 - startedAt));
  if (secs < 60) return `${secs}s`;
  return `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

function toggleTasksPanel() {
  const panel = document.getElementById('tasks-panel');
  if (!panel) return;
  _tasksPanelOpen = !_tasksPanelOpen;
  panel.style.display = _tasksPanelOpen ? 'block' : 'none';
  if (_tasksPanelOpen) _refreshTasks();
}

document.addEventListener('click', (e) => {
  if (!_tasksPanelOpen) return;
  const wrap = document.getElementById('tasks-wrap');
  if (wrap && !wrap.contains(e.target)) toggleTasksPanel();
});


// ── Local mode + sync (#625) ────────────────────────────────────────────────
// The laptop instance is a full copy of the app that happens to lose its AI
// and TTS when the network goes away, so `offline` is a live value: poll it and
// re-apply the UI bits that depend on it.

async function _refreshMode() {
  const mode = await api('GET', '/api/mode').catch(() => null);
  if (!mode) return;
  const wasOffline = _offlineMode;
  _offlineMode = !!mode.offline;
  _localMode = !!mode.local;
  const syncBtn = document.getElementById('sync-btn');
  if (syncBtn) syncBtn.style.display = _localMode ? '' : 'none';
  _renderOfflineBanner();
  // The regenerate affordances are hidden while offline; re-run the logic that
  // owns them if the network state actually flipped.
  if (wasOffline !== _offlineMode) showView(_currentView);
}

function _startModePolling() {
  if (!_localMode || _modePollTimer) return;
  _modePollTimer = setInterval(_refreshMode, 60000);
}

function openSyncPopup() {
  document.getElementById('sync-modal-overlay').style.display = 'block';
  document.getElementById('sync-modal').style.display = 'block';
  if (!_syncPollTimer) _pollSyncProgress();   // pick up a run started earlier
}

function closeSyncPopup() {
  document.getElementById('sync-modal-overlay').style.display = 'none';
  document.getElementById('sync-modal').style.display = 'none';
}

async function startSync(mode = 'sync') {
  document.getElementById('sync-run-btn').disabled = true;
  document.getElementById('sync-force-btn').style.display = 'none';
  document.getElementById('sync-status').textContent = 'Starting…';
  try {
    await api('POST', `/api/sync/start?mode=${mode}`);
  } catch (e) {
    // 409 means a run is already going — just attach to it.
    document.getElementById('sync-status').textContent = String(e.message || e);
  }
  _pollSyncProgress();
}

// Escape hatch for a local database the server refuses to merge: no sync token
// (it never came from a pull) or a rotated one (already pushed). Downloading
// over it is the only way forward, so make the data loss explicit (#625).
async function forcePull() {
  const ok = await showConfirm(
    'Throw away this local database and download the server\'s copy?\n\n' +
    'Any reviews done here that were never synced will be lost.');
  if (ok) startSync('pull');
}

// Polls until the run finishes. The database was swapped underneath us, so a
// successful sync ends in a full reload rather than a partial refresh.
async function _pollSyncProgress() {
  clearTimeout(_syncPollTimer);
  const p = await api('GET', '/api/sync/progress').catch(() => null);
  if (!p) { _syncPollTimer = setTimeout(_pollSyncProgress, 2000); return; }

  const log = document.getElementById('sync-log');
  if (p.lines.length) {
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
    log.style.display = 'block';
    log.textContent = p.lines.join('\n');
    if (atBottom) log.scrollTop = log.scrollHeight;
  }
  const status = document.getElementById('sync-status');
  const btn = document.getElementById('sync-run-btn');

  if (p.running) {
    status.textContent = 'Syncing…';
    btn.disabled = true;
    _syncPollTimer = setTimeout(_pollSyncProgress, 1000);
    return;
  }
  _syncPollTimer = null;
  btn.disabled = false;
  if (p.ok === true) {
    status.textContent = '✅ Done — reloading…';
    setTimeout(() => location.reload(), 1200);
  } else if (p.ok === false) {
    status.textContent = `❌ ${p.error || 'Sync failed'}`;
    // A refused merge is the one failure the user can resolve from here.
    const refused = p.lines.some(l => l.includes('sync token'));
    document.getElementById('sync-force-btn').style.display = refused ? '' : 'none';
  } else {
    status.textContent = '';
  }
}


// ── Deck list ───────────────────────────────────────────────────────────────
// keepView: refresh the deck data in place without switching to the home view.
// Background work (adding a word during a review, #695) needs the due counts
// updated, but must never yank the user out of whatever they are doing.
async function loadDecks({ keepView = false } = {}) {
  if (!keepView) setLoading('Loading decks…');
  try {
    const [langs, mode] = await Promise.all([
      api('GET', '/api/langs').catch(() => ['zh']),
      api('GET', '/api/mode').catch(() => ({ offline: false })),
    ]);
    _availableLangs = langs && langs.length ? langs : ['zh'];
    _offlineMode = !!(mode && mode.offline);
    _localMode = !!(mode && mode.local);
    const syncBtn = document.getElementById('sync-btn');
    if (syncBtn) syncBtn.style.display = _localMode ? '' : 'none';
    _renderOfflineBanner();
    _startModePolling();
    // Only scope requests to the active tab once there's more than one language
    // in use — keeps a pure-Chinese install byte-identical to pre-#436 behavior.
    const langParam = _availableLangs.length > 1 ? `&lang=${activeLang()}` : '';
    const [decks, retention] = await Promise.all([
      api('GET', `/api/decks?unfinished_scope=${_unfinishedScope}${langParam}`),
      api('GET', `/api/retention?days=0${langParam}`).catch(() => null),
    ]);
    _cachedDecks = decks;
    _retentionData = retention;
    _deckLangById = {};
    for (const d of flatten(decks)) {
      if (d.id != null) _deckLangById[d.id] = d.lang || 'zh';
    }
    renderDecks(decks);
    if (!keepView) showView('decks');
  } catch (e) {
    // A background refresh failing is not worth an error screen — the user
    // never asked for it, and the banner already reports the word's outcome.
    if (keepView) { console.warn('background deck refresh failed', e); return; }
    showError('Could not load decks: ' + e.message);
    showView('decks');
  }
}

// Tab bar shown above the deck list — only when more than one language is in
// use (issue #436). Selecting a tab re-scopes the whole home page: deck tree,
// All-deck aggregation, unfinished cards, and the stats charts.
// They live in the header (#816) rather than in the page body, because the
// language is application-wide state — dictionary, add-word and story modes all
// follow it — so it belongs next to the app title, above everything it scopes.
const _LANG_TAB_LABELS = { zh: '中文', fr: 'Français', es: 'Español' };

// Each language gets its own accent colour (#824), carried by body[data-lang] so
// the header's bottom rule is tinted in *every* view — the tab bar itself only
// shows on two of them, but "which language am I in" matters during review too.
// Single-language users get no attribute at all, and so the original grey rule.
function _applyLangTheme() {
  const body = document.body;
  if (!body) return;
  if (_availableLangs.length <= 1) delete body.dataset.lang;
  else body.dataset.lang = activeLang();
}

function _renderHeaderLangTabs(readOnly = false) {
  _applyLangTheme();
  const box = document.getElementById('header-lang-tabs');
  if (!box) return;
  if (_availableLangs.length <= 1) { box.style.display = 'none'; box.innerHTML = ''; return; }
  const cur = activeLang();
  const curLabel = _LANG_TAB_LABELS[cur] || cur;
  box.innerHTML = readOnly
    ? `<span class="lang-tab lang-tab-active lang-tab-static">${curLabel}</span>`
    : _availableLangs.map(l => {
        const label = _LANG_TAB_LABELS[l] || l;
        const active = l === cur ? ' lang-tab-active' : '';
        return `<button class="lang-tab${active}" onclick="setActiveLang('${l}')">${label}</button>`;
      }).join('');
  box.style.display = 'flex';
}

function flatten(nodes, depth = 0) {
  return nodes.flatMap(n => [{ ...n, _depth: depth }, ...flatten(n.children || [], depth + 1)]);
}

// Which of the three categories are switched on anywhere (#869). A category
// turned off in every deck's preset must leave no trace in the UI — a "创 0·0·0"
// tile or an empty "Creating" chart tab is noise about something that cannot
// happen. Any deck having it on counts as on; virtual decks (Unfinished) carry
// none of these fields and are skipped.
//
// Before /api/decks has answered we report everything as enabled: showing one
// tile too many for a second beats blanking the UI on missing data.
function _enabledCategories() {
  const all = { reading: true, listening: true, creating: true };
  if (!_cachedDecks) return all;
  const decks = flatten(_cachedDecks).filter(d => d.reading_enabled !== undefined);
  if (!decks.length) return all;
  return {
    reading:   decks.some(d => d.reading_enabled),
    listening: decks.some(d => d.listening_enabled),
    creating:  decks.some(d => d.creating_enabled),
  };
}

// Direct category-leaf children of a deck keyed by category
function getCategoryLeaves(deck) {
  const map = {};
  for (const child of (deck.children || [])) {
    if (child.category && (!child.children || child.children.length === 0)) {
      map[child.category] = child;
    }
  }
  return map;
}

// All category-leaf decks anywhere under this deck (recursive)
function getDeepCategoryLeaves(deck) {
  const result = [];
  for (const child of (deck.children || [])) {
    if (child.category && (!child.children || child.children.length === 0)) {
      result.push(child);
    } else {
      result.push(...getDeepCategoryLeaves(child));
    }
  }
  return result;
}

// ── Retention rate helpers ────────────────────────────────────────────────────

function _rrClass(val) {
  if (val === null) return '';
  if (val >= 0.90) return 'rr-high';
  if (val >= 0.75) return 'rr-mid';
  return 'rr-low';
}

function _formatRR(val) {
  if (val === null) return '—';
  return Math.round(val * 100) + '%';
}

function _mixNewBtn(deckId, override) {
  const icons = { mixed: '⇄', reviews_first: '↓', new_first: '↑' };
  const titles = {
    mixed:        'Override: mixed (click → after reviews)',
    reviews_first:'Override: new after reviews (click → new before reviews)',
    new_first:    'Override: new before reviews (click → no override)',
    null:         'No override — using deck setting (click → mixed)',
  };
  const icon  = icons[override] || '⇄';
  const title = titles[override ?? 'null'] || titles['null'];
  const cls   = override ? 'mix-new-btn mix-on' : 'mix-new-btn';
  return `<button class="${cls}" onclick="event.stopPropagation();toggleMixNew(${deckId})" title="${title}">${icon}</button>`;
}

// Compute RR for a deck (structural or leaf) using cached _retentionData
function _calcDeckRR(deck) {
  if (!_retentionData?.by_deck) return { overall: null, by_category: {} };
  const leaves = deck.category
    ? [deck]
    : getDeepCategoryLeaves(deck);

  let totalC = 0, totalT = 0;
  const byCat = {};

  for (const leaf of leaves) {
    const d = _retentionData.by_deck[leaf.id];
    if (!d) continue;
    totalC += d.correct;
    totalT += d.total;
    const cat = leaf.category;
    if (cat) {
      if (!byCat[cat]) byCat[cat] = { c: 0, t: 0 };
      byCat[cat].c += d.correct;
      byCat[cat].t += d.total;
    }
  }

  const overall = totalT > 0 ? totalC / totalT : null;
  const by_category = {};
  for (const [cat, v] of Object.entries(byCat)) {
    by_category[cat] = v.t > 0 ? v.c / v.t : null;
  }
  return { overall, total: totalT, by_category };
}

// Build tooltip text for a deck's RR
function _rrTooltip(rr) {
  const lines = [`Today's retention: ${_formatRR(rr.overall)} (${rr.total ?? 0} reviews)`];
  const LABELS = { reading: 'R', listening: 'L', creating: 'C' };
  for (const [cat, val] of Object.entries(rr.by_category)) {
    lines.push(`${LABELS[cat] ?? cat}: ${_formatRR(val)}`);
  }
  return lines.join(' · ');
}

// Update the RR badge in the review header
function _updateReviewRRBadge(deckOrId) {
  const badge = document.getElementById('review-rr-badge');
  if (!_retentionData) return;
  let rr;
  if (typeof deckOrId === 'object') {
    rr = _calcDeckRR(deckOrId);
  } else {
    const deck = _findDeckInTree(_cachedDecks, deckOrId);
    if (!deck) { if (badge) badge.style.display = 'none'; _clearCatRRSpans(); return; }
    rr = _calcDeckRR(deck);
  }
  // Overall badge
  if (badge) {
    badge.textContent = 'RR ' + _formatRR(rr.overall);
    badge.className = 'review-rr-badge' + (rr.overall === null ? ' rr-no-data' : '');
    badge.title = rr.overall === null ? 'No reviews yet today' : _rrTooltip(rr);
    badge.style.display = '';
  }
  // Per-category spans
  const MAP = { reading: 'r', listening: 'l', creating: 'c' };
  for (const [cat, key] of Object.entries(MAP)) {
    const el = document.getElementById(`cnt-${key}-rr`);
    if (!el) continue;
    const val = rr.by_category[cat] ?? null;
    el.textContent = _formatRR(val);
    el.className = 'cnt-cat-rr';
  }
}

function _clearCatRRSpans() {
  for (const key of ['r', 'l', 'c']) {
    const el = document.getElementById(`cnt-${key}-rr`);
    if (el) { el.textContent = ''; el.className = 'cnt-cat-rr'; }
  }
}

function _findDeckInTree(nodes, id) {
  for (const n of (nodes || [])) {
    if (n.id === id) return n;
    const found = _findDeckInTree(n.children, id);
    if (found) return found;
  }
  return null;
}

// Aggregate counts for one category from all deep leaves
function aggregateCounts(deck, category) {
  const leaves = getDeepCategoryLeaves(deck).filter(l => l.category === category);
  const agg = { new: 0, learning: 0, review: 0, learning_soon: 0 };
  for (const l of leaves) for (const k of ['new', 'learning', 'review', 'learning_soon']) agg[k] += (l.counts || {})[k] || 0;
  return agg;
}

// Cards you just rated Again sit in the 1m/10m steps and are not due *right
// now*, so the backend keeps them out of `learning` (which several server-side
// checks read as "due this second") and reports them separately as
// `learning_soon`. The 1d/3d steps are deliberately not in it: they are tomorrow.
//
// They used to be summed into the orange learning number (#844: a counter that
// drops to 0 right after pressing Again reads like the card was lost), but that
// number then claims work that isn't available — the deck says 6 and opens
// straight into the "nothing to do, wait" screen (#1032). So they get their own
// ⏳ badge: still on screen, just marked as pending instead of reviewable.
function lrnHtml(c) {
  const soon = (c?.learning_soon || 0);
  return `<span class="n-lrn">${c?.learning || 0}</span>`
       + (soon > 0 ? `<span class="n-soon" title="${soon} in a learning step, not due yet">⏳${soon}</span>` : '');
}

// Same split for the review-screen counters (fixed elements, so the ⏳ badge is
// its own sibling span rather than markup): the orange number is what's
// reviewable now, the ⏳ is what's still waiting in a learning step.
function setLrnCounter(numId, soonId, c) {
  const num = document.getElementById(numId);
  const soonEl = document.getElementById(soonId);
  if (num) num.textContent = c?.learning || 0;
  if (soonEl) {
    const soon = c?.learning_soon || 0;
    soonEl.textContent = soon > 0 ? `⏳${soon}` : '';
    soonEl.title = soon > 0 ? `${soon} in a learning step, not due yet` : '';
  }
}

function countHtml(c) {
  return `<span class="n-new">${c.new}</span> ${lrnHtml(c)} <span class="n-rev">${c.review}</span>`;
}


// Compute RR for a list of leaf deck objects (using cached _retentionData)
function _leavesRR(leaves) {
  if (!_retentionData?.by_deck) return null;
  let c = 0, t = 0;
  for (const l of leaves) {
    const d = _retentionData.by_deck[l.id];
    if (d) { c += d.correct; t += d.total; }
  }
  return t > 0 ? c / t : null;
}

function _catRRSpan(val) {
  const cls = val === null ? 'rr-none' : '';
  const txt = val === null ? '—' : _formatRR(val);
  return `<span class="cat-pill-rr ${cls}">${txt}</span>`;
}

// One category pill: suspend-badge + pill (review) + quick (speed mode) + gear
// (options) + regen (issue #857 — regenerate just this category's story). Shared
// by both branches of buildCategoryButtons below — they used to carry two
// almost-identical copies of this template (one keyed on a leaf id, one on the
// aggregating deck's id), which meant every future addition (like this ↺
// button) had to be pasted in twice.
function _catPillGroup(id, cat, label, safeName, c, allSusp, noStory, rr) {
  const badgeIcon = allSusp ? '▶' : '⏸';
  const badgeClass = allSusp ? 'cat-susp-badge cat-badge-suspended' : 'cat-susp-badge cat-badge-active';
  const pillClass = allSusp ? 'cat-pill cat-pill-dimmed' : 'cat-pill';
  const title = allSusp ? `Unsuspend all ${label} cards` : `Suspend all ${label} cards`;
  // Hidden for no_story categories (nothing to regenerate) and offline (the
  // regeneration is an AI call, so it can only fail — same reasoning as the
  // deck-level ↺ and the review header's ↺, #612).
  const regenBtn = (!noStory && !_offlineMode)
    ? `<button class="cat-pill-regen" onclick="event.stopPropagation();regenerateStoryFromList(${id},'${cat}')" title="Regenerate ${label} story">↺</button>`
    : '';
  return `<span class="cat-pill-group"><button class="${badgeClass}" onclick="event.stopPropagation();toggleCategorySuspension(${id},'${cat}')" title="${title}">${badgeIcon}</button><span class="cat-pill-wrap"><button class="${pillClass}" onclick="event.stopPropagation();startReview(${id},'${cat}','${safeName}',${!!noStory})"><span class="cat-pill-label">${label}</span><span class="cat-pill-counts">${countHtml(c)}</span>${_catRRSpan(rr)}</button><button class="cat-pill-quick" onclick="event.stopPropagation();startReview(${id},'${cat}','${safeName}',${!!noStory},true)" title="Speed mode — words only, no sentences">⚡</button><button class="cat-pill-gear" onclick="event.stopPropagation();openOptions(${id})" title="Options">⚙</button>${regenBtn}</span></span>`;
}

// Build 3 inline pills (L/R/C) for any deck. Uses direct cat leaves if present, else aggregates.
function buildCategoryButtons(deck) {
  const DEFAULT_ORDER = ['listening', 'reading', 'creating'];
  const orderStr = deck.category_order || 'listening,reading,creating';
  const ordered = orderStr.split(',').map(s => s.trim()).filter(s => DEFAULT_ORDER.includes(s));
  // Ensure all 3 categories present (in case of corrupt/partial value),
  // then drop any category the deck's preset disables
  const CATS = [...ordered, ...DEFAULT_ORDER.filter(c => !ordered.includes(c))]
    .filter(c => deck[`${c}_enabled`] !== 0);
  const LABELS = { listening: 'L', reading: 'R', creating: 'C' };
  const catLeaves = getCategoryLeaves(deck);
  const safeName  = deck.name.replace(/'/g, "\\'");
  return CATS.map(cat => {
    const label = LABELS[cat];
    const leaf = catLeaves[cat];
    if (leaf) {
      const c = leaf.counts || { new: 0, learning: 0, review: 0 };
      const allSusp = !!leaf.all_suspended;
      const rr = _leavesRR([leaf]);
      return _catPillGroup(leaf.id, cat, label, safeName, c, allSusp, leaf.no_story, rr);
    }
    const c = aggregateCounts(deck, cat);
    const hasCards = getDeepCategoryLeaves(deck).some(l => l.category === cat);
    if (!hasCards) return `<button class="cat-pill" disabled><span class="cat-pill-label">${label}</span><span class="cat-pill-counts"><span class="n-zero">—</span></span></button>`;
    const leaves = getDeepCategoryLeaves(deck).filter(l => l.category === cat);
    const allSusp = leaves.length > 0 && leaves.every(l => !!l.all_suspended);
    const rr = _leavesRR(leaves);
    return _catPillGroup(deck.id, cat, label, safeName, c, allSusp, deck.no_story, rr);
  }).join('');
}

function renderDecks(decks) {
  const navRow = `
    <div class="nav-row">
      <button class="nav-btn" onclick="openAddWordModal()" title="Add a new word (⌘A)">＋ Add Word</button>
      <a class="nav-btn" href="/dict" title="Dictionary lookup">📖 Dictionary</a>
      <button class="nav-btn" onclick="openRandomWords()" title="10 random words for today">🎲 Random</button>
      <button class="nav-btn" onclick="openBrowse()" title="Shortcut: B">Browse Cards</button>
      <button class="nav-btn" onclick="openStats()">Stats</button>
      <button class="nav-btn" onclick="openArchive()" title="Every generated story and every study session">📜 Archive</button>
      <button class="nav-btn" onclick="openKnowledge()">🧠 Knowledge</button>
      <button class="nav-btn" onclick="openBooks()" title="Read an uploaded book">📚 Books</button>
      <button class="nav-btn" onclick="openSettings()" title="Customize shortcuts">⚙ Settings</button>
      <button class="nav-btn" onclick="openCostModal()">API Costs</button>
      <button class="nav-btn" onclick="openImportModal()" title="Shortcut: Command+I">Import</button>
      <button class="nav-btn" onclick="openQuickAddCard()" title="Shortcut: A">Add Card</button>
      <button class="nav-btn" onclick="createDeck()">New Deck</button>
      <button class="nav-btn" onclick="openTrash()">Trash</button>
    </div>`;

  const virtualDecks = decks.filter(d => d.virtual);
  const allDeck = virtualDecks.find(d => d.name === 'All');
  // Real decks live as children of the "All" virtual deck
  const allChildren = allDeck ? (allDeck.children || []) : decks.filter(d => !d.virtual);
  const regularDecks = allChildren.filter(d => d.name !== 'Default');

  // ── Filtered Decks section ────────────────────────────────────────────────
  let filteredHtml = '';

  for (const vd of virtualDecks) {
    if (vd.id === 'unfinished') {
      const c = vd.counts;
      const total = (c.new || 0) + (c.learning || 0) + (c.review || 0);
      filteredHtml += `
        <div class="filtered-row unfinished-entry" onclick="openUnfinishedModal()">
          <span class="filtered-name">${vd.name}</span>
          <span class="filtered-count">${total}</span>
        </div>`;
    }
  }

  if (allDeck) {
    const safeName = 'All';
    const allBuryMode  = allDeck.bury_mode || 'all';
    const allBuryIcon  = allBuryMode === 'all' ? '⛓' : allBuryMode === 'none' ? '⊘' : '≡';
    const allBuryClass = `bury-btn bury-${allBuryMode}`;
    const allBuryTitle = allBuryMode === 'all'  ? 'Bury siblings: All (click for None)'
                       : allBuryMode === 'none' ? 'Bury siblings: None (click for Custom)'
                       :                          'Bury siblings: Custom (click for All)';
    const allRRData = _retentionData?.all;
    const allRRVal = allRRData?.total > 0 ? allRRData.correct / allRRData.total : null;
    const allRRBadge = allRRVal !== null
      ? `<span class="deck-rr-badge" title="Today's retention: ${_formatRR(allRRVal)} (${allRRData.total} reviews)">${_formatRR(allRRVal)}</span>`
      : '';
    filteredHtml += `
      <div class="tree-row tree-parent">
        <span class="tree-toggle"></span>
        <span class="tree-name-wrap">
          <span class="tree-name" onclick="startReviewMixed(${allDeck.id},'${safeName}')" style="cursor:pointer">All</span>
          ${!allDeck.no_story && !_offlineMode ? `<button class="deck-regen-btn" onclick="event.stopPropagation();regenerateStoryFromList(${allDeck.id})" title="Regenerate story">↺</button>` : ''}
        </span>
        <span class="deck-counts">${_mixNewBtn(allDeck.id, allDeck.new_review_order_override)}<span class="n-new">${(allDeck.counts||{}).new||0}</span>${lrnHtml(allDeck.counts)}<span class="n-rev">${(allDeck.counts||{}).review||0}</span></span>
        ${allRRBadge}
        <button class="${allBuryClass}" onclick="event.stopPropagation();toggleBury(${allDeck.id})" title="${allBuryTitle}">${allBuryIcon}</button>
        <div class="deck-menu-wrap">
          <button class="deck-susp-btn ${allDeck.deck_all_suspended ? 'deck-all-suspended' : ''}" onclick="event.stopPropagation();toggleDeckAllSuspension(${allDeck.id})" title="${allDeck.deck_all_suspended ? 'Unsuspend all cards' : 'Suspend all cards'}">${allDeck.deck_all_suspended ? '▶' : '⏸'}</button>
          <button class="gear-btn" onclick="event.stopPropagation();toggleDeckMenu(event,${allDeck.id},'${safeName}',false)" title="Deck options">⚙</button>
        </div>
        <div class="cat-pills-row">${buildCategoryButtons(allDeck)}</div>
      </div>`;
  }

  let filteredSection = '';
  if (filteredHtml) {
    filteredSection = `<div class="section-label">Filtered Decks</div><div class="tree-card filtered-tree-card">${filteredHtml}</div>`;
  }

  // ── Regular Decks section ─────────────────────────────────────────────────
  const deckSortMode = localStorage.getItem('deckSortMode') || 'name';
  const sortLabel = deckSortMode === 'due' ? 'Sort: Due ↓' : deckSortMode === 'name-desc' ? 'Sort: Z→A' : 'Sort: A→Z';
  const regularHtml = renderDeckRows(regularDecks, 0, deckSortMode);
  let regularSection = '';
  if (regularHtml.trim()) {
    regularSection = `<div class="section-label section-label-row">Decks<button class="deck-sort-btn" onclick="toggleDeckSort()">${sortLabel}</button></div><div class="tree-card">${regularHtml}</div>`;
  }

  document.getElementById('view-decks').innerHTML =
    navRow + filteredSection +
    '<div id="home-calendar" class="hcal-card"></div>' +
    '<div id="home-evolution" class="hcal-card"></div>' + regularSection;
  _renderHeaderLangTabs();
  if (typeof initHomeCalendar === 'function') initHomeCalendar();
  if (typeof initHomeEvolution === 'function') initHomeEvolution();
}

function toggleDeckSort() {
  const cur = localStorage.getItem('deckSortMode') || 'name';
  const next = cur === 'name' ? 'name-desc' : cur === 'name-desc' ? 'due' : 'name';
  localStorage.setItem('deckSortMode', next);
  renderDecks(_cachedDecks);
}

function renderDeckRows(decks, depth, sortMode) {
  const mode = sortMode || 'name';
  const sorted = [...decks].sort((a, b) => {
    if (mode === 'due') {
      const dueA = (a.counts?.new || 0) + (a.counts?.learning || 0) + (a.counts?.review || 0);
      const dueB = (b.counts?.new || 0) + (b.counts?.learning || 0) + (b.counts?.review || 0);
      return dueB - dueA || a.name.localeCompare(b.name);
    }
    if (mode === 'name-desc') return b.name.localeCompare(a.name);
    return a.name.localeCompare(b.name);
  });
  return sorted.map(deck => {
    // Category leaf decks are consumed as pills — not rendered as rows
    if (deck.category && (!deck.children || deck.children.length === 0)) return '';

    const structChildren = (deck.children || [])
      .filter(c => !(c.category && (!c.children || c.children.length === 0)));
    const hasStructChildren = structChildren.length > 0;
    const isCollapsed = collapsed.has(deck.id);
    const indent = depth * 18;

    const toggleIcon = hasStructChildren ? (isCollapsed ? '▶' : '▼') : '';
    const safeName  = deck.name.replace(/'/g, "\\'");
    const c = deck.counts || { new: 0, learning: 0, review: 0 };
    const deckCounts = `<span class="deck-counts">${_mixNewBtn(deck.id, deck.new_review_order_override)}<span class="n-new">${c.new}</span>${lrnHtml(c)}<span class="n-rev">${c.review}</span></span>`;

    // Future-dated daily decks are locked until their date: greyed, not reviewable.
    if (deck.locked) {
      const lockRow = `
        <div class="tree-row tree-parent deck-locked" style="padding-left:${16 + indent}px">
          <span class="tree-toggle"></span>
          <span class="tree-name-wrap">
            <span class="tree-name">${deck.name}</span>
            <span class="deck-lock-badge" title="Locked until ${deck.unlock_date}">🔒 unlocks ${deck.unlock_date}</span>
          </span>
        </div>`;
      const lockedChildRows = hasStructChildren && !isCollapsed
        ? renderDeckRows(structChildren, depth + 1, mode)
        : '';
      return lockRow + lockedChildRows;
    }

    const buryMode   = deck.bury_mode || 'all';
    const buryIcon   = buryMode === 'all' ? '⛓' : buryMode === 'none' ? '⊘' : '≡';
    const buryClass  = `bury-btn bury-${buryMode}`;
    const buryTitle  = buryMode === 'all'    ? 'Bury siblings: All (click for None)'
                     : buryMode === 'none'   ? 'Bury siblings: None (click for Custom)'
                     :                         'Bury siblings: Custom (click for All)';
    const rrData = _calcDeckRR(deck);
    const rrBadge = rrData.overall !== null
      ? `<span class="deck-rr-badge" title="${_rrTooltip(rrData)}">${_formatRR(rrData.overall)}</span>`
      : '';
    const row = `
      <div class="tree-row tree-parent" style="padding-left:${16 + indent}px">
        <span class="tree-toggle" onclick="toggleDeck(${deck.id})">${toggleIcon}</span>
        <span class="tree-name-wrap">
          <span class="tree-name" onclick="startReviewMixed(${deck.id},'${safeName}',${!!deck.no_story})" style="cursor:pointer">${deck.name}</span>
          ${deck.lang === 'fr' ? `<span class="deck-lang-chip" title="French deck">FR</span>` : ''}
          ${!deck.no_story && !_offlineMode ? `<button class="deck-regen-btn" onclick="event.stopPropagation();regenerateStoryFromList(${deck.id})" title="Regenerate story">↺</button>` : ''}
        </span>
        ${deckCounts}
        ${rrBadge}
        <button class="${buryClass}" onclick="event.stopPropagation();toggleBury(${deck.id})" title="${buryTitle}">${buryIcon}</button>
        <div class="deck-menu-wrap">
          <button class="deck-susp-btn ${deck.deck_all_suspended ? 'deck-all-suspended' : ''}" onclick="event.stopPropagation();toggleDeckAllSuspension(${deck.id})" title="${deck.deck_all_suspended ? 'Unsuspend all cards' : 'Suspend all cards'}">${deck.deck_all_suspended ? '▶' : '⏸'}</button>
          <button class="gear-btn" onclick="event.stopPropagation();toggleDeckMenu(event,${deck.id},'${safeName}',${!!deck.filtered})" title="Deck options">⚙</button>
        </div>
        <div class="cat-pills-row">${buildCategoryButtons(deck)}</div>
      </div>`;

    const childRows = hasStructChildren && !isCollapsed
      ? renderDeckRows(structChildren, depth + 1, mode)
      : '';

    return row + childRows;
  }).join('');
}

async function toggleCategorySuspension(deckId, category) {
  try {
    // Scope the toggle to the active tab: 'All' descends into every language
    // tree, so an unscoped pause under 中文 suspends the French cards too (#918).
    await api('POST', `/api/decks/${deckId}/categories/${category}/toggle-suspension${_optLangQ()}`);
    // Scope the refresh to the active tab like loadDecks() does — an unfiltered
    // reload repaints the tree with every language's decks in it (#915).
    const decks = await api('GET', `/api/decks${_optLangQ()}`);
    _cachedDecks = decks;
    renderDecks(decks);
  } catch (e) {
    showError('Could not toggle suspension: ' + e.message);
  }
}

async function toggleDeckAllSuspension(deckId) {
  try {
    await api('POST', `/api/decks/${deckId}/toggle-all-suspension${_optLangQ()}`);
    const decks = await api('GET', `/api/decks${_optLangQ()}`);
    _cachedDecks = decks;
    renderDecks(decks);
  } catch (e) {
    showError('Could not toggle suspension: ' + e.message);
  }
}

function toggleDeck(deckId) {
  if (collapsed.has(deckId)) {
    collapsed.delete(deckId);
  } else {
    collapsed.add(deckId);
  }
  localStorage.setItem('collapsedDecks', JSON.stringify([...collapsed]));
  if (_cachedDecks) {
    const scrollEl = document.querySelector('main');
    const scrollY = scrollEl ? scrollEl.scrollTop : 0;
    renderDecks(_cachedDecks);
    if (scrollEl) scrollEl.scrollTop = scrollY;
  } else {
    loadDecks();
  }
}

async function toggleBury(deckId) {
  try {
    const { bury_mode } = await api('POST', `/api/decks/${deckId}/preset/toggle-bury`);
    // Optimistic update in cached tree
    if (_cachedDecks) {
      const flat = [];
      const walk = nodes => nodes.forEach(n => { flat.push(n); walk(n.children || []); });
      walk(_cachedDecks);
      const deck = flat.find(d => d.id === deckId);
      if (deck) deck.bury_mode = bury_mode;
      const scrollEl = document.querySelector('main');
      const scrollY = scrollEl ? scrollEl.scrollTop : 0;
      renderDecks(_cachedDecks);
      if (scrollEl) scrollEl.scrollTop = scrollY;
    }
  } catch (e) {
    showError('Failed to toggle burying: ' + e.message);
  }
}

async function toggleMixNew(deckId) {
  try {
    const { new_review_order_override } = await api('POST', `/api/decks/${deckId}/preset/toggle-mix`);
    if (_cachedDecks) {
      const flat = [];
      const walk = nodes => nodes.forEach(n => { flat.push(n); walk(n.children || []); });
      walk(_cachedDecks);
      const deck = flat.find(d => d.id === deckId);
      if (deck) deck.new_review_order_override = new_review_order_override;
      const scrollEl = document.querySelector('main');
      const scrollY = scrollEl ? scrollEl.scrollTop : 0;
      renderDecks(_cachedDecks);
      if (scrollEl) scrollEl.scrollTop = scrollY;
    }
  } catch (e) {
    showError('Failed to toggle mix setting: ' + e.message);
  }
}

// ── Deck context menu ────────────────────────────────────────────────────────
function toggleDeckMenu(e, id, safeName, filtered = false) {
  closeDeckMenu();
  const btn = e.currentTarget;
  const menu = document.createElement('div');
  menu.id = 'deck-menu';
  menu.className = 'deck-dropdown';
  if (filtered) {
    menu.innerHTML = `
      <button onclick="closeDeckMenu();openBrowseForDeck(${id})">Browse</button>
      <button onclick="closeDeckMenu();openOptions(${id})">Options</button>
      <button onclick="closeDeckMenu();clearDeckCards(${id},'${safeName}')">Clear all cards</button>
    `;
  } else {
    menu.innerHTML = `
      <button onclick="closeDeckMenu();openBrowseForDeck(${id})">Browse</button>
      <button onclick="closeDeckMenu();renameDeck(${id},'${safeName}')">Rename</button>
      <button onclick="closeDeckMenu();openOptions(${id})">Options</button>
      <button onclick="closeDeckMenu();deleteDeck(${id},'${safeName}')">Delete</button>
    `;
  }
  document.body.appendChild(menu);
  const r = btn.getBoundingClientRect();
  const menuH = menu.offsetHeight;
  const spaceBelow = window.innerHeight - r.bottom;
  const top = spaceBelow >= menuH + 4
    ? r.bottom + window.scrollY + 4
    : r.top  + window.scrollY - menuH - 4;
  menu.style.top  = top + 'px';
  menu.style.left = (r.left + window.scrollX - menu.offsetWidth + btn.offsetWidth) + 'px';
  setTimeout(() => document.addEventListener('click', closeDeckMenu, { once: true }), 0);
}
function closeDeckMenu() {
  document.getElementById('deck-menu')?.remove();
}

async function deleteDeck(id, name) {
  const confirmed = await showConfirm(`Delete deck "${name}" and all its cards? This cannot be undone.`);
  if (!confirmed) return;
  try {
    await api('DELETE', `/api/decks/${id}`);
    loadDecks();
  } catch (e) {
    showError('Delete failed: ' + e.message);
  }
}

async function clearDeckCards(id, name) {
  const confirmed = await showConfirm(`Delete all notes in "${name}"? This cannot be undone.`);
  if (!confirmed) return;
  try {
    await api('DELETE', `/api/decks/${id}/cards`);
    loadDecks();
  } catch (e) {
    showError('Clear failed: ' + e.message);
  }
}

async function renameDeck(id, currentName) {
  const name = await showPrompt('Rename deck', currentName);
  if (!name || name === currentName) return;
  try {
    await api('PUT', `/api/decks/${id}`, { name });
    loadDecks();
  } catch (e) {
    showError('Rename failed: ' + e.message);
  }
}

async function createDeck() {
  const path = await showPrompt('New deck path (use :: to nest, e.g. Daily::03-19)');
  if (!path || !path.trim()) return;
  let lang = await showPrompt('Language: zh (Chinese) or fr (French)', activeLang());
  if (lang === null) return; // user cancelled
  lang = lang.trim().toLowerCase() || 'zh';
  if (lang !== 'zh' && lang !== 'fr') {
    showError(`Unknown language "${lang}" — expected zh or fr`);
    return;
  }
  try {
    await api('POST', `/api/decks?name=${encodeURIComponent(path.trim())}&lang=${encodeURIComponent(lang)}`);
    loadDecks();
  } catch (e) {
    showError('Create deck failed: ' + e.message);
  }
}

async function openQuickAddCard() {
  const defaultDeck = (document.getElementById('import-deck-path')?.value || '').trim();
  const deckPath = await showPrompt('Deck path for new card (use :: to nest)', defaultDeck);
  if (!deckPath || !deckPath.trim()) return;

  const yamlTemplate = [
    'type: word',
    'simplified: ',
    'traditional: ',
    'pinyin: ',
    'hsk: 1',
    'translations:',
    '  en: ',
    '  zh-CN: ',
  ].join('\n');

  openYamlEdit('Add card', yamlTemplate, deckPath.trim(), -1);
}

// ── Browse ───────────────────────────────────────────────────────────────────
let _browseSearchTimer = null;
let _browseMode       = 'notes';   // 'notes' | 'hanzi'
let _browseFilter     = 'all';     // note_type or 'all'; for hanzi mode: 'all'
let _browseCardStatus = 'all';     // one of BROWSE_STATUSES, or 'all'/'starred'/'flagged'
let _browseDeckId     = null;      // deck filter (notes mode only)
let _allHanzi         = [];        // cache

// ── Word status model (#1015) ────────────────────────────────────────────────
// Daniel's own vocabulary is one chest with five compartments, and "which one
// is this word in" must have exactly ONE answer: the chip counts are only
// trustworthy — and only add up to the total — while every word lands in
// precisely one bucket. So this is a priority chain, not a set of overlapping
// predicates, and it is the single place that decides a word's status.
//
// 'mature' = learned well enough that it barely comes back: every card still in
// rotation is in review state with an interval of at least three weeks (the
// Anki convention). It is deliberately not "suspended": a mature word is still
// scheduled, it just sits far out in the future.
const BROWSE_MATURE_DAYS = 21;

function _wordStatus(w) {
  const cards = w.cards || [];
  if (!cards.length) return 'reference';
  if (cards.some(c => c.is_leech)) return 'leech';
  if (cards.some(c => c.deck_name === 'Saved')) return 'saved';
  const live = cards.filter(c => c.state !== 'suspended');
  if (!live.length) return 'suspended';
  if (live.every(c => c.state === 'review' && (c.interval || 0) >= BROWSE_MATURE_DAYS)) return 'mature';
  return 'active';
}

// Order here is the order of the chips. `word` = counted from the word list;
// the two sentence views at the end are a different kind of entity and carry
// no count (they are fetched separately, #692/#854).
const BROWSE_STATUSES = [
  { key: 'all',       icon: '\u{1F5C3}\uFE0F', label: 'All',        word: true,
    title: 'Every entry in the chest' },
  { key: 'active',    icon: '\u{1F525}',        label: 'Active',     word: true,
    title: 'In rotation right now — these are the words the stories are built from' },
  { key: 'mature',    icon: '\u{1F331}',        label: 'Learned',    word: true,
    title: 'Every card in review with an interval of ' + BROWSE_MATURE_DAYS + '+ days — learned, so it barely comes up' },
  { key: 'saved',     icon: '\u2605',           label: 'Star List',  word: true,
    title: 'Parked in the Saved deck, not scheduled yet' },
  { key: 'suspended', icon: '\u23F8\uFE0F',    label: 'Suspended',  word: true,
    title: 'All cards suspended — taken out of the rotation by hand' },
  { key: 'leech',     icon: '\u{1F41B}',        label: 'Leeches',    word: true,
    title: 'Forgotten too often — flagged as a leech and suspended' },
  { key: 'reference', icon: '\u{1F4C4}',        label: 'Reference',  word: true,
    title: 'Entry without any cards — looked up, never scheduled' },
  { key: 'starred',   icon: '\u2B50',           label: 'Sentences',  word: false, sep: true,
    title: 'Sentences you starred while reviewing — good examples for prompt tuning' },
  { key: 'flagged',   icon: '\u2691',           label: 'Sentences',  word: false,
    title: 'Sentences you flagged while reviewing — bad examples for prompt tuning' },
];

function _sortWords(words) {
  const sorted = [...words];
  const locale = { sensitivity: 'base' };
  switch (_browseSort) {
    case 'pinyin-asc':  sorted.sort((a, b) => (a.pinyin || '').localeCompare(b.pinyin || '', 'en', locale)); break;
    case 'pinyin-desc': sorted.sort((a, b) => (b.pinyin || '').localeCompare(a.pinyin || '', 'en', locale)); break;
    case 'hanzi-asc':   sorted.sort((a, b) => (a.word_zh || '').localeCompare(b.word_zh || '', 'zh')); break;
    case 'hanzi-desc':  sorted.sort((a, b) => (b.word_zh || '').localeCompare(a.word_zh || '', 'zh')); break;
    case 'newest':      sorted.sort((a, b) => b.id - a.id); break;
    case 'leeched-desc': case 'leeched-asc': {
      // Sort key = the latest leeched_at across a word's cards (a word has up
      // to three cards, only some of which may be leeches). Words with no
      // leeched_at at all sort last regardless of direction — they don't
      // belong on this axis, so burying them mid-list would look broken (#773).
      const asc = _browseSort === 'leeched-asc';
      const key = w => (w.cards || []).reduce((max, c) => c.leeched_at && c.leeched_at > max ? c.leeched_at : max, '');
      sorted.sort((a, b) => {
        const ka = key(a), kb = key(b);
        if (!ka && !kb) return 0;
        if (!ka) return 1;   // no leeched_at → always last
        if (!kb) return -1;
        if (ka === kb) return 0;
        return (ka < kb) === asc ? -1 : 1;
      });
      break;
    }
  }
  return sorted;
}

function onBrowseSort(val) {
  _browseSort = val;
  // Picking a starred-*/flagged-* option re-sorts the sentence list in place —
  // it must NOT leave that sentence view, unlike every other sort option (#773,
  // generalized to flagged in #854).
  if (val.startsWith('starred-') && _browseCardStatus === 'starred') { renderStarredSentences(); return; }
  if (val.startsWith('flagged-') && _browseCardStatus === 'flagged') { renderFlaggedSentences(); return; }
  _leaveSentenceView();  // the sort options are word fields (pinyin/hanzi), #692
  const q = document.getElementById('browse-search').value.trim();
  if (_browseMode === 'hanzi') renderHanziList(_allHanzi, q);
  else if (q) onBrowseSearch(q); else renderBrowseWords(_filteredBrowseWords());
}

// The Leeched, ⭐ Sentences and ⚑ Sentences views sort by a timestamp the other
// views don't have (leeched_at / starred_at / flagged_at), so their options only
// appear while that view is active, and switching away falls back to the default
// word sort (#773, generalized to flagged in #854). Must run BEFORE the list is
// (re)rendered so _browseSort is already correct.
function _syncSortOptions() {
  const showLeeched = _browseCardStatus === 'leech';
  const showStarred = _browseCardStatus === 'starred';
  const showFlagged = _browseCardStatus === 'flagged';
  document.querySelectorAll('#browse-sort option[value^="leeched-"]').forEach(o => o.hidden = !showLeeched);
  document.querySelectorAll('#browse-sort option[value^="starred-"]').forEach(o => o.hidden = !showStarred);
  document.querySelectorAll('#browse-sort option[value^="flagged-"]').forEach(o => o.hidden = !showFlagged);
  if (showLeeched && !_browseSort.startsWith('leeched-')) _browseSort = 'leeched-desc';
  else if (showStarred && !_browseSort.startsWith('starred-')) _browseSort = 'starred-desc';
  else if (showFlagged && !_browseSort.startsWith('flagged-')) _browseSort = 'flagged-desc';
  else if (!showLeeched && !showStarred && !showFlagged &&
           (_browseSort.startsWith('leeched-') || _browseSort.startsWith('starred-') || _browseSort.startsWith('flagged-'))) {
    _browseSort = DEFAULT_BROWSE_SORT;
  }
  const sel = document.getElementById('browse-sort');
  if (sel) sel.value = _browseSort;
}

function _leafDeckIds(deckId) {
  const deck = _browseDecks.find(d => d.id === deckId);
  if (!deck) return new Set([deckId]);
  const ids = new Set();
  function collect(nodes) {
    for (const n of nodes) {
      if (!n.children?.length) ids.add(n.id);
      else collect(n.children);
    }
  }
  if (deck.children?.length) collect(deck.children);
  else ids.add(deckId);
  return ids;
}

// Type + deck only. The status chips count against THIS set, so their numbers
// answer "within what I'm looking at", not "in the whole database" — a count
// that ignores the active deck would send him to an empty list (#1015).
function _baseBrowseWords() {
  let words = browseWords;
  if (_browseFilter !== 'all') words = words.filter(w => w.note_type === _browseFilter);
  if (_browseDeckId !== null) {
    const leafIds = _leafDeckIds(_browseDeckId);
    words = words.filter(w => w.cards.some(c => leafIds.has(c.deck_id)));
  }
  return words;
}

function _filteredBrowseWords() {
  const words = _baseBrowseWords();
  if (_browseCardStatus === 'all') return words;
  return words.filter(w => _wordStatus(w) === _browseCardStatus);
}

// Chips carry live counts — without them "Leeches" is a button you have to
// press to find out it is empty.
function renderBrowseChips() {
  const box = document.getElementById('browse-chips');
  if (!box) return;
  const base = _baseBrowseWords();
  const counts = {};
  for (const w of base) {
    const st = _wordStatus(w);
    counts[st] = (counts[st] || 0) + 1;
  }
  counts.all = base.length;
  const inHanzi = _browseMode === 'hanzi';
  box.innerHTML = BROWSE_STATUSES.map(st => {
    const on = (!inHanzi && _browseCardStatus === st.key) ? ' bc-on' : '';
    const sep = st.sep ? ' bc-sep' : '';
    const count = st.word
      ? `<span class="bc-count">${counts[st.key] || 0}</span>`
      : '';
    const empty = st.word && !(counts[st.key] || 0) ? ' bc-empty' : '';
    return `<button class="browse-chip bc-${st.key}${on}${sep}${empty}"
      title="${_escHtml(st.title)}" onclick="setBrowseStatusFilter('${st.key}')"
      ><span class="bc-icon">${st.icon}</span><span class="bc-label">${st.label}</span>${count}</button>`;
  }).join('');
  // Hanzi last, behind the same separator as the sentence views: it is the third
  // kind of thing in the chest (characters, not words or sentences) and the one
  // piece of the old sidebar worth keeping (#1023). Chinese-only (#815).
  if (activeLang() === 'zh') {
    box.innerHTML += `<button class="browse-chip bc-hanzi bc-sep${inHanzi ? ' bc-on' : ''}"
      title="Every character in the database, grouped by pinyin"
      onclick="setBrowseHanzi()"><span class="bc-icon">\u{5B57}</span><span class="bc-label">Hanzi</span>` +
      `<span class="bc-count">${_allHanzi.length}</span></button>`;
  }
  const sub = document.getElementById('wortschatz-sub');
  if (sub) {
    const NOUN = { all: 'entries', vocabulary: 'words', sentence: 'sentences', chengyu: 'chengyu' };
    if (inHanzi) { sub.textContent = `${_allHanzi.length} characters`; return; }
    let text = `${base.length} ${NOUN[_browseFilter] || 'entries'}`;
    if (_browseDeckId !== null) {
      const deck = _browseDecks.find(d => d.id === _browseDeckId);
      if (deck) text += ` in ${deck.name}`;
    }
    sub.textContent = text;
  }
}

function setBrowseFilter(mode, filter) {
  _browseMode   = mode;
  _browseFilter = filter;
  _browseDeckId = null;
  _leaveSentenceView();
  _syncSortOptions();
  _syncBrowseSelects();
  document.getElementById('browse-search').value = '';
  _browseSelected.clear();
  _updateBrowseActionBar();
  renderBrowseChips();
  if (mode === 'hanzi') renderHanziList(_allHanzi);
  else renderBrowseWords(_filteredBrowseWords());
}

// The Hanzi chip (#1023) is the one sidebar entry worth keeping, so it sits in
// the chip row like everything else — but it lists characters, not words, so
// entering it leaves every word-level filter behind.
function setBrowseHanzi() { setBrowseFilter('hanzi', 'all'); }

// Type and deck are two independent narrowing axes now that both are dropdowns
// (#1023): setting one must not silently reset the other, which is what the
// sidebar's setBrowseFilter() did (there, picking a type meant leaving the deck).
function onBrowseTypeSelect(val) {
  const deckId = _browseDeckId;
  setBrowseFilter('notes', val);
  if (deckId !== null) setBrowseDeckFilter(deckId);
}

function setBrowseStatusFilter(status) {
  // Chips are word-level; clicking one from the hanzi list means "back to words".
  _browseMode = 'notes';
  _browseCardStatus = status;
  _syncSortOptions();
  _syncBrowseSelects();
  document.getElementById('browse-search').value = '';
  _browseSelected.clear();
  _updateBrowseActionBar();
  renderBrowseChips();
  // 'starred'/'flagged' list sentences, not words, so they can't go through the
  // word filter chain in _filteredBrowseWords() — they're their own rendering
  // branch (#692, generalized to flagged in #854).
  if (status === 'starred') { renderStarredSentences(); return; }
  if (status === 'flagged') { renderFlaggedSentences(); return; }
  renderBrowseWords(_filteredBrowseWords());
}

// Any word-level filter action leaves the current sentence view (starred or
// flagged): it lists a different kind of thing, so silently keeping its tab
// highlighted would be a lie (#692, generalized from _leaveStarredView in #854).
function _leaveSentenceView() {
  if (_browseCardStatus !== 'starred' && _browseCardStatus !== 'flagged') return;
  _browseCardStatus = 'all';
  _starredSentencesCache = null;  // stale on next entry — re-fetch (#773)
  _flaggedSentencesCache = null;
  _syncSortOptions();
  renderBrowseChips();
}

// The two dropdowns and the mode must always say the same thing as the state —
// they are set from code (openBrowseForDeck, a chip click) as often as by hand.
function _syncBrowseSelects() {
  const type = document.getElementById('browse-type');
  const deck = document.getElementById('browse-deck');
  const hanzi = _browseMode === 'hanzi';
  if (type) { type.value = _browseFilter; type.disabled = hanzi; }
  if (deck) { deck.value = _browseDeckId === null ? '' : String(_browseDeckId); deck.disabled = hanzi; }
}

function onBrowseDeckSelect(val) {
  setBrowseDeckFilter(val === '' ? null : Number(val));
}

function setBrowseDeckFilter(deckId) {
  _leaveSentenceView();
  _browseMode   = 'notes';
  _browseDeckId = deckId;
  _syncBrowseSelects();
  document.getElementById('browse-search').value = '';
  _browseSelected.clear();
  _updateBrowseActionBar();
  renderBrowseChips();
  renderBrowseWords(_filteredBrowseWords());
}

async function openBrowseForDeck(deckId) {
  await openBrowse();
  setBrowseDeckFilter(deckId);
}

async function openBrowse() {
  navPush('browse');
  setLoading('Loading…');
  try {
    const [words, hanzi, deckTree] = await Promise.all([
      // Browse is language-scoped like every other view (#815): the word
      // list, the deck picker and the search all take the active tab's lang,
      // so French words can never show up under the Chinese tab.
      api('GET', `/api/browse-words${_langQP('?')}`),
      api('GET', '/api/hanzi'),
      api('GET', `/api/decks${_langQP('?')}`),
    ]);
    browseWords = words;
    _allHanzi = hanzi;
    _browseDecks = _flattenDecks(deckTree);
    // Top-level user decks: children of the "All" virtual root
    const _allRoot = deckTree.find(d => d.virtual && d.id !== 'unfinished');
    _browseDeckTree = _allRoot ? (_allRoot.children || []) : deckTree.filter(d => !d.virtual);
    _browseSelected.clear();
    _browseCardStatus = 'all';
    _starredSentencesCache = null;  // fresh browse open — re-fetch, don't reuse a stale list (#773)
    _flaggedSentencesCache = null;
    _syncSortOptions();
    showView('browse');
    document.getElementById('browse-search').value = '';
    _renderBrowseDeckSelect();
    _updateBrowseActionBar();
    setBrowseFilter('notes', 'all');
  } catch (e) {
    showError('Browse failed: ' + e.message);
    showView('decks');
  }
}

function _flattenDecks(tree) {
  const result = [];
  function walk(nodes) {
    for (const n of nodes) {
      if (!n.virtual) result.push(n);
      if (n.children?.length) walk(n.children);
    }
  }
  walk(tree);
  return result;
}

// Deck picker (#1023). The tree became a flat option list: an indented select
// says the same thing in one line, and "which deck" is a narrowing filter, not
// a place to browse around in. Parents stay selectable — picking one includes
// its whole subtree, exactly as the tree did.
function _browseDeckOptions(nodes, depth, out) {
  for (const d of [...nodes].sort((a, b) => a.name.localeCompare(b.name))) {
    if (d.category || d.virtual) continue;
    out.push(`<option value="${d.id}">${'\u00a0'.repeat(depth * 3)}${_escHtml(d.name)}</option>`);
    if (d.children?.length) _browseDeckOptions(d.children, depth + 1, out);
  }
  return out;
}

function _renderBrowseDeckSelect() {
  const sel = document.getElementById('browse-deck');
  if (!sel) return;
  const opts = _browseDeckOptions(_browseDeckTree, 0, []);
  sel.innerHTML = '<option value="">All decks</option>' + opts.join('');
}

function onBrowseSearch(val) {
  clearTimeout(_browseSearchTimer);
  _leaveSentenceView();  // searching is word-level (#692)
  const q = val.trim();
  if (_browseMode === 'hanzi') { renderHanziList(_allHanzi, q); return; }
  if (!q) { renderBrowseWords(_filteredBrowseWords()); return; }
  _browseSearchTimer = setTimeout(async () => {
    try {
      const result = await api('GET', `/api/search-words?q=${encodeURIComponent(q)}${_langQP('&')}`);
      const base = _filteredBrowseWords();
      const primarySet   = new Set(result.primary);
      const secondarySet = new Set(result.secondary);
      const primary   = base.filter(w => primarySet.has(w.id));
      const secondary = base.filter(w => secondarySet.has(w.id));
      renderBrowseSearchResults(primary, secondary, q);
    } catch (e) { showError('Search failed: ' + e.message); }
  }, 250);
}

function _wordRow(w) {
  const def = (w.definition || '').slice(0, 60) + ((w.definition || '').length > 60 ? '…' : '');
  const sel = _browseSelected.has(w.id) ? ' bw-row-selected' : '';
  // The same status the chips count by, as a coloured stripe down the row: in
  // the unfiltered list that is the only thing telling a leech apart from a
  // word he has long since learned (#1015).
  const status = _wordStatus(w);
  let rightHtml;
  if (_browseCardStatus === 'saved') {
    // Words parked via ★ List (#677) already carry a full entry — offering
    // "Generate" there would just burn an API call to re-produce it.
    rightHtml =
      (w.definition ? '' :
        `<button class="bw-saved-btn" onclick="event.stopPropagation();savedGenerate(${w.id},this)" title="Generate content with AI">✨ Generate</button>`) +
      `<button class="bw-saved-btn bw-saved-promote" onclick="event.stopPropagation();savedPromote(${w.id},this)" title="Add to today's Daily deck">→ Add to Daily</button>`;
  } else if (_browseCardStatus === 'leech' && w.cards.some(c => c.is_leech)) {
    rightHtml =
      `<button class="bw-unleech-btn" onclick="event.stopPropagation();browseUnleechWord(${w.id},this)"` +
      ` title="Clear the leech flag and unsuspend">✓ Unleech</button>`;
  } else if (w.cards.length === 0) {
    rightHtml = `<button class="bw-add-btn" onclick="openAddToDeckModal(event,${w.id})" title="Add to deck">＋ Add</button>`;
  } else {
    const CAT_LETTER = { listening: 'L', reading: 'R', creating: 'C' };
    rightHtml = ['listening', 'reading', 'creating'].map(cat => {
      const c = w.cards.find(c => c.category === cat);
      const letter = CAT_LETTER[cat];
      if (!c) return `<button class="rcat-btn bw-rcat-missing" title="${cat}: —" disabled>${letter}</button>`;
      const isSusp = c.state === 'suspended';
      const cls = `rcat-btn ${isSusp ? 'rcat-susp' : 'rcat-active'}`;
      const tip = `${cat}: ${c.state} — click to ${isSusp ? 'activate' : 'suspend'}`;
      return `<button class="${cls}" title="${tip}" onclick="toggleBrowseDotSuspend(event,${c.id},${w.id})">${letter}</button>`;
    }).join('');
  }
  // Per-row delete (#815) — always present, whatever the status filter shows,
  // so getting rid of one bad entry doesn't require the select-then-bulk-bar detour.
  const delBtn = `<button class="bw-del-btn" title="Delete this word and all its cards"
          onclick="browseDeleteWord(event,${w.id})">🗑</button>`;
  return `
    <div class="bw-row bw-st-${status}${sel}" data-word-id="${w.id}" onclick="onBrowseRowClick(event,${w.id})">
      <div class="bw-left">
        <span class="bw-hanzi">${w.word_zh}</span>
        <span class="bw-pinyin">${w.pinyin || ''}</span>
      </div>
      <div class="bw-mid">
        <span class="bw-def">${def}</span>
      </div>
      <div class="bw-right">${rightHtml}${delBtn}</div>
    </div>`;
}

// Clear the leech flag on every leeched card of a word (Leeched list, single row).
async function browseUnleechWord(wordId, btn) {
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = '…';
  try {
    await api('POST', '/api/cards/bulk-unleech', { word_ids: [wordId] });
    await _browseReload();
    showQuickAddBanner('✓ Leech cleared', false);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = orig;
    showError('Unleech failed: ' + e.message);
  }
}

// Generate AI content for a saved word (stays in the Saved list, now filled in).
async function savedGenerate(wordId, btn) {
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = '…';
  try {
    await api('POST', `/api/word/${wordId}/ai-enrich`);
    await _browseReload();
    showQuickAddBanner('✨ Content generated', false);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = orig;
    showError('Generate failed: ' + e.message);
  }
}

// Promote a saved word into today's Daily deck (leaves the Saved list, #728).
async function savedPromote(wordId, btn) {
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = '…';
  try {
    const r = await api('POST', `/api/saved/${wordId}/promote`);
    await _browseReload();
    showQuickAddBanner(`→ Added to ${r.deck_path}`, false);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = orig;
    showError('Add to Daily failed: ' + e.message);
  }
}

function onBrowseRowClick(e, wordId) {
  if (e.metaKey || e.ctrlKey || _browseSelected.size > 0) {
    if (_browseSelected.has(wordId)) {
      _browseSelected.delete(wordId);
    } else {
      _browseSelected.add(wordId);
    }
    document.querySelectorAll(`.bw-row[data-word-id="${wordId}"]`).forEach(el => {
      el.classList.toggle('bw-row-selected', _browseSelected.has(wordId));
    });
    _updateBrowseActionBar();
  } else {
    openWordDetail(wordId);
  }
}

function _updateBrowseActionBar() {
  const bar = document.getElementById('browse-action-bar');
  const n = _browseSelected.size;
  if (!n) { bar.style.display = 'none'; return; }
  bar.style.display = 'flex';
  document.getElementById('ba-count').textContent = `${n} word${n > 1 ? 's' : ''} selected`;
  // Populate move deck dropdown
  const sel = document.getElementById('ba-move-deck');
  const current = sel.value;
  sel.innerHTML = _browseDecks
    .filter(d => !d.virtual)
    .map(d => `<option value="${d.id}">${d.name}</option>`)
    .join('');
  if (current) sel.value = current;
}

function clearBrowseSelection() {
  _browseSelected.clear();
  document.querySelectorAll('.bw-row-selected').forEach(el => el.classList.remove('bw-row-selected'));
  _updateBrowseActionBar();
}

function toggleBrowseMovePanel() {
  const panel = document.getElementById('ba-move-panel');
  panel.style.display = panel.style.display === 'none' ? '' : 'none';
}

async function browseActionBury() {
  const word_ids = [..._browseSelected];
  try {
    await api('POST', '/api/cards/bulk-bury', { word_ids });
    await _browseReload();
  } catch (e) { showError('Bury failed: ' + e.message); }
}

async function browseActionSuspend() {
  const word_ids = [..._browseSelected];
  try {
    await api('POST', '/api/cards/bulk-suspend', { word_ids });
    await _browseReload();
  } catch (e) { showError('Suspend failed: ' + e.message); }
}

async function browseActionUnleech() {
  const word_ids = [..._browseSelected];
  try {
    const r = await api('POST', '/api/cards/bulk-unleech', { word_ids });
    await _browseReload();
    showQuickAddBanner(`✓ Leech cleared on ${r.count} card${r.count === 1 ? '' : 's'}`, false);
  } catch (e) { showError('Unleech failed: ' + e.message); }
}

async function browseActionDelete() {
  const n = _browseSelected.size;
  const ok = await showConfirm(`Delete ${n} note${n > 1 ? 's' : ''}? This cannot be undone.`);
  if (!ok) return;
  const word_ids = [..._browseSelected];
  try {
    await api('POST', '/api/cards/bulk-delete', { word_ids });
    await _browseReload();
  } catch (e) { showError('Delete failed: ' + e.message); }
}

async function browseActionMove() {
  const deck_id = parseInt(document.getElementById('ba-move-deck').value);
  if (!deck_id) return;
  const word_ids = [..._browseSelected];
  try {
    await api('POST', '/api/cards/bulk-move', { word_ids, deck_id });
    document.getElementById('ba-move-panel').style.display = 'none';
    await _browseReload();
  } catch (e) { showError('Move failed: ' + e.message); }
}

async function _browseReload() {
  const q = document.getElementById('browse-search').value.trim();
  browseWords = await api('GET', `/api/browse-words${_langQP('?')}`);
  _browseSelected.clear();
  _updateBrowseActionBar();
  renderBrowseChips();   // unleeching / promoting a word moves it between buckets
  if (q) onBrowseSearch(q);
  else renderBrowseWords(_filteredBrowseWords());
}

// ── Add to deck modal ─────────────────────────────────────────────────────────
let _addToDeckEntryId = null;

function openAddToDeckModal(e, entryId) {
  e.stopPropagation();
  _addToDeckEntryId = entryId;
  const select = document.getElementById('add-to-deck-select');
  // Only decks that already own the three category leaf-decks can take a card:
  // database.add_entry_to_deck() writes into '<parent> · Listening/Reading/Creating'
  // and fails outright when they are missing. Tree roots ('All', 'Français') and
  // the 'Saved' staging deck have none, so offering them guaranteed an error —
  // and under a language tab the root was the *first* option in the list.
  const parentDecks = _browseDecks.filter(
    d => !d.category && !d.virtual && (d.children || []).some(c => c.category));
  if (!parentDecks.length) { showError('No deck with Listening/Reading/Creating sub-decks available'); return; }
  select.innerHTML = parentDecks.map(d => `<option value="${d.id}">${d.name}</option>`).join('');
  document.getElementById('add-to-deck-modal-overlay').style.display = '';
  document.getElementById('add-to-deck-modal').style.display = '';
}

function closeAddToDeckModal() {
  document.getElementById('add-to-deck-modal-overlay').style.display = 'none';
  document.getElementById('add-to-deck-modal').style.display = 'none';
  _addToDeckEntryId = null;
}

async function confirmAddToDeck() {
  const deckId = parseInt(document.getElementById('add-to-deck-select').value);
  if (!deckId || !_addToDeckEntryId) return;
  try {
    await api('POST', `/api/entries/${_addToDeckEntryId}/add-to-deck`, { deck_id: deckId });
    closeAddToDeckModal();
    await _browseReload();
  } catch (e) { showError('Failed to add to deck: ' + e.message); }
}

function renderBrowseWords(words) {
  const list = document.getElementById('browse-list');
  if (!words.length) {
    list.innerHTML = '<div class="browse-empty">No words found</div>';
    return;
  }
  list.innerHTML = `<div class="bw-list">${_sortWords(words).map(_wordRow).join('')}</div>`;
}

// Hard-deletes the entry itself, not just its cards — the entry is what a
// Browse row *is*. Irreversible (no trash), hence the confirm; the row is
// removed from the local list rather than re-fetching the whole page.
async function browseDeleteWord(e, wordId) {
  e.stopPropagation();
  const word = browseWords.find(w => w.id === wordId);
  const ok = await showConfirm(
    `Delete "${word ? word.word_zh : wordId}" and all its cards? This cannot be undone.`);
  if (!ok) return;
  try {
    await api('DELETE', `/api/word/${wordId}`);
    browseWords = browseWords.filter(w => w.id !== wordId);
    _browseSelected.delete(wordId);
    _updateBrowseActionBar();
    const q = document.getElementById('browse-search').value.trim();
    if (q) onBrowseSearch(q); else renderBrowseWords(_filteredBrowseWords());
  } catch (err) { showError('Delete failed: ' + err.message); }
}

// ── Starred / Flagged sentences views (#692, #854) ───────────────────────────
// Two independent judgments only possible in the instant you read a sentence
// during review: ★ a good one worth keeping as a positive prompt-tuning example,
// ⚑ a bad one (grammar mistake, awkward phrasing) worth keeping as a negative
// one. Both list sentences, not words, so they share one rendering branch that
// none of the word filters above can handle — this config drives that branch.
const _SENTENCE_VIEWS = {
  starred: {
    field: 'starred', tsField: 'starred_at', apiPath: '/api/starred-sentences',
    toggleUrl: id => `/api/story-sentence/${id}/star`,
    icon: '★', emptyIcon: '☆', emptyShortcut: 'Shift+F', emptyLabel: 'starred',
    undoTitle: 'Remove the star', undoVerb: 'Unstar',
  },
  flagged: {
    field: 'flagged', tsField: 'flagged_at', apiPath: '/api/flagged-sentences',
    toggleUrl: id => `/api/story-sentence/${id}/flag`,
    icon: '⚑', emptyIcon: '⚐', emptyShortcut: 'Shift+G', emptyLabel: 'flagged',
    undoTitle: 'Remove the flag', undoVerb: 'Unflag',
  },
};

let _starredSentencesCache = null;  // avoids a refetch when only the sort changes (#773)
let _flaggedSentencesCache = null;  // same idea, for the flagged view (#854)

function _sentenceCache(kind) {
  return kind === 'starred' ? _starredSentencesCache : _flaggedSentencesCache;
}
function _setSentenceCache(kind, val) {
  if (kind === 'starred') _starredSentencesCache = val; else _flaggedSentencesCache = val;
}

// {kind}-asc = oldest first; anything else (including the default) = newest
// first. Sentences without a timestamp (shouldn't happen, but defensive) sort
// last regardless of direction, same rule as the leeched sort.
function _sortSentenceView(kind, sentences) {
  const cfg = _SENTENCE_VIEWS[kind];
  const asc = _browseSort === `${kind}-asc`;
  const sorted = [...sentences];
  sorted.sort((a, b) => {
    const ka = a[cfg.tsField] || '', kb = b[cfg.tsField] || '';
    if (!ka && !kb) return 0;
    if (!ka) return 1;
    if (!kb) return -1;
    if (ka === kb) return 0;
    return (ka < kb) === asc ? -1 : 1;
  });
  return sorted;
}

// Single paint path for both the fetched and the cached list, so the empty
// state can't go missing on one of them (removing the last sentence goes
// through the cached branch).
function _paintSentenceView(kind, sentences) {
  const cfg = _SENTENCE_VIEWS[kind];
  const list = document.getElementById('browse-list');
  if (!sentences.length) {
    list.innerHTML = `<div class="browse-empty">No ${cfg.emptyLabel} sentences yet — press ${cfg.emptyShortcut} ` +
                     `or tap ${cfg.emptyIcon} while reviewing to mark one.</div>`;
    return;
  }
  list.innerHTML = `<div class="bw-list">${_sortSentenceView(kind, sentences).map(s => _sentenceRow(kind, s)).join('')}</div>`;
}

async function _renderSentenceView(kind) {
  const cfg = _SENTENCE_VIEWS[kind];
  const list = document.getElementById('browse-list');
  const cached = _sentenceCache(kind);
  if (cached) { _paintSentenceView(kind, cached); return; }
  list.innerHTML = '<div class="browse-empty">Loading…</div>';
  let sentences;
  try {
    const r = await api('GET', `${cfg.apiPath}${_langQP('?')}`);
    sentences = r.sentences;
  } catch (e) {
    list.innerHTML = `<div class="browse-empty">Could not load ${cfg.emptyLabel} sentences: ${_escHtml(e.message)}</div>`;
    return;
  }
  if (_browseCardStatus !== kind) return;  // user switched tabs while loading
  _setSentenceCache(kind, sentences);
  _paintSentenceView(kind, sentences);
}

function renderStarredSentences() { return _renderSentenceView('starred'); }
function renderFlaggedSentences() { return _renderSentenceView('flagged'); }

function _sentenceRow(kind, s) {
  const cfg = _SENTENCE_VIEWS[kind];
  const trans = s.sentence_de || s.sentence_fr || s.sentence_en || '';
  const words = (s.words || []).map(w => _escHtml(w.word_zh)).join('、');
  const source = s.source_url
    ? `<a class="ss-source" href="${_escHtml(s.source_url)}" target="_blank" rel="noopener"
          onclick="event.stopPropagation()">${_escHtml(s.source_title || s.source_name || 'source')}</a>`
    : (s.source_title ? `<span class="ss-source">${_escHtml(s.source_title)}</span>` : '');
  const meta = [
    `<span class="ss-mode">${_escHtml(s.mode || 'story')}</span>`,
    s.story_date ? `<span>${_escHtml(s.story_date)}</span>` : '',
    s.deck_name ? `<span>${_escHtml(s.deck_name)}</span>` : '',
    words ? `<span class="ss-words">${words}</span>` : '',
    source,
  ].filter(Boolean).join('<span class="ss-dot">·</span>');

  // The prompt link is the point of the whole feature (#697) — a starred or
  // flagged sentence is only actionable next to the prompt that produced it.
  const promptBtn = s.has_prompt
    ? `<button class="ss-prompt-btn" title="Show the prompt that generated this sentence"
               onclick="showStoryPrompt(${s.story_id})">📝 Prompt</button>`
    : `<button class="ss-prompt-btn" disabled
               title="No prompt stored for this story (legacy story, or an offline snapshot — it strips prompt_text)">📝 Prompt</button>`;

  return `<div class="bw-row ss-row">
    <div class="ss-main">
      <div class="ss-zh">${_escHtml(s.sentence_zh)}</div>
      ${trans ? `<div class="ss-trans">${_escHtml(trans)}</div>` : ''}
      <div class="ss-meta">${meta}</div>
    </div>
    <div class="ss-actions">
      ${promptBtn}
      <button class="ss-unstar" title="${cfg.undoTitle}"
              onclick="_removeFromSentenceView('${kind}', ${s.id}, this)">${cfg.icon}</button>
    </div>
  </div>`;
}

async function _removeFromSentenceView(kind, sentenceId, btn) {
  const cfg = _SENTENCE_VIEWS[kind];
  btn.disabled = true;
  try {
    await api('POST', cfg.toggleUrl(sentenceId), { [cfg.field]: false });
    const cached = _sentenceCache(kind);
    if (cached) _setSentenceCache(kind, cached.filter(s => s.id !== sentenceId));
    btn.closest('.ss-row')?.remove();
    if (!document.querySelector('.ss-row')) _renderSentenceView(kind);  // back to empty state
  } catch (e) {
    btn.disabled = false;
    showError(`${cfg.undoVerb} failed: ` + e.message);
  }
}

function unstarSentence(sentenceId, btn) { return _removeFromSentenceView('starred', sentenceId, btn); }
function unflagSentence(sentenceId, btn) { return _removeFromSentenceView('flagged', sentenceId, btn); }

function renderBrowseSearchResults(primary, secondary, q) {
  const list = document.getElementById('browse-list');
  if (!primary.length && !secondary.length) {
    list.innerHTML = '<div class="browse-empty">No results for "' + q + '"</div>';
    return;
  }
  let html = '';
  if (primary.length) {
    html += `<div class="browse-section-label">Words (${primary.length})</div>
             <div class="bw-list">${_sortWords(primary).map(_wordRow).join('')}</div>`;
  }
  if (secondary.length) {
    html += `<div class="browse-section-label">Found in examples / notes (${secondary.length})</div>
             <div class="bw-list">${_sortWords(secondary).map(_wordRow).join('')}</div>`;
  }
  list.innerHTML = html;
}

function renderHanziList(hanzi, q = '') {
  const list = document.getElementById('browse-list');
  let items = hanzi;
  if (q) {
    const lq = q.toLowerCase();
    items = hanzi.filter(h =>
      h.char.includes(q) || (h.pinyin || '').toLowerCase().includes(lq) ||
      (h.etymology || '').toLowerCase().includes(lq)
    );
  }
  if (!items.length) {
    list.innerHTML = '<div class="browse-empty">No hanzi found</div>';
    return;
  }
  // Group alphabetically by pinyin first letter
  const groups = {};
  items.forEach(h => {
    const key = (h.pinyin || '?')[0].toUpperCase();
    (groups[key] = groups[key] || []).push(h);
  });
  const sortedKeys = Object.keys(groups).sort();
  let html = '';
  for (const key of sortedKeys) {
    html += `<div class="browse-section-label">${key}</div>
             <div class="bw-list">${groups[key].map(_hanziRow).join('')}</div>`;
  }
  list.innerHTML = html;
}

function _hanziRow(h) {
  const hsk = h.hsk_level ? `<span class="bw-hsk">HSK${h.hsk_level}</span>` : '';
  const etym = (h.etymology || '').slice(0, 60) + ((h.etymology || '').length > 60 ? '…' : '');
  return `<div class="bw-row" onclick="openHanziDetail(${h.id})">
    <div class="bw-left">
      <span class="bw-hanzi">${h.char}</span>
      <span class="bw-pinyin">${h.pinyin || ''}</span>
    </div>
    <div class="bw-mid"><span class="bw-def">${etym}</span></div>
    <div class="bw-right">${hsk}</div>
  </div>`;
}

// ── Word Detail ───────────────────────────────────────────────────────────────
async function openWordByZh(zh) {
  let word = browseWords.find(w => w.word_zh === zh);
  if (word) { openWordDetail(word.id); return; }
  try {
    const all = await api('GET', `/api/browse-words${_langQP('?')}`);
    browseWords = all;
    const found = all.find(w => w.word_zh === zh);
    if (found) openWordDetail(found.id);
    else showError(`"${zh}" not found`);
  } catch (e) { showError(e.message); }
}

async function openWordDetail(wordId) {
  navPush(`word:${wordId}`);
  // Capture which view we're coming from so we can go back to it
  const views = ['review', 'browse', 'hanzi-detail', 'word-detail', 'stats', 'done', 'decks'];
  _prevView = views.find(v => document.getElementById(`view-${v}`)?.style.display !== 'none') || null;
  _currentWordId = wordId;
  setLoading('Loading word…');
  try {
    const word = await api('GET', `/api/word/${wordId}`);
    word.cards = await api('GET', `/api/words/${wordId}/cards`);
    renderWordDetail(word);
    showView('word-detail');
    const backBtn = document.getElementById('wd-back-review-btn');
    if (backBtn) backBtn.style.display = _prevView === 'review' ? 'block' : 'none';
  } catch (e) {
    showError('Failed to load word: ' + e.message);
    showView('browse');
  }
}

function renderWordDetail(word) {
  document.getElementById('wd-edit-btn').onclick = () => openWordEditModal(word);
  const regenAllBtn = document.getElementById('wd-regen-all-btn');
  if (regenAllBtn) regenAllBtn.onclick = () => word.id && regenAllFields(word.id);
  document.getElementById('wd-hanzi').textContent = word.word_zh || '';
  document.getElementById('wd-pinyin').textContent = word.pinyin || '';
  document.getElementById('wd-def').textContent = word.definition ? `🇬🇧 ${word.definition}` : '';
  const posEl = document.getElementById('wd-pos');
  posEl.textContent = word.pos || '—';
  posEl.style.display = 'inline-block';
  const regEl = document.getElementById('wd-register');
  const regLabels = {
    spoken: '口语', written: '书面语', both: '通用',
    spoken_colloquial: '口语俚语', spoken_neutral: '中性口语',
    neutral: '通用', formal_written: '正式书面语', literary: '文学语体'
  };
  if (word.register) {
    regEl.textContent = regLabels[word.register] || word.register;
    regEl.style.display = 'inline-block';
  } else {
    regEl.style.display = 'none';
  }
  const defZhEl = document.getElementById('wd-def-zh');
  defZhEl.textContent = word.definition_zh || '';
  defZhEl.style.display = word.definition_zh ? 'block' : 'none';
  const defDeEl = document.getElementById('wd-def-de');
  defDeEl.textContent = word.definition_de ? `🇩🇪 ${word.definition_de}` : '';
  defDeEl.style.display = word.definition_de ? 'block' : 'none';
  const defFrEl = document.getElementById('wd-def-fr');
  defFrEl.textContent = word.definition_fr ? `🇫🇷 ${word.definition_fr}` : '';
  defFrEl.style.display = word.definition_fr ? 'block' : 'none';

  const defRegenEl = document.getElementById('wd-def-regen');
  if (defRegenEl) {
    defRegenEl.innerHTML = word.id
      ? `<button class="field-regen-btn" onclick="event.stopPropagation();regenFields(${word.id},['definition','definition_zh','definition_de','definition_fr','pos'],'wd-def-block')" title="Regenerate definitions & part of speech">↺</button>`
      : '';
  }

  // Synonyms / antonyms section — collapsible, clickable
  const relEl = document.getElementById('wd-relations-section');
  const synonyms = (word.relations || []).filter(r => r.relation_type === 'synonym');
  const antonyms = (word.relations || []).filter(r => r.relation_type === 'antonym');
  if (synonyms.length || antonyms.length) {
    const _relItem = r =>
      `<span class="wd-rel-item wd-rel-link" title="${r.related_de || ''}"
        onclick="openWordByZh(${_ea(JSON.stringify(r.related_zh))})">${r.related_zh}` +
      (r.related_pinyin ? ` <span class="wd-rel-pin">${r.related_pinyin}</span>` : '') +
      `</span>`;
    let inner = '';
    if (synonyms.length) {
      inner += `<div class="wd-rel-group"><span class="wd-rel-label">近义词</span>`;
      inner += synonyms.map(_relItem).join('');
      inner += `</div>`;
    }
    if (antonyms.length) {
      inner += `<div class="wd-rel-group"><span class="wd-rel-label">反义词</span>`;
      inner += antonyms.map(_relItem).join('');
      inner += `</div>`;
    }
    relEl.innerHTML =
      `<div class="section-label section-toggle" onclick="toggleSection('wd-relations-body')">` +
        `<span id="wd-relations-body-arrow">▶</span> Relations</div>` +
      `<div id="wd-relations-body" style="display:none">${inner}</div>`;
  } else {
    relEl.innerHTML = '';
  }

  // Shared sections (notes, conjugation, word analysis, examples)
  renderConjugationSection(document.getElementById('wd-conjugation-section'), word);
  renderInflectionSection(document.getElementById('wd-inflection-section'), word);
  renderNotesSection(document.getElementById('wd-notes-section'), word.notes, word.id);
  renderWordAnalysis(document.getElementById('wd-word-analysis-section'), word, word.id);
  renderEtymologySection(document.getElementById('wd-etymology-section'), word, word.id);
  renderVocabDetail(document.getElementById('wd-examples-section'), word.examples, word.id);

  // Cards section
  renderWordDetailCards(word.cards || [], word.id);

  // Schedule tile (interval graph / calendar)
  _wdLoadCardTile(word.cards || []);
}

function renderWordDetailCards(cards, wordId) {
  const el = document.getElementById('wd-cards-section');
  if (!cards.length) { el.innerHTML = ''; return; }
  const CAT_FULL = { listening: 'Listening', reading: 'Reading', creating: 'Creating' };
  const rows = cards.map(c => {
    const isSuspended = c.state === 'suspended';
    const intv  = c.interval > 0 ? `${c.interval}d` : '—';
    const ease  = c.ease ? `${Math.round(c.ease * 100)}%` : '—';
    const due   = c.due ? c.due.slice(0, 10) : '—';
    const isBuried = c.buried_until && c.buried_until >= new Date().toISOString().slice(0, 10);
    return `
      <div class="wd-card-block" id="wd-card-${c.id}">
        <div class="wd-card-head">
          <span class="wd-cat-label">${CAT_FULL[c.category] || c.category}</span>
          <span class="badge badge-${c.state}">${c.state}</span>
          ${c.is_leech ? '<span class="badge badge-leech" title="Suspended as a leech">leech</span>' : ''}
          ${isBuried ? '<span class="badge badge-buried">buried</span>' : ''}
          <div class="wd-card-menu-wrap">
            <button class="wd-menu-btn" onclick="toggleCardMenu(${c.id}, event)">⋯</button>
            <div class="wd-card-menu" id="wd-menu-${c.id}" style="display:none">
              <button class="wd-menu-item" onclick="cardAction(${c.id}, 'bury', ${wordId})">Bury until tomorrow</button>
              <button class="wd-menu-item ${isSuspended ? 'wd-menu-item-active' : ''}"
                      onclick="cardAction(${c.id}, 'suspend', ${wordId})">
                ${isSuspended ? 'Unsuspend' : 'Suspend'}
              </button>
              <button class="wd-menu-item" onclick="openMoveCardPanel(${c.id}, event)">Move to deck…</button>
              <button class="wd-menu-item wd-menu-item-danger"
                      onclick="cardAction(${c.id}, 'reset', ${wordId})">Reset to new</button>
            </div>
            <div class="wd-move-panel" id="wd-move-${c.id}" style="display:none" onclick="event.stopPropagation()">
              <input id="wd-move-inp-${c.id}" class="wd-deck-picker-input" autocomplete="off" placeholder="Deck…"
                onfocus="wdPickerOpen(this)" oninput="wdPickerFilter(this)" onkeydown="wdPickerKey(event, this)">
              <button onclick="applyMoveCard(${c.id}, ${wordId})">Apply</button>
            </div>
          </div>
        </div>
        <div class="wd-card-stats">
          <span>Deck <b>${c.deck_path || c.deck_name || '—'}</b></span>
          <span>Interval <b>${intv}</b></span>
          <span>Due <b>${due}</b></span>
          <span>Ease <b>${ease}</b></span>
          <span>Lapses <b>${c.lapses}</b></span>
        </div>
      </div>`;
  }).join('');
  const CAT_LETTER = { listening: 'L', reading: 'R', creating: 'C' };
  const circles = ['listening', 'reading', 'creating'].map(cat => {
    const c = cards.find(c => c.category === cat);
    const letter = CAT_LETTER[cat];
    if (!c) return `<button class="rcat-btn bw-rcat-missing" disabled title="${cat}: —">${letter}</button>`;
    const isSusp = c.state === 'suspended';
    const cls = `rcat-btn ${isSusp ? 'rcat-susp' : 'rcat-active'}`;
    const tip = `${cat}: ${c.state} — click to ${isSusp ? 'activate' : 'suspend'}`;
    return `<button class="${cls}" title="${tip}" onclick="toggleBrowseDotSuspend(event,${c.id},${wordId})">${letter}</button>`;
  }).join('');
  el.innerHTML = `<div class="wd-section-head wd-cards-head">
    <span>Cards <span class="wd-cat-circles">${circles}</span></span>
    <button class="wd-move-all-btn" onclick="openMoveAllCardsPanel(${wordId})">Move all…</button>
  </div>
  <div class="wd-move-all-panel" id="wd-move-all-${wordId}" style="display:none" onclick="event.stopPropagation()">
    <input id="wd-move-all-inp-${wordId}" class="wd-deck-picker-input" autocomplete="off" placeholder="Deck…"
      onfocus="wdPickerOpen(this)" oninput="wdPickerFilter(this)" onkeydown="wdPickerKey(event, this)">
    <button onclick="applyMoveAllCards(${wordId})">Apply</button>
    <button onclick="document.getElementById('wd-move-all-${wordId}').style.display='none';wdPickerClose()">✕</button>
  </div>
  <div class="wd-cards-list">${rows}</div>`;
}

function toggleCardMenu(cardId, e) {
  e.stopPropagation();
  const menu = document.getElementById(`wd-menu-${cardId}`);
  const isOpen = menu.style.display !== 'none';
  closeAllCardMenus();
  if (!isOpen) menu.style.display = 'block';
}

function closeAllCardMenus() {
  document.querySelectorAll('.wd-card-menu').forEach(m => m.style.display = 'none');
  document.querySelectorAll('.wd-move-panel').forEach(p => p.style.display = 'none');
}

document.addEventListener('click', closeAllCardMenus);

async function cardAction(cardId, action, wordId) {
  closeAllCardMenus();
  try {
    await api('POST', `/api/cards/${cardId}/${action}`);
    const word = await api('GET', `/api/word/${wordId}`);
    renderWordDetailCards(word.cards || [], wordId);
  } catch (e) {
    showError(`Action failed: ${e.message}`);
  }
}

function openMoveAllCardsPanel(wordId) {
  const panel = document.getElementById(`wd-move-all-${wordId}`);
  if (!panel) return;
  const isOpen = panel.style.display !== 'none';
  if (isOpen) { panel.style.display = 'none'; wdPickerClose(); return; }
  panel.style.display = 'flex';
  const inp = document.getElementById(`wd-move-all-inp-${wordId}`);
  inp.value = '';
  inp.focus();
}

async function applyMoveAllCards(wordId) {
  const panel = document.getElementById(`wd-move-all-${wordId}`);
  const inp = document.getElementById(`wd-move-all-inp-${wordId}`);
  const path = inp.value.trim();
  if (!path) return;
  panel.style.display = 'none';
  wdPickerClose();
  try {
    const deck_id = await _wdResolveDeck(path);
    await api('POST', '/api/cards/bulk-move', { word_ids: [wordId], deck_id });
    const word = await api('GET', `/api/word/${wordId}`);
    word.cards = await api('GET', `/api/words/${wordId}/cards`);
    renderWordDetailCards(word.cards || [], wordId);
  } catch (e) {
    showError(`Move failed: ${e.message}`);
  }
}

async function toggleBrowseDotSuspend(e, cardId, wordId) {
  e.stopPropagation();
  const btn = e.currentTarget;
  const isSuspended = btn.classList.contains('rcat-susp');
  const newState = isSuspended ? 'new' : 'suspended';
  try {
    await api('POST', `/api/cards/${cardId}/suspend`);
    // Update in-memory browseWords
    const word = browseWords.find(w => w.id === wordId);
    if (word) {
      const card = word.cards.find(c => c.id === cardId);
      if (card) card.state = newState;
    }
    btn.className = `rcat-btn ${newState === 'suspended' ? 'rcat-susp' : 'rcat-active'}`;
    btn.title = btn.title.replace(/— .+$/, `— ${newState === 'suspended' ? 'click to activate' : 'click to suspend'}`);
  } catch (err) {
    showError('Suspend failed: ' + err.message);
  }
}

function openMoveCardPanel(cardId, e) {
  e.stopPropagation();
  closeAllCardMenus();
  const panel = document.getElementById(`wd-move-${cardId}`);
  document.querySelectorAll('.wd-move-panel').forEach(p => p.style.display = 'none');
  wdPickerClose();
  panel.style.display = 'flex';
  const inp = document.getElementById(`wd-move-inp-${cardId}`);
  inp.value = '';
  inp.focus();
}

async function applyMoveCard(cardId, wordId) {
  const panel = document.getElementById(`wd-move-${cardId}`);
  const inp = document.getElementById(`wd-move-inp-${cardId}`);
  const path = inp.value.trim();
  if (!path) return;
  panel.style.display = 'none';
  wdPickerClose();
  try {
    const deck_id = await _wdResolveDeck(path);
    await api('POST', `/api/cards/${cardId}/move`, { deck_id });
    const word = await api('GET', `/api/word/${wordId}`);
    word.cards = await api('GET', `/api/words/${wordId}/cards`);
    renderWordDetailCards(word.cards || [], wordId);
  } catch (e) {
    showError(`Move failed: ${e.message}`);
  }
}

// ── Word edit (from word-detail view) ────────────────────────────────────────
function openWordEditModal(word) {
  _editFromWord = true;
  _openEditModal(word);
}

// ── Hanzi Regenerate Modal ───────────────────────────────────────────────────
let _regenCharId     = null;
let _regenFromReview = false;

function openHanziRegenModal(charId, char, pinyin, fromReview = false) {
  _regenCharId     = charId;
  _regenFromReview = fromReview;
  document.getElementById('hanzi-regen-char').textContent = char;
  document.getElementById('hanzi-regen-pin').textContent  = pinyin || '';
  document.getElementById('hanzi-regen-modal-overlay').style.display = '';
  document.getElementById('hanzi-regen-modal').style.display         = '';
}

function closeHanziRegenModal() {
  document.getElementById('hanzi-regen-modal-overlay').style.display = 'none';
  document.getElementById('hanzi-regen-modal').style.display         = 'none';
}

async function confirmHanziRegen() {
  closeHanziRegenModal();
  try {
    const updated = await api('POST', `/api/hanzi/${_regenCharId}/regenerate`);
    if (_regenFromReview) {
      // Patch in-memory wordDetails and re-render the card back without navigating away
      if (wordDetails?.characters) {
        wordDetails.characters = wordDetails.characters.map(c =>
          c.char_id === _regenCharId
            ? { ...c, etymology: updated.etymology, other_meanings: updated.other_meanings }
            : c
        );
      }
      _callRenderWordAnalysis();
    } else {
      if (_currentWordId) await openWordDetail(_currentWordId);
    }
  } catch (e) {
    showError('Regeneration failed: ' + e.message);
  }
}

// ── Hanzi Detail ─────────────────────────────────────────────────────────────
async function openHanziDetail(charId) {
  navPush(`hanzi:${charId}`);
  _currentHanziId = charId;
  setLoading('Loading hanzi…');
  try {
    const hanzi = await api('GET', `/api/hanzi/${charId}`);
    renderHanziDetail(hanzi);
    showView('hanzi-detail');
  } catch (e) {
    showError('Failed to load hanzi: ' + e.message);
    showView('browse');
  }
}

function renderHanziDetail(h) {
  document.getElementById('hd-char').textContent   = h.char || '';
  document.getElementById('hd-pinyin').textContent = h.pinyin || '';
  const tradRow = document.getElementById('hd-trad-row');
  if (h.traditional) {
    document.getElementById('hd-trad').textContent = h.traditional;
    tradRow.style.display = '';
  } else {
    tradRow.style.display = 'none';
  }
  document.getElementById('hd-edit-btn').onclick = () => openHanziEditModal(h);

  let bodyHtml = '';

  if (h.etymology) {
    bodyHtml += `<div class="wd-section-head">Etymology</div>
      <div class="wd-section-body"><div class="wd-etym">${h.etymology}</div></div>`;
  }

  const compounds = Array.isArray(h.compounds) ? h.compounds : [];
  if (compounds.length) {
    bodyHtml += `<div class="wd-section-head">Compounds</div>
      <div class="wd-section-body"><div class="hd-compounds">` +
      compounds.map(c => {
        const zh = c.compound_zh || c.simplified || String(c);
        const tip = c.meaning ? ` title="${c.meaning}"` : '';
        return `<span class="hd-compound"${tip}>${zh}</span>`;
      }).join('') +
      `</div></div>`;
  }

  if (h.words?.length) {
    bodyHtml += `<div class="wd-section-head">Words containing ${h.char}</div>
      <div class="wd-section-body bw-list">` +
      h.words.map(w => `<div class="bw-row" onclick="openWordDetail(${w.id})">
        <div class="bw-left"><span class="bw-hanzi">${w.word_zh}</span><span class="bw-pinyin">${w.pinyin||''}</span></div>
        <div class="bw-mid"><span class="bw-def">${(w.definition||'').slice(0,60)}</span></div>
      </div>`).join('') +
      `</div>`;
  }

  document.getElementById('hd-body').innerHTML = bodyHtml || '<div class="browse-empty">No data</div>';
}

// ── Hanzi edit modal ──────────────────────────────────────────────────────────
let _editHanziId = null;

function openHanziEditModal(h) {
  _editHanziId = h.id;
  document.getElementById('hedit-pinyin').value    = h.pinyin    || '';
  document.getElementById('hedit-trad').value      = h.traditional || '';
  document.getElementById('hedit-hsk').value       = h.hsk_level != null ? h.hsk_level : '';
  document.getElementById('hedit-etym').value      = h.etymology  || '';
  document.getElementById('hedit-compounds').value = Array.isArray(h.compounds)
    ? JSON.stringify(h.compounds, null, 2)
    : (h.compounds || '');
  document.getElementById('hanzi-edit-modal-overlay').style.display = '';
  document.getElementById('hanzi-edit-modal').style.display         = '';
}
function closeHanziEditModal() {
  document.getElementById('hanzi-edit-modal-overlay').style.display = 'none';
  document.getElementById('hanzi-edit-modal').style.display         = 'none';
}
async function saveHanziEdit() {
  const body = {
    pinyin:        document.getElementById('hedit-pinyin').value.trim(),
    traditional:   document.getElementById('hedit-trad').value.trim(),
    hsk_level:     document.getElementById('hedit-hsk').value ? parseInt(document.getElementById('hedit-hsk').value) : null,
    etymology:     document.getElementById('hedit-etym').value.trim(),
    compounds:     document.getElementById('hedit-compounds').value.trim(),
  };
  try {
    const updated = await api('PUT', `/api/hanzi/${_editHanziId}`, body);
    closeHanziEditModal();
    renderHanziDetail(updated);
  } catch (e) {
    showError('Save failed: ' + e.message);
  }
}

// ── applyFilters kept for legacy (no longer used by browse) ──────────────────
function applyFilters() {}

// ── Stats ────────────────────────────────────────────────────────────────────
async function openStats() {
  navPush('stats');
  setLoading('Loading stats…');
  try {
    const data = await api('GET', '/api/stats');
    showView('stats');
    renderStats(data);
  } catch (e) {
    showError('Stats failed: ' + e.message);
    showView('decks');
  }
}

// ── Settings (customize review shortcuts) ────────────────────────────────────
let _capturingAction = null;
let _settingsMsg = '';
let _dayCutoffHour = 5;
function openSettings() {
  navPush('settings');
  _capturingAction = null; _settingsMsg = '';
  showView('settings');
  renderSettings();
  _loadDayCutoffHour();
  _loadAgainRegenEnabled();
}

// ── Again → new sentence switch (issue #714) ────────────────────────────────
// Server-side setting, not localStorage: the regeneration runs on the server, so
// a browser-local preference would have the phone doing the opposite.
let _againRegenEnabled = true;

async function _loadAgainRegenEnabled() {
  try {
    const res = await api('GET', '/api/again-regen-enabled');
    _againRegenEnabled = res?.enabled !== false;
    renderSettings();
  } catch (e) { /* keep default (on) */ }
}

async function setAgainRegenEnabled(enabled) {
  const prev = _againRegenEnabled;
  _againRegenEnabled = enabled;
  try {
    const res = await api('PUT', '/api/again-regen-enabled', { enabled });
    _againRegenEnabled = res?.enabled !== false && enabled;
  } catch (e) {
    _againRegenEnabled = prev;
    showError('Could not save: ' + e.message);
  }
  renderSettings();
}

async function _loadDayCutoffHour() {
  try {
    const res = await api('GET', '/api/day-cutoff-hour');
    if (res && Number.isInteger(res.hour)) {
      _dayCutoffHour = res.hour;
      renderSettings();
    }
  } catch (e) { /* keep default */ }
}

async function saveDayCutoffHour() {
  const input = document.getElementById('day-cutoff-hour-input');
  const msg = document.getElementById('day-cutoff-msg');
  const hour = parseInt(input.value, 10);
  if (!Number.isInteger(hour) || hour < 0 || hour > 23) {
    if (msg) msg.textContent = 'Enter 0–23';
    return;
  }
  try {
    const res = await api('PUT', '/api/day-cutoff-hour', { hour });
    _dayCutoffHour = res.hour;
    if (msg) msg.textContent = 'Saved ✓';
  } catch (e) {
    if (msg) msg.textContent = 'Failed: ' + e.message;
  }
}
function _keymapRowHtml(a) {
  const capturing = _capturingAction === a.id;
  const keyTxt = capturing ? 'Press a key…' : _keyLabel(_keymap[a.id]);
  const isDefault = _keymap[a.id] === KEYMAP_DEFAULTS[a.id];
  const isUnbound = _keymap[a.id] == null;
  return `<div class="keymap-row">
      <span class="keymap-label">${a.label}</span>
      <button class="keymap-key${capturing ? ' capturing' : ''}${isUnbound ? ' unbound' : ''}" onclick="startKeyCapture('${a.id}')">${keyTxt}</button>
      <button class="keymap-clear" onclick="clearKeymapAction('${a.id}')" ${isUnbound ? 'disabled' : ''} title="Remove shortcut">✕</button>
      <button class="keymap-reset" onclick="resetKeymapAction('${a.id}')" ${isDefault ? 'disabled' : ''} title="Reset to default">↺</button>
    </div>`;
}
function renderSettings() {
  const groups = KEYMAP_SCOPE_GROUPS.map(g => {
    const actions = KEYMAP_ACTIONS.filter(a => a.scope === g.scope);
    if (!actions.length) return '';
    return `<div class="keymap-group">
      <h3 class="keymap-subheading">${g.name}</h3>
      ${actions.map(_keymapRowHtml).join('')}
    </div>`;
  }).join('');
  const msg = _settingsMsg ? `<div class="keymap-msg">${_settingsMsg}</div>` : '';
  const nfZh = _newsflowLang === 'zh';
  document.getElementById('view-settings-content').innerHTML = `
    <div class="keymap-panel">
      <h2 class="keymap-heading">Review shortcuts</h2>
      <p class="keymap-hint">Click a key, then press the new key — Shift is allowed (e.g. Shift+F), Ctrl/Cmd/Alt combos are not. Press Backspace or ✕ to remove a shortcut. Rating keys 1–4 and Esc are fixed.</p>
      ${msg}
      ${groups}
      <button class="keymap-reset-all" onclick="resetKeymapAll()">Reset all to defaults</button>
    </div>
    <div class="keymap-panel">
      <h2 class="keymap-heading">Again → new sentence</h2>
      <p class="keymap-hint">Rating a card <b>Again</b> regenerates its sentence in the background, so it looks different when the card comes back. <b>Off</b> = the card keeps its original sentence (and costs no AI call) — better when the sentence was fine and only the recall failed. The <b>New sentence</b> button (${_keyLabel(_key('new-sentence'))}) always regenerates, switch or not.</p>
      <div class="keymap-row">
        <span class="keymap-label">Regenerate after Again</span>
        <label class="switch-wrap">
          <input type="checkbox" id="again-regen-switch" ${_againRegenEnabled ? 'checked' : ''}
                 onchange="setAgainRegenEnabled(this.checked)" style="width:18px;height:18px;cursor:pointer">
          <span>${_againRegenEnabled ? 'On' : 'Off'}</span>
        </label>
      </div>
    </div>
    <div class="keymap-panel">
      <h2 class="keymap-heading">Kontextsummary</h2>
      <p class="keymap-hint">Language of the context and source titles on Kontextsummary cards. Publisher names stay as-is. Toggle during review with <b>g</b>.</p>
      <div class="keymap-row">
        <span class="keymap-label">Context &amp; titles in Chinese</span>
        <label class="switch-wrap">
          <input type="checkbox" id="newsflow-lang-switch" ${nfZh ? 'checked' : ''}
                 onchange="setNewsflowLangFromSwitch(this.checked)" style="width:18px;height:18px;cursor:pointer">
          <span id="newsflow-lang-value">${nfZh ? '中文' : 'Original (DE)'}</span>
        </label>
      </div>
    </div>
    <div class="keymap-panel">
      <h2 class="keymap-heading">Day boundary</h2>
      <p class="keymap-hint">Hour a new day starts. Cards due the next day stay out of the stack until this time, so late-night reviews still count as the previous day. Default 5.</p>
      <div class="keymap-row">
        <span class="keymap-label">New day starts at</span>
        <input class="opt-input" id="day-cutoff-hour-input" type="number" min="0" max="23"
               value="${_dayCutoffHour}" style="width:64px">
        <span class="keymap-label">:00</span>
        <button class="keymap-reset-all" onclick="saveDayCutoffHour()">Save</button>
        <span id="day-cutoff-msg" class="keymap-label"></span>
      </div>
    </div>
    <div class="keymap-panel">
      <h2 class="keymap-heading">Morning pre-generation</h2>
      <p class="keymap-hint">Story type generated automatically each morning, per category. Independent of the style you pick when regenerating during the day. <b>Off</b> = repeat whatever you last generated manually.</p>
      <div id="pregen-config-body">Loading…</div>
    </div>
    <div class="keymap-panel">
      <h2 class="keymap-heading">Server logs</h2>
      <p class="keymap-hint">View the last lines of the server log (helpful for debugging story generation, TTS, etc). Shortcut: Option+L (Alt+L).</p>
      <button class="keymap-reset-all" onclick="openLogsViewer()">Open logs</button>
    </div>`;
  _loadPregenSettings();
}

// ── Morning pre-generation config (issue #473) ──────────────────────────────
const PREGEN_MODE_OPTIONS = [
  ['', 'Off (repeat last manual)'],
  ['story', 'Story'],
  ['kahneman', 'Kahneman'],
];
let _pregenDecks = [];
let _pregenEntries = [];
let _pregenDeckId = null;
let _pregenEnabled = true;

async function _loadPregenSettings() {
  try {
    const [cfg, decks] = await Promise.all([
      api('GET', '/api/pregen-config'),
      api('GET', '/api/decks'),
    ]);
    _pregenEntries = cfg.entries || [];
    _pregenEnabled = cfg.enabled !== false;
    _pregenDecks = [];
    const walk = (nodes, depth) => (nodes || []).forEach(d => {
      _pregenDecks.push({ id: d.id, name: '  '.repeat(depth) + d.name });
      walk(d.children, depth + 1);
    });
    walk(decks, 0);
    if (_pregenDeckId == null || !_pregenDecks.some(d => d.id === _pregenDeckId)) {
      _pregenDeckId = _pregenEntries[0]?.deck_id ?? _pregenDecks[0]?.id ?? null;
    }
    _renderPregenPanel();
  } catch (e) {
    const el = document.getElementById('pregen-config-body');
    if (el) el.textContent = 'Failed to load: ' + e.message;
  }
}

function _pregenSelectDeck(id) {
  _pregenDeckId = parseInt(id);
  _renderPregenPanel();
}

function _renderPregenPanel() {
  const el = document.getElementById('pregen-config-body');
  if (!el) return;
  const deckOpts = _pregenDecks.map(d =>
    `<option value="${d.id}" ${d.id === _pregenDeckId ? 'selected' : ''}>${d.name}</option>`).join('');
  const rows = ['listening', 'creating', 'reading'].map(cat => {
    const e = _pregenEntries.find(x => x.deck_id === _pregenDeckId && x.category === cat);
    const modeOpts = PREGEN_MODE_OPTIONS.map(([v, label]) =>
      `<option value="${v}" ${(e?.mode || '') === v ? 'selected' : ''}>${label}</option>`).join('');
    return `<div class="keymap-row">
      <span class="keymap-label">${cat[0].toUpperCase() + cat.slice(1)}</span>
      <select class="opt-input" id="pregen-mode-${cat}" style="flex:1">${modeOpts}</select>
      <span style="font-size:12px;color:var(--muted);white-space:nowrap" title="Max HSK level for background vocabulary in the generated story">HSK&nbsp;≤</span>
      <input class="opt-input" id="pregen-hsk-${cat}" type="number" min="1" max="6"
             value="${e?.max_hsk ?? 3}" title="Max HSK level for background vocabulary in the generated story" style="width:56px">
    </div>`;
  }).join('');
  const dimmed = _pregenEnabled ? '' : 'opacity:0.4;pointer-events:none';
  el.innerHTML = `
    <div class="keymap-row">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <input type="checkbox" id="pregen-enabled-switch" ${_pregenEnabled ? 'checked' : ''}
               onchange="setPregenEnabled(this.checked)" style="width:18px;height:18px;cursor:pointer">
        <span class="keymap-label">Enable morning pre-generation</span>
      </label>
    </div>
    ${_pregenEnabled ? '' :
      '<p class="keymap-hint" style="margin:2px 0 8px">Off — the server generates nothing in the morning; open a category during the day to generate on demand.</p>'}
    <div style="${dimmed}">
      <div class="keymap-row">
        <span class="keymap-label">Deck</span>
        <select class="opt-input" id="pregen-deck-select" style="flex:1" onchange="_pregenSelectDeck(this.value)">${deckOpts}</select>
      </div>
      ${rows}
      <div class="keymap-row">
        <button class="keymap-reset-all" onclick="savePregenConfig()">Save morning settings</button>
        <span id="pregen-save-msg" class="keymap-label"></span>
      </div>
    </div>`;
}

async function setPregenEnabled(enabled) {
  try {
    const res = await api('PUT', '/api/pregen-enabled', { enabled });
    _pregenEnabled = res?.enabled !== false && enabled;
  } catch (e) {
    _pregenEnabled = !enabled; // revert optimistic state on failure
  }
  _renderPregenPanel();
}

async function savePregenConfig() {
  const entries = [];
  for (const cat of ['listening', 'creating', 'reading']) {
    const mode = document.getElementById(`pregen-mode-${cat}`)?.value;
    if (!mode) continue; // Off → no row for this category
    const hsk = parseInt(document.getElementById(`pregen-hsk-${cat}`)?.value) || 3;
    entries.push({ category: cat, mode, max_hsk: hsk });
  }
  const msg = document.getElementById('pregen-save-msg');
  try {
    const res = await api('PUT', '/api/pregen-config', { deck_id: _pregenDeckId, entries });
    if (res?.ok) {
      _pregenEntries = res.entries || [];
      if (msg) msg.textContent = '✓ Saved';
    } else if (msg) {
      msg.textContent = 'Error: ' + (res?.reason || 'save failed');
    }
  } catch (e) {
    if (msg) msg.textContent = 'Error: ' + e.message;
  }
}

// ── Knowledge base: one unified material library ────────────────────────────
// #936 (umbrella #934) replaced the four kind sub-tabs (podcast/video/reel/
// article, #653/#764) with a SINGLE list. Kind is now just one filter among
// several, because "which of the four buckets did I put this in" is not how
// Daniel looks for something — "the thing I processed last week by that
// author" is. Two parallel list implementations were also two places to fix
// every future sort and filter, so the tabs are gone rather than kept
// alongside.
//
// Screens (_knowledgeScreen):
//   'list'  the unified material list — sort bar, filter bar, one Add button
//   'feed'  one RSS feed's episodes (unchanged from #502; reachable from the
//           feed filter chip and from the feeds screen)
//   'feeds' RSS feed management (add/delete/auto-process/detail level) — it
//           moved off the old podcast tab into its own screen behind the
//           header's 📡 button
// Layer 3 (item detail) is shared by all of them, unchanged.
//
// Hash routes (the bare "podcast" prefix is kept working FOREVER — episode
// links already went out in notification emails and Signal messages):
//   #podcast-<id>      / #knowledge-<id>       -> item detail
//   #podcast-feed-<id> / #knowledge-feed-<id>  -> one feed's episodes
//   #knowledge-podcast|video|reel|article      -> the unified list with that
//                                                 kind preselected as a filter
//                                                 (the /knowledge/<kind>
//                                                 redirect's target, #704)
// The tab form is letters-only and the other two digits-only, so they can
// never collide.
const PODCAST_STATUS_LABEL = {
  summarized:    'Summarized',
  no_transcript: 'No transcript',
  error:         'Error',
  pending:       'Pending',
  processing:    'processing…',
};
const PODCAST_STATUS_CLASS = {
  summarized:    'podcast-badge-ok',
  no_transcript: 'podcast-badge-muted',
  error:         'podcast-badge-error',
  pending:       'podcast-badge-pending',
  processing:    'podcast-badge-pending',
};

const KNOWLEDGE_KIND_LABEL = {
  podcast: '🎙️ Podcast', video: '📺 Video',
  article: '📄 Article', newsletter: '📰 Newsletter',
};
const KNOWLEDGE_PLATFORM_LABEL = {
  youtube: 'YouTube', instagram: 'Instagram', podcast: 'Podcast RSS',
  web: 'Web', upload: 'Upload', paste: 'Paste', email: 'E-Mail', signal: 'Signal',
};
// Label + backend sort key. Must stay in sync with database.EPISODE_SORTS —
// an unknown key doesn't 400, it silently falls back to the default order,
// which would look like "the sort button does nothing".
const KNOWLEDGE_SORTS = [
  ['processed_at', 'Processed'],
  ['published_at', 'Published'],
  ['created_at',   'Added'],
  ['title',        'Title'],
  ['author',       'Author'],
  ['duration',     'Length'],
];
const KNOWLEDGE_SINCE = [['', 'Any time'], ['7', 'Last 7 days'],
                         ['30', 'Last 30 days'], ['365', 'This year']];

let _podcastFeeds = [];
let _podcastEpisodes = [];
let _podcastConfig = null;
let _podcastCurrentFeedId = null;   // 'feed' screen: whose episodes are shown
let _podcastPollTimer = null;       // re-poll while any listed item is "processing"
let _knowledgeFacets = null;        // GET /api/knowledge/facets — the filter bar's option lists
let _knowledgeScreen = 'list';      // 'list' | 'feed' | 'feeds'
let _knowledgeAddOpen = false;
let _knowledgeAddMode = 'link';     // 'link' | 'text' | 'file' | 'feed'

// Filters live in localStorage: Daniel comes back to this page many times a
// day and re-picking the same three filters every time is the kind of friction
// that makes a filter bar go unused.
const KNOWLEDGE_FILTER_DEFAULTS = {
  sort: 'processed_at', order: 'desc',
  kind: [], platform: [], author: [], tag: [], status: [],
  since: '', archived: false, listId: null,
};
function _loadKnowledgeFilters() {
  try {
    const saved = JSON.parse(localStorage.getItem('knowledgeFilters') || '{}');
    // Merge onto the defaults rather than trusting the stored object: a
    // filter added in a later version must not be `undefined` for anyone who
    // already has a saved blob.
    return { ...KNOWLEDGE_FILTER_DEFAULTS, ...saved };
  } catch (e) { return { ...KNOWLEDGE_FILTER_DEFAULTS }; }
}
let _kFilters = _loadKnowledgeFilters();

function _saveKnowledgeFilters() {
  try { localStorage.setItem('knowledgeFilters', JSON.stringify(_kFilters)); }
  catch (e) { /* private mode / quota — filtering still works this session */ }
}

function _clearPodcastPoll() {
  if (_podcastPollTimer) { clearTimeout(_podcastPollTimer); _podcastPollTimer = null; }
}

function _isInstagramEpisode(ep) {
  // platform (#935) is now authoritative; the URL check stays as the fallback
  // for any row that predates the backfill or was hand-edited to blank.
  if (ep.platform) return ep.platform === 'instagram';
  let host = '';
  try { host = new URL(ep.youtube_url || '').hostname; } catch (e) { return false; }
  return /(^|\.)instagram\.com$/.test(host);
}

// A row stays self-describing wherever it's rendered (#761): the icon says
// what kind of material this is, since the list no longer sits inside a tab
// that already answered that.
function _knowledgeSourceIcon(ep) {
  if (_isInstagramEpisode(ep)) return '📱 ';
  return ({ podcast: '🎙️ ', video: '📺 ', article: '📄 ', newsletter: '📰 ' })[ep.kind] || '';
}

// Query string for the material list. Every axis repeats (?tag=a&tag=b), which
// the backend reads as OR-within-axis / AND-across-axes.
function _knowledgeQuery() {
  const p = new URLSearchParams();
  p.set('limit', '1000');
  p.set('sort', _kFilters.sort);
  p.set('order', _kFilters.order);
  for (const axis of ['kind', 'platform', 'author', 'tag', 'status']) {
    for (const v of _kFilters[axis] || []) p.append(axis, v);
  }
  if (_kFilters.since) {
    const days = parseInt(_kFilters.since, 10);
    if (days > 0) {
      const d = new Date(Date.now() - days * 86400000);
      p.set('since', d.toISOString().slice(0, 10));
    }
  }
  if (_kFilters.archived) p.set('include_archived', 'true');
  if (_kFilters.listId != null) p.set('list_id', String(_kFilters.listId));
  return p.toString();
}

// ── Screen: the unified material list ───────────────────────────────────────

// `tab` (optional) comes from the #knowledge-<kind> hash route and the
// /knowledge/<kind> redirect (#704). Those links predate #936 and must keep
// working, so a tab name is translated into the equivalent filter: 'reel' is
// platform=instagram (it was always a frontend-only split of kind=video,
// #764), everything else is a kind.
async function openKnowledge(tab) {
  navPush('knowledge:list');
  if (tab) {
    _kFilters.kind = [];
    _kFilters.platform = [];
    if (tab === 'reel') _kFilters.platform = ['instagram'];
    else if (tab) _kFilters.kind = [tab];
    _saveKnowledgeFilters();
  }
  _clearPodcastPoll();
  _podcastCurrentFeedId = null;
  _knowledgeScreen = 'list';
  setLoading('Loading…');
  await _loadKnowledgeList();
}

async function _loadKnowledgeList() {
  _clearPodcastPoll();
  _podcastCurrentFeedId = null;
  _knowledgeScreen = 'list';
  try {
    const [episodes, facets] = await Promise.all([
      api('GET', `/api/podcast/episodes?${_knowledgeQuery()}`),
      api('GET', '/api/knowledge/facets'),
    ]);
    _podcastEpisodes = episodes || [];
    _knowledgeFacets = facets || null;
    // facets already carries the list catalog — no second request for it.
    _knowledgeLists = facets?.lists || _knowledgeLists;
    _renderKnowledgeList();
    showView('knowledge');
    _initKnowledgeSwipe();
    _schedulePodcastPollIfNeeded();
  } catch (e) {
    showError('Knowledge failed: ' + e.message);
    showView('decks');
  }
}

// Re-fetch after a filter change without the full-screen loading flash — the
// bar stays put, only the rows below it swap.
async function _refreshKnowledgeList() {
  try {
    const [episodes, lists] = await Promise.all([
      api('GET', `/api/podcast/episodes?${_knowledgeQuery()}`),
      api('GET', '/api/knowledge/lists'),
    ]);
    _podcastEpisodes = episodes || [];
    _knowledgeLists = lists || _knowledgeLists;
    _renderKnowledgeList();
    _schedulePodcastPollIfNeeded();
  } catch (e) {
    showError('Knowledge failed: ' + e.message);
  }
}

function _knowledgeOptionsHtml(axis, facetKey, labels) {
  const opts = (_knowledgeFacets?.[facetKey] || [])
    .filter(o => !(_kFilters[axis] || []).includes(o.value))
    .map(o => `<option value="${_escHtml(o.value)}">${_escHtml((labels && labels[o.value]) || o.value)} (${o.count})</option>`)
    .join('');
  return opts;
}

// One <select> per axis that ADDS a chip and resets itself. Plain selects
// rather than a custom multi-select widget: no build step here, and the
// native picker is the one control that is genuinely good on iOS.
function _knowledgeFilterBarHtml() {
  const axes = [
    ['kind', 'kinds', 'Kind', KNOWLEDGE_KIND_LABEL],
    ['platform', 'platforms', 'Platform', KNOWLEDGE_PLATFORM_LABEL],
    ['author', 'authors', 'Author', null],
    ['tag', null, 'Tag', null],
    ['status', 'statuses', 'Status', null],
  ];
  const selects = axes.map(([axis, facetKey, label, labels]) => {
    const opts = axis === 'tag'
      ? (_knowledgeFacets?.tags || [])
          .filter(t => !(_kFilters.tag || []).includes(t.name))
          .map(t => `<option value="${_escHtml(t.name)}">${_escHtml(t.name)} (${t.count})</option>`).join('')
      : _knowledgeOptionsHtml(axis, facetKey, labels);
    if (!opts) return '';
    return `<select class="opt-input knowledge-filter-select"
                    onchange="knowledgeAddFilter('${axis}', this.value); this.selectedIndex=0">
              <option value="">${label}</option>${opts}
            </select>`;
  }).join('');

  const sinceOpts = KNOWLEDGE_SINCE.map(([v, l]) =>
    `<option value="${v}" ${_kFilters.since === v ? 'selected' : ''}>${l}</option>`).join('');
  const archivedCount = _knowledgeFacets?.archived_count || 0;

  return `<div class="knowledge-filter-bar">
    ${selects}
    <select class="opt-input knowledge-filter-select" onchange="knowledgeSetSince(this.value)">${sinceOpts}</select>
    ${archivedCount ? `<label class="knowledge-filter-toggle">
      <input type="checkbox" ${_kFilters.archived ? 'checked' : ''} onchange="knowledgeToggleArchived(this.checked)">
      Archived (${archivedCount})
    </label>` : ''}
  </div>`;
}

function _knowledgeChipsHtml() {
  const chips = [];
  const labelFor = {
    kind: (v) => KNOWLEDGE_KIND_LABEL[v] || v,
    platform: (v) => KNOWLEDGE_PLATFORM_LABEL[v] || v,
    author: (v) => v, tag: (v) => '#' + v, status: (v) => v,
  };
  for (const axis of ['kind', 'platform', 'author', 'tag', 'status']) {
    for (const v of _kFilters[axis] || []) {
      chips.push(`<button class="knowledge-chip" onclick="knowledgeRemoveFilter('${axis}', ${JSON.stringify(v).replace(/"/g, '&quot;')})">
        ${_escHtml(labelFor[axis](v))} ✕</button>`);
    }
  }
  if (!chips.length) return '';
  return `<div class="knowledge-chips">${chips.join('')}
    <button class="knowledge-chip knowledge-chip-clear" onclick="knowledgeClearFilters()">Clear all</button></div>`;
}

function _knowledgeSortBarHtml() {
  const opts = KNOWLEDGE_SORTS.map(([v, l]) =>
    `<option value="${v}" ${_kFilters.sort === v ? 'selected' : ''}>${l}</option>`).join('');
  const arrow = _kFilters.order === 'asc' ? '↑' : '↓';
  return `<div class="knowledge-sort-row">
    <span class="keymap-label">Sort</span>
    <select class="opt-input knowledge-filter-select" onchange="knowledgeSetSort(this.value)">${opts}</select>
    <button class="btn-secondary knowledge-order-btn" onclick="knowledgeToggleOrder()"
            title="${_kFilters.order === 'asc' ? 'Ascending' : 'Descending'}">${arrow}</button>
    <span class="knowledge-count">${_podcastEpisodes.length} item${_podcastEpisodes.length === 1 ? '' : 's'}</span>
  </div>`;
}

function _renderKnowledgeList() {
  const el = document.getElementById('view-knowledge-content');
  if (!el) return;
  const anyFilter = ['kind', 'platform', 'author', 'tag', 'status']
    .some(a => (_kFilters[a] || []).length) || _kFilters.since;
  const rows = _podcastEpisodes.map(ep => _knowledgeMaterialRowHtml(ep)).join('') ||
    `<div class="keymap-hint">${anyFilter
      ? 'Nothing matches these filters.'
      : 'Nothing here yet — use ＋ Add to paste a link, some text or a file.'}</div>`;
  el.innerHTML = `
    <div class="knowledge-header">
      <h2 class="keymap-heading" style="margin:0">Knowledge</h2>
      <span style="flex:1"></span>
      <button class="btn-secondary" onclick="openKnowledgeTags()" title="Manage tags">🏷 Tags</button>
      <button class="btn-secondary" onclick="openMailbox()" title="Gmail inbox — pick what to read">📬 Inbox</button>
      <button class="btn-secondary" onclick="openKnowledgeSubs()" title="Newsletters and podcast feeds you subscribe to">📡 Subscriptions</button>
      <button class="btn-secondary" onclick="toggleKnowledgeAdd()">${_knowledgeAddOpen ? '✕ Close' : '＋ Add'}</button>
    </div>
    ${_knowledgeAddOpen ? _knowledgeAddPanelHtml() : ''}
    ${_knowledgeListsBarHtml()}
    ${_knowledgeSearchBarHtml()}
    ${_knowledgeSearchResults !== null ? _knowledgeSearchResultsHtml() : `
    ${_knowledgeSortBarHtml()}
    ${_knowledgeFilterBarHtml()}
    ${_knowledgeChipsHtml()}
    <div class="podcast-list">${rows}</div>`}`;
  const box = document.getElementById('knowledge-search-input');
  // Re-focus after the re-render, and put the caret at the end: this input is
  // rebuilt on every keystroke's response, and losing focus mid-word would
  // make the search box unusable.
  if (box && _knowledgeSearchFocused) { box.focus(); box.selectionStart = box.selectionEnd = box.value.length; }
}

// ── Search (#939) ───────────────────────────────────────────────────────────
// Searches everything: titles, transcripts, both AI summaries and every
// per-language rendition. It replaces the list rather than filtering it —
// relevance order and "which field did this match in?" are the point, and
// neither survives being folded into the sort/filter bar.
let _knowledgeSearchQuery = '';
let _knowledgeSearchResults = null;   // null = not searching; [] = no hits
let _knowledgeSearchTimer = null;
let _knowledgeSearchFocused = false;

function _knowledgeSearchBarHtml() {
  return `<div class="knowledge-search-row">
    <input type="text" class="opt-input" id="knowledge-search-input"
           placeholder="🔍 Search titles, transcripts, summaries, translations…"
           value="${_escHtml(_knowledgeSearchQuery)}"
           oninput="onKnowledgeSearchInput(this.value)"
           onfocus="_knowledgeSearchFocused = true"
           onblur="_knowledgeSearchFocused = false">
    ${_knowledgeSearchQuery ? `<button class="btn-secondary" onclick="clearKnowledgeSearch()">✕</button>` : ''}
  </div>`;
}

function onKnowledgeSearchInput(value) {
  _knowledgeSearchQuery = value;
  _knowledgeSearchFocused = true;
  if (_knowledgeSearchTimer) clearTimeout(_knowledgeSearchTimer);
  if (!value.trim()) {
    _knowledgeSearchResults = null;
    _renderKnowledgeList();
    return;
  }
  // Debounced: this hits FTS over every transcript in the library, and firing
  // it per keystroke would queue up requests whose answers arrive out of order.
  _knowledgeSearchTimer = setTimeout(() => _runKnowledgeSearch(value), 250);
}

async function _runKnowledgeSearch(query) {
  try {
    const results = await api('GET', `/api/knowledge/search?q=${encodeURIComponent(query)}`);
    // A slow response for an older query must not overwrite a newer one.
    if (query !== _knowledgeSearchQuery) return;
    _knowledgeSearchResults = results || [];
    _renderKnowledgeList();
  } catch (e) {
    showError('Search failed: ' + e.message);
  }
}

function clearKnowledgeSearch() {
  _knowledgeSearchQuery = '';
  _knowledgeSearchResults = null;
  if (_knowledgeSearchTimer) clearTimeout(_knowledgeSearchTimer);
  _renderKnowledgeList();
}

// The server marks matches with \x02/\x03 — characters that cannot occur in
// real text. Escape the snippet FIRST, then turn the sentinels into <mark>:
// this text was written by an AI or copied off the web, so it never gets to
// contribute markup of its own.
function _knowledgeSnippetHtml(snippet) {
  return _escHtml(snippet || '')
    .split('\u0002').join('<mark>')
    .split('\u0003').join('</mark>');
}

function _knowledgeSearchResultsHtml() {
  const results = _knowledgeSearchResults || [];
  if (!results.length) {
    return `<div class="keymap-hint">Nothing matches “${_escHtml(_knowledgeSearchQuery)}”.</div>`;
  }
  const rows = results.map(r => {
    const clickable = r.status === 'summarized';
    return `<div class="podcast-row${clickable ? ' podcast-row-clickable' : ''}"
                 ${clickable ? `onclick="openKnowledgeItem(${r.episode_id})"` : ''}>
      <span class="podcast-row-title podcast-row-title-stack">
        <span>${_knowledgeSourceIcon(r)}${_escHtml(r.title || '(untitled)')}</span>
        <span class="knowledge-snippet">${_knowledgeSnippetHtml(r.snippet)}</span>
        <span class="knowledge-snippet-fields">${r.fields.map(f => _escHtml(f)).join(' · ')}</span>
      </span>
      <span class="podcast-row-meta">
        ${r.author ? `<span class="podcast-row-date">${_escHtml(r.author)}</span>` : ''}
        <span class="podcast-row-date">${_localDate(r.processed_at || r.published_at || r.created_at || '')}</span>
      </span>
    </div>`;
  }).join('');
  return `<div class="knowledge-sort-row">
      <span class="keymap-label">Search results</span>
      <span class="knowledge-count">${results.length} item${results.length === 1 ? '' : 's'}</span>
    </div>
    <div class="podcast-list">${rows}</div>`;
}

function knowledgeAddFilter(axis, value) {
  if (!value) return;
  if (!(_kFilters[axis] || []).includes(value)) _kFilters[axis] = [...(_kFilters[axis] || []), value];
  _saveKnowledgeFilters();
  _refreshKnowledgeList();
}
function knowledgeRemoveFilter(axis, value) {
  _kFilters[axis] = (_kFilters[axis] || []).filter(v => v !== value);
  _saveKnowledgeFilters();
  _refreshKnowledgeList();
}
function knowledgeClearFilters() {
  _kFilters = { ..._kFilters, kind: [], platform: [], author: [], tag: [], status: [],
                since: '', listId: null };
  _saveKnowledgeFilters();
  _refreshKnowledgeList();
}
function knowledgeSetSort(sort) {
  _kFilters.sort = sort;
  _saveKnowledgeFilters();
  _refreshKnowledgeList();
}
function knowledgeToggleOrder() {
  _kFilters.order = _kFilters.order === 'asc' ? 'desc' : 'asc';
  _saveKnowledgeFilters();
  _refreshKnowledgeList();
}
function knowledgeSetSince(v) {
  _kFilters.since = v;
  _saveKnowledgeFilters();
  _refreshKnowledgeList();
}
function knowledgeToggleArchived(checked) {
  _kFilters.archived = !!checked;
  _saveKnowledgeFilters();
  _refreshKnowledgeList();
}

// A row shows what the filters filter on — kind icon, title, author, platform,
// tags, the processed date. Without those the filter bar would be operating on
// data the list never displays.
//
// The row is wrapped in a swipe container (#940): the two coloured action
// panes sit behind it and are revealed as the row itself is dragged sideways.
function _knowledgeMaterialRowHtml(ep) {
  const status = ep.status || 'pending';
  const label = PODCAST_STATUS_LABEL[status] || status;
  const cls = PODCAST_STATUS_CLASS[status] || 'podcast-badge-muted';
  const clickable = status === 'summarized';
  const date = _localDate(ep.processed_at || ep.published_at || ep.created_at || '');
  let source = ep.author || '';
  if (!source && ep.youtube_url) {
    try { source = new URL(ep.youtube_url).hostname.replace(/^www\./, ''); } catch (e) { /* leave blank */ }
  }
  const platform = ep.platform ? (KNOWLEDGE_PLATFORM_LABEL[ep.platform] || ep.platform) : '';
  const tags = (ep.tags || []).map(t =>
    `<span class="knowledge-tag ${t.source === 'ai' ? 'knowledge-tag-ai' : ''}">${_escHtml(t.name)}</span>`).join('');
  const transcribable = ['pending', 'no_transcript', 'error'].includes(status);
  const transcribeBtn = transcribable
    ? `<button class="btn-secondary podcast-transcribe-btn" onclick="event.stopPropagation(); _transcribePodcastEpisode(${ep.id})">Transcribe</button>`
    : '';
  const inReadLater = _knowledgeInReadLater(ep);
  const archived = !!ep.archived_at;
  return `<div class="knowledge-swipe" data-episode-id="${ep.id}">
    <div class="knowledge-swipe-action knowledge-swipe-left">${inReadLater ? '✕ Read Later' : '📖 Read Later'}</div>
    <div class="knowledge-swipe-action knowledge-swipe-right">${archived ? '↩ Unarchive' : '🗄 Archive'}</div>
    <div class="podcast-row knowledge-swipe-row${clickable ? ' podcast-row-clickable' : ''}${archived ? ' knowledge-row-archived' : ''}"
         ${clickable ? `onclick="openKnowledgeItem(${ep.id})"` : ''}>
      <span class="podcast-row-title podcast-row-title-stack">
        <span class="podcast-row-title-main">${inReadLater ? '<span title="Read Later">📖 </span>' : ''}${_knowledgeSourceIcon(ep)}${_escHtml(ep.title || '(untitled)')}</span>
        ${ep.title_en ? `<span style="font-size:12px;color:var(--muted)">${_escHtml(ep.title_en)}</span>` : ''}
        ${tags ? `<span class="knowledge-tag-row">${tags}</span>` : ''}
      </span>
      <span class="podcast-row-meta">
        ${source ? `<span class="podcast-row-date">${_escHtml(source)}</span>` : ''}
        ${platform ? `<span class="podcast-row-date">${_escHtml(platform)}</span>` : ''}
        <span class="podcast-row-date">${date}</span>
        <span class="podcast-badge ${cls}">${label}</span>
        ${transcribeBtn}
        <span class="knowledge-hover-actions">
          <button class="btn-secondary podcast-transcribe-btn" title="Read Later"
                  onclick="event.stopPropagation(); toggleKnowledgeReadLater(${ep.id})">${inReadLater ? '✕📖' : '📖'}</button>
          <button class="btn-secondary podcast-transcribe-btn" title="${archived ? 'Unarchive' : 'Archive'}"
                  onclick="event.stopPropagation(); toggleKnowledgeArchived(${ep.id})">${archived ? '↩' : '🗄'}</button>
        </span>
      </span>
    </div>
  </div>`;
}

// ── Lists, archiving and the swipe gesture (#940) ───────────────────────────
// Ingesting and reading are two different moments: material is fetched and
// summarized long before Daniel sits down with it. Read Later is where the
// second moment gets recorded, and swiping is how it gets recorded without
// opening anything.

let _knowledgeLists = [];

function _readLaterId() {
  const builtin = _knowledgeLists.find(l => l.is_builtin);
  return builtin ? builtin.id : null;
}

function _knowledgeInReadLater(ep) {
  const id = _readLaterId();
  return id != null && (ep.list_ids || []).includes(id);
}

function _knowledgeListsBarHtml() {
  if (!_knowledgeLists.length) return '';
  const btn = (id, label, count, extra = '') =>
    `<button class="hcal-seg-btn ${_kFilters.listId === id ? 'active' : ''}"
             onclick="knowledgeSetList(${id === null ? 'null' : id})">${label}${
      count != null ? ` (${count})` : ''}${extra}</button>`;
  const buttons = [btn(null, 'All', null)].concat(_knowledgeLists.map(l =>
    btn(l.id, `${l.icon || '📂'} ${_escHtml(l.name)}`, l.count))).join('');
  const current = _knowledgeLists.find(l => l.id === _kFilters.listId);
  return `<div class="hcal-seg knowledge-lists-bar">
      ${buttons}
      <button class="hcal-seg-btn" onclick="createKnowledgeList()">＋</button>
      ${current && !current.is_builtin
        ? `<button class="hcal-seg-btn" onclick="deleteKnowledgeList(${current.id})" title="Delete this list">🗑</button>`
        : ''}
    </div>`;
}

function knowledgeSetList(listId) {
  _kFilters.listId = listId;
  _saveKnowledgeFilters();
  _refreshKnowledgeList();
}

async function createKnowledgeList() {
  const name = prompt('New list name');
  if (!name || !name.trim()) return;
  try {
    const created = await api('POST', '/api/knowledge/lists', { name: name.trim() });
    _knowledgeLists = await api('GET', '/api/knowledge/lists') || [];
    _kFilters.listId = created?.id ?? null;
    _saveKnowledgeFilters();
    await _refreshKnowledgeList();
  } catch (e) {
    showError('Could not create list: ' + e.message);
  }
}

async function deleteKnowledgeList(listId) {
  const list = _knowledgeLists.find(l => l.id === listId);
  if (!confirm(`Delete the list "${list?.name || ''}"? The material itself is kept.`)) return;
  try {
    await api('DELETE', `/api/knowledge/lists/${listId}`);
    _knowledgeLists = await api('GET', '/api/knowledge/lists') || [];
    _kFilters.listId = null;
    _saveKnowledgeFilters();
    await _refreshKnowledgeList();
  } catch (e) {
    showError('Could not delete list: ' + e.message);
  }
}

// Both actions are optimistic — on a phone, a row that sits still for half a
// second after a swipe feels broken. On failure the change is rolled back and
// the error is shown; nothing is silently dropped.
async function toggleKnowledgeReadLater(episodeId) {
  const listId = _readLaterId();
  if (listId == null) return;
  const ep = _podcastEpisodes.find(e => e.id === episodeId);
  if (!ep) return;
  const wasIn = _knowledgeInReadLater(ep);
  ep.list_ids = wasIn ? (ep.list_ids || []).filter(id => id !== listId)
                      : [...(ep.list_ids || []), listId];
  _renderKnowledgeList();
  try {
    if (wasIn) await api('DELETE', `/api/knowledge/lists/${listId}/items/${episodeId}`);
    else await api('POST', `/api/knowledge/lists/${listId}/items`, { episode_id: episodeId });
    _knowledgeToast(wasIn ? 'Removed from Read Later' : 'Added to Read Later',
                    () => toggleKnowledgeReadLater(episodeId));
  } catch (e) {
    ep.list_ids = wasIn ? [...(ep.list_ids || []), listId]
                        : (ep.list_ids || []).filter(id => id !== listId);
    _renderKnowledgeList();
    showError('Read Later failed: ' + e.message);
  }
}

async function toggleKnowledgeArchived(episodeId) {
  const ep = _podcastEpisodes.find(e => e.id === episodeId);
  if (!ep) return;
  const wasArchived = !!ep.archived_at;
  ep.archived_at = wasArchived ? null : new Date().toISOString();
  // An archived row leaves the default view entirely, so drop it from the
  // in-memory list too rather than leaving a ghost behind.
  const hideIt = !wasArchived && !_kFilters.archived;
  if (hideIt) _podcastEpisodes = _podcastEpisodes.filter(e => e.id !== episodeId);
  _renderKnowledgeList();
  try {
    await api('POST', `/api/podcast/episodes/${episodeId}/archive?archived=${!wasArchived}`);
    _knowledgeToast(wasArchived ? 'Unarchived' : 'Archived',
                    () => _undoArchive(episodeId, wasArchived));
  } catch (e) {
    showError('Archive failed: ' + e.message);
    await _refreshKnowledgeList();
  }
}

async function _undoArchive(episodeId, wasArchived) {
  try {
    await api('POST', `/api/podcast/episodes/${episodeId}/archive?archived=${wasArchived}`);
    await _refreshKnowledgeList();
  } catch (e) {
    showError('Undo failed: ' + e.message);
  }
}

// A short-lived undo strip. Mis-swiping on a phone is far too easy for an
// action to be one-way.
let _knowledgeToastTimer = null;
function _knowledgeToast(message, undo) {
  let el = document.getElementById('knowledge-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'knowledge-toast';
    document.body.appendChild(el);
  }
  el.textContent = '';
  el.appendChild(document.createTextNode(message + ' '));
  if (undo) {
    const btn = document.createElement('button');
    btn.textContent = 'Undo';
    btn.onclick = () => { _hideKnowledgeToast(); undo(); };
    el.appendChild(btn);
  }
  el.style.display = 'flex';
  if (_knowledgeToastTimer) clearTimeout(_knowledgeToastTimer);
  _knowledgeToastTimer = setTimeout(_hideKnowledgeToast, 5000);
}
function _hideKnowledgeToast() {
  const el = document.getElementById('knowledge-toast');
  if (el) el.style.display = 'none';
  if (_knowledgeToastTimer) { clearTimeout(_knowledgeToastTimer); _knowledgeToastTimer = null; }
}

// Swipe, iPhone-Mail style. Plain touch events — this project has no build
// step and no room for a gesture library.
const _SWIPE_TRIGGER = 90;     // px past which the action fires on release
const _SWIPE_MAX = 130;        // px the row can be dragged
const _SWIPE_DECIDE = 12;      // px of movement before the axis is locked in

let _swipe = null;

function _initKnowledgeSwipe() {
  const host = document.getElementById('view-knowledge-content');
  if (!host || host.dataset.swipeBound) return;
  host.dataset.swipeBound = '1';
  // Delegated, and bound once: the list's innerHTML is replaced on every
  // filter change, so per-row listeners would have to be re-attached forever.
  host.addEventListener('touchstart', _onSwipeStart, { passive: true });
  host.addEventListener('touchmove', _onSwipeMove, { passive: false });
  host.addEventListener('touchend', _onSwipeEnd);
  host.addEventListener('touchcancel', _onSwipeEnd);
}

function _onSwipeStart(e) {
  const wrap = e.target.closest?.('.knowledge-swipe');
  if (!wrap || e.touches.length !== 1) return;
  const t = e.touches[0];
  _swipe = { wrap, row: wrap.querySelector('.knowledge-swipe-row'),
             x0: t.clientX, y0: t.clientY, dx: 0, axis: null,
             id: parseInt(wrap.dataset.episodeId, 10) };
}

function _onSwipeMove(e) {
  if (!_swipe) return;
  const t = e.touches[0];
  const dx = t.clientX - _swipe.x0;
  const dy = t.clientY - _swipe.y0;
  if (_swipe.axis === null) {
    if (Math.abs(dx) < _SWIPE_DECIDE && Math.abs(dy) < _SWIPE_DECIDE) return;
    // Lock the axis once, on the first real movement. Without this the page
    // can no longer be scrolled past the list — a vertical drag that starts
    // with a pixel of horizontal noise would be eaten as a swipe.
    _swipe.axis = Math.abs(dx) > Math.abs(dy) * 1.5 ? 'x' : 'y';
  }
  if (_swipe.axis !== 'x') return;
  e.preventDefault();
  _swipe.dx = Math.max(-_SWIPE_MAX, Math.min(_SWIPE_MAX, dx));
  _swipe.wrap.classList.add('knowledge-swipe-active');
  if (_swipe.row) _swipe.row.style.transform = `translateX(${_swipe.dx}px)`;
}

function _onSwipeEnd() {
  const swipe = _swipe;
  _swipe = null;
  if (!swipe || swipe.axis !== 'x') return;
  if (swipe.row) { swipe.row.style.transform = ''; }
  swipe.wrap.classList.remove('knowledge-swipe-active');
  if (swipe.dx >= _SWIPE_TRIGGER) toggleKnowledgeReadLater(swipe.id);
  else if (swipe.dx <= -_SWIPE_TRIGGER) toggleKnowledgeArchived(swipe.id);
}

// ── The one Add button ──────────────────────────────────────────────────────
// #936: every way material gets in is behind this single button. Before, a
// link box lived on three tabs, the paste/file boxes only on Articles, and
// "add an RSS feed" was a fourth box on a different screen — which of them you
// got depended on which tab you happened to be standing on.

function toggleKnowledgeAdd() {
  _knowledgeAddOpen = !_knowledgeAddOpen;
  _renderKnowledgeList();
  if (_knowledgeAddOpen) {
    const first = document.getElementById('knowledge-add-url')
               || document.getElementById('knowledge-add-text');
    if (first) first.focus();
  }
}

function switchKnowledgeAddMode(mode) {
  _knowledgeAddMode = mode;
  _renderKnowledgeList();
}

function _knowledgeAddModeBarHtml() {
  const modes = [['link', '🔗 Link'], ['text', '📋 Text'], ['file', '📎 File'], ['feed', '📡 Feed']];
  const btns = modes.map(([id, label]) =>
    `<button class="hcal-seg-btn ${_knowledgeAddMode === id ? 'active' : ''}" onclick="switchKnowledgeAddMode('${id}')">${label}</button>`
  ).join('');
  return `<div class="hcal-seg" style="margin-bottom:12px">${btns}</div>`;
}

// china-kritisch (#731): DeepSeek summarizes these dishonestly, so the box
// sends a flag that makes the server's API fallback use GPT. Deliberately
// resets to unchecked on every re-render — the overwhelming majority of
// material is fine on the cheap model, and a sticky checkbox would silently
// spend GPT money on all of it.
function _knowledgeChinaCriticalChecked() {
  return !!document.getElementById('knowledge-add-china')?.checked;
}

const _KNOWLEDGE_CHINA_ROW = `
  <label class="keymap-row" style="gap:6px;font-size:12px;color:var(--muted);cursor:pointer">
    <input type="checkbox" id="knowledge-add-china" style="margin:0">
    china-kritisch — summarize with GPT instead of DeepSeek
  </label>
  <span id="knowledge-add-msg" style="font-size:12px;color:var(--muted)"></span>`;

// The three optional metadata fields the text/file forms share (#833/#835):
// whatever is left blank is read out of the material itself by one cheap AI
// call, so none of them is required.
const _KNOWLEDGE_META_FIELDS = `
  <input type="text" class="edit-input" id="knowledge-add-source-url"
         placeholder="Original link (optional)">
  <input type="text" class="edit-input" id="knowledge-add-title"
         placeholder="Title (optional — read from the text)">
  <input type="text" class="edit-input" id="knowledge-add-author"
         placeholder="Author (optional — read from the text)">`;

function _knowledgeAddPanelHtml() {
  let body;
  if (_knowledgeAddMode === 'file') {
    body = `<div class="keymap-row" style="align-items:flex-start">
        <div style="flex:1;display:flex;flex-direction:column;gap:6px">
          <input type="file" id="knowledge-add-file"
                 accept=".txt,.md,.markdown,.pdf,.docx" style="font-size:13px">
          ${_KNOWLEDGE_META_FIELDS}
        </div>
        <button class="btn-secondary" onclick="submitKnowledgeFile()" style="flex-shrink:0">Add</button>
      </div>${_KNOWLEDGE_CHINA_ROW}`;
  } else if (_knowledgeAddMode === 'text') {
    body = `<div class="keymap-row" style="align-items:flex-start">
        <div style="flex:1;display:flex;flex-direction:column;gap:6px">
          <textarea class="edit-input" id="knowledge-add-text" rows="10"
                    placeholder="Paste the full article text here…"
                    style="width:100%;box-sizing:border-box;resize:vertical"></textarea>
          ${_KNOWLEDGE_META_FIELDS}
        </div>
        <button class="btn-secondary" onclick="submitKnowledgeText()" style="flex-shrink:0">Add</button>
      </div>${_KNOWLEDGE_CHINA_ROW}`;
  } else if (_knowledgeAddMode === 'feed') {
    // Subscribing to a podcast is "adding material" too, so it belongs behind
    // the same button; managing the existing feeds is a different job and
    // lives on its own screen (📡 Feeds).
    body = `<div class="keymap-row">
        <input type="text" class="opt-input" id="knowledge-add-feed-url"
               placeholder="Podcast RSS feed URL…" style="flex:1"
               onkeydown="if(event.key==='Enter') submitKnowledgeFeed()">
        <button class="btn-secondary" onclick="submitKnowledgeFeed()">Add</button>
      </div>
      <span id="knowledge-add-msg" style="font-size:12px;color:var(--muted)"></span>
      <p class="keymap-hint" style="margin:6px 0 0">New episodes are crawled hourly. Manage feeds under 📡 Feeds.</p>`;
  } else {
    body = `<div class="keymap-row">
        <input type="text" class="opt-input" id="knowledge-add-url"
               placeholder="Paste a link — article, YouTube, Instagram Reel…" style="flex:1"
               onkeydown="if(event.key==='Enter') submitKnowledgeUrl()">
        <button class="btn-secondary" onclick="submitKnowledgeUrl()">Add</button>
      </div>${_KNOWLEDGE_CHINA_ROW}`;
  }
  return `<div class="keymap-panel">${_knowledgeAddModeBarHtml()}${body}</div>`;
}

// Submits, clears immediately so the next link can be pasted right away (same
// pattern as the #636 add-word box), then kicks off processing and starts
// polling for the status update.
async function submitKnowledgeUrl() {
  const input = document.getElementById('knowledge-add-url');
  const msg = document.getElementById('knowledge-add-msg');
  const url = (input?.value || '').trim();
  if (!url) return;
  // Read the checkbox BEFORE clearing/re-rendering — _submitKnowledgeAdd
  // rebuilds this panel's DOM when it refreshes the list.
  const china_critical = _knowledgeChinaCriticalChecked();
  if (input) { input.value = ''; input.focus(); }
  await _submitKnowledgeAdd({ url, china_critical }, msg);
}

// Paste-a-body (#668) — for paywalled pieces the server can't fetch. Only the
// body is required (#833): a blank title/author/link is filled in server-side
// from the text itself, so there is nothing to validate here beyond "is there
// a body".
async function submitKnowledgeText() {
  const titleInput = document.getElementById('knowledge-add-title');
  const authorInput = document.getElementById('knowledge-add-author');
  const urlInput = document.getElementById('knowledge-add-source-url');
  const textInput = document.getElementById('knowledge-add-text');
  const msg = document.getElementById('knowledge-add-msg');
  const text = (textInput?.value || '').trim();
  if (!text) return;
  const payload = {
    text,
    title: (titleInput?.value || '').trim(),
    author: (authorInput?.value || '').trim(),
    source_url: (urlInput?.value || '').trim(),
    china_critical: _knowledgeChinaCriticalChecked(),
  };
  for (const input of [titleInput, authorInput, urlInput]) if (input) input.value = '';
  if (textInput) { textInput.value = ''; textInput.focus(); }
  await _submitKnowledgeAdd(payload, msg);
}

// File upload (#835) — .txt/.md/.pdf/.docx. Goes through ingestKnowledgeFile()
// in shared.js, which posts the file and then starts processing exactly like a
// paste does.
async function submitKnowledgeFile() {
  const fileInput = document.getElementById('knowledge-add-file');
  const msg = document.getElementById('knowledge-add-msg');
  const file = fileInput?.files?.[0];
  if (!file) {
    if (msg) msg.textContent = 'Pick a file first.';
    return;
  }
  const fields = {
    title: (document.getElementById('knowledge-add-title')?.value || '').trim(),
    author: (document.getElementById('knowledge-add-author')?.value || '').trim(),
    source_url: (document.getElementById('knowledge-add-source-url')?.value || '').trim(),
    china_critical: _knowledgeChinaCriticalChecked(),
  };
  if (msg) msg.textContent = `Uploading ${file.name}…`;
  try {
    const res = await ingestKnowledgeFile(file, fields);
    const done = res?.status === 'already_exists'
      ? 'Already in your library.' : 'Added — processing on the server.';
    await _refreshKnowledgeList();
    // Re-query: the refresh above rebuilds this panel, so the element
    // captured before it is no longer on the page.
    const after = document.getElementById('knowledge-add-msg');
    if (after) after.textContent = done;
  } catch (e) {
    if (msg) msg.textContent = 'Error: ' + e.message;
  }
}

async function submitKnowledgeFeed() {
  const input = document.getElementById('knowledge-add-feed-url');
  const msg = document.getElementById('knowledge-add-msg');
  const url = (input?.value || '').trim();
  if (!url) return;
  if (msg) msg.textContent = 'Adding…';
  try {
    await api('POST', '/api/podcast/feeds', { url });
    if (input) input.value = '';
    await _refreshKnowledgeList();
    const after = document.getElementById('knowledge-add-msg');
    if (after) after.textContent = 'Feed added — its latest episodes are being fetched.';
  } catch (e) {
    if (msg) msg.textContent = 'Error: ' + e.message;
  }
}

// Shared tail end of the add boxes: ingest (shared.js kicks off processing),
// then refresh the list.
async function _submitKnowledgeAdd(payload, msg) {
  if (msg) msg.textContent = 'Adding…';
  try {
    const res = await ingestKnowledge(payload);
    await _refreshKnowledgeList();
    const after = document.getElementById('knowledge-add-msg');
    if (after) {
      after.textContent = res?.status === 'already_exists'
        ? 'Already in your library.' : 'Added — processing on the server.';
    }
  } catch (e) {
    const after = document.getElementById('knowledge-add-msg') || msg;
    if (after) after.textContent = 'Error: ' + e.message;
  }
}

// ── Screen: tag management (#938) ───────────────────────────────────────────
// The AI tagger will produce near-duplicates ('KI' next to 'AI') no matter how
// carefully the prompt asks it not to. Without a place to merge and delete
// them, the tag vocabulary degrades until the filter is useless — so this
// screen is part of the feature, not an extra.

let _knowledgeTags = [];

async function openKnowledgeTags() {
  navPush('knowledge:tags');
  _clearPodcastPoll();
  _podcastCurrentFeedId = null;
  _knowledgeScreen = 'tags';
  setLoading('Loading…');
  try {
    _knowledgeTags = await api('GET', '/api/knowledge/tags') || [];
    _renderKnowledgeTags();
    showView('knowledge');
  } catch (e) {
    showError('Tags failed: ' + e.message);
    openKnowledge();
  }
}

function _renderKnowledgeTags() {
  const el = document.getElementById('view-knowledge-content');
  if (!el) return;
  const rows = _knowledgeTags.map(t => `
    <div class="podcast-row">
      <span class="podcast-row-title">
        <input type="text" class="edit-input" value="${_escHtml(t.name)}"
               onchange="renameKnowledgeTag(${t.id}, this.value)">
      </span>
      <span class="podcast-row-meta">
        <span class="podcast-row-date">${t.count} item${t.count === 1 ? '' : 's'}</span>
        <button class="btn-secondary podcast-transcribe-btn" onclick="deleteKnowledgeTag(${t.id})">Delete</button>
      </span>
    </div>`).join('') || '<div class="keymap-hint">No tags yet — they appear once material is summarized.</div>';
  el.innerHTML = `
    <div class="keymap-panel">
      <h2 class="keymap-heading">Tags</h2>
      <p class="keymap-hint">Rename a tag onto an existing name to <b>merge</b> the two — that is how near-duplicates from the auto-tagger get cleaned up. Deleting a tag removes it from every item.</p>
    </div>
    <div class="podcast-list">${rows}</div>`;
}

async function renameKnowledgeTag(tagId, name) {
  const trimmed = (name || '').trim();
  if (!trimmed) { _renderKnowledgeTags(); return; }
  try {
    _knowledgeTags = await api('PUT', `/api/knowledge/tags/${tagId}`, { name: trimmed }) || [];
    _knowledgeFacets = null;   // the vocabulary changed under the filter bar
    _renderKnowledgeTags();
  } catch (e) {
    showError('Rename failed: ' + e.message);
    _renderKnowledgeTags();
  }
}

async function deleteKnowledgeTag(tagId) {
  const tag = _knowledgeTags.find(t => t.id === tagId);
  if (!confirm(`Delete "${tag?.name || 'this tag'}"? It is removed from ${tag?.count || 0} item(s).`)) return;
  try {
    await api('DELETE', `/api/knowledge/tags/${tagId}`);
    _knowledgeTags = _knowledgeTags.filter(t => t.id !== tagId);
    _knowledgeFacets = null;
    _renderKnowledgeTags();
  } catch (e) {
    showError('Delete failed: ' + e.message);
  }
}

// ── Screen: Subscriptions (#988) ────────────────────────────────────────────
// "Where do I subscribe / unsubscribe?" must have exactly one answer. Before
// this the two halves lived in unrelated places — podcast RSS behind the
// Knowledge header's 📡 button, newsletter senders behind a tab inside the
// standalone Mailbox view — and Daniel could not find either. Both are now
// tabs of one screen. The ⚡ button on each inbox row stays: it is the *same*
// switch (database.mail_senders.auto_process), so the two can never disagree.
let _subsTab = 'newsletters';        // 'newsletters' | 'feeds'

async function openKnowledgeSubs(tab) {
  navPush('knowledge:subs');
  _clearPodcastPoll();
  _podcastCurrentFeedId = null;
  _knowledgeScreen = 'subs';
  if (tab) _subsTab = tab;
  _mailboxState.notice = null;
  setLoading('Loading…');
  if (_subsTab === 'feeds') {
    try {
      const [feeds, config] = await Promise.all([
        api('GET', '/api/podcast/feeds'),
        api('GET', '/api/podcast/config'),
      ]);
      _podcastFeeds = feeds || [];
      _podcastConfig = config || {};
      _renderKnowledgeSubs();
      showView('knowledge');
    } catch (e) {
      showError('Feeds failed: ' + e.message);
      openKnowledge();
    }
    return;
  }
  showView('knowledge');
  // Cached from a previous visit: a full mailbox scan is expensive, and the
  // sender list does not change from one minute to the next (⟳ forces it).
  if (_mailboxState.senders) _renderKnowledgeSubs();
  else await _loadMailboxSenders();
}

// Kept as the old entry point — hash routes and the feed screen's back button
// still call it.
function openKnowledgeFeeds() { return openKnowledgeSubs('feeds'); }

function _subsTabsHtml() {
  return `
    <div class="knowledge-header">
      <h2 class="keymap-heading" style="margin:0">Subscriptions</h2>
    </div>
    <div class="mailbox-tabs">
      <button class="mailbox-tab${_subsTab === 'newsletters' ? ' active' : ''}"
              onclick="openKnowledgeSubs('newsletters')">📰 Newsletters</button>
      <button class="mailbox-tab${_subsTab === 'feeds' ? ' active' : ''}"
              onclick="openKnowledgeSubs('feeds')">🎙 Podcasts</button>
    </div>`;
}

function _renderKnowledgeSubs() {
  const el = document.getElementById('view-knowledge-content');
  if (!el) return;
  el.innerHTML = _subsTabsHtml() +
    (_subsTab === 'feeds' ? _podcastFeedsBodyHtml() : _mailboxSendersBodyHtml());
}

function _renderPodcastFeedList() {
  if (_knowledgeScreen !== 'subs') return;
  _renderKnowledgeSubs();
}

function _podcastFeedsBodyHtml() {
  const detailLevel = _podcastConfig?.detail_level || 'medium';
  const cards = _podcastFeeds.map(f => `
    <div class="podcast-feed-card">
      <div class="podcast-feed-card-main" onclick="openPodcastFeed(${f.id})">
        <span class="podcast-feed-card-title">${_escHtml(f.title || f.url)}</span>
        <span class="podcast-feed-card-count">${f.episode_count} episode${f.episode_count === 1 ? '' : 's'}</span>
      </div>
      <div class="podcast-feed-card-controls">
        <label class="podcast-feed-auto-toggle">
          <input type="checkbox" ${f.auto_process ? 'checked' : ''} onchange="_toggleFeedAuto(${f.id}, this.checked)">
          Auto-process new episodes
        </label>
        <button class="btn-secondary podcast-feed-delete" onclick="_deletePodcastFeed(${f.id})">Delete</button>
      </div>
    </div>`).join('') || '<div class="keymap-hint">No feeds yet — add one below.</div>';

  return `
    <div class="keymap-panel">
      <p class="keymap-hint">RSS feeds crawled hourly for new episodes. Auto-process feeds are transcribed+summarized automatically; other feeds only store new episodes' metadata until you pick one to transcribe.</p>
      <div class="keymap-row">
        <span class="keymap-label">Summary detail level</span>
        <select class="opt-input" id="podcast-detail-level" style="flex:1" onchange="_savePodcastDetailLevel(this.value)">
          <option value="short" ${detailLevel === 'short' ? 'selected' : ''}>Short</option>
          <option value="medium" ${detailLevel === 'medium' ? 'selected' : ''}>Medium</option>
          <option value="detailed" ${detailLevel === 'detailed' ? 'selected' : ''}>Detailed</option>
        </select>
        <span id="podcast-detail-save-msg" style="font-size:12px;color:var(--muted);min-width:60px"></span>
      </div>
    </div>
    <div class="podcast-feed-list">${cards}</div>
    <div class="keymap-panel podcast-add-feed-panel">
      <h2 class="keymap-heading">Add feed</h2>
      <div class="keymap-row">
        <input type="text" class="opt-input" id="podcast-add-feed-url" placeholder="RSS feed URL" style="flex:1">
        <button class="btn-secondary" onclick="_addPodcastFeed()">Add</button>
      </div>
      <span id="podcast-add-feed-msg" style="font-size:12px;color:var(--muted)"></span>
    </div>`;
}

async function _toggleFeedAuto(feedId, checked) {
  try {
    await api('PUT', `/api/podcast/feeds/${feedId}`, { auto_process: checked });
    const f = _podcastFeeds.find(x => x.id === feedId);
    if (f) f.auto_process = checked ? 1 : 0;
  } catch (e) {
    showError('Failed to update feed: ' + e.message);
    _renderPodcastFeedList();
  }
}

async function _deletePodcastFeed(feedId) {
  if (!confirm('Delete this feed? Already-ingested episodes are kept as history.')) return;
  try {
    await api('DELETE', `/api/podcast/feeds/${feedId}`);
    _podcastFeeds = _podcastFeeds.filter(f => f.id !== feedId);
    _renderPodcastFeedList();
  } catch (e) {
    showError('Failed to delete feed: ' + e.message);
  }
}

async function _addPodcastFeed() {
  const input = document.getElementById('podcast-add-feed-url');
  const msg = document.getElementById('podcast-add-feed-msg');
  const url = (input?.value || '').trim();
  if (!url) return;
  if (msg) msg.textContent = 'Adding…';
  try {
    const feed = await api('POST', '/api/podcast/feeds', { url });
    _podcastFeeds.push({ ...feed, episode_count: 0 });
    _renderPodcastFeedList();
  } catch (e) {
    if (msg) msg.textContent = 'Error: ' + e.message;
  }
}

async function _savePodcastDetailLevel(value) {
  const msg = document.getElementById('podcast-detail-save-msg');
  try {
    const res = await api('PUT', '/api/podcast/config', { detail_level: value });
    _podcastConfig = res || _podcastConfig;
    if (msg) msg.textContent = '✓ Saved';
  } catch (e) {
    if (msg) msg.textContent = 'Error: ' + e.message;
  }
}

// ── Screen: one feed's episodes ─────────────────────────────────────────────
// Kept as its own screen (rather than folded into a feed filter on the unified
// list) because it owns "Load more": paging a feed's back catalog is a
// per-feed action with no meaning in a mixed list.

async function openPodcastFeed(feedId) {
  navPush(`knowledge:feed:${feedId}`);
  _clearPodcastPoll();
  _podcastCurrentFeedId = feedId;
  _knowledgeScreen = 'feed';
  setLoading('Loading episodes…');
  try {
    // Published order here, not the unified list's processed order: inside one
    // podcast, "which episode is this" is a question about the show's own
    // timeline.
    const [episodes, feeds] = await Promise.all([
      api('GET', `/api/podcast/episodes?feed_id=${feedId}&limit=1000&sort=published_at&order=desc`),
      _podcastFeeds.length ? Promise.resolve(_podcastFeeds) : api('GET', '/api/podcast/feeds'),
    ]);
    _podcastEpisodes = episodes || [];
    _podcastFeeds = feeds || [];
    showView('knowledge');
    _renderPodcastEpisodeList();
    _schedulePodcastPollIfNeeded();
  } catch (e) {
    showError('Episodes failed: ' + e.message);
    openKnowledge();
  }
}

function _formatPodcastDuration(seconds) {
  if (!seconds) return '';
  return `${Math.round(seconds / 60)} min`;
}

// 把 ISO 时间字符串转成柏林时区的 YYYY-MM-DD（#532：修正单集日期时区偏差）
function _localDate(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleDateString('sv-SE', { timeZone: 'Europe/Berlin' }); }
  catch (e) { return String(iso).slice(0, 10); }
}

function _renderPodcastEpisodeList() {
  const el = document.getElementById('view-knowledge-content');
  if (!el) return;
  const feed = _podcastFeeds.find(f => f.id === _podcastCurrentFeedId);
  const rows = _podcastEpisodes.map(ep => {
    const date = _localDate(ep.published_at || ep.created_at || '');
    const status = ep.status || 'pending';
    const label = PODCAST_STATUS_LABEL[status] || status;
    const cls = PODCAST_STATUS_CLASS[status] || 'podcast-badge-muted';
    const clickable = status === 'summarized';
    const duration = _formatPodcastDuration(ep.duration_seconds);
    const transcribable = ['pending', 'no_transcript', 'error'].includes(status);
    const transcribeBtn = transcribable
      ? `<button class="btn-secondary podcast-transcribe-btn" onclick="event.stopPropagation(); _transcribePodcastEpisode(${ep.id})">Transcribe</button>`
      : '';
    return `<div class="podcast-row${clickable ? ' podcast-row-clickable' : ''}"
                 ${clickable ? `onclick="openKnowledgeItem(${ep.id})"` : ''}>
      <span class="podcast-row-title">${_escHtml(ep.title || '(untitled)')}</span>
      <span class="podcast-row-meta">
        <span class="podcast-row-date">${date}</span>
        ${duration ? `<span class="podcast-row-date">${duration}</span>` : ''}
        <span class="podcast-badge ${cls}">${label}</span>
        ${transcribeBtn}
      </span>
    </div>`;
  }).join('') || '<div class="keymap-hint">No episodes yet.</div>';

  el.innerHTML = `
    <button class="keymap-reset-all" onclick="openKnowledgeFeeds()">← Feeds</button>
    <div class="keymap-panel">
      <h2 class="keymap-heading">${_escHtml(feed?.title || feed?.url || 'Feed')}</h2>
    </div>
    <div class="podcast-list">${rows}</div>
    <div class="podcast-load-more-row">
      <button class="btn-secondary" id="podcast-load-more-btn" onclick="_loadMorePodcastEpisodes()">Load more</button>
      <span id="podcast-load-more-msg" style="font-size:12px;color:var(--muted)"></span>
    </div>`;
}

async function _loadMorePodcastEpisodes() {
  if (_podcastCurrentFeedId == null) return;
  const btn = document.getElementById('podcast-load-more-btn');
  const msg = document.getElementById('podcast-load-more-msg');
  if (btn) btn.disabled = true;
  if (msg) msg.textContent = 'Loading…';
  try {
    const res = await api('POST', `/api/podcast/feeds/${_podcastCurrentFeedId}/load-more`);
    const episodes = await api('GET', `/api/podcast/episodes?feed_id=${_podcastCurrentFeedId}&limit=1000&sort=published_at&order=desc`);
    _podcastEpisodes = episodes || [];
    _renderPodcastEpisodeList();
    const added = res?.added || 0;
    const laterMsg = document.getElementById('podcast-load-more-msg');
    if (laterMsg) laterMsg.textContent = added ? `+${added} episode${added === 1 ? '' : 's'}` : 'No older episodes.';
  } catch (e) {
    if (msg) msg.textContent = 'Error: ' + e.message;
    if (btn) btn.disabled = false;
  }
}

async function _transcribePodcastEpisode(episodeId) {
  try {
    await api('POST', `/api/podcast/episodes/${episodeId}/process`);
    const ep = _podcastEpisodes.find(e => e.id === episodeId);
    if (ep) ep.status = 'processing';
    if (_knowledgeScreen === 'feed') _renderPodcastEpisodeList(); else _renderKnowledgeList();
    _schedulePodcastPollIfNeeded();
  } catch (e) {
    showError('Failed to start transcription: ' + e.message);
  }
}

// Re-poll while any item in the currently shown list is "processing" — works
// for both the unified list and one feed's episodes; _knowledgeScreen says
// which is on screen, so the poll re-fetches with that screen's own query.
function _schedulePodcastPollIfNeeded() {
  _clearPodcastPoll();
  const hasProcessing = _podcastEpisodes.some(ep => ep.status === 'processing');
  if (!hasProcessing || (_knowledgeScreen !== 'list' && _knowledgeScreen !== 'feed')) return;
  _podcastPollTimer = setTimeout(async () => {
    const screen = _knowledgeScreen;
    const feedId = _podcastCurrentFeedId;
    if (screen !== _knowledgeScreen) return;  // left this view meanwhile
    try {
      const url = screen === 'feed'
        ? `/api/podcast/episodes?feed_id=${feedId}&limit=1000&sort=published_at&order=desc`
        : `/api/podcast/episodes?${_knowledgeQuery()}`;
      const episodes = await api('GET', url);
      _podcastEpisodes = episodes || [];
      if (screen === 'feed') _renderPodcastEpisodeList(); else _renderKnowledgeList();
      _schedulePodcastPollIfNeeded();
    } catch (e) { /* transient error — next poll cycle will retry */ }
  }, 10000);
}

// Layer 3: item detail — shared by all three kinds -------------------------------

// _knowledgeDetailId (#804) tracks which episode is currently open so a
// language-tab switch (setActiveLang) can silently re-fetch it in the new
// language — see the hook at the bottom of setActiveLang. null when no
// detail view is open (the podcast list, or any other view).
let _knowledgeDetailId = null;

async function openKnowledgeItem(id) {
  navPush(`knowledge:item:${id}`);
  setLoading('Loading…');
  _knowledgeEditOpen = false;   // #937: a fresh item opens read-only
  _knowledgeView = 'summary';   // #972: and on the summary, not whichever
  _knowledgeFulltext = null;    //       view the previous item was left on
  try {
    // lang (#804): the detail endpoint returns a translated+annotated
    // rendition of the summary for non-Chinese tabs; zh's response is
    // byte-identical to before #804 either way (see routes/podcast.py).
    const ep = await api('GET', `/api/podcast/episodes/${id}?lang=${activeLang()}`);
    _clearPodcastPoll();
    _knowledgeDetailId = id;
    showView('knowledge');
    _renderKnowledgeDetail(ep);
  } catch (e) {
    showError('Failed to load: ' + e.message);
    openKnowledge();
  }
}

function closeKnowledgeDetail() {
  _knowledgeDetailId = null;
  // Leaving the item stops its audio — the player bar goes with the page, and
  // a voice reading on from a screen he left is only confusing (#993).
  _kTtsStopPlayback();
  _kTts = { key: '', chunks: [], idx: -1, playing: false, src: '' };
  // Back to wherever this item was opened from — through goBack(), so that
  // this ✕ and the browser's own back button end up on the same screen
  // (#1009). goBack() falls back to the deck list when there is no history
  // (an item opened straight from an email link into a fresh tab).
  goBack();
}

// A very short German TL;DR, derived from summary_de at render time (#971).
//
// No new column and no extra AI call: the summary prompt (#567) already
// requires summary_de to be <p> paragraphs whose FIRST sentence is wrapped in
// <b> and summarises that paragraph. Stringing those lead sentences together
// is the TL;DR, and it works retroactively on every item already in the
// database instead of only on ones summarised from now on.
//
// Pre-#708 summaries are plain text with no <b> at all — those fall back to
// the first two sentences of the text. Returns '' when neither yields
// anything, so the caller can drop the block entirely.
function _knowledgeTldrDe(summaryDe) {
  const raw = (summaryDe || '').trim();
  if (!raw) return '';
  // Parsed, never injected: the result is escaped again before it reaches the
  // page, same rule as _summaryZhHtml — AI-written text never carries markup.
  const doc = new DOMParser().parseFromString(`<div>${raw}</div>`, 'text/html');
  // Only the <b> that OPENS a paragraph counts. The same prompt also uses
  // <strong> for mid-paragraph highlights, and a plain querySelectorAll would
  // splice those loose fragments into the TL;DR.
  const leads = Array.from(doc.querySelectorAll('p'))
    .map(par => par.firstElementChild)
    .filter(el => el && /^(B|STRONG)$/.test(el.tagName)
                 && !(el.previousSibling && el.previousSibling.textContent.trim()))
    .map(el => (el.textContent || '').trim())
    .filter(Boolean);
  if (leads.length) return leads.join(' ');
  const plain = (doc.body.textContent || '').replace(/\s+/g, ' ').trim();
  if (!plain) return '';
  const sentences = plain.match(/[^.!?]+[.!?]+/g);
  return sentences ? sentences.slice(0, 2).join(' ').trim() : plain.slice(0, 300);
}

// Open by default: a TL;DR nobody sees is pointless. The toggle is remembered
// so Daniel can fold it away for good once he stops wanting it.
function _knowledgeTldrHtml(ep) {
  const text = _knowledgeTldrDe(ep.summary_de);
  if (!text) return '';
  const open = localStorage.getItem('knowledgeTldrOpen') === '0' ? '' : ' open';
  return `<details class="knowledge-tldr" onclick="setTimeout(_rememberKnowledgeTldr, 0)"${open}>
      <summary>Kurzfassung</summary>
      <p>${_escHtml(text)}</p>
    </details>`;
}

function _rememberKnowledgeTldr() {
  const el = document.querySelector('.knowledge-tldr');
  if (el) localStorage.setItem('knowledgeTldrOpen', el.open ? '1' : '0');
}

// The summary block of a knowledge item. Shared (#929) by the detail view below
// and the popup the story loading screen opens — a second copy would drift the
// moment one of the two language branches changes.
//
// #804: zh's summary block is untouched. Every other language shows its
// rendition (translated from summary_de, new words annotated inline); a
// failed/not-yet-generated rendition shows the reason rather than silently
// falling back to a German block Daniel didn't ask to read.
function _knowledgeSummaryHtml(ep) {
  if (activeLang() === 'zh') {
    return `${_knowledgeTldrHtml(ep)}
       ${ep.summary_zh ? `<div id="podcast-summary-zh">${_summaryZhHtml(ep.summary_zh)}</div>` : ''}
       <div id="podcast-summary-de">${ep.summary_de || ''}</div>`;
  }
  // The TL;DR stays German in every language (#971): the block below is the
  // rendition Daniel reads for practice, this is the gist he reads to decide.
  return _knowledgeTldrHtml(ep) + (ep.rendition
    // Same whitelist sanitizer the zh summary uses: the rendition text
    // passed through Google Translate and the annotator, so it gets
    // escaped and only <p>/<b>/<em>/<i>/<br> are let back through.
    ? `<div id="podcast-summary-rendition">${_summaryZhHtml(ep.rendition.summary || '')}</div>`
    : `<p class="keymap-hint">${_escHtml(ep.rendition_error || 'Rendition unavailable.')}</p>`);
}

// ── Summary / Full text (#972) ─────────────────────────────────────────────
// Two views of the same item: the AI summary, and the untruncated source
// text translated into the reading language and annotated by exactly the
// same pipeline (knowledge/rendition.render_html).
//
// Per episode+language, because it is a property of what is on screen: the
// full text of a French reading of item 12 is not the one of its Chinese
// reading, and switching languages must not show the other one's text.
let _knowledgeView = 'summary';        // 'summary' | 'fulltext'
let _knowledgeFulltext = null;         // {episode_id, lang, text, new_words} | null
let _knowledgeFulltextBusy = false;

// A record with text === null means "asked, there is none yet" — without it
// every re-render (tab switch, word marked known) would fire the GET again.
function _knowledgeFulltextChecked(ep, lang) {
  const ft = _knowledgeFulltext;
  return !!(ft && ft.episode_id === ep.id && ft.lang === lang);
}

function _knowledgeFulltextFor(ep, lang) {
  const ft = _knowledgeFulltext;
  return _knowledgeFulltextChecked(ep, lang) && ft.text ? ft : null;
}

function _knowledgeViewTabs(ep) {
  return `
    <div class="mailbox-tabs" style="margin:10px 0 12px">
      <button class="mailbox-tab${_knowledgeView === 'summary' ? ' active' : ''}"
              onclick="switchKnowledgeView('summary')">Summary</button>
      <button class="mailbox-tab${_knowledgeView === 'fulltext' ? ' active' : ''}"
              onclick="switchKnowledgeView('fulltext')">Full text</button>
    </div>`;
}

function switchKnowledgeView(view) {
  if (_knowledgeView === view) return;
  _knowledgeView = view;
  if (_knowledgeDetailEpisode) _renderKnowledgeDetail(_knowledgeDetailEpisode);
}

function _knowledgeFulltextHtml(ep, lang) {
  if (_knowledgeFulltextBusy) {
    return `<p class="keymap-hint">Translating and annotating the full text — this takes a moment for long material…</p>`;
  }
  const ft = _knowledgeFulltextFor(ep, lang);
  if (ft) {
    // Same whitelist sanitizer as the summary blocks: this text went
    // through Google Translate and the annotator, so it is escaped and only
    // <p>/<b>/<em>/<i>/<br> come back through.
    return `<div id="knowledge-fulltext">${_summaryZhHtml(ft.text || '')}</div>`;
  }
  // Never generated silently on open: for anything but a newsletter this is
  // a whole transcript to translate, and most items Daniel only reads the
  // summary of.
  return `
    <p class="keymap-hint">The full text has not been generated for this language yet.</p>
    <button class="btn-secondary" onclick="doGenerateFulltext()">Generate full text</button>`;
}

async function _loadKnowledgeFulltext(episodeId, lang) {
  try {
    const data = await api('GET', `/api/podcast/episodes/${episodeId}/fulltext?lang=${encodeURIComponent(lang)}`);
    _knowledgeFulltext = data.status === 'ready'
      ? { episode_id: episodeId, lang, text: data.text, new_words: data.new_words || [] }
      : { episode_id: episodeId, lang, text: null, new_words: [] };
    if (data.status === 'ready' && _knowledgeView === 'fulltext' && _knowledgeDetailEpisode &&
        _knowledgeDetailEpisode.id === episodeId) _renderKnowledgeDetail(_knowledgeDetailEpisode);
  } catch (e) {
    // A missing full text is not an error worth interrupting the page for —
    // the tab shows the Generate button instead.
  }
}

async function doGenerateFulltext() {
  const ep = _knowledgeDetailEpisode;
  if (!ep || _knowledgeFulltextBusy) return;
  const lang = activeLang();
  _knowledgeFulltextBusy = true;
  _renderKnowledgeDetail(ep);
  try {
    const data = await api('POST', `/api/podcast/episodes/${ep.id}/fulltext?lang=${encodeURIComponent(lang)}`);
    _knowledgeFulltext = { episode_id: ep.id, lang, text: data.text, new_words: data.new_words || [] };
  } catch (e) {
    showError('Could not generate the full text: ' + (e.message || 'error'));
  } finally {
    _knowledgeFulltextBusy = false;
    _renderKnowledgeDetail(ep);
  }
}

// ── 摘要朗读 (#993) ─────────────────────────────────────────────────────────
// Listen to the summary (or the full text) in the reading language. Two rules
// from Daniel: the server never generates audio on its own — only pressing ▶
// does, so opening an item costs nothing — and the speed must be adjustable.
//
// Speed is the browser's playbackRate, not edge-tts's `rate`: the latter would
// mint a separate mp3 per speed for the very same sentence (the cache is keyed
// on the text) and changing speed would mean regenerating everything.
const KNOWLEDGE_TTS_RATES = [0.75, 1, 1.25, 1.5, 1.75, 2];
const _KTTS_CHUNK_MAX = 180;   // chars per request — one edge-tts round trip
// Two reading modes (#1017). 'plain' is the original behaviour; 'gloss' reads
// the German definition of every word Daniel does not know yet, immediately
// followed by the word itself, and then carries on with the sentence.
const KNOWLEDGE_TTS_MODES = { plain: 'Plain', gloss: '+ Vokabeln' };
// A chunk is now {text, lang}: the gloss parts are German, everything else is
// the reading language. Plain mode simply tags every chunk with that language.
let _kTts = { key: '', chunks: [], idx: -1, playing: false, src: '' };
let _kTtsRate = (() => {
  const v = parseFloat(localStorage.getItem('knowledgeTtsRate'));
  return KNOWLEDGE_TTS_RATES.includes(v) ? v : 1;
})();
let _kTtsMode = (() => {
  const v = localStorage.getItem('knowledgeTtsMode');
  return KNOWLEDGE_TTS_MODES[v] ? v : 'plain';
})();

// Inline annotations are for the eye, not the ear: "生态（shēngtài - Ökologie）"
// read aloud by a Chinese voice is unlistenable, and the romance annotator's
// " (Gloss)" is German dropped into a French sentence. Both are parenthesised,
// so both go. Losing a genuine parenthetical from the source is a fair price.
function _kTtsStripAnnotations(text, lang) {
  let out = String(text || '').replace(/（[^（）]*）/g, '');
  if (lang !== 'zh') out = out.replace(/\([^()]*\)/g, '');
  return out.replace(/[ \t]{2,}/g, ' ');
}

// Sentence boundaries, scanned by hand rather than with a lookbehind regex —
// the terminator set differs per script (。！？ vs. a period that is only a
// terminator when whitespace follows, so "12.5" and "z. B." stay whole).
function _kTtsSentences(para) {
  const TERM = '。！？…；;!?';
  const out = [];
  let buf = '';
  for (let i = 0; i < para.length; i++) {
    buf += para[i];
    const next = para[i + 1] || '';
    // A period ends a sentence only with whitespace after it AND at least two
    // letters/digits before it — "z. B." and "M. Dupont" would otherwise be
    // torn into their own chunks.
    const isTerm = TERM.includes(para[i]) ||
      (para[i] === '.' && (!next || /\s/.test(next)) && /[\p{L}\p{N}]{2}$/u.test(buf.slice(0, -1)));
    if (!isTerm || TERM.includes(next) || next === '.') continue;
    // Pull in the closing quote/bracket and the space that belong to this sentence.
    while (i + 1 < para.length && /["'\u201d\u2019)）\s]/.test(para[i + 1])) buf += para[++i];
    out.push(buf);
    buf = '';
  }
  if (buf) out.push(buf);
  return out;
}

// Split into chunks of at most _KTTS_CHUNK_MAX chars, at sentence boundaries.
// A single oversized sentence is cut at its last space — better a seam
// mid-sentence than a minute-long edge-tts request waited out in silence.
function _kTtsChunks(text, lang) {
  const out = [];
  for (const para of _kTtsStripAnnotations(text, lang).split(/\n+/)) {
    let buf = '';
    for (let s of _kTtsSentences(para)) {
      while (s.length > _KTTS_CHUNK_MAX) {
        if (buf) { out.push(buf); buf = ''; }
        const head = s.slice(0, _KTTS_CHUNK_MAX);
        const cut = head.lastIndexOf(' ') > _KTTS_CHUNK_MAX / 2 ? head.lastIndexOf(' ') : _KTTS_CHUNK_MAX;
        out.push(s.slice(0, cut));
        s = s.slice(cut);
      }
      if (buf && (buf + s).length > _KTTS_CHUNK_MAX) { out.push(buf); buf = ''; }
      buf += s;
    }
    if (buf) out.push(buf);
  }
  return out.map(c => c.trim()).filter(c => /[\p{L}\p{N}]/u.test(c));
}

// Every occurrence of `word` in `text`, as start offsets. Chinese has no word
// boundaries so a plain substring match is right; for the romance languages it
// would match "chat" inside "château", so the neighbouring characters must not
// be letters or digits. Matching is case-insensitive there (a word at the start
// of a sentence is capitalised, the vocabulary list is not).
function _kTtsIsWordChar(ch) {
  return !!ch && /[\p{L}\p{N}]/u.test(ch);
}

function _kTtsWordHits(text, word, lang) {
  const hits = [];
  const cjk = lang === 'zh';
  const hay = cjk ? text : text.toLowerCase();
  const needle = cjk ? word : word.toLowerCase();
  if (!needle) return hits;
  let i = 0;
  while ((i = hay.indexOf(needle, i)) !== -1) {
    if (cjk || (!_kTtsIsWordChar(hay[i - 1]) && !_kTtsIsWordChar(hay[i + needle.length])))
      hits.push(i);
    i += needle.length;
  }
  return hits;
}

// The definition column is written for the eye: "der Fluss; Strom (geogr.)".
// Read aloud, the semicolon list and the abbreviated register note are noise,
// so only the first sense is spoken and parentheticals are dropped.
function _kTtsGlossText(gloss) {
  return String(gloss || '')
    .replace(/[(（][^()（）]*[)）]/g, ' ')
    .split(/[;；]/)[0]
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 80);
}

// Split one chunk around the words Daniel does not know yet: everything up to
// the word in the reading language, the German gloss, then the word itself,
// then on with the rest. `used` is shared across the whole text, so a word is
// glossed at its first occurrence only — hearing it on every repetition would
// be unbearable.
function _kTtsGlossParts(text, lang, words, used) {
  const marks = [];
  for (const w of words) {
    const word = String(w.word || w.word_zh || '').trim();
    const gloss = _kTtsGlossText(w.definition_de || w.definition || '');
    if (!word || !gloss || used.has(word)) continue;
    const hits = _kTtsWordHits(text, word, lang);
    if (hits.length) marks.push({ pos: hits[0], word, gloss });
  }
  marks.sort((a, b) => a.pos - b.pos || b.word.length - a.word.length);
  const parts = [];
  let cursor = 0;
  for (const m of marks) {
    // A shorter word starting inside one already glossed ("模型" inside
    // "大语言模型") would tear the longer one apart — skip it, and leave it
    // unmarked in `used` so it can still be glossed where it stands alone.
    if (m.pos < cursor) continue;
    used.add(m.word);
    const head = text.slice(cursor, m.pos);
    if (head.trim()) parts.push({ text: head, lang });
    parts.push({ text: m.gloss, lang: 'de' });
    parts.push({ text: text.substr(m.pos, m.word.length), lang });
    cursor = m.pos + m.word.length;
  }
  const tail = text.slice(cursor);
  if (tail.trim()) parts.push({ text: tail, lang });
  return parts;
}

// The vocabulary of whatever is on screen — the same list the word table
// below the text is built from (the full text and the summary have different
// vocabulary, and offering one while reading the other would be two texts'
// words side by side).
function _kTtsNewWords(ep, lang) {
  if (_knowledgeView === 'fulltext') {
    const ft = _knowledgeFulltextFor(ep, lang);
    return (ft && ft.new_words) || [];
  }
  if (lang === 'zh') return ep.hsk_words || [];
  return (ep.rendition && ep.rendition.new_words) || [];
}

// What is on screen right now — the reading-language block, or the full text
// when that tab is open. The German summary_de block is deliberately not in
// here: there is no German voice in the language registry, and that block is
// the gist Daniel reads to decide, not the material he studies.
function _kTtsSourceText(ep, lang) {
  if (_knowledgeView === 'fulltext') {
    const ft = _knowledgeFulltextFor(ep, lang);
    return ft ? _htmlToPlainText(_summaryZhHtml(ft.text || '')) : '';
  }
  if (lang === 'zh') return _htmlToPlainText(_summaryZhHtml(ep.summary_zh || ''));
  return ep.rendition ? _htmlToPlainText(_summaryZhHtml(ep.rendition.summary || '')) : '';
}

// Episode + language + view identify the audio: switching any of them means
// the bar is now pointing at different words, so playback resets.
function _kTtsSync(ep, lang) {
  const key = `${ep.id}|${lang}|${_knowledgeView}|${_kTtsMode}`;
  if (_kTts.key === key) return _kTts.chunks;
  if (_kTts.playing || _kTts.idx >= 0) _kTtsStopPlayback();
  const texts = _kTtsChunks(_kTtsSourceText(ep, lang), lang);
  let chunks;
  if (_kTtsMode === 'gloss') {
    const words = _kTtsNewWords(ep, lang);
    const used = new Set();
    chunks = texts.flatMap(t => _kTtsGlossParts(t, lang, words, used));
  } else {
    chunks = texts.map(text => ({ text, lang }));
  }
  _kTts = { key, chunks, idx: -1, playing: false, src: '' };
  return _kTts.chunks;
}

function _kTtsBarHtml(ep, lang) {
  const chunks = _kTtsSync(ep, lang);
  if (!chunks.length) return '';
  const pos = _kTts.idx >= 0 ? `${_kTts.idx + 1} / ${chunks.length}` : `${chunks.length} parts`;
  return `
    <div class="knowledge-tts-bar">
      <button class="btn-secondary" id="knowledge-tts-toggle" onclick="toggleKnowledgeTts()">${
        _kTts.playing ? '⏸ Pause' : (_kTts.idx >= 0 ? '▶ Resume' : '🔊 Listen')}</button>
      ${_kTts.idx >= 0 ? `<button class="btn-secondary" onclick="stopKnowledgeTts()">■</button>` : ''}
      <span class="keymap-hint" id="knowledge-tts-pos">${pos}</span>
      <select class="knowledge-tts-rate" onchange="setKnowledgeTtsMode(this.value)" title="Reading mode">
        ${Object.entries(KNOWLEDGE_TTS_MODES).map(([v, label]) =>
          `<option value="${v}"${v === _kTtsMode ? ' selected' : ''}>${label}</option>`).join('')}
      </select>
      <select class="knowledge-tts-rate" onchange="setKnowledgeTtsRate(this.value)" title="Playback speed">
        ${KNOWLEDGE_TTS_RATES.map(r =>
          `<option value="${r}"${r === _kTtsRate ? ' selected' : ''}>${r}×</option>`).join('')}
      </select>
    </div>`;
}

// Repaint the bar in place — a full _renderKnowledgeDetail() on every chunk
// would rebuild the whole detail view (and scroll position) six times a minute.
function _kTtsUpdateBar() {
  const btn = document.getElementById('knowledge-tts-toggle');
  const pos = document.getElementById('knowledge-tts-pos');
  if (btn) btn.textContent = _kTts.playing ? '⏸ Pause' : (_kTts.idx >= 0 ? '▶ Resume' : '🔊 Listen');
  if (pos) pos.textContent = _kTts.idx >= 0 ? `${_kTts.idx + 1} / ${_kTts.chunks.length}`
                                            : `${_kTts.chunks.length} parts`;
}

function _kTtsPlayAt(idx) {
  if (idx < 0 || idx >= _kTts.chunks.length) { stopKnowledgeTts(); return; }
  _kTts.idx = idx;
  _kTts.playing = true;
  const seq = ++_playSeq;
  const a = _getAudioEl();
  a.onended = () => { if (seq === _playSeq) _kTtsPlayAt(idx + 1); };
  a.onerror = () => { if (seq === _playSeq) _kTtsPlayAt(idx + 1); };
  _kTts.src = _ttsUrl(_kTts.chunks[idx].text, _kTts.chunks[idx].lang);
  a.src = _kTts.src;
  a.playbackRate = _kTtsRate;
  a.play().catch(() => { if (seq === _playSeq) _kTtsPlayAt(idx + 1); });
  // The only audio generated ahead of time: the one chunk that plays next,
  // fetched while this one sounds. Without it every seam is a silent wait for
  // edge-tts. Nothing is warmed before ▶ is pressed.
  if (idx + 1 < _kTts.chunks.length)
    _warmAudio(_ttsUrl(_kTts.chunks[idx + 1].text, _kTts.chunks[idx + 1].lang));
  _kTtsUpdateBar();
}

function _kTtsStopPlayback() {
  _stopSharedPlayback();
  _kTts.playing = false;
}

function toggleKnowledgeTts() {
  if (!_kTts.chunks.length) return;
  const a = _sharedAudio;
  if (_kTts.playing) {
    // A real pause, not a stop: the onended chain stays armed, so resuming
    // continues the chunk where it stopped instead of replaying it.
    _kTts.playing = false;
    try { a && a.pause(); } catch (_) {}
    _kTtsUpdateBar();
    return;
  }
  // Resume only if the shared element is still holding our chunk — anything
  // else (a word tapped in the review view) has taken it over since.
  if (_kTts.idx >= 0 && a && _kTts.src && a.src === new URL(_kTts.src, location.href).href) {
    _kTts.playing = true;
    a.playbackRate = _kTtsRate;
    a.play().catch(() => {});
    _kTtsUpdateBar();
    return;
  }
  _kTtsPlayAt(_kTts.idx >= 0 ? _kTts.idx : 0);
  if (_knowledgeDetailEpisode) _renderKnowledgeDetail(_knowledgeDetailEpisode);
}

function stopKnowledgeTts() {
  _kTtsStopPlayback();
  _kTts.idx = -1;
  if (_knowledgeDetailEpisode) _renderKnowledgeDetail(_knowledgeDetailEpisode);
}

// Switching mode changes what the parts are, so playback resets — resuming at
// part 12 of a list that no longer exists would land in the middle of a
// different sentence.
function setKnowledgeTtsMode(value) {
  if (!KNOWLEDGE_TTS_MODES[value] || value === _kTtsMode) return;
  _kTtsStopPlayback();
  _kTts = { key: '', chunks: [], idx: -1, playing: false, src: '' };
  _kTtsMode = value;
  try { localStorage.setItem('knowledgeTtsMode', value); } catch (_) {}
  if (_knowledgeDetailEpisode) _renderKnowledgeDetail(_knowledgeDetailEpisode);
}

function setKnowledgeTtsRate(value) {
  const rate = parseFloat(value);
  if (!KNOWLEDGE_TTS_RATES.includes(rate)) return;
  _kTtsRate = rate;
  try { localStorage.setItem('knowledgeTtsRate', String(rate)); } catch (_) {}
  if (_sharedAudio) _sharedAudio.playbackRate = rate;   // takes effect mid-chunk
}

function _renderKnowledgeDetail(ep) {
  const el = document.getElementById('view-knowledge-content');
  if (!el) return;
  const kind = ep.kind || 'podcast';
  const isPodcast = kind === 'podcast';
  const contentLabel = kind === 'video' ? 'Subtitles' : kind === 'article' ? 'Article text' :
    kind === 'newsletter' ? 'Newsletter text' : 'Transcript';
  const date = _localDate(ep.published_at || ep.created_at || '');
  // lang (#804): zh reads hsk_words/summary_zh/summary_de exactly as before
  // #804 — not one byte of that path changes. Every other language reads
  // its lazily-generated rendition instead (routes/podcast.py attaches it
  // to the response as ep.rendition / ep.rendition_error).
  const lang = activeLang();
  const isZh = lang === 'zh';
  // Keep the raw word objects around so click handlers can look them up by
  // index instead of serializing them into onclick attributes (avoids
  // quote/apostrophe escaping issues in word_zh/definition_de text).
  // The word list belongs to whatever is on screen: reading the full text
  // and being offered the summary's words would be two different texts'
  // vocabulary side by side.
  const _ft = _knowledgeFulltextFor(ep, lang);
  setWordTable(
    _knowledgeView === 'fulltext' ? ((_ft && _ft.new_words) || [])
      : isZh ? (ep.hsk_words || [])
             : ((ep.rendition && ep.rendition.new_words) || []), lang);
  _podcastDetailEpisodeId = ep.id;
  _knowledgeDetailEpisode = ep;
  const hskTable = wordTableHtml('No HSK vocabulary extracted.');
  const links = [
    ep.youtube_url ? `<a href="${_escHtml(ep.youtube_url)}" target="_blank" rel="noopener" class="btn-secondary">${isPodcast ? 'YouTube' : 'Open'} ↗</a>` : '',
    isPodcast && ep.spotify_url ? `<a href="${_escHtml(ep.spotify_url)}" target="_blank" rel="noopener" class="btn-secondary">Spotify ↗</a>` : '',
    ep.status === 'summarized' ? `<button id="podcast-notify-signal" class="btn-secondary" onclick="doPodcastNotify('signal')">Send to Signal</button>` : '',
    ep.status === 'summarized' ? `<button id="podcast-notify-email" class="btn-secondary" onclick="doPodcastNotify('email')">Send Email</button>` : '',
    ep.status === 'summarized' ? `<button id="podcast-regen-summary" class="btn-secondary" onclick="doPodcastRegenerateSummary()">Regenerate summary</button>` : '',
    ep.status === 'summarized' ? `<button id="knowledge-retag" class="btn-secondary" onclick="doKnowledgeRetag()">↻ Retag</button>` : '',
    _knowledgeSummaryText(ep) ? `<button id="knowledge-copy-summary" class="btn-secondary" onclick="doKnowledgeCopySummary(this)">\u{1F4CB} Copy summary</button>` : '',
    ep.status === 'processing' ? `<span class="keymap-hint">⏳ processing…</span>` : '',
  ].filter(Boolean).join(' ');
  const trPairs = Array.isArray(ep.transcript_de) ? ep.transcript_de : [];
  const trBody = trPairs.length
    ? trPairs.map(p => `<div class="podcast-tr-pair">
         <div class="podcast-tr-zh">${_escHtml(p.zh || '')}</div>
         <div class="podcast-tr-de">${_escHtml(p.de || '')}</div>
       </div>`).join('')
    : (ep.transcript_zh ? _escHtml(ep.transcript_zh).replace(/\n/g, '<br>') : '');
  const transcript = (trPairs.length || ep.transcript_zh)
    ? `<div class="podcast-transcript-wrap">
         <button class="keymap-reset-all" onclick="_togglePodcastTranscript()">Show/hide ${contentLabel.toLowerCase()}</button>
         <button class="keymap-reset-all" style="margin-left:8px" id="knowledge-copy-transcript" onclick="doKnowledgeCopyTranscript(this)">\u{1F4CB} Copy ${contentLabel.toLowerCase()}</button>
         <div id="podcast-transcript-body" class="podcast-transcript" style="display:none">${trBody}</div>
       </div>`
    : '';
  const summaryBlock = _knowledgeSummaryHtml(ep);

  el.innerHTML = `
    <button class="keymap-reset-all" onclick="closeKnowledgeDetail()">← Back</button>
    <div class="keymap-panel">
      <div class="knowledge-header">
        <h2 class="keymap-heading" style="margin:0">${_escHtml(ep.title || '(untitled)')}</h2>
        <span style="flex:1"></span>
        <button class="btn-secondary" onclick="toggleKnowledgeEdit()">${_knowledgeEditOpen ? '✕ Close' : '✎ Edit'}</button>
      </div>
      <p class="keymap-hint">${[_escHtml(ep.author || ''), _escHtml(_knowledgePlatformLabel(ep)), date].filter(Boolean).join(' · ')}</p>
      ${_knowledgeTagRowHtml(ep)}
      ${_knowledgeEditOpen ? _knowledgeEditFormHtml(ep) : ''}
      <div style="margin:4px 0 10px">${links}</div>
      ${_knowledgeViewTabs(ep)}
      ${_kTtsBarHtml(ep, lang)}
      ${_knowledgeView === 'fulltext' ? _knowledgeFulltextHtml(ep, lang) : summaryBlock}
    </div>
    <div class="keymap-panel">
      <h2 class="keymap-heading">${isZh ? 'HSK vocabulary' : 'New words'}</h2>
      ${hskTable}
    </div>
    <div class="keymap-panel">
      <h2 class="keymap-heading">${contentLabel}</h2>
      ${transcript || `<p class="keymap-hint">No ${contentLabel.toLowerCase()} available.</p>`}
    </div>
    ${_knowledgeChatHtml(ep)}`;
  // #967: only the summary/rendition blocks, not the transcript below — the
  // word list was extracted from the summary, and the bilingual transcript
  // columns are for reading along, not for picking words out of.
  ['podcast-summary-zh', 'podcast-summary-de', 'podcast-summary-rendition', 'knowledge-fulltext']
    .forEach(id => _makeWordsTappable(document.getElementById(id)));
  // Ask whether a full text already exists (a newsletter's was built when it
  // was processed). GET never generates, so this is cheap and safe to fire
  // on every detail view.
  if (!_knowledgeFulltextChecked(ep, lang) && !_knowledgeFulltextBusy) _loadKnowledgeFulltext(ep.id, lang);
  // The saved conversation (#945) is fetched after the markup exists, so the
  // detail view still renders instantly when the chat request is slow.
  _loadKnowledgeChat(ep.id);
}

// ── Hand-editing an item's metadata (#937) ─────────────────────────────────
// Title/author/platform/date come from RSS, yt-dlp or an AI extraction, and
// all three get it wrong regularly. Everything saved here is recorded server
// side in manual_fields, so no later AI pass overwrites it.
let _knowledgeEditOpen = false;

function toggleKnowledgeEdit() {
  _knowledgeEditOpen = !_knowledgeEditOpen;
  if (_knowledgeDetailEpisode) _renderKnowledgeDetail(_knowledgeDetailEpisode);
}

function _knowledgePlatformLabel(ep) {
  return ep.platform ? (KNOWLEDGE_PLATFORM_LABEL[ep.platform] || ep.platform) : '';
}

function _knowledgeTagRowHtml(ep) {
  const tags = ep.tags || [];
  if (!tags.length) return '';
  return `<div class="knowledge-tag-row" style="margin:6px 0">${tags.map(t =>
    `<span class="knowledge-tag ${t.source === 'ai' ? 'knowledge-tag-ai' : ''}">${_escHtml(t.name)}</span>`
  ).join('')}</div>`;
}

function _knowledgeEditFormHtml(ep) {
  // Every platform the app knows about, plus whatever this row already has —
  // a value typed in by hand or invented by a future ingest path must not
  // silently disappear from its own dropdown.
  const platforms = [...Object.keys(KNOWLEDGE_PLATFORM_LABEL)];
  if (ep.platform && !platforms.includes(ep.platform)) platforms.push(ep.platform);
  const platformOpts = ['<option value="">—</option>'].concat(platforms.map(pl =>
    `<option value="${_escHtml(pl)}" ${ep.platform === pl ? 'selected' : ''}>${_escHtml(KNOWLEDGE_PLATFORM_LABEL[pl] || pl)}</option>`
  )).join('');
  const tagValue = (ep.tags || []).map(t => t.name).join(', ');
  return `<div class="knowledge-edit-form">
    <label class="knowledge-edit-row"><span>Title</span>
      <input type="text" class="edit-input" id="kedit-title" value="${_escHtml(ep.title || '')}"></label>
    <label class="knowledge-edit-row"><span>Author</span>
      <input type="text" class="edit-input" id="kedit-author" value="${_escHtml(ep.author || '')}"></label>
    <label class="knowledge-edit-row"><span>Platform</span>
      <select class="opt-input" id="kedit-platform">${platformOpts}</select></label>
    <label class="knowledge-edit-row"><span>Published</span>
      <input type="text" class="edit-input" id="kedit-published" placeholder="YYYY-MM-DD"
             value="${_escHtml(ep.published_at ? String(ep.published_at).slice(0, 10) : '')}"></label>
    <label class="knowledge-edit-row"><span>Link</span>
      <input type="text" class="edit-input" id="kedit-url" value="${_escHtml(ep.youtube_url || '')}"></label>
    <label class="knowledge-edit-row"><span>Tags</span>
      <input type="text" class="edit-input" id="kedit-tags" list="kedit-tag-options"
             placeholder="comma separated" value="${_escHtml(tagValue)}"></label>
    <datalist id="kedit-tag-options">${
      (_knowledgeFacets?.tags || []).map(t => `<option value="${_escHtml(t.name)}"></option>`).join('')
    }</datalist>
    <div class="knowledge-edit-row">
      <span></span>
      <span style="display:flex;gap:8px;align-items:center">
        <button class="btn-secondary" onclick="saveKnowledgeEdit()">Save</button>
        <span id="kedit-msg" style="font-size:12px;color:var(--muted)"></span>
      </span>
    </div>
    <p class="keymap-hint" style="margin:2px 0 0">Anything you change here is yours — later AI passes leave it alone. Tags saved here are never removed by re-tagging.</p>
  </div>`;
}

async function saveKnowledgeEdit() {
  const ep = _knowledgeDetailEpisode;
  if (!ep) return;
  const msg = document.getElementById('kedit-msg');
  const val = (id) => (document.getElementById(id)?.value || '').trim();
  const payload = {
    title: val('kedit-title'),
    author: val('kedit-author'),
    platform: val('kedit-platform'),
    published_at: val('kedit-published'),
    youtube_url: val('kedit-url'),
    tags: val('kedit-tags').split(',').map(t => t.trim()).filter(Boolean),
  };
  if (msg) msg.textContent = 'Saving…';
  try {
    const updated = await api('PATCH', `/api/podcast/episodes/${ep.id}`, payload);
    // Re-fetch rather than trusting the PATCH response: the detail view also
    // needs the per-language rendition, which the PATCH endpoint knows nothing
    // about (it would come back missing and the summary would blank out).
    _knowledgeEditOpen = false;
    // The facet lists just changed (a new author, a new tag), so drop the
    // cached copy — the filter bar rebuilds from the server on next open.
    _knowledgeFacets = null;
    await openKnowledgeItem(updated?.id || ep.id);
  } catch (e) {
    if (msg) msg.textContent = 'Error: ' + e.message;
  }
}

function _togglePodcastTranscript() {
  const body = document.getElementById('podcast-transcript-body');
  if (!body) return;
  body.style.display = body.style.display === 'none' ? 'block' : 'none';
}

// ── One-click copy of a knowledge item's summary / transcript (#943) ─────────
// The summary is <p>/<b> markup and the transcript is a two-column bilingual
// layout: selecting either by hand is painful, so both get a button that puts
// plain text on the clipboard.

// Plain text out of the summary markup. Everything is pushed through
// _summaryZhHtml first (escape, then whitelist <p>/<b>/<em>/<i>/<br>), so the
// detached div below can never be handed model-written markup verbatim.
function _htmlToPlainText(html) {
  const div = document.createElement('div');
  div.innerHTML = String(html || '').replace(/<br\s*\/?>/gi, '\n');
  const paras = div.querySelectorAll('p');
  if (paras.length) {
    return Array.from(paras).map(p => p.textContent.trim()).filter(Boolean).join('\n\n');
  }
  return div.textContent.trim();
}

// Mirrors _knowledgeSummaryHtml's language split: zh copies both the Chinese
// and the German summary, every other language copies its rendition.
function _knowledgeSummaryText(ep) {
  if (!ep) return '';
  if (activeLang() === 'zh') {
    return [ep.summary_zh, ep.summary_de]
      .map(t => _htmlToPlainText(_summaryZhHtml(t)))
      .filter(Boolean).join('\n\n');
  }
  return ep.rendition ? _htmlToPlainText(_summaryZhHtml(ep.rendition.summary || '')) : '';
}

// Bilingual transcripts (#772) copy as "<Chinese line>\n<other line>" per
// segment; single-language transcripts copy as they are.
function _knowledgeTranscriptText(ep) {
  if (!ep) return '';
  const pairs = Array.isArray(ep.transcript_de) ? ep.transcript_de : [];
  if (pairs.length) {
    return pairs.map(p => [p.zh || '', p.de || ''].map(t => String(t).trim()).filter(Boolean).join('\n'))
                .filter(Boolean).join('\n\n');
  }
  return String(ep.transcript_zh || '').trim();
}

// navigator.clipboard only exists in a secure context — the local/offline
// instance runs on plain http, so keep the old execCommand path as a fallback
// rather than letting the button do nothing there.
function _copyTextFallback(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  document.body.removeChild(ta);
  return ok;
}

async function _copyToClipboard(text, btn) {
  if (!text) { showError('Nothing to copy'); return; }
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (e) { ok = false; }
  if (!ok) ok = _copyTextFallback(text);
  if (!ok) { showError('Could not copy to clipboard'); return; }
  if (!btn) return;
  const original = btn.textContent;
  btn.textContent = '\u2713 Copied';
  setTimeout(() => { btn.textContent = original; }, 1500);
}

function doKnowledgeCopySummary(btn) {
  _copyToClipboard(_knowledgeSummaryText(_knowledgeDetailEpisode), btn);
}

function doKnowledgeCopyTranscript(btn) {
  _copyToClipboard(_knowledgeTranscriptText(_knowledgeDetailEpisode), btn);
}

// Episode id for the currently rendered podcast detail — used by
// doPodcastNotify (#530) so the Send to Signal/Email buttons don't need to
// embed the id in their onclick attribute.
let _podcastDetailEpisodeId = null;

// The full episode object behind the detail view (#943) — the copy buttons
// need summary/transcript text, not just the id.
let _knowledgeDetailEpisode = null;

// Regenerate the summary of the currently shown episode (#567): POST kicks
// off a background thread on the server, then poll the detail endpoint until
// the overlaid status leaves 'processing' and re-render with the new summary.
// A NotebookLM round can take ~10 min, so polling is deliberately patient.
let _podcastRegenTimer = null;

// ↻ Retag (#938): replace this item's machine-guessed tags. Synchronous — one
// cheap call over the summary, not a transcription. Tags Daniel typed himself
// are untouched (the server only replaces source='ai' rows).
async function doKnowledgeRetag() {
  const btn = document.getElementById('knowledge-retag');
  const id = _podcastDetailEpisodeId;
  if (id == null) return;
  if (btn) { btn.disabled = true; btn.textContent = '↻ Tagging…'; }
  try {
    await api('POST', `/api/podcast/episodes/${id}/retag`);
    _knowledgeFacets = null;   // new tags may have entered the vocabulary
    await openKnowledgeItem(id);
  } catch (e) {
    showError('Retag failed: ' + e.message);
    if (btn) { btn.disabled = false; btn.textContent = '↻ Retag'; }
  }
}

async function doPodcastRegenerateSummary() {
  const btn = document.getElementById('podcast-regen-summary');
  const id = _podcastDetailEpisodeId;
  if (!btn || !id) return;
  btn.disabled = true;
  btn.textContent = 'Regenerating…';
  try {
    await api('POST', `/api/podcast/episodes/${id}/regenerate-summary`);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Regenerate summary';
    showError(e.message || 'Failed to start regeneration');
    return;
  }
  const poll = async () => {
    if (_podcastDetailEpisodeId !== id) return; // user navigated away
    try {
      const ep = await api('GET', `/api/podcast/episodes/${id}`);
      if (ep.status !== 'processing') {
        _renderKnowledgeDetail(ep);
        return;
      }
    } catch (e) { /* transient error — keep polling */ }
    _podcastRegenTimer = setTimeout(poll, 5000);
  };
  _podcastRegenTimer = setTimeout(poll, 5000);
}

async function doPodcastNotify(channel) {
  const btnId = channel === 'signal' ? 'podcast-notify-signal' : 'podcast-notify-email';
  const label = channel === 'signal' ? 'Send to Signal' : 'Send Email';
  const btn = document.getElementById(btnId);
  if (!btn || !_podcastDetailEpisodeId) return;
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const result = await api('POST', `/api/podcast/episodes/${_podcastDetailEpisodeId}/notify`, { channel });
    if (result.sent) {
      btn.textContent = '✓ sent';
      setTimeout(() => { btn.disabled = false; btn.textContent = label; }, 2000);
    } else {
      btn.disabled = false;
      btn.textContent = label;
      showError(result.detail || `Failed to send via ${channel}`);
    }
  } catch (e) {
    btn.disabled = false;
    btn.textContent = label;
    showError(e.message || `Failed to send via ${channel}`);
  }
}

// `extraBtn` (#967) is the button of the tap-a-word popup over the text. Both
// it and the table row's own button show the same progress, so a word added
// from the text does not leave a still-clickable "★ List" in the table below
// suggesting nothing happened.
function doWordTableAdd(idx, extraBtn) {
  const w = _wordTableWords[idx];
  const btns = [document.getElementById(`word-table-add-${idx}`), extraBtn].filter(Boolean);
  if (!w || !btns.length) return;
  const wordZh = w.word || w.word_zh || '';
  if (!wordZh) return;

  _setWordBtns(btns, '…', { disabled: true });
  // Generation takes ~30s in the background — the other rows stay clickable,
  // so several words can be queued up at once.
  //
  // Always the ★ List (#715): a word met while reading goes to the staging
  // area, never straight into today's or tomorrow's review queue. Activating
  // it is a separate, deliberate step in Browse's saved view.
  //
  // lang (#804): file the word under the language it was actually read in,
  // not always Chinese — same reasoning as #726's addWordViaAi lang param.
  addWordViaAi(wordZh, 'list', (state, text) => {
    // 'idle' (#888): the "already in your collection" confirmation was
    // cancelled — nothing happened, so put the row's button back as if it
    // had never been clicked.
    if (state === 'idle') {
      _setWordBtns(btns, '★ List', { disabled: false, error: false, done: false });
      return;
    }
    // Only a failure is worth retrying; a finished add is not repeatable.
    _setWordBtns(btns, text, { disabled: state !== 'error', error: state === 'error',
                               done: state === 'done' });
  }, _wordTableLang);
}

// Same label and state on every button standing for one word — the table row's
// and, when the word was tapped in the text, the popup's (#967).
function _setWordBtns(btns, text, { disabled, error, done } = {}) {
  btns.forEach(b => {
    b.textContent = text;
    if (disabled !== undefined) b.disabled = disabled;
    if (error !== undefined) b.classList.toggle('podcast-add-error', error);
    // #1003: "★ List" -> "★ added to your list" is one word apart at a
    // glance, and a disabled button just looks greyed out — the green state
    // is what actually says "that worked".
    if (done !== undefined) b.classList.toggle('word-table-btn-done', done);
  });
}

// ── New-words table, shared by the knowledge detail page and the book reader ─
//
// Both screens show the same thing — words the annotator flagged in the text
// above, each with ★ List and ✓ Known — so they share one renderer and one
// pair of handlers (#836). A second copy would mean the next fix to the add
// path lands on only one of them, which is exactly what #643 was about.
//
// The word objects are kept in a module-level array so the buttons can look
// them up by index rather than serialising word/definition text (quotes,
// apostrophes) into onclick attributes.
let _wordTableWords = [];
let _wordTableLang = 'zh';

function setWordTable(words, lang) {
  _wordTableWords = words || [];
  _wordTableLang = lang || 'zh';
}

function wordTableHtml(emptyHint) {
  const rows = _wordTableWords.map((w, idx) => `<tr id="word-table-row-${idx}">
      <td class="word-zh">${_escHtml(w.word || w.word_zh || '')}</td>
      <td class="word-pinyin">${_escHtml(w.pinyin || '')}</td>
      <td>${_escHtml(w.definition_de || w.definition || '')}</td>
      <td><button id="word-table-add-${idx}" class="word-table-btn" onclick="doWordTableAdd(${idx})" title="Add to the ★ List">★ List</button></td>
      <td><button id="word-table-known-${idx}" class="word-table-btn" onclick="doWordTableKnown(${idx})" title="I already know this word — stop flagging it">✓ Known</button></td>
    </tr>`).join('');
  if (!rows) return `<p class="keymap-hint">${_escHtml(emptyHint)}</p>`;
  return `<div class="word-table-wrap"><table class="cost-table cost-table-compact"><thead><tr><th>Word</th><th>Pinyin</th><th>German</th><th>Save</th><th>Known</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}


// "✓ Known" in the HSK word table (#710): Daniel already knows this word, it
// just never made it into the collection. One background POST — no reload, no
// re-render of the episode, the other rows stay clickable.
//
// The row is greyed out immediately rather than removed: the annotations in
// the summary text above were baked in when it was generated and don't change,
// so making the row vanish would suggest the word is gone from the page when
// it plainly isn't. What actually changes is the NEXT summary.
function doWordTableKnown(idx, extraBtn) {
  const w = _wordTableWords[idx];
  const btns = [document.getElementById(`word-table-known-${idx}`), extraBtn].filter(Boolean);
  if (!w || !btns.length) return;
  const wordZh = w.word || w.word_zh || '';
  if (!wordZh) return;

  _setWordBtns(btns, '…', { disabled: true });
  // lang (#804): known_words is per-language — see markWordKnown's docstring.
  markWordKnown(wordZh, _wordTableLang).then(() => {
    _setWordBtns(btns, '✓ marked known', { done: true });
    document.getElementById(`word-table-row-${idx}`)?.classList.add('podcast-word-known');
  }).catch(e => {
    // only a failure is worth retrying
    _setWordBtns(btns, e.message || 'failed', { disabled: false, error: true, done: false });
  });
}

// ── Tapping a new word in the text itself (#967) ────────────────────────────
//
// The word table under the text already offers ★ List and ✓ Known, but while
// reading, the eye is up in the sentence and the hand has to go hunt for the
// same word in a table below — on a phone especially. The Chrome extension
// solves this on the desktop (K / Shift+K over any word); a phone cannot run
// extensions at all, which is what this is for.
//
// Only the two screens that render text and word table in the same pass call
// this (knowledge detail, book page). The story loading screen shares
// _knowledgeSummaryHtml() but never sets the word table, so it would wrap
// against whatever list the previous screen left behind.
function _makeWordsTappable(root) {
  if (!root) return;
  // Captured before any DOM mutation below — the #1018 all-words fetch at
  // the bottom needs the ORIGINAL text (mutating first would fold the
  // hidden .tap-word-gloss text, already in the DOM per #996, into what
  // gets sent for segmentation and double-count those words).
  const originalText = root.textContent;

  if (_wordTableWords.length) {
    const isZh = _wordTableLang === 'zh';
    // First index wins: the table is in order of first appearance, and a word
    // repeated in the list would otherwise point the text at the later row.
    const index = new Map();
    _wordTableWords.forEach((w, i) => {
      const key = (w.word || w.word_zh || '').trim();
      if (key && !index.has(isZh ? key : key.toLowerCase())) {
        index.set(isZh ? key : key.toLowerCase(), i);
      }
    });
    // A bare `return` here would also skip the gloss-reveal binding and the
    // #1018 all-words fetch below — this only needs to skip the new-word
    // wrapping itself.
    if (index.size) {
    // Longest first, so "大语言模型" wins over "模型" at the same position.
    const alternation = [...index.keys()]
      .sort((a, b) => b.length - a.length)
      .map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
      .join('|');
    const re = new RegExp(`(${alternation})`, isZh ? 'g' : 'gi');

    const nodes = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) nodes.push(walker.currentNode);

    nodes.forEach(node => {
      const text = node.nodeValue;
      if (!text || !text.trim()) return;
      re.lastIndex = 0;
      let match, cursor = 0, frag = null;
      while ((match = re.exec(text)) !== null) {
        // Letter boundaries are checked here rather than with a lookbehind in
        // the pattern: Safari only learned lookbehind in 16.4, and a phone is
        // exactly where this feature matters. Chinese needs no check — it has
        // no letter boundaries to respect.
        if (!isZh && (_isLetter(text[match.index - 1]) ||
                      _isLetter(text[match.index + match[0].length]))) continue;
        frag = frag || document.createDocumentFragment();
        frag.appendChild(document.createTextNode(text.slice(cursor, match.index)));
        const wordIdx = index.get(isZh ? match[0] : match[0].toLowerCase());
        const span = document.createElement('span');
        span.className = 'tap-word';
        span.dataset.wordIdx = String(wordIdx);
        span.appendChild(document.createTextNode(match[0]));
        // The gloss rides along in the markup from the start (#996), hidden by
        // CSS. Building it on demand would mean re-walking the whole text every
        // time Cmd is pressed — and it must appear in the same frame as the
        // key, not after a reflow the eye can follow.
        const w = _wordTableWords[wordIdx];
        const gloss = w && (w.definition_de || w.definition || '');
        if (gloss) {
          const g = document.createElement('span');
          g.className = 'tap-word-gloss';
          g.textContent = gloss;
          span.appendChild(g);
        }
        frag.appendChild(span);
        cursor = match.index + match[0].length;
      }
      if (!frag) return;
      frag.appendChild(document.createTextNode(text.slice(cursor)));
      node.parentNode.replaceChild(frag, node);
    });

    // Bound once per element (#1006): the listening hint re-renders into the
    // same node on every slider move, and a second listener would mean two
    // panels opening for one tap.
    if (!root.dataset.tapBound) {
      root.dataset.tapBound = '1';
      root.addEventListener('click', (e) => {
        const span = e.target.closest?.('.tap-word');
        if (!span) return;
        e.preventDefault();
        _openWordActions(Number(span.dataset.wordIdx), span);
      });
    }
    }
  }

  _initGlossReveal(root);

  // #1018: everything else in the text gets glossed too, not just the new
  // words above — fetched live (once per unique text+lang, cached both here
  // and per-word on the server) and layered in as a second, non-interactive
  // pass once it lands. Never blocks the first pass above.
  if (originalText && originalText.trim()) {
    _fetchAllWords(originalText, _wordTableLang).then(words => {
      if (!root.isConnected) return;   // page moved on while this was in flight
      _wrapAllWordGlosses(root, words);
    });
  }
}

// ── Glossing every word, not just the new ones (#1018) ─────────────────────
//
// #996 only ever glossed the new-word table. This fetches the live
// "everything" list once per unique (lang, text) and caches the in-flight
// promise so re-renders of the same sentence (the listening-hint slider
// moves, a card reappears) never refetch. Server-side, the words are cached
// per-process too (zh_annotate._translation_cache / annotate.romance's
// batch), so a repeat visit after the page cache is gone is still fast.
let _allWordsCache = new Map();

function _fetchAllWords(text, lang) {
  const key = (lang || 'zh') + '\n' + text;
  if (_allWordsCache.has(key)) return _allWordsCache.get(key);
  const promise = api('POST', '/api/new-words', { text, lang, mode: 'all' })
    .then(r => r.words || [])
    .catch(() => []);
  _allWordsCache.set(key, promise);
  return promise;
}

// Second, non-interactive pass: wraps whatever text in `root` is NOT already
// inside a .tap-word span (the new-word pass above already claimed those —
// re-matching them would double the translation into the hidden gloss text
// it left behind). No click handler and no dataset.wordIdx here — only new
// words get the ★ List / ✓ Known panel (#967); this is display-only.
function _wrapAllWordGlosses(root, words) {
  if (!root || !words || !words.length) return;
  const isZh = _wordTableLang === 'zh';
  const index = new Map();
  words.forEach(w => {
    const key = (w.word || '').trim();
    const gloss = (w.definition_de || '').trim();
    if (key && gloss && !index.has(isZh ? key : key.toLowerCase())) {
      index.set(isZh ? key : key.toLowerCase(), gloss);
    }
  });
  if (!index.size) return;
  const alternation = [...index.keys()]
    .sort((a, b) => b.length - a.length)
    .map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .join('|');
  const re = new RegExp(`(${alternation})`, isZh ? 'g' : 'gi');

  const nodes = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      return node.parentElement && node.parentElement.closest('.tap-word')
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
    }
  });
  while (walker.nextNode()) nodes.push(walker.currentNode);

  nodes.forEach(node => {
    const text = node.nodeValue;
    if (!text || !text.trim()) return;
    re.lastIndex = 0;
    let match, cursor = 0, frag = null;
    while ((match = re.exec(text)) !== null) {
      if (!isZh && (_isLetter(text[match.index - 1]) ||
                    _isLetter(text[match.index + match[0].length]))) continue;
      frag = frag || document.createDocumentFragment();
      frag.appendChild(document.createTextNode(text.slice(cursor, match.index)));
      const span = document.createElement('span');
      span.className = 'gloss-word';
      span.appendChild(document.createTextNode(match[0]));
      const g = document.createElement('span');
      g.className = 'tap-word-gloss';
      g.textContent = index.get(isZh ? match[0] : match[0].toLowerCase());
      span.appendChild(g);
      frag.appendChild(span);
      cursor = match.index + match[0].length;
    }
    if (!frag) return;
    frag.appendChild(document.createTextNode(text.slice(cursor)));
    node.parentNode.replaceChild(frag, node);
  });
}

// ── Showing every gloss at once, under the hanzi (#996) ─────────────────────
//
// Tapping one word at a time (#967) answers "what is this word"; this answers
// "what does this paragraph say" without leaving the text. The glosses are
// already in the DOM, so both triggers are one class on <body> — no re-render,
// no reflow beyond the line height growing.
//
// Desktop: hold Cmd (or Ctrl on a keyboard without one) and everything is
// glossed; release and it is gone. A held key is the right shape for it —
// it is a glance, not a mode to remember to turn off.
// Phone: swipe left across the text toggles it, swipe left again clears it.
// A phone has no modifier key, and a tap is already taken by the popup.
function _setGlossMode(on) {
  document.body.classList.toggle('gloss-on', !!on);
}

function _glossKeyIsModifier(e) {
  return e.key === 'Meta' || e.key === 'Control';
}

let _glossKeysBound = false;

function _bindGlossKeys() {
  if (_glossKeysBound) return;
  _glossKeysBound = true;
  document.addEventListener('keydown', (e) => {
    if (_glossKeyIsModifier(e)) _setGlossMode(true);
  });
  document.addEventListener('keyup', (e) => {
    if (_glossKeyIsModifier(e)) _setGlossMode(false);
  });
  // Cmd+Tab away and the keyup lands in the other window: without this the
  // page would still be fully glossed when Daniel comes back.
  window.addEventListener('blur', () => _setGlossMode(false));
}

function _initGlossReveal(root) {
  _bindGlossKeys();
  if (root.dataset.glossSwipeBound) return;
  root.dataset.glossSwipeBound = '1';
  let g = null;
  root.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) { g = null; return; }
    g = { x0: e.touches[0].clientX, y0: e.touches[0].clientY, axis: null };
  }, { passive: true });
  // Never preventDefault: the axis lock alone decides whether this was a
  // swipe, and stealing the touch would stop the page scrolling (#940).
  root.addEventListener('touchmove', (e) => {
    if (!g || g.axis !== null) return;
    const dx = e.touches[0].clientX - g.x0;
    const dy = e.touches[0].clientY - g.y0;
    if (Math.abs(dx) < _SWIPE_DECIDE && Math.abs(dy) < _SWIPE_DECIDE) return;
    g.axis = Math.abs(dx) > Math.abs(dy) * 1.5 ? 'x' : 'y';
    g.dx = dx;
  }, { passive: true });
  root.addEventListener('touchend', (e) => {
    const swipe = g;
    g = null;
    if (!swipe || swipe.axis !== 'x') return;
    const dx = (e.changedTouches[0]?.clientX ?? swipe.x0) - swipe.x0;
    if (dx <= -_SWIPE_TRIGGER) _setGlossMode(!document.body.classList.contains('gloss-on'));
  });
  root.addEventListener('touchcancel', () => { g = null; });
}

function _isLetter(ch) {
  return !!ch && /[\p{L}\p{M}]/u.test(ch);
}

// The tap target's own little panel: what the word means, and the same two
// actions as its table row. Both buttons hand off to the table's handlers —
// there is exactly one add path in this application (#643) and exactly one
// "I know this" path (#710), and neither gets a second copy here.
function _openWordActions(idx, anchor) {
  closeWordActions();
  const w = _wordTableWords[idx];
  if (!w) return;
  const word = w.word || w.word_zh || '';
  const gloss = w.definition_de || w.definition || '';

  const box = document.createElement('div');
  box.className = 'word-actions';
  box.id = 'word-actions';
  box.innerHTML = `
    <div class="word-actions-head">
      <span class="word-actions-word">${_escHtml(word)}</span>
      ${w.pinyin ? `<span class="word-actions-pinyin">${_escHtml(w.pinyin)}</span>` : ''}
      <button class="word-actions-close" aria-label="Close">✕</button>
    </div>
    ${gloss ? `<p class="word-actions-gloss">${_escHtml(gloss)}</p>` : ''}
    <div class="word-actions-buttons">
      <button class="word-table-btn" id="word-actions-add">★ List</button>
      <button class="word-table-btn" id="word-actions-known">✓ Known</button>
    </div>`;
  document.body.appendChild(box);

  box.querySelector('.word-actions-close').onclick = closeWordActions;
  const addBtn = box.querySelector('#word-actions-add');
  const knownBtn = box.querySelector('#word-actions-known');
  addBtn.onclick = () => doWordTableAdd(idx, addBtn);
  knownBtn.onclick = () => doWordTableKnown(idx, knownBtn);

  _placeWordActions(box, anchor);
  setTimeout(() => document.addEventListener('click', _wordActionsOutside), 0);
  document.addEventListener('keydown', _wordActionsEscape);
}

// Pin the panel to the word itself on every screen size (#994). It used to
// become a bottom sheet under 600px, but on a phone that puts the translation
// at the far end of the screen while the eye is up in the sentence — and it
// covers the text it is explaining. Below the word by default, above it when
// below does not fit and above has more room, horizontally centred on the
// word and clamped 8px inside the viewport.
const _WORD_ACTIONS_GAP = 6;
const _WORD_ACTIONS_EDGE = 8;

function _placeWordActions(box, anchor) {
  const rect = anchor.getBoundingClientRect();
  const w = box.offsetWidth;
  const h = box.offsetHeight;
  const below = window.innerHeight - rect.bottom;
  const above = rect.top;
  // Flip only when the panel genuinely does not fit below — a word near the
  // middle of the screen keeps the reading direction (word, then gloss).
  const flip = below < h + _WORD_ACTIONS_GAP + _WORD_ACTIONS_EDGE && above > below;
  const top = flip ? rect.top - h - _WORD_ACTIONS_GAP : rect.bottom + _WORD_ACTIONS_GAP;
  const left = rect.left + rect.width / 2 - w / 2;
  const maxTop = window.innerHeight - h - _WORD_ACTIONS_EDGE;
  box.style.top = `${Math.max(_WORD_ACTIONS_EDGE, Math.min(top, maxTop))}px`;
  box.style.left = `${Math.max(_WORD_ACTIONS_EDGE, Math.min(left, window.innerWidth - w - _WORD_ACTIONS_EDGE))}px`;
}

function _wordActionsOutside(e) {
  if (!e.target.closest?.('#word-actions')) closeWordActions();
}

function _wordActionsEscape(e) {
  if (e.key === 'Escape') closeWordActions();
}

function closeWordActions() {
  document.getElementById('word-actions')?.remove();
  document.removeEventListener('click', _wordActionsOutside);
  document.removeEventListener('keydown', _wordActionsEscape);
}

// Hash direct-link support: pre-#653 podcast emails/Signal messages link to
// /#podcast-<id> (episode detail) — that form must keep working forever.
// New links (video/article, or anything generated after #653) use
// /#knowledge-<id> and /#knowledge-feed-<id>. Called once at boot, after the
// deck list has loaded, so the knowledge view replaces it. The feed-id form
// must be checked *before* the plain item-detail form — otherwise the
// item-detail regex below never gets a chance to reject it, since both start
// with the same prefix ("#podcast-"/"#knowledge-").
function _openKnowledgeFromHash() {
  const feedMatch = /^#(?:podcast|knowledge)-feed-(\d+)$/.exec(location.hash);
  if (feedMatch) { openPodcastFeed(parseInt(feedMatch[1])); return; }
  const m = /^#(?:podcast|knowledge)-(\d+)$/.exec(location.hash);
  if (m) { openKnowledgeItem(parseInt(m[1])); return; }
  // Tab link (#704): #knowledge-video etc. — the letters-only form, which the
  // digits-only patterns above can never match. This is what the server's
  // /knowledge/videos redirect lands on; nothing in the frontend writes it.
  const tabMatch = /^#knowledge-(podcast|video|reel|article|newsletter)$/.exec(location.hash);
  if (tabMatch) openKnowledge(tabMatch[1]);
}

function startKeyCapture(id) {
  _capturingAction = id; _settingsMsg = ''; renderSettings();
}
function resetKeymapAction(id) {
  _keymap[id] = KEYMAP_DEFAULTS[id]; _saveKeymap();
  _capturingAction = null; _settingsMsg = ''; renderSettings();
}
function clearKeymapAction(id) {
  _keymap[id] = null; _saveKeymap();
  _capturingAction = null; _settingsMsg = ''; renderSettings();
}
function resetKeymapAll() {
  _keymap = { ...KEYMAP_DEFAULTS }; _saveKeymap();
  _capturingAction = null; _settingsMsg = ''; renderSettings();
}
// Capture-phase listener: grabs the next keypress while rebinding,
// before the global review handler can act on it.
function _settingsKeydown(e) {
  if (!_capturingAction) return;
  e.preventDefault(); e.stopPropagation();
  const id = _capturingAction;
  // A modifier pressed on its own fires its own keydown first (#885): holding
  // Shift to type 'W' sends {key:'Shift'} before {key:'W'}. Taking that first
  // event as the binding ended the capture before the real key ever arrived —
  // Shift+X combos were simply unbindable. Ignore them and keep capturing.
  if (KEYMAP_MODIFIER_KEYS.includes(e.key)) return;
  if (e.key === 'Escape') { _capturingAction = null; _settingsMsg = ''; renderSettings(); return; }
  if (e.key === 'Backspace' || e.key === 'Delete') {
    _keymap[id] = null; _saveKeymap();
    _capturingAction = null; _settingsMsg = ''; renderSettings(); return;
  }
  // Shift is allowed (it's how you type 'D', 'C', 'F', etc.) — only Ctrl/Cmd/Alt
  // combos are rejected, since those are reserved for fixed app-wide shortcuts.
  if (e.ctrlKey || e.metaKey || e.altKey) {
    _settingsMsg = 'Press a key without Ctrl, Cmd or Alt.'; renderSettings(); return;
  }
  const key = e.key;
  if (KEYMAP_RESERVED.includes(key)) {
    _settingsMsg = `"${_keyLabel(key)}" is reserved and can't be reassigned.`; renderSettings(); return;
  }
  const action = KEYMAP_ACTIONS.find(a => a.id === id);
  const mySet = _scopeSet(action ? action.scope : id);
  const clash = KEYMAP_ACTIONS.find(a => {
    if (a.id === id || _keymap[a.id] !== key) return false;
    const otherSet = _scopeSet(a.scope);
    for (const v of mySet) if (otherSet.has(v)) return true;
    return false;
  });
  if (clash) {
    _settingsMsg = `"${_keyLabel(key)}" is already used by "${clash.label}" (${_scopeDisplayName(clash.scope)}).`;
    renderSettings(); return;
  }
  _keymap[id] = key; _saveKeymap();
  _capturingAction = null; _settingsMsg = ''; renderSettings();
}
document.addEventListener('keydown', _settingsKeydown, true);

// ── Server logs viewer (issue #454) ─────────────────────────────────────────
let _logsRawText = '';
let _logsAutoTimer = null;

async function openLogsViewer() {
  document.getElementById('logs-modal-overlay').style.display = 'block';
  document.getElementById('logs-modal').style.display = 'flex';
  await refreshLogs();
}

function closeLogsViewer() {
  document.getElementById('logs-modal-overlay').style.display = 'none';
  document.getElementById('logs-modal').style.display = 'none';
  if (_logsAutoTimer) { clearInterval(_logsAutoTimer); _logsAutoTimer = null; }
}

async function refreshLogs() {
  try {
    const res = await fetch('/api/logs?lines=800');
    _logsRawText = res.ok ? await res.text() : `Failed to load logs (${res.status})`;
  } catch (e) {
    _logsRawText = 'Failed to load logs: ' + e.message;
  }
  _applyLogsFilter();
}

function _applyLogsFilter() {
  const body = document.getElementById('logs-body');
  const q = (document.getElementById('logs-filter')?.value || '').toLowerCase();
  const text = q
    ? _logsRawText.split('\n').filter(line => line.toLowerCase().includes(q)).join('\n')
    : _logsRawText;
  body.textContent = text;
  body.scrollTop = body.scrollHeight;
}

function toggleLogsAuto(checked) {
  if (_logsAutoTimer) { clearInterval(_logsAutoTimer); _logsAutoTimer = null; }
  if (checked) _logsAutoTimer = setInterval(refreshLogs, 2000);
}

async function openCostModal() {
  try {
    const data = await api('GET', '/api/costs');
    renderCostModal(data);
    document.getElementById('cost-modal-overlay').style.display = 'block';
    document.getElementById('cost-modal').style.display = 'flex';
  } catch (e) {
    showError('Failed to load cost data: ' + e.message);
  }
}

function closeCostModal() {
  document.getElementById('cost-modal-overlay').style.display = 'none';
  document.getElementById('cost-modal').style.display = 'none';
}

function _prettyModel(model) {
  return model
    .replace('claude-', '')
    .replace('-20251001', '')
    .replace('gpt-5.6-luna', 'GPT-5.6 Luna')
    .replace('gpt-5.6-terra', 'GPT-5.6 Terra')
    .replace('gpt-5.6-sol', 'GPT-5.6 Sol')
    .replace('gpt-5.1', 'GPT-5.1')
    .replace('gpt-5-mini', 'GPT-5 Mini')
    .replace('gpt-4o-mini-transcribe', 'Whisper (audio)')
    .replace('deepseek-v4-flash', 'DeepSeek V4 Flash')
    .replace('deepseek-v4-pro', 'DeepSeek V4 Pro')
    .replace('deepseek-chat', 'DeepSeek V3')
    .replace('deepseek-reasoner', 'DeepSeek R1')
    .replace('glm-4-flash', 'GLM-4-Flash')
    .replace('glm-4-air', 'GLM-4-Air')
    .replace('qwen-turbo', 'Qwen Turbo')
    .replace('qwen-plus', 'Qwen Plus');
}

// called_at is stored as UTC "YYYY-MM-DD HH:MM:SS" (SQLite datetime('now'),
// no timezone marker) — append 'Z' so Date parses it as UTC, then format in
// the viewer's local time. Today's calls show just HH:MM; older ones get the
// date prefixed.
function _fmtCostTime(calledAt) {
  const d = new Date(calledAt.replace(' ', 'T') + 'Z');
  const now = new Date();
  const pad = n => String(n).padStart(2, '0');
  const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const sameDay = d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  if (sameDay) return hhmm;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hhmm}`;
}

function renderCostModal(data) {
  const fmt = n => '$' + n.toFixed(2);
  const fmtCost = n => (n === null || n === undefined) ? '?' : '$' + n.toFixed(4);

  let html = `<div class="cost-total">
    <span>Total spent <b>${fmt(data.total_cost)}</b></span>
    <span>Last 30 days <b>${fmt(data.total_cost_30d)}</b></span>
  </div>`;

  for (const b of data.balances || []) {
    let value;
    if (b.unsupported) {
      value = `<span style="color:var(--muted)">${_escHtml(b.note || 'no balance API')}</span>`;
    } else if (b.balance == null) {
      value = `<span style="color:var(--muted)">unavailable</span>`;
    } else {
      const symbol = b.currency === 'CNY' ? '¥' : (b.currency === 'USD' ? '$' : (b.currency + ' '));
      value = `${symbol}${b.balance}`;
    }
    html += `<div class="cost-balance">${_escHtml(b.provider)} balance: ${value}</div>`;
  }

  if (data.unknown_calls > 0) {
    html += `<div class="cost-warning">⚠ ${data.unknown_calls} call(s) could not be priced ` +
      `(unknown models: ${data.unknown_models.join(', ')})</div>`;
  }

  if (!data.actions.length) {
    html += '<div class="cost-empty">No API calls logged yet.</div>';
  } else {
    html += '<table class="cost-table cost-actions-table"><tbody>';
    data.actions.forEach((a, idx) => {
      const callWord = a.call_count === 1 ? 'call' : 'calls';
      // Legacy actions (#537) are reconstructed from time-adjacency, not a real
      // action_id — flag them so the grouping reads as approximate, not exact.
      const approxTag = a.approx
        ? ` <span class="cost-approx-tag" title="Grouped by timing — these calls predate per-action tracking, so the grouping is approximate.">≈ grouped</span>`
        : '';
      html += `<tr class="cost-action-row" onclick="toggleCostAction(${idx})">
        <td class="cost-action-arrow" id="cost-action-arrow-${idx}">&#9656;</td>
        <td class="cost-action-time">${_fmtCostTime(a.started_at)}</td>
        <td class="cost-action-label">${_escHtml(a.label)}${approxTag}</td>
        <td class="cost-num" style="color:var(--muted)">${a.call_count} ${callWord}</td>
        <td class="cost-num cost-value">${fmtCost(a.total_cost)}</td>
      </tr>`;
      html += `<tr class="cost-action-detail" id="cost-action-${idx}" style="display:none">
        <td colspan="5">
          <table class="cost-table cost-calls-table">
            <thead><tr>
              <th>Time</th><th>Model</th><th>Purpose</th><th>Tokens in / out</th>
              <th style="text-align:right">Cost</th><th></th>
            </tr></thead>
            <tbody>`;
      for (const c of a.calls) {
        const model = _prettyModel(c.model);
        const cachedTitle = c.cached_input_tokens > 0
          ? ` title="(${c.cached_input_tokens.toLocaleString()} cached)"` : '';
        const promptBtn = (c.has_prompt
          ? `<button class="cost-prompt-btn" onclick="event.stopPropagation(); showCostPrompt(${c.id}, 'prompt')">Prompt</button>`
          : '') + (c.has_response
          ? ` <button class="cost-prompt-btn" onclick="event.stopPropagation(); showCostPrompt(${c.id}, 'response')">Response</button>`
          : '');
        html += `<tr>
          <td>${_fmtCostTime(c.called_at)}</td>
          <td><span class="cost-model">${model}</span></td>
          <td style="color:var(--muted)">${_escHtml(c.purpose)}</td>
          <td class="cost-num" style="color:var(--muted)"${cachedTitle}>${c.input_tokens.toLocaleString()} / ${c.output_tokens.toLocaleString()}</td>
          <td class="cost-num cost-value">${fmtCost(c.cost)}</td>
          <td>${promptBtn}</td>
        </tr>`;
      }
      html += '</tbody></table></td></tr>';
    });
    html += '</tbody></table>';
  }

  html += `<div class="cost-footnote">Prices as of ${data.pricing_as_of} — edit database/stats.py when providers change pricing.</div>`;

  document.getElementById('cost-modal-body').innerHTML = html;
}

function toggleCostAction(idx) {
  const row = document.getElementById('cost-action-' + idx);
  const arrow = document.getElementById('cost-action-arrow-' + idx);
  if (!row) return;
  const open = row.style.display !== 'none';
  row.style.display = open ? 'none' : 'table-row';
  arrow.innerHTML = open ? '&#9656;' : '&#9662;';
}

// Shared monospace text overlay: the cost page's prompt/response viewer and the
// starred-sentence prompt link (#697) show the same kind of thing, so they use
// the same box rather than each growing their own.
function _openPromptOverlay(title) {
  let overlay = document.getElementById('cost-prompt-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'cost-prompt-overlay';
    overlay.className = 'cost-prompt-overlay';
    overlay.innerHTML = `
      <div class="cost-prompt-box">
        <div class="cost-prompt-header">
          <span id="cost-prompt-title">Prompt</span>
          <button class="cost-prompt-close" onclick="closeCostPrompt()">&times;</button>
        </div>
        <pre class="cost-prompt-body" id="cost-prompt-body">Loading…</pre>
      </div>`;
    overlay.addEventListener('click', e => { if (e.target === overlay) closeCostPrompt(); });
    document.body.appendChild(overlay);
  }
  overlay.style.display = 'flex';
  document.getElementById('cost-prompt-title').textContent = title;
  const body = document.getElementById('cost-prompt-body');
  body.textContent = 'Loading…';
  return body;
}

async function showCostPrompt(callId, kind = 'prompt') {
  const body = _openPromptOverlay(kind === 'response' ? 'Response' : 'Prompt');
  try {
    const data = await api('GET', `/api/costs/call/${callId}`);
    body.textContent = data[kind] || `(no ${kind} stored)`;
  } catch (e) {
    body.textContent = `Failed to load ${kind}: ` + e.message;
  }
}

// The prompt that generated a starred sentence (#697) — the reason for starring
// it in the first place is being able to come back and read this.
async function showStoryPrompt(storyId) {
  const body = _openPromptOverlay('Prompt');
  try {
    const d = await api('GET', `/api/story-prompt/${storyId}`);
    document.getElementById('cost-prompt-title').textContent =
      `Prompt — ${d.mode}${d.model ? ` · ${d.model}` : ''} · ${d.date}`;
    // An empty prompt is a normal state (legacy story, or the offline snapshot
    // strips prompt_text) — say which, don't show an empty box.
    body.textContent = d.prompt ||
      '(no prompt stored for this story — either it predates prompt logging, ' +
      'or this database is an offline snapshot, which clears prompt_text to save space)';
  } catch (e) {
    body.textContent = 'Failed to load prompt: ' + e.message;
  }
}

function closeCostPrompt() {
  const overlay = document.getElementById('cost-prompt-overlay');
  if (overlay) overlay.style.display = 'none';
}

// ── Prompt template editor (issue #581, versioned presets since #610) ───────
// Edit the full prompt of the currently selected story mode; dynamic content
// ({words}, {topic}, …) stays as placeholders so the word list never clutters
// the editor. Each mode can hold several named versions (prompt_presets
// table); at most one is "active" and applies everywhere the mode
// generates — full stories and Again single-sentence regens alike. No
// active version = built-in default.
let _promptEditorMode = null;
let _promptEditorData = null;  // last GET /api/prompt-template/{mode} response

async function openPromptEditor() {
  const mode = document.getElementById('setup-mode').value;
  let overlay = document.getElementById('prompt-editor-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'prompt-editor-overlay';
    overlay.className = 'cost-prompt-overlay';
    overlay.innerHTML = `
      <div class="cost-prompt-box" style="display:flex;flex-direction:column;gap:8px">
        <div class="cost-prompt-header">
          <span id="prompt-editor-title">Prompt template</span>
          <button class="cost-prompt-close" onclick="closePromptEditor()">&times;</button>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <select id="prompt-preset-select" class="edit-input" style="flex:1;min-width:140px" onchange="onPromptPresetChange()"></select>
          <input id="prompt-preset-name" class="edit-input" style="flex:1;min-width:140px" placeholder="Name for new version">
        </div>
        <div id="prompt-editor-vars" style="font-size:12px;color:var(--muted,#888)"></div>
        <textarea id="prompt-editor-text" spellcheck="false"
          style="width:100%;min-height:50vh;font-family:monospace;font-size:12px;line-height:1.45;resize:vertical"></textarea>
        <div style="display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap">
          <button class="edit-cancel-btn" onclick="resetPromptTemplate()">Reset to default</button>
          <button class="edit-cancel-btn" onclick="deletePromptPreset()">Delete</button>
          <button class="edit-save-btn" onclick="savePromptPresetAsNew()">Save as new</button>
          <button class="edit-save-btn" onclick="savePromptTemplate()">Save</button>
        </div>
      </div>`;
    overlay.addEventListener('click', e => { if (e.target === overlay) closePromptEditor(); });
    document.body.appendChild(overlay);
  }
  overlay.style.display = 'flex';
  _promptEditorMode = mode;
  await loadPromptEditor();
}

// (Re)loads the mode's template metadata + presets from the server and
// refreshes the title, placeholder hint, preset dropdown and textarea.
async function loadPromptEditor() {
  const mode = _promptEditorMode;
  const title = document.getElementById('prompt-editor-title');
  const ta = document.getElementById('prompt-editor-text');
  const select = document.getElementById('prompt-preset-select');
  document.getElementById('prompt-preset-name').value = '';
  title.textContent = 'Loading…';
  ta.value = '';
  try {
    const data = await api('GET', `/api/prompt-template/${mode}`);
    _promptEditorData = data;
    document.getElementById('prompt-editor-vars').textContent =
      'Placeholders (replaced at generation time): ' + data.variables.map(v => `{${v}}`).join('  ');
    ta.value = data.template;

    // Build options via the DOM, not an HTML string — preset names are free
    // text and would break the markup on a quote or angle bracket.
    select.innerHTML = '';
    const defaultOpt = new Option('Built-in default', '');
    select.appendChild(defaultOpt);
    data.presets.forEach(p => select.appendChild(new Option(p.name, String(p.id))));
    select.value = data.active_id != null ? String(data.active_id) : '';

    const active = data.presets.find(p => p.is_active);
    title.textContent = `Prompt template — ${mode}` + (active ? ` · ${active.name}` : '');
  } catch (e) {
    title.textContent = 'Failed to load template';
    ta.value = e.message;
  }
}

// Switching the dropdown activates the chosen preset (or deactivates all
// presets for "Built-in default") and reloads the editor with its content.
async function onPromptPresetChange() {
  const select = document.getElementById('prompt-preset-select');
  const value = select.value;
  try {
    if (value) {
      await api('POST', `/api/prompt-presets/${value}/activate`);
    } else {
      await api('DELETE', `/api/prompt-template/${_promptEditorMode}`);
    }
    await loadPromptEditor();
  } catch (e) {
    showError('Failed to switch version: ' + e.message);
  }
}

async function savePromptTemplate() {
  if (!_promptEditorMode) return;
  const template = document.getElementById('prompt-editor-text').value;
  const activeId = _promptEditorData && _promptEditorData.active_id;
  try {
    if (activeId != null) {
      await api('PUT', `/api/prompt-presets/${activeId}`, { template });
    } else {
      const name = document.getElementById('prompt-preset-name').value.trim() || 'Custom';
      await api('POST', `/api/prompt-presets/${_promptEditorMode}`, { name, template });
    }
    await loadPromptEditor();
    closePromptEditor();
  } catch (e) {
    showError('Save failed: ' + e.message);
  }
}

async function savePromptPresetAsNew() {
  if (!_promptEditorMode) return;
  const name = document.getElementById('prompt-preset-name').value.trim();
  if (!name) {
    showError('Enter a name for the new version.');
    return;
  }
  const template = document.getElementById('prompt-editor-text').value;
  try {
    await api('POST', `/api/prompt-presets/${_promptEditorMode}`, { name, template });
    await loadPromptEditor();
  } catch (e) {
    showError('Save failed: ' + e.message);
  }
}

async function deletePromptPreset() {
  const activeId = _promptEditorData && _promptEditorData.active_id;
  if (activeId == null) {
    showError('Select a saved version to delete.');
    return;
  }
  if (!confirm('Delete this prompt version? This cannot be undone.')) return;
  try {
    await api('DELETE', `/api/prompt-presets/${activeId}`);
    await loadPromptEditor();
  } catch (e) {
    showError('Delete failed: ' + e.message);
  }
}

async function resetPromptTemplate() {
  if (!_promptEditorMode) return;
  try {
    await api('DELETE', `/api/prompt-template/${_promptEditorMode}`);
    await loadPromptEditor();
  } catch (e) {
    showError('Reset failed: ' + e.message);
  }
}

function closePromptEditor() {
  const overlay = document.getElementById('prompt-editor-overlay');
  if (overlay) overlay.style.display = 'none';
}

function renderStats(data) {
  // Big numbers
  document.getElementById('stat-grid').innerHTML = [
    { num: data.streak_days,    label: 'Day Streak' },
    { num: data.total_words,    label: 'Total Words' },
    { num: data.reviews_today,  label: 'Reviews Today' },
    { num: data.new_today,      label: 'New Today' },
  ].map(s => `
    <div class="stat-card">
      <div class="stat-num">${s.num}</div>
      <div class="stat-label">${s.label}</div>
    </div>`).join('');

  // Bar chart
  const days = data.reviews_by_day || [];
  const maxCount = Math.max(...days.map(d => d.count), 1);
  document.getElementById('bar-chart').innerHTML = days.map(d => {
    const pct = Math.round((d.count / maxCount) * 100);
    const label = d.date.slice(5); // MM-DD
    return `
      <div class="bar-col" title="${d.date}: ${d.count}">
        <div class="bar-fill" style="height:${pct}%"></div>
        <div class="bar-day">${label}</div>
      </div>`;
  }).join('');

  // State pills
  const sc = data.state_counts || {};
  const STATES = ['new','learning','review','relearn','suspended'];
  const colors = { new:'var(--primary)', learning:'var(--hard)', review:'var(--good)',
                   relearn:'var(--again)', suspended:'var(--muted)' };
  document.getElementById('state-row').innerHTML = STATES.map(s => `
    <div class="state-pill">
      <div class="state-pill-num" style="color:${colors[s]}">${sc[s] || 0}</div>
      <div class="state-pill-label">${s}</div>
    </div>`).join('');
}

// ── Options modal ─────────────────────────────────────────────────────────────
let allPresets = [];

const CAT_LABELS = { listening: 'L – Listening', reading: 'R – Reading', creating: 'C – Creating' };

function _setCategoryOrderUI(order) {
  const list = document.getElementById('opt-cat-order-list');
  if (!list) return;
  list.innerHTML = '';
  order.forEach((cat, i) => {
    const li = document.createElement('li');
    li.dataset.cat = cat;
    li.innerHTML = `<span class="cat-order-label">${CAT_LABELS[cat] || cat}</span>
      <span class="cat-order-btns">
        <button type="button" onclick="_moveCatOrder(this,-1)" ${i === 0 ? 'disabled' : ''}>▲</button>
        <button type="button" onclick="_moveCatOrder(this,1)"  ${i === order.length - 1 ? 'disabled' : ''}>▼</button>
      </span>`;
    list.appendChild(li);
  });
}

function _moveCatOrder(btn, dir) {
  const li = btn.closest('li');
  const list = li.parentElement;
  const items = [...list.children];
  const idx = items.indexOf(li);
  const swapIdx = idx + dir;
  if (swapIdx < 0 || swapIdx >= items.length) return;
  if (dir === -1) list.insertBefore(li, items[swapIdx]);
  else list.insertBefore(items[swapIdx], li);
  const newOrder = [...list.children].map(el => el.dataset.cat);
  _setCategoryOrderUI(newOrder);
}

function _getCategoryOrderUI() {
  const list = document.getElementById('opt-cat-order-list');
  if (!list) return 'listening,reading,creating';
  return [...list.children].map(el => el.dataset.cat).join(',');
}

let currentPresetId = null;

// Show only the fields relevant to the chosen scheduler.
// FSRS on  → hide .sched-sm2 (graduating/easy interval)
// FSRS off → hide .sched-fsrs (desired retention, maximum interval)
function applySchedulerVisibility() {
  const fsrs = document.getElementById('opt-enable-fsrs').checked;
  document.querySelectorAll('.sched-sm2').forEach(el => { el.style.display = fsrs ? 'none' : ''; });
  document.querySelectorAll('.sched-fsrs').forEach(el => { el.style.display = fsrs ? '' : 'none'; });
}

// Learning-leech off → hide the threshold input (it only matters when on).
function applyLearningLeechVisibility() {
  const on = document.getElementById('opt-enable-learning-leech').checked;
  document.querySelectorAll('.learning-leech-row').forEach(el => { el.style.display = on ? '' : 'none'; });
}

// Clickable ⓘ explanations for scheduling fields.
const INFO_TEXT = {
  enable_fsrs: ['Enable FSRS',
    'FSRS is a modern scheduler that models each card with Stability and Difficulty to predict the best review time. Turn it off to fall back to the legacy SM-2 (ease-factor) algorithm. Switching hides the fields that don\'t apply to the chosen scheduler.'],
  hard_1d: ['Hard = fixed days',
    'While a card is still in learning or relearning, pressing Hard sends it forward by a fixed number of days (set below) instead of repeating in a few minutes. Applies to both schedulers.'],
  hard_days: ['Hard delay (days)',
    'How many days Hard pushes a learning/relearning card forward when "Hard = fixed days" is enabled. Fractional values allowed (e.g. 0.5 = half a day).'],
  learning_steps: ['Learning steps',
    'The sub-day intervals (in minutes) a new card steps through before it graduates to review. Example: "10m" means one 10-minute step. Used by both schedulers.'],
  graduating_interval: ['Graduating interval (SM-2 only)',
    'The interval (in days) a card gets the first time it leaves learning with Good. Only used by SM-2 — under FSRS the first interval is computed from the card\'s initial stability instead.'],
  easy_interval: ['Easy interval (SM-2 only)',
    'The interval (in days) a learning card jumps to when rated Easy. Only used by SM-2 — under FSRS this is computed from stability.'],
  learned_interval: ['Learned interval',
    'The interval (in days) a card must reach before it counts as "learned/mature". Reviews of cards below this interval — plus all relearning cards — are treated as "still learning" in the retention stats and the deck badge counts. With Graduation probation on, this is also the gap a card must survive before it truly becomes a review card. Default 4.'],
  enable_probation: ['Graduation probation',
    'When on, a card that finishes its learning (or relearning) steps does NOT immediately become a review card. Instead it stays in learning/relearn on "probation" until it survives an interval of at least the Learned interval. Only then does it graduate to review (where Again counts as a lapse). Turn it off for classic Anki behaviour: the card graduates to review the moment its steps are done.'],
  desired_retention: ['Desired retention (FSRS only)',
    'The probability you want of still recalling a card when it comes due, e.g. 90%. Higher retention = shorter intervals and more reviews; lower = longer intervals, fewer reviews but more lapses. This is the main FSRS knob.'],
  maximum_interval: ['Maximum interval (FSRS only)',
    'An upper cap (in days) on how far into the future a review can be scheduled. Default 36500 (~100 years) effectively means no cap.'],
};

function showInfoPop(target, key) {
  const info = INFO_TEXT[key];
  if (!info) return;
  const pop = document.getElementById('info-pop');
  document.getElementById('info-pop-title').textContent = info[0];
  document.getElementById('info-pop-body').textContent  = info[1];
  pop.style.display = 'block';
  // Position below the icon, kept inside the viewport.
  const r = target.getBoundingClientRect();
  pop.style.visibility = 'hidden';
  const pw = pop.offsetWidth, ph = pop.offsetHeight;
  let left = r.left;
  if (left + pw > window.innerWidth - 8) left = window.innerWidth - pw - 8;
  let top = r.bottom + 6;
  if (top + ph > window.innerHeight - 8) top = r.top - ph - 6;
  pop.style.left = Math.max(8, left) + 'px';
  pop.style.top  = Math.max(8, top) + 'px';
  pop.style.visibility = 'visible';
}
function hideInfoPop() { document.getElementById('info-pop').style.display = 'none'; }

document.addEventListener('click', (e) => {
  const icon = e.target.closest('.info-i');
  if (icon) {
    e.stopPropagation();
    const pop = document.getElementById('info-pop');
    const same = pop.style.display === 'block' && pop.dataset.key === icon.dataset.info;
    if (same) { hideInfoPop(); return; }
    pop.dataset.key = icon.dataset.info;
    showInfoPop(icon, icon.dataset.info);
    return;
  }
  if (!e.target.closest('#info-pop')) hideInfoPop();
});

function loadPresetFields(preset) {
  currentPresetId = preset.id;
  document.getElementById('opt-new-per-day').value     = preset.new_per_day;
  document.getElementById('opt-reviews-per-day').value = preset.reviews_per_day;
  document.getElementById('opt-learn-steps').value     = preset.learning_steps;
  document.getElementById('opt-grad-int').value        = preset.graduating_interval;
  document.getElementById('opt-easy-int').value        = preset.easy_interval;
  document.getElementById('opt-learned-int').value     = preset.learned_interval ?? 4;
  document.getElementById('opt-relearn-steps').value   = preset.relearning_steps;
  document.getElementById('opt-leech').value           = preset.leech_threshold;
  document.getElementById('opt-learning-leech').value  = preset.learning_leech_threshold;
  document.getElementById('opt-enable-learning-leech').checked = preset.enable_learning_leech == null ? true : !!preset.enable_learning_leech;
  document.getElementById('opt-new-gather-order').value        = preset.new_gather_order                || 'ascending_position';
  document.getElementById('opt-new-sort-order').value          = preset.new_sort_order                  || 'card_type_gathered';
  document.getElementById('opt-new-review-order').value        = preset.new_review_order                || 'mixed';
  document.getElementById('opt-interday-learning-order').value = preset.interday_learning_review_order  || 'mixed';
  document.getElementById('opt-review-sort-order').value       = preset.review_sort_order               || 'due_random';
  document.getElementById('opt-bury-new').checked      = !!preset.bury_new_siblings;
  document.getElementById('opt-bury-review').checked   = !!preset.bury_review_siblings;
  document.getElementById('opt-bury-interday').checked = !!preset.bury_interday_siblings;
  document.getElementById('opt-sibling-sep').value     = preset.sibling_separation ?? 3;
  document.getElementById('opt-sibling-factor').value  = preset.sibling_factor ?? 0.2;
  document.getElementById('opt-enable-probation').checked = preset.enable_probation == null ? true : !!preset.enable_probation;
  document.getElementById('opt-enable-fsrs').checked   = preset.enable_fsrs == null ? true : !!preset.enable_fsrs;
  document.getElementById('opt-hard-1d').checked       = preset.learning_hard_1d == null ? true : !!preset.learning_hard_1d;
  document.getElementById('opt-hard-days').value       = preset.learning_hard_days == null ? 1 : preset.learning_hard_days;
  document.getElementById('opt-desired-retention').value = Math.round((preset.desired_retention ?? 0.9) * 100);
  document.getElementById('opt-max-int').value         = preset.maximum_interval ?? 36500;
  document.getElementById('opt-reading-enabled').checked = !!preset.reading_enabled;
  document.getElementById('opt-listening-enabled').checked = preset.listening_enabled == null ? true : !!preset.listening_enabled;
  document.getElementById('opt-creating-enabled').checked  = preset.creating_enabled  == null ? true : !!preset.creating_enabled;
  applySchedulerVisibility();
  applyLearningLeechVisibility();

  // Category order
  const order = (preset.category_order || 'listening,reading,creating').split(',').map(s => s.trim());
  _setCategoryOrderUI(order);
  const btnDef = document.getElementById('btn-set-default');
  btnDef.textContent = preset.is_default ? '✓ Already default' : 'Set as default';
  btnDef.disabled = !!preset.is_default;
  const btnDel = document.getElementById('btn-delete-preset');
  btnDel.disabled = allPresets.length <= 1;

  // Category overrides
  _loadCategoryOverrides(preset.category_overrides || {});
}

const _CAT_OVERRIDE_FIELDS = [
  'new_per_day', 'reviews_per_day', 'learning_steps',
  'graduating_interval', 'easy_interval', 'relearning_steps',
];

function _loadCategoryOverrides(overrides) {
  for (const details of document.querySelectorAll('.cat-override-details')) {
    const cat = details.dataset.cat;
    const catOverrides = overrides[cat] || {};
    let hasAny = false;
    for (const input of details.querySelectorAll('input[data-field]')) {
      const val = catOverrides[input.dataset.field];
      input.value = val != null ? val : '';
      if (val != null) hasAny = true;
    }
    if (hasAny) {
      details.setAttribute('data-has-overrides', '');
      details.open = true;
    } else {
      details.removeAttribute('data-has-overrides');
      details.open = false;
    }
  }
}

function _collectCategoryOverrides() {
  const result = {};
  for (const details of document.querySelectorAll('.cat-override-details')) {
    const cat = details.dataset.cat;
    const fields = {};
    for (const input of details.querySelectorAll('input[data-field]')) {
      const raw = input.value.trim();
      if (raw !== '') {
        fields[input.dataset.field] = input.type === 'number' ? Number(raw) : raw;
      }
    }
    if (Object.keys(fields).length > 0) result[cat] = fields;
  }
  return result;
}

// The three category switches of an aggregating deck shown under several
// language tabs (the root 'All') belong to the *active language's* preset, not
// to the deck's own — otherwise turning Creating on under Français turns it on
// under 中文 too (#898). The backend does the routing; it just needs to know
// which tab we are on.
function _optLangQ() { return _langQ() ? `?${_langQ()}` : ''; }


function renderPresetSelect(selectedId) {
  const sel = document.getElementById('opt-preset-select');
  sel.innerHTML = allPresets.map(p =>
    `<option value="${p.id}" ${p.id === selectedId ? 'selected' : ''}>${p.name}${p.is_default ? ' ★' : ''}</option>`
  ).join('');
}

async function openOptions(deckId) {
  optDeckId = deckId;
  try {
    const [preset, presets] = await Promise.all([
      api('GET', `/api/decks/${deckId}/preset${_optLangQ()}`),
      api('GET', '/api/presets'),
    ]);
    allPresets = presets;
    renderPresetSelect(preset.id);
    loadPresetFields(preset);
    document.getElementById('modal-overlay').classList.add('open');
  } catch (e) {
    showError('Could not load options: ' + e.message);
  }
}

async function switchPreset(presetId) {
  presetId = parseInt(presetId);
  try {
    await api('PUT', `/api/decks/${optDeckId}/preset/assign?preset_id=${presetId}`);
    const preset = await api('GET', `/api/decks/${optDeckId}/preset${_optLangQ()}`);
    loadPresetFields(preset);
  } catch (e) {
    showError('Failed to switch preset: ' + e.message);
  }
}

async function addPreset() {
  const name = prompt('Preset name:');
  if (!name) return;
  const currentId = parseInt(document.getElementById('opt-preset-select').value);
  try {
    const preset = await api('POST', `/api/presets?name=${encodeURIComponent(name)}&clone_from_id=${currentId}`);
    allPresets = await api('GET', '/api/presets');
    renderPresetSelect(preset.id);
    await switchPreset(preset.id);
  } catch (e) {
    showError('Failed to create preset: ' + e.message);
  }
}

async function renamePreset() {
  const currentId = parseInt(document.getElementById('opt-preset-select').value);
  const current = allPresets.find(p => p.id === currentId);
  const name = prompt('New name:', current?.name || '');
  if (!name || name === current?.name) return;
  try {
    await api('PUT', `/api/decks/${optDeckId}/preset`, { name });
    allPresets = await api('GET', '/api/presets');
    renderPresetSelect(currentId);
  } catch (e) {
    showError('Failed to rename: ' + e.message);
  }
}

async function deletePreset() {
  if (allPresets.length <= 1) return;
  const currentId = parseInt(document.getElementById('opt-preset-select').value);
  const current = allPresets.find(p => p.id === currentId);
  if (!confirm(`Delete preset "${current?.name}"? Decks using it will be reassigned to the default preset.`)) return;
  // First reassign all decks using this preset to the default
  const defaultPreset = allPresets.find(p => p.is_default && p.id !== currentId) || allPresets.find(p => p.id !== currentId);
  try {
    await api('PUT', `/api/decks/${optDeckId}/preset/assign?preset_id=${defaultPreset.id}`);
    await api('DELETE', `/api/presets/${currentId}`);
    allPresets = await api('GET', '/api/presets');
    renderPresetSelect(defaultPreset.id);
    loadPresetFields(defaultPreset);
  } catch (e) {
    showError('Delete failed: ' + e.message);
  }
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
  optDeckId = null;
}

async function saveOptions() {
  if (!optDeckId) return;
  const fields = {
    new_per_day:         parseInt(document.getElementById('opt-new-per-day').value),
    reviews_per_day:     parseInt(document.getElementById('opt-reviews-per-day').value),
    learning_steps:      document.getElementById('opt-learn-steps').value.trim(),
    graduating_interval: parseInt(document.getElementById('opt-grad-int').value),
    easy_interval:       parseInt(document.getElementById('opt-easy-int').value),
    learned_interval:    parseInt(document.getElementById('opt-learned-int').value),
    enable_probation:       document.getElementById('opt-enable-probation').checked ? 1 : 0,
    relearning_steps:    document.getElementById('opt-relearn-steps').value.trim(),
    leech_threshold:     parseInt(document.getElementById('opt-leech').value),
    learning_leech_threshold: parseInt(document.getElementById('opt-learning-leech').value),
    enable_learning_leech:    document.getElementById('opt-enable-learning-leech').checked ? 1 : 0,
    new_gather_order:               document.getElementById('opt-new-gather-order').value,
    new_sort_order:                 document.getElementById('opt-new-sort-order').value,
    new_review_order:               document.getElementById('opt-new-review-order').value,
    interday_learning_review_order: document.getElementById('opt-interday-learning-order').value,
    review_sort_order:              document.getElementById('opt-review-sort-order').value,
    bury_new_siblings:      document.getElementById('opt-bury-new').checked      ? 1 : 0,
    bury_review_siblings:   document.getElementById('opt-bury-review').checked   ? 1 : 0,
    bury_interday_siblings: document.getElementById('opt-bury-interday').checked ? 1 : 0,
    sibling_separation:     parseInt(document.getElementById('opt-sibling-sep').value) || 3,
    sibling_factor:         parseFloat(document.getElementById('opt-sibling-factor').value) || 0.2,
    enable_fsrs:            document.getElementById('opt-enable-fsrs').checked ? 1 : 0,
    learning_hard_1d:       document.getElementById('opt-hard-1d').checked ? 1 : 0,
    learning_hard_days:     Math.max(0.1, parseFloat(document.getElementById('opt-hard-days').value) || 1),
    desired_retention:      Math.min(0.99, Math.max(0.70, (parseInt(document.getElementById('opt-desired-retention').value) || 90) / 100)),
    maximum_interval:       Math.max(1, parseInt(document.getElementById('opt-max-int').value) || 36500),
    category_order: _getCategoryOrderUI(),
    reading_enabled:        document.getElementById('opt-reading-enabled').checked ? 1 : 0,
    listening_enabled:      document.getElementById('opt-listening-enabled').checked ? 1 : 0,
    creating_enabled:       document.getElementById('opt-creating-enabled').checked ? 1 : 0,
  };
  try {
    const [savedPreset] = await Promise.all([
      api('PUT', `/api/decks/${optDeckId}/preset${_optLangQ()}`, fields),
    ]);
    const presetId = currentPresetId;
    // Save category overrides
    const catOverrides = _collectCategoryOverrides();
    const cats = ['listening', 'reading', 'creating'];
    await Promise.all(cats.map(cat => {
      if (catOverrides[cat]) {
        return api('PUT', `/api/presets/${presetId}/categories/${cat}`, catOverrides[cat]);
      } else {
        return api('DELETE', `/api/presets/${presetId}/categories/${cat}`).catch(() => {});
      }
    }));
    closeModal();
    // Options can be opened mid-review (the `o` shortcut), and loadDecks()
    // would otherwise showView('decks') and throw the session away. Refresh
    // the deck tree in the background instead — the backend already
    // invalidated the queue, so the rebuilt one picks up the new settings.
    loadDecks({ keepView: _currentView !== 'decks' });
  } catch (e) {
    showError('Save failed: ' + e.message);
  }
}

async function setDefaultPreset() {
  if (!optDeckId) return;
  try {
    await api('POST', `/api/decks/${optDeckId}/preset/set-default`);
    allPresets = await api('GET', '/api/presets');
    const currentId = parseInt(document.getElementById('opt-preset-select').value);
    renderPresetSelect(currentId);
    const btn = document.getElementById('btn-set-default');
    btn.textContent = '✓ Already default';
    btn.disabled = true;
  } catch (e) {
    showError('Failed: ' + e.message);
  }
}

// Jump straight into the "All" deck's review for a given category.
// Used by the home-view keyboard shortcuts (L → listening, C → creating).
function _startAllDeckCategory(cat) {
  const allDeck = (_cachedDecks || []).find(d => d.virtual && d.name === 'All');
  if (!allDeck) return;
  startReview(allDeck.id, cat, 'All', !!allDeck.no_story);
}

// ── Start review session ────────────────────────────────────────────────────
async function startReview(id, cat, name, noStory = false, quick = false) {
  navPush('review');
  quickMode = quick;
  deckId   = id;
  category = cat;
  deckName = name;
  _sessionReviewedCount = 0;
  _sessionReviewedIds = [];
  _sessionTotalMs = 0;
  _sessionRatedCount = 0;
  _updateAvgTimeBadge();
  _updateReviewRRBadge(id);

  // A background story is already generating for this deck/category (the user
  // clicked "Continue in background"): re-open its loading screen instead of the
  // setup modal — we must not start a second generation.
  if (!noStory && !quick) {
    const bgCtx = _bgStories[`${id}/${cat}`];
    if (bgCtx) {
      delete _bgStories[bgCtx.key];
      _resumeBackgroundReview(bgCtx);
      return;
    }
  }

  try {
    if (noStory || quick) {
      await _doStartReview(null, 2);
      return;
    }
    const [{ count, has_story, estimated_tokens }, todayCounts] = await Promise.all([
      api('GET', `/api/story/${deckId}/${category}/count${_langQP('?')}`),
      api('GET', `/api/today/${deckId}/${category}${_langQP('?')}`),
    ]);
    const learning = todayCounts?.counts?.learning_future || 0;
    if (has_story || count === 0) {
      await _doStartReview(null, 2);
    } else {
      await openStorySetup(count, { learningCount: learning, estimatedTokens: estimated_tokens });
    }
  } catch (e) {
    await _startQuickFallback(e.message);
    return;
  }
}

// ── Background story generation ─────────────────────────────────────────────
// Let the user leave the "Generating story…" screen and do other things (check
// stats, review another deck) while the story finishes generating server-side,
// then notify them with a clickable banner when it's ready.
const _BG_LEFT = Symbol('bg-left');
let _bgStories = {};            // key → resumeCtx, generating while the user is elsewhere
let _bgStoryPoller = null;      // interval polling those keys for readiness
let _bgActiveResume = null;     // resumeCtx for the story currently on the loading screen
let _bgLeaveRequested = false;  // set when the user clicks "Continue in background"

function _showLoadingBgButton() {
  const b = document.getElementById('loading-bg-btn');
  if (b) b.style.display = 'block';
  const c = document.getElementById('loading-cancel-btn');
  if (c) { c.style.display = 'block'; c.disabled = false; c.textContent = 'Cancel ✕'; }
}
function _hideLoadingBgButton() {
  const b = document.getElementById('loading-bg-btn');
  if (b) b.style.display = 'none';
  const c = document.getElementById('loading-cancel-btn');
  if (c) c.style.display = 'none';
}

// User clicked "Cancel" (#828): tell the server to stop and go back to the deck
// list WITHOUT registering the run in _bgStories. The difference from
// "Continue in background" is the whole point — that one keeps spending money
// and ends in a "Story ready" banner for a story nobody wants.
async function _cancelStoryGeneration() {
  if (!_bgActiveResume) return;
  const ctx = _bgActiveResume;
  const btn = document.getElementById('loading-cancel-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Cancelling…'; }
  // Stop our own polling first so the in-flight _pollBackgroundStory resolves
  // to _BG_LEFT and its caller returns instead of racing us to showView().
  _bgLeaveRequested = true;
  _bgActiveResume = null;
  // A regenerate's POST is still in flight and will resolve regardless. If the
  // server got the flag too late and hands back a finished story, this tells
  // _doRegenerateStory to drop it rather than yank the user's screen around.
  if (ctx.isRegen) { _regenCancelRequested = true; _regenBgRequested = false; }
  _stopFakeProgress();
  _stopStoryProgressPoll();
  try {
    await api('POST', `/api/story/${ctx.storyDeckId}/${ctx.storyCategory}/cancel${_langQP('?')}`);
  } catch (e) {
    // The generation may well have finished a second ago — either way the user
    // asked to leave, so leaving is the honest response. Say what happened
    // rather than pretending the cancel went through.
    showNotice('Could not reach the server to cancel — generation may still be running.');
  }
  _hideLoadingBgButton();
  // A regenerate was started from inside a review session, so that is where
  // cancelling belongs — the old story is untouched and still reviewable.
  // Dropping the user on the deck list would end a session they never left.
  if (ctx.isRegen) { showView('review'); showFront(); } else { loadDecks(); }
}

// Poll the background story endpoint until it returns a story (or an error dict),
// or the user clicks "Continue in background" (→ resolves to _BG_LEFT).
async function _pollBackgroundStory(bgUrl) {
  while (true) {
    if (_bgLeaveRequested) return _BG_LEFT;
    const r = await api('GET', bgUrl);
    if (_bgLeaveRequested) return _BG_LEFT;
    if (r && r.generating) { await new Promise(res => setTimeout(res, 1500)); continue; }
    return r;  // story | null | { error }
  }
}

// User clicked "Continue in background": register the in-flight story, start the
// global readiness poller, and return to the deck list. Generation keeps running.
function _continueStoryInBackground() {
  if (!_bgActiveResume) return;
  // Regenerate (#868) takes a different exit: its POST is already in flight and
  // resolves with the finished story, so there is nothing for _bgStories /
  // _ensureBgStoryPoller (which poll `background=true` GETs) to watch. Hand the
  // user back to their review session and let _doRegenerateStory's own promise
  // put up the "ready" banner when it lands.
  if (_bgActiveResume.isRegen) {
    _regenBgRequested = true;
    _bgActiveResume = null;
    _stopFakeProgress();
    _stopStoryProgressPoll();
    _hideLoadingBgButton();
    _showRegenBgBanner();
    showView('review');
    showFront();
    return;
  }
  _bgLeaveRequested = true;
  _bgStories[_bgActiveResume.key] = _bgActiveResume;
  _bgActiveResume = null;
  _stopFakeProgress();
  _stopStoryProgressPoll();
  _hideLoadingBgButton();
  _ensureBgStoryPoller();
  loadDecks();
}

// Set while a regenerate started from the review screen finishes in the
// background. Read once by _doRegenerateStory when its POST resolves: the user
// is back on their card, so the new story must arrive as a banner they can
// accept, never as a view switch under their hands.
let _regenBgRequested = false;
// Set by _cancelStoryGeneration for a regenerate run; see there.
let _regenCancelRequested = false;

function _showRegenBgBanner() {
  const banner = document.getElementById('bg-story-banner');
  if (!banner) return;
  banner.classList.add('bg-banner-progress');
  banner.textContent = '⏳ Regenerating story in the background — keep reviewing…';
  banner.onclick = null;
  banner.style.display = 'block';
}

function _hideRegenBgBanner() {
  const banner = document.getElementById('bg-story-banner');
  if (!banner) return;
  banner.classList.remove('bg-banner-progress');
  banner.onclick = null;
  banner.style.display = 'none';
}

// Poll every in-flight background story for readiness; banner the user when ready.
function _ensureBgStoryPoller() {
  if (_bgStoryPoller) return;
  _bgStoryPoller = setInterval(async () => {
    const keys = Object.keys(_bgStories);
    if (keys.length === 0) { clearInterval(_bgStoryPoller); _bgStoryPoller = null; return; }
    for (const key of keys) {
      const ctx = _bgStories[key];
      let s = null;
      try {
        s = await api('GET', `/api/story/${ctx.storyDeckId}/${ctx.storyCategory}?no_generate=true${_langQP('&')}`);
      } catch (_) { continue; }
      if (s && s.sentences) {            // ready
        delete _bgStories[key];
        _showStoryReadyBanner(ctx);
      }
    }
  }, 3000);
}

function _showStoryReadyBanner(ctx) {
  const el = document.getElementById('bg-story-banner');
  if (!el) return;
  el.textContent = `📖 Story ready — ${ctx.deckName} · click to review`;
  el.style.display = 'block';
  el.onclick = () => { el.style.display = 'none'; _resumeBackgroundReview(ctx); };
}

// Resume a session whose background story is now cached → starts instantly.
function _resumeBackgroundReview(ctx) {
  _storyLoadingSources = ctx.sources || [];   // #929
  deckId     = ctx.deckId;
  category   = ctx.category;
  deckName   = ctx.deckName;
  rootDeckId = ctx.rootDeckId;
  quickMode  = false;
  _doStartReview(ctx.topic, ctx.maxHsk, ctx.model, ctx.grammarFocus, ctx.grammarPct, ctx.mode, ctx.chapterIds, null, ctx.episodeIds, ctx.bookChapterId);
}

async function _doStartReview(topic, maxHsk, model, grammarFocus, grammarPct, mode = 'story', chapterIds = null, articles = null, episodeIds = null, bookChapterId = null) {
  if (quickMode) {
    setLoading('Loading audio…', true);
    try {
      const todayData = await api('GET', `/api/today/${deckId}/${category}${_langQP('?')}`);
      if (!todayData.card) { showView('done'); return; }
      try {
        await fetch(`/api/preload-session/${deckId}/${category}?quick=true${_langQP('&')}`, { method: 'POST' });
      } catch (_) {}
      showView('review');
      loadCard(todayData.card, todayData.counts);
    } catch (e) {
      showError('Failed to start session: ' + e.message);
      showView('decks');
    }
    return;
  }
  setLoading('Generating story…', true);
  setLoadingStep(10, null, 'Sending request to AI…');
  _startFakeProgress(10, 55, 45000);
  try {
    const storyDeckId = rootDeckId || deckId;
    const storyCategory = rootDeckId ? 'unified' : category;
    _startStoryProgressPoll(storyDeckId, storyCategory);

    // Capture enough context to resume this exact session if the user chooses to
    // let the story finish generating in the background and walk away.
    const resumeCtx = {
      key: `${storyDeckId}/${storyCategory}`,
      deckId, category, deckName, rootDeckId, storyDeckId, storyCategory,
      topic, maxHsk, model, grammarFocus, grammarPct, mode, chapterIds, episodeIds, bookChapterId,
      sources: _storyLoadingSources,   // #929: re-shown when this run is re-opened
    };
    _bgLeaveRequested = false;
    _bgActiveResume = resumeCtx;
    _showLoadingBgButton();

    const storyUrl = `/api/story/${storyDeckId}/${storyCategory}` + _storyParams(topic, maxHsk, model, grammarFocus, grammarPct, mode, chapterIds, episodeIds, bookChapterId);
    const bgUrl = storyUrl + (storyUrl.includes('?') ? '&' : '?') + 'background=true';
    let todayData, storyData;
    try {
      todayData = await api('GET', `/api/today/${deckId}/${category}${_langQP('?')}`);
      storyData = await _fetchStoryOrNewsRegen(storyDeckId, storyCategory, topic, maxHsk, model,
        grammarFocus, grammarPct, mode, chapterIds, articles, bgUrl, episodeIds, bookChapterId);
    } catch (e) {
      await _startQuickFallback(e.message);
      return;
    }

    // User clicked "Continue in background" — we've already returned to the deck list.
    if (storyData === _BG_LEFT) return;

    _hideLoadingBgButton();
    _stopFakeProgress(); _stopStoryProgressPoll();
    setLoadingStep(65, null, 'Story received, processing…');
    story = await _resolveStory(storyData, storyDeckId, storyCategory, topic, maxHsk, grammarFocus, grammarPct, mode);

    if (!todayData.card) {
      showView('done');
      return;
    }

    const sentenceCount = story?.sentences?.length ?? 0;
    setLoadingStep(70, 'Story ready!',
      sentenceCount > 0 ? `Generating audio — 0 / ${sentenceCount} sentences…` : 'Loading audio…');
    await _preloadWithProgress(deckId, category, (done, total) => {
      const pct = 70 + Math.round((done / total) * 28);
      setLoadingStep(pct, null, `Generating audio — ${done} / ${total} sentences…`);
    });

    _showLoadingSuccess('Ready!');
    await new Promise(r => setTimeout(r, 300));
    showView('review');
    loadCard(todayData.card, todayData.counts);
  } catch (e) {
    await _startQuickFallback(e.message);
  }
}

// Story generation failed (network hiccup or AI provider unreachable). Rather than
// kicking the user back to the deck list with nothing to review, silently fall back
// to words-only quick mode so they can keep going (issue #545). /api/today hits the
// local DB, so it works even when only the AI provider is down. Only if that also
// fails (server itself unreachable) do we surface the error and return to the decks.
async function _startQuickFallback(reason) {
  _stopFakeProgress(); _stopStoryProgressPoll(); _hideLoadingBgButton();
  _bgLeaveRequested = false; _bgActiveResume = null;
  quickMode = true;
  story = null;
  sentence = null;
  // Shown on the banner above the review view. If the fallback itself fails below,
  // showError() overwrites this same element, so no contradictory message lingers.
  showNotice('Story unavailable — words-only mode.');
  try {
    if (rootDeckId) {
      // Mixed (all-category) session — reuse its own words-only path.
      await _doStartReviewMixed(null, 2, null, null, 50, 'story', true);
      return;
    }
    setLoading('Loading words…', true);
    const todayData = await api('GET', `/api/today/${deckId}/${category}${_langQP('?')}`);
    if (!todayData.card) { showView('done'); return; }
    try {
      await fetch(`/api/preload-session/${deckId}/${category}?quick=true${_langQP('&')}`, { method: 'POST' });
    } catch (_) {}
    showView('review');
    loadCard(todayData.card, todayData.counts);
  } catch (e) {
    _showLoadingError('Failed to load session', e.message);
    await new Promise(r => setTimeout(r, 2000));
    showError('Failed to start session: ' + e.message);
    showView('decks');
  }
}

// Paste mode with pasted texts goes through the regenerate POST body (texts are
// too large for a GET query string). Everything else falls back to the normal
// GET/poll flow: it returns today's cached story if one exists (e.g. resuming
// a session).
async function _fetchStoryOrNewsRegen(storyDeckId, storyCategory, topic, maxHsk, model,
                                      grammarFocus, grammarPct, mode, chapterIds, articles, bgUrl,
                                      episodeIds, bookChapterId) {
  if (mode === 'paste' && articles && articles.length) {
    const url = `/api/story/${storyDeckId}/${storyCategory}/regenerate`
      + _storyParams(topic, maxHsk, model, grammarFocus, grammarPct, mode, chapterIds, episodeIds, bookChapterId);
    return api('POST', url, { articles });
  }
  return _pollBackgroundStory(bgUrl);
}

// episodeIds: array of item ids for knowledge mode (issue #752, multi-select —
// previously a single episodeId / `episode_id` param). Backend accepts the new
// comma-joined `episode_ids` and still accepts the old singular `episode_id`,
// but the frontend only ever sends the former now.
function _storyParams(topic, maxHsk, model, grammarFocus, grammarPct, mode, chapterIds, episodeIds, bookChapterId) {
  const p = new URLSearchParams();
  if (topic)                              p.set('topic', topic);
  if (maxHsk !== 3)                       p.set('max_hsk', maxHsk);
  if (model && model !== 'deepseek-v4-flash') p.set('model', model);
  if (grammarFocus)                       p.set('grammar_focus', grammarFocus);
  if (grammarFocus && grammarPct !== 75)  p.set('grammar_pct', grammarPct);
  if (mode && mode !== 'story')           p.set('mode', mode);
  if (chapterIds && chapterIds.length)    p.set('chapter_ids', chapterIds.join(','));
  if (episodeIds && episodeIds.length)    p.set('episode_ids', episodeIds.join(','));
  if (bookChapterId)                      p.set('book_chapter_id', bookChapterId);
  // Words per AI call (issue #563 podcast-only, #574 all modes) — persisted
  // per mode in localStorage (like setupModel) rather than yet another
  // positional parameter through every call chain. Unset = mode default.
  const _bs = _savedBatchSize(mode);
  if (_bs)                                p.set('batch_size', _bs);
  // Active language tab (issue #436) — only sent once more than one language is
  // in use, so a pure-Chinese install's requests stay byte-identical.
  if (_langQ())                           p.set('lang', activeLang());
  const s = p.toString();
  return s ? '?' + s : '';
}

// ── Start mixed (all-category) review session ────────────────────────────────
async function startReviewMixed(id, name, noStory = false, quick = false) {
  navPush('review');
  quickMode  = quick;
  rootDeckId = id;
  deckId     = id;
  deckName   = name;
  story      = null;
  _sessionReviewedCount = 0;
  _sessionReviewedIds = [];
  _sessionTotalMs = 0;
  _sessionRatedCount = 0;
  _updateAvgTimeBadge();
  _updateReviewRRBadge(id);
  try {
    const todayData = await api('GET', `/api/today-mixed/${id}${_langQP('?')}`);
    if (!todayData.card) {
      rootDeckId = null;
      showView('done');
      return;
    }
    if (noStory || quick) {
      await _doStartReviewMixed(null, 2, null, null, 50, 'story', true);
      return;
    }
    const c = todayData.counts;
    const total = (c.new || 0) + (c.learning || 0) + (c.review || 0);
    const learning = c.learning_future || 0;
    const firstCat = todayData.card.category;
    const { has_story, estimated_tokens } = await api('GET', `/api/story/${id}/unified/count${_langQP('?')}`);
    if (has_story) {
      await _doStartReviewMixed(null, 2, null, null, 50, 'story');
    } else {
      openStorySetup(total, { isMixed: true, learningCount: learning, estimatedTokens: estimated_tokens });
    }
  } catch (e) {
    await _startQuickFallback(e.message);
  }
}

async function _doStartReviewMixed(topic, maxHsk, model, grammarFocus, grammarPct, mode = 'story', noStory = false, chapterIds = null, articles = null, episodeIds = null, bookChapterId = null) {
  setLoading(noStory ? 'Loading…' : 'Generating stories…', !noStory);
  if (!noStory) {
    setLoadingStep(10, null, 'Sending request to AI…');
    _startFakeProgress(10, 55, 45000);
    _startStoryProgressPoll(rootDeckId, 'unified');
  }
  try {
    const todayData = await api('GET', `/api/today-mixed/${rootDeckId}${_langQP('?')}`);
    if (!todayData.card) {
      _stopFakeProgress(); _stopStoryProgressPoll();
      rootDeckId = null;
      showView('done');
      return;
    }
    category = todayData.card.category;

    if (!noStory) {
      // Load a single unified story covering all categories (1 AI call instead of 3)
      try {
        story = (mode === 'paste' && articles && articles.length)
          ? await api('POST', `/api/story/${rootDeckId}/unified/regenerate` + _storyParams(topic, maxHsk, model, grammarFocus, grammarPct, mode, chapterIds, episodeIds, bookChapterId), { articles })
          : await api('GET', `/api/story/${rootDeckId}/unified` + _storyParams(topic, maxHsk, model, grammarFocus, grammarPct, mode, chapterIds, episodeIds, bookChapterId));
      } catch (e) {
        await _startQuickFallback(e.message);
        return;
      }
      _stopFakeProgress(); _stopStoryProgressPoll();
      fetch(`/api/preload-session/${rootDeckId}/unified${_langQP('?')}`, { method: 'POST' }).catch(() => {});
    }

    if (!noStory) {
      const sentenceCount = story?.sentences?.length ?? 0;
      setLoadingStep(70, 'Story ready!',
        sentenceCount > 0 ? `Generating audio — 0 / ${sentenceCount} sentences…` : 'Loading audio…');
      await _preloadWithProgress(rootDeckId, category, (done, total) => {
        const pct = 70 + Math.round((done / total) * 28);
        setLoadingStep(pct, null, `Generating audio — ${done} / ${total} sentences…`);
      });
      _showLoadingSuccess('Ready!');
      await new Promise(r => setTimeout(r, 300));
    } else {
      try {
        await fetch(`/api/preload-session/${rootDeckId}/${category}${_langQP('?')}`, { method: 'POST' });
      } catch (_) {}
    }
    showView('review');
    loadCard(todayData.card, todayData.counts);
  } catch (e) {
    _stopFakeProgress(); _stopStoryProgressPoll();
    _showLoadingError('Failed to load session', e.message);
    await new Promise(r => setTimeout(r, 2500));
    showError('Failed to start session: ' + e.message);
    rootDeckId = null;
    showView('decks');
  }
}

// ── Unfinished-deck start modal (scope + story choice) ───────────────────────
function openUnfinishedModal() {
  // Pre-select the persisted scope and last story mode
  document.querySelector(`input[name="unf-scope"][value="${_unfinishedScope}"]`).checked = true;
  document.querySelector(`input[name="unf-story"][value="${_unfinishedStoryMode}"]`).checked = true;
  document.getElementById('unfinished-modal-overlay').style.display = 'block';
  document.getElementById('unfinished-modal').style.display = 'flex';
}

function closeUnfinishedModal() {
  document.getElementById('unfinished-modal-overlay').style.display = 'none';
  document.getElementById('unfinished-modal').style.display = 'none';
}

function confirmUnfinishedStart() {
  _unfinishedScope     = document.querySelector('input[name="unf-scope"]:checked')?.value || 'unfinished';
  _unfinishedStoryMode = document.querySelector('input[name="unf-story"]:checked')?.value || 'existing';
  localStorage.setItem('unfinishedScope', _unfinishedScope);
  localStorage.setItem('unfinishedStoryMode', _unfinishedStoryMode);
  closeUnfinishedModal();
  startReviewUnfinished();
}

// ── Start "Unfinished Cards" review session ───────────────────────────────────
async function startReviewUnfinished() {
  navPush('review');
  deckName = 'Unfinished Cards';
  story    = null;
  _sessionReviewedCount = 0;
  _sessionTotalMs = 0;
  _sessionRatedCount = 0;
  _updateAvgTimeBadge();
  try {
    const counts = await api('GET', `/api/today-unfinished?scope=${_unfinishedScope}${_langQP('&')}`);
    if (!counts.card) {
      showView('done');
      return;
    }
    await _doStartReviewUnfinished(null, 3, null);
  } catch (e) {
    showError('Failed to start session: ' + e.message);
    showView('decks');
  }
}

async function _doStartReviewUnfinished(topic, maxHsk, model, grammarFocus, grammarPct, mode = 'story', chapterIds = null, articles = null, episodeIds = null, bookChapterId = null) {
  unfinishedMode = true;
  setLoading('Loading cards…');
  // In "existing" story mode, never trigger generation — fetch cached stories only.
  const noGen = _unfinishedStoryMode === 'existing';
  try {
    const [combos, todayData] = await Promise.all([
      api('GET', `/api/today-unfinished-decks?scope=${_unfinishedScope}${_langQP('&')}`),
      api('GET', `/api/today-unfinished?scope=${_unfinishedScope}${_langQP('&')}`),
    ]);
    if (!todayData.card) {
      unfinishedMode = false;
      showView('done');
      return;
    }
    category = todayData.card.category;
    const firstDeckId = todayData.card.deck_id;
    // Load the first card's deck story (generate only when story mode = "new")
    try {
      if (!noGen && mode === 'paste' && articles && articles.length) {
        story = await api('POST', `/api/story/${firstDeckId}/unified/regenerate`
          + _storyParams(topic, maxHsk, model, grammarFocus, grammarPct, mode, chapterIds, episodeIds, bookChapterId), { articles });
      } else {
        let url = `/api/story/${firstDeckId}/unified` + _storyParams(topic, maxHsk, model, grammarFocus, grammarPct, mode, chapterIds, episodeIds, bookChapterId);
        if (noGen) url += (url.includes('?') ? '&' : '?') + 'no_generate=true';
        story = await api('GET', url);
      }
    } catch (_) {}
    if (!noGen) fetch(`/api/preload-session/${firstDeckId}/unified${_langQP('?')}`, { method: 'POST' }).catch(() => {});
    showView('review');
    loadCard(todayData.card, todayData.counts);
  } catch (e) {
    showError('Failed to start session: ' + e.message);
    unfinishedMode = false;
    showView('decks');
  }
}

// ── Load a card ─────────────────────────────────────────────────────────────
function loadCard(c, counts) {
  card = c;
  wordDetails = null;
  renderReviewCatRow(); // clear circles immediately when new card loads

  // In unfinished mode each card may belong to a different deck/category
  if (unfinishedMode) {
    category = c.category;
    deckId   = c.deck_id;
  }

  // Update progress counts
  _lastCounts = counts;
  document.getElementById('cnt-new').textContent = counts.new;
  setLrnCounter('cnt-lrn', 'cnt-lrn-soon', counts);
  document.getElementById('cnt-rev').textContent = counts.review;

  // Highlight the active state item — same classification as the backend
  // counts: a review card below learned_interval is still "learning"
  const stateToItemId = { new: 'cnt-item-new', learning: 'cnt-item-lrn', review: 'cnt-item-rev', relearn: 'cnt-item-lrn' };
  ['cnt-item-new', 'cnt-item-lrn', 'cnt-item-rev'].forEach(id => document.getElementById(id)?.classList.remove('cnt-item-active'));
  const youngReview = c?.state === 'review' && (c.interval || 0) < (c.learned_interval ?? 4);
  const activeStateId = youngReview ? 'cnt-item-lrn' : stateToItemId[c?.state];
  if (activeStateId) document.getElementById(activeStateId)?.classList.add('cnt-item-active');

  // Per-category breakdown (mixed/all mode only)
  const byCatEl = document.getElementById('cnt-by-cat');
  if (counts.by_cat && byCatEl) {
    byCatEl.style.display = 'flex';
    const catMap = {r: 'reading', l: 'listening', c: 'creating'};
    for (const [prefix, cat] of Object.entries(catMap)) {
      const cc = counts.by_cat[cat] || {new: 0, learning: 0, review: 0};
      document.getElementById(`cnt-${prefix}-new`).textContent = cc.new;
      setLrnCounter(`cnt-${prefix}-lrn`, `cnt-${prefix}-lrn-soon`, cc);
      document.getElementById(`cnt-${prefix}-rev`).textContent = cc.review;
    }
    // A category disabled via preset is absent from by_cat → hide its tile.
    // One rule for all three (#869): the reading-only special case left "创 0·0·0"
    // sitting in the top bar for a category that can never produce a card.
    for (const cat of Object.values(catMap)) {
      const item = document.getElementById(`cnt-cat-${cat}`);
      if (item) item.style.display = counts.by_cat[cat] ? '' : 'none';
    }
    // Highlight the active category item
    ['cnt-cat-reading', 'cnt-cat-listening', 'cnt-cat-creating'].forEach(id => document.getElementById(id)?.classList.remove('cnt-cat-item-active'));
    const activeCat = c?.category;
    if (activeCat) document.getElementById(`cnt-cat-${activeCat}`)?.classList.add('cnt-cat-item-active');
  } else if (byCatEl) {
    byCatEl.style.display = 'none';
  }

  // Set interval labels on rating buttons (e.g. "1m", "10m", "4d")
  const iv = card.intervals || {};
  [1, 2, 3, 4].forEach(r => {
    document.getElementById(`int-${r}`).textContent = iv[r] || '';
  });

  // Pre-fill the "note for next time" left on this card last time (if any)
  const noteInput = document.getElementById('next-note-input');
  if (noteInput) {
    noteInput.value = card.next_note || '';
    noteInput.classList.toggle('has-note', !!(card.next_note || '').trim());
  }

  // Find sentence for this card's word.
  // A card that was rated Again carries a freshly regenerated sentence (again_sentence) —
  // prefer it so the reappearing card shows something new instead of the old story sentence.
  // Otherwise look it up in the story; if no match, leave null and renderSentence() shows just the word.
  sentence = card.again_sentence
    || story?.sentences?.find(s => s.word_ids?.includes(card.word_id))
    || null;

  // Warm the browser cache for the flip audio right away (#554); the async story
  // path below re-prefetches once the real sentence arrives.
  _prefetchSentenceAudio();
  _prefetchStoryAudio(story?.sentences);

  // In unfinished mode or mixed mode: story may be from a different deck/category.
  // Async-load the correct story and update the display when it arrives.
  if (!sentence && (unfinishedMode || rootDeckId) && !quickMode) {
    const snap = c;
    const storyDeckId = unfinishedMode ? c.deck_id : rootDeckId;
    // Push the freshly-found `sentence` into the visible UI (reading / cloze / sentence-note).
    const applySentenceToUI = () => {
      _updateStoryInfoRow();
      const isListening  = category === 'listening';
      const isCreating   = category === 'creating';
      const isSentenceNt = card.note_type === 'sentence';
      const isCloze      = isCreating && !isSentenceNt;
      if (!isListening && !isCreating) {
        // Reading: update sentence with full highlighted sentence
        const sentFront = document.getElementById('sentence-front');
        if (sentFront.style.display !== 'none') sentFront.innerHTML = renderSentence();
      } else if (isCloze) {
        // Word bank: sentence just loaded — update hint text and rebuild token bank.
        // Visibility (hidden unless "always show translation" is on) is controlled
        // by toggleCreatingFrontTranslation() / the t key, not here (#515).
        const enFront = document.getElementById('sentence-en-front');
        enFront.textContent = sentence.sentence_de || sentence.sentence_fr || sentence.sentence_en || '';
        enFront.style.display = _alwaysTranslation && enFront.textContent ? 'block' : 'none';
        _syncFrontTransToggle();
        if (document.getElementById('word-bank-wrap').style.display !== 'none') {
          renderWordBankUI();
        }
      } else if (isCreating && isSentenceNt) {
        // Sentence notes: update translation prompt text (visibility unchanged here).
        const inp = document.getElementById('sentence-en-front');
        inp.textContent = sentence.sentence_de || sentence.sentence_fr || '';
        _syncFrontTransToggle();
      }
      // Real sentence now known — prefetch its audio so the flip plays instantly (#554).
      _prefetchSentenceAudio();
    };
    // In unfinished "existing story" mode, only fetch a cached story (never generate).
    const unfNoGen = unfinishedMode && _unfinishedStoryMode === 'existing';
    const storyUrl = `/api/story/${storyDeckId}/unified` + (unfNoGen ? `?no_generate=true${_langQP('&')}` : (_langQ() ? `?${_langQ()}` : ''));
    fetch(storyUrl)
      .then(r => r.ok ? r.json() : null)
      .then(s => {
        if (card !== snap) return;
        if (!unfNoGen) fetch(`/api/preload-session/${storyDeckId}/unified${_langQP('?')}`, { method: 'POST' }).catch(() => {});
        if (s?.sentences) {
          story    = s;
          sentence = story.sentences.find(s => s.word_ids?.includes(card.word_id)) || null;
          _prefetchStoryAudio(story.sentences);
        }
        if (sentence) {
          applySentenceToUI();
        } else {
          // Word not in this story (e.g. cross-day card in mixed review): fall back to
          // the word's own most recent sentence, which carries the German translation.
          fetch(`/api/sentence-for-word/${card.word_id}`)
            .then(r => r.ok ? r.json() : null)
            .then(d => {
              if (card !== snap || !d?.sentence) return;
              sentence = d.sentence;
              applySentenceToUI();
            }).catch(() => {});
        }
        // Update listening hint now that sentence is loaded
        if (snap.category === 'listening' && document.getElementById('side-back').style.display === 'none') {
          _initListenHint();
        }
        // Auto-play deferred from loadCard: play now that story is loaded
        if (snap.category === 'listening' && document.getElementById('side-back').style.display === 'none') {
          if (card === snap) playSentence();
        }
      }).catch(() => {
        // On fetch error, still play audio (falls back to word_zh)
        if (card === snap && snap.category === 'listening' &&
            document.getElementById('side-back').style.display === 'none') {
          if (card === snap) playSentence();
        }
      });
  }

  // Update story info row (sentence counter + topic)
  _updateStoryInfoRow();

  // Update card type badge (note type only — category shown by circles)
  const noteLabel = { vocabulary: 'Word', sentence: 'Sentence', chengyu: '成语', expression: '表达' }[card.note_type] || card.note_type;
  document.getElementById('card-type-badge').textContent = noteLabel;

  // Story-mode badge: the mode name now lives in the story-info-row next to the
  // sentence counter + date (issue #452), so this separate badge stays hidden to
  // avoid showing the mode name twice.
  document.getElementById('card-mode-badge').style.display = 'none';

  // Deck path bar
  const deckPath = document.getElementById('card-deck-path');
  if (card.deck_path) {
    deckPath.textContent = card.deck_path.replace(/_/g, ' ');
    deckPath.style.display = 'block';
  } else {
    deckPath.style.display = 'none';
  }

  // Level badge — zh shows "HSK n" ("HSK -" when unknown, click to AI-fill);
  // CEFR languages show "B1" etc. when the entry has a level, otherwise the
  // badge is hidden (AI-enrich is a Chinese-only feature) — issue #596.
  const hskBadge = document.getElementById('card-hsk-badge');
  const _cardLang = currentCardLang();
  if (_cardLang !== 'zh') {
    if (card.hsk_level) {
      hskBadge.textContent = levelLabel(_cardLang, card.hsk_level);
      hskBadge.classList.remove('hsk-unknown');
      hskBadge.disabled = true;
      hskBadge.style.display = 'inline';
    } else {
      hskBadge.style.display = 'none';
    }
  } else {
    hskBadge.textContent = card.hsk_level ? `HSK ${card.hsk_level}` : 'HSK -';
    hskBadge.classList.toggle('hsk-unknown', !card.hsk_level);
    hskBadge.disabled = false;
    hskBadge.style.display = 'inline';
  }

  // Reset pinyin (clear content + hide revealed state)
  const _pr = document.getElementById('pinyin-row');
  _pr.innerHTML = '';
  _pr.dataset.loadedFor = '';
  _pr.classList.remove('pinyin-revealed');

  // Close modals if open
  closeEditCard();
  closeStoryModal();
  document.getElementById('review-card-menu').style.display = 'none';
  const reviewSuspendBtn = document.getElementById('review-suspend-btn');
  if (reviewSuspendBtn) reviewSuspendBtn.textContent = (c.state === 'suspended') ? 'Unsuspend' : 'Suspend';

  // Preload full word details for the back side (local DB — near-instant)
  fetch(`/api/word/${c.word_id}`)
    .then(r => r.ok ? r.json() : null)
    .then(d => {
      if (!d) return;
      wordDetails = d;
      // If back is already showing (user flipped before fetch completed), re-render with full data
      if (document.getElementById('side-back').style.display !== 'none') {
        // Re-render interactive word-zh now that components are available
        const nt = wordDetails?.note_type || card.note_type;
        const wzEl = document.getElementById('word-zh');
        const isMultiWord = nt === 'sentence' || nt === 'chengyu' || nt === 'expression';
        if (isMultiWord && wordDetails?.components?.length) {
          wzEl.innerHTML = renderInteractiveZh(card.word_zh, wordDetails.components);
        }
        renderVocabDetail();
        renderNotesSection();
        renderConjugationSection();
        renderInflectionSection();
        _callRenderWordAnalysis();
        // MOST IMPORTANT: Re-render category row with actual card data
        renderReviewCatRow();
      }
    })
    .catch(() => {});

  showFront();
  _startTimer();
  _loadCardTile(c.id, c.category);

  // Auto-play audio for the listening category.
  // If sentence is missing and a story fetch is in flight, defer to the fetch callback above.
  if (category === 'listening') {
    if (!sentence && (unfinishedMode || rootDeckId)) {
      // Deferred — fetch callback will call playSentence() once story is loaded
    } else {
      const snap = c;
      if (card === snap) playSentence();
    }
  }
}

// ── Front of card ───────────────────────────────────────────────────────────
function showFront() {
  const isListening  = category === 'listening';
  const isCreating   = category === 'creating';
  const isSentence   = card.note_type === 'sentence';

  document.getElementById('review-cat-row').innerHTML = '';
  document.getElementById('side-front').style.display = 'flex';
  document.getElementById('side-front').style.flexDirection = 'column';
  document.getElementById('side-front').style.gap = '16px';
  document.getElementById('side-back').style.display = 'none';
  const _mascot = document.getElementById('front-mascot');
  if (_mascot) {
    _mascot.style.display = 'flex';
    // Listening: the cat is the big replay target (the header 🔊 is tiny on phones).
    _mascot.classList.toggle('mascot-playable', isListening);
    const _cap = document.getElementById('mascot-caption');
    if (_cap) _cap.textContent = isListening ? '点我再听一遍' : '专注 — 准备好了就翻牌';
  }
  const _vc = document.getElementById('vocab-content');
  if (_vc) _vc.style.display = 'none';

  // Listening: play button lives in the card header (same spot as on the back)
  document.getElementById('meta-play-btn').style.display = isListening ? 'flex' : 'none';
  _listenCount = 0;
  _updateListenCounters();

  // Listening hint slider (sentence lives above the sentence area; the slider
  // row itself is docked below Show Answer, #1033)
  const hintWrap = document.getElementById('listen-hint-wrap');
  const hintSliderWrap = document.getElementById('listen-hint-slider-wrap');
  if (isListening) {
    hintWrap.style.display = 'flex';
    hintSliderWrap.style.display = 'block';
    _initListenHint();
  } else {
    hintWrap.style.display = 'none';
    hintSliderWrap.style.display = 'none';
  }

  // Word bank mode: creating category for non-sentence notes (disabled in quick mode)
  const isCloze = isCreating && !isSentence && !quickMode;

  // Reading only: Chinese sentence on front
  const sentFront = document.getElementById('sentence-front');
  sentFront.style.display = !isListening && !isCreating ? 'flex' : 'none';
  if (!isListening && !isCreating) sentFront.innerHTML = renderSentence();

  // Kontextsummary: the Chinese sentence is clickable — open the sentence's
  // source article in a new tab (issue #444). Context + source line are rendered
  // by _renderNewsFront (respects the news-flow display-language toggle, #452).
  const _sourceUrl = sentence?.source_url || '';
  const _sentClickable = !isListening && !isCreating && !!_sourceUrl;
  sentFront.classList.toggle('clickable-sentence', _sentClickable);
  // Knowledge mode (#931): source_url has been an in-app hash link since #790,
  // so window.open() here threw Daniel out of the review session into a second
  // tab of the app. Same fix as the back side — pop the summary modal, which
  // closes back onto the card.
  const _frontEpId = _episodeIdFromSourceUrl(_sourceUrl);
  sentFront.onclick = !_sentClickable ? null
    : _frontEpId !== null
      ? () => openKnowledgeSummaryPopup(_frontEpId, sentence?.source_title || '')
      : () => window.open(_sourceUrl, '_blank', 'noopener');
  _renderNewsFront();

  // Creating: show translation row + appropriate input. The translation text
  // itself starts hidden (no blur-on-hover any more, #515) — press t to reveal
  // it, or tap the 🇩🇪 button on mobile; shown by default only when the
  // persistent "always show translation" preference is on.
  document.getElementById('sentence-en-front-row').style.display = isCreating ? 'flex' : 'none';
  document.getElementById('sentence-en-front').style.display     = 'none';
  document.getElementById('creating-input-wrap').style.display = (isCreating && !isCloze) ? 'flex' : 'none';
  document.getElementById('word-bank-wrap').style.display      = isCloze ? 'flex' : 'none';
  if (isCloze) _initWordBankSlider();

  // Creating: target-word translation hint (🇬🇧/🇫🇷/🇩🇪). Hidden by default;
  // press k to toggle, or the eye icon to make it always show (see toggleWordDef).
  const wordDefHint   = document.getElementById('creating-word-def');
  const wordDefHintWb = document.getElementById('creating-word-def-wb');
  const wordDefRow    = document.getElementById('creating-word-def-row');
  const wordDefRowWb  = document.getElementById('creating-word-def-wb-row');
  if (isCreating) {
    const parts = [];
    if (card.definition) parts.push(`🇬🇧 ${card.definition}`);
    if (card.definition_fr) parts.push(`🇫🇷 ${card.definition_fr}`);
    if (card.definition_de) parts.push(`🇩🇪 ${card.definition_de}`);
    const defText = parts.join('<br>');
    // Only one placement is active (word bank for cloze, plain otherwise); clear the other.
    const activeHint = isCloze ? wordDefHintWb : wordDefHint;
    const activeRow  = isCloze ? wordDefRowWb  : wordDefRow;
    const otherHint  = isCloze ? wordDefHint   : wordDefHintWb;
    const otherRow   = isCloze ? wordDefRow    : wordDefRowWb;
    otherHint.innerHTML = '';
    otherRow.style.display = 'none';
    activeHint.innerHTML = defText;
    activeRow.style.display = defText ? 'flex' : 'none';
    // Hidden unless the persistent "always show" preference is on.
    activeHint.style.display = (defText && _alwaysWordDef) ? 'block' : 'none';
  } else {
    wordDefHint.innerHTML = '';
    wordDefHintWb.innerHTML = '';
    wordDefRow.style.display = 'none';
    wordDefRowWb.style.display = 'none';
  }
  _syncWordDefEye();

  if (isCreating) {
    if (isSentence || quickMode) {
      // Sentence notes or quick mode: text input
      const prompt = isSentence
        ? (card.source_sentence || card.definition || '')
        : (card.definition_de || card.definition || '');
      document.getElementById('sentence-en-front').textContent = prompt;
      document.getElementById('creating-input-label').textContent = isSentence ? 'Your translation in Chinese' : 'Write the word in Chinese';
      document.getElementById('creating-input').placeholder = 'Type here…';
      const inp = document.getElementById('creating-input');
      inp.value = '';
      userInput = '';
      setTimeout(() => inp.focus(), 80);
    } else {
      // Word bank mode: German/French translation as hint; word bank renders below
      document.getElementById('sentence-en-front').textContent = sentence?.sentence_de || sentence?.sentence_fr || sentence?.sentence_en || '';
      userInput = '';
      renderWordBankUI();
    }
    // Default visibility: shown when "always show translation" is on, else hidden
    // (press t to toggle, or tap the 🇩🇪 button on mobile).
    const _enFront = document.getElementById('sentence-en-front');
    _enFront.style.display = (_alwaysTranslation && _enFront.textContent) ? 'block' : 'none';
    _syncFrontTransToggle();
  }

  // Rename reveal button for creating
  document.getElementById('reveal-btn').textContent = isCreating ? 'Check Answer' : 'Show Answer';
}

// ── Answer diff (creating category) ─────────────────────────────────────────
function diffAnswer(userInput, correct, wordZh) {
  if (!userInput) return { html: '(no answer)', pct: 0, bar: '░'.repeat(10) };

  const userChars = [...userInput];
  const corrChars = correct ? [...correct] : [];

  // Find where the target word starts in the user's input
  const wordIdx = userInput.indexOf(wordZh);
  const wordLen = [...wordZh].length;

  // Bag-of-characters: which hanzi from correct appear in user's answer?
  const hanzi = /[\u4e00-\u9fff\u3400-\u4dbf]/;
  const corrSet = new Set(corrChars.filter(ch => hanzi.test(ch)));
  const userSet = new Set(userChars.filter(ch => hanzi.test(ch)));
  const total   = corrSet.size;
  const matched = [...corrSet].filter(ch => userSet.has(ch)).length;
  const pct = total > 0 ? Math.round((matched / total) * 100) : 0;
  const filled = Math.round(pct / 10);
  const bar = '▓'.repeat(filled) + '░'.repeat(10 - filled);

  // Per-character coloring: green if char appears anywhere in correct sentence
  const html = userChars.map((ch, i) => {
    const inWord = wordIdx >= 0 && i >= wordIdx && i < wordIdx + wordLen;
    if (inWord) return `<span class="ch-target">${ch}</span>`;
    if (hanzi.test(ch) && corrSet.has(ch)) return `<span class="ch-match">${ch}</span>`;
    return `<span class="ch-miss">${ch}</span>`;
  }).join('');

  return { html, pct, bar };
}

// ── Back of card ────────────────────────────────────────────────────────────
function revealAnswer() {
  // Keep the timer running on the back side (it freezes at the 40s cap).
  const isCreating = category === 'creating';

  // Capture user input before hiding front
  if (isCreating) {
    const isClozeMode = card.note_type !== 'sentence' && !quickMode;
    if (isClozeMode) {
      // Word bank mode: parse number sequence into reconstructed sentence
      const wbRaw = document.getElementById('word-bank-input').value.trim();
      userInput = _parseWordBankInput(wbRaw).join('');
    } else {
      userInput = document.getElementById('creating-input').value.trim();
    }
  }

  document.getElementById('side-front').style.display = 'none';
  document.getElementById('side-back').style.display  = 'flex';
  document.getElementById('side-back').style.flexDirection = 'column';
  document.getElementById('side-back').style.gap = '16px';
  const _mascotBack = document.getElementById('front-mascot');
  if (_mascotBack) _mascotBack.style.display = 'none';
  const _vcBack = document.getElementById('vocab-content');
  if (_vcBack) _vcBack.style.display = 'block';
  // Back of card = answer revealed, so audio is fine for every mode (including
  // creating, where the front deliberately hides it to avoid leaking the answer).
  document.getElementById('meta-play-btn').style.display = 'flex';
  _updateListenCounters();

  // Pre-load pinyin in background (shown blurred until p is pressed).
  // Pinyin is a Chinese-only concept — pypinyin garbles French text.
  const _pinyinText = sentence?.sentence_zh || card?.word_zh;
  if (_pinyinText && currentCardLang() === 'zh') _loadPinyinRow(_pinyinText);

  const isSentenceNote = card.note_type === 'sentence';

  if (isCreating) {
    // Show answer comparison block; hide normal sentence row
    document.getElementById('creating-answer-section').style.display = 'flex';
    document.getElementById('sentence-row-back').style.display = 'none';
    const matchBar = document.getElementById('answer-match-bar');

    if (!isSentenceNote) {
      // ── Word bank mode: compare reconstructed sentence ────────────────────
      const correctZh = sentence?.sentence_zh || card.word_zh;

      // LCS-based match percentage (handles missing/extra words gracefully)
      const ua = [...userInput], ca = [...correctZh];
      const dp = Array(ua.length + 1).fill(null).map(() => Array(ca.length + 1).fill(0));
      for (let i = 1; i <= ua.length; i++)
        for (let j = 1; j <= ca.length; j++)
          dp[i][j] = ua[i-1] === ca[j-1] ? dp[i-1][j-1] + 1 : Math.max(dp[i-1][j], dp[i][j-1]);
      const lcs = dp[ua.length][ca.length];
      const pct = ca.length > 0 ? Math.round((lcs / ca.length) * 100) : 0;

      // Per-character coloring: target word = blue, others green/red by presence
      const corrSet = new Set(ca);
      const hanzi = /[\u4E00-\u9FFF]/;
      const targetIdx = userInput.indexOf(card.word_zh);
      const targetLen = [...card.word_zh].length;
      let userHtml;
      if (!userInput) {
        userHtml = '<span class="ch-miss">(no answer)</span>';
      } else {
        const chars = [...userInput];
        const tStart = targetIdx >= 0 ? [...userInput.slice(0, targetIdx)].length : -1;
        userHtml = chars.map((ch, i) => {
          if (tStart >= 0 && i >= tStart && i < tStart + targetLen)
            return `<span class="ch-target">${ch}</span>`;
          if (hanzi.test(ch) && corrSet.has(ch)) return `<span class="ch-match">${ch}</span>`;
          return `<span class="ch-miss">${ch}</span>`;
        }).join('');
      }
      document.getElementById('user-answer-text').innerHTML = userHtml;

      if (userInput) {
        const filled = Math.round(pct / 10);
        const bar = '▓'.repeat(filled) + '░'.repeat(10 - filled);
        const color = pct >= 100 ? 'var(--good)' : pct >= 60 ? 'var(--hard)' : 'var(--again)';
        matchBar.innerHTML = `<span class="match-bar" style="color:${color}">${bar} ${pct}%</span>`;
        matchBar.style.display = 'block';
        if (pct >= 100) triggerApplause();
      } else {
        matchBar.style.display = 'none';
      }
      document.getElementById('correct-answer-text').innerHTML = renderSentence();
    } else {
      // ── Sentence notes: full translation comparison (old behaviour) ──────
      const correctZh = card.word_zh;
      const { html: userHtml, pct, bar } = diffAnswer(userInput, correctZh, card.word_zh);
      document.getElementById('user-answer-text').innerHTML = userHtml;
      if (correctZh && userInput) {
        const color = pct >= 80 ? 'var(--good)' : pct >= 50 ? 'var(--hard)' : 'var(--again)';
        matchBar.innerHTML = `<span class="match-bar" style="color:${color}">${bar} ${pct}%</span>`;
        matchBar.style.display = 'block';
        if (pct >= 100) triggerApplause();
      } else {
        matchBar.style.display = 'none';
      }
      document.getElementById('correct-answer-text').innerHTML = renderSentence();
    }
  } else {
    document.getElementById('creating-answer-section').style.display = 'none';
    document.getElementById('sentence-row-back').style.display = 'flex';
    document.getElementById('sentence-back').innerHTML = renderSentence();
  }

  // Sentence notes have no story — hide story button; show German/French translation
  const _sentFrEl = document.getElementById('sentence-fr');
  const _sentDeEl = document.getElementById('sentence-de');
  // Fill in the translation text but keep it hidden by default — press u to toggle.
  if (isSentenceNote) {
    _sentFrEl.textContent = '';
    _sentDeEl.textContent = card.definition || '';
  } else {
    _sentFrEl.textContent = sentence?.sentence_fr || '';
    _sentDeEl.textContent = sentence?.sentence_de || '';
  }
  // Default visibility: shown when "always show translation" is on, else hidden (press u to toggle).
  _sentFrEl.style.display = (_alwaysTranslation && _sentFrEl.textContent) ? '' : 'none';
  _sentDeEl.style.display = (_alwaysTranslation && _sentDeEl.textContent) ? '' : 'none';
  _syncTransEye();
  _syncCardToggleBar();

  // Kahneman concept box (compact: part + chapter title only) + reasoning light
  // bulb — Kahneman mode only. Kontextsummary shows context + clickable source
  // title/publisher above the target sentence instead (news-back-source, #452).
  const _conceptRow = document.getElementById('sentence-concept-row');
  const _conceptEl = document.getElementById('sentence-concept');
  const _reasonBtn = document.getElementById('sentence-reasoning-btn');
  const _chNum = (!isSentenceNote && sentence?.concept_en)
    ? parseInt(sentence.concept_en.match(/Chapter (\d+)/)?.[1]) : null;
  const _isKahneman = !isSentenceNote && !!_chNum;
  const _hasNewsSrc = !isSentenceNote && !_isKahneman
    && !!(sentence?.reasoning_zh || sentence?.context_de || sentence?.source_url
          || sentence?.concept_zh || sentence?.source_title);

  // Kontextsummary: render the context + source block above the sentence; no light bulb.
  _renderNewsBackSource(_hasNewsSrc ? sentence : null);

  if (_isKahneman) {
    const renderConcept = (ch) => {
      _conceptEl.innerHTML =
          (ch?.part_zh ? `<span class="concept-part-label">${ch.part_zh}</span>` : '')
        + `<span class="concept-chapter-title">${sentence.concept_zh}</span>`;
      _conceptEl.classList.add('concept-clickable');
      _conceptEl.onclick = () => openKahnemanExamples(_chNum, sentence.concept_zh);
    };
    _conceptEl.style.display = '';
    const cachedCh = _kahnemanChapters ? _kahnemanChapters.find(c => c.number === _chNum) : null;
    renderConcept(cachedCh);
    if (!cachedCh) {
      _ensureKahnemanChapters().then(() => {
        const ch = _kahnemanChapters?.find(c => c.number === _chNum);
        if (ch) renderConcept(ch);
      });
    }
    // Light bulb opens the per-sentence reasoning popup (only if content exists)
    _currentReasoning = sentence.reasoning_zh || '';
    _currentSourceUrl = sentence.source_url || '';
    _currentReasoningIsNews = false;
    _reasonBtn.style.display = (_currentReasoning || _currentSourceUrl) ? '' : 'none';
    _currentReasoningIsKnowledge = false;
    _conceptRow.style.display = '';
  } else if (_hidesInlineContext(sentence) && sentence?.reasoning_zh) {
    // Knowledge mode (#931): same 💡 light bulb kahneman has, holding the
    // model's "Fakt: … Warum: …" note about why it picked this passage. No
    // concept box — knowledge sentences have no chapter, concept_zh is empty.
    //
    // _currentSourceUrl stays EMPTY on purpose: since #790 source_url is an
    // in-app hash link, and the popup's "open source" anchor is an <a href>
    // meant for external pages — pointing it at /#knowledge-12 would navigate
    // away mid-review. The 📄 button (in _renderNewsBackSource) is how the
    // source gets opened here, in a modal that closes back onto the card.
    _conceptRow.style.display = '';
    _conceptEl.innerHTML = '';
    _conceptEl.style.display = 'none';
    _currentReasoning = sentence.reasoning_zh || '';
    _currentSourceUrl = '';
    _currentReasoningIsNews = false;
    _currentReasoningIsKnowledge = true;
    _reasonBtn.style.display = '';
  } else {
    _conceptRow.style.display = 'none';
    _conceptEl.innerHTML = '';
    _conceptEl.style.display = '';
    _reasonBtn.style.display = 'none';
    _currentReasoning = '';
    _currentSourceUrl = '';
    _currentReasoningIsNews = false;
    _currentReasoningIsKnowledge = false;
  }

  const noteType = wordDetails?.note_type || card.note_type;
  const wordZhEl = document.getElementById('word-zh');
  const isMultiWord = noteType === 'sentence' || noteType === 'chengyu' || noteType === 'expression';
  if (isMultiWord && wordDetails?.components?.length) {
    wordZhEl.innerHTML = renderInteractiveZh(card.word_zh, wordDetails.components);
  } else {
    wordZhEl.textContent = card.word_zh;
  }
  const wordPinEl = document.getElementById('word-pin');
  wordPinEl.textContent = isSentenceNote ? '' : (card.pinyin || '');
  wordPinEl.style.display = isSentenceNote ? 'none' : '';
  document.getElementById('word-def').textContent = card.definition ? `🇬🇧 ${card.definition}` : '';
  const wordDefDeEl = document.getElementById('word-def-de');
  wordDefDeEl.textContent = card.definition_de ? `🇩🇪 ${card.definition_de}` : '';
  wordDefDeEl.style.display = card.definition_de ? 'block' : 'none';
  const wordDefFrEl = document.getElementById('word-def-fr');
  wordDefFrEl.textContent = card.definition_fr ? `🇫🇷 ${card.definition_fr}` : '';
  wordDefFrEl.style.display = card.definition_fr ? 'block' : 'none';

  const posEl = document.getElementById('word-pos');
  posEl.textContent   = card.pos || '';
  posEl.style.display = card.pos ? 'inline-block' : 'none';

  const regEl = document.getElementById('word-register');
  const regLabels = {
    spoken: '口语', written: '书面语', both: '通用',
    spoken_colloquial: '口语俚语', spoken_neutral: '中性口语',
    neutral: '通用', formal_written: '正式书面语', literary: '文学语体'
  };
  if (card.register) {
    regEl.textContent = regLabels[card.register] || card.register;
    regEl.style.display = 'inline-block';
  } else {
    regEl.style.display = 'none';
  }

  // Re-enable rating buttons
  document.querySelectorAll('.r-btn').forEach(b => b.disabled = false);

  // Show multi-word rating UI when the sentence contains multiple vocab words
  _renderMultiRatingIfNeeded();

  // Populate character breakdown, examples, notes, conjugation, and word analysis
  renderNotesSection();
  renderConjugationSection();
  renderInflectionSection();
  _callRenderWordAnalysis();
  renderVocabDetail();
  renderReviewCatRow();

  // Auto-play audio immediately on reveal (all categories) — issue #539.
  playSentence();
}

// ── Populate vocab detail (chars + examples) ────────────────────────────────
function toggleSection(id) {
  const body = document.getElementById(id);
  const arrow = document.getElementById(id + '-arrow');
  if (body.dataset.peek) {
    // Three-state cycle: peek → open → closed → peek
    const state = body.dataset.state || 'peek';
    if (state === 'peek') {
      body.classList.remove('section-peek');
      body.classList.add('section-open');
      body.style.display = '';
      body.dataset.state = 'open';
      arrow.textContent = '▼';
    } else if (state === 'open') {
      body.classList.remove('section-open');
      body.style.display = 'none';
      body.dataset.state = 'closed';
      arrow.textContent = '▶';
    } else {
      body.classList.add('section-peek');
      body.classList.remove('section-open');
      body.style.display = '';
      body.dataset.state = 'peek';
      arrow.textContent = '▷';
    }
  } else {
    const open = body.style.display !== 'none';
    body.style.display = open ? 'none' : 'block';
    arrow.textContent = open ? '▶' : '▼';
  }
}

// ── Interactive sentence/chengyu rendering ───────────────────────────────────

// Wrap component words in hoverable spans; unmatched chars are plain text.
function renderInteractiveZh(text, components) {
  // Build a list of (start, end, compIdx) matches
  const matches = [];
  for (let i = 0; i < components.length; i++) {
    const w = components[i].word_zh;
    const pos = text.indexOf(w);
    if (pos !== -1) matches.push({ start: pos, end: pos + [...w].length, idx: i });
  }
  // Sort by start; drop overlaps
  matches.sort((a, b) => a.start - b.start);
  const used = [];
  for (const m of matches) {
    if (used.length && used[used.length - 1].end > m.start) continue;
    used.push(m);
  }
  // Build HTML char-by-char
  const chars = [...text];
  let html = '';
  let i = 0;
  for (const m of used) {
    while (i < m.start) html += chars[i++];
    const span = chars.slice(m.start, m.end).join('');
    html += `<span class="iword" data-comp-idx="${m.idx}" ` +
            `onmouseenter="showWordTip(${m.idx},this)" ` +
            `onmouseleave="hideWordTip()">${span}</span>`;
    i = m.end;
  }
  while (i < chars.length) html += chars[i++];
  return html;
}

let _tipTimeout = null;

function showWordTip(idx, el) {
  clearTimeout(_tipTimeout);
  const comp = wordDetails?.components?.[idx];
  if (!comp) return;

  const tipChars = comp.characters || [];
  let inner = `<div class="tip-header">
    <span class="tip-zh">${comp.word_zh}</span>
    ${comp.pinyin ? `<span class="tip-pin">${comp.pinyin}</span>` : ''}
  </div>`;
  if (comp.definition) inner += `<div class="tip-def">${comp.definition}</div>`;
  if (tipChars.length) {
    inner += `<hr class="tip-divider">`;
    for (const c of tipChars) {
      inner += `<div class="tip-char-row">
        <span class="tip-char-zh">${c.char}</span>
        ${c.pinyin ? `<span class="tip-char-pin">${c.pinyin}</span>` : ''}
        ${c.meaning_in_context ? `<span class="tip-char-ctx">— ${c.meaning_in_context}</span>` : ''}
      </div>`;
      if (c.etymology) inner += `<div class="tip-etym">${c.etymology.trim()}</div>`;
    }
  }

  const tip = document.getElementById('word-tip');
  tip.innerHTML = inner;
  tip.style.display = 'block';

  // Position: centred above (or below if not enough room)
  const rect = el.getBoundingClientRect();
  const tipW = Math.min(300, window.innerWidth - 24);
  let left = rect.left + rect.width / 2 - tipW / 2;
  left = Math.max(12, Math.min(left, window.innerWidth - tipW - 12));
  tip.style.maxWidth = tipW + 'px';
  tip.style.left = left + 'px';
  const tipH = tip.offsetHeight || 200;
  tip.style.top = rect.top > tipH + 8
    ? (rect.top - tipH - 8) + 'px'
    : (rect.bottom + 8) + 'px';
}

function hideWordTip() {
  _tipTimeout = setTimeout(() => {
    const tip = document.getElementById('word-tip');
    if (tip) tip.style.display = 'none';
  }, 80);
}

// ── Category suspension row on review card back ──────────────────────────────
function renderReviewCatRow() {
  const el = document.getElementById('review-cat-row');
  if (!el) return;
  const cards = wordDetails?.cards;
  if (!cards?.length) { el.innerHTML = ''; return; }
  const CATS = ['reading', 'listening', 'creating'];
  const LABELS = { reading: 'Reading', listening: 'Listening', creating: 'Creating' };
  const html = CATS.map(cat => {
    const c = cards.find(c => c.category === cat && !c.deleted_at);
    if (!c) return '';
    const isCurrent = cat === card?.category;
    const isSusp = c.state === 'suspended';
    const cls = ['rcat-btn', isSusp ? 'rcat-susp' : 'rcat-active', isCurrent ? 'rcat-current' : ''].join(' ').trim();
    const title = isSusp ? `Activate ${LABELS[cat]}` : `Suspend ${LABELS[cat]}`;
    const letter = LABELS[cat][0];
    return `<button class="${cls}" onclick="toggleReviewCat(${c.id})" type="button" title="${title}">${letter}</button>`;
  }).join('');
  el.innerHTML = html;
}

function _toggleSuspendCat(category) {
  const cards = wordDetails?.cards || [];
  const c = cards.find(c => c.category === category && !c.deleted_at);
  if (c) toggleReviewCat(c.id);
}

async function toggleReviewCat(cardId) {
  try {
    await api('POST', `/api/cards/${cardId}/suspend`);
    const updated = await api('GET', `/api/words/${card.word_id}/cards`);
    if (wordDetails) wordDetails.cards = updated;
    renderReviewCatRow();
  } catch (e) {
    showError('Failed: ' + e.message);
  }
}

function _getActiveWordId() {
  return _currentWordId ?? wordDetails?.id ?? card?.word_id ?? null;
}

// ── Regen preview modal ──────────────────────────────────────────────────────
let _regenState = null; // { wordId, fields, containerId }

// Fields "↺ All" regenerates. The last three are character-level and only
// exist for Chinese; a Romance entry gets its entry-level etymology instead
// (#906) — asking the Chinese prompt to analyse the characters of "parler"
// would spend money on nonsense.
function _allRegenFields(wordData) {
  const base = ['definition', 'definition_zh', 'definition_de', 'definition_fr', 'pos',
                'notes', 'examples'];
  return _entryLang(wordData) === 'zh'
    ? [...base, 'etymology', 'compounds', 'other_meanings']
    : [...base, 'entry_etymology'];
}

function regenAllFields(wordId) {
  regenFields(wordId, _allRegenFields(wordDetails), 'wd-all');
}

function regenAllFieldsFromReview() {
  const wordId = _getActiveWordId();
  if (!wordId) return showError('No active word');
  regenFields(wordId, _allRegenFields(wordDetails), 'review-regen-all');
}

async function regenFields(wordId, fields, containerId) {
  const el = document.getElementById(containerId);
  const btn = el?.querySelector('.field-regen-btn');
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const preview = await api('POST', `/api/word/${wordId}/regenerate-fields`, { fields, preview: true });
    _regenState = { wordId, fields, containerId };
    _showRegenPreviewModal(preview);
  } catch (e) {
    showError('Regeneration failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↺'; }
  }
}

function _showRegenPreviewModal(previewData) {
  _closeRegenPreviewModal();
  const { fields, result } = previewData;
  const overlay = document.createElement('div');
  overlay.id = 'regen-preview-overlay';
  overlay.className = 'regen-preview-overlay';
  overlay.onclick = (e) => { if (e.target === overlay) _closeRegenPreviewModal(); };

  const wantEtym = fields.includes('etymology');
  const wantComp = fields.includes('compounds');

  let bodyHtml = '';

  const DEF_FIELDS = ['definition', 'definition_zh', 'definition_de', 'definition_fr', 'pos'];
  if (fields.some(f => DEF_FIELDS.includes(f))) {
    const esc = s => (s || '').replace(/"/g, '&quot;');
    let defHtml = '';
    if (fields.includes('pos'))
      defHtml += `<div class="regen-def-row"><label>POS</label><input type="text" id="regen-pos" value="${esc(result.pos)}" placeholder="n. / v. / adj."></div>`;
    if (fields.includes('definition'))
      defHtml += `<div class="regen-def-row"><label>EN</label><input type="text" id="regen-def" value="${esc(result.definition)}" placeholder="English definition"></div>`;
    if (fields.includes('definition_zh'))
      defHtml += `<div class="regen-def-row"><label>ZH</label><input type="text" id="regen-def-zh" value="${esc(result.definition_zh)}" placeholder="中文释义"></div>`;
    if (fields.includes('definition_de'))
      defHtml += `<div class="regen-def-row"><label>DE</label><input type="text" id="regen-def-de" value="${esc(result.definition_de)}" placeholder="Deutsche Definition"></div>`;
    if (fields.includes('definition_fr'))
      defHtml += `<div class="regen-def-row"><label>FR</label><input type="text" id="regen-def-fr" value="${esc(result.definition_fr)}" placeholder="Définition française"></div>`;
    bodyHtml += `<div><div class="regen-section-label">Definitions &amp; Part of Speech</div><div class="regen-def-group">${defHtml}</div></div>`;
  }

  if (fields.includes('notes')) {
    const text = (result.notes || '').replace(/"/g, '&quot;');
    bodyHtml += `<div>
      <div class="regen-section-label">Notes</div>
      <textarea id="regen-notes-text" class="regen-notes-textarea">${result.notes || ''}</textarea>
    </div>`;
  }

  if (fields.includes('entry_etymology')) {
    bodyHtml += `<div>
      <div class="regen-section-label">Etymology</div>
      <textarea id="regen-entry-etym-text" class="regen-notes-textarea">${result.entry_etymology || ''}</textarea>
    </div>`;
  }

  if (fields.includes('examples')) {
    const exRows = (result.examples || []).map((ex, i) => _regenExampleRowHtml(ex, i)).join('');
    bodyHtml += `<div>
      <div class="regen-section-label">Examples</div>
      <div class="regen-example-labels">
        <span>ZH</span><span>Pinyin</span><span>English</span><span>DE</span><span></span>
      </div>
      <div id="regen-examples-list">${exRows}</div>
      <button class="regen-add-btn" onclick="_addRegenExample()">+ Add example</button>
    </div>`;
  }

  const wantMeanings = fields.includes('other_meanings');
  if (wantEtym || wantComp || wantMeanings) {
    const chars = result.characters || [];
    const charSections = chars.map(c => {
      const charEsc = (c.char || '').replace(/'/g, "\\'");
      let inner = `<div class="regen-char-header">${c.char || ''}</div>`;
      if (wantMeanings) {
        const meanVal = Array.isArray(c.other_meanings) ? c.other_meanings.join(', ') : (c.other_meanings || '');
        inner += `<input type="text" class="regen-meanings-input" data-field="other_meanings" placeholder="Bedeutungen (kommagetrennt)" value="${meanVal.replace(/"/g, '&quot;')}">`;
      }
      if (wantEtym) {
        inner += `<textarea class="regen-etym-textarea" data-field="etymology" placeholder="Etymology…">${c.etymology || ''}</textarea>`;
      }
      if (wantComp) {
        const cpRows = (c.compounds || []).map(cp => _regenCompoundRowHtml(cp)).join('');
        inner += `<div class="regen-compound-labels">
          <span>Simplified</span><span>Pinyin</span><span>Meaning</span><span></span>
        </div>
        <div class="regen-compounds-list">${cpRows}</div>
        <button class="regen-add-btn" onclick="_addRegenCompound(this)">+ Add compound</button>`;
      }
      return `<div class="regen-char-group" data-char-id="${c.char_id || ''}" data-char="${charEsc}">${inner}</div>`;
    }).join('');
    bodyHtml += `<div>
      <div class="regen-section-label">Characters</div>
      <div id="regen-chars-list">${charSections}</div>
    </div>`;
  }

  overlay.innerHTML = `<div class="regen-preview-modal" onclick="event.stopPropagation()">
    <div class="regen-preview-header">
      <span>AI Preview</span>
      <button onclick="_closeRegenPreviewModal()">×</button>
    </div>
    <div class="regen-preview-body">${bodyHtml}</div>
    <div id="regen-modal-error" style="display:none;color:#b91c1c;background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:8px 12px;margin:8px 16px;font-size:13px"></div>
    <div class="regen-preview-footer">
      <button class="regen-btn regen-btn-regenerate" id="regen-btn-regen" onclick="_rerunRegen()">↺ Regenerate</button>
      <button class="regen-btn regen-btn-reject" onclick="_closeRegenPreviewModal()">✗ Reject</button>
      <button class="regen-btn regen-btn-apply" id="regen-btn-apply" onclick="_applyRegenResult()">✓ Apply</button>
    </div>
  </div>`;
  document.body.appendChild(overlay);
}

function _regenExampleRowHtml(ex, idx) {
  const esc = s => (s || '').replace(/"/g, '&quot;');
  return `<div class="regen-example-row">
    <input type="text" data-field="zh" value="${esc(ex.zh)}" placeholder="中文">
    <input type="text" data-field="pinyin" value="${esc(ex.pinyin)}" placeholder="pīnyīn">
    <input type="text" data-field="english" value="${esc(ex.english)}" placeholder="English">
    <input type="text" data-field="de" value="${esc(ex.de)}" placeholder="Deutsch">
    <button class="regen-row-del" onclick="this.closest('.regen-example-row').remove()">−</button>
  </div>`;
}

function _regenCompoundRowHtml(cp) {
  const esc = s => (s || '').replace(/"/g, '&quot;');
  return `<div class="regen-compound-row">
    <input type="text" data-field="simplified" value="${esc(cp.simplified || cp.compound_zh)}" placeholder="词">
    <input type="text" data-field="pinyin" value="${esc(cp.pinyin)}" placeholder="pīnyīn">
    <input type="text" data-field="meaning" value="${esc(cp.meaning)}" placeholder="Bedeutung">
    <button class="regen-row-del" onclick="this.closest('.regen-compound-row').remove()">−</button>
  </div>`;
}

function _addRegenExample() {
  const list = document.getElementById('regen-examples-list');
  if (list) list.insertAdjacentHTML('beforeend', _regenExampleRowHtml({}, list.children.length));
}

function _addRegenCompound(btn) {
  const list = btn.previousElementSibling;
  if (list) list.insertAdjacentHTML('beforeend', _regenCompoundRowHtml({}));
}

function _closeRegenPreviewModal() {
  document.getElementById('regen-preview-overlay')?.remove();
}

function _getRegenResultFromModal() {
  const result = {};
  const fields = _regenState?.fields || [];

  const DEF_FIELDS = ['definition', 'definition_zh', 'definition_de', 'definition_fr', 'pos'];
  if (fields.some(f => DEF_FIELDS.includes(f))) {
    if (fields.includes('pos'))           result.pos           = document.getElementById('regen-pos')?.value?.trim()    || '';
    if (fields.includes('definition'))    result.definition    = document.getElementById('regen-def')?.value?.trim()    || '';
    if (fields.includes('definition_zh')) result.definition_zh = document.getElementById('regen-def-zh')?.value?.trim() || '';
    if (fields.includes('definition_de')) result.definition_de = document.getElementById('regen-def-de')?.value?.trim() || '';
    if (fields.includes('definition_fr')) result.definition_fr = document.getElementById('regen-def-fr')?.value?.trim() || '';
  }

  if (fields.includes('notes')) {
    result.notes = document.getElementById('regen-notes-text')?.value?.trim() || '';
  }

  if (fields.includes('entry_etymology')) {
    result.entry_etymology = document.getElementById('regen-entry-etym-text')?.value?.trim() || '';
  }

  if (fields.includes('examples')) {
    const rows = document.querySelectorAll('#regen-examples-list .regen-example-row');
    result.examples = Array.from(rows).map(row => ({
      zh:      row.querySelector('[data-field="zh"]')?.value?.trim() || '',
      pinyin:  row.querySelector('[data-field="pinyin"]')?.value?.trim() || '',
      english: row.querySelector('[data-field="english"]')?.value?.trim() || '',
      de:      row.querySelector('[data-field="de"]')?.value?.trim() || '',
    })).filter(ex => ex.zh);
  }

  if (fields.includes('etymology') || fields.includes('compounds')) {
    const charGroups = document.querySelectorAll('#regen-chars-list .regen-char-group');
    result.characters = Array.from(charGroups).map(group => {
      const rawId = parseInt(group.dataset.charId);
      const charResult = {
        char:    group.dataset.char,
        char_id: isNaN(rawId) ? null : rawId,
      };
      if (fields.includes('other_meanings')) {
        const raw = group.querySelector('[data-field="other_meanings"]')?.value?.trim() || '';
        charResult.other_meanings = raw ? raw.split(',').map(s => s.trim()).filter(Boolean) : [];
      }
      if (fields.includes('etymology')) {
        charResult.etymology = group.querySelector('[data-field="etymology"]')?.value?.trim() || '';
      }
      if (fields.includes('compounds')) {
        const cpRows = group.querySelectorAll('.regen-compound-row');
        charResult.compounds = Array.from(cpRows).map(row => ({
          simplified: row.querySelector('[data-field="simplified"]')?.value?.trim() || '',
          pinyin:     row.querySelector('[data-field="pinyin"]')?.value?.trim() || '',
          meaning:    row.querySelector('[data-field="meaning"]')?.value?.trim() || '',
        })).filter(c => c.simplified);
      }
      return charResult;
    });
  }

  return result;
}

async function _applyRegenResult() {
  const { wordId, fields, containerId } = _regenState || {};
  if (!wordId) return;
  const applyBtn = document.getElementById('regen-btn-apply');
  const regenBtn = document.getElementById('regen-btn-regen');
  if (applyBtn) applyBtn.disabled = true;
  if (regenBtn) regenBtn.disabled = true;
  try {
    const result = _getRegenResultFromModal();
    const updated = await api('POST', `/api/word/${wordId}/apply-regen-result`, { fields, result });
    _closeRegenPreviewModal();
    if (wordDetails?.id === wordId) wordDetails = updated;
    const DEF_FIELDS = ['definition', 'definition_zh', 'definition_de', 'definition_fr', 'pos'];
    const isDefRegen = fields.some(f => DEF_FIELDS.includes(f));
    console.log('[apply] wordId=', wordId, '_currentWordId=', _currentWordId, 'fields=', fields, 'containerId=', containerId, 'isDefRegen=', isDefRegen);
    if (containerId === 'review-regen-all') {
      // Re-render all review side-panel sections
      renderNotesSection(null, updated.notes, wordId);
      renderWordAnalysis(null, updated, wordId);
      renderEtymologySection(null, updated, wordId);
      renderVocabDetail(null, updated.examples, wordId);
    } else if (containerId === 'wd-all' && _currentWordId === wordId) {
      updated.cards = wordDetails?.cards || [];
      renderWordDetail(updated);
    } else if (isDefRegen && _currentWordId === wordId) {
      // Definition/POS regen: full re-render is safe (header is always visible)
      updated.cards = wordDetails?.cards || [];
      renderWordDetail(updated);
    } else {
      // Section regen (notes/examples/etymology/compounds): targeted re-render to keep section open
      const target = document.getElementById(containerId);
      console.log('[apply] target=', target, 'containerId=', containerId);
      if (target) {
        if (isDefRegen) {
          const posEl = document.getElementById('wd-pos');
          if (posEl) { posEl.textContent = updated.pos || '—'; posEl.style.display = 'inline-block'; }
          const defEl = document.getElementById('wd-def');
          if (defEl) defEl.textContent = updated.definition ? `🇬🇧 ${updated.definition}` : '';
          const defZhEl = document.getElementById('wd-def-zh');
          if (defZhEl) { defZhEl.textContent = updated.definition_zh || ''; defZhEl.style.display = updated.definition_zh ? 'block' : 'none'; }
          const defDeEl = document.getElementById('wd-def-de');
          if (defDeEl) { defDeEl.textContent = updated.definition_de ? `🇩🇪 ${updated.definition_de}` : ''; defDeEl.style.display = updated.definition_de ? 'block' : 'none'; }
          const defFrEl = document.getElementById('wd-def-fr');
          if (defFrEl) { defFrEl.textContent = updated.definition_fr ? `🇫🇷 ${updated.definition_fr}` : ''; defFrEl.style.display = updated.definition_fr ? 'block' : 'none'; }
        } else if (fields.includes('notes'))            renderNotesSection(target, updated.notes, wordId);
        else if (fields.includes('examples'))           renderVocabDetail(target, updated.examples, wordId);
        else if (fields.includes('entry_etymology'))    renderEtymologySection(target, updated, wordId);
        else                                            renderWordAnalysis(target, updated, wordId);
        const body  = document.getElementById(containerId + '-body');
        const arrow = document.getElementById(containerId + '-body-arrow');
        console.log('[apply] body=', body, 'arrow=', arrow);
        if (body)  body.style.display = 'block';
        if (arrow) arrow.textContent = '▼';
      }
    }
  } catch (e) {
    const modalErr = document.getElementById('regen-modal-error');
    if (modalErr) { modalErr.textContent = 'Apply failed: ' + e.message; modalErr.style.display = 'block'; }
    else showError('Apply failed: ' + e.message);
    if (applyBtn) applyBtn.disabled = false;
    if (regenBtn) regenBtn.disabled = false;
  }
}

async function _rerunRegen() {
  const { wordId, fields, containerId } = _regenState || {};
  if (!wordId) return;
  const regenBtn = document.getElementById('regen-btn-regen');
  const applyBtn = document.getElementById('regen-btn-apply');
  if (regenBtn) { regenBtn.disabled = true; regenBtn.textContent = '…'; }
  if (applyBtn) applyBtn.disabled = true;
  try {
    const preview = await api('POST', `/api/word/${wordId}/regenerate-fields`, { fields, preview: true });
    _regenState = { wordId, fields, containerId };
    _showRegenPreviewModal(preview);
  } catch (e) {
    showError('Regeneration failed: ' + e.message);
    if (regenBtn) { regenBtn.disabled = false; regenBtn.textContent = '↺ Regenerate'; }
    if (applyBtn) applyBtn.disabled = false;
  }
}

function renderVocabDetail(container, examples, wordId) {
  const el = container ?? document.getElementById('examples-section');
  const items = examples ?? wordDetails?.examples ?? [];
  const wid = wordId ?? _getActiveWordId();
  const bodyId = el.id + '-body';
  const regenBtn = wid ? `<button class="field-regen-btn" onclick="event.stopPropagation();regenFields(${wid},['examples'],'${el.id}')" title="Regenerate examples">↺</button>` : '';
  const html = items.length > 0
    ? items.map(ex => {
        let h = `<div class="example-item">`;
        h += `<div class="example-zh">${ex.example_zh || ''}</div>`;
        if (ex.example_pinyin) h += `<div class="example-pin">${ex.example_pinyin}</div>`;
        if (ex.example_de)     h += `<div class="example-de">${ex.example_de}</div>`;
        h += `</div>`;
        return h;
      }).join('')
    : `<div class="section-empty">—</div>`;
  el.innerHTML =
    `<div class="section-label section-label-row section-toggle" onclick="toggleSection('${bodyId}')">` +
      `<span><span id="${bodyId}-arrow">▶</span> Examples</span>${regenBtn}</div>` +
    `<div id="${bodyId}" style="display:none">${html}</div>`;
}

function renderNotesSection(container, notes, wordId) {
  const el = container ?? document.getElementById('notes-section');
  const text = notes ?? card?.notes;
  const wid = wordId ?? _getActiveWordId();
  const bodyId = el.id + '-body';
  const regenBtn = wid ? `<button class="field-regen-btn" onclick="event.stopPropagation();regenFields(${wid},['notes'],'${el.id}')" title="Regenerate notes">↺</button>` : '';
  const bodyContent = text
    ? `<div class="notes-body">${renderMarkdown(text)}</div>`
    : `<div class="section-empty">—</div>`;
  el.innerHTML =
    `<div class="section-label section-label-row section-toggle" onclick="toggleSection('${bodyId}')">` +
      `<span><span id="${bodyId}-arrow">▷</span> Notes</span>${regenBtn}</div>` +
    `<div id="${bodyId}" class="section-peek" data-peek="1" data-state="peek">${bodyContent}</div>`;
  el.style.display = '';
}

// Word-origin section (issue #906) — Romance languages only.
//
// A French word has no characters to break down, so the Chinese "Word
// Analysis" block degenerates to a single row repeating the headword. This
// takes its place in the panel: entries.etymology, German prose written by the
// entry prompt (or the ↺ button here). Chinese entries render nothing —
// their etymology is per character and already lives inside Word Analysis.
function renderEtymologySection(container, wordData, wordId) {
  const el = container ?? document.getElementById('etymology-section');
  if (!el) return;
  const wd = wordData ?? wordDetails;
  if (_entryLang(wd) === 'zh') { el.innerHTML = ''; return; }

  const wid = wordId ?? _getActiveWordId();
  const regenBtn = wid
    ? `<button class="field-regen-btn" onclick="event.stopPropagation();regenFields(${wid},['entry_etymology'],'${el.id}')" title="Regenerate etymology">↺</button>`
    : '';
  const text = (wd?.etymology || '').trim();
  const bodyId = el.id + '-body';
  const bodyContent = text
    ? `<div class="notes-body">${renderMarkdown(text)}</div>`
    : `<div class="section-empty">—</div>`;
  el.innerHTML =
    `<div class="section-label section-label-row section-toggle" onclick="toggleSection('${bodyId}')">` +
      `<span><span id="${bodyId}-arrow">▼</span> Etymology</span>${regenBtn}</div>` +
    `<div id="${bodyId}" class="section-open" data-peek="1" data-state="open">${bodyContent}</div>`;
  el.style.display = '';
}

// Which language an entry belongs to. Word detail passes the entry row (which
// carries `lang`); during review the fetch for /api/word/{id} may not have
// landed yet, so fall back to the current card's deck language.
function _entryLang(wordData) {
  return wordData?.lang || currentCardLang() || 'zh';
}

// Verb conjugation section (issue #596) — French & future conjugating
// languages. wordData.conjugations rows come pre-ordered by position; group
// them back into per-tense cards (person '' = impersonal form, e.g. participle).
function renderConjugationSection(container, wordData) {
  const el = container ?? document.getElementById('conjugation-section');
  if (!el) return;
  const conj = (wordData ?? wordDetails)?.conjugations || [];
  if (!conj.length) { el.innerHTML = ''; return; }
  const tenses = [];
  const byTense = new Map();
  conj.forEach(c => {
    if (!byTense.has(c.tense)) { byTense.set(c.tense, []); tenses.push(c.tense); }
    byTense.get(c.tense).push(c);
  });
  const bodyId = el.id + '-body';
  const cards = tenses.map(t => {
    const rows = byTense.get(t).map(c =>
      c.person
        ? `<div class="conj-row"><span class="conj-person">${c.person}</span><span class="conj-form">${c.form}</span></div>`
        : `<div class="conj-row"><span class="conj-form">${c.form}</span></div>`
    ).join('');
    return `<div class="conj-tense-card"><div class="conj-tense-name">${t}</div>${rows}</div>`;
  }).join('');
  el.innerHTML =
    `<div class="section-label section-toggle" onclick="toggleSection('${bodyId}')">` +
      `<span id="${bodyId}-arrow">▶</span> Conjugation</div>` +
    `<div id="${bodyId}" style="display:none"><div class="conj-grid">${cards}</div></div>`;
}

// Noun/adjective inflection section (issue #805) — plural, gender agreement.
// Chinese entries never have wordData.gender or .inflections, so this section
// is simply absent for them. wordData.inflections rows come pre-ordered by
// position, shaped like conjugations ({paradigm, slot, form}); group them
// back into per-dimension cards (e.g. paradigm='nombre'/'genre').
const GENDER_LABELS = { m: 'masculine', f: 'feminine', mf: 'masculine/feminine' };

function renderInflectionSection(container, wordData) {
  const el = container ?? document.getElementById('inflection-section');
  if (!el) return;
  const wd = wordData ?? wordDetails;
  const inflections = wd?.inflections || [];
  const gender = wd?.gender;
  if (!inflections.length && !gender) { el.innerHTML = ''; return; }

  let genderRow = '';
  if (gender) {
    genderRow = `<div class="conj-row"><span class="conj-person">Gender</span>` +
      `<span class="conj-form">${GENDER_LABELS[gender] || gender}</span></div>`;
  }

  const paradigms = [];
  const byParadigm = new Map();
  inflections.forEach(f => {
    if (!byParadigm.has(f.paradigm)) { byParadigm.set(f.paradigm, []); paradigms.push(f.paradigm); }
    byParadigm.get(f.paradigm).push(f);
  });
  const cards = paradigms.map(p => {
    const rows = byParadigm.get(p).map(f =>
      f.slot
        ? `<div class="conj-row"><span class="conj-person">${f.slot}</span><span class="conj-form">${f.form}</span></div>`
        : `<div class="conj-row"><span class="conj-form">${f.form}</span></div>`
    ).join('');
    return `<div class="conj-tense-card"><div class="conj-tense-name">${p}</div>${rows}</div>`;
  }).join('');

  const bodyId = el.id + '-body';
  const genderCard = genderRow ? `<div class="conj-tense-card">${genderRow}</div>` : '';
  el.innerHTML =
    `<div class="section-label section-toggle" onclick="toggleSection('${bodyId}')">` +
      `<span id="${bodyId}-arrow">▶</span> Inflection</div>` +
    `<div id="${bodyId}" style="display:none"><div class="conj-grid">${genderCard}${cards}</div></div>`;
}

function renderWordAnalysis(container, wordData, wordId) {
  const el = container ?? document.getElementById('word-analysis-section');
  const wd = wordData ?? wordDetails;
  // Chinese-only block (issue #906): character breakdown, measure words and
  // component words are all 汉字 machinery. For a French/Spanish entry it
  // rendered one row repeating the headword — renderEtymologySection() takes
  // this slot instead.
  if (_entryLang(wd) !== 'zh') { el.innerHTML = ''; return; }
  const nt = wd?.note_type ?? card?.note_type;
  const isMultiWord = nt === 'sentence' || nt === 'chengyu' || nt === 'expression';
  const prefix = el.id;
  const bodyId = prefix + '-body';

  // Build word groups for all note types
  let wordGroups = [];
  if (isMultiWord) {
    wordGroups = wd?.components || [];
    // chengyu/sentence with no components: fall back to characters linked directly to the entry
    if (wordGroups.length === 0 && wd?.characters?.length > 0) {
      wordGroups = [{
        id: wd.id,
        word_zh:       wd.word_zh    || card?.word_zh,
        pinyin:        wd.pinyin     || card?.pinyin,
        hsk_level:     wd.hsk_level  || card?.hsk_level,
        definition:    wd.definition || card?.definition,
        measure_words: wd.measure_words || [],
        characters:    wd.characters || [],
      }];
    }
  } else if (wd?.components?.length > 0) {
    // New-format vocabulary: word_analyses stored as components (each with own characters)
    wordGroups = wd.components;
  } else if (wd) {
    // Old-format vocabulary: characters linked directly to the entry
    wordGroups = [{
      id: wd.id,
      word_zh:       wd.word_zh    || card?.word_zh,
      pinyin:        wd.pinyin     || card?.pinyin,
      hsk_level:     wd.hsk_level  || card?.hsk_level,
      definition:    wd.definition || card?.definition,
      measure_words: wd.measure_words || [],
      characters:    wd.characters || [],
    }];
  }

  const wid = wordId ?? _getActiveWordId();
  const regenBtnWA = wid ? `<button class="field-regen-btn" onclick="event.stopPropagation();regenFields(${wid},['etymology','compounds','other_meanings'],'${el.id}')" title="Regenerate etymology, compounds &amp; meanings">↺</button>` : '';

  if (wordGroups.length === 0) {
    el.innerHTML =
      `<div class="section-label section-label-row section-toggle" onclick="toggleSection('${bodyId}')">` +
        `<span><span id="${bodyId}-arrow">▼</span> Word Analysis</span>${regenBtnWA}</div>` +
      `<div id="${bodyId}" class="wa-list section-open" data-peek="1" data-state="open"><div class="section-empty">—</div></div>`;
    return;
  }

  const wordCards = wordGroups.map((comp, idx) => {
    const wid = comp.id;
    const charBodyId = `${prefix}-wa-${idx}`;

    // Header: word (clickable to Browse) + pinyin + HSK + definition
    const zhSpan = wid
      ? `<span class="wa-word-zh wa-browse-link" onclick="openWordDetail(${wid})">${comp.word_zh || ''}</span>`
      : `<span class="wa-word-zh">${comp.word_zh || ''}</span>`;
    let header = zhSpan;
    if (comp.pinyin)     header += `<span class="wa-word-pin">${comp.pinyin}</span>`;
    if (comp.hsk_level)  header += `<span class="wa-hsk-badge">HSK ${comp.hsk_level}</span>`;
    const compDef = comp.definition || (() => {
      try { const m = JSON.parse(comp.characters?.[0]?.other_meanings || '[]'); return m.slice(0, 2).join('; '); }
      catch { return ''; }
    })();
    if (compDef) header += `<span class="wa-word-def">${compDef}</span>`;

    // Measure words row
    let mwHtml = '';
    const mw = comp.measure_words || [];
    if (mw.length) {
      const items = mw.map(m =>
        `<span class="wa-mw-item">${m.measure_zh}` +
        (m.pinyin ? ` <span class="wa-mw-pin">${m.pinyin}</span>` : '') +
        (m.meaning ? ` <span class="wa-mw-meaning">${m.meaning}</span>` : '') +
        `</span>`
      ).join('');
      mwHtml = `<div class="wa-measure-row"><span class="wa-rel-label">量词</span>${items}</div>`;
    }

    // Characters body (collapsed sub-toggle)
    const chars = comp.characters || [];
    let charBody = '';
    if (chars.length) {
      charBody = chars.map(c => {
        const charEsc = (c.char || '').replace(/'/g, "\\'");
        const pinEsc  = (c.pinyin || '').replace(/'/g, "\\'");
        let right = '';
        if (c.pinyin) right += `<span class="wa-char-pin">${c.pinyin}</span>`;
        const charMeaning = c.meaning_in_context || (() => {
          try { const m = JSON.parse(c.other_meanings || '[]'); return m.slice(0, 2).join('; '); }
          catch { return ''; }
        })();
        if (charMeaning) right += `<span class="wa-char-ctx">${charMeaning}</span>`;
        if (c.compounds?.length) {
          const cps = c.compounds.map(cp => {
            const highlightedZh = (cp.compound_zh || '').split('').map(ch =>
              ch === c.char ? `<span class="wa-compound-hl">${ch}</span>` : ch
            ).join('');
            const zhEsc = (cp.compound_zh || '').replace(/'/g, "\\'");
            const pinEsc = (cp.pinyin || '').replace(/'/g, "\\'");
            const meanEsc = (cp.meaning || '').replace(/'/g, "\\'");
            return `<span class="wa-compound-item wa-compound-clickable" onclick="event.stopPropagation();openQuickAddMenu(event,'${zhEsc}','${pinEsc}','${meanEsc}')">${highlightedZh}` +
              (cp.pinyin ? ` <span class="wa-compound-pin">${cp.pinyin}</span>` : '') +
              (cp.meaning ? ` <span class="wa-compound-meaning">${cp.meaning}</span>` : '') +
              `</span>`;
          }).join('');
          right += `<div class="wa-compounds">${cps}</div>`;
        }
        if (c.etymology) right += `<div class="wa-char-etym">${c.etymology}</div>`;
        const tradHtml = (c.traditional && c.traditional !== c.char)
          ? `<span class="wa-char-trad">${c.traditional}</span>`
          : '';
        return `<div class="wa-char-row" onclick="openHanziRegenModal(${c.char_id},'${charEsc}','${pinEsc}',true)">` +
          `<span class="wa-char-zh-col"><span class="wa-char-zh">${c.char}</span>${tradHtml}</span>` +
          `<div class="wa-char-right">${right}</div>` +
          `</div>`;
      }).join('');
    }

    const hasChars = charBody.length > 0;
    return `<div class="wa-word-card">` +
      `<div class="wa-word-header">${header}</div>` +
      (mwHtml ? `<div class="wa-word-extra">${mwHtml}</div>` : '') +
      (hasChars ? `<div class="wa-chars-list">${charBody}</div>` : '') +
      `</div>`;
  }).join('');

  el.innerHTML =
    `<div class="section-label section-label-row section-toggle" onclick="toggleSection('${bodyId}')">` +
      `<span><span id="${bodyId}-arrow">▼</span> Word Analysis</span>${regenBtnWA}</div>` +
    `<div id="${bodyId}" class="wa-list section-open" data-peek="1" data-state="open">${wordCards}</div>`;
}

function _callRenderWordAnalysis() {
  // Two mutually exclusive renderers share this slot in the review panel:
  // Word Analysis for Chinese, Etymology for everything else (#906). Both are
  // called; each one clears itself for the language it doesn't serve.
  renderWordAnalysis();
  renderEtymologySection();
}

// ── Quick-add compound word to tomorrow's Daily deck ────────────────────────

let _quickAddMenu = null;

function openQuickAddMenu(event, wordZh, pinyin, meaning) {
  closeQuickAddMenu();

  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  const tomorrowStr = String(tomorrow.getMonth() + 1).padStart(2, '0') + '-' + String(tomorrow.getDate()).padStart(2, '0');

  const menu = document.createElement('div');
  menu.id = 'quick-add-menu';
  menu.className = 'quick-add-menu';
  const wEsc = wordZh.replace(/'/g, "\\'");
  const pEsc = pinyin.replace(/'/g, "\\'");
  const mEsc = meaning.replace(/'/g, "\\'");
  menu.innerHTML =
    `<div class="qa-word">${wordZh}` +
      (pinyin ? ` <span class="qa-pin">${pinyin}</span>` : '') +
    `</div>` +
    (meaning ? `<div class="qa-meaning">${meaning}</div>` : '') +
    `<button class="qa-add-btn qa-save-btn" onclick="doSaveWord('${wEsc}','${pEsc}','${mEsc}',this)">★ Save for later</button>` +
    `<div class="qa-deck-label">or add now to daily::${tomorrowStr}</div>` +
    `<button class="qa-add-btn" onclick="doQuickAdd('${wEsc}',this)">+ Add to Daily deck</button>`;

  document.body.appendChild(menu);
  _quickAddMenu = menu;

  // Position near the click
  const x = Math.min(event.clientX, window.innerWidth - 220);
  const y = event.clientY + 8;
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';

  // Close on outside click
  setTimeout(() => document.addEventListener('click', closeQuickAddMenu, { once: true }), 0);
}

function closeQuickAddMenu() {
  if (_quickAddMenu) {
    _quickAddMenu.remove();
    _quickAddMenu = null;
  }
}

async function doSaveWord(wordZh, pinyin, meaning, btn) {
  btn.disabled = true;
  btn.textContent = '…';
  try {
    // Same reasoning as doQuickAdd: the word came out of a card, so it is
    // staged in that card's language's Saved deck (#726).
    const result = await api('POST', '/api/save-word',
                             { word_zh: wordZh, pinyin, meaning, lang: currentCardLang() });
    closeQuickAddMenu();
    const msgs = {
      saved:            `★ "${wordZh}" saved for later`,
      already_saved:    `"${wordZh}" is already saved`,
      exists_elsewhere: `"${wordZh}" is already in a deck`,
    };
    showQuickAddBanner(msgs[result.status] || '★ Saved', result.status !== 'saved');
  } catch (e) {
    btn.disabled = false;
    btn.textContent = '★ Save for later';
    showError(e.message || 'Failed to save word');
  }
}

// pinyin/meaning are no longer passed: DeepSeek writes every field itself (#643).
function doQuickAdd(wordZh, btn) {
  btn.disabled = true;
  btn.textContent = '…';
  // The menu closes immediately — generation takes ~30s in the background and
  // reports through the banner, so reviewing is never blocked (#643).
  closeQuickAddMenu();
  showQuickAddBanner(`⏳ Generating entry for "${wordZh}"…`, true);
  // The word was long-pressed inside a card, so it belongs to that card's
  // language — never to whatever tab the home page happens to be on (#726).
  addWordViaAi(wordZh, 'tomorrow', (state, text, deckPath) => {
    if (state === 'running') return;
    // 'idle' (#888): the "already in your collection" confirmation was
    // cancelled — nothing happened, so just clear the "Generating…" banner
    // rather than reporting either success or failure.
    if (state === 'idle') {
      hideQuickAddBanner();
      return;
    }
    showQuickAddBanner(
      state === 'done' ? `✓ "${wordZh}" added to ${deckPath}` : `❌ "${wordZh}": ${text}`,
      state !== 'done');
  }, currentCardLang());
}

function showQuickAddBanner(msg, isInfo) {
  let el = document.getElementById('quick-add-banner');
  if (!el) {
    el = document.createElement('div');
    el.id = 'quick-add-banner';
    el.className = 'quick-add-banner';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = 'quick-add-banner' + (isInfo ? ' qa-info' : ' qa-success');
  el.style.display = 'block';
  clearTimeout(el._hideTimer);
  el._hideTimer = setTimeout(() => { el.style.display = 'none'; }, 3500);
}

function hideQuickAddBanner() {
  const el = document.getElementById('quick-add-banner');
  if (!el) return;
  clearTimeout(el._hideTimer);
  el.style.display = 'none';
}

// ── Add a word from the header (#627) ───────────────────────────────────────
// Type a Chinese word → DeepSeek writes a full de-zh-bot style entry → it lands
// in today's Daily deck, due today. Generation takes ~30s, so the request only
// returns a job id and we poll for the outcome.

// Generation takes ~30s but the request returns a job id immediately, so the
// input never blocks: each submitted word becomes a queue entry that polls its
// own job while the user types the next one (#636).
let _addWordQueue = [];             // [{key, wordZh, state, text}]
let _addWordSeq = 0;
// Which language the next word is added in (#726). Set from the home page's
// active tab each time the modal opens, then switchable per word: adding one
// French word should not mean leaving the Chinese tab and coming back.
let _addWordLang = 'zh';

// Placeholder + hint lead per language — the backend rejects the wrong script
// outright, so the box has to say which one it wants.
const _ADD_WORD_PROMPTS = {
  zh: { placeholder: '新词…',     lead: 'Enter a Chinese word' },
  fr: { placeholder: 'nouveau mot…', lead: 'Enter a French word' },
};

function setAddWordLang(lang) {
  _addWordLang = lang;
  const p = _ADD_WORD_PROMPTS[lang] || _ADD_WORD_PROMPTS.zh;
  const input = document.getElementById('add-word-input');
  input.placeholder = p.placeholder;
  document.getElementById('add-word-hint-lead').textContent = p.lead;
  _renderAddWordLangs();
  input.focus();
}

function _renderAddWordLangs() {
  const el = document.getElementById('add-word-langs');
  if (!el) return;
  // One language in use → no picker at all, exactly as before #726.
  if (_availableLangs.length <= 1) { el.innerHTML = ''; return; }
  el.innerHTML = _availableLangs.map(l => {
    const label = _LANG_TAB_LABELS[l] || l;
    const active = l === _addWordLang ? ' lang-tab-active' : '';
    return `<button class="lang-tab${active}" onclick="setAddWordLang('${l}')">${label}</button>`;
  }).join('');
}

// preferredLang: the review header's ＋ passes the current card's language
// (#829) — the word was picked out of that card, so the home page's tab is
// irrelevant there. Everywhere else it's omitted and the tab decides.
function openAddWordModal(preferredLang) {
  document.getElementById('add-word-overlay').style.display = 'block';
  document.getElementById('add-word-modal').style.display = 'block';
  const input = document.getElementById('add-word-input');
  input.value = '';
  input.disabled = false;
  document.getElementById('add-word-status').textContent = '';
  // Follow the home page's language tab, but only for languages that exist —
  // a stale localStorage value must not send words into an unknown tree.
  const wanted = preferredLang || activeLang();
  setAddWordLang(_availableLangs.includes(wanted) ? wanted : 'zh');
  // Jobs that finished while the modal was closed already reported via banner.
  _addWordQueue = _addWordQueue.filter(item => item.state === 'running');
  _renderAddWordQueue();
  input.focus();
}

function closeAddWordModal() {
  document.getElementById('add-word-overlay').style.display = 'none';
  document.getElementById('add-word-modal').style.display = 'none';
  // Running jobs keep going server-side and their polls keep the queue up to
  // date; closing only hides the list. Drop finished rows so reopening the
  // modal shows a clean slate.
  _addWordQueue = _addWordQueue.filter(item => item.state === 'running');
}

// Shared by the two keyboard ways in (#788's ⌘A and #927's optional single
// key) — one toggle so both can never drift apart.
function toggleAddWordModal() {
  const modal = document.getElementById('add-word-modal');
  if (modal && modal.style.display !== 'none') { closeAddWordModal(); return; }
  // In review the word was picked out of the card, so that card's language
  // wins (same rule as the header ＋, #829). Anywhere else the argument must
  // stay omitted so the home page's language tab decides.
  openAddWordModal(_currentView === 'review' ? currentCardLang() : undefined);
}

function _renderAddWordQueue() {
  const el = document.getElementById('add-word-queue');
  if (!el) return;
  // Built with textContent, not innerHTML — the word is free user input.
  el.textContent = '';
  for (const item of _addWordQueue) {
    const row = document.createElement('div');
    row.className = 'add-word-queue-item awq-' + item.state;
    const word = document.createElement('b');
    word.textContent = item.wordZh;
    const state = document.createElement('span');
    state.textContent = item.text;
    row.append(word, state);
    el.appendChild(row);
  }
}

function _setAddWordItem(item, state, text) {
  item.state = state;
  item.text = text;
  _renderAddWordQueue();
}

function submitAddWord() {
  const input = document.getElementById('add-word-input');
  const wordZh = input.value.trim();
  if (!wordZh) return;

  // Clear right away — the whole point is being able to type the next word
  // while this one generates in the background.
  input.value = '';
  input.focus();
  document.getElementById('add-word-status').textContent = '';

  const item = { key: ++_addWordSeq, wordZh, state: 'running', text: 'Generating…' };
  _addWordQueue.unshift(item);
  _renderAddWordQueue();

  // Always the ★ List (#715): every add-word entry point parks the word in
  // Saved, suspended, and activating it is a separate step in Browse.
  addWordViaAi(wordZh, 'list', (state, text, deckPath) => {
    // 'idle' (#888): the "already in your collection" confirmation was
    // cancelled — nothing happened, so drop the queued entry entirely rather
    // than leaving it stuck on "Generating…".
    if (state === 'idle') {
      const idx = _addWordQueue.indexOf(item);
      if (idx !== -1) _addWordQueue.splice(idx, 1);
      _renderAddWordQueue();
      return;
    }
    _setAddWordItem(item, state, text);
    // The modal may already be closed; the banner is how the user finds out.
    if (state === 'done' &&
        document.getElementById('add-word-modal').style.display === 'none') {
      showQuickAddBanner(`✓ "${wordZh}" added to ${deckPath}`, false);
    }
  }, _addWordLang);
}

// ── Listening hint slider (#1006) ───────────────────────────────────────────
//
// Three stops and nothing in between, replacing the 0..8 HSK scale and its two
// blanking modes (#850/#862/#874/#983/#984):
//
//   0  Show all         — the whole sentence except the target word, which is
//                         blanked at every stop: it is the answer.
//   1  New words only   — everything he already knows is blanked; what stays
//                         standing is what he would have to look up.
//   2  Hide all         — nothing but blanks.
//
// Stop 1 asks the SAME judge the knowledge reader and the book reader ask
// (POST /api/new-words -> annotate.annotate_summary), instead of the old
// browser-side vocab-index + hsk_levels.json rule: that one knew nothing about
// known_words (#710), the baseline lists (#922) or the transparent-compound
// filter (#638), so the same word could count as new while reading and as
// known while reviewing.
const _HINT_MIN = 0;
const _HINT_MAX = 2;
const _HINT_LABELS = ['Show all', 'New words only', 'Hide all'];

function _hintSavedDefault() {
  const n = parseInt(localStorage.getItem('listenHintState') ?? '1', 10);
  return Number.isFinite(n) ? Math.max(_HINT_MIN, Math.min(_HINT_MAX, n)) : 1;
}

// The new words of the sentence currently on screen, keyed by lang+sentence so
// a stale response can never land on the next card. `words` stays null while
// the request is in flight; `error` is set when it fails and the label says so
// — the middle stop must never silently degrade into "hide all", which is
// exactly what an empty list would look like.
let _hintWords = { key: null, words: null, error: null };

function _hintKey(text, lang) { return lang + '\n' + text; }

async function _loadHintNewWords(text, lang) {
  const key = _hintKey(text, lang);
  if (_hintWords.key === key) return;
  _hintWords = { key, words: null, error: null };
  try {
    const r = await api('POST', '/api/new-words', { text, lang });
    if (_hintWords.key !== key) return;   // the card moved on while we waited
    _hintWords.words = r.words || [];
  } catch (e) {
    if (_hintWords.key !== key) return;
    _hintWords.words = [];
    _hintWords.error = e.message || 'unavailable';
  }
  const slider = document.getElementById('listen-hint-slider');
  if (slider) onListenHintSlider(slider.value);
}

function _hintLabelFor(level) {
  const label = _HINT_LABELS[level] ?? _HINT_LABELS[1];
  if (level === 1 && _hintWords.error) return `${label} — unavailable`;
  return label;
}

function _updateHintStar(currentVal) {
  const btn = document.getElementById('hint-save-btn');
  if (!btn) return;
  const isSaved = currentVal === _hintSavedDefault();
  btn.textContent = isSaved ? '★' : '☆';
  btn.classList.toggle('saved', isSaved);
}

// The sentence this card's hint is about: story sentence, or the card itself
// for sentence notes (they are excluded from stories).
function _hintSentenceText() {
  const isSentenceNote = card?.note_type === 'sentence';
  return sentence?.sentence_zh || (isSentenceNote ? card?.word_zh : null) || '';
}

async function _initListenHint() {
  const slider = document.getElementById('listen-hint-slider');
  if (!slider) return;
  const saved = _hintSavedDefault();
  slider.value = saved;
  document.getElementById('listen-hint-pct').textContent = _hintLabelFor(saved);
  _updateHintStar(saved);
  _renderListenHint(saved);
  const zh = _hintSentenceText();
  // Fetched at every stop, not just at stop 1: the words that stay visible are
  // tappable (★ List / ✓ Known), which needs the list either way.
  if (zh) await _loadHintNewWords(zh, currentCardLang());
}

function saveListenHintDefault() {
  const val = parseInt(document.getElementById('listen-hint-slider').value, 10);
  localStorage.setItem('listenHintState', val);
  _updateHintStar(val);
}

// ── Word bank tile count slider ───────────────────────────────────────────────
function _wordBankTileDefault() {
  return parseInt(localStorage.getItem('wordBankTiles') ?? '0', 10);
}

function _updateWordBankStar(val) {
  const btn = document.getElementById('word-bank-save-btn');
  if (!btn) return;
  const isSaved = val === _wordBankTileDefault();
  btn.textContent = isSaved ? '★' : '☆';
  btn.classList.toggle('saved', isSaved);
}

function _initWordBankSlider() {
  const slider = document.getElementById('word-bank-slider');
  if (!slider) return;
  const saved = _wordBankTileDefault();
  slider.value = saved;
  document.getElementById('word-bank-slider-pct').textContent = saved;
  _updateWordBankStar(saved);
}

function onWordBankSlider(val) {
  const n = parseInt(val, 10);
  document.getElementById('word-bank-slider-pct').textContent = n;
  _updateWordBankStar(n);
  renderWordBankUI();
}

function saveWordBankDefault() {
  const val = parseInt(document.getElementById('word-bank-slider').value, 10);
  localStorage.setItem('wordBankTiles', val);
  _updateWordBankStar(val);
}

// Every position of the target word (and any co-occurring vocab word) in the
// sentence — always blanked, at every stop: it is what the card is asking for.
function _getTargetPositions(zh) {
  const targetWords = [];
  if (card?.word_zh) targetWords.push(card.word_zh);
  if (sentence?.words) {
    for (const w of sentence.words) {
      if (w.word_zh && !targetWords.includes(w.word_zh)) targetWords.push(w.word_zh);
    }
  }
  const positions = new Set();
  for (const tw of targetWords) {
    // Separable words like "由...组成" — search each part independently
    const parts = tw.includes('...') ? tw.split('...').filter(p => p.length > 0) : [tw];
    for (const part of parts) {
      let start = 0;
      while (true) {
        const idx = zh.indexOf(part, start);
        if (idx === -1) break;
        for (let k = 0; k < part.length; k++) positions.add(idx + k);
        start = idx + part.length;
      }
    }
  }
  return positions;
}

// Every position covered by any of `words` in `text`. Chinese matches
// literally; for the Romance languages the annotator hands back lowercased,
// elision-stripped cores, so match case-insensitively and only on whole words
// ("an" must not light up inside "manger").
function _markWordPositions(text, words, isZh) {
  const positions = new Set();
  const isLetter = ch => !!ch && /[\p{L}\p{M}]/u.test(ch);
  const hay = isZh ? text : text.toLowerCase();
  for (const raw of words) {
    const w = isZh ? raw : (raw || '').toLowerCase();
    if (!w) continue;
    let start = 0;
    while (true) {
      const idx = hay.indexOf(w, start);
      if (idx === -1) break;
      if (isZh || (!isLetter(text[idx - 1]) && !isLetter(text[idx + w.length]))) {
        for (let k = 0; k < w.length; k++) positions.add(idx + k);
      }
      start = idx + w.length;
    }
  }
  return positions;
}

function _renderListenHint(level) {
  const el = document.getElementById('listen-hint-sentence');
  if (!el) return;
  const zh = _hintSentenceText();
  if (!zh) { el.textContent = ''; return; }

  const lang = currentCardLang();
  const isZh = lang === 'zh';
  // What a blank can stand for: any letter or digit — a hanzi, but also the
  // Latin runs and numerals a Chinese sentence still carries ("Musk", "20").
  // #1012: those used to be unmaskable, so they stayed legible even at "Hide
  // all". They are ordinary words now — the prompt writes proper nouns in
  // Chinese, and whatever Latin is left behaves like the rest of the sentence.
  const isMaskable = isZh ? (ch => /[\p{L}\p{M}\p{Nd}]/u.test(ch))
                          : (ch => /[\p{L}\p{M}]/u.test(ch));

  // Sentence notes have no single target word to blank when there is no story.
  const isSentenceNote = card?.note_type === 'sentence';
  const targetPositions = isSentenceNote && !sentence ? new Set() : _getTargetPositions(zh);

  const loaded = _hintWords.key === _hintKey(zh, lang) ? _hintWords.words : null;
  // Positions to leave visible. null = leave everything visible.
  let keep = null;
  if (level >= _HINT_MAX) {
    keep = new Set();
  } else if (level >= 1) {
    // While the words are still loading, blank everything rather than flash
    // the sentence he is supposed to be recalling. On a failure the label says
    // "unavailable" and the sentence stays readable — the honest degradation,
    // since a blanked sentence would look like a deliberate setting.
    keep = _hintWords.error ? null
         : loaded === null ? new Set()
         : _markWordPositions(zh, loaded.map(w => w.word || w.word_zh || ''), isZh);
  }

  let html = '';
  for (let i = 0; i < zh.length; i++) {
    const ch = zh[i];
    if (targetPositions.has(i)) {
      html += `<span class="hint-blank hint-blank-target">_</span>`;
    } else if (!isMaskable(ch) || keep === null || keep.has(i)) {
      html += _escHtml(ch);
    } else {
      html += `<span class="hint-blank">_</span>`;
    }
  }
  el.innerHTML = html;

  // The words left standing are the ones he does not know — tapping one opens
  // the reader's own panel (★ List / ✓ Known, #967). Same table, same
  // handlers, no second add path (#643/#710).
  if (level < _HINT_MAX && loaded && loaded.length) {
    setWordTable(loaded, lang);
    _makeWordsTappable(el);
  }
}

function onListenHintSlider(val) {
  const lvl = parseInt(val, 10);
  document.getElementById('listen-hint-pct').textContent = _hintLabelFor(lvl);
  _updateHintStar(lvl);
  _renderListenHint(lvl);
}

function _adjustListenHintSlider(delta) {
  const slider = document.getElementById('listen-hint-slider');
  if (!slider || document.getElementById('listen-hint-slider-wrap')?.style.display === 'none') return;
  const next = Math.max(_HINT_MIN, Math.min(_HINT_MAX, parseInt(slider.value, 10) + delta));
  slider.value = next;
  onListenHintSlider(next);
}

// ── Render sentence (with target word highlighted) ──────────────────────────
function renderSentence() {
  if (!sentence) {
    return `<span class="hl">${card.word_zh}</span>`;
  }
  let zh = sentence.sentence_zh;
  // Highlight co-occurring vocab words (secondary), then the current card's word (primary)
  const coWords = (sentence.words || []).filter(w => w.word_id !== card.word_id);
  for (const w of coWords) {
    zh = zh.replace(w.word_zh, `<span class="hl-secondary">${w.word_zh}</span>`);
  }
  const targetParts = card.word_zh.includes('...') ? card.word_zh.split('...').filter(p => p.length > 0) : [card.word_zh];
  for (const part of targetParts) {
    zh = zh.replace(part, `<span class="hl">${part}</span>`);
  }
  return `<span>${zh}</span>`;
}

// ── Pick a random 2-char CJK word that doesn't overlap with excludeWord ─────
function pickExtraBlankWord(zh, excludeWord) {
  const excludeIdx = zh.indexOf(excludeWord);
  const excludeEnd = excludeIdx >= 0 ? excludeIdx + excludeWord.length : -1;
  const isCjk = ch => ch >= '\u4E00' && ch <= '\u9FFF';
  const candidates = [];
  for (let i = 0; i < zh.length - 1; i++) {
    if (excludeIdx >= 0 && i < excludeEnd && i + 2 > excludeIdx) continue;
    if (isCjk(zh[i]) && isCjk(zh[i + 1])) candidates.push(i);
  }
  if (!candidates.length) return '';
  const idx = candidates[Math.floor(Math.random() * candidates.length)];
  return zh.slice(idx, idx + 2);
}

// ── Non-zh target matching: dictionary form vs. inflected surface form ─────
// Romance-language sentences rarely use the target's dictionary form verbatim
// (target "réduire" appears as "a réduit"; target "la bourse" appears as just
// "bourse" once the article is dropped/changed to fit the sentence). Chinese
// word forms never change, so this whole module is a no-op for lang==='zh' —
// callers keep using plain indexOf there (see resolveTargetSurfaces below).
// Issue #903.

// Leading articles/determiners a dictionary-form target may carry that the
// sentence's inflected instance can drop or swap out (mirrors the set the
// knowledge-mode sentence matcher accepts server-side, ai._ROMANCE_ARTICLE_PREFIXES,
// plus a couple of the AI is also allowed to use e.g. "l'"/"des").
const _ROMANCE_ARTICLE_PREFIXES = {
  fr: ["le ", "la ", "les ", "l'", "l’", "un ", "une ", "des ", "du ", "de la ", "de l'", "de l’"],
  es: ["el ", "la ", "los ", "las ", "un ", "una ", "unos ", "unas "],
};

function _stripLeadingArticle(word, lang) {
  const prefixes = _ROMANCE_ARTICLE_PREFIXES[lang] || [];
  const lower = word.toLowerCase();
  for (const p of prefixes) {
    if (lower.startsWith(p)) return word.slice(p.length);
  }
  return null;
}

// \b is unreliable on accented letters, so boundaries are hand-rolled:
// neither neighbor may be a letter/digit/apostrophe (the latter so "l'or"
// doesn't count as a boundary-having match for "or").
function _findWordBoundaryMatch(text, candidate, searchFrom) {
  if (!candidate) return null;
  const escaped = candidate.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(`(?<![\\p{L}\\p{N}'’])${escaped}(?![\\p{L}\\p{N}'’])`, 'giu');
  let m;
  while ((m = re.exec(text))) {
    if (m.index >= searchFrom) return [m.index, m.index + m[0].length];
    if (m[0].length === 0) re.lastIndex += 1; // guard against zero-width matches looping
  }
  return null;
}

// Locate one target part in `zh` starting at or after `searchFrom`, returning
// a [start, end) character range or null if nothing matched. zh is a plain,
// case-sensitive, no-boundary indexOf (identical to the pre-#903 behavior);
// other languages try the dictionary form and every stored inflected form
// (longest first, so a multi-word form wins over a bare headword prefix),
// each on a word boundary and case-insensitively, then — if none of those
// hit — strip a leading article from the dictionary form and try once more
// (covers sentences that dropped/changed the article without otherwise
// inflecting the word).
function _locateTargetPart(zh, part, forms, lang, searchFrom) {
  if (lang === 'zh') {
    const idx = zh.indexOf(part, searchFrom);
    return idx < 0 ? null : [idx, idx + part.length];
  }
  const candidates = [...new Set([part, ...(forms || [])])].sort((a, b) => b.length - a.length);
  for (const candidate of candidates) {
    const range = _findWordBoundaryMatch(zh, candidate, searchFrom);
    if (range) return range;
  }
  const stripped = _stripLeadingArticle(part, lang);
  if (stripped) {
    const range = _findWordBoundaryMatch(zh, stripped, searchFrom);
    if (range) return range;
  }
  return null;
}

// Resolve every separable part of `target` (see the "由...组成" handling
// below) to its actual character range in `zh`, in left-to-right order.
// Stops at the first part that can't be located, mirroring the original
// buildWordBankOrder loop it replaces — callers pad any remaining parts with
// blank targets. Pure function so tests can run it directly (issue #903).
function resolveTargetSurfaces(zh, target, forms, lang) {
  const targetParts = target.includes('...') ? target.split('...').filter(p => p.length > 0) : [target];
  const ranges = [];
  let searchFrom = 0;
  for (const part of targetParts) {
    const range = _locateTargetPart(zh, part, forms, lang, searchFrom);
    if (!range) break;
    ranges.push(range);
    searchFrom = range[1];
  }
  return ranges;
}

// ── Word bank: locate the target word inside the sentence ──────────────────
// Pure function (no DOM/global access) so tests can run it directly — see
// tests/test_word_bank.py, which extracts this source and runs it in node.
//
// Returns the ordered token stream [{type:'char'|'target', char|word}, ...].
// The target is located by CHARACTER OFFSET in the raw sentence, not by token
// identity: the tokenizer regularly cuts straight through it (#699 — target
// 活下 in "TÜV在严格监管中活下来。" is tokenized as …中活/下来…, i.e. the
// suffix of one token plus the prefix of the next). Matching whole tokens
// left the target unblanked, printed in plain sight above its own answer box.
function buildWordBankOrder(zh, tokens, target, forms = [], lang = 'zh') {
  // Separable words like "由...组成" — each part is located independently
  const targetParts = target.includes('...') ? target.split('...').filter(p => p.length > 0) : [target];

  // Token texts must rejoin into the exact sentence for offsets to line up;
  // anything else (malformed AI tokens) falls back to per-character tokens.
  let tokenTexts = (tokens && tokens.length) ? tokens.map(t => t[0]) : null;
  if (!tokenTexts || tokenTexts.join('') !== zh) tokenTexts = [...zh];

  // Character ranges of the target parts, searched left to right (issue #903:
  // non-zh matching also tries stored conjugated/inflected forms and a word
  // boundary, not just a bare indexOf of the dictionary form).
  const ranges = resolveTargetSurfaces(zh, target, forms, lang);

  // Slice the token stream at those ranges
  const order = [];
  let pos = 0;
  for (const text of tokenTexts) {
    const start = pos;
    const end = pos + text.length;
    pos = end;
    let cur = start;
    for (const [rs, re] of ranges) {
      if (re <= cur || rs >= end) continue;
      if (rs > cur) order.push({ type: 'char', char: zh.slice(cur, rs) });
      // A target spanning several tokens is emitted once, where it starts
      if (rs >= start) order.push({ type: 'target', word: zh.slice(rs, re) });
      cur = Math.min(re, end);
    }
    if (cur < end) order.push({ type: 'char', char: zh.slice(cur, end) });
  }

  // Target (or some of its parts) missing from the sentence — append a blank
  // for each one so the user is still asked for the word.
  for (let i = ranges.length; i < targetParts.length; i++) {
    order.push({ type: 'target', word: targetParts[i] });
  }
  return order;
}

// ── Word bank (creating mode, non-sentence notes) ─────────────────────────
async function _buildWordBank() {
  const zh = sentence?.sentence_zh;
  // No sentence for this card yet — clear stale state so the previous card's
  // word bank doesn't linger on screen (renderWordBankUI clears the DOM too).
  if (!zh || !card?.word_zh) { wordBankOrder = []; wordBankTokens = []; return; }
  const order = buildWordBankOrder(zh, sentence.tokens, card.word_zh, card.word_forms || [], currentCardLang());

  const MAX_TILES = parseInt(document.getElementById('word-bank-slider')?.value ?? _wordBankTileDefault(), 10);
  // Chinese: a "word" tile is any CJK character. French (space-separated tokens):
  // a tile is any non-whitespace token — whitespace-only tokens (preserved as
  // separate tokens by the tokenizer) must stay pre-placed, never become an
  // empty chip.
  const isWord = tok => currentCardLang() === 'zh'
    ? /[一-鿿㐀-䶿]/.test(tok.char)
    : tok.char.trim().length > 0;
  const allChars = order.filter(it => it.type === 'char');
  allChars.forEach(c => { if (!isWord(c)) c.type = 'pre'; });
  const wordTokens = allChars.filter(c => c.type === 'char');
  if (wordTokens.length > MAX_TILES) {
    const tileIdxSet = new Set();
    while (tileIdxSet.size < MAX_TILES) tileIdxSet.add(Math.floor(Math.random() * wordTokens.length));
    wordTokens.forEach((c, i) => { if (!tileIdxSet.has(i)) c.type = 'pre'; });
  }
  const tileChars = order.filter(it => it.type === 'char');
  const shuffled  = [...tileChars].sort(() => Math.random() - 0.5);

  shuffled.forEach((item, n) => { item.num = n + 1; });

  wordBankOrder  = order;
  wordBankTokens = shuffled;
}

function _parseWordBankInput(text) {
  // Segment into tokens without requiring spaces:
  // - CJK runs → one token (target word)
  // - Digits: greedy 2-digit if it's a valid token number, else single digit
  const isCjk = ch => /[\u3000-\u9FFF\uF900-\uFAFF]/.test(ch);
  const chars = [...text.replace(/\s+/g, '')];
  const raw = [];
  let i = 0;
  while (i < chars.length) {
    if (isCjk(chars[i])) {
      let s = chars[i++];
      while (i < chars.length && isCjk(chars[i])) s += chars[i++];
      raw.push(s);
    } else if (/\d/.test(chars[i])) {
      // Try 2-digit match first
      if (i + 1 < chars.length && /\d/.test(chars[i + 1])) {
        const two = parseInt(chars[i] + chars[i + 1], 10);
        if (wordBankTokens.some(t => t.num === two)) { raw.push(String(two)); i += 2; continue; }
      }
      raw.push(chars[i++]);
    } else {
      // Include punctuation that matches a tile char (e.g. ，。、)
      const ch = chars[i];
      if (wordBankTokens.some(t => t.char === ch)) raw.push(ch);
      i++;
    }
  }
  // Walk wordBankOrder: pre-placed tokens auto-fill; tiles and target come from user input in order
  let rawIdx = 0;
  const result = [];
  for (const tok of wordBankOrder) {
    if (tok.type === 'pre') { result.push(tok.char); continue; }
    if (rawIdx >= raw.length) break; // user hasn't typed this far yet
    const part = raw[rawIdx++];
    if (tok.type === 'char') {
      const n = parseInt(part, 10);
      const tile = isNaN(n) ? null : wordBankTokens.find(t => t.num === n);
      result.push(tile ? tile.char : part);
    } else {
      result.push(part); // target: pass CJK through
    }
  }
  return result;
}

function updateWordBankPreview(text) {
  // Compute slot values by walking wordBankOrder with parsed user tokens
  const isCjk = ch => /[\u3000-\u9FFF\uF900-\uFAFF]/.test(ch);
  const chars = [...text.replace(/\s+/g, '')];
  const raw = [];
  let i = 0;
  while (i < chars.length) {
    if (isCjk(chars[i])) {
      let s = chars[i++];
      while (i < chars.length && isCjk(chars[i])) s += chars[i++];
      raw.push(s);
    } else if (/\d/.test(chars[i])) {
      if (i + 1 < chars.length && /\d/.test(chars[i + 1])) {
        const two = parseInt(chars[i] + chars[i + 1], 10);
        if (wordBankTokens.some(t => t.num === two)) { raw.push(String(two)); i += 2; continue; }
      }
      raw.push(chars[i++]);
    } else {
      const ch = chars[i];
      if (wordBankTokens.some(t => t.char === ch)) raw.push(ch);
      i++;
    }
  }

  // Walk wordBankOrder to assign values to numbered slots
  let rawIdx = 0, slotIdx = 0;
  const usedNums = new Set();
  // Reset: empty target slot falls back to its faint German hint, others to ＿
  document.querySelectorAll('.wb-skel-blank[data-slot]').forEach(span => {
    if (span.dataset.de) { span.textContent = span.dataset.de; span.classList.add('wb-de-hint'); }
    else span.textContent = '＿';
  });

  for (const tok of wordBankOrder) {
    if (tok.type === 'pre') continue;
    const span = document.querySelector(`.wb-skel-blank[data-slot="${slotIdx++}"]`);
    if (rawIdx >= raw.length) continue;
    const part = raw[rawIdx++];
    if (tok.type === 'char') {
      const n = parseInt(part, 10);
      const tile = isNaN(n) ? null : wordBankTokens.find(t => t.num === n);
      if (tile) { usedNums.add(tile.num); if (span) span.textContent = tile.char; }
      else if (span) span.textContent = part;
    } else {
      // target word filled: replace faint German hint with the typed text
      if (span) { span.textContent = part; span.classList.remove('wb-de-hint'); }
    }
  }

  // Grey out used tile buttons
  document.querySelectorAll('.wb-token-btn').forEach(btn => {
    const num = parseInt(btn.querySelector('.wb-num').textContent, 10);
    btn.classList.toggle('wb-used', usedNums.has(num));
  });
}

function wordBankAddToken(num) {
  const inp = document.getElementById('word-bank-input');
  const cur = inp.value.trim();
  inp.value = cur ? cur + ' ' + num : String(num);
  updateWordBankPreview(inp.value);
  inp.focus();
}

async function renderWordBankUI() {
  await _buildWordBank();
  if (!wordBankOrder.length) {
    // Sentence not loaded / no match — clear any stale skeleton + tiles from the
    // previous card instead of leaving them on screen as a wrong sentence.
    document.getElementById('word-bank-skeleton')?.replaceChildren();
    document.getElementById('word-bank-tokens')?.replaceChildren();
    return;
  }

  // Sentence skeleton: pre-placed tokens shown as text, blanks for tiles/target (data-slot for live update)
  const skelEl = document.getElementById('word-bank-skeleton');
  if (skelEl) {
    const escAttr = s => String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    // Hint shown faintly inside the target word's blank (first target only):
    // prefer the German definition, fall back to English when no German exists.
    const deHint = (card.definition_de || card.definition || '').trim();
    let deShown = false;
    let slotIdx = 0;
    skelEl.innerHTML = wordBankOrder.map(tok => {
      if (tok.type === 'pre') return `<span class="wb-skel-pre">${tok.char}</span>`;
      const slot = slotIdx++;
      if (tok.type === 'target' && deHint && !deShown) {
        deShown = true;
        return `<span class="wb-skel-blank wb-de-hint" data-slot="${slot}" data-de="${escAttr(deHint)}">${escAttr(deHint)}</span>`;
      }
      return `<span class="wb-skel-blank" data-slot="${slot}">＿</span>`;
    }).join('');
  }

  const tokensEl = document.getElementById('word-bank-tokens');
  tokensEl.innerHTML = wordBankTokens.map(tok =>
    `<button class="wb-token-btn" onmousedown="event.preventDefault()" onclick="wordBankAddToken(${tok.num})">`
    + `<span class="wb-num">${tok.num}</span>`
    + `<span class="wb-char">${tok.char}</span>`
    + `</button>`
  ).join('');

  const inp = document.getElementById('word-bank-input');
  inp.value = '';
  userInput = '';
  setTimeout(() => inp.focus(), 80);
}

// ── Cloze sentence (creating category, non-sentence notes) ──────────────────
function renderClozeSentence() {
  const inputEl = `<input class="cloze-inline-input" id="cloze-inline-input" type="text"`
    + ` autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"`
    + ` style="width:5.8em"`
    + ` onkeydown="if(event.key==='Enter')revealAnswer()">`;
  if (!sentence) return `<span>${inputEl}</span>`;
  const zh = sentence.sentence_zh;

  // Pick an extra word to blank out (chosen before any replacements)
  clozeExtraWord = pickExtraBlankWord(zh, card.word_zh);

  // Use a temporary placeholder so the two replacements don't interfere. zh
  // keeps the exact pre-#903 literal-substring check; other languages locate
  // the actual inflected surface form via the shared resolveTargetSurfaces()
  // (also used by buildWordBankOrder) instead of matching the bare headword.
  let text;
  if (currentCardLang() === 'zh') {
    text = zh.includes(card.word_zh)
      ? zh.replace(card.word_zh, '\x00T\x00')
      : `${zh} \x00T\x00`;
  } else {
    const ranges = resolveTargetSurfaces(zh, card.word_zh, card.word_forms || [], currentCardLang());
    if (ranges.length) {
      const [start, end] = ranges[0];
      text = zh.slice(0, start) + '\x00T\x00' + zh.slice(end);
    } else {
      text = `${zh} \x00T\x00`;
    }
  }

  if (clozeExtraWord && text.includes(clozeExtraWord)) {
    const blank = `<span class="cloze-blank">${'＿'.repeat(clozeExtraWord.length)}</span>`;
    text = text.replace(clozeExtraWord, blank);
  }

  text = text.replace('\x00T\x00', inputEl);
  return `<span>${text}</span>`;
}

// ── Cloze answer diff ────────────────────────────────────────────────────────
function diffClozeAnswer(userInput, targetWord) {
  if (!userInput) return { html: '<span class="ch-miss">(no answer)</span>', pct: 0 };
  const userChars   = [...userInput];
  const targetChars = [...targetWord];
  const html = userChars.map((ch, i) => {
    if (ch === targetChars[i]) return `<span class="ch-match">${ch}</span>`;
    return `<span class="ch-miss">${ch}</span>`;
  }).join('');
  const matched = userChars.filter((ch, i) => ch === targetChars[i]).length;
  const pct = targetChars.length > 0 ? Math.round((matched / targetChars.length) * 100) : 0;
  return { html, pct };
}

function _renderMultiRatingIfNeeded() {
  document.getElementById('rating-row').style.display = '';
}

// ── Submit rating ───────────────────────────────────────────────────────────
async function rate(rating) {
  document.querySelectorAll('.r-btn').forEach(b => b.disabled = true);
  let _cardMs = null;
  if (_timerStart) {
    // Cap at 40s: time spent past that likely isn't real study time.
    _cardMs = Math.min(Date.now() - _timerStart, _TIMER_CAP_MS);
    _sessionTotalMs += _cardMs;
    _sessionRatedCount++;
    _updateAvgTimeBadge();
  }
  try {
    let url = `/api/review?card_id=${card.id}&rating=${rating}`;
    if (_cardMs != null) url += `&duration_ms=${_cardMs}`;
    const noteInput = document.getElementById('next-note-input');
    if (noteInput) url += `&next_note=${encodeURIComponent(noteInput.value)}`;
    if (unfinishedMode) url += `&unfinished_mode=true&unfinished_scope=${_unfinishedScope}`;
    else if (rootDeckId) url += `&root_deck_id=${rootDeckId}`;
    else if (deckId) url += `&parent_deck_id=${deckId}`;
    url += _langQP('&');
    const reviewedId = card.id;
    const result = await api('POST', url);
    _sessionReviewedCount++;
    if (reviewedId != null) _sessionReviewedIds.push(reviewedId);
    if (result.transition?.changed) showStateChangeAnim(result.transition);
    if (typeof invalidateHomeCalendar === 'function') invalidateHomeCalendar();
    if (typeof invalidateHomeEvolution === 'function') invalidateHomeEvolution();
    api('GET', `/api/retention?days=0${_langQP('&')}`).then(r => {
      _retentionData = r;
      _updateReviewRRBadge(deckId);
    }).catch(() => {});
    if (!result.next_card) {
      _lastCounts = result.counts || _lastCounts;
      rootDeckId = null;
      unfinishedMode = false;
      showView('done');
      return;
    }
    if (unfinishedMode || rootDeckId) category = result.next_card.category;
    loadCard(result.next_card, result.counts);
    document.getElementById('undo-btn').disabled = false;
  } catch (e) {
    showError('Submit failed: ' + e.message);
    document.querySelectorAll('.r-btn').forEach(b => b.disabled = false);
  }
}

// ── "New sentence" — re-show this card soon with a freshly generated sentence ──
// Lives on the FRONT of the card: when the sentence reads badly, swap it before
// even flipping. Not a rating: scheduling (ease/interval/state/today's count) is
// untouched. The card is soft-requeued ~1 min out while a new sentence
// regenerates in the background, so the user reviews other cards meanwhile.
async function requeueNewSentence() {
  const btn = document.getElementById('new-sentence-btn');
  if (btn) btn.disabled = true;
  try {
    let url = `/api/review/requeue?card_id=${card.id}`;
    if (unfinishedMode) url += `&unfinished_mode=true&unfinished_scope=${_unfinishedScope}`;
    else if (rootDeckId) url += `&root_deck_id=${rootDeckId}`;
    else if (deckId) url += `&parent_deck_id=${deckId}`;
    url += _langQP('&');
    const result = await api('POST', url);
    if (!result.next_card) {
      _lastCounts = result.counts || _lastCounts;
      rootDeckId = null;
      unfinishedMode = false;
      showView('done');
      return;
    }
    if (unfinishedMode || rootDeckId) category = result.next_card.category;
    loadCard(result.next_card, result.counts);
    if (btn) btn.disabled = false;
  } catch (e) {
    showError('Could not requeue: ' + e.message);
    if (btn) btn.disabled = false;
  }
}

// ── Undo last rating ─────────────────────────────────────────────────────────
async function undoReview() {
  try {
    const result = await api('POST', '/api/review/undo');
    showView('review');
    loadCard(result.card, result.counts);
    // Show the back of the card so the user can re-rate
    revealAnswer();
    // Only disable when the stack is empty (allow multiple undos like Anki/Word)
    document.getElementById('undo-btn').disabled = result.stack_size === 0;
  } catch (e) {
    showError('Nothing to undo');
  }
}

// ── Pinyin toggle ────────────────────────────────────────────────────────────
let pinyinCache = {};

async function _loadPinyinRow(text) {
  const row = document.getElementById('pinyin-row');
  if (!text || row.dataset.loadedFor === text) return;
  if (!pinyinCache[text]) {
    try {
      const data = await api('GET', `/api/pinyin?text=${encodeURIComponent(text)}`);
      pinyinCache[text] = data.syllables;
    } catch (e) {
      return;
    }
  }
  const syllables = pinyinCache[text];
  const chars = [...text];
  const wordStart = text.indexOf(card.word_zh);
  const wordEnd = wordStart + [...card.word_zh].length;
  row.innerHTML = chars.map((_ch, i) => {
    const py = syllables[i] || '';
    const isTarget = wordStart >= 0 && i >= wordStart && i < wordEnd;
    return `<span class="py-char${isTarget ? ' py-target' : ''}">`+
             `<span class="py-syl">${py}</span>`+
           `</span>`;
  }).join('');
  row.dataset.loadedFor = text;
}

async function togglePinyin() {
  if (currentCardLang() !== 'zh') return; // no-op for non-Chinese decks
  const row = document.getElementById('pinyin-row');
  const text = sentence?.sentence_zh || card?.word_zh;
  if (!text) return;
  await _loadPinyinRow(text);
  row.classList.toggle('pinyin-revealed');
  _syncCardToggleBar();
}

// ── Back-side tap toggle bar (#535) ───────────────────────────────────────────
// Gives touch/mouse users the p (pinyin) and t (translation) shortcuts on the
// back of a card. Each button shows only when the card has that content and
// highlights while it's currently visible, so tapping and pressing the key stay
// in sync. Called on reveal and after every relevant toggle.
function _syncCardToggleBar() {
  const bar = document.getElementById('card-toggle-bar');
  if (!bar) return;
  const pinBtn   = document.getElementById('toggle-pinyin-btn');
  const transBtn = document.getElementById('toggle-trans-btn');

  // Pinyin: Chinese decks only (pypinyin garbles other scripts).
  const pinAvail = currentCardLang() === 'zh' && !!(sentence?.sentence_zh || card?.word_zh);
  pinBtn.style.display = pinAvail ? '' : 'none';
  pinBtn.classList.toggle('active',
    !!document.getElementById('pinyin-row')?.classList.contains('pinyin-revealed'));

  // Translation: only when this card carries fr/de text.
  const fr = document.getElementById('sentence-fr');
  const de = document.getElementById('sentence-de');
  const transAvail = !!(fr?.textContent || de?.textContent);
  transBtn.style.display = transAvail ? '' : 'none';
  const transVisible = (fr?.textContent && fr.style.display !== 'none') ||
                       (de?.textContent && de.style.display !== 'none');
  transBtn.classList.toggle('active', !!transVisible);

  // Star: only when this card actually shows a stored story sentence. A card
  // with no sentence (no story yet — renderSentence() falls back to the bare
  // word) has nothing to star, so the button stays hidden rather than failing.
  const starBtn = document.getElementById('toggle-star-btn');
  const starAvail = !!sentence?.id;
  if (starBtn) {
    starBtn.style.display = starAvail ? '' : 'none';
    _syncStarBtn();
  }

  // Flag: same availability rule as star — no stored sentence, nothing to flag.
  const flagBtn = document.getElementById('toggle-flag-btn');
  const flagAvail = !!sentence?.id;
  if (flagBtn) {
    flagBtn.style.display = flagAvail ? '' : 'none';
    _syncFlagBtn();
  }

  // Ask-AI button (#853): same "does this card even have a sentence" gate as
  // the star button, plus it needs AI to actually be reachable — no point
  // showing a button that will just 400 in offline/hard-offline mode.
  const questionBtn = document.getElementById('toggle-question-btn');
  const questionAvail = starAvail && !_offlineMode;
  if (questionBtn) questionBtn.style.display = questionAvail ? '' : 'none';

  bar.style.display = (pinAvail || transAvail || starAvail || flagAvail || questionAvail) ? '' : 'none';
}

// ── Starred sentences (#692) ─────────────────────────────────────────────────
// While reviewing, a sentence is either good or it isn't — and that judgement is
// only available in the second you read it. Starring collects those good ones as
// positive examples for tuning the generation prompts later (Browse → ★).

function _syncStarBtn() {
  const btn = document.getElementById('toggle-star-btn');
  if (!btn) return;
  const on = !!sentence?.starred;
  btn.textContent = on ? '★' : '☆';
  btn.classList.toggle('active', on);
  btn.title = on ? 'Starred — click to unstar (Shift+F)'
                 : 'Star this sentence as a good example (Shift+F)';
}

async function toggleSentenceStar() {
  if (!sentence?.id) return;
  const next = !sentence.starred;
  // Optimistic: the star is a note to self, and a stalled button mid-review is
  // more disruptive than a star that turns out not to have saved.
  sentence.starred = next ? 1 : 0;
  _syncStarBtn();
  try {
    const r = await api('POST', `/api/story-sentence/${sentence.id}/star`, { starred: next });
    sentence.starred = r.starred;
    sentence.starred_at = r.starred_at;
  } catch (e) {
    sentence.starred = next ? 0 : 1;
    showError('Star failed: ' + e.message);
  }
  _syncStarBtn();
}

// ── Ask AI about this sentence (#853) ────────────────────────────────────────
// Single-turn, in-place grammar/naturalness check — sentence generation
// occasionally produces awkward or outright wrong sentences, and this is the
// only moment (right when reading it) that catching that is easy.

function openSentenceQuestionModal() {
  if (!sentence?.sentence_zh) return;
  document.getElementById('sentence-question-overlay').style.display = 'block';
  document.getElementById('sentence-question-modal').style.display = 'flex';
  document.getElementById('sentence-question-sentence').textContent = sentence.sentence_zh;
  const input = document.getElementById('sentence-question-input');
  input.value = '';
  input.disabled = false;
  document.getElementById('sentence-question-submit').disabled = false;
  const answerEl = document.getElementById('sentence-question-answer');
  answerEl.textContent = '';
  answerEl.className = '';
  input.focus();
}

function closeSentenceQuestionModal() {
  document.getElementById('sentence-question-overlay').style.display = 'none';
  document.getElementById('sentence-question-modal').style.display = 'none';
}

async function submitSentenceQuestion() {
  if (!sentence?.sentence_zh) return;
  const input = document.getElementById('sentence-question-input');
  const submitBtn = document.getElementById('sentence-question-submit');
  const answerEl = document.getElementById('sentence-question-answer');

  input.disabled = true;
  submitBtn.disabled = true;
  answerEl.textContent = 'Asking…';
  answerEl.className = 'sq-loading';

  try {
    // Raw fetch (not the shared api() helper) so a 400/500 shows the actual
    // server-side reason (HTTPException detail), not just an HTTP status code
    // — "静默失败" is explicitly ruled out for this feature.
    const res = await fetch('/api/sentence-question', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sentence_zh: sentence.sentence_zh,
        question: input.value.trim(),
        word_zh: card?.word_zh,
        lang: currentCardLang(),
      }),
    });
    if (res.status === 401) { location.href = '/login'; return; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);
    // AI text rendered with textContent only — never innerHTML (#853, same
    // rule as /dict).
    answerEl.textContent = data.answer;
    answerEl.className = '';
  } catch (e) {
    // Show the server's error text as-is — a silent failure here is worse
    // than an ugly one, since the whole point is trustworthy feedback.
    answerEl.textContent = 'Failed: ' + e.message;
    answerEl.className = 'sq-error';
  } finally {
    input.disabled = false;
    submitBtn.disabled = false;
  }
}

// ── Flagged sentences (#854) ─────────────────────────────────────────────────
// Mirror of starring, for the other judgment: a sentence that reads badly
// (grammar mistake, awkward phrasing) — a negative example worth reading back
// when tuning the generation prompts (Browse → ⚑). Independent of the star —
// not a three-way toggle.

function _syncFlagBtn() {
  const btn = document.getElementById('toggle-flag-btn');
  if (!btn) return;
  const on = !!sentence?.flagged;
  btn.textContent = on ? '⚑' : '⚐';
  btn.classList.toggle('active', on);
  btn.title = on ? 'Flagged — click to unflag (Shift+G)'
                 : 'Flag this sentence as a bad example (Shift+G)';
}

async function toggleSentenceFlag() {
  if (!sentence?.id) return;
  const next = !sentence.flagged;
  // Optimistic, same reasoning as toggleSentenceStar(): a stuck button mid-review
  // is worse than an occasional flag that turns out not to have saved.
  sentence.flagged = next ? 1 : 0;
  _syncFlagBtn();
  try {
    const r = await api('POST', `/api/story-sentence/${sentence.id}/flag`, { flagged: next });
    sentence.flagged = r.flagged;
    sentence.flagged_at = r.flagged_at;
  } catch (e) {
    sentence.flagged = next ? 0 : 1;
    showError('Flag failed: ' + e.message);
  }
  _syncFlagBtn();
}

// ── Translation toggle (German/French sentence translation) ───────────────────
// Hidden by default to save space; press u to show/hide. Only elements that
// actually have text participate, and they toggle together as one group.
function toggleTranslation() {
  const fr = document.getElementById('sentence-fr');
  const de = document.getElementById('sentence-de');
  const anyVisible = (fr.textContent && fr.style.display !== 'none') ||
                     (de.textContent && de.style.display !== 'none');
  const show = !anyVisible;
  fr.style.display = (show && fr.textContent) ? '' : 'none';
  de.style.display = (show && de.textContent) ? '' : 'none';
  // Hiding the translation with u while "always show" is on deactivates the preference.
  if (!show && _alwaysTranslation) {
    _alwaysTranslation = false;
    localStorage.setItem('alwaysTranslation', '0');
  }
  _syncTransEye();
  _syncCardToggleBar();
}

// Persistent "always show translation" preference (survives across sessions).
let _alwaysTranslation = localStorage.getItem('alwaysTranslation') === '1';

// The eye icon only appears while the translation is visible; it is highlighted
// when "always show" is on, grayed out when off.
function _syncTransEye() {
  const fr  = document.getElementById('sentence-fr');
  const de  = document.getElementById('sentence-de');
  const eye = document.getElementById('always-trans-eye');
  if (!eye) return;
  const visible = (fr.textContent && fr.style.display !== 'none') ||
                  (de.textContent && de.style.display !== 'none');
  eye.style.display = visible ? '' : 'none';
  eye.classList.toggle('active', _alwaysTranslation);
}

function toggleAlwaysTranslation() {
  setAlwaysTranslation(!_alwaysTranslation);
}

function setAlwaysTranslation(on) {
  _alwaysTranslation = !!on;
  localStorage.setItem('alwaysTranslation', _alwaysTranslation ? '1' : '0');
  // Turning on reveals the translation immediately so the change is visible;
  // turning off only changes the default for future cards (leaves the current one).
  if (_alwaysTranslation) {
    const fr = document.getElementById('sentence-fr');
    const de = document.getElementById('sentence-de');
    if (fr.textContent) fr.style.display = '';
    if (de.textContent) de.style.display = '';
    const enFront = document.getElementById('sentence-en-front');
    if (enFront.textContent) enFront.style.display = 'block';
  }
  _syncTransEye();
  _syncFrontTransToggle();
}

// ── Creating-mode front translation toggle (replaces the old blur-on-hover, #515) ──
// Same shortcut ('t') and persistent "always show" preference as the back-side
// toggle above — pressing t on the front (creating mode, card not flipped) or
// tapping the 🇩🇪 button (mobile, no keyboard) shows/hides this element only.
function toggleCreatingFrontTranslation() {
  const front = document.getElementById('sentence-en-front');
  if (!front || !front.textContent) return;
  const show = front.style.display === 'none';
  front.style.display = show ? 'block' : 'none';
  // Hiding it while "always show" is on deactivates the preference (mirrors toggleTranslation).
  if (!show && _alwaysTranslation) {
    _alwaysTranslation = false;
    localStorage.setItem('alwaysTranslation', '0');
  }
  _syncFrontTransToggle();
}

// The tap button stays visible at all times in creating mode (it's the only way to
// reveal the translation on mobile), but highlights when the translation is showing.
function _syncFrontTransToggle() {
  const front = document.getElementById('sentence-en-front');
  const btn   = document.getElementById('sentence-en-front-toggle');
  if (!btn) return;
  const visible = !!(front && front.textContent && front.style.display !== 'none');
  btn.classList.toggle('active', visible);
}

// ── Creating-mode translation hint toggle (🇬🇧/🇫🇷/🇩🇪) ───────────────────────
// The hint is hidden by default so the user recalls first; press k to peek, or
// click the eye icon to keep it always visible (persistent preference).
// Two placements exist (plain input vs word bank); only one holds content at a time.
const _WORDDEF_PLACEMENTS = [
  ['creating-word-def',    'creating-word-def-eye'],
  ['creating-word-def-wb', 'creating-word-def-wb-eye'],
];

function _activeWordDefHint() {
  return _WORDDEF_PLACEMENTS
    .map(([hintId]) => document.getElementById(hintId))
    .find(el => el && el.innerHTML.trim()) || null;
}

function toggleWordDef() {
  const hint = _activeWordDefHint();
  if (!hint) return;
  const show = hint.style.display === 'none';
  hint.style.display = show ? 'block' : 'none';
  // Hiding while "always show" is on turns the preference off.
  if (!show && _alwaysWordDef) {
    _alwaysWordDef = false;
    localStorage.setItem('alwaysWordDef', '0');
  }
  _syncWordDefEye();
}

// Persistent "always show creating-mode hint" preference (survives across sessions).
let _alwaysWordDef = localStorage.getItem('alwaysWordDef') === '1';

// The eye icon only appears while the hint is visible; highlighted when "always show" is on.
function _syncWordDefEye() {
  for (const [hintId, eyeId] of _WORDDEF_PLACEMENTS) {
    const hint = document.getElementById(hintId);
    const eye  = document.getElementById(eyeId);
    if (!hint || !eye) continue;
    const visible = hint.innerHTML.trim() && hint.style.display !== 'none';
    eye.style.display = visible ? '' : 'none';
    eye.classList.toggle('active', _alwaysWordDef);
  }
}

function toggleAlwaysWordDef() { setAlwaysWordDef(!_alwaysWordDef); }

function setAlwaysWordDef(on) {
  _alwaysWordDef = !!on;
  localStorage.setItem('alwaysWordDef', _alwaysWordDef ? '1' : '0');
  // Turning on reveals the hint immediately so the change is visible.
  if (_alwaysWordDef) {
    const hint = _activeWordDefHint();
    if (hint) hint.style.display = 'block';
  }
  _syncWordDefEye();
}

// ── Story error modal ─────────────────────────────────────────────────────────
let _storyErrorResolve = null;

function _openStoryErrorModal(errorData) {
  document.getElementById('story-error-msg').textContent =
    `Failed using ${errorData.model}: ${errorData.reason}`;
  const histBtn  = document.getElementById('story-error-history-btn');
  const histNote = document.getElementById('story-error-history-note');
  if (errorData.has_history) {
    histBtn.disabled = false;
    histBtn.style.opacity = '';
    histNote.textContent = '⚠ Saved sentences may not include all current words';
    histNote.style.display = '';
  } else {
    histBtn.disabled = true;
    histBtn.style.opacity = '0.4';
    histNote.style.display = 'none';
  }
  const sel = document.getElementById('story-error-model');
  for (const opt of sel.options) {
    if (opt.value !== errorData.model) { opt.selected = true; break; }
  }
  document.getElementById('story-error-overlay').style.display = 'block';
  document.getElementById('story-error-modal').style.display = 'flex';
  return new Promise(r => { _storyErrorResolve = r; });
}

function _closeStoryErrorModal() {
  document.getElementById('story-error-overlay').style.display = 'none';
  document.getElementById('story-error-modal').style.display = 'none';
}

function storyErrorSkip() {
  _closeStoryErrorModal();
  if (_storyErrorResolve) { _storyErrorResolve({ action: 'skip' }); _storyErrorResolve = null; }
}

function storyErrorRetry() {
  const model = document.getElementById('story-error-model').value;
  _closeStoryErrorModal();
  if (_storyErrorResolve) { _storyErrorResolve({ action: 'retry', model }); _storyErrorResolve = null; }
}

function storyErrorUseHistory() {
  _closeStoryErrorModal();
  if (_storyErrorResolve) { _storyErrorResolve({ action: 'history' }); _storyErrorResolve = null; }
}

async function _resolveStory(storyData, resolvedeckId, resolveCat, topic, maxHsk, grammarFocus, grammarPct, mode = 'story') {
  if (!storyData?.error) return storyData;
  const choice = await _openStoryErrorModal(storyData);
  if (choice.action === 'skip') return null;
  if (choice.action === 'history') {
    try { return await api('GET', `/api/story/${resolvedeckId}/${resolveCat}/history${_langQP('?')}`); }
    catch (_) { return null; }
  }
  // retry with new model — not counted toward the 2-attempt limit
  setLoading('Generating your story…', true);
  setLoadingStep(10, null, 'Sending request to AI…');
  _startFakeProgress(10, 55, 45000);
  _startStoryProgressPoll(resolvedeckId, resolveCat);
  let newData;
  try {
    newData = await api('GET', `/api/story/${resolvedeckId}/${resolveCat}` + _storyParams(topic, maxHsk, choice.model, grammarFocus, grammarPct, mode));
  } catch (e) {
    newData = { error: true, reason: e.message, model: choice.model, has_history: storyData.has_history };
  }
  _stopFakeProgress(); _stopStoryProgressPoll();
  return _resolveStory(newData, resolvedeckId, resolveCat, topic, maxHsk, grammarFocus, grammarPct, mode);
}

// ── Story setup modal ────────────────────────────────────────────────────────
let _setupResolve = null;
let _setupIsRegen = false;
let _setupIsMixed = false;
let _setupIsUnfinished = false;
let _setupIsDeckListRegen = false;
let _deckListRegenId = null;
// Category the deck-list ↺ targets (#857): 'unified' for the parent-row button
// (whole-deck mixed story, unchanged pre-#857 behavior), or 'listening' /
// 'reading' / 'creating' when a mode pill's own ↺ was clicked.
let _deckListRegenCategory = 'unified';

function openStorySetup(sentenceCount, { isMixed = false, isUnfinished = false, learningCount = 0, estimatedTokens = 0 } = {}) {
  _setupIsRegen = !isMixed && !isUnfinished && !!card; // card exists (fresh single-cat) → regenerating
  _setupIsMixed = isMixed;
  _setupIsUnfinished = isUnfinished;
  _setupIsDeckListRegen = false;
  const _countLabel = document.getElementById('setup-count-label');
  _countLabel.textContent =
    `This story will have ${sentenceCount} sentence${sentenceCount !== 1 ? 's' : ''}.`;
  // Remember this text so switching away from Words-only mode restores it (#547).
  _countLabel.dataset.storyText = _countLabel.textContent;
  const warn = document.getElementById('setup-learning-warning');
  if (learningCount > 0) {
    warn.textContent = `⚠ ${learningCount} card${learningCount !== 1 ? 's' : ''} still in the Again queue. Generating now may cause a mismatch between story order and review order.`;
    warn.style.display = 'block';
  } else {
    warn.style.display = 'none';
  }
  const tokenWarn = document.getElementById('setup-token-warning');
  if (tokenWarn) {
    if (estimatedTokens > 3000) {
      tokenWarn.textContent = `⚠ ~${estimatedTokens.toLocaleString()} tokens estimated. This story is large and may be slow or expensive.`;
      tokenWarn.style.display = 'block';
    } else {
      tokenWarn.style.display = 'none';
    }
  }
  document.getElementById('setup-topic').value = '';
  document.getElementById('setup-grammar').value = '';
  document.getElementById('setup-grammar-pct').value = 50;
  // Knowledge-item multi-select (issue #752): each fresh open of the modal starts
  // with nothing picked, same as topic/grammar being cleared above — a leftover
  // selection from a previous session would silently reuse the wrong sources.
  _setupSelectedEpisodes.clear();
  _renderSetupSelectedEpisodes();
  // In-session regenerate: prefill mode + HSK from the active story's gen_params
  // so a quick regenerate keeps Kontextsummary / kahneman / … instead of silently
  // downgrading to mode=story (issue #468 — a briefing was overwritten exactly
  // this way). Other entry points (fresh session, unfinished mode) keep the
  // 'story' default so another deck's mode is never carried over by accident.
  let _prefillGp = null;
  if (_setupIsRegen) {
    try {
      const raw = story?.gen_params;
      _prefillGp = raw ? (typeof raw === 'string' ? JSON.parse(raw) : raw) : null;
    } catch { _prefillGp = null; }
  }
  // Default background level: HSK 3 for zh, A2 (=2) for CEFR languages (#596)
  const _setupLang = setupLang();
  document.getElementById('setup-hsk-slider').value =
    _prefillGp?.max_hsk ?? (_setupLang === 'zh' ? 3 : 2);
  const _modeSel = document.getElementById('setup-mode');
  // Remembered mode (#972): Daniel almost always regenerates with the same mode
  // he used last time, so re-defaulting to 'story' on every open means picking
  // it again by hand each day. Stored per language because the zh-only modes
  // (kahneman/contextsummary/paste) don't exist for fr/es — a fr deck must not
  // inherit 'contextsummary' from the zh tab. gen_params still wins for regenerate.
  _modeSel.value = _prefillGp?.mode || localStorage.getItem('setupMode:' + _setupLang) || 'story';
  if (_modeSel.selectedIndex < 0) _modeSel.value = 'story'; // unknown/stale mode
  updateHskLabel();
  _applySetupLangRestrictions();
  updateSetupMode();
  document.getElementById('setup-modal-overlay').style.display = 'block';
  document.getElementById('setup-modal').style.display        = 'flex';
  document.getElementById('setup-topic').focus();
  return new Promise(resolve => { _setupResolve = resolve; });
}

// Story setup modal: kahneman/contextsummary/paste and grammar-focus are
// Chinese-only server features (backend rejects those modes, and grammar
// patterns like 把字句 don't apply to French) — hide them for non-zh decks.
// Knowledge mode is deliberately NOT in this list (issue #806): it's
// language-agnostic (the source material's language doesn't matter, the AI
// writes in the deck's target language), so it stays visible for every deck.
function _applySetupLangRestrictions() {
  const lang = setupLang();
  const modeSelect = document.getElementById('setup-mode');
  const zhOnlyOptions = modeSelect.querySelectorAll('option.setup-mode-zh-only');
  // hidden alone is ignored by iOS Safari for <option> — disabled greys it out there
  zhOnlyOptions.forEach(opt => { opt.hidden = lang !== 'zh'; opt.disabled = lang !== 'zh'; });
  if (lang !== 'zh' && zhOnlyOptions && [...zhOnlyOptions].some(o => o.value === modeSelect.value)) {
    modeSelect.value = 'story';
  }
  const grammarLabel = document.getElementById('setup-grammar-label');
  if (grammarLabel) grammarLabel.style.display = lang === 'zh' ? '' : 'none';
  // Difficulty slider label follows the deck's level system (#596)
  const hskLabelText = document.getElementById('setup-hsk-label-text');
  if (hskLabelText) hskLabelText.textContent = lang === 'zh'
    ? 'Max HSK level for background vocabulary'
    : 'Max CEFR level for background vocabulary';
}

function togglePriceTable(e) {
  e.preventDefault();
  e.stopPropagation();
  const popup = document.getElementById('price-table-popup');
  popup.style.display = popup.style.display === 'none' ? 'block' : 'none';
}

function updateHskLabel() {
  const v = parseInt(document.getElementById('setup-hsk-slider').value, 10);
  const lang = setupLang();
  document.getElementById('setup-hsk-badge').textContent = levelLabel(lang, v);
}

function updateSetupMode() {
  const mode = document.getElementById('setup-mode').value;
  const topicLabel = document.getElementById('setup-topic-label');
  const topicInput = document.getElementById('setup-topic');
  const btn = document.getElementById('setup-generate-btn');
  const kahnemanSection = document.getElementById('setup-kahneman-section');
  const pasteSection = document.getElementById('setup-paste-section');
  const podcastSection = document.getElementById('setup-podcast-section');
  const bookSection = document.getElementById('setup-book-section');
  pasteSection.style.display = 'none';
  if (podcastSection) podcastSection.style.display = 'none';
  if (bookSection) bookSection.style.display = 'none';
  _autoSwitchModelForMode(mode);

  // Words-only mode (issue #547): no story is generated, so the story-only
  // controls (model, HSK-background level, grammar focus) don't apply — hide them.
  // Non-vocab modes restore Model/HSK unconditionally; grammar follows the deck's
  // language (Chinese-only, same rule as _applySetupLangRestrictions).
  const isVocab = mode === 'vocab';
  const modelLabel = document.getElementById('setup-model-label');
  const hskLabel = document.getElementById('setup-hsk-label');
  const grammarLabel = document.getElementById('setup-grammar-label');
  if (modelLabel) modelLabel.style.display = isVocab ? 'none' : '';
  if (hskLabel) hskLabel.style.display = isVocab ? 'none' : '';
  // Words per AI call (issue #574): per-mode persisted value, hidden in
  // words-only mode (nothing is generated there).
  const batchLabel = document.getElementById('setup-batch-label');
  const batchInp = document.getElementById('setup-batch-size');
  if (batchLabel) batchLabel.style.display = isVocab ? 'none' : '';
  if (batchInp) batchInp.value = _savedBatchSize(mode) || '';
  // Prompt template editor (issue #581): only modes with an editable template,
  // zh decks only (fr uses the built-in language-neutral prompt path).
  const editPromptBtn = document.getElementById('setup-edit-prompt-btn');
  if (editPromptBtn) {
    const editable = ['story', 'qa', 'expository', 'knowledge'].includes(mode)
      && setupLang() === 'zh';
    editPromptBtn.style.display = editable ? '' : 'none';
  }
  if (grammarLabel) grammarLabel.style.display =
    isVocab ? 'none' : (setupLang() === 'zh' ? '' : 'none');
  const countLabel = document.getElementById('setup-count-label');
  // Restore the story-count text when switching back from Words-only mode.
  if (!isVocab && countLabel && countLabel.dataset.storyText != null)
    countLabel.textContent = countLabel.dataset.storyText;

  if (mode === 'qa') {
    topicLabel.childNodes[0].textContent = 'Question ';
    topicInput.placeholder = 'e.g. How was life in ancient China?';
    btn.textContent = 'Generate answer';
    topicLabel.style.display = '';
    kahnemanSection.style.display = 'none';
  } else if (mode === 'expository') {
    topicLabel.childNodes[0].textContent = 'Topic ';
    topicInput.placeholder = 'e.g. The Second World War';
    btn.textContent = 'Generate text';
    topicLabel.style.display = '';
    kahnemanSection.style.display = 'none';
  } else if (mode === 'kahneman') {
    topicLabel.style.display = 'none';
    kahnemanSection.style.display = 'block';
    btn.textContent = 'Generate Kahneman';
    _loadKahnemanChapters();
  } else if (mode === 'contextsummary') {
    // Kontextsummary (issue #1011): the renamed News flow, now sourced from the
    // knowledge base — it reuses knowledge mode's item multi-select rather than
    // getting a second copy of the same picker.
    topicLabel.style.display = 'none';
    kahnemanSection.style.display = 'none';
    if (podcastSection) podcastSection.style.display = 'block';
    btn.textContent = 'Generate Kontextsummary';
    _loadPodcastEpisodesForSetup();
  } else if (mode === 'paste') {
    topicLabel.style.display = 'none';
    kahnemanSection.style.display = 'none';
    pasteSection.style.display = 'block';
    btn.textContent = 'Generate summary';
    if (!document.getElementById('setup-paste-blocks').children.length) addPasteBlock();
  } else if (mode === 'knowledge') {
    topicLabel.style.display = 'none';
    kahnemanSection.style.display = 'none';
    if (podcastSection) podcastSection.style.display = 'block';
    btn.textContent = 'Generate from source';
    _loadPodcastEpisodesForSetup();
  } else if (mode === 'book') {
    topicLabel.style.display = 'none';
    kahnemanSection.style.display = 'none';
    if (bookSection) bookSection.style.display = 'block';
    btn.textContent = 'Generate from chapter';
    _loadBooksForSetup();
  } else if (mode === 'vocab') {
    topicLabel.style.display = 'none';
    kahnemanSection.style.display = 'none';
    btn.textContent = 'Start (words only)';
    if (countLabel) countLabel.textContent = 'Review the due words directly — no story is generated.';
  } else {
    topicLabel.childNodes[0].textContent = 'Topic ';
    topicInput.placeholder = 'e.g. a day at a coffee shop';
    btn.textContent = 'Generate story';
    topicLabel.style.display = '';
    kahnemanSection.style.display = 'none';
  }
}

// Dropdown value meaning "let the server pick the model" — mirrors
// routes/story.SERVER_MODEL_SENTINEL. Not a model: the backend maps it to
// BRIEFING_MODEL and it is deliberately absent from ALLOWED_MODELS.
const SERVER_MODEL_VALUE = 'briefing-server';

// ── Modes that default to the server-resolved model ─────────────────────────
// paste (#910) and contextsummary (#1011) share the server-side briefing
// pipeline. Neither locks the dropdown: the OpenAI lock only ever existed
// because DeepSeek censors *news*, and #1011 removed the news auto-fetch
// entirely — pasted text and knowledge-base items are not news. Both merely
// *default* to the server placeholder, the only configuration this pipeline is
// verified on. knowledge mode made the same move earlier (#561/#640).

// Per-mode remembered model (issue #561): knowledge mode has its own
// first-time default, then remembers whatever the user picked last. Since
// #640 that default is DeepSeek (like kahneman) rather than gpt-5-mini — must
// stay in sync with ai.DEFAULT_MODEL, which is the backend-side default.
// paste/contextsummary default to the server placeholder itself.
const MODE_MODEL_DEFAULTS = {
  knowledge: 'deepseek-v4-flash',
  book: 'deepseek-v4-flash',
  paste: SERVER_MODEL_VALUE,
  contextsummary: SERVER_MODEL_VALUE,
};
let _modelSelMode = 'story';   // mode the model dropdown's current value belongs to

// Modes whose dropdown carries the "let the server decide" option.
const SERVER_MODEL_MODES = ['paste', 'contextsummary'];

function _autoSwitchModelForMode(mode) {
  const modelSel = document.getElementById('setup-model');
  if (!modelSel) return;

  // The "let the server decide" option exists for the modes the server may
  // resolve on its own. routes/story.py maps this value back to BRIEFING_MODEL
  // (SERVER_MODEL_SENTINEL there).
  const wantsServerOpt = SERVER_MODEL_MODES.includes(mode);
  let serverOpt = document.getElementById('setup-model-server-opt');
  if (wantsServerOpt && !serverOpt) {
    serverOpt = document.createElement('option');
    serverOpt.id = 'setup-model-server-opt';
    serverOpt.value = SERVER_MODEL_VALUE;
    serverOpt.textContent = 'Server: BRIEFING_MODEL (gpt-5.6-luna)';
    modelSel.appendChild(serverOpt);
  }

  modelSel.disabled = false;
  modelSel.title = wantsServerOpt
    ? 'Server: BRIEFING_MODEL is the default — pick another model to override it'
    : '';
  if (serverOpt && !wantsServerOpt) {
    // Leaving a server-placeholder mode: the placeholder is not a model, so it
    // must never survive into a mode whose branch does not understand it.
    if (modelSel.value === SERVER_MODEL_VALUE) modelSel.value = '';
    serverOpt.remove();
  }
  // Every mode remembers its own model selection (issue #561) — save the
  // outgoing mode's value, then restore the incoming mode's last pick (or its
  // hardcoded default, or just leave the current value untouched).
  // paste/contextsummary may persist the server placeholder: for them it is a
  // real choice ("let the server decide"), not a locked-in stand-in (#910).
  if (_modelSelMode
      && (modelSel.value !== SERVER_MODEL_VALUE || SERVER_MODEL_MODES.includes(_modelSelMode)))
    localStorage.setItem('setupModel:' + _modelSelMode, modelSel.value);
  const remembered = localStorage.getItem('setupModel:' + mode);
  modelSel.value = remembered || MODE_MODEL_DEFAULTS[mode] || modelSel.value;
  if (!modelSel.value) modelSel.selectedIndex = 0;   // remembered value no longer in the list
  _modelSelMode = mode;
}

// ── Paste mode: repeatable pasted-content blocks ────────────────────────────
let _pasteBlockSeq = 0;

function addPasteBlock() {
  const container = document.getElementById('setup-paste-blocks');
  const id = `paste-block-${_pasteBlockSeq++}`;
  const block = document.createElement('div');
  block.className = 'paste-block';
  block.id = id;
  block.style.cssText = 'border:1px solid var(--border,#ddd);border-radius:6px;padding:8px;margin-bottom:8px';
  block.innerHTML = `
    <div style="display:flex;gap:8px;margin-bottom:6px;align-items:center">
      <input class="edit-input paste-block-title" type="text" placeholder="Title (optional)" style="flex:1;min-width:0;box-sizing:border-box">
      <input class="edit-input paste-block-url" type="url" placeholder="URL (optional)" style="flex:1;min-width:0;box-sizing:border-box">
      <button class="edit-cancel-btn" style="padding:4px 10px;font-size:12px;flex-shrink:0" onclick="document.getElementById('${id}').remove()">✕</button>
    </div>
    <textarea class="edit-input paste-block-text" rows="5" placeholder="Paste content here…" style="display:block;width:100%;box-sizing:border-box;resize:vertical"></textarea>`;
  container.appendChild(block);
}

function _collectPastedContents() {
  return Array.from(document.querySelectorAll('#setup-paste-blocks .paste-block'))
    .map(block => ({
      title: block.querySelector('.paste-block-title').value.trim(),
      url: block.querySelector('.paste-block-url').value.trim(),
      text: block.querySelector('.paste-block-text').value.trim(),
    }))
    .filter(a => a.text);
}

let _kahnemanChapters = null;

async function _ensureKahnemanChapters() {
  if (_kahnemanChapters) return;
  try {
    const data = await api('GET', '/api/kahneman/chapters');
    if (data.available && data.chapters.length) _kahnemanChapters = data.chapters;
  } catch (e) { /* silent */ }
}

// ── Kahneman examples popup (chapter summary + book's original quotes) ──────────
const _kahnemanExamplesCache = {}; // chapter number → { summary, examples }

async function openKahnemanExamples(chNum, conceptZh) {
  const overlay = document.getElementById('kahneman-examples-overlay');
  const modal   = document.getElementById('kahneman-examples-modal');
  const titleEl = document.getElementById('kahneman-examples-title');
  const bodyEl  = document.getElementById('kahneman-examples-body');
  titleEl.textContent = conceptZh || `第${chNum}章`;
  bodyEl.innerHTML = '<div class="kahneman-examples-loading">加载中…</div>';
  overlay.style.display = '';
  modal.style.display = '';

  let chapter = _kahnemanExamplesCache[chNum];
  if (!chapter) {
    try {
      const data = await api('GET', `/api/kahneman/chapter/${chNum}`);
      chapter = {
        summary: data.chapter?.concept_zh || '',
        detail: data.chapter?.summary_zh || '',
        examples: data.chapter?.examples_zh || [],
        part: data.chapter?.part_zh || '',
        titleEn: data.chapter?.title_en || '',
      };
      _kahnemanExamplesCache[chNum] = chapter;
    } catch (e) { chapter = { summary: '', examples: [], part: '' }; }
  }
  if (modal.style.display === 'none') return; // closed while loading
  const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const enHtml = chapter.titleEn
    ? `<div class="kahneman-title-en">${esc(chapter.titleEn)}</div>` : '';
  const partHtml = chapter.part
    ? `<div class="kahneman-part-label">${esc(chapter.part)}</div>` : '';
  const summaryHtml = chapter.summary
    ? `<div class="kahneman-summary">${esc(chapter.summary)}</div>` : '';
  const detailHtml = chapter.detail
    ? `<div class="kahneman-examples-label">本章机制与典型情境</div>`
      + `<div class="kahneman-detail">${esc(chapter.detail)}</div>`
    : '';
  const examplesHtml = chapter.examples.length
    ? `<div class="kahneman-examples-label">书中原句</div>`
      + chapter.examples.map(ex => `<p class="kahneman-example">${esc(ex)}</p>`).join('')
    : '<div class="kahneman-examples-loading">本章暂无书中原句。</div>';
  bodyEl.innerHTML = enHtml + partHtml + summaryHtml + detailHtml + examplesHtml;
}

function closeKahnemanExamples() {
  document.getElementById('kahneman-examples-overlay').style.display = 'none';
  document.getElementById('kahneman-examples-modal').style.display = 'none';
}

let _currentReasoning = '';
let _currentSourceUrl = '';
let _currentReasoningIsNews = false;
// Knowledge mode (#931) is the third kind of reasoning popup, next to
// kahneman's and news flow's — the title is picked from this rather than from
// another boolean, because "which of N" stops being expressible as a flag the
// moment there are three of them.
let _currentReasoningIsKnowledge = false;

// Episode id encoded in a sentence's source_url, or null.
//
// Since #790 knowledge mode stores the IN-APP detail page as source_url
// ("/#podcast-12" for podcasts, "/#knowledge-12" for everything else), never
// the external article/video URL. So the episode id a sentence came from is
// already carried per sentence — no gen_params lookup (which only knows the
// story's FIRST source, wrong as soon as several were selected, #752/#776)
// and no new column. Returns null for kahneman, briefing/news flow, book mode
// and any external URL. Kontextsummary (#1011) DOES match — its material comes
// from the knowledge base — so ask _hidesInlineContext() below, not this
// function, whenever the question is "does this mode use the light bulb".
function _episodeIdFromSourceUrl(url) {
  const m = /^\/#(?:podcast|knowledge)-(\d+)$/.exec(url || '');
  return m ? parseInt(m[1], 10) : null;
}

// Whether this sentence's explanation belongs in the 💡 popup (knowledge/book,
// #931) instead of an inline context block above the sentence.
//
// The episode id alone is NOT enough since #1011: Kontextsummary sources its
// material from the knowledge base too, so its sentences carry the same in-app
// source_url — but it IS the inline-context mode (context_de / reasoning_zh are
// the whole point of it), so it must not be swept into the knowledge branch by
// URL shape. Navigation stays URL-based (an in-app hash link must pop the modal
// rather than open a tab) — only the context/light-bulb split is mode-based.
function _hidesInlineContext(s) {
  return _episodeIdFromSourceUrl(s?.source_url) !== null
      && _activeStoryMode() !== 'contextsummary';
}
// Mode of the last story generation started from the setup modal ('' when the
// session was resumed without it) — only used to pick the background-popup title.
let _currentStoryMode = '';

// Mode of the currently loaded story — reads the story's stored gen_params
// (survives session resume, unlike _currentStoryMode).
function _activeStoryMode() {
  try {
    const gp = story?.gen_params;
    if (gp) return (typeof gp === 'string' ? JSON.parse(gp) : gp).mode || '';
  } catch { /* malformed gen_params — fall through */ }
  return _currentStoryMode || '';
}

// ── Kontextsummary display language (issue #452) ────────────────────────────
// Toggle whether the context text and article titles show in the original
// language (German, 'de') or Chinese ('zh'). Publisher (source_name) is a brand
// name, always shown as-is. Persisted in localStorage; press g or use the
// settings switch to flip. Chinese title falls back to the German source title
// for the briefing pipeline (which has no AI headline).
let _newsflowLang = (localStorage.getItem('newsflowLang') === 'zh') ? 'zh' : 'de';

function _escHtml(t) {
  return String(t == null ? '' : t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Chinese summary of a knowledge item (#708): a full translation of the German
// one, carrying the same <p>/<b> markup. Mirrors podcast._summary_zh_html —
// escape everything, then let only those structural tags back through, so a
// model that ignores the contract can't inject markup into the page.
// Summaries written before #708 are plain text: their blank-line paragraphs
// become <p> here, since the CSS no longer preserves newlines.
function _summaryZhHtml(t) {
  const esc = _escHtml(t).replace(/&lt;(\/?(?:p|b|strong|em|i)|br\s*\/?)&gt;/g, '<$1>');
  if (esc.includes('<p>')) return esc;
  return esc.split(/\n\s*\n/).map(p => p.trim()).filter(Boolean)
            .map(p => `<p>${p.replace(/\n/g, '<br>')}</p>`).join('');
}

// Context text for the current news card in the selected language.
function _newsContextText(s) {
  if (!s) return '';
  return _newsflowLang === 'zh' ? (s.reasoning_zh || '') : (s.context_de || '');
}

// Small "title · publisher" HTML (an <a> when a source_url exists, else <span>).
function _newsSourceHtml(s) {
  if (!s) return '';
  const title = _newsflowLang === 'zh'
    ? (s.concept_zh || s.source_title || '')
    : (s.source_title || '');
  const label = [title, s.source_name || ''].filter(Boolean).join(' · ');
  if (!label) return '';
  const inner = `${_escHtml(label)} ↗`;
  if (s.source_url) {
    const href = _escHtml(s.source_url).replace(/"/g, '&quot;');
    return `<a class="news-source-line" href="${href}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${inner}</a>`;
  }
  return `<span class="news-source-line">${_escHtml(label)}</span>`;
}

// Knowledge mode's 📄 source button (#931), used on both card faces.
//
// Same idea as book mode's chapter link (#865): the source title points at an
// in-app page (#790), so letting the <a> navigate would throw Daniel out of
// the review session. Clicking pops the item's summary in the shared modal
// (#930) and closing it lands back on the card, mid-review.
//
// Applies to every sentence whose source_url is an in-app hash link, which
// since #1011 includes Kontextsummary — that mode keeps its inline context but
// its source title is an in-app page just the same, so it must pop the modal
// rather than open a tab. Historical news/briefing stories carry real external
// article links and keep opening in a new tab.
function _wireKnowledgeSourceLink(container, s) {
  const epId = _episodeIdFromSourceUrl(s?.source_url);
  if (epId === null || !container) return;
  const link = container.querySelector('a.news-source-line');
  if (!link) return;
  link.onclick = (e) => {
    e.preventDefault();
    e.stopPropagation();
    openKnowledgeSummaryPopup(epId, s?.source_title || '');
  };
}

// Front side: context (above the Chinese sentence) + source line (below it).
function _renderNewsFront() {
  const ctxDe = document.getElementById('sentence-context-de');
  const url = sentence?.source_url || '';
  // Knowledge mode (#931): the reasoning lives in the 💡 popup now, not printed
  // above the sentence. On the front it was worse than clutter — reasoning_zh
  // starts with the very fact the sentence retells, so a reading card showed
  // the answer before it was flipped. Kontextsummary keeps its context (#452).
  const ctxText = _hidesInlineContext(sentence) ? '' : _newsContextText(sentence);
  ctxDe.textContent = ctxText;
  ctxDe.style.display = ctxText ? 'block' : 'none';
  const ctxClickable = !!(ctxText && url);
  ctxDe.classList.toggle('clickable-sentence', ctxClickable);
  ctxDe.onclick = ctxClickable ? () => window.open(url, '_blank', 'noopener') : null;

  // Title · publisher on the front for every category (issue #464) — listening
  // and creating cards were previously excluded, matching nothing on the back.
  const srcEl = document.getElementById('news-source-front');
  if (srcEl) {
    const html = _newsSourceHtml(sentence);
    srcEl.innerHTML = html;
    srcEl.style.display = html ? 'block' : 'none';
    _wireKnowledgeSourceLink(srcEl, sentence);
  }
}

// Back side: context + source line, small, above the target sentence
// (replaces the old light bulb for the briefing pipeline). Kahneman keeps its light bulb.
function _renderNewsBackSource(s) {
  const el = document.getElementById('news-back-source');
  if (!el) return;
  const ctx = _newsContextText(s);
  const srcHtml = _newsSourceHtml(s);
  if (!ctx && !srcHtml) { el.style.display = 'none'; el.innerHTML = ''; return; }
  const url = s?.source_url || '';
  // Knowledge mode (#931): the reasoning moves into the 💡 popup (wired up in
  // the card renderer) and the source title becomes the 📄 button below, so
  // the inline context block is NOT rendered here — Daniel asked for the two
  // clean kahneman-style buttons instead of a wall of text above the sentence.
  // Kontextsummary keeps its inline context: that block is what #452/#454/#464
  // deliberately built, and its cards have no light bulb to move it into.
  const knowledgeEpId = _hidesInlineContext(s) ? _episodeIdFromSourceUrl(url) : null;
  // Title · publisher above the context (issue #454).
  let html = srcHtml;
  if (ctx && knowledgeEpId === null) html += `<div class="news-back-context${url ? ' clickable-sentence' : ''}">${_escHtml(ctx)}</div>`;
  el.innerHTML = html;
  el.style.display = 'block';
  if (ctx && url && knowledgeEpId === null) {
    const c = el.querySelector('.news-back-context');
    if (c) c.onclick = () => window.open(url, '_blank', 'noopener');
  }
  // Knowledge mode's 📄 button (#931): same idea as book mode's chapter link
  // below — the source title is an in-app page, so opening it in a new tab
  // would throw Daniel out of the review session. Clicking pops the item's
  // summary in the shared modal (openKnowledgeSummaryPopup, #930) and closing
  // it lands back on the card, mid-review, exactly where he was.
  _wireKnowledgeSourceLink(el, s);
  // Book mode's clickable source title (issue #865): the same news-back-source
  // slot knowledge mode uses, but a book chapter isn't an external page to open
  // in a new tab — clicking it pops the chapter's own summary modal (#864's
  // kahneman-style concept + summary), which is what Daniel actually asked for
  // ("quick summary of this chapter while reviewing").
  const bookLink = el.querySelector('a.news-source-line');
  if (bookLink) {
    const m = /^\/#book-(\d+)-chapter-(\d+)$/.exec(bookLink.getAttribute('href') || '');
    if (m) {
      bookLink.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        openBookChapterSummary(parseInt(m[1], 10), parseInt(m[2], 10));
      };
    }
  }
}

// Flip the news-flow display language and re-render the visible news UI.
function toggleNewsflowLang() {
  _newsflowLang = _newsflowLang === 'zh' ? 'de' : 'zh';
  localStorage.setItem('newsflowLang', _newsflowLang);
  _renderNewsFront();
  _renderNewsBackSource(sentence);
  _syncNewsflowLangSwitch();
}

// Keep the settings switch label in sync with the current value.
function _syncNewsflowLangSwitch() {
  const sw = document.getElementById('newsflow-lang-switch');
  if (sw) {
    sw.checked = _newsflowLang === 'zh';
    const lbl = document.getElementById('newsflow-lang-value');
    if (lbl) lbl.textContent = _newsflowLang === 'zh' ? '中文' : 'Original (DE)';
  }
}

function setNewsflowLangFromSwitch(checked) {
  _newsflowLang = checked ? 'zh' : 'de';
  localStorage.setItem('newsflowLang', _newsflowLang);
  _renderNewsFront();
  _renderNewsBackSource(sentence);
  _syncNewsflowLangSwitch();
}

function openReasoning() {
  if (!_currentReasoning && !_currentSourceUrl) return;
  document.getElementById('reasoning-modal-title').textContent =
    _currentReasoningIsKnowledge
      ? '💡 为什么选这句?'
      : _currentReasoningIsNews
        ? (_currentStoryMode === 'paste' ? '📋 内容背景' : '📰 新闻背景')
        : '💡 为什么这句体现本章偏误?';
  document.getElementById('reasoning-body').textContent = _currentReasoning;
  const linkEl = document.getElementById('reasoning-source-link');
  if (_currentSourceUrl) {
    linkEl.href = _currentSourceUrl;
    linkEl.style.display = '';
  } else {
    linkEl.style.display = 'none';
  }
  document.getElementById('reasoning-overlay').style.display = '';
  document.getElementById('reasoning-modal').style.display = '';
}

function closeReasoning() {
  document.getElementById('reasoning-overlay').style.display = 'none';
  document.getElementById('reasoning-modal').style.display = 'none';
}

async function _loadKahnemanChapters() {
  const container = document.getElementById('setup-kahneman-chapters');
  const loading = document.getElementById('setup-kahneman-loading');
  if (_kahnemanChapters) { _renderKahnemanChapters(); return; }
  container.style.display = 'none';
  loading.style.display = 'block';
  try {
    const data = await api('GET', '/api/kahneman/chapters');
    if (!data.available || !data.chapters.length) {
      loading.textContent = 'No chapters found. Run python extract_kahneman.py first.';
      return;
    }
    _kahnemanChapters = data.chapters;
    loading.style.display = 'none';
    container.style.display = 'block';
    _renderKahnemanChapters();
  } catch (e) {
    loading.textContent = 'Failed to load chapters.';
  }
}

function _renderKahnemanChapters() {
  const container = document.getElementById('setup-kahneman-chapters');
  if (!_kahnemanChapters) return;
  let lastPart = null;
  container.innerHTML = _kahnemanChapters.map(ch => {
    // Insert a part header (with a select-all checkbox) before the first chapter of each part.
    let header = '';
    if (ch.part_number != null && ch.part_number !== lastPart) {
      lastPart = ch.part_number;
      header = `
      <label class="kahneman-part-header">
        <input type="checkbox" class="kahneman-part-cb" data-part="${ch.part_number}"
               onchange="_toggleKahnemanPart(${ch.part_number}, this.checked)">
        <span class="kahneman-part-title">${ch.part_zh || ''}</span>
      </label>`;
    }
    return header + `
    <label class="kahneman-chapter-row">
      <input type="checkbox" class="kahneman-chapter-cb" value="${ch.number}" data-part="${ch.part_number}" onchange="_updateKahnemanCount()">
      <div class="kahneman-chapter-info">
        <span class="kahneman-chapter-title">第${ch.number}章 ${ch.title_zh}</span>
        <span class="kahneman-chapter-concept">${ch.concept_zh}</span>
      </div>
    </label>`;
  }).join('');
  _updateKahnemanCount();
}

function _toggleKahnemanPart(partNum, checked) {
  document.querySelectorAll(`.kahneman-chapter-cb[data-part="${partNum}"]`)
    .forEach(cb => { cb.checked = checked; });
  _updateKahnemanCount();
}

function _updateKahnemanCount() {
  const checked = document.querySelectorAll('.kahneman-chapter-cb:checked').length;
  const countEl = document.getElementById('setup-kahneman-count');
  countEl.textContent = checked ? `(${checked} selected)` : '(none selected → random 5)';
  // Sync each part checkbox: checked if all chapters selected, indeterminate if some.
  document.querySelectorAll('.kahneman-part-cb').forEach(partCb => {
    const part = partCb.dataset.part;
    const chapters = document.querySelectorAll(`.kahneman-chapter-cb[data-part="${part}"]`);
    const sel = document.querySelectorAll(`.kahneman-chapter-cb[data-part="${part}"]:checked`).length;
    partCb.checked = sel > 0 && sel === chapters.length;
    partCb.indeterminate = sel > 0 && sel < chapters.length;
  });
}

function randomKahnemanChapters() {
  if (!_kahnemanChapters) return;
  const all = Array.from(document.querySelectorAll('.kahneman-chapter-cb'));
  all.forEach(cb => { cb.checked = false; });
  const indices = [];
  while (indices.length < Math.min(5, all.length)) {
    const i = Math.floor(Math.random() * all.length);
    if (!indices.includes(i)) indices.push(i);
  }
  indices.forEach(i => { all[i].checked = true; });
  _updateKahnemanCount();
}

// ── Knowledge item picker (issue #482, feed filter #561, kind filter #654) —
// single-select, template copied from the kahneman chapter selector above
// but radio-style (one item per story). Renamed from podcast-only to
// knowledge (podcast/video/article) in #654 — element ids kept as
// setup-podcast-* to minimize churn (they're internal, not user-facing).
let _setupPodcastFeeds = null;              // null = not loaded yet
// key `${kind}|${feedId}` ('' feedId = all podcasts, only meaningful for kind=podcast) → episodes array
let _setupPodcastEpisodesByFeed = {};
let _setupKnowledgeKind = localStorage.getItem('setupKnowledgeKind') || 'podcast';
// Multi-select of knowledge-base items (issue #752): id -> {id, title, kind}. This
// Map — not the checkbox DOM — is the single source of truth, because switching
// Source type or podcast feed re-renders #setup-podcast-episodes from scratch and
// would otherwise wipe out any selection made under a different kind/feed.
// Modes that source their material from the knowledge base and therefore share
// this multi-select: knowledge (#752) and contextsummary (#1011).
const KNOWLEDGE_SOURCE_MODES = ['knowledge', 'contextsummary'];
let _setupSelectedEpisodes = new Map();
// Words per AI call (issue #563 podcast-only, #574 all modes): persisted per
// mode like setupModel. null = the mode's default chunking. Read by
// _storyParams for every generation URL.
function _savedBatchSize(mode) {
  return parseInt(localStorage.getItem('setupBatch:' + mode), 10) || null;
}
// One-time migrations: the podcast-only #563 key, then the #654 mode rename
// (podcast -> knowledge) so an existing per-mode batch size still applies.
(() => {
  const old = localStorage.getItem('podcastBatchSize');
  if (old) {
    localStorage.setItem('setupBatch:podcast', old);
    localStorage.removeItem('podcastBatchSize');
  }
  const oldPodcastBatch = localStorage.getItem('setupBatch:podcast');
  if (oldPodcastBatch && !localStorage.getItem('setupBatch:knowledge')) {
    localStorage.setItem('setupBatch:knowledge', oldPodcastBatch);
  }
  const oldPodcastModel = localStorage.getItem('setupModel:podcast');
  if (oldPodcastModel && !localStorage.getItem('setupModel:knowledge')) {
    localStorage.setItem('setupModel:knowledge', oldPodcastModel);
  }
})();

async function _loadPodcastEpisodesForSetup() {
  const container = document.getElementById('setup-podcast-episodes');
  const loading = document.getElementById('setup-podcast-loading');
  const feedSel = document.getElementById('setup-podcast-feed');
  const kindSel = document.getElementById('setup-knowledge-kind');
  if (!container) return;
  if (kindSel) {
    kindSel.value = _setupKnowledgeKind;
    kindSel.onchange = _onKnowledgeKindChange;
  }
  // The feed filter dropdown only makes sense for the podcast kind — videos
  // and articles aren't grouped by RSS feed (issue #654).
  if (feedSel) feedSel.style.display = _setupKnowledgeKind === 'podcast' ? '' : 'none';
  if (_setupPodcastFeeds === null) {
    try {
      const feeds = await api('GET', '/api/podcast/feeds');
      _setupPodcastFeeds = feeds || [];
      if (feedSel) {
        const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        feedSel.innerHTML = '<option value="">All podcasts</option>' +
          _setupPodcastFeeds.map(f => `<option value="${f.id}">${esc(f.title || f.url)}</option>`).join('');
        feedSel.onchange = _onPodcastFeedFilterChange;
      }
    } catch (e) {
      _setupPodcastFeeds = [];
    }
  }
  await _loadPodcastEpisodesForCurrentFeed();
}

// Source-type switch (🎙️ podcast | 📺 video | 📄 article |
// 📰 newsletter, issues #654/#925) —
// re-renders the episode list for the newly selected kind.
function _onKnowledgeKindChange() {
  const kindSel = document.getElementById('setup-knowledge-kind');
  _setupKnowledgeKind = (kindSel && kindSel.value) || 'podcast';
  localStorage.setItem('setupKnowledgeKind', _setupKnowledgeKind);
  const feedSel = document.getElementById('setup-podcast-feed');
  if (feedSel) feedSel.style.display = _setupKnowledgeKind === 'podcast' ? '' : 'none';
  // The list is re-rendered from scratch for the new kind, but the selection
  // itself survives (issue #752) — _setupSelectedEpisodes, not the DOM, is
  // the source of truth, and _renderPodcastEpisodes re-checks boxes from it.
  _loadPodcastEpisodesForCurrentFeed();
}

function _onPodcastFeedFilterChange() {
  _loadPodcastEpisodesForCurrentFeed();
}

async function _loadPodcastEpisodesForCurrentFeed() {
  const container = document.getElementById('setup-podcast-episodes');
  const loading = document.getElementById('setup-podcast-loading');
  const feedSel = document.getElementById('setup-podcast-feed');
  if (!container) return;
  const kind = _setupKnowledgeKind || 'podcast';
  const feedId = (kind === 'podcast' && feedSel) ? feedSel.value : '';
  const cacheKey = `${kind}|${feedId}`;
  if (_setupPodcastEpisodesByFeed[cacheKey]) { _renderPodcastEpisodes(cacheKey); return; }
  container.style.display = 'none';
  if (loading) loading.style.display = 'block';
  try {
    // kind=podcast keeps the existing feed_id filter; video/article ignore it
    // and just list every item of that kind (issue #654 — /api/podcast/episodes
    // already supports ?kind= since #651).
    const url = '/api/podcast/episodes?limit=1000&kind=' + encodeURIComponent(kind) +
      (feedId ? `&feed_id=${feedId}` : '');
    const data = await api('GET', url);
    // Only items with a finished summary can seed a story (issue #482).
    _setupPodcastEpisodesByFeed[cacheKey] = (data || []).filter(ep => ep.status === 'summarized');
    if (loading) loading.style.display = 'none';
    container.style.display = 'block';
    _renderPodcastEpisodes(cacheKey);
  } catch (e) {
    if (loading) loading.textContent = 'Failed to load items.';
  }
}

function _renderPodcastEpisodes(cacheKey) {
  const container = document.getElementById('setup-podcast-episodes');
  const episodes = _setupPodcastEpisodesByFeed[cacheKey];
  if (!container || !episodes) return;
  if (!episodes.length) {
    container.innerHTML = '<div style="padding:12px;text-align:center;color:var(--muted,#888);font-size:13px">No summarized items yet.</div>';
    return;
  }
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  // Checkboxes, not radios (issue #752 — multiple items can seed one story).
  // `checked` is driven by _setupSelectedEpisodes, not by what was checked
  // before the list was re-rendered (e.g. after switching Source type).
  container.innerHTML = episodes.map(ep => `
    <label class="kahneman-chapter-row">
      <input type="checkbox" class="podcast-episode-cb" value="${ep.id}"
        ${_setupSelectedEpisodes.has(ep.id) ? 'checked' : ''}
        onchange="_onEpisodeCheckboxChange(this, ${ep.id}, '${_setupKnowledgeKind}')">
      <div class="kahneman-chapter-info">
        <span class="kahneman-chapter-title">${esc(ep.title || '(untitled)')}</span>
        <span class="kahneman-chapter-concept">${esc(_localDate(ep.published_at || ''))}</span>
      </div>
    </label>`).join('');
}

// Checkbox change handler for the knowledge-item list (issue #752). Keeps
// _setupSelectedEpisodes (the source of truth) in sync and re-renders the
// "Selected: N" chip list above it. Title/kind are captured from the row at
// check-time so the chip list still shows a name after the underlying list
// is re-rendered for a different kind/feed.
function _onEpisodeCheckboxChange(cb, id, kind) {
  if (cb.checked) {
    // Pull the title straight from the row rather than re-searching the
    // cached lists — the row that fired this event always has it.
    const title = cb.closest('.kahneman-chapter-row')
      ?.querySelector('.kahneman-chapter-title')?.textContent || `#${id}`;
    _setupSelectedEpisodes.set(id, { id, title, kind });
  } else {
    _setupSelectedEpisodes.delete(id);
  }
  _renderSetupSelectedEpisodes();
}

// Renders the "Selected: N" chip list above the item picker (issue #752).
// Hidden entirely when nothing is selected — an empty chip bar is just noise.
function _renderSetupSelectedEpisodes() {
  const box = document.getElementById('setup-podcast-selected');
  if (!box) return;
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const items = Array.from(_setupSelectedEpisodes.values());
  if (!items.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
  box.style.display = 'block';
  const kindIcon = { podcast: '🎙️', video: '📺', article: '📄', newsletter: '📰' };
  box.innerHTML = `<div style="font-size:12px;color:var(--muted,#888);margin-bottom:4px">Selected: ${items.length}</div>` +
    items.map(it => `
      <span style="display:inline-flex;align-items:center;gap:4px;background:var(--hover-bg,#f0f0f0);
        border-radius:12px;padding:2px 8px 2px 10px;margin:0 4px 4px 0;font-size:12px;max-width:100%">
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:220px">${kindIcon[it.kind] || ''} ${esc(it.title)}</span>
        <span role="button" aria-label="Remove" onclick="_setupRemoveEpisode(${it.id})"
          style="cursor:pointer;color:var(--muted,#888);font-weight:700;padding:0 2px">×</span>
      </span>`).join('');
}

// Removing via the chip's × (issue #752): drop from the selection map, then
// uncheck the corresponding checkbox if it's currently visible in the list
// (it won't be if the user is looking at a different kind/feed right now).
function _setupRemoveEpisode(id) {
  _setupSelectedEpisodes.delete(id);
  const cb = document.querySelector(`.podcast-episode-cb[value="${id}"]`);
  if (cb) cb.checked = false;
  _renderSetupSelectedEpisodes();
}

function _getSelectedChapterIds() {
  return Array.from(document.querySelectorAll('.kahneman-chapter-cb:checked'))
    .map(cb => parseInt(cb.value));
}

// ── Book chapter picker (issue #865): two dropdowns, book then chapter — a
// reading list makes no sense here (unlike knowledge mode's multi-select),
// Daniel's ask was "pick a book, pick a chapter". Only EPUBs are listed
// (PDFs have no chapter structure, #864) and only chapters with
// status==='summarized' are selectable (an unsummarized chapter has no
// material to build sentences from).
let _setupBooks = null;          // null = not loaded yet; [] once loaded (possibly empty)
let _setupBookChaptersCache = {}; // book id -> chapters response ({chapters, available, reason})

async function _loadBooksForSetup() {
  const bookSel = document.getElementById('setup-book-select');
  const loading = document.getElementById('setup-book-loading');
  if (!bookSel) return;
  if (_setupBooks === null) {
    if (loading) loading.style.display = 'block';
    try {
      const books = await api('GET', '/api/books');
      _setupBooks = (books || []).filter(b => b.format === 'epub');
    } catch (e) {
      _setupBooks = [];
    }
    if (loading) loading.style.display = 'none';
  }
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  if (!_setupBooks.length) {
    bookSel.innerHTML = '<option value="">No books uploaded yet</option>';
    bookSel.disabled = true;
    _renderSetupBookChapters([], null);
    return;
  }
  bookSel.disabled = false;
  bookSel.innerHTML = _setupBooks.map(b => `<option value="${b.id}">${esc(b.title)}</option>`).join('');
  await _onSetupBookChange();
}

async function _onSetupBookChange() {
  const bookSel = document.getElementById('setup-book-select');
  const loading = document.getElementById('setup-book-loading');
  if (!bookSel || !bookSel.value) { _renderSetupBookChapters([], null); return; }
  const bookId = parseInt(bookSel.value, 10);
  if (!_setupBookChaptersCache[bookId]) {
    if (loading) loading.style.display = 'block';
    try {
      _setupBookChaptersCache[bookId] = await api('GET', `/api/books/${bookId}/chapters`);
    } catch (e) {
      _setupBookChaptersCache[bookId] = { chapters: [], available: false, reason: 'Failed to load chapters.' };
    }
    if (loading) loading.style.display = 'none';
  }
  _renderSetupBookChapters(_setupBookChaptersCache[bookId].chapters, _setupBookChaptersCache[bookId]);
}

function _renderSetupBookChapters(chapters, data) {
  const chapterSel = document.getElementById('setup-book-chapter-select');
  const hint = document.getElementById('setup-book-hint');
  if (!chapterSel) return;
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  // Only chapters that already have a summary can seed a story (issue #865) —
  // an unsummarized chapter has no material for the AI to work from.
  const summarized = (chapters || []).filter(ch => ch.status === 'summarized');
  if (!chapters || !chapters.length) {
    chapterSel.innerHTML = '<option value="">—</option>';
    chapterSel.disabled = true;
    if (hint) {
      hint.style.display = 'block';
      hint.textContent = (data && data.reason) || 'Select a book first.';
    }
    return;
  }
  if (!summarized.length) {
    chapterSel.innerHTML = '<option value="">—</option>';
    chapterSel.disabled = true;
    if (hint) {
      hint.style.display = 'block';
      hint.textContent = '这本书还没有生成过章节摘要';
    }
    return;
  }
  if (hint) hint.style.display = 'none';
  chapterSel.disabled = false;
  chapterSel.innerHTML = summarized.map(ch =>
    `<option value="${ch.id}">第${ch.number}章 · ${esc(ch.title_zh || ch.ref_label || '')}</option>`).join('');
}

function _getSelectedBookChapterId() {
  const chapterSel = document.getElementById('setup-book-chapter-select');
  const v = chapterSel && chapterSel.value;
  return v ? parseInt(v, 10) : null;
}

// Podcast/video/article item picker (issue #482, multi-select since #752).
// Reads from _setupSelectedEpisodes (not the DOM) so a selection made under
// a different Source type/feed — currently not rendered — still counts.
function _getSelectedEpisodeIds() {
  return Array.from(_setupSelectedEpisodes.keys());
}

function confirmStorySetup() {
  const topic       = document.getElementById('setup-topic').value.trim() || null;
  const maxHsk      = parseInt(document.getElementById('setup-hsk-slider').value, 10);
  const model       = document.getElementById('setup-model').value;
  const grammarFocus = document.getElementById('setup-grammar').value.trim() || null;
  const grammarPct  = parseInt(document.getElementById('setup-grammar-pct').value, 10) || 75;
  const mode        = document.getElementById('setup-mode').value;
  // Remember this mode's model choice (issue #561). The server placeholder is
  // a real selection for paste/contextsummary — "let the server decide" is what
  // those modes default to and the user may deliberately keep it (#910/#1011);
  // for every other mode it is not a model and must not be persisted.
  if (model && (model !== SERVER_MODEL_VALUE || SERVER_MODEL_MODES.includes(mode)))
    localStorage.setItem('setupModel:' + mode, model);
  // Remember the mode itself, per language (#972) — see openStorySetup().
  localStorage.setItem('setupMode:' + setupLang(), mode);
  const chapterIds  = mode === 'kahneman' ? _getSelectedChapterIds() : null;
  const articles    = mode === 'paste' ? _collectPastedContents() : null;
  // Kontextsummary shares knowledge mode's item picker (issue #1011).
  const episodeIds  = KNOWLEDGE_SOURCE_MODES.includes(mode) ? _getSelectedEpisodeIds() : null;
  // #929: snapshot the picked items (title + kind) for the loading screen's
  // source buttons. A snapshot, not a live read of _setupSelectedEpisodes —
  // that Map is cleared the next time the setup modal opens.
  // Kahneman (#980) gets the same buttons for its chapters. Seeded here so they
  // appear the instant the loading screen does; the server then reports the
  // chapters it actually used through /api/story-progress (authoritative — an
  // empty selection means "random 5", which only the server knows).
  _storyLoadingSources = KNOWLEDGE_SOURCE_MODES.includes(mode)
    ? Array.from(_setupSelectedEpisodes.values()).map(e => ({ id: e.id, title: e.title, kind: e.kind }))
    : mode === 'kahneman'
    ? (chapterIds || []).map(n => {
        const ch = _kahnemanChapters?.find(c => c.number === n);
        return { id: n, kind: 'kahneman', title: `第${n}章 ${ch?.title_zh || ''}`.trim() };
      })
    : [];
  const bookChapterId = mode === 'book' ? _getSelectedBookChapterId() : null;
  // Kontextsummary never sends articles: the server builds them from the picked
  // knowledge items (#1011). Paste needs at least one non-empty text (#396).
  if (mode === 'paste' && !articles.length) {
    showError('Paste mode needs at least one text — paste some content first.');
    return;
  }
  // Knowledge and Kontextsummary require at least one source item (#482/#654/#752/#1011).
  if (KNOWLEDGE_SOURCE_MODES.includes(mode) && !episodeIds.length) {
    showError('This mode needs a source — select at least one knowledge item first.');
    return;
  }
  // Book mode requires picking a chapter (issue #865).
  if (mode === 'book' && !bookChapterId) {
    showError('Book mode needs a chapter — select one first.');
    return;
  }
  // Words per AI call (issue #563 podcast-only, #574 all modes): empty input
  // = the mode's default chunking; persisted per mode like setupModel.
  {
    const batchInp = document.getElementById('setup-batch-size');
    const v = parseInt((batchInp && batchInp.value) || '', 10);
    if (v > 0) localStorage.setItem('setupBatch:' + mode, v);
    else localStorage.removeItem('setupBatch:' + mode);
  }
  _currentStoryMode = mode;
  _closeSetupModal();
  // Words-only mode (issue #547): no story to generate — start a quick words-only
  // session directly, reusing the same path as the ⚡ speed button. Handled before
  // the story-generation dispatch below so every entry point behaves consistently.
  if (mode === 'vocab') {
    quickMode = true;
    story = null;
    if (_setupIsDeckListRegen) {
      if (_deckListRegenCategory === 'unified') {
        // Parent-row ↺ targets a whole deck → words-only mixed (all-category) review.
        rootDeckId = _deckListRegenId;
        deckId = _deckListRegenId;
        _doStartReviewMixed(null, 2, null, null, 50, 'story', true);
      } else {
        // Mode-pill ↺ (#857) targets one category → words-only single-category
        // review, same path startReview() itself uses for its ⚡ quick mode.
        rootDeckId = null;
        deckId = _deckListRegenId;
        category = _deckListRegenCategory;
        _doStartReview(null, 2);
      }
    } else if (_setupIsMixed || rootDeckId) {
      _doStartReviewMixed(null, 2, null, null, 50, 'story', true);
    } else {
      _doStartReview(null, 2);
    }
    return;
  }
  if (_setupIsDeckListRegen) {
    _doRegenStoryForDeckList(_deckListRegenId, topic, maxHsk, model, grammarFocus, grammarPct, mode, chapterIds, articles, episodeIds, bookChapterId);
  } else if (_setupIsRegen) {
    _doRegenerateStory(topic, maxHsk, model, grammarFocus, grammarPct, mode, chapterIds, articles, episodeIds, bookChapterId);
  } else if (_setupIsUnfinished) {
    _doStartReviewUnfinished(topic, maxHsk, model, grammarFocus, grammarPct, mode, chapterIds, articles, episodeIds, bookChapterId);
  } else if (_setupIsMixed) {
    // noStory=false: confirmStorySetup always wants a fresh generation, unlike
    // the `?scope=` quick paths above that call this with noStory=true directly.
    // (Pre-#482 this call under-supplied args, so chapterIds silently landed in
    // the noStory slot — harmless for story/paste/news since chapterIds was null
    // there, but it would have broken kahneman+podcast in mixed review.)
    _doStartReviewMixed(topic, maxHsk, model, grammarFocus, grammarPct, mode, false, chapterIds, articles, episodeIds, bookChapterId);
  } else {
    _doStartReview(topic, maxHsk, model, grammarFocus, grammarPct, mode, chapterIds, articles, episodeIds, bookChapterId);
  }
}

function cancelStorySetup() {
  _closeSetupModal();
  if (!_setupIsRegen && !_setupIsDeckListRegen) showView('decks');
}

function _closeSetupModal() {
  document.getElementById('setup-modal-overlay').style.display = 'none';
  document.getElementById('setup-modal').style.display        = 'none';
  document.getElementById('price-table-popup').style.display  = 'none';
}

// ── Story modal ───────────────────────────────────────────────────────────────
// Article section-header HTML shown once per article in the full-story modal
// (replaces the old per-sentence source line, issue #454). Same title/label
// logic as _newsSourceHtml, minus the trailing arrow.
function _articleHeaderHtml(s, escAttr) {
  const title = _newsflowLang === 'zh'
    ? (s.concept_zh || s.source_title || '')
    : (s.source_title || '');
  const label = [title, s.source_name || ''].filter(Boolean).join(' · ');
  if (!label) return '';
  const url = s.source_url || '';
  return `<div class="story-article-header"${url ? ` data-url="${escAttr(url)}"` : ''}>📰 ${escAttr(label)}</div>`;
}

function openStoryModal() {
  if (!story?.sentences?.length) return;
  const escAttr = s => String(s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const currentPos = sentence?.position ?? -1;
  const parts = [];
  let prevUrl = null;
  for (const s of story.sentences) {
    const isCurrent = s.position === currentPos;
    const url = s.source_url || '';
    // Kontextsummary: source section header, inserted once whenever a new source
    // article starts (issue #454; replaces the old per-sentence source line).
    if (url && url !== prevUrl) {
      parts.push(_articleHeaderHtml(s, escAttr));
      prevUrl = url;
    }
    // Kontextsummary: context sentence(s) preceding this target — shown between the
    // target sentences (Chinese + German), clickable to the source (issue #452).
    const ctxZh = s.reasoning_zh || '';
    const ctxDe = s.context_de || '';
    if (ctxZh || ctxDe) {
      parts.push(`<div class="story-context${url ? ' clickable-sentence' : ''}"${url ? ` data-url="${escAttr(url)}"` : ''}>
        ${ctxZh ? `<div class="story-context-zh">${escAttr(ctxZh)}</div>` : ''}
        ${ctxDe ? `<div class="story-context-de">🇩🇪 ${escAttr(ctxDe)}</div>` : ''}
      </div>`);
    }
    const highlighted = s.sentence_zh.replace(
      s.word_zh,
      `<span class="story-target">${s.word_zh}</span>`
    );
    const conceptBadge = s.concept_zh
      ? `<div class="story-concept-badge" title="${s.concept_en || ''}">
           <span class="concept-name">${s.concept_zh}</span>
         </div>`
      : '';
    parts.push(`<div class="story-sentence${isCurrent ? ' story-sentence-current' : ''}" data-idx="${s.position}">
      <span class="story-num">${s.position + 1}</span>
      <div class="story-content">
        <div class="story-zh${url ? ' clickable-sentence' : ''}"${url ? ` data-url="${escAttr(url)}"` : ''}>${highlighted}</div>
        ${conceptBadge}
        ${s.sentence_fr ? `<div class="story-fr">🇫🇷 ${s.sentence_fr}</div>` : ''}
        ${s.sentence_de ? `<div class="story-de">🇩🇪 ${s.sentence_de}</div>` : ''}
      </div>
      <button class="story-play-btn" onclick="storyJumpTo(${s.position})" title="Play">▶</button>
    </div>`);
  }
  document.getElementById('story-modal-body').innerHTML = parts.join('');
  // Attach click-to-open handlers for elements carrying a source URL (avoids
  // inline-onclick URL-injection issues).
  document.querySelectorAll('#story-modal-body [data-url]').forEach(el => {
    el.onclick = (ev) => { ev.stopPropagation(); window.open(el.dataset.url, '_blank', 'noopener'); };
  });
  document.getElementById('story-modal-title').textContent = story.topic || 'Full story';
  if (_storyPlaying && _currentPlayIdx >= 0) updateStoryHighlight(_currentPlayIdx);
  document.getElementById('story-modal-overlay').style.display = 'block';
  document.getElementById('story-modal').style.display = 'flex';
  // Jump straight to the current sentence (issue #454).
  document.querySelector('#story-modal-body .story-sentence-current')?.scrollIntoView({ block: 'center' });
}

let _storyPlaying = false;
let _currentPlayIdx = -1;
let _storyStoppedAt = -1;

// ── iOS-friendly shared audio element (#606) ─────────────────────────────────
// iOS Safari only lets you play() an Audio element whose FIRST play() happened
// inside a user gesture. So we keep ONE element, unlock it on the first
// pointerdown with a silent clip, then reuse it for every playback. That way
// autoplay (listening front, reveal) and the onended-driven full-story chain —
// none of which run inside a gesture — are still allowed to play on iPhone.
const _SILENT_WAV = 'data:audio/wav;base64,UklGRnQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==';
let _sharedAudio = null;
let _playSeq = 0;   // bumped on every (re)start so stale onended/onerror handlers no-op

function _getAudioEl() {
  if (!_sharedAudio) { _sharedAudio = new Audio(); _sharedAudio.preload = 'auto'; }
  return _sharedAudio;
}

// Stop whatever the shared element is doing and invalidate pending chain callbacks.
function _stopSharedPlayback() {
  _playSeq++;
  const a = _sharedAudio;
  if (a) { a.onended = null; a.onerror = null; try { a.pause(); } catch (_) {} }
}

// Unlock the shared element on the first pointerdown by play()ing a silent clip
// inside the gesture; retries on each pointerdown until one succeeds.
function _unlockAudio() {
  const a = _getAudioEl();
  try {
    a.src = _SILENT_WAV;
    const p = a.play();
    const done = () => {
      try { a.pause(); a.currentTime = 0; } catch (_) {}
      document.removeEventListener('pointerdown', _unlockAudio, true);
    };
    if (p && p.then) p.then(done).catch(() => {});
    else done();
  } catch (_) {}
}
document.addEventListener('pointerdown', _unlockAudio, true);

function updateStoryHighlight(idx) {
  document.querySelectorAll('#story-modal-body .story-sentence').forEach(el => {
    const isPlaying = parseInt(el.dataset.idx) === idx;
    el.classList.toggle('story-sentence-playing', isPlaying);
    const playBtn = el.querySelector('.story-play-btn');
    if (playBtn) playBtn.textContent = isPlaying ? '⏸' : '▶';
    if (isPlaying) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  });
}

function _storyAudioUrl(idx) {
  return `/api/tts-file?text=${encodeURIComponent(story.sentences[idx].sentence_zh)}&lang=${currentCardLang()}`;
}

function _playStoryAtIdx(idx) {
  if (!_storyPlaying || idx < 0 || idx >= story.sentences.length) {
    _storyPlaying = false;
    _currentPlayIdx = -1;
    updateStoryHighlight(-1);
    const btn = document.getElementById('story-play-all-btn');
    if (btn) btn.textContent = '▶ Play full story';
    return;
  }

  _currentPlayIdx = idx;
  updateStoryHighlight(idx);

  // Reuse the unlocked shared element so the onended-driven chain keeps playing
  // on iOS (a fresh Audio() would be rejected outside the user gesture). seq
  // guards against stale handlers firing after a jump/stop.
  const seq = ++_playSeq;
  const a = _getAudioEl();
  a.onended = () => { if (seq === _playSeq) _playStoryAtIdx(idx + 1); };
  a.onerror = () => { if (seq === _playSeq) _playStoryAtIdx(idx + 1); };
  a.src = _storyAudioUrl(idx);
  a.play().catch(() => { if (seq === _playSeq) _playStoryAtIdx(idx + 1); });
}

async function _startPlayback(startIdx) {
  if (!story?.sentences?.length) return;
  _storyPlaying = true;
  const btn = document.getElementById('story-play-all-btn');

  btn.textContent = '⏳ Loading audio…';
  const storyDeckId = rootDeckId || deckId;
  try {
    await api('POST', `/api/preload-session/${storyDeckId}/${category}${_langQP('?')}`);
  } catch (_) {}

  if (!_storyPlaying) return;

  _storyStoppedAt = -1;
  if (btn) btn.textContent = '■ Stop';
  _playStoryAtIdx(startIdx);
}

async function toggleFullStory() {
  if (_storyPlaying) { stopFullStory(); return; }
  const startIdx = _storyStoppedAt >= 0 ? _storyStoppedAt : 0;
  await _startPlayback(startIdx);
}

function storyJumpTo(idx) {
  _stopSharedPlayback();
  if (!_storyPlaying) {
    _storyPlaying = true;
    const btn = document.getElementById('story-play-all-btn');
    if (btn) btn.textContent = '■ Stop';
  }
  _playStoryAtIdx(idx);
}

function storySkipNext() {
  if (!_storyPlaying || _currentPlayIdx < 0) return;
  const next = _currentPlayIdx + 1;
  if (next >= story.sentences.length) return;
  storyJumpTo(next);
}

function storySkipPrev() {
  if (!_storyPlaying || _currentPlayIdx < 0) return;
  storyJumpTo(Math.max(0, _currentPlayIdx - 1));
}

function storyRepeat() {
  if (_currentPlayIdx < 0) return;
  storyJumpTo(_currentPlayIdx);
}

function stopFullStory() {
  if (!_storyPlaying) return;
  _storyStoppedAt = _currentPlayIdx;
  _storyPlaying = false;
  _currentPlayIdx = -1;
  _stopSharedPlayback();
  updateStoryHighlight(-1);
  const btn = document.getElementById('story-play-all-btn');
  if (btn) btn.textContent = '▶ Continue';
}

function closeStoryModal() {
  stopFullStory();
  document.getElementById('story-modal-overlay').style.display = 'none';
  document.getElementById('story-modal').style.display = 'none';
}

// ── Edit card modal ───────────────────────────────────────────────────────────
let _editWordId   = null;   // word ID being edited
let _editFromWord = false;  // true when opened from word-detail view
let _editHasEtymology = false;  // #906: only then does saving touch entries.etymology

function _openEditModal(wordObj) {
  _editWordId = wordObj.word_id || wordObj.id;
  document.getElementById('edit-word-zh').value       = wordObj.word_zh       || '';
  document.getElementById('edit-pinyin').value        = wordObj.pinyin        || '';
  document.getElementById('edit-definition').value    = wordObj.definition    || '';
  document.getElementById('edit-pos').value           = wordObj.pos           || '';
  document.getElementById('edit-traditional').value   = wordObj.traditional   || '';
  document.getElementById('edit-definition-zh').value = wordObj.definition_zh || '';
  document.getElementById('edit-definition-de').value = wordObj.definition_de || '';
  document.getElementById('edit-definition-fr').value = wordObj.definition_fr || '';
  document.getElementById('edit-notes').value         = wordObj.notes         || '';
  // Etymology is a Romance-only column (#906) — the field stays hidden for
  // Chinese entries so nobody types prose into a column nothing renders.
  //
  // The review card row (database/cards.py) does not select entries.etymology,
  // so during review the text comes from the wordDetails fetch. If neither
  // source carries the column, the field stays hidden and saveEditCard() omits
  // it entirely — saving an empty textarea would silently wipe the entry.
  const _etymSource = ('etymology' in wordObj) ? wordObj
    : (wordDetails?.id === (wordObj.word_id || wordObj.id) ? wordDetails : null);
  _editHasEtymology = !!_etymSource && _entryLang(wordObj) !== 'zh';
  document.getElementById('edit-etymology').value = _etymSource?.etymology || '';
  document.getElementById('edit-etymology-label').style.display = _editHasEtymology ? '' : 'none';
  // Show card action menu only when opened during active review
  const menuWrap = document.getElementById('edit-card-menu-wrap');
  menuWrap.style.display = _editFromWord ? 'none' : 'inline-block';
  if (!_editFromWord && card) {
    const isSuspended = card.state === 'suspended';
    document.getElementById('edit-suspend-btn').textContent = isSuspended ? 'Unsuspend' : 'Suspend';
  }
  document.getElementById('edit-card-menu').style.display = 'none';
  document.getElementById('edit-modal-overlay').style.display = 'block';
  document.getElementById('edit-modal').style.display         = 'flex';
}

function openEditCard() {
  _editFromWord = false;
  _openEditModal(card);
}

function openEditCardFromDetail(wordId) {
  closeAllCardMenus();
  _editFromWord = true;
  api('GET', `/api/word/${wordId}`).then(w => _openEditModal(w)).catch(e => showError(e.message));
}

function closeEditCard() {
  document.getElementById('edit-modal-overlay').style.display = 'none';
  document.getElementById('edit-modal').style.display         = 'none';
  document.getElementById('edit-card-menu').style.display     = 'none';
}

function toggleEditCardMenu(e) {
  e.stopPropagation();
  const menu = document.getElementById('edit-card-menu');
  menu.style.display = menu.style.display === 'none' ? 'block' : 'none';
}

function toggleReviewCardMenu(e) {
  e.stopPropagation();
  const menu = document.getElementById('review-card-menu');
  menu.style.display = menu.style.display === 'none' ? 'block' : 'none';
}

async function reviewCardAction(action) {
  if (!card) return;
  document.getElementById('review-card-menu').style.display = 'none';
  const cardId = card.id;
  try {
    if (action === 'delete') {
      await api('DELETE', `/api/cards/${cardId}`);
    } else {
      await api('POST', `/api/cards/${cardId}/${action}`);
    }
    if (action === 'leech') showStateChangeAnim({ to: 'suspended' });
    let nextData;
    if (unfinishedMode) {
      nextData = await api('GET', `/api/today-unfinished?scope=${_unfinishedScope}${_langQP('&')}`);
    } else if (rootDeckId) {
      nextData = await api('GET', `/api/today-mixed/${rootDeckId}${_langQP('?')}`);
    } else {
      nextData = await api('GET', `/api/today/${deckId}/${category}${_langQP('?')}`);
    }
    if (!nextData.card) {
      rootDeckId = null;
      unfinishedMode = false;
      showView('done');
      return;
    }
    if (unfinishedMode || rootDeckId) category = nextData.card.category;
    loadCard(nextData.card, nextData.counts);
  } catch (e) {
    showError(`Action failed: ${e.message}`);
  }
}

document.addEventListener('click', () => {
  const menu = document.getElementById('edit-card-menu');
  if (menu) menu.style.display = 'none';
  const rmenu = document.getElementById('review-card-menu');
  if (rmenu) rmenu.style.display = 'none';
});

async function editModalCardAction(action) {
  if (!card) return;
  const cardId = card.id;
  closeEditCard();
  try {
    if (action === 'delete') {
      await api('DELETE', `/api/cards/${cardId}`);
    } else {
      await api('POST', `/api/cards/${cardId}/${action}`);
    }
    // Advance to next card
    let nextData;
    if (unfinishedMode) {
      nextData = await api('GET', `/api/today-unfinished?scope=${_unfinishedScope}${_langQP('&')}`);
    } else if (rootDeckId) {
      nextData = await api('GET', `/api/today-mixed/${rootDeckId}${_langQP('?')}`);
    } else {
      nextData = await api('GET', `/api/today/${deckId}/${category}${_langQP('?')}`);
    }
    if (!nextData.card) {
      rootDeckId = null;
      unfinishedMode = false;
      showView('done');
      return;
    }
    if (unfinishedMode || rootDeckId) category = nextData.card.category;
    loadCard(nextData.card, nextData.counts);
  } catch (e) {
    showError(`Action failed: ${e.message}`);
  }
}

async function saveEditCard() {
  const body = {
    word_zh:       document.getElementById('edit-word-zh').value.trim(),
    pinyin:        document.getElementById('edit-pinyin').value.trim(),
    definition:    document.getElementById('edit-definition').value.trim(),
    pos:           document.getElementById('edit-pos').value.trim(),
    traditional:   document.getElementById('edit-traditional').value.trim(),
    definition_zh: document.getElementById('edit-definition-zh').value.trim(),
    definition_de: document.getElementById('edit-definition-de').value.trim(),
    definition_fr: document.getElementById('edit-definition-fr').value.trim(),
    notes:         document.getElementById('edit-notes').value.trim(),
  };
  if (_editHasEtymology) {
    body.etymology = document.getElementById('edit-etymology').value.trim();
  }
  try {
    const updated = await api('PUT', `/api/word/${_editWordId}`, body);
    closeEditCard();
    if (_editFromWord) {
      await openWordDetail(_editWordId);
    } else {
      // Refresh review card in place
      Object.assign(card, {
        word_zh: updated.word_zh, pinyin: updated.pinyin,
        definition: updated.definition, pos: updated.pos,
        traditional: updated.traditional, definition_zh: updated.definition_zh,
        definition_de: updated.definition_de,
        definition_fr: updated.definition_fr,
        notes: updated.notes,
        etymology: updated.etymology,
      });
      document.getElementById('word-zh').textContent  = updated.word_zh || '';
      document.getElementById('word-pin').textContent = updated.pinyin  || '';
      document.getElementById('word-def').textContent = updated.definition ? `🇬🇧 ${updated.definition}` : '';
      const wordDefDeEl2 = document.getElementById('word-def-de');
      wordDefDeEl2.textContent = updated.definition_de ? `🇩🇪 ${updated.definition_de}` : '';
      wordDefDeEl2.style.display = updated.definition_de ? 'block' : 'none';
      const wordDefFrEl2 = document.getElementById('word-def-fr');
      wordDefFrEl2.textContent = updated.definition_fr ? `🇫🇷 ${updated.definition_fr}` : '';
      wordDefFrEl2.style.display = updated.definition_fr ? 'block' : 'none';
      const posEl = document.getElementById('word-pos');
      posEl.textContent   = updated.pos || '';
      posEl.style.display = updated.pos ? 'inline-block' : 'none';
      renderNotesSection();
      // updated is the full entry row, so the Etymology block (#906) picks the
      // edit up straight away instead of showing the pre-edit text.
      if (wordDetails?.id === updated.id) wordDetails = { ...wordDetails, ...updated };
      renderEtymologySection();
    }
  } catch (e) {
    showError('Save failed: ' + e.message);
  }
}

// ── AI Enrich (HSK badge click) ──────────────────────────────────────────────
async function enrichCard() {
  if (!card) return;
  const badge = document.getElementById('card-hsk-badge');
  badge.textContent = '…';
  badge.disabled = true;
  try {
    const updated = await api('POST', `/api/word/${card.word_id}/ai-enrich`);
    // Update in-memory card HSK level
    if (updated?.hsk_level) card.hsk_level = updated.hsk_level;
    badge.textContent = card.hsk_level ? `HSK ${card.hsk_level}` : 'HSK -';
    badge.classList.toggle('hsk-unknown', !card.hsk_level);
    // Refresh word detail if back is visible
    if (updated && document.getElementById('side-back').style.display !== 'none') {
      wordDetails = updated;
      renderVocabDetail();
      _callRenderWordAnalysis();
    }
  } catch (e) {
    badge.textContent = card.hsk_level ? `HSK ${card.hsk_level}` : 'HSK -';
    showError('AI enrich failed: ' + e.message);
  } finally {
    badge.disabled = false;
  }
}

// ── TTS ─────────────────────────────────────────────────────────────────────
let _listenCount = 0;

function _updateListenCounters() {
  const label = _listenCount > 0 ? `×${_listenCount}` : '';
  const show  = _listenCount > 0;
  ['listen-counter-meta'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = label;
    el.style.display = show ? 'inline-block' : 'none';
  });
}

// TTS URL for `text` in `lang`.
function _ttsUrl(text, lang) {
  if (!text) return null;
  return `/api/tts-file?text=${encodeURIComponent(text)}&lang=${lang}`;
}

// TTS URL for the current card's sentence (falls back to the bare word).
function _sentenceAudioUrl() {
  return _ttsUrl(sentence?.sentence_zh || card?.word_zh, currentCardLang());
}

// Browser-side TTS prefetch (#554, #557, #606). /api/tts-file is immutable-cached,
// so fetch()ing a URL downloads the mp3 into the browser HTTP cache; a later
// play() of the same URL then starts with no network round trip. We use fetch()
// (not Audio.load()) because iOS Safari ignores `preload` and won't buffer media
// outside a user gesture, which left mobile playback slow. _warmed just dedups
// URLs already fetched (LRU-capped).
const _warmed = new Set();
const _WARM_MAX = 200;
function _warmAudio(url) {
  if (!url || _warmed.has(url)) return;
  _warmed.add(url);
  if (_warmed.size > _WARM_MAX) _warmed.delete(_warmed.values().next().value);
  fetch(url).catch(() => {});
}

// Warm the current card's sentence audio.
function _prefetchSentenceAudio() {
  _warmAudio(_sentenceAudioUrl());
}

// Warm every sentence in the loaded story so later listening fronts / flips play
// instantly — prefetched in the background while the user reviews earlier cards.
function _prefetchStoryAudio(sentences) {
  if (!Array.isArray(sentences)) return;
  const lang = currentCardLang();
  for (const s of sentences) _warmAudio(_ttsUrl(s?.sentence_zh, lang));
}

// Clicking the front-side mascot replays the sentence (listening only).
function mascotClick() {
  if (category !== 'listening') return;
  playSentence();
}

function playSentence() {
  const url = _sentenceAudioUrl();
  if (!url) return;
  _listenCount++;
  _updateListenCounters();
  _stopSharedPlayback();
  // Play through the shared, gesture-unlocked element so autoplay (listening
  // front, reveal) works on iOS too — a fresh Audio() created outside a user
  // gesture is silently rejected on iPhone (#590 → #606). The URL is
  // immutable-cached, so setting .src replays instantly once the mp3 is cached.
  const a = _getAudioEl();
  a.src = url;
  a.play().catch(() => {});
}

// ── Regenerate story ─────────────────────────────────────────────────────────
async function regenerateStory() {
  const count = story?.sentences?.length ?? 0;
  let learning = 0;
  try {
    if (deckId && category) {
      const todayCounts = await api('GET', `/api/today/${deckId}/${category}${_langQP('?')}`);
      learning = todayCounts?.counts?.learning_future || 0;
    }
  } catch (_) {}
  try {
    await openStorySetup(count, { learningCount: learning });
  } catch (_) {
    showView('review');
  }
}

async function _doRegenerateStory(topic, maxHsk, model, grammarFocus, grammarPct, mode = 'story', chapterIds = null, articles = null, episodeIds = null, bookChapterId = null) {
  setLoading('Regenerating story…', true);
  setLoadingStep(10, null, 'Sending request to AI…');
  _startFakeProgress(10, 55, 45000);
  const storyDeckId = rootDeckId || deckId;
  const storyCategory = rootDeckId ? 'unified' : category;
  // Cancel ✕ / Continue in background on this screen too (#868): a regenerate
  // takes as long as a first generation and used to be an unbreakable wait.
  // isRegen routes both buttons back into the review session instead of the
  // deck list, and tells _continueStoryInBackground not to hand this run to the
  // `background=true` poller — this POST is the only thing that will deliver it.
  _bgLeaveRequested = false;
  _regenBgRequested = false;
  _regenCancelRequested = false;
  _bgActiveResume = { key: `${storyDeckId}/${storyCategory}`, storyDeckId, storyCategory, isRegen: true };
  _showLoadingBgButton();
  try {
    _startStoryProgressPoll(storyDeckId, storyCategory);
    let storyData;
    try {
      storyData = await api('POST', `/api/story/${storyDeckId}/${storyCategory}/regenerate` + _storyParams(topic, maxHsk, model, grammarFocus, grammarPct, mode, chapterIds, episodeIds, bookChapterId), { articles });
    } catch (e) {
      _stopFakeProgress(); _stopStoryProgressPoll();
      _clearRegenBgState();
      if (_regenBgRequested) { _hideRegenBgBanner(); showError('Regenerate failed: ' + e.message); return; }
      _showLoadingError('AI request failed', e.message);
      await new Promise(r => setTimeout(r, 2500));
      showError('Regenerate failed: ' + e.message);
      showView('review');
      return;
    }
    _stopFakeProgress(); _stopStoryProgressPoll();

    // Cancelled (#828/#868): nothing was written, so the old story stands, and
    // _cancelStoryGeneration already returned the user to their card. The local
    // flag covers the race where the server finished just before the flag
    // landed — the user asked to abandon this run either way.
    if ((storyData && storyData.cancelled) || _regenCancelRequested) { _clearRegenBgState(); return; }

    // Left in the background: the user is reviewing on the old story. Load the
    // new one into memory but do not touch what is on screen — turn the banner
    // into the offer to switch over.
    if (_regenBgRequested) {
      _clearRegenBgState();
      await _finishRegenInBackground(storyData, storyDeckId, storyCategory,
                                     topic, maxHsk, grammarFocus, grammarPct, mode);
      return;
    }

    _hideLoadingBgButton();
    _clearRegenBgState();
    setLoadingStep(65, null, 'Story received, processing…');
    story = await _resolveStory(storyData, storyDeckId, storyCategory, topic, maxHsk, grammarFocus, grammarPct, mode);
    sentence = story?.sentences?.find(s => s.word_ids?.includes(card.word_id)) || null;
    _updateStoryInfoRow();

    const sentenceCount = story?.sentences?.length ?? 0;
    setLoadingStep(70, 'Story ready!',
      sentenceCount > 0 ? `Generating audio — 0 / ${sentenceCount} sentences…` : 'Loading audio…');
    await _preloadWithProgress(storyDeckId, storyCategory, (done, total) => {
      const pct = 70 + Math.round((done / total) * 28);
      setLoadingStep(pct, null, `Generating audio — ${done} / ${total} sentences…`);
    });
    _showLoadingSuccess('Story regenerated!');
    await new Promise(r => setTimeout(r, 500));
    showView('review');
    showFront();
  } catch (e) {
    _stopFakeProgress(); _stopStoryProgressPoll();
    const wasBg = _regenBgRequested;
    _clearRegenBgState();
    if (wasBg) { _hideRegenBgBanner(); showError('Regenerate failed: ' + e.message); return; }
    _showLoadingError('Regenerate failed', e.message);
    await new Promise(r => setTimeout(r, 2500));
    showError('Regenerate failed: ' + e.message);
    showView('review');
  }
}

// Every exit from _doRegenerateStory runs this: a leaked _bgActiveResume makes
// the next Cancel click cancel the wrong run, and a stuck _regenBgRequested
// sends the following regenerate straight into the background path.
function _clearRegenBgState() {
  _bgActiveResume = null;
  _bgLeaveRequested = false;
  _regenBgRequested = false;
  _regenCancelRequested = false;
  _hideLoadingBgButton();
}

// The story finished while the user kept reviewing. Warm the audio cache, then
// offer the switch — swapping the sentence under a card being answered would
// change the question mid-answer.
async function _finishRegenInBackground(storyData, storyDeckId, storyCategory,
                                        topic, maxHsk, grammarFocus, grammarPct, mode) {
  const newStory = await _resolveStory(storyData, storyDeckId, storyCategory,
                                       topic, maxHsk, grammarFocus, grammarPct, mode);
  _preloadWithProgress(storyDeckId, storyCategory, () => {}).catch(() => {});
  const banner = document.getElementById('bg-story-banner');
  if (!banner) return;
  banner.classList.remove('bg-banner-progress');
  banner.textContent = '📖 Story regenerated — click to use it';
  banner.style.display = 'block';
  banner.onclick = () => {
    _hideRegenBgBanner();
    story = newStory;
    sentence = story?.sentences?.find(s => s.word_ids?.includes(card?.word_id)) || null;
    _updateStoryInfoRow();
    showView('review');
    showFront();
  };
}

async function regenerateStoryFromList(deckId, category = 'unified') {
  _deckListRegenId = deckId;
  _deckListRegenCategory = category;
  _setupIsDeckListRegen = true;
  _setupIsRegen = false;
  _setupIsMixed = false;
  _setupIsUnfinished = false;
  let sentenceCount = 0;
  try {
    const data = await api('GET', `/api/story/${deckId}/${category}/count${_langQP('?')}`);
    sentenceCount = data?.count ?? 0;
  } catch (_) {}
  const _countLabel = document.getElementById('setup-count-label');
  _countLabel.textContent =
    `This story will have ${sentenceCount} sentence${sentenceCount !== 1 ? 's' : ''}.`;
  // Remember this text so switching away from Words-only mode restores it (#547).
  _countLabel.dataset.storyText = _countLabel.textContent;
  const warn = document.getElementById('setup-learning-warning');
  warn.style.display = 'none';
  const tokenWarn = document.getElementById('setup-token-warning');
  if (tokenWarn) tokenWarn.style.display = 'none';
  document.getElementById('setup-topic').value = '';
  document.getElementById('setup-grammar').value = '';
  document.getElementById('setup-grammar-pct').value = 50;
  document.getElementById('setup-hsk-slider').value = 3;
  // Knowledge-item multi-select (issue #752) — reset on every fresh open, same
  // reasoning as openStorySetup above.
  _setupSelectedEpisodes.clear();
  _renderSetupSelectedEpisodes();
  updateHskLabel();
  // Sync the per-mode control visibility to the current dropdown value (e.g. hide
  // story-only controls when Words-only is selected, #547).
  updateSetupMode();
  document.getElementById('setup-modal-overlay').style.display = 'block';
  document.getElementById('setup-modal').style.display = 'flex';
  document.getElementById('setup-topic').focus();
}

// Regenerate a deck's story in the BACKGROUND: instead of a blocking full-screen
// loader, show a small persistent banner and let the user keep reviewing. When
// the new story is ready the banner turns into a clickable "open for review".
async function _doRegenStoryForDeckList(deckId, topic, maxHsk, model, grammarFocus, grammarPct, mode = 'story', chapterIds = null, articles = null, episodeIds = null, bookChapterId = null) {
  // Read (not a parameter): confirmStorySetup calls this positionally the same
  // way it's called for every other setup-modal path, and regenerateStoryFromList
  // already stashed the category here when the modal was opened (#857).
  const category = _deckListRegenCategory || 'unified';
  const deck = flatten(_cachedDecks || []).find(d => d.id === deckId);
  const deckName = deck ? deck.name : 'deck';
  const noStory = !!(deck && deck.no_story);
  const label = category === 'unified' ? deckName : `${deckName} (${category})`;

  const banner = document.getElementById('bg-story-banner');
  if (banner) {
    banner.classList.add('bg-banner-progress');
    banner.textContent = `⏳ Regenerating story for ${label} in the background — keep reviewing…`;
    banner.onclick = null;
    banner.style.display = 'block';
  }

  try {
    await api('POST', `/api/story/${deckId}/${category}/regenerate` + _storyParams(topic, maxHsk, model, grammarFocus, grammarPct, mode, chapterIds, episodeIds, bookChapterId), { articles });
    // Warm the audio cache in the background so review starts fast; don't block
    // the "ready" banner on it.
    _preloadWithProgress(deckId, category, () => {}).catch(() => {});
    if (banner) {
      banner.classList.remove('bg-banner-progress');
      banner.textContent = `📖 Story ready — ${label} · click to review`;
      banner.style.display = 'block';
      banner.onclick = () => {
        banner.style.display = 'none';
        banner.onclick = null;
        if (category === 'unified') {
          startReviewMixed(deckId, deckName, noStory);
        } else {
          startReview(deckId, category, deckName, noStory);
        }
      };
    }
  } catch (e) {
    if (banner) {
      banner.classList.remove('bg-banner-progress');
      banner.onclick = null;
      banner.style.display = 'none';
    }
    showError('Regenerate failed: ' + e.message);
  }
}

// ── Navigation history (#1009) ───────────────────────────────────────────────
// The header's ← used to be a hard-wired "back to the deck list", and the
// browser's own back button left the site entirely (the app never wrote a
// single history entry). Both now walk the same in-app stack: ← goes to the
// LAST screen, the 邁 logo goes home.
//
// History entries carry only an index (`{navIdx}`); the restore closures live
// in `_navEntries`, parallel to the browser's stack. They cannot be
// serialised, so a page reload loses them — an entry we can't restore falls
// back to the deck list rather than pretending to navigate.
//
// The one rule that makes both directions work: **before leaving the current
// screen** (a push OR a popstate), snapshot where we are into
// `_navEntries[_navIdx]`. Backwards then finds the previous screen and
// forwards finds the one we just left.
let _navEntries = [];      // index -> {key, restore}
let _navIdx = 0;
let _navSuppress = false;  // true while a restore closure replays a screen

// A snapshot of the screen currently on display. Returns null for screens
// that are not a place you can come back to (loading, done).
function _navHere() {
  const view = _currentView;
  if (view === 'knowledge') {
    if (_knowledgeDetailId != null) {
      const id = _knowledgeDetailId;
      return { key: `knowledge:item:${id}`, restore: () => openKnowledgeItem(id) };
    }
    if (_knowledgeScreen === 'feed') {
      const f = _podcastCurrentFeedId;
      return { key: `knowledge:feed:${f}`, restore: () => openPodcastFeed(f) };
    }
    // The subs screen's two tabs are one location: flipping between them must
    // not pile up history entries.
    if (_knowledgeScreen === 'subs') {
      const t = _subsTab;
      return { key: 'knowledge:subs', restore: () => openKnowledgeSubs(t) };
    }
    if (_knowledgeScreen === 'tags')    return { key: 'knowledge:tags', restore: () => openKnowledgeTags() };
    if (_knowledgeScreen === 'mailbox') return { key: 'knowledge:mailbox', restore: () => openMailbox() };
    // No argument: an argument would reset the filter bar (see
    // closeKnowledgeDetail).
    return { key: 'knowledge:list', restore: () => openKnowledge() };
  }
  if (view === 'books') {
    const id = _bookState.bookId, page = _bookState.pageNo, lang = _bookState.lang;
    if (id) return { key: `books:${id}`, restore: () => openBook(id, page, lang) };
    return { key: 'books', restore: () => openBooks() };
  }
  // browse and review are restored by simply showing the view again: their
  // state is still in memory, so the scroll position, filters and the current
  // card survive — reloading them would throw all of that away. Both guard on
  // that state, because going home clears it and the browser's Forward button
  // can still land here afterwards.
  if (view === 'browse') {
    return { key: 'browse', restore: () => browseWords.length ? showView('browse') : openBrowse() };
  }
  if (view === 'review') {
    return { key: 'review', restore: () => card ? showView('review') : _goHomeNow() };
  }
  // The detail pages re-fetch instead: they are a single rendered page, so
  // reloading costs one request and can never show a stale or empty shell.
  if (view === 'word-detail') {
    const id = _currentWordId;
    return { key: `word:${id}`, restore: () => openWordDetail(id) };
  }
  if (view === 'hanzi-detail') {
    const id = _currentHanziId;
    return { key: `hanzi:${id}`, restore: () => openHanziDetail(id) };
  }
  if (view === 'archive') {
    const id = _archiveState.storyId;
    if (id != null) return { key: `archive:story:${id}`, restore: () => openArchiveStory(id) };
    const t = _archiveState.tab;
    return { key: `archive:${t}`, restore: () => openArchive(t) };
  }
  if (view === 'stats')         return { key: 'stats', restore: () => openStats() };
  if (view === 'settings')      return { key: 'settings', restore: () => openSettings() };
  if (view === 'decks')         return { key: 'decks', restore: () => _goHomeNow() };
  return null;
}

// Called at the top of every screen-opening function, BEFORE it changes any
// state: it records the screen being left, not the one being opened. Pass the
// key of the screen being opened so that re-entering the screen you are
// already on (a tab flip, a re-render after an edit) doesn't pile up history
// entries that all lead back to the same place.
function navPush(destKey) {
  if (_navSuppress) return;
  const here = _navHere();
  if (!here) return;
  _navEntries[_navIdx] = here;
  if (destKey && here.key === destKey) return;
  _navIdx += 1;
  _navEntries.length = _navIdx;   // a new branch drops any forward entries
  history.pushState({ navIdx: _navIdx }, '');
  _updateBackBtn();
}

function _navReplay(entry) {
  _navSuppress = true;
  try { entry.restore(); }
  // The restore closures push nothing before their first await, so clearing
  // the flag on the next macrotask is enough to cover their synchronous part.
  finally { setTimeout(() => { _navSuppress = false; }, 0); }
}

function _updateBackBtn() {
  const b = document.getElementById('back-btn');
  if (b) b.style.display = _navIdx > 0 ? 'block' : 'none';
}

window.addEventListener('popstate', e => {
  const target = (e.state && typeof e.state.navIdx === 'number') ? e.state.navIdx : 0;
  const here = _navHere();
  if (here) _navEntries[_navIdx] = here;   // so Forward can come back here
  _navIdx = target;
  const entry = _navEntries[target];
  _updateBackBtn();
  // No entry means the page was reloaded since that entry was written — the
  // closure is gone. Home is the honest answer; silently staying put would
  // look like a dead back button.
  if (entry) _navReplay(entry);
  else _goHomeNow();
});

// ── Back / home ──────────────────────────────────────────────────────────────
// ← walks the history so that the browser's own back button (and iOS's
// swipe-back) stay in step with it.
function goBack() {
  if (_navIdx > 0) { history.back(); return; }
  _goHomeNow();
}

// The 邁 logo: all the way home, unwinding the browser history with it so
// that "back" afterwards doesn't walk into screens we already left.
function goHome() {
  if (_navIdx > 0) { history.go(-_navIdx); return; }
  _goHomeNow();
}

function _goHomeNow() {
  card = null; story = null; sentence = null; wordDetails = null; userInput = '';
  rootDeckId = null; unfinishedMode = false; _sessionReviewedCount = 0;
  browseWords = []; browseAll = []; _browseSelected.clear();
  _knowledgeDetailId = null;
  _bookState.bookId = null;
  loadDecks();
}

// ── Daily random words popup ─────────────────────────────────────────────────
// Opens a small separate browser window showing 10 random words to use today.
// Reusing the window name means a second click reloads it → a fresh set of words.
function openRandomWords() {
  window.open('/static/random-words.html', 'randomwords',
              'width=460,height=680,menubar=no,toolbar=no,location=no');
}

// ── Import modal ─────────────────────────────────────────────────────────────

let importResolutions = {};    // {word_zh: "keep"|"update"|"custom"}
let _previewEntries = [];      // full entry list from last preview (with raw_yaml)
let _cardConfigs = {};         // {word_zh: {include, deck_path, suspended:{reading,listening,creating}}}
let _importDeckOptions = [];   // flat list of deck paths for per-card dropdowns
let _conflictData = [];        // full conflict list from last preview
let _conflictEdits = {};       // {word_zh: {field: value}} custom edits
let _conflictSelections = {};  // {word_zh: "keep"|"update"}

// Default per-category suspension states (creating active, others suspended)
const IMPORT_DEFAULT_SUSPENDED = { reading: true, listening: false, creating: false };

const NOTE_TYPE_LABEL = { vocabulary: 'Word', sentence: 'Sentence', chengyu: '成语', expression: 'Expr' };
const STATUS_ICON  = { ok: '✓', duplicate: '⚠', invalid: '✕' };
const STATUS_COLOR = { ok: 'var(--clr-ok,#27ae60)', duplicate: '#e67e22', invalid: '#e74c3c' };

// Escape a value for use in an HTML attribute (prevents quote-breaking)
function _ea(str) { return String(str ?? '').replace(/&/g,'&amp;').replace(/"/g,'&quot;'); }

function openYamlEditFromBtn(btn) {
  const idx = btn.dataset.idx !== undefined ? parseInt(btn.dataset.idx) : -1;
  openYamlEdit(btn.dataset.word, btn.dataset.yaml, btn.dataset.deck, idx);
}

// ── Render the per-card import table ─────────────────────────────────────────

function _importRenderTable() {
  const tbody = document.getElementById('import-table-body');
  const globalDeck = document.getElementById('import-deck-path').value.trim();

  const deckOptHtml = `<option value="">— default —</option>` +
    _importDeckOptions.map(p => `<option value="${_ea(p)}">${p}</option>`).join('');

  tbody.innerHTML = _previewEntries.map((e, idx) => {
    const cfg = _cardConfigs[e.simplified] || {};
    const include  = cfg.include ?? (e.status !== 'invalid');
    const susp     = cfg.suspended || IMPORT_DEFAULT_SUSPENDED;
    const deckVal  = cfg.deck_path || '';
    const isInvalid = e.status === 'invalid';
    const isB = deckVal === '__deckB__';

    const rowClass = isInvalid ? 'import-row-invalid' : (!include ? 'import-row-excluded' : '');

    const inclBtnCls = isInvalid ? 'import-toggle-btn inactive' :
                       (include ? 'import-toggle-btn active' : 'import-toggle-btn inactive');
    const inclLabel  = include ? '+' : '−';
    const inclDisabled = isInvalid ? 'disabled' : '';

    const suspBtn = (cat) => {
      const isSusp = susp[cat] ?? IMPORT_DEFAULT_SUSPENDED[cat];
      const cls = isSusp ? 'import-toggle-btn suspended' : 'import-toggle-btn unsuspended';
      const lbl = isSusp ? '✕' : '✓';
      const dis = (!include || isInvalid) ? 'disabled' : '';
      return `<button class="${cls}" ${dis}
        onclick="importToggleSuspended(${_ea(JSON.stringify(e.simplified))}, '${cat}')"
        title="${isSusp ? 'suspended — click to activate' : 'active — click to suspend'}">${lbl}</button>`;
    };

    const statusSpan = `<span style="color:${STATUS_COLOR[e.status]}">${STATUS_ICON[e.status]}</span>` +
      (e.reason ? ` <span style="font-size:10px;color:${STATUS_COLOR[e.status]}" title="${_ea(e.reason)}">!</span>` : '');

    const isDuplicate = e.status === 'duplicate';
    let midCols;
    if (isDuplicate) {
      const dupAction = cfg.duplicate_action || 'move_import';
      const moveTarget = cfg.move_target || '';
      const moveCats = cfg.move_categories || null; // null = all
      const catChecked = (cat) => (!moveCats || moveCats.includes(cat)) ? 'checked' : '';
      const catCheckboxes = `<span style="margin-left:4px;font-size:11px">
          <label title="Listening"><input type="checkbox" ${catChecked('listening')}
            onchange="importToggleDupMoveCat(${_ea(JSON.stringify(e.simplified))}, 'listening', this.checked)">L</label>
          <label title="Reading"><input type="checkbox" ${catChecked('reading')}
            onchange="importToggleDupMoveCat(${_ea(JSON.stringify(e.simplified))}, 'reading', this.checked)">R</label>
          <label title="Creating"><input type="checkbox" ${catChecked('creating')}
            onchange="importToggleDupMoveCat(${_ea(JSON.stringify(e.simplified))}, 'creating', this.checked)">C</label>
        </span>`;
      const moveOpts = dupAction === 'move' ? `
        <input list="import-deck-datalist" class="dup-move-target" value="${_ea(moveTarget)}"
          placeholder="deck path"
          oninput="importSetDupMoveTarget(${_ea(JSON.stringify(e.simplified))}, this.value)"
          style="width:120px;font-size:11px;margin-left:4px">
        ${catCheckboxes}` :
        dupAction === 'move_import' ? `
        <span style="margin-left:4px;font-size:11px;color:var(--clr-muted,#888)">→ import deck</span>
        ${catCheckboxes}` : '';
      const currentDecksHtml = (e.current_decks && e.current_decks.length)
        ? `<span style="font-size:10px;color:var(--clr-muted,#888);margin-right:4px" title="Currently in: ${_ea(e.current_decks.join(', '))}">📂 ${_ea(e.current_decks.join(', '))}</span>`
        : '';
      midCols = `<td colspan="4" style="padding:2px 6px">
        <div style="display:flex;align-items:center;flex-wrap:wrap;gap:2px">
          ${currentDecksHtml}
          <select style="font-size:11px" onchange="importSetDupAction(${_ea(JSON.stringify(e.simplified))}, this.value)">
            <option value="skip"${dupAction==='skip'?' selected':''}>Skip</option>
            <option value="reset"${dupAction==='reset'?' selected':''}>Reset progress</option>
            <option value="move_import"${dupAction==='move_import'?' selected':''}>Move to import deck</option>
            <option value="move"${dupAction==='move'?' selected':''}>Move to deck…</option>
          </select>
          ${moveOpts}
        </div>
      </td>`;
    } else {
      midCols = `<td>${suspBtn('listening')}</td>
      <td>${suspBtn('reading')}</td>
      <td>${suspBtn('creating')}</td>
      <td>
        <div class="import-deck-cell">
          <button class="import-deck-b-badge${isB ? ' active' : ''}"
            onclick="event.stopPropagation();importToggleDeckB(${_ea(JSON.stringify(e.simplified))})"
            title="${isB ? 'Remove Deck B — use default' : 'Assign to Deck B'}"
            ${(!include || isInvalid || !_deckBPath) ? 'disabled' : ''}>B</button>
          <select class="import-row-deck-select"
            onchange="importSetCardDeck(${_ea(JSON.stringify(e.simplified))}, this.value)"
            ${(!include || isInvalid || isB) ? 'disabled' : ''}>
            ${deckOptHtml}
          </select>
        </div>
      </td>`;
    }

    return `<tr class="${rowClass}" id="import-row-${idx}">
      <td>
        <button class="${inclBtnCls}" ${inclDisabled}
          onclick="importToggleInclude(${_ea(JSON.stringify(e.simplified))})">${inclLabel}</button>
      </td>
      <td style="font-weight:500" title="${_ea(e.simplified)}">${e.simplified.length > 6 ? e.simplified.slice(0,4) + '…' : e.simplified}
        ${e.raw_yaml ? `<button class="edit-cancel-btn" style="font-size:10px;padding:1px 5px;margin-left:4px"
          data-word="${_ea(e.simplified)}" data-yaml="${_ea(e.raw_yaml)}" data-deck="" data-idx="${idx}"
          onclick="openYamlEditFromBtn(this)">Edit</button>` : ''}
      </td>
      <td style="color:var(--clr-muted,#888);font-size:11px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${_ea(e.english || '')}">${e.english || ''}</td>
      ${midCols}
      <td style="color:var(--clr-muted,#888);font-size:11px">${NOTE_TYPE_LABEL[e.note_type] || e.note_type}</td>
      <td style="color:var(--clr-muted,#888);font-size:11px">${e.hsk || ''}</td>
      <td>${statusSpan}</td>
    </tr>`;
  }).join('');

  // Set selected deck value for each row's <select> (skip B-assigned rows)
  tbody.querySelectorAll('select.import-row-deck-select').forEach((sel, i) => {
    const e = _previewEntries[i];
    if (!e) return;
    const dp = (_cardConfigs[e.simplified] || {}).deck_path || '';
    sel.value = dp === '__deckB__' ? '' : dp;
  });
}

let _resizeHandlesInited = false;
function _initImportColResize() {
  if (_resizeHandlesInited) return;
  _resizeHandlesInited = true;
  // Remove any leftover handles from a previous open
  document.querySelectorAll('.import-table .col-resize-handle').forEach(h => h.remove());
  document.querySelectorAll('.import-table thead th').forEach(th => {
    const handle = document.createElement('div');
    handle.className = 'col-resize-handle';
    th.appendChild(handle);
    let startX, startW;
    handle.addEventListener('mousedown', e => {
      startX = e.pageX;
      startW = th.offsetWidth;
      handle.classList.add('resizing');
      const onMove = e2 => { th.style.minWidth = Math.max(30, startW + e2.pageX - startX) + 'px'; };
      const onUp = () => {
        handle.classList.remove('resizing');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
      e.preventDefault();
      e.stopPropagation();
    });
  });
}

function importToggleInclude(wordZh) {
  const cfg = _cardConfigs[wordZh] || {};
  _cardConfigs[wordZh] = { ...cfg, include: !(cfg.include ?? true) };
  _importRenderTable();
}

function importToggleSuspended(wordZh, category) {
  const cfg = _cardConfigs[wordZh] || {};
  const susp = { ...IMPORT_DEFAULT_SUSPENDED, ...(cfg.suspended || {}) };
  susp[category] = !susp[category];
  _cardConfigs[wordZh] = { ...cfg, suspended: susp };
  _importRenderTable();
}

function importSetCardDeck(wordZh, deckPath) {
  const cfg = _cardConfigs[wordZh] || {};
  _cardConfigs[wordZh] = { ...cfg, deck_path: deckPath || null };
}

function importSetDupAction(wordZh, action) {
  const cfg = _cardConfigs[wordZh] || {};
  _cardConfigs[wordZh] = { ...cfg, duplicate_action: action };
  _importRenderTable();
}

function importSetDupMoveTarget(wordZh, target) {
  const cfg = _cardConfigs[wordZh] || {};
  _cardConfigs[wordZh] = { ...cfg, move_target: target || null };
}

function importToggleDupMoveCat(wordZh, cat, checked) {
  const cfg = _cardConfigs[wordZh] || {};
  // null means all-categories; convert to explicit list on first toggle
  const allCats = ['listening', 'reading', 'creating'];
  let cats = cfg.move_categories ? [...cfg.move_categories] : [...allCats];
  if (checked) {
    if (!cats.includes(cat)) cats.push(cat);
  } else {
    cats = cats.filter(c => c !== cat);
  }
  // If all selected, store null (= all)
  _cardConfigs[wordZh] = { ...cfg, move_categories: cats.length === allCats.length ? null : cats };
}

function importSelectAll(include) {
  _previewEntries.forEach(e => {
    if (e.status === 'invalid') return;
    const cfg = _cardConfigs[e.simplified] || {};
    _cardConfigs[e.simplified] = { ...cfg, include };
  });
  _importRenderTable();
}

function importSetAllSuspended(category, suspended) {
  _previewEntries.forEach(e => {
    if (e.status === 'invalid') return;
    const cfg = _cardConfigs[e.simplified] || {};
    const susp = { ...IMPORT_DEFAULT_SUSPENDED, ...(cfg.suspended || {}) };
    susp[category] = suspended;
    _cardConfigs[e.simplified] = { ...cfg, suspended: susp };
  });
  _importRenderTable();
}

function selectDailyDeck() {
  const d = new Date();
  const mmdd = String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  deckPickerSelect('daily::' + mmdd);
  importApplyGlobalDeck();
}

function importApplyGlobalDeck() {
  // Keep datalist in sync so new deck names appear in the move-target autocomplete
  const importDeckPath = document.getElementById('import-deck-path').value.trim();
  if (importDeckPath && !_importDeckOptions.includes(importDeckPath)) {
    const dl = document.getElementById('import-deck-datalist');
    if (dl) dl.innerHTML = [..._importDeckOptions, importDeckPath].map(p => `<option value="${_ea(p)}">`).join('');
  }
  _importRenderTable();
}

let _importDecksPromise = null;

async function _loadImportDeckSuggestions() {
  const decks = await api('GET', '/api/decks');
  window._deckSuggestions = [];
  _importDeckOptions = [];
  function addDeckSuggestions(list, prefix) {
    for (const d of list) {
      if (d.virtual) {
        if (d.children && d.children.length) addDeckSuggestions(d.children, prefix);
        continue;
      }
      if (d.category) continue;
      const path = prefix ? `${prefix}::${d.name}` : d.name;
      window._deckSuggestions.push(path);
      _importDeckOptions.push(path);
      if (d.children && d.children.length) addDeckSuggestions(d.children, path);
    }
  }
  addDeckSuggestions(decks, '');

  // Populate datalist for duplicate move-target autocomplete
  const dl = document.getElementById('import-deck-datalist');
  if (dl) dl.innerHTML = _importDeckOptions.map(p => `<option value="${_ea(p)}">`).join('');
}

function openImportModal() {
  // Hide modal in case this is a "Try Again" from an error state
  document.getElementById('import-modal-overlay').style.display = 'none';
  document.getElementById('import-modal').style.display = 'none';

  importResolutions = {};
  _previewEntries = [];
  _cardConfigs = {};
  _conflictData = [];
  _conflictEdits = {};
  _conflictSelections = {};
  document.getElementById('import-file').value = '';
  document.getElementById('import-preview').style.display = 'none';
  document.getElementById('import-conflicts-section').style.display = 'none';
  document.getElementById('import-result').style.display = 'none';
  document.getElementById('import-submit-btn').style.display = '';
  document.getElementById('import-deck-path').value = '';
  document.getElementById('deck-picker-new-badge').style.display = 'none';
  document.getElementById('deck-picker-dropdown').style.display = 'none';

  // Open OS file picker immediately — .click() must stay synchronous within the
  // user gesture; awaiting the network first delayed the picker by seconds (#466)
  document.getElementById('import-file').click();

  // Deck suggestions load in parallel; previewImport awaits them before rendering
  _importDecksPromise = _loadImportDeckSuggestions();
}

function closeImportModal() {
  document.getElementById('import-modal-overlay').style.display = 'none';
  document.getElementById('import-modal').style.display = 'none';
  const btn = document.getElementById('import-submit-btn');
  btn.onclick = doImport;
  btn.disabled = false;
  btn.textContent = 'Import';
  _resizeHandlesInited = false;
  _deckBPath = null;
  document.getElementById('import-deck-b-path').value = '';
  document.getElementById('deck-b-new-badge').style.display = 'none';
  document.getElementById('deck-b-picker-dropdown').style.display = 'none';
}

function onImportFileChange() {
  const fileInput = document.getElementById('import-file');
  if (!fileInput.files.length) return;  // user cancelled picker

  importResolutions = {};
  _previewEntries = [];
  _cardConfigs = {};
  _conflictData = [];
  _conflictEdits = {};
  _conflictSelections = {};
  document.getElementById('import-preview').style.display = 'none';
  document.getElementById('import-conflicts-section').style.display = 'none';
  document.getElementById('import-result').style.display = 'none';
  document.getElementById('import-deck-section').style.display = 'none';
  document.getElementById('import-submit-btn').style.display = 'none';

  // Open modal now that a file has been chosen
  document.getElementById('import-modal-overlay').style.display = 'block';
  document.getElementById('import-modal').style.display = 'flex';

  // Auto-preview as soon as a file is selected
  previewImport();
}

async function previewImport(yamlContent) {
  const fileInput = document.getElementById('import-file');
  if (!yamlContent && !fileInput.files.length) { showError('Please select a YAML file.'); return; }

  const btn = document.getElementById('import-preview-btn');
  btn.disabled = true;
  btn.textContent = 'Loading…';

  const form = new FormData();
  if (yamlContent) {
    form.append('file', new File([yamlContent], 'edited.yaml', { type: 'application/x-yaml' }));
  } else {
    form.append('file', fileInput.files[0]);
  }

  try {
    const res = await fetch('/api/import/preview', { method: 'POST', body: form });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const data = await res.json();

    if (data.error) {
      const resultEl = document.getElementById('import-result');
      const d = data.error_detail || {};
      let msg = '<strong style="color:#e74c3c">⚠ YAML parse error</strong>';
      if (d.line) msg += ` at line ${d.line}${d.column ? `, column ${d.column}` : ''}`;
      msg += '<br>';
      const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      if (d.problem) msg += `<br><strong>Problem:</strong> ${esc(d.problem)}`;
      if (d.context) msg += `<br><strong>Context:</strong> ${esc(d.context)}`;
      if (d.tip)     msg += `<br><br><span style="color:#f39c12">${esc(d.tip)}</span>`;
      if (!d.problem && !d.context) msg += `<br>${esc(data.error)}`;
      resultEl.innerHTML = `<div style="font-family:monospace;font-size:12px;background:rgba(231,76,60,.08);border-radius:4px;padding:10px;line-height:1.7">${msg}</div>`;
      resultEl.style.display = 'block';
      // Show "Try Again" button — no deck picker needed yet
      document.getElementById('import-deck-section').style.display = 'none';
      const submitBtn = document.getElementById('import-submit-btn');
      submitBtn.textContent = 'Try Again';
      submitBtn.onclick = openImportModal;
      submitBtn.style.display = '';
      btn.disabled = false;
      btn.textContent = 'Preview';
      return;
    }

    // Summary line
    const s = data.summary;
    const summaryEl = document.getElementById('import-summary');
    const parts = [];
    if (s.ok)           parts.push(`<span style="color:${STATUS_COLOR.ok}">${s.ok} ready</span>`);
    if (s.duplicate)    parts.push(`<span style="color:${STATUS_COLOR.duplicate}">${s.duplicate} duplicate</span>`);
    if (s.invalid)      parts.push(`<span style="color:${STATUS_COLOR.invalid}">${s.invalid} invalid</span>`);
    if (s.unknown_type) parts.push(`${s.unknown_type} unknown type`);
    summaryEl.innerHTML = parts.join(' · ') || 'No importable entries found.';

    // Initialize card configs with defaults
    _previewEntries = data.entries;
    const prevConfigs = { ..._cardConfigs };  // preserve any existing user changes
    _cardConfigs = {};
    data.entries.forEach(e => {
      if (prevConfigs[e.simplified]) {
        const prev = prevConfigs[e.simplified];
        // If status changed from invalid → ok/duplicate, reset include to true
        const wasInvalid = prev.include === false && e.status !== 'invalid';
        _cardConfigs[e.simplified] = wasInvalid
          ? { ...prev, include: true }
          : prev;
      } else {
        _cardConfigs[e.simplified] = {
          include: e.status !== 'invalid',
          deck_path: null,
          suspended: { ...IMPORT_DEFAULT_SUSPENDED },
          ...(e.status === 'duplicate' ? { duplicate_action: 'move_import' } : {}),
        };
      }
    });

    // Deck suggestions were kicked off in openImportModal; the table's per-card
    // deck dropdowns need them, so wait here (usually already resolved)
    if (_importDecksPromise) { try { await _importDecksPromise; } catch (e) { console.error('Deck suggestions failed to load:', e); } }

    _importRenderTable();
    document.getElementById('import-preview').style.display = 'block';
    _initImportColResize();

    // Conflict resolution
    if (data.conflicts && data.conflicts.length > 0) {
      importResolutions = {};
      _conflictData = data.conflicts;
      _conflictSelections = {};
      _conflictEdits = {};
      data.conflicts.forEach(c => { _conflictSelections[c.simplified] = 'keep'; });
      document.getElementById('import-conflicts-count').textContent = data.conflicts.length;
      document.getElementById('import-conflicts-section').style.display = 'block';
    } else {
      _conflictData = [];
      document.getElementById('import-conflicts-section').style.display = 'none';
    }

    // Show deck picker + Import button now that YAML is valid
    document.getElementById('import-deck-section').style.display = '';
    // Auto-select today's daily deck as the target, unless the user already chose one.
    if (!document.getElementById('import-deck-path').value.trim()) selectDailyDeck();
    const submitBtn = document.getElementById('import-submit-btn');
    submitBtn.textContent = 'Import';
    submitBtn.onclick = doImport;
    submitBtn.style.display = '';
    if (!yamlContent) btn.style.display = 'none';
    else { btn.disabled = false; btn.textContent = 'Preview'; }
  } catch (e) {
    showError('Preview failed: ' + e.message);
    btn.disabled = false;
    btn.textContent = 'Preview';
  }
}

async function doImport() {
  // If a file was loaded via the YAML editor preview flow, fall back to upload
  const fileInput = document.getElementById('import-file');
  if (fileInput.files.length) { return _doUploadImport(); }

  const deckPath  = document.getElementById('import-deck-path').value.trim();
  const resultEl  = document.getElementById('import-result');

  if (!deckPath) { showError('Please enter a target deck.'); return; }

  const btn = document.getElementById('import-submit-btn');
  btn.disabled = true;
  btn.textContent = 'Importing…';
  resultEl.style.display = 'none';

  const form = new FormData();
  form.append('deck_path', deckPath);

  try {
    const res = await fetch('/api/import/directory', { method: 'POST', body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);

    const hasErrors = data.errors && data.errors.length > 0;

    if (!hasErrors) {
      closeImportModal();
    }

    loadDecks();
    const parts = [`✓ Imported ${data.imported}`];
    if (data.skipped_duplicate) parts.push(`${data.skipped_duplicate} duplicates skipped`);
    if (data.skipped_invalid)   parts.push(`${data.skipped_invalid} invalid skipped`);
    if (hasErrors) parts.push(`${data.errors.length} file error(s)`);

    if (hasErrors) {
      // Show detailed errors inside the modal
      const errLines = data.errors.map(e => {
        let msg = `⚠ ${e.file || 'unknown file'}`;
        if (e.line)    msg += `, line ${e.line}`;
        if (e.column)  msg += `, col ${e.column}`;
        msg += '\n';
        if (e.problem) msg += `  Problem: ${e.problem}\n`;
        if (e.context) msg += `  Context: ${e.context}\n`;
        if (e.tip)     msg += `  Tip: ${e.tip}\n`;
        return msg;
      }).join('\n');
      resultEl.innerHTML =
        `<div style="color:#27ae60;margin-bottom:6px">${parts.join(' · ')}</div>` +
        `<div style="color:#e74c3c;background:rgba(231,76,60,.08);border-radius:4px;padding:8px;font-family:monospace;font-size:12px">${errLines.replace(/</g,'&lt;').replace(/\n/g,'<br>')}</div>`;
      resultEl.style.display = 'block';
      btn.disabled = false;
      btn.textContent = 'Import';
    } else {
      const banner = document.getElementById('error-banner');
      banner.textContent = parts.join(' · ');
      banner.style.background = '#27ae60';
      banner.style.color = '#fff';
      banner.style.display = 'block';
      setTimeout(() => { banner.style.display = 'none'; banner.style.background = ''; banner.style.color = ''; }, 4000);
    }
  } catch (e) {
    resultEl.style.display = 'block';
    resultEl.innerHTML = `<span style="color:#e74c3c">Error: ${e.message}</span>`;
    btn.disabled = false;
    btn.textContent = 'Import';
  }
}

// Poll /api/import/progress/{jobId} until the background import thread
// finishes (issue #458 — upload no longer blocks the request). Resolves with
// the import summary dict, or throws on error/timeout.
async function _pollImportJob(jobId, { timeoutMs = 5 * 60 * 1000, intervalMs = 1000 } = {}) {
  const start = Date.now();
  while (true) {
    if (Date.now() - start > timeoutMs) throw new Error('Import timed out after 5 minutes.');
    const res = await fetch(`/api/import/progress/${jobId}`);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const job = await res.json();
    if (job.status === 'done') return job.summary;
    if (job.status === 'error') throw new Error(job.error || 'Import failed');
    await new Promise(r => setTimeout(r, intervalMs));
  }
}

function _showImportPollingSpinner(resultEl) {
  resultEl.style.display = 'block';
  resultEl.innerHTML = '<div style="display:flex;align-items:center;gap:10px;padding:4px 0">'
    + '<div class="spinner" style="width:18px;height:18px;border-width:2px;margin:0"></div>'
    + '<span>Importing…</span></div>';
}

async function _doUploadImport() {
  // Legacy flow: used when YAML editor previews a file via file input
  const fileInput = document.getElementById('import-file');
  const deckPath  = document.getElementById('import-deck-path').value.trim();
  const resultEl  = document.getElementById('import-result');

  if (!deckPath) { showError('Please enter a target deck.'); return; }

  const btn = document.getElementById('import-submit-btn');
  btn.disabled = true;
  btn.textContent = 'Importing…';

  const cardConfigsMap = {};
  _previewEntries.forEach(e => {
    const cfg = _cardConfigs[e.simplified];
    if (cfg) {
      let resolved = {
        ...cfg,
        deck_path: cfg.deck_path === '__deckB__' ? (_deckBPath || null) : cfg.deck_path
      };
      if (resolved.duplicate_action === 'move_import') {
        resolved = { ...resolved, duplicate_action: 'move', move_target: deckPath || null };
      }
      cardConfigsMap[e.simplified] = resolved;
    }
  });

  const form = new FormData();
  form.append('file', fileInput.files[0]);
  form.append('deck_path', deckPath);
  if (Object.keys(importResolutions).length > 0) {
    form.append('resolutions', JSON.stringify(importResolutions));
  }
  form.append('card_configs', JSON.stringify(cardConfigsMap));
  const customFieldsMap = {};
  _conflictData.forEach(c => {
    if (importResolutions[c.simplified] === 'custom') {
      const sel = _conflictSelections[c.simplified] || 'keep';
      const base = sel === 'keep' ? c.existing : c.incoming;
      const edits = _conflictEdits[c.simplified] || {};
      customFieldsMap[c.simplified] = { ...base, ...edits };
    }
  });
  if (Object.keys(customFieldsMap).length > 0) {
    form.append('custom_fields', JSON.stringify(customFieldsMap));
  }

  try {
    const res = await fetch('/api/import/upload', { method: 'POST', body: form });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    const { job_id } = await res.json();
    _showImportPollingSpinner(resultEl);
    const data = await _pollImportJob(job_id);
    resultEl.style.display = 'none';
    closeImportModal();
    loadDecks();

    const parts = [`✓ Imported ${data.imported}`];
    if (data.skipped_duplicate) parts.push(`${data.skipped_duplicate} duplicates skipped`);
    if (data.skipped_invalid)   parts.push(`${data.skipped_invalid} invalid skipped`);
    const banner = document.getElementById('error-banner');
    banner.textContent = parts.join(' · ');
    banner.style.background = data.skipped_invalid ? '#e67e22' : '#27ae60';
    banner.style.color = '#fff';
    banner.style.display = 'block';
    setTimeout(() => { banner.style.display = 'none'; banner.style.background = ''; banner.style.color = ''; }, 4000);
  } catch (e) {
    resultEl.style.display = 'block';
    resultEl.innerHTML = `<span style="color:#e74c3c">Error: ${e.message}</span>`;
    btn.disabled = false;
    btn.textContent = 'Import';
  }
}

const _CF_FIELD_LABELS = { pinyin: 'Pinyin', definition: 'Definition', traditional: 'Traditional' };

function openConflictModal() {
  _renderConflictModal();
  document.getElementById('conflict-modal-overlay').style.display = 'block';
  document.getElementById('conflict-modal').style.display = 'flex';
}

function closeConflictModal() {
  document.getElementById('conflict-modal-overlay').style.display = 'none';
  document.getElementById('conflict-modal').style.display = 'none';
}

function _renderConflictModal() {
  const body = document.getElementById('conflict-modal-body');
  body.innerHTML = _conflictData.map((c, idx) => {
    const sel = _conflictSelections[c.simplified] || 'keep';
    const edits = _conflictEdits[c.simplified] || {};

    const renderField = (f) => {
      const existingVal = c.existing[f] || '';
      const incomingVal = c.incoming[f] || '';
      const isEdited = edits[f] !== undefined;
      const isDiff = existingVal !== incomingVal;
      const currentVal = isEdited ? edits[f] : (sel === 'keep' ? existingVal : incomingVal);
      return `
        <div class="cf-field">
          <div class="cf-field-label">
            ${_CF_FIELD_LABELS[f]}
            <span id="cf-badge-${idx}-${f}" class="cf-edited-badge" style="${isEdited ? '' : 'display:none'}">edited</span>
            ${isDiff && !isEdited ? `<span class="cf-diff-badge">differs</span>` : ''}
          </div>
          <div class="cf-field-compare">
            <span class="cf-compare-val ${sel === 'keep' && !isEdited ? 'cf-active' : ''}"
              title="Existing: ${_ea(existingVal)}"
              onclick="conflictLoadField(${idx},'${f}','existing')">${existingVal || '—'}</span>
            <span style="color:var(--clr-muted,#888)">↔</span>
            <span class="cf-compare-val ${sel === 'update' && !isEdited ? 'cf-active' : ''}"
              title="Incoming: ${_ea(incomingVal)}"
              onclick="conflictLoadField(${idx},'${f}','incoming')">${incomingVal || '—'}</span>
          </div>
          <input class="edit-input cf-field-input" value="${_ea(currentVal)}"
            oninput="conflictEditField(${idx},'${f}',this.value)">
        </div>`;
    };

    return `
      <div class="cf-card">
        <div class="cf-card-header">
          <span class="cf-word">${c.simplified}</span>
          <div class="cf-version-btns">
            <button class="cf-version-btn ${sel === 'keep' ? 'cf-version-selected' : ''}"
              onclick="conflictSelectVersion(${idx},'keep')">✓ Existing</button>
            <button class="cf-version-btn ${sel === 'update' ? 'cf-version-selected' : ''}"
              onclick="conflictSelectVersion(${idx},'update')">✓ Incoming</button>
          </div>
        </div>
        ${Object.keys(_CF_FIELD_LABELS).map(renderField).join('')}
      </div>`;
  }).join('');
}

function conflictSelectVersion(idx, version) {
  const c = _conflictData[idx];
  if (!c) return;
  _conflictSelections[c.simplified] = version;
  delete _conflictEdits[c.simplified];
  _renderConflictModal();
}

function conflictLoadField(idx, field, source) {
  const c = _conflictData[idx];
  if (!c) return;
  const val = source === 'existing' ? (c.existing[field] || '') : (c.incoming[field] || '');
  _conflictEdits[c.simplified] = { ...(_conflictEdits[c.simplified] || {}), [field]: val };
  _renderConflictModal();
}

function conflictEditField(idx, field, value) {
  const c = _conflictData[idx];
  if (!c) return;
  const edits = { ...(_conflictEdits[c.simplified] || {}) };
  const sel = _conflictSelections[c.simplified] || 'keep';
  const baseVal = sel === 'keep' ? (c.existing[field] || '') : (c.incoming[field] || '');
  if (value !== baseVal) {
    edits[field] = value;
  } else {
    delete edits[field];
  }
  _conflictEdits[c.simplified] = Object.keys(edits).length ? edits : undefined;
  if (!_conflictEdits[c.simplified]) delete _conflictEdits[c.simplified];
  // Update just the badge without re-rendering (preserve focus)
  const badgeEl = document.getElementById(`cf-badge-${idx}-${field}`);
  if (badgeEl) {
    badgeEl.style.display = edits[field] !== undefined ? '' : 'none';
  }
}

function conflictAcceptAll(version) {
  _conflictData.forEach(c => {
    _conflictSelections[c.simplified] = version;
    delete _conflictEdits[c.simplified];
  });
  _renderConflictModal();
}

function conflictDone() {
  importResolutions = {};
  _conflictData.forEach(c => {
    const edits = _conflictEdits[c.simplified];
    if (edits && Object.keys(edits).length > 0) {
      importResolutions[c.simplified] = 'custom';
    } else {
      importResolutions[c.simplified] = _conflictSelections[c.simplified] || 'keep';
    }
  });
  closeConflictModal();
}

// ── Trash ────────────────────────────────────────────────────────────────────
let _trashData = null;
let _trashExpandedDecks = new Set();

function _trashDaysLeft(deleted_at) {
  const purgeDate = new Date(deleted_at + 'Z');
  purgeDate.setDate(purgeDate.getDate() + 30);
  return Math.ceil((purgeDate - Date.now()) / 86400000);
}

function _renderTrash() {
  const { decks, cards } = _trashData;
  const body = document.getElementById('trash-modal-body');
  const isEmpty = !decks.length && !cards.length;
  document.getElementById('trash-empty-all-btn').style.display = isEmpty ? 'none' : '';
  let html = '';

  if (decks.length) {
    html += '<div class="trash-section-header">Decks</div>';
    for (const d of decks) {
      const expanded = _trashExpandedDecks.has(d.id);
      const hasCards = d.cards && d.cards.length > 0;
      const toggleIcon = hasCards
        ? `<button class="trash-toggle" onclick="toggleTrashDeck(${d.id})">${expanded ? '▾' : '▸'}</button>`
        : `<span class="trash-toggle-spacer"></span>`;
      html += `<div class="trash-row">
        ${toggleIcon}
        <div class="trash-row-info">
          <span class="trash-name">${d.name}</span>
          <span class="trash-meta">${hasCards ? d.cards.length + ' card' + (d.cards.length !== 1 ? 's' : '') : 'empty'} · ${_trashDaysLeft(d.deleted_at)}d left</span>
        </div>
        <div class="trash-row-actions">
          <button class="trash-restore-btn" onclick="restoreDeck(${d.id})">Restore</button>
          <button class="trash-purge-btn" onclick="purgeDeck(${d.id})">Delete</button>
        </div>
      </div>`;
      if (expanded && hasCards) {
        html += `<div class="trash-deck-cards">
          <div class="trash-deck-cards-header">
            <span class="trash-deck-cards-count">${d.cards.length} card${d.cards.length !== 1 ? 's' : ''}</span>
            <button class="trash-purge-btn trash-purge-all-cards-btn" onclick="purgeAllCardsFromDeck(${d.id}, ${d.cards.length})">Delete all</button>
          </div>`;
        for (const c of d.cards) {
          html += `<div class="trash-card-row">
            <div class="trash-row-info">
              <span class="trash-name">${c.word_zh}</span>
              <span class="trash-meta">${c.category} · ${c.state}</span>
            </div>
            <button class="trash-purge-btn" onclick="purgeCardFromDeck(${d.id}, ${c.id})">Delete</button>
          </div>`;
        }
        html += '</div>';
      }
    }
  }

  if (cards.length) {
    html += '<div class="trash-section-header">Cards</div>';
    html += cards.map(c => `<div class="trash-row">
      <div class="trash-row-info">
        <span class="trash-name">${c.word_zh}</span>
        <span class="trash-meta">${c.category} · ${c.deck_path} · ${_trashDaysLeft(c.deleted_at)}d left</span>
      </div>
      <div class="trash-row-actions">
        <button class="trash-restore-btn" onclick="restoreCard(${c.id})">Restore</button>
        <button class="trash-purge-btn" onclick="purgeCard(${c.id})">Delete</button>
      </div>
    </div>`).join('');
  }

  body.innerHTML = html || '<div class="trash-empty">Trash is empty</div>';
}

async function _refreshTrash() {
  const body = document.getElementById('trash-modal-body');
  try {
    _trashData = await api('GET', '/api/trash');
    _renderTrash();
  } catch (e) {
    body.innerHTML = `<div class="trash-empty">Error: ${e.message}</div>`;
  }
}

async function openTrash() {
  document.getElementById('trash-modal-overlay').style.display = '';
  document.getElementById('trash-modal').style.display = '';
  document.getElementById('trash-modal-body').innerHTML = '<div class="trash-empty">Loading…</div>';
  await _refreshTrash();
}

function toggleTrashDeck(id) {
  if (_trashExpandedDecks.has(id)) _trashExpandedDecks.delete(id);
  else _trashExpandedDecks.add(id);
  _renderTrash();
}

function closeTrash() {
  document.getElementById('trash-modal-overlay').style.display = 'none';
  document.getElementById('trash-modal').style.display = 'none';
}
async function restoreDeck(id) {
  await api('POST', `/api/trash/${id}/restore`);
  loadDecks();
  await _refreshTrash();
}
async function purgeDeck(id) {
  const ok = await showConfirm('Permanently delete this deck and all its cards?');
  if (!ok) return;
  await api('DELETE', `/api/trash/${id}`);
  _trashExpandedDecks.delete(id);
  await _refreshTrash();
  loadDecks();
}
async function restoreCard(id) {
  await api('POST', `/api/trash/cards/${id}/restore`);
  await _refreshTrash();
}
async function purgeCard(id) {
  const ok = await showConfirm('Permanently delete this card?');
  if (!ok) return;
  await api('DELETE', `/api/trash/cards/${id}`);
  await _refreshTrash();
}
async function purgeCardFromDeck(deckId, cardId) {
  const ok = await showConfirm('Permanently delete this card?');
  if (!ok) return;
  await api('DELETE', `/api/trash/${deckId}/cards/${cardId}`);
  await _refreshTrash();
}
async function purgeAllCardsFromDeck(deckId, count) {
  const ok = await showConfirm(`Permanently delete all ${count} card${count !== 1 ? 's' : ''} in this deck?`);
  if (!ok) return;
  await api('DELETE', `/api/trash/${deckId}/cards`);
  await _refreshTrash();
}
async function emptyTrash() {
  const ok = await showConfirm('Permanently delete everything in trash? This cannot be undone.');
  if (!ok) return;
  await api('DELETE', '/api/trash');
  _trashExpandedDecks.clear();
  await _refreshTrash();
  loadDecks();
}
// ── YAML entry editor ────────────────────────────────────────────────────────

let _yamlEditDeckPath = '';
let _yamlEditEntryIdx = -1; // >=0 means opened from preview table → Save mode

function openYamlEdit(wordZh, rawYaml, deckPath, entryIdx) {
  _yamlEditDeckPath = deckPath || document.getElementById('import-deck-path').value.trim();
  _yamlEditEntryIdx = (entryIdx !== undefined && entryIdx >= 0) ? entryIdx : -1;
  document.getElementById('yaml-edit-title').textContent = wordZh;
  document.getElementById('yaml-edit-textarea').value = rawYaml;
  document.getElementById('yaml-edit-feedback').style.display = 'none';
  document.getElementById('yaml-edit-feedback').innerHTML = '';
  document.getElementById('yaml-edit-check-btn').disabled = false;
  const importBtn = document.getElementById('yaml-edit-import-btn');
  importBtn.disabled = false;
  if (_yamlEditEntryIdx >= 0) {
    importBtn.textContent = 'Save';
    importBtn.onclick = saveYamlEdit;
  } else {
    importBtn.textContent = 'Import';
    importBtn.onclick = importYamlEntry;
  }
  document.getElementById('yaml-edit-overlay').style.display = 'block';
  document.getElementById('yaml-edit-modal').style.display = 'flex';
}

function closeYamlEdit() {
  document.getElementById('yaml-edit-overlay').style.display = 'none';
  document.getElementById('yaml-edit-modal').style.display = 'none';
}

async function saveYamlEdit() {
  if (_yamlEditEntryIdx < 0 || !_previewEntries.length) return;
  const newYaml = document.getElementById('yaml-edit-textarea').value.trim();
  // Update the entry in our in-memory list
  _previewEntries[_yamlEditEntryIdx].raw_yaml = newYaml;
  // Reconstruct the full YAML from all entries that have raw_yaml
  const yamlContent = _previewEntries
    .filter(e => e.raw_yaml)
    .map(e => `- ${e.raw_yaml.replace(/\n/g, '\n  ')}`)
    .join('\n');
  closeYamlEdit();
  await previewImport(yamlContent);
}

async function checkYamlEntry() {
  const yamlText = document.getElementById('yaml-edit-textarea').value.trim();
  const feedbackEl = document.getElementById('yaml-edit-feedback');
  const btn = document.getElementById('yaml-edit-check-btn');
  btn.disabled = true;
  btn.textContent = 'Checking…';

  try {
    const blob = new Blob([`- ${yamlText.replace(/\n/g, '\n  ')}`], { type: 'application/x-yaml' });
    const form = new FormData();
    form.append('file', new File([blob], 'entry.yaml'));
    const res = await fetch('/api/import/preview', { method: 'POST', body: form });
    const data = await res.json();

    feedbackEl.style.display = 'block';
    if (data.error) {
      feedbackEl.innerHTML = `<span style="color:#e74c3c">YAML error: ${data.error}</span>`;
    } else if (!data.entries.length) {
      feedbackEl.innerHTML = `<span style="color:#e74c3c">No entry found — check the YAML structure.</span>`;
    } else {
      const e = data.entries[0];
      const color = STATUS_COLOR[e.status] || '#888';
      feedbackEl.innerHTML = `<span style="color:${color}">${STATUS_ICON[e.status]} ${e.simplified}</span>`
        + (e.reason ? ` <span style="color:#e74c3c;font-size:12px">${e.reason}</span>` : '')
        + (e.status === 'ok' ? ` <span style="color:var(--clr-muted,#888);font-size:12px">— ready to import</span>` : '');
    }
  } catch (err) {
    feedbackEl.style.display = 'block';
    feedbackEl.innerHTML = `<span style="color:#e74c3c">Check failed: ${err.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Check';
  }
}

async function importYamlEntry() {
  const yamlText = document.getElementById('yaml-edit-textarea').value.trim();
  const feedbackEl = document.getElementById('yaml-edit-feedback');
  const btn = document.getElementById('yaml-edit-import-btn');

  if (!_yamlEditDeckPath) {
    feedbackEl.style.display = 'block';
    feedbackEl.innerHTML = `<span style="color:#e74c3c">No target deck — go back and set one.</span>`;
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Importing…';

  try {
    // Wrap the entry dict as a YAML list item
    const blob = new Blob([`- ${yamlText.replace(/\n/g, '\n  ')}`], { type: 'application/x-yaml' });
    const form = new FormData();
    form.append('file', new File([blob], 'entry.yaml'));
    form.append('deck_path', _yamlEditDeckPath);
    const res = await fetch('/api/import/upload', { method: 'POST', body: form });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const { job_id } = await res.json();
    feedbackEl.style.display = 'block';
    feedbackEl.innerHTML = '<div style="display:flex;align-items:center;gap:8px">'
      + '<div class="spinner" style="width:16px;height:16px;border-width:2px;margin:0"></div>'
      + '<span>Importing…</span></div>';
    const data = await _pollImportJob(job_id);

    feedbackEl.style.display = 'block';
    if (data.imported > 0) {
      feedbackEl.innerHTML = `<span style="color:${STATUS_COLOR.ok}">Imported successfully.</span>`;
      btn.textContent = 'Done';
      btn.onclick = closeYamlEdit;
      btn.disabled = false;
      loadDecks();
    } else if (data.skipped_duplicate > 0) {
      feedbackEl.innerHTML = `<span style="color:#e67e22">Already in deck — nothing imported.</span>`;
      btn.disabled = false;
      btn.textContent = 'Import';
    } else {
      const reason = data.skipped_entries?.[0]?.reason || 'unknown reason';
      feedbackEl.innerHTML = `<span style="color:#e74c3c">Still invalid: ${reason}</span>`;
      btn.disabled = false;
      btn.textContent = 'Import';
    }
  } catch (err) {
    feedbackEl.style.display = 'block';
    feedbackEl.innerHTML = `<span style="color:#e74c3c">Import failed: ${err.message}</span>`;
    btn.disabled = false;
    btn.textContent = 'Import';
  }
}


function _isVisible(id) {
  const el = document.getElementById(id);
  return !!el && getComputedStyle(el).display !== 'none';
}

function _isEditableFocusTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  // Non-text input controls (range slider, checkbox, etc.) don't capture
  // typing, so they must NOT block review/global shortcuts. Otherwise, e.g.
  // focusing the listening Hint slider would swallow keys 1–5 and Space.
  if (tag === 'INPUT') {
    const NON_TEXT = ['range', 'checkbox', 'radio', 'button', 'submit', 'reset', 'file', 'color'];
    if (NON_TEXT.includes((el.type || '').toLowerCase())) return false;
  }
  const editable = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
  if (!editable) return false;
  const style = getComputedStyle(el);
  return style.display !== 'none' && style.visibility !== 'hidden';
}

function _hasOpenModal() {
  const modalIds = [
    'modal-overlay',
    'edit-modal-overlay',
    'story-modal-overlay',
    'import-modal-overlay',
    'yaml-edit-overlay',
    'prompt-modal-overlay',
    'trash-modal-overlay',
    'story-error-overlay',
    'hanzi-regen-modal-overlay',
    'hanzi-edit-modal-overlay',
    'conflict-modal-overlay',
    'kahneman-examples-overlay',
    'session-summary-overlay',
    'logs-modal-overlay',
  ];
  return modalIds.some(_isVisible);
}

// ── FSRS scheduler inspector (Shift+S) ──────────────────────────────────────
const _RATING_NAMES = { 1: 'Again', 2: 'Hard', 3: 'Good', 4: 'Easy' };

function _fsrsInspectorOpen() {
  const ov = document.getElementById('fsrs-inspector-overlay');
  return ov && ov.style.display !== 'none' && ov.style.display !== '';
}
function toggleFsrsInspector() {
  if (_fsrsInspectorOpen()) closeFsrsInspector();
  else openFsrsInspector();
}
function openFsrsInspector() {
  const ov = document.getElementById('fsrs-inspector-overlay');
  if (!ov) return;
  renderFsrsInspector();
  ov.style.display = 'flex';
}
function closeFsrsInspector() {
  const ov = document.getElementById('fsrs-inspector-overlay');
  if (ov) ov.style.display = 'none';
}

function _fsrsFmtIvl(d) {
  if (d == null) return '—';
  if (d < 1) return '<1d';
  if (d < 31) return d + 'd';
  if (d < 365) { const m = Math.floor(d / 30), r = d % 30; return r ? `${m}mo ${r}d` : `${m}mo`; }
  return (d / 365).toFixed(1).replace(/\.0$/, '') + 'y';
}
function _fsrsParam(k, v, title) {
  const t = title ? ` title="${title}"` : '';
  return `<div class="fsrs-param"${t}><span class="k">${k}</span><span class="v">${v}</span></div>`;
}

function renderFsrsInspector() {
  const body = document.getElementById('fsrs-insp-body');
  if (!body) return;
  if (!card) { body.innerHTML = '<div class="fsrs-note">No card is being reviewed.</div>'; return; }
  const f = card.fsrs;
  if (!f) { body.innerHTML = '<div class="fsrs-note">This card has no FSRS data.</div>'; return; }

  const word = card.word_zh ? `「${card.word_zh}」` : '';
  const S = f.stability, D = f.difficulty, R = f.retrievability;
  let html = '';

  if (!f.enabled) html += `<div class="fsrs-note">⚠️ FSRS is not enabled for this deck (still using legacy SM-2).</div>`;

  html += `<div class="fsrs-section-label">Current parameters ${word} · ${f.state}</div>`;
  html += '<div class="fsrs-params">';
  html += _fsrsParam('Stability', S != null ? S.toFixed(2) + ' d' : '—', 'How many days memory lasts (decays to 90%)');
  html += _fsrsParam('Difficulty', D != null ? D.toFixed(2) + ' /10' : '—', '1–10, higher is harder; reverts toward the mean each time');
  html += _fsrsParam('Elapsed', f.elapsed_days + ' d', 'Days since the last review');
  html += _fsrsParam('Desired R', Math.round(f.desired_retention * 100) + '%', 'Target recall rate; determines all intervals');
  if (R != null) {
    html += `<div class="fsrs-param full"><span class="k">Retrievability</span><span class="v">${(R * 100).toFixed(1)}%</span></div>`;
    html += `<div class="fsrs-param full" style="background:transparent;padding:0"><div class="fsrs-bar"><i style="width:${Math.round(R * 100)}%"></i></div></div>`;
  }
  html += '</div>';

  if (f.ratings && Object.keys(f.ratings).length) {
    html += `<div class="fsrs-section-label">What each rating does</div>`;
    html += '<table class="fsrs-table"><thead><tr><th>Rating</th><th>New S</th><th>New D</th><th>Next interval</th></tr></thead><tbody>';
    [1, 2, 3, 4].forEach(r => {
      const e = f.ratings[String(r)];
      if (!e) return;
      const note = r === 1 ? ' <span style="color:var(--muted);font-weight:400">(enters relearning first)</span>' : '';
      html += `<tr class="r-row-${r}"><td class="rate-cell">${_RATING_NAMES[r]}</td><td>${e.stability}</td><td>${e.difficulty}</td><td class="ivl">${_fsrsFmtIvl(e.interval)}${note}</td></tr>`;
    });
    html += '</tbody></table>';
  } else {
    html += `<div class="fsrs-note">This card is still in the learning/relearning phase; button intervals are set by minute-level steps and it has not yet entered the FSRS memory model.</div>`;
  }

  html += `<div class="fsrs-section-label">How the parameters plug into the formulas</div>`;
  html += `<div class="fsrs-formula">
    <b>1. Retrievability</b> R = (1 + 19/81 · t/S)<sup>−0.5</sup>, t=${f.elapsed_days}, S=${S != null ? S.toFixed(1) : '—'} → R=${R != null ? (R * 100).toFixed(1) + '%' : '—'}<br>
    <b>2. Correct →</b> stability grows, <code>the longer you wait (lower R) and the lower the difficulty, the bigger the boost</code>; Hard is discounted, Easy gets a bonus.<br>
    <b>3. Wrong (Again) →</b> stability drops gently (not halved), difficulty rises.<br>
    <b>4. Next interval</b> = the number of days for R to decay to the target ${Math.round(f.desired_retention * 100)}% ≈ the new S.<br>
    <b>5. Difficulty</b> reverts toward the mean every time → never gets permanently stuck (no ease hell).
  </div>`;
  html += `<div class="fsrs-note">Intervals are forced monotonic: Again < Hard ≤ Good < Easy. Press Shift+S or Esc to close.</div>`;

  body.innerHTML = html;
}

document.addEventListener('keydown', async e => {
  const inInput = _isEditableFocusTarget(document.activeElement);

  // Book reader (#836): ←/→ turn the page. Guarded on the reader actually
  // being open — the jump-to-page box is an input, so `inInput` keeps arrow
  // keys working normally inside it.
  if (_currentView === 'books' && _bookState.bookId && !inInput &&
      !e.metaKey && !e.ctrlKey && !e.altKey &&
      (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
    e.preventDefault();
    turnBookPage(e.key === 'ArrowRight' ? 1 : -1);
    return;
  }

  if (e.key === 'Escape') {
    if (_fsrsInspectorOpen()) {
      e.preventDefault();
      closeFsrsInspector();
      return;
    }
    const sessOverlay = document.getElementById('session-summary-overlay');
    if (sessOverlay && sessOverlay.style.display !== 'none') {
      e.preventDefault();
      closeSessionSummary();
      return;
    }
    const kahnemanOverlay = document.getElementById('kahneman-examples-overlay');
    if (kahnemanOverlay && kahnemanOverlay.style.display !== 'none') {
      e.preventDefault();
      closeKahnemanExamples();
      return;
    }
    const storyOverlay = document.getElementById('story-modal-overlay');
    if (storyOverlay && storyOverlay.style.display !== 'none') {
      e.preventDefault();
      closeStoryModal();
      return;
    }
    const reasoningModal = document.getElementById('reasoning-modal');
    if (reasoningModal && reasoningModal.style.display !== 'none') {
      e.preventDefault();
      closeReasoning();
      return;
    }
    const logsOverlay = document.getElementById('logs-modal-overlay');
    if (logsOverlay && logsOverlay.style.display !== 'none') {
      e.preventDefault();
      closeLogsViewer();
      return;
    }
    // Same for the sentence-question modal (#853) — its input holds focus too,
    // so it has to be handled before the blur branch below.
    const sqModal = document.getElementById('sentence-question-modal');
    if (sqModal && sqModal.style.display !== 'none') {
      e.preventDefault();
      closeSentenceQuestionModal();
      return;
    }
    // Esc closes the add-word modal (its input has focus, so this must come
    // before the blur branch below). Running jobs keep going server-side.
    const addWordModal = document.getElementById('add-word-modal');
    if (addWordModal && addWordModal.style.display !== 'none') {
      e.preventDefault();
      closeAddWordModal();
      return;
    }
    // Blur input fields in review view so space bar can flip the card
    if (inInput) {
      const reviewView = document.getElementById('view-review');
      if (reviewView && reviewView.style.display !== 'none') {
        document.activeElement.blur();
        return;
      }
    }
  }

  if (!inInput) {
    const storyOverlay = document.getElementById('story-modal-overlay');
    if (storyOverlay && storyOverlay.style.display !== 'none' && !e.metaKey && !e.ctrlKey && !e.altKey) {
      if (e.key === _key('story-play')) { e.preventDefault(); toggleFullStory(); return; }
      if (e.key === _key('story-prev')) { e.preventDefault(); storySkipPrev(); return; }
      if (e.key === _key('story-repeat')) { e.preventDefault(); storyRepeat(); return; }
      if (e.key === _key('story-next')) { e.preventDefault(); storySkipNext(); return; }
    }
  }

  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
    const editModal = document.getElementById('edit-modal');
    if (editModal && editModal.style.display !== 'none') {
      e.preventDefault();
      saveEditCard();
      return;
    }
  }

  if (e.key === _key('restart-server') && !e.ctrlKey && !e.metaKey) {
    if (!inInput) { e.preventDefault(); _restartServer(); }
    return;
  }

  // Stars/unstars the sentence on the current card (#692)
  if (e.key === _key('star-sentence') && !e.ctrlKey && !e.metaKey) {
    if (!inInput) { e.preventDefault(); toggleSentenceStar(); }
    return;
  }

  // Flags/unflags the sentence on the current card (#854, mirror of star-sentence)
  if (e.key === _key('flag-sentence') && !e.ctrlKey && !e.metaKey) {
    if (!inInput) { e.preventDefault(); toggleSentenceFlag(); }
    return;
  }

  // Cmd+A (Ctrl+A on Windows/Linux) opens (or closes) the add-word modal (#788).
  // Use e.code, not e.key: with Cmd held macOS may report a different e.key.
  // Inside inputs we let the browser's select-all through untouched.
  if (e.code === 'KeyA' && (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey) {
    if (!inInput) { e.preventDefault(); toggleAddWordModal(); }
    return;
  }

  // Optional single-key binding for the same action (#927). Unbound by default,
  // so this branch is inert until the user assigns a key in Settings — ⌘A above
  // stays the always-available way in.
  if (_key('add-word') != null && e.key === _key('add-word') && !e.ctrlKey && !e.metaKey) {
    if (!inInput) { e.preventDefault(); toggleAddWordModal(); }
    return;
  }

  // Toggles the FSRS scheduler inspector
  if (e.key === _key('fsrs-inspector') && !e.ctrlKey && !e.metaKey) {
    if (!inInput) { e.preventDefault(); toggleFsrsInspector(); }
    return;
  }

  // Alt+L (Option+L) toggles the logs viewer. Use e.code, not e.key: on macOS
  // Option+L produces the character '¬', so e.key wouldn't be 'L'.
  if (e.code === 'KeyL' && e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
    if (!inInput) {
      e.preventDefault();
      const overlay = document.getElementById('logs-modal-overlay');
      if (overlay && overlay.style.display !== 'none') closeLogsViewer();
      else openLogsViewer();
    }
    return;
  }

  // Enter in word-detail → back to review (if opened from review)
  if (e.key === 'Enter' && !e.metaKey && !e.ctrlKey && !e.altKey && !inInput && !_hasOpenModal()) {
    if (document.getElementById('view-word-detail')?.style.display !== 'none' && _prevView === 'review') {
      e.preventDefault();
      goBack();
      return;
    }
  }

  if (!inInput && !_hasOpenModal()) {
    const code = e.code;

    if ((e.metaKey || e.ctrlKey) && code === 'KeyI' && !e.altKey) {
      e.preventDefault();
      openImportModal();
      return;
    }

    // In the review view, let configured shortcut keys fall through to the
    // review handler below instead of firing global nav (Back/Browse/Add Card).
    // Only actions whose scope actually includes 'review' count here — home/
    // word-detail/story bindings must not block review's own shortcuts, and
    // the global-nav actions themselves are excluded (checking a key against
    // its own action would trivially always match).
    const _reviewActive = document.getElementById('view-review')?.style.display !== 'none';
    const _reviewBoundKeys = new Set(
      KEYMAP_ACTIONS
        .filter(a => a.scope !== 'global' && _scopeSet(a.scope).has('review'))
        .map(a => _keymap[a.id])
        .filter(k => k != null)
    );
    const _mappedInReview = _reviewActive && _reviewBoundKeys.has(e.key);
    if (!e.metaKey && !e.ctrlKey && !e.altKey && !_mappedInReview) {
      if (e.key === _key('nav-back')) {
        e.preventDefault();
        goBack();
        return;
      }
      if (e.key === _key('nav-browse')) {
        e.preventDefault();
        openBrowse();
        return;
      }
      if (e.key === _key('nav-add-card')) {
        e.preventDefault();
        openQuickAddCard();
        return;
      }
      // On the home (decks) view: home-listening → All deck Listening, home-creating → All deck Creating.
      const _decksActive = document.getElementById('view-decks')?.style.display !== 'none';
      if (_decksActive) {
        if (e.key === _key('home-listening')) {
          e.preventDefault();
          _startAllDeckCategory('listening');
          return;
        }
        if (e.key === _key('home-creating')) {
          e.preventDefault();
          _startAllDeckCategory('creating');
          return;
        }
      }
    }
  }

  if (!inInput && e.code === 'Space' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    const reviewView = document.getElementById('view-review');
    if (reviewView && reviewView.style.display !== 'none') {
      const backVisible = document.getElementById('side-back')?.style.display === 'flex';
      if (!backVisible) { e.preventDefault(); revealAnswer(); return; }
    }
  }

  if (inInput || e.ctrlKey || e.metaKey || e.altKey) return;


  const _toggleAndScroll = (bodyId, containerId, block = 'nearest') => {
    toggleSection(bodyId);
    if (document.getElementById(bodyId)?.style.display !== 'none')
      document.getElementById(containerId)?.scrollIntoView({ behavior: 'smooth', block });
  };

  // The 'word analysis' key serves whichever renderer owns that panel slot for
  // the current entry: Word Analysis (zh) or Etymology (Romance) — #906.
  const _toggleAnalysisSlot = (prefix) => {
    const id = document.getElementById(prefix + 'word-analysis-section')?.innerHTML
      ? prefix + 'word-analysis-section'
      : prefix + 'etymology-section';
    _toggleAndScroll(id + '-body', id, 'end');
  };

  // Review shortcuts
  const reviewView = document.getElementById('view-review');
  if (reviewView && reviewView.style.display !== 'none') {
    const backVisible = document.getElementById('side-back')?.style.display === 'flex';
    // Note: 'restart-server' (default 'R') is handled by the top-level branch
    // above, which always intercepts and returns before this block runs —
    // branch order is unchanged from before #856, just now keymap-driven.
    if (e.key === _key('replay')) {
      e.preventDefault(); playSentence();
    } else if (e.key === _key('pinyin')) {
      e.preventDefault(); togglePinyin();
    } else if (e.key === _key('translation')) {
      e.preventDefault();
      // Front side of a creating-mode card: toggle the front translation hint
      // instead of the back-side sentence-fr/sentence-de pair (#515).
      if (!backVisible && category === 'creating') toggleCreatingFrontTranslation();
      else toggleTranslation();
    } else if (e.key === _key('worddef')) {
      e.preventDefault(); toggleWordDef();
    } else if (e.key === _key('reveal')) {
      e.preventDefault(); if (!backVisible) revealAnswer();
    } else if (['1','2','3','4'].includes(e.key) && backVisible) {
      e.preventDefault();
      const btns = document.querySelectorAll('.r-btn');
      if (btns.length && !btns[0].disabled) rate(Number(e.key));
    } else if (e.key === _key('new-sentence') && !backVisible) {
      // New sentence: regenerate a fresh sentence and requeue this card (front only)
      const nsBtn = document.getElementById('new-sentence-btn');
      if (nsBtn && nsBtn.offsetParent !== null && !nsBtn.disabled) {
        e.preventDefault(); requeueNewSentence();
      }
    } else if (e.key === _key('undo')) {
      const undoBtn = document.getElementById('undo-btn');
      if (undoBtn && !undoBtn.disabled) { e.preventDefault(); undoReview(); }
    } else if (backVisible && e.key === _key('examples')) {
      e.preventDefault(); _toggleAndScroll('examples-section-body', 'examples-section');
    } else if (backVisible && e.key === _key('notes')) {
      e.preventDefault(); _toggleAndScroll('notes-section-body', 'notes-section');
    } else if (backVisible && e.key === _key('word-analysis')) {
      e.preventDefault(); _toggleAnalysisSlot('');
    } else if (e.key === _key('hint-minus')) {
      e.preventDefault(); _adjustListenHintSlider(-1);
    } else if (e.key === _key('hint-plus')) {
      e.preventDefault(); _adjustListenHintSlider(1);
    } else if (e.key === _key('story-modal')) {
      e.preventDefault();
      const _storyOpen = document.getElementById('story-modal-overlay')?.style.display !== 'none';
      if (_storyOpen) closeStoryModal(); else openStoryModal();
    } else if (e.key === _key('suspend-reading')) {
      e.preventDefault(); _toggleSuspendCat('reading');
    } else if (e.key === _key('suspend-listening')) {
      e.preventDefault(); _toggleSuspendCat('listening');
    } else if (e.key === _key('suspend-creating')) {
      e.preventDefault(); _toggleSuspendCat('creating');
    } else if (e.key === _key('regen-all')) {
      e.preventDefault(); regenAllFieldsFromReview();
    } else if (e.key === _key('delete-card') || e.key === _key('delete-card-alt')) {
      e.preventDefault();
      reviewCardAction('delete');
    } else if (e.key === _key('leech')) {
      e.preventDefault();
      reviewCardAction('leech');
    } else if (e.key === _key('deck-options')) {
      e.preventDefault();
      if (deckId) openOptions(deckId);
    } else if (e.key === _key('reasoning')) {
      e.preventDefault();
      // Kahneman cards keep g for the reasoning popup; everything else uses g to
      // flip the news-flow display language (original DE ↔ Chinese, issue #452).
      const _lampVisible = document.getElementById('sentence-reasoning-btn')?.style.display !== 'none'
        && document.getElementById('sentence-concept-row')?.style.display !== 'none';
      if (_lampVisible) {
        const _rOpen = document.getElementById('reasoning-modal')?.style.display !== 'none';
        if (_rOpen) closeReasoning(); else openReasoning();
      } else {
        toggleNewsflowLang();
      }
    }
    return;
  }

  // Word-detail shortcuts
  const wdView = document.getElementById('view-word-detail');
  if (wdView && wdView.style.display !== 'none') {
    if (e.key === _key('examples')) {
      e.preventDefault(); _toggleAndScroll('wd-examples-section-body', 'wd-examples-section');
    } else if (e.key === _key('notes')) {
      e.preventDefault(); _toggleAndScroll('wd-notes-section-body', 'wd-notes-section');
    } else if (e.key === _key('word-analysis')) {
      e.preventDefault(); _toggleAnalysisSlot('wd-');
    } else if (e.key === _key('regen-all')) {
      e.preventDefault(); if (_currentWordId) regenAllFields(_currentWordId);
    } else if (e.key === _key('relations')) {
      e.preventDefault(); _toggleAndScroll('wd-relations-body', 'wd-relations-section');
    }
  }
});

// ── Word-detail deck picker ───────────────────────────────────────────────────

let _wdPickerActiveInput = null;
let _wdPickerActiveIdx = -1;
let _wdDeckSuggestions = []; // [{path, id}]

function _wdBuildSuggestions() {
  const result = [];
  function walk(nodes, prefix) {
    for (const d of nodes) {
      if (d.virtual || d.category) { if (d.children) walk(d.children, prefix); continue; }
      const path = prefix ? `${prefix}::${d.name}` : d.name;
      result.push({ path, id: d.id });
      if (d.children) walk(d.children, path);
    }
  }
  walk(_browseDeckTree, '');
  return result;
}

function _wdRenderDropdown(suggestions, query) {
  const dd = document.getElementById('wd-deck-picker-dd');
  if (!dd) return;
  const isNew = !!query && !suggestions.some(s => s.path.toLowerCase() === query.toLowerCase());
  _wdPickerActiveIdx = -1;
  let html = suggestions.map((s, i) =>
    `<div class="deck-picker-option" data-idx="${i}" onclick="wdPickerSelect('${s.path.replace(/'/g, "\\'")}',${s.id})">${_deckPathHtml(s.path)}</div>`
  ).join('');
  if (!html && !isNew) html = '<div class="deck-picker-empty">No existing decks</div>';
  if (isNew && query) {
    html += `<div class="deck-picker-create" onclick="wdPickerSelect('${query.replace(/'/g, "\\'")}',null)">+ Create ${_deckPathHtml(query)}</div>`;
  }
  dd.innerHTML = html;
  _wdPositionDropdown();
  dd.style.display = '';
}

function _wdPositionDropdown() {
  const inp = _wdPickerActiveInput;
  const dd = document.getElementById('wd-deck-picker-dd');
  if (!inp || !dd) return;
  const r = inp.getBoundingClientRect();
  dd.style.width = r.width + 'px';
  dd.style.left = r.left + 'px';
  const ddH = Math.min(220, dd.scrollHeight || 220);
  if (r.bottom + ddH + 4 > window.innerHeight && r.top - ddH - 4 > 0) {
    dd.style.bottom = (window.innerHeight - r.top + 4) + 'px';
    dd.style.top = 'auto';
  } else {
    dd.style.top = (r.bottom + 4) + 'px';
    dd.style.bottom = 'auto';
  }
}

function wdPickerOpen(inp) {
  _wdPickerActiveInput = inp;
  _wdDeckSuggestions = _wdBuildSuggestions();
  const q = inp.value.trim();
  const filtered = _wdDeckSuggestions.filter(s => !q || s.path.toLowerCase().includes(q.toLowerCase()));
  _wdRenderDropdown(filtered, q);
}

function wdPickerFilter(inp) {
  _wdPickerActiveInput = inp;
  if (!_wdDeckSuggestions.length) _wdDeckSuggestions = _wdBuildSuggestions();
  const q = inp.value.trim();
  const filtered = _wdDeckSuggestions.filter(s => !q || s.path.toLowerCase().includes(q.toLowerCase()));
  _wdRenderDropdown(filtered, q);
}

function wdPickerSelect(path, id) {
  if (_wdPickerActiveInput) _wdPickerActiveInput.value = path;
  if (id !== null) _wdPickerActiveInput.dataset.deckId = id;
  else delete _wdPickerActiveInput.dataset.deckId;
  document.getElementById('wd-deck-picker-dd').style.display = 'none';
}

function wdPickerClose() {
  const dd = document.getElementById('wd-deck-picker-dd');
  if (dd) dd.style.display = 'none';
  _wdPickerActiveInput = null;
}

function wdPickerKey(e, inp) {
  const dd = document.getElementById('wd-deck-picker-dd');
  if (!dd || dd.style.display === 'none') {
    if (e.key === 'ArrowDown') { e.preventDefault(); wdPickerOpen(inp); }
    return;
  }
  const opts = dd.querySelectorAll('.deck-picker-option, .deck-picker-create');
  if (e.key === 'Escape') { dd.style.display = 'none'; return; }
  if (e.key === 'ArrowDown') { e.preventDefault(); _wdPickerActiveIdx = Math.min(_wdPickerActiveIdx + 1, opts.length - 1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); _wdPickerActiveIdx = Math.max(_wdPickerActiveIdx - 1, -1); }
  else if (e.key === 'Enter' && _wdPickerActiveIdx >= 0) { e.preventDefault(); opts[_wdPickerActiveIdx].click(); return; }
  else { return; }
  opts.forEach((o, i) => o.classList.toggle('active', i === _wdPickerActiveIdx));
  if (_wdPickerActiveIdx >= 0) opts[_wdPickerActiveIdx].scrollIntoView({ block: 'nearest' });
}

async function _wdResolveDeck(path) {
  // Try to find existing deck by path match
  if (!_wdDeckSuggestions.length) _wdDeckSuggestions = _wdBuildSuggestions();
  const found = _wdDeckSuggestions.find(s => s.path.toLowerCase() === path.toLowerCase());
  if (found) return found.id;
  // Create new deck via API (supports :: hierarchy)
  const deck = await api('POST', `/api/decks?name=${encodeURIComponent(path)}`);
  // Refresh deck data so future operations work
  const deckTree = await api('GET', '/api/decks');
  _browseDecks = _flattenDecks(deckTree);
  const allRoot = deckTree.find(d => d.virtual && d.id !== 'unfinished');
  _browseDeckTree = allRoot ? (allRoot.children || []) : deckTree.filter(d => !d.virtual);
  _wdDeckSuggestions = _wdBuildSuggestions();
  return deck.id;
}

document.addEventListener('click', e => {
  const dd = document.getElementById('wd-deck-picker-dd');
  if (!dd || dd.style.display === 'none') return;
  if (_wdPickerActiveInput && !_wdPickerActiveInput.contains(e.target) && !dd.contains(e.target)) {
    dd.style.display = 'none';
  }
});

// ── Deck picker ───────────────────────────────────────────────────────────────

let _deckPickerActiveIdx = -1;
let _deckBPickerActiveIdx = -1;
let _deckBPath = null;

function _deckPathHtml(path) {
  return path.split('::').map(s => `<span>${s}</span>`).join('<span class="deck-picker-sep"> :: </span>');
}

function _renderDeckDropdown(suggestions, query) {
  const dd = document.getElementById('deck-picker-dropdown');
  if (!dd) return;
  const isNew = !!query && !suggestions.some(s => s.toLowerCase() === query.toLowerCase());
  document.getElementById('deck-picker-new-badge').style.display = (isNew && query) ? '' : 'none';
  _deckPickerActiveIdx = -1;

  let html = suggestions.map((s, i) =>
    `<div class="deck-picker-option" data-idx="${i}" onclick="deckPickerSelect('${s.replace(/'/g, "\\'")}')">${_deckPathHtml(s)}</div>`
  ).join('');

  if (!html && !isNew) html = '<div class="deck-picker-empty">No existing decks</div>';

  if (isNew && query) {
    html += `<div class="deck-picker-create" onclick="deckPickerSelect('${query.replace(/'/g, "\\'")}')">+ Create ${_deckPathHtml(query)}</div>`;
  }

  dd.innerHTML = html;
  const show = !!(suggestions.length || isNew || !query);
  dd.style.display = show ? 'block' : 'none';
  if (show) _positionDeckDropdown();
}

function _positionDeckDropdown() {
  const input = document.getElementById('import-deck-path');
  const dd = document.getElementById('deck-picker-dropdown');
  if (!input || !dd) return;
  const r = input.getBoundingClientRect();
  const ddH = Math.min(220, dd.scrollHeight);
  const spaceAbove = r.top;
  const spaceBelow = window.innerHeight - r.bottom;
  dd.style.width = r.width + 'px';
  dd.style.left = r.left + 'px';
  if (spaceAbove >= ddH + 8 || spaceAbove > spaceBelow) {
    dd.style.bottom = (window.innerHeight - r.top + 4) + 'px';
    dd.style.top = 'auto';
  } else {
    dd.style.top = (r.bottom + 4) + 'px';
    dd.style.bottom = 'auto';
  }
}

function deckPickerOpen() {
  const q = document.getElementById('import-deck-path').value.trim();
  const filtered = (window._deckSuggestions || []).filter(s => !q || s.toLowerCase().includes(q.toLowerCase()));
  _renderDeckDropdown(filtered, q);
}

function deckPickerFilter() {
  const q = document.getElementById('import-deck-path').value.trim();
  const filtered = (window._deckSuggestions || []).filter(s => !q || s.toLowerCase().includes(q.toLowerCase()));
  _renderDeckDropdown(filtered, q);
}

function deckPickerSelect(path) {
  document.getElementById('import-deck-path').value = path;
  document.getElementById('deck-picker-dropdown').style.display = 'none';
  const isNew = !(window._deckSuggestions || []).some(s => s.toLowerCase() === path.toLowerCase());
  document.getElementById('deck-picker-new-badge').style.display = (isNew && path) ? '' : 'none';
}

function deckPickerKey(e) {
  const dd = document.getElementById('deck-picker-dropdown');
  if (!dd) return;
  if (dd.style.display === 'none') {
    if (e.key === 'ArrowDown') { e.preventDefault(); deckPickerOpen(); }
    return;
  }
  const opts = dd.querySelectorAll('.deck-picker-option, .deck-picker-create');
  if (e.key === 'Escape') { dd.style.display = 'none'; return; }
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    _deckPickerActiveIdx = Math.min(_deckPickerActiveIdx + 1, opts.length - 1);
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    _deckPickerActiveIdx = Math.max(_deckPickerActiveIdx - 1, -1);
  } else if (e.key === 'Enter' && _deckPickerActiveIdx >= 0) {
    e.preventDefault();
    opts[_deckPickerActiveIdx].click();
    return;
  } else { return; }
  opts.forEach((o, i) => o.classList.toggle('active', i === _deckPickerActiveIdx));
  if (_deckPickerActiveIdx >= 0) opts[_deckPickerActiveIdx].scrollIntoView({ block: 'nearest' });
}

document.addEventListener('click', e => {
  const picker = document.getElementById('deck-picker');
  const dd = document.getElementById('deck-picker-dropdown');
  if (picker && dd && !picker.contains(e.target) && !dd.contains(e.target)) {
    dd.style.display = 'none';
  }
  const pickerB = document.getElementById('deck-b-picker');
  const ddB = document.getElementById('deck-b-picker-dropdown');
  if (pickerB && ddB && !pickerB.contains(e.target) && !ddB.contains(e.target)) {
    ddB.style.display = 'none';
  }
});

// ── Deck B picker ─────────────────────────────────────────────────────────────

function _renderDeckBDropdown(suggestions, query) {
  const dd = document.getElementById('deck-b-picker-dropdown');
  if (!dd) return;
  const isNew = !!query && !suggestions.some(s => s.toLowerCase() === query.toLowerCase());
  document.getElementById('deck-b-new-badge').style.display = (isNew && query) ? '' : 'none';
  _deckBPickerActiveIdx = -1;
  let html = suggestions.map((s, i) =>
    `<div class="deck-picker-option" data-idx="${i}" onclick="deckBPickerSelect('${s.replace(/'/g, "\\'")}')">${_deckPathHtml(s)}</div>`
  ).join('');
  if (!html && !isNew) html = '<div class="deck-picker-empty">No existing decks</div>';
  if (isNew && query) {
    html += `<div class="deck-picker-create" onclick="deckBPickerSelect('${query.replace(/'/g, "\\'")}')">+ Create ${_deckPathHtml(query)}</div>`;
  }
  dd.innerHTML = html;
  const show = !!(suggestions.length || isNew || !query);
  dd.style.display = show ? 'block' : 'none';
  if (show) _positionDeckBDropdown();
}

function _positionDeckBDropdown() {
  const input = document.getElementById('import-deck-b-path');
  const dd = document.getElementById('deck-b-picker-dropdown');
  if (!input || !dd) return;
  const r = input.getBoundingClientRect();
  const ddH = Math.min(220, dd.scrollHeight);
  const spaceAbove = r.top;
  const spaceBelow = window.innerHeight - r.bottom;
  dd.style.width = r.width + 'px';
  dd.style.left = r.left + 'px';
  if (spaceAbove >= ddH + 8 || spaceAbove > spaceBelow) {
    dd.style.bottom = (window.innerHeight - r.top + 4) + 'px';
    dd.style.top = 'auto';
  } else {
    dd.style.top = (r.bottom + 4) + 'px';
    dd.style.bottom = 'auto';
  }
}

function deckBPickerOpen() {
  const q = document.getElementById('import-deck-b-path').value.trim();
  const filtered = (window._deckSuggestions || []).filter(s => !q || s.toLowerCase().includes(q.toLowerCase()));
  _renderDeckBDropdown(filtered, q);
}

function deckBPickerFilter() {
  const q = document.getElementById('import-deck-b-path').value.trim();
  const filtered = (window._deckSuggestions || []).filter(s => !q || s.toLowerCase().includes(q.toLowerCase()));
  _renderDeckBDropdown(filtered, q);
}

function deckBPickerSelect(path) {
  document.getElementById('import-deck-b-path').value = path;
  document.getElementById('deck-b-picker-dropdown').style.display = 'none';
  const isNew = !(window._deckSuggestions || []).some(s => s.toLowerCase() === path.toLowerCase());
  document.getElementById('deck-b-new-badge').style.display = (isNew && path) ? '' : 'none';
  _deckBPath = path || null;
  _importRenderTable();
}

function deckBPickerKey(e) {
  const dd = document.getElementById('deck-b-picker-dropdown');
  if (!dd) return;
  if (dd.style.display === 'none') {
    if (e.key === 'ArrowDown') { e.preventDefault(); deckBPickerOpen(); }
    return;
  }
  const opts = dd.querySelectorAll('.deck-picker-option, .deck-picker-create');
  if (e.key === 'Escape') { dd.style.display = 'none'; return; }
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    _deckBPickerActiveIdx = Math.min(_deckBPickerActiveIdx + 1, opts.length - 1);
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    _deckBPickerActiveIdx = Math.max(_deckBPickerActiveIdx - 1, -1);
  } else if (e.key === 'Enter' && _deckBPickerActiveIdx >= 0) {
    e.preventDefault();
    opts[_deckBPickerActiveIdx].click();
    return;
  } else { return; }
  opts.forEach((o, i) => o.classList.toggle('active', i === _deckBPickerActiveIdx));
  if (_deckBPickerActiveIdx >= 0) opts[_deckBPickerActiveIdx].scrollIntoView({ block: 'nearest' });
}

function importApplyDeckB() {
  _deckBPath = document.getElementById('import-deck-b-path').value.trim() || null;
  _importRenderTable();
}

function importToggleDeckB(wordZh) {
  const cfg = _cardConfigs[wordZh] || {};
  const isB = cfg.deck_path === '__deckB__';
  _cardConfigs[wordZh] = { ...cfg, deck_path: isB ? null : '__deckB__' };
  _importRenderTable();
}

// ── UI Click Logger ───────────────────────────────────────────────────────────
const _UI_ACTION_MAP = {
  startReviewMixed:         '开始复习牌组',
  startReviewUnfinished:    '开始复习未完成卡片',
  openBrowse:               '打开浏览',
  openBrowseForDeck:        '浏览牌组卡片',
  openStats:                '打开统计',
  openCostModal:            '打开 API 费用',
  openImportModal:          '打开导入',
  openQuickAddCard:         '快速添加卡片',
  createDeck:               '新建牌组',
  openTrash:                '打开垃圾桶',
  toggleBury:               '切换埋葬',
  toggleDeckAllSuspension:  '切换暂停所有卡片',
  toggleDeckMenu:           '打开牌组菜单',
  renameDeck:               '重命名牌组',
  deleteDeck:               '删除牌组',
  clearDeckCards:           '清空牌组卡片',
  openOptions:              '打开牌组选项',
  toggleDeck:               '折叠/展开牌组',
  openWordDetail:           '查看词语详情',
  openHanziDetail:          '查看汉字详情',
  onBrowseRowClick:         '点击浏览行',
  openAddToDeckModal:       '添加到牌组',
  setBrowseDeckFilter:      '筛选牌组',
  cardAction:               '卡片操作',
  toggleCardMenu:           '卡片菜单',
  openHanziRegenModal:      '汉字重新生成',
  openWordEditModal:        '编辑词语',
  openHanziEditModal:       '编辑汉字',
  toggleSection:            '折叠/展开区块',
  toggleReviewCat:          '切换复习类别',
  _moveCatOrder:            '调整类别顺序',
  confirmPromptModal:       '确认对话框',
  cancelPromptModal:        '取消对话框',
  closeDeckMenu:            '关闭牌组菜单',
};

document.addEventListener('click', function(e) {
  const el = e.target.closest('[onclick], button, a');
  if (!el) return;

  const onclickAttr = el.getAttribute('onclick') || '';
  const fnMatch = onclickAttr.match(/^(?:event\.stopPropagation\(\);)?(\w+)/);
  const fnName = fnMatch ? fnMatch[1] : '';

  const label = _UI_ACTION_MAP[fnName]
    || (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 30)
    || fnName
    || el.tagName;

  const extra = fnName && !_UI_ACTION_MAP[fnName] ? '' : fnName ? ` [${fnName}]` : '';
  const action = `${label}${extra}`;
  fetch('/api/log', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action }),
  }).catch(() => {});
}, true);

// ── Confetti (100% score) ─────────────────────────────────────────────────────
function triggerApplause() {
  const colors = ['#16a34a', '#2563eb', '#d97706', '#dc2626', '#0891b2', '#9333ea'];
  const count = 48;
  for (let i = 0; i < count; i++) {
    const el = document.createElement('div');
    el.className = 'confetti-piece';
    el.style.left = Math.random() * 100 + 'vw';
    el.style.backgroundColor = colors[Math.floor(Math.random() * colors.length)];
    el.style.animationDuration = (1.0 + Math.random() * 1.2) + 's';
    el.style.animationDelay = (Math.random() * 0.4) + 's';
    el.style.borderRadius = Math.random() > 0.5 ? '50%' : '2px';
    document.body.appendChild(el);
    el.addEventListener('animationend', () => el.remove());
  }
}

// ── Server restart (Shift+R, no button) ──────────────────────────────────────
async function _restartServer() {
  try { await fetch('/api/restart', { method: 'POST' }); } catch (_) {}
  const poll = async () => {
    try { const r = await fetch('/api/decks'); if (r.ok) { location.reload(); return; } } catch (_) {}
    setTimeout(poll, 400);
  };
  setTimeout(poll, 600);
}

// ── Running-version badge (issue #450) ──────────────────────────────────────
// Bottom-right corner: branch@commit · deploy time. Hover shows the commit
// message (title); a click/tap toggles it inline for mobile.
// Deploy time is always shown in Daniel's own timezone (#706), same rule as
// podcast episode dates (#532) — the server runs on Asia/Shanghai and the
// phone travels, so neither of those is the timezone the badge is read in.
// Intl does the conversion; hand-rolling it from getHours() would just read
// back whatever timezone the browser happens to be in.
function _formatBerlin(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return new Intl.DateTimeFormat('de-DE', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
    timeZone: 'Europe/Berlin',
  }).format(d);
}

async function _loadVersionBadge() {
  try {
    const v = await api('GET', '/api/version');
    const el = document.getElementById('version-badge');
    if (!el || !v.commit) return;
    const when = v.deployed_at ? _formatBerlin(v.deployed_at) : '';
    const short = `${v.branch}@${v.commit}${when ? ` · ${when}` : ''}`;
    el.textContent = short;
    el.title = v.message ? `${v.message}\n(deployed ${when || v.deployed_at})` : '';
    el.onclick = () => {
      const expanded = el.classList.toggle('expanded');
      el.textContent = expanded && v.message ? `${short} — ${v.message}` : short;
    };
    el.style.display = 'block';
  } catch (e) { /* badge is best-effort — never break the app over it */ }
}

// ── Boot ─────────────────────────────────────────────────────────────────────
// Index 0 of the nav history (#1009). Without a state object on the entry the
// app starts from, the first popstate would arrive with `null` and we could
// not tell "back to the start" from "some other page's entry".
history.replaceState({ navIdx: 0 }, '');
_updateBackBtn();

// Hash direct-link (#480, feed layer #502, generalized to video/article #653):
// if the URL already points at a knowledge item (link from an email/Signal
// message) or feed, open it straight away instead of the deck list. The
// feed-id form must be checked first — it also matches the plain item-detail
// form's prefix. Both the legacy #podcast-* and new #knowledge-* hash shapes
// are recognized (see _openKnowledgeFromHash).
if (/^#(?:podcast|knowledge)-feed-\d+$/.test(location.hash)
    || /^#(?:podcast|knowledge)-\d+$/.test(location.hash)
    || /^#knowledge-(?:podcast|video|reel|article|newsletter)$/.test(location.hash)) {
  _openKnowledgeFromHash();
  // Consume the hash: a direct link is a one-shot instruction to open one
  // thing, not a sticky location. Leaving it in the address bar made every
  // later reload land back in the knowledge base instead of the home screen
  // (#792) — the whole reason nothing in the app writes a hash any more.
  history.replaceState(history.state, '', location.pathname + location.search);
} else {
  loadDecks();
}
_loadVersionBadge();
_startTasksPolling();
_syncShortcutTitles();


// ===== Home calendar heatmap (issue #307) — inlined here to dodge index.html caching =====
// ============================================================================
// Home-page calendar heatmap (issue #307)
// Shows per-day study stats above the deck list. Four metrics (retention /
// cards / time / future), two display modes (heatmap / graph), hover for a
// day summary, click a day for a full breakdown.
// ============================================================================

let _hcalData = null;          // cached /api/calendar-stats response
let _hcalLoading = false;
let _hcalMetric = localStorage.getItem('calMetric') || 'retention';
let _hcalMode   = localStorage.getItem('calMode')   || 'heatmap';
let _hcalSelectedDay = null;   // 'YYYY-MM-DD' currently shown in the detail panel

const _HCAL_ALL_CATS = [
  { key: 'listening', zh: '听', en: 'Listening' },
  { key: 'reading',   zh: '读', en: 'Reading'   },
  { key: 'creating',  zh: '创', en: 'Creating'  },
];
// Rows for categories still switched on (#869) — a permanently-zero row for a
// disabled category is noise, not information.
function _hcalCats() {
  const on = _enabledCategories();
  return _HCAL_ALL_CATS.filter(c => on[c.key]);
}
const _HCAL_METRICS = [
  { key: 'retention', label: 'Retention' },
  { key: 'cards',     label: 'Cards'     },
  { key: 'time',      label: 'Time'      },
  { key: 'future',    label: 'Scheduled' },
];

// ── Date helpers (local, no timezone surprises) ─────────────────────────────
function _hcalYmd(d) {
  return d.getFullYear() + '-' +
    String(d.getMonth() + 1).padStart(2, '0') + '-' +
    String(d.getDate()).padStart(2, '0');
}
function _hcalParse(s) { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); }
function _hcalAddDays(d, n) { const r = new Date(d); r.setDate(r.getDate() + n); return r; }
function _hcalWeekday(d) { return (d.getDay() + 6) % 7; }   // Mon=0 … Sun=6

// ── Entry point (called from renderDecks) ───────────────────────────────────
function initHomeCalendar() {
  const el = document.getElementById('home-calendar');
  if (!el) return;
  if (_hcalData) { _hcalRender(); return; }
  if (_hcalLoading) return;
  _hcalLoading = true;
  el.innerHTML = '<div class="hcal-loading">Loading calendar…</div>';
  api('GET', '/api/calendar-stats?days=365')
    .then(d => { _hcalData = d; _hcalLoading = false; _hcalRender(); })
    .catch(err => {
      _hcalLoading = false;
      el.innerHTML = `<div class="hcal-loading">Calendar unavailable — ${
        (err && err.message) || 'failed to load stats'}. Restart the server?</div>`;
    });
}

// Force a refetch (e.g. after reviewing). Safe to call even if not mounted.
function invalidateHomeCalendar() { _hcalData = null; }

// ── Top-level render ────────────────────────────────────────────────────────
function _hcalRender() {
  const el = document.getElementById('home-calendar');
  if (!el || !_hcalData) return;

  const metricBtns = _HCAL_METRICS.map(m =>
    `<button class="hcal-seg-btn ${m.key === _hcalMetric ? 'active' : ''}"
             onclick="hcalSetMetric('${m.key}')">${m.label}</button>`).join('');
  const modeBtns = [['heatmap', 'Heatmap'], ['graph', 'Graph']].map(([k, lbl]) =>
    `<button class="hcal-seg-btn ${k === _hcalMode ? 'active' : ''}"
             onclick="hcalSetMode('${k}')">${lbl}</button>`).join('');

  el.innerHTML = `
    <div class="hcal-controls">
      <div class="hcal-seg hcal-seg-metric">${metricBtns}</div>
      <div class="hcal-seg hcal-seg-mode">${modeBtns}</div>
    </div>
    <div class="hcal-body">${_hcalMode === 'heatmap' ? _hcalRenderHeatmap() : _hcalRenderGraph()}</div>
    <div class="hcal-detail" id="hcal-detail">${_hcalRenderDetail()}</div>`;

  // Past metrics: newest data is at the right edge → scroll there. Future: keep left (today first).
  const wrap = el.querySelector('.hcal-heatmap-wrap, .hcal-graph-wrap');
  if (wrap) wrap.scrollLeft = (_hcalMetric === 'future') ? 0 : wrap.scrollWidth;
}

function hcalSetMetric(m) {
  _hcalMetric = m; localStorage.setItem('calMetric', m);
  _hcalSelectedDay = null; _hcalRender();
}
function hcalSetMode(m) {
  _hcalMode = m; localStorage.setItem('calMode', m); _hcalRender();
}

// ── Per-day value extraction ────────────────────────────────────────────────
// Returns {value, has} where value is null when there's nothing to show.
function _hcalDayValue(date) {
  if (_hcalMetric === 'future') {
    const f = _hcalData.future[date];
    return { value: f ? f.total : null, has: !!f };
  }
  const d = _hcalData.by_date[date];
  if (!d) return { value: null, has: false };
  if (_hcalMetric === 'retention') {
    // "Learned cards only" — count reviews in the review phase (state='review'),
    // matching the daily badge / Anki true retention. Learning steps excluded.
    const rv = d.review;
    return { value: rv.total > 0 ? rv.correct / rv.total : null, has: rv.total > 0 };
  }
  if (_hcalMetric === 'cards') {
    return { value: d.cards || 0, has: (d.cards || 0) > 0 };
  }
  if (_hcalMetric === 'time') {
    return { value: d.duration_ms || 0, has: (d.duration_ms || 0) > 0 };
  }
  return { value: null, has: false };
}

// Colour for a heatmap cell given its value and the window max.
function _hcalColor(value, has, max) {
  if (!has || value == null) return 'var(--hcal-empty)';
  if (_hcalMetric === 'retention') {
    // Almost all of Daniel's days fall in 80–90%, so concentrate the entire
    // colour contrast there: 80%→90% sweeps the full red→amber→green spectrum,
    // while everything below 80% / above 90% is compressed into a tiny near-red /
    // near-green band (so those extremes look "almost the same", as requested).
    let h;
    if (value >= 0.90) {
      h = 110 + Math.min(1, (value - 0.90) / 0.10) * 10;   // 110→120 (near-green)
    } else if (value <= 0.80) {
      h = Math.max(0, (value - 0.50) / 0.30) * 10;          // 0→10  (near-red)
    } else {
      h = 10 + ((value - 0.80) / 0.10) * 100;               // 10→110 (full sweep)
    }
    const l = 36 + Math.round(h / 120 * 16);                // darker red → brighter green
    return `hsl(${Math.round(h)}, 70%, ${l}%)`;
  }
  // count-like metrics: 4 intensity buckets
  const palettes = {
    cards:  ['#9be9a8', '#40c463', '#30a14e', '#216e39'],
    time:   ['#9be9a8', '#40c463', '#30a14e', '#216e39'],
    future: ['#b3c7ff', '#7aa2ff', '#4d7cff', '#2952cc'],
  };
  const pal = palettes[_hcalMetric] || palettes.cards;
  if (max <= 0) return pal[0];
  const frac = value / max;
  const idx = value <= 0 ? -1 : Math.min(pal.length - 1, Math.floor(frac * pal.length - 1e-9));
  return idx < 0 ? 'var(--hcal-empty)' : pal[Math.max(0, idx)];
}

// Window of dates for the current metric.
function _hcalWindow() {
  const today = _hcalParse(_hcalData.today);
  if (_hcalMetric === 'future') {
    return { start: today, end: _hcalAddDays(today, 90) };
  }
  return { start: _hcalAddDays(today, -364), end: today };
}

// ── Heatmap rendering ───────────────────────────────────────────────────────
function _hcalRenderHeatmap() {
  const { start, end } = _hcalWindow();

  // Window max for count metrics
  let max = 0;
  for (let d = new Date(start); d <= end; d = _hcalAddDays(d, 1)) {
    const { value, has } = _hcalDayValue(_hcalYmd(d));
    if (has && _hcalMetric !== 'retention') max = Math.max(max, value);
  }

  // Build padded day list, then chunk into weekly columns
  const cells = [];
  for (let i = 0; i < _hcalWeekday(start); i++) cells.push(null);
  for (let d = new Date(start); d <= end; d = _hcalAddDays(d, 1)) cells.push(_hcalYmd(d));
  const weeks = [];
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));

  const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  let lastMonth = -1;
  const monthLabels = weeks.map(w => {
    const firstReal = w.find(c => c);
    if (!firstReal) return '<span class="hcal-month"></span>';
    const m = _hcalParse(firstReal).getMonth();
    const dom = _hcalParse(firstReal).getDate();
    if (m !== lastMonth && dom <= 7) { lastMonth = m; return `<span class="hcal-month">${MONTHS[m]}</span>`; }
    return '<span class="hcal-month"></span>';
  }).join('');

  const weekCols = weeks.map(w => {
    const days = w.map(date => {
      if (!date) return '<span class="hcal-day hcal-pad"></span>';
      const { value, has } = _hcalDayValue(date);
      const color = _hcalColor(value, has, max);
      const sel = date === _hcalSelectedDay ? ' hcal-sel' : '';
      return `<span class="hcal-day${sel}" style="background:${color}"
                onmouseenter="hcalShowTip(event,'${date}')" onmouseleave="hcalHideTip()"
                onclick="hcalSelectDay('${date}')"></span>`;
    }).join('');
    return `<span class="hcal-week">${days}</span>`;
  }).join('');

  return `
    <div class="hcal-heatmap-wrap">
      <div class="hcal-months">${monthLabels}</div>
      <div class="hcal-grid">${weekCols}</div>
      ${_hcalLegend(max)}
    </div>`;
}

function _hcalLegend(max) {
  if (_hcalMetric === 'retention') {
    return `<div class="hcal-legend">
      <span>≤80%</span>
      <span class="hcal-leg-sw" style="background:${_hcalColor(0.80, true, 1)}"></span>
      <span class="hcal-leg-sw" style="background:${_hcalColor(0.825, true, 1)}"></span>
      <span class="hcal-leg-sw" style="background:${_hcalColor(0.85, true, 1)}"></span>
      <span class="hcal-leg-sw" style="background:${_hcalColor(0.875, true, 1)}"></span>
      <span class="hcal-leg-sw" style="background:${_hcalColor(0.90, true, 1)}"></span>
      <span>90%+</span></div>`;
  }
  const unit = _hcalMetric === 'time' ? 'min' : _hcalMetric === 'future' ? 'due' : 'cards';
  return `<div class="hcal-legend"><span>less</span>
    <span class="hcal-leg-sw" style="background:${_hcalColor(max * 0.1, true, max)}"></span>
    <span class="hcal-leg-sw" style="background:${_hcalColor(max * 0.4, true, max)}"></span>
    <span class="hcal-leg-sw" style="background:${_hcalColor(max * 0.7, true, max)}"></span>
    <span class="hcal-leg-sw" style="background:${_hcalColor(max, true, max)}"></span>
    <span>more (${unit})</span></div>`;
}

// ── Graph rendering (vertical bars, x = time along the long axis) ────────────
function _hcalRenderGraph() {
  const { end } = _hcalWindow();
  const span = _hcalMetric === 'future' ? 45 : 45;
  const start = _hcalMetric === 'future' ? _hcalParse(_hcalData.today) : _hcalAddDays(end, -(span - 1));
  const last  = _hcalMetric === 'future' ? _hcalAddDays(start, span - 1) : end;

  const items = [];
  let max = 0;
  for (let d = new Date(start); d <= last; d = _hcalAddDays(d, 1)) {
    const date = _hcalYmd(d);
    const { value, has } = _hcalDayValue(date);
    let v = 0;
    if (has) v = _hcalMetric === 'retention' ? value : value;
    if (_hcalMetric !== 'retention') max = Math.max(max, v);
    items.push({ date, value: v, has });
  }
  if (_hcalMetric === 'retention') max = 1;
  if (max <= 0) max = 1;

  const bars = items.map(it => {
    const h = it.has ? Math.max(2, Math.round(it.value / max * 100)) : 0;
    const color = it.has ? _hcalColor(it.value, it.has,
      _hcalMetric === 'retention' ? 1 : max) : 'var(--hcal-empty)';
    const sel = it.date === _hcalSelectedDay ? ' hcal-sel' : '';
    return `<span class="hcal-bar-col${sel}" onmouseenter="hcalShowTip(event,'${it.date}')"
              onmouseleave="hcalHideTip()" onclick="hcalSelectDay('${it.date}')">
              <span class="hcal-bar" style="height:${h}%;background:${color}"></span>
            </span>`;
  }).join('');

  const fmt = x => `${x.getMonth() + 1}/${x.getDate()}`;
  return `
    <div class="hcal-graph-wrap">
      <div class="hcal-graph">${bars}</div>
      <div class="hcal-graph-axis"><span>${fmt(start)}</span><span>${fmt(last)}</span></div>
    </div>`;
}

// ── Floating tooltip ────────────────────────────────────────────────────────
function _hcalTip() {
  let t = document.getElementById('hcal-tip');
  if (!t) { t = document.createElement('div'); t.id = 'hcal-tip'; t.className = 'hcal-tip'; document.body.appendChild(t); }
  return t;
}
function hcalShowTip(ev, date) {
  const t = _hcalTip();
  t.innerHTML = _hcalTipHtml(date);
  t.style.display = 'block';
  const r = ev.target.getBoundingClientRect();
  const tw = t.offsetWidth, th = t.offsetHeight;
  let left = r.left + r.width / 2 - tw / 2 + window.scrollX;
  left = Math.max(6, Math.min(left, window.innerWidth - tw - 6 + window.scrollX));
  let top = r.top + window.scrollY - th - 8;
  if (top < window.scrollY + 4) top = r.bottom + window.scrollY + 8;
  t.style.left = left + 'px';
  t.style.top = top + 'px';
}
function hcalHideTip() { const t = document.getElementById('hcal-tip'); if (t) t.style.display = 'none'; }

function _hcalFmtTime(ms) {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`;
}
function _hcalFmtRR(c, tot) { return tot > 0 ? Math.round(c / tot * 100) + '%' : '—'; }

function _hcalTipHtml(date) {
  const nice = _hcalParse(date).toLocaleDateString(undefined,
    { weekday: 'short', month: 'short', day: 'numeric' });

  if (_hcalMetric === 'future') {
    const f = _hcalData.future[date];
    if (!f) return `<div class="hcal-tip-date">${nice}</div><div class="hcal-tip-empty">nothing scheduled</div>`;
    const cats = _hcalCats().map(c => `${c.zh} ${f.by_cat[c.key] || 0}`).join(' · ');
    return `<div class="hcal-tip-date">${nice}</div>
            <div class="hcal-tip-big">${f.total} scheduled</div>
            <div class="hcal-tip-cats">${cats}</div>`;
  }

  const d = _hcalData.by_date[date];
  if (!d || d.total === 0) return `<div class="hcal-tip-date">${nice}</div><div class="hcal-tip-empty">no reviews</div>`;

  const head = `<div class="hcal-tip-big">${d.cards} cards · ${_hcalFmtRR(d.review.correct, d.review.total)} retention</div>`;
  const time = d.timed_count > 0
    ? `<div class="hcal-tip-sub">${_hcalFmtTime(d.duration_ms)} total · ${_hcalFmtTime(d.duration_ms / d.timed_count)}/card</div>`
    : '';
  const rows = _hcalCats().filter(c => d.by_cat[c.key]).map(c => {
    const cd = d.by_cat[c.key];
    const ph = `learning ${_hcalFmtRR(cd.learning.correct, cd.learning.total)}`;
    return `<div class="hcal-tip-row"><b>${c.zh}</b> ${cd.cards}c · ${_hcalFmtRR(cd.review.correct, cd.review.total)} <span class="hcal-tip-dim">(${ph})</span></div>`;
  }).join('');
  return `<div class="hcal-tip-date">${nice}</div>${head}${time}<div class="hcal-tip-rows">${rows}</div>`;
}

// ── Click → detail panel ────────────────────────────────────────────────────
function hcalSelectDay(date) {
  _hcalSelectedDay = (_hcalSelectedDay === date) ? null : date;
  _hcalRender();
}

function _hcalRenderDetail() {
  if (!_hcalSelectedDay) return '';
  const date = _hcalSelectedDay;
  const nice = _hcalParse(date).toLocaleDateString(undefined,
    { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });

  if (_hcalMetric === 'future') {
    const f = _hcalData.future[date];
    const body = !f ? '<div class="hcal-tip-empty">Nothing scheduled.</div>'
      : `<table class="hcal-tbl"><tr><th></th><th>Scheduled</th></tr>${
          _hcalCats().map(c => `<tr><td>${c.zh} ${c.en}</td><td>${f.by_cat[c.key] || 0}</td></tr>`).join('')
        }<tr class="hcal-tbl-total"><td>Total</td><td>${f.total}</td></tr></table>`;
    return `<div class="hcal-detail-head">${nice}</div>${body}`;
  }

  const d = _hcalData.by_date[date];
  if (!d || d.total === 0) return `<div class="hcal-detail-head">${nice}</div><div class="hcal-tip-empty">No reviews this day.</div>`;

  const catRows = _hcalCats().map(c => {
    const cd = d.by_cat[c.key];
    if (!cd) return `<tr><td>${c.zh} ${c.en}</td><td>0</td><td>—</td><td>—</td><td>—</td></tr>`;
    const avg = cd.timed_count > 0 ? _hcalFmtTime(cd.duration_ms / cd.timed_count) : '—';
    const tot = cd.timed_count > 0 ? _hcalFmtTime(cd.duration_ms) : '—';
    return `<tr>
      <td>${c.zh} ${c.en}</td>
      <td>${cd.cards}</td>
      <td>${_hcalFmtRR(cd.review.correct, cd.review.total)}</td>
      <td>${_hcalFmtRR(cd.learning.correct, cd.learning.total)}</td>
      <td>${avg} <span class="hcal-tip-dim">/ ${tot}</span></td>
    </tr>`;
  }).join('');

  const totAvg = d.timed_count > 0 ? _hcalFmtTime(d.duration_ms / d.timed_count) : '—';
  const totTot = d.timed_count > 0 ? _hcalFmtTime(d.duration_ms) : '—';
  return `
    <div class="hcal-detail-head">${nice}</div>
    <table class="hcal-tbl">
      <tr><th>Category</th><th>Cards</th><th>Retention</th><th>Learn</th><th>Avg / Total</th></tr>
      ${catRows}
      <tr class="hcal-tbl-total">
        <td>All</td><td>${d.cards}</td><td>${_hcalFmtRR(d.review.correct, d.review.total)}</td>
        <td>${_hcalFmtRR(d.learning.correct, d.learning.total)}</td>
        <td>${totAvg} <span class="hcal-tip-dim">/ ${totTot}</span></td>
      </tr>
    </table>`;
}

// ============================================================================
// Home-page card-evolution chart (issue #321)
// Stacked area chart of card-state counts over time (New / Learning /
// Learnt / Relearn), with Listening / Creating / All views.
// ============================================================================

let _evoData = null;           // cached /api/card-evolution response
let _evoLoading = false;
let _evoView = localStorage.getItem('evoView') || 'all';
let _evoCalc = null;           // per-render geometry cache for tooltips

// Stack order: bottom → top. Colors from the shared colorblind-safe palette.
const _EVO_STATES = [
  { key: 'review',   label: 'Learnt',   color: _STATE_COLOR.review   },
  { key: 'relearn',  label: 'Relearn',  color: _STATE_COLOR.relearn  },
  { key: 'learning', label: 'Learning', color: _STATE_COLOR.learning },
  { key: 'new',      label: 'New',      color: _STATE_COLOR.new      },
];
const _EVO_ALL_VIEWS = [['listening', 'Listening'], ['reading', 'Reading'],
                        ['creating', 'Creating'], ['all', 'All']];
// Tabs for categories still switched on, plus All (#869). If the stored view
// points at a category that has since been disabled, fall back to 'all' —
// otherwise the chart stays permanently blank with no hint why.
function _evoViews() {
  const on = _enabledCategories();
  const views = _EVO_ALL_VIEWS.filter(([k]) => k === 'all' || on[k]);
  if (!views.some(([k]) => k === _evoView)) _evoView = 'all';
  return views;
}

function initHomeEvolution() {
  const el = document.getElementById('home-evolution');
  if (!el) return;
  if (_evoData) { _evoRender(); return; }
  if (_evoLoading) return;
  _evoLoading = true;
  el.innerHTML = '<div class="hcal-loading">Loading card evolution…</div>';
  const langParam = _availableLangs.length > 1 ? `&lang=${activeLang()}` : '';
  api('GET', `/api/card-evolution?days=365${langParam}`)
    .then(d => { _evoData = d; _evoLoading = false; _evoRender(); })
    .catch(err => {
      _evoLoading = false;
      el.innerHTML = `<div class="hcal-loading">Card evolution unavailable — ${
        (err && err.message) || 'failed to load stats'}.</div>`;
    });
}

// Force a refetch (e.g. after reviewing). Safe to call even if not mounted.
function invalidateHomeEvolution() { _evoData = null; }

function evoSetView(v) {
  _evoView = v; localStorage.setItem('evoView', v); _evoRender();
}

// Sum the requested categories into one {state: [counts]} object.
function _evoSeries() {
  const n = _evoData.dates.length;
  const out = {};
  for (const s of _EVO_STATES) out[s.key] = new Array(n).fill(0);
  const cats = _evoView === 'all' ? Object.keys(_evoData.series) : [_evoView];
  for (const cat of cats) {
    const sr = _evoData.series[cat];
    if (!sr) continue;
    for (const s of _EVO_STATES) {
      const arr = sr[s.key] || [];
      for (let i = 0; i < n; i++) out[s.key][i] += arr[i] || 0;
    }
  }
  return out;
}

function _evoRender() {
  const el = document.getElementById('home-evolution');
  if (!el || !_evoData) return;

  // Resolve the tab list first: it also repairs a stored _evoView pointing at a
  // now-disabled category, and _evoSeries() below reads _evoView (#869).
  const views = _evoViews();
  const sr = _evoSeries();
  const allDates = _evoData.dates;
  const totalsAll = allDates.map((_, i) =>
    _EVO_STATES.reduce((a, s) => a + sr[s.key][i], 0));

  // Trim leading days before the first card existed (young collections)
  let first = totalsAll.findIndex(t => t > 0);
  if (first < 0) first = 0;
  first = Math.max(0, first - 1);
  const dates = allDates.slice(first);
  const series = {};
  for (const s of _EVO_STATES) series[s.key] = sr[s.key].slice(first);
  const n = dates.length;

  const W = 730, H = 190, PAD_T = 10, PAD_B = 2;
  let ymax = Math.max(1, ...totalsAll);
  const step = ymax > 200 ? 100 : ymax > 50 ? 25 : 10;
  ymax = Math.ceil(ymax / step) * step;

  const x = i => (i / Math.max(1, n - 1)) * W;
  const y = v => PAD_T + (1 - v / ymax) * (H - PAD_T - PAD_B);

  let lower = new Array(n).fill(0);
  const layers = _EVO_STATES.map(s => {
    const upper = lower.map((v, i) => v + series[s.key][i]);
    const top = upper.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
    const bot = lower.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).reverse();
    const pts = `${top.join(' ')} ${bot.join(' ')}`;
    lower = upper;
    return `<polygon points="${pts}" fill="${s.color}" fill-opacity="0.8"/>`;
  }).join('');

  const grid = [0.25, 0.5, 0.75, 1].map(f =>
    `<line x1="0" y1="${y(ymax * f).toFixed(1)}" x2="${W}" y2="${y(ymax * f).toFixed(1)}"
           stroke="var(--border)" stroke-width="0.6"/>`).join('');
  const ylabels = [0.5, 1].map(f =>
    `<span class="evo-ylabel" style="top:${(y(ymax * f) / H * 100).toFixed(2)}%">${
      Math.round(ymax * f)}</span>`).join('');

  const viewBtns = views.map(([k, lbl]) =>
    `<button class="hcal-seg-btn ${k === _evoView ? 'active' : ''}"
             onclick="evoSetView('${k}')">${lbl}</button>`).join('');
  const legend = _EVO_STATES.slice().reverse().map(s =>
    `<span class="evo-leg"><span class="hcal-leg-sw" style="background:${s.color}"></span>${s.label}</span>`).join('');

  const fmt = d => { const [, m, dd] = d.split('-'); return `${+m}/${+dd}`; };

  _evoCalc = { dates, series, n };

  el.innerHTML = `
    <div class="hcal-controls">
      <div class="hcal-seg">${viewBtns}</div>
      <div class="evo-legend">${legend}</div>
    </div>
    <div class="evo-chart-wrap">
      ${ylabels}
      <svg class="evo-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
           onmousemove="evoMove(event)" onmouseleave="evoLeave()">
        ${grid}${layers}
        <line id="evo-cursor" x1="0" y1="0" x2="0" y2="${H}"
              stroke="#1e293b" stroke-width="0.8" style="display:none"/>
      </svg>
      <div class="hcal-graph-axis"><span>${fmt(dates[0])}</span><span>${fmt(dates[n - 1])}</span></div>
    </div>`;
}

function evoMove(ev) {
  if (!_evoCalc) return;
  const svg = ev.currentTarget;
  const r = svg.getBoundingClientRect();
  const { dates, series, n } = _evoCalc;
  const i = Math.max(0, Math.min(n - 1,
    Math.round((ev.clientX - r.left) / r.width * (n - 1))));

  const cursor = svg.querySelector('#evo-cursor');
  if (cursor) {
    const cx = (i / Math.max(1, n - 1)) * 730;
    cursor.setAttribute('x1', cx); cursor.setAttribute('x2', cx);
    cursor.style.display = '';
  }

  const nice = _hcalParse(dates[i]).toLocaleDateString(undefined,
    { weekday: 'short', month: 'short', day: 'numeric' });
  let total = 0;
  const rows = _EVO_STATES.slice().reverse().map(s => {
    const v = series[s.key][i];
    total += v;
    return `<div class="hcal-tip-row"><span class="hcal-leg-sw" style="background:${
      s.color};margin-right:5px"></span>${s.label}: <b>${v}</b></div>`;
  }).join('');

  const t = _hcalTip();
  t.innerHTML = `<div class="hcal-tip-date">${nice}</div>
                 <div class="hcal-tip-big">${total} cards</div>${rows}`;
  t.style.display = 'block';
  let left = ev.pageX - t.offsetWidth / 2;
  left = Math.max(6, Math.min(left, window.scrollX + window.innerWidth - t.offsetWidth - 6));
  let top = ev.pageY - t.offsetHeight - 14;
  if (top < window.scrollY + 4) top = ev.pageY + 14;
  t.style.left = left + 'px';
  t.style.top = top + 'px';
}

function evoLeave() {
  hcalHideTip();
  const cursor = document.getElementById('evo-cursor');
  if (cursor) cursor.style.display = 'none';
}

// ═══════════════════════════════════════════════════════════════════════════
// Book reader (#836)
//
// Upload a German/English EPUB or PDF, then read it page by page in whichever
// language is being studied. Each page is translated and annotated server-side
// on first view (knowledge/rendition.py's render_html — the same pipeline the
// knowledge base uses) and cached, so paging back is instant and paging
// forward is only slow the first time through.
//
// The reader deliberately prefetches the next page as soon as one is shown:
// the translation is free (Google Translate) and the wait is the only thing
// standing between Daniel and reading.
// ═══════════════════════════════════════════════════════════════════════════

const _bookState = { bookId: null, pageNo: 1, pageCount: 0, lang: 'zh', loading: false };
let _bookLangs = null;   // [{code, name}] from /api/langs?available=1

// Every registered language, not just the ones already in use: a book is a new
// thing, and filtering by "languages that have decks" would make the first
// book in a language unreadable (same reasoning as /add and /dict, #805).
async function _loadBookLangs() {
  if (_bookLangs) return _bookLangs;
  try {
    // /api/langs returns bare codes, e.g. ["zh", "fr", "es"].
    _bookLangs = await api('GET', '/api/langs?available=1') || ['zh'];
  } catch (e) {
    _bookLangs = ['zh'];
  }
  return _bookLangs;
}

async function openBooks() {
  navPush('books');
  _bookState.bookId = null;
  showView('books');
  document.getElementById('view-books-content').innerHTML =
    '<p class="keymap-hint">Loading…</p>';
  await _loadBookLangs();
  try {
    const data = await api('GET', '/api/books');
    _renderBookList(data.books || []);
  } catch (e) {
    document.getElementById('view-books-content').innerHTML =
      `<p class="keymap-hint">Could not load books: ${_escHtml(e.message || 'error')}</p>`;
  }
}

function _bookLangOptions(selected) {
  return (_bookLangs || []).map(l =>
    `<option value="${_escHtml(l)}"${l === selected ? ' selected' : ''}>${_escHtml(_LANG_TAB_LABELS[l] || l)}</option>`
  ).join('');
}

function _renderBookList(books) {
  const rows = books.map(b => {
    const progress = Object.entries(b.progress || {})
      .map(([lang, page]) => `${_escHtml(lang)} p.${page}`).join(' · ');
    const chaptersBtn = b.format === 'epub'
      ? `<button class="word-table-btn" onclick="openBookChapters(${b.id})" title="Chapter summaries">📖</button>`
      : '';
    return `<tr>
      <td><a href="#" onclick="event.preventDefault();openBook(${b.id})">${_escHtml(b.title)}</a></td>
      <td>${_escHtml(b.author || '')}</td>
      <td>${_escHtml(b.format)} · ${_escHtml(b.source_lang)}</td>
      <td>${b.page_count}</td>
      <td class="keymap-hint">${progress || '—'}</td>
      <td>${chaptersBtn}<button class="word-table-btn" onclick="editBook(${b.id})" title="Edit title / author / language">✏️</button><button class="word-table-btn" onclick="deleteBook(${b.id})" title="Delete this book">🗑</button></td>
    </tr>`;
  }).join('');

  document.getElementById('view-books-content').innerHTML = `
    <div class="keymap-panel">
      <h2 class="keymap-heading">Upload a book</h2>
      <p class="keymap-hint">EPUB or PDF in German or English. It is split into
        reading pages once, then translated page by page into the language you
        pick when you open it. Scanned PDFs (no text layer) cannot be read.</p>
      <div class="book-upload-row">
        <input type="file" id="book-file" accept=".epub,.pdf">
        <input type="text" id="book-title" placeholder="Title (optional)">
        <select id="book-source-lang">
          <option value="">Detect language</option>
          <option value="de">German</option>
          <option value="en">English</option>
        </select>
        <button class="btn-secondary" id="book-upload-btn" onclick="doBookUpload()">Upload</button>
      </div>
      <p class="keymap-hint" id="book-upload-status"></p>
    </div>
    <div class="keymap-panel">
      <h2 class="keymap-heading">Your books</h2>
      ${rows
        ? `<div class="word-table-wrap"><table class="cost-table cost-table-compact">
             <thead><tr><th>Title</th><th>Author</th><th>Format</th><th>Pages</th><th>Progress</th><th></th></tr></thead>
             <tbody>${rows}</tbody></table></div>`
        : '<p class="keymap-hint">No books yet.</p>'}
    </div>`;
}

async function doBookUpload() {
  const fileInput = document.getElementById('book-file');
  const btn = document.getElementById('book-upload-btn');
  const status = document.getElementById('book-upload-status');
  const file = fileInput && fileInput.files && fileInput.files[0];
  if (!file) { showError('Pick an EPUB or PDF file first'); return; }

  const form = new FormData();
  form.append('file', file);
  const title = (document.getElementById('book-title').value || '').trim();
  if (title) form.append('title', title);
  const srcLang = document.getElementById('book-source-lang').value;
  if (srcLang) form.append('source_lang', srcLang);

  btn.disabled = true;
  status.textContent = 'Uploading…';
  let jobId;
  try {
    // Not api(): that helper sends JSON, and this is a multipart upload.
    const resp = await fetch('/api/books', { method: 'POST', body: form });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || `Upload failed (${resp.status})`);
    jobId = data.job_id;
  } catch (e) {
    btn.disabled = false;
    status.textContent = '';
    showError(e.message || 'Upload failed');
    return;
  }

  status.textContent = 'Reading the file…';
  const poll = async () => {
    let job;
    try {
      job = await api('GET', `/api/books/upload-progress/${jobId}`);
    } catch (e) {
      btn.disabled = false;
      status.textContent = '';
      showError(e.message || 'Lost track of the upload');
      return;
    }
    if (job.status === 'running') { setTimeout(poll, 1500); return; }
    btn.disabled = false;
    if (job.status === 'error') {
      status.textContent = '';
      // The server's reason is the whole point here — "no text layer",
      // "DRM-protected", "unsupported file type" all need saying out loud.
      showError(job.error || 'Could not read this file');
      return;
    }
    status.textContent = job.message || 'Done';
    fileInput.value = '';
    document.getElementById('book-title').value = '';
    openBooks();
  };
  setTimeout(poll, 1500);
}

// Three sequential single-field prompts rather than a custom modal (#882):
// this reuses the same showPrompt() every other rename flow in the app uses
// (see renameDeck), instead of one-off modal markup for three fields.
async function editBook(id) {
  let book;
  try {
    const data = await api('GET', '/api/books');
    book = (data.books || []).find(b => b.id === id);
  } catch (e) { /* fall through to the not-found check below */ }
  if (!book) { showError('Could not load this book'); return; }

  const title = await showPrompt('Title', book.title);
  if (title === null) return;
  if (!title.trim()) { showError('Title cannot be empty'); return; }

  const author = await showPrompt('Author (optional)', book.author || '');
  if (author === null) return;

  const sourceLang = await showPrompt('Source language: de or en', book.source_lang);
  if (sourceLang === null) return;
  if (!['de', 'en'].includes(sourceLang.trim())) {
    showError('Source language must be "de" or "en"');
    return;
  }

  try {
    await api('PATCH', `/api/books/${id}`,
      { title: title.trim(), author: author.trim(), source_lang: sourceLang.trim() });
  } catch (e) {
    showError(e.message || 'Could not update book');
    return;
  }
  openBooks();
}

async function deleteBook(id) {
  if (!await showConfirm('Delete this book, its pages and all cached translations?')) return;
  try {
    await api('DELETE', `/api/books/${id}`);
    openBooks();
  } catch (e) {
    showError(e.message || 'Could not delete the book');
  }
}

// ── Reader ─────────────────────────────────────────────────────────────────

async function openBook(id, pageNo, lang) {
  navPush(`books:${id}`);
  _bookState.bookId = id;
  const langs = await _loadBookLangs();
  const wanted = lang || _bookState.lang || activeLang();
  _bookState.lang = langs.includes(wanted) ? wanted : (langs[0] || 'zh');
  showView('books');
  // No page given → continue where this book was left off in this language.
  if (pageNo == null) {
    try {
      const data = await api('GET', '/api/books');
      const book = (data.books || []).find(b => b.id === id);
      pageNo = (book && book.progress && book.progress[_bookState.lang]) || 1;
    } catch (e) {
      pageNo = 1;
    }
  }
  _showBookPage(pageNo);
}

async function _showBookPage(pageNo) {
  const id = _bookState.bookId;
  if (!id || _bookState.loading) return;
  _bookState.loading = true;
  document.getElementById('view-books-content').innerHTML =
    '<p class="keymap-hint">Translating this page…</p>';
  let page;
  try {
    page = await api('GET', `/api/books/${id}/page/${pageNo}?lang=${encodeURIComponent(_bookState.lang)}`);
  } catch (e) {
    _bookState.loading = false;
    document.getElementById('view-books-content').innerHTML = `
      <div class="keymap-panel">
        <p class="keymap-hint">Could not render page ${pageNo}: ${_escHtml(e.message || 'error')}</p>
        <button class="btn-secondary" onclick="openBooks()">← Back to books</button>
      </div>`;
    return;
  }
  _bookState.loading = false;
  if (_bookState.bookId !== id) return;   // navigated away while translating
  _bookState.pageNo = page.page_no;
  _bookState.pageCount = page.page_count;
  _renderBookPage(page);
  // Fire-and-forget: remembering the position must never block reading, and a
  // failed save just means resuming a page early next time.
  api('POST', `/api/books/${id}/progress`,
      { lang: _bookState.lang, page_no: page.page_no }).catch(() => {});
  _prefetchBookPage(page.page_no + 1);
}

// Render the page after this one into the server-side cache while Daniel
// reads. The response is thrown away — the point is the cached rendition.
function _prefetchBookPage(pageNo) {
  const id = _bookState.bookId;
  if (!id || pageNo > _bookState.pageCount) return;
  fetch(`/api/books/${id}/page/${pageNo}?lang=${encodeURIComponent(_bookState.lang)}`)
    .catch(() => {});
}

function _renderBookPage(page) {
  setWordTable(page.new_words || [], page.lang);
  const ref = page.ref_label ? ` · ${_escHtml(page.ref_label)}` : '';
  const atStart = page.page_no <= 1;
  const atEnd = page.page_no >= page.page_count;
  document.getElementById('view-books-content').innerHTML = `
    <div class="book-reader-head">
      <button class="btn-secondary" onclick="openBooks()">← Books</button>
      <span class="book-reader-title">${_escHtml(page.title)}${page.author ? ` · ${_escHtml(page.author)}` : ''}</span>
      <button class="btn-secondary" onclick="openBookChapters(${_bookState.bookId})" title="Chapter summaries">📖 Chapters</button>
      <select id="book-lang-select" onchange="changeBookLang(this.value)" title="Reading language">
        ${_bookLangOptions(page.lang)}
      </select>
    </div>
    <div class="keymap-panel book-page">${_summaryZhHtml(page.text || '')}</div>
    <div class="book-reader-nav">
      <button class="btn-secondary" onclick="turnBookPage(-1)" ${atStart ? 'disabled' : ''}>← Prev</button>
      <span class="keymap-hint">
        <input type="number" id="book-jump" min="1" max="${page.page_count}"
               value="${page.page_no}" onchange="jumpToBookPage(this.value)"> / ${page.page_count}${ref}
      </span>
      <button class="btn-secondary" onclick="turnBookPage(1)" ${atEnd ? 'disabled' : ''}>Next →</button>
    </div>
    <div class="keymap-panel">
      <h2 class="keymap-heading">New words on this page</h2>
      ${wordTableHtml('Nothing above your level on this page.')}
    </div>`;
  _makeWordsTappable(document.querySelector('#view-books-content .book-page'));
}

function turnBookPage(delta) {
  const next = _bookState.pageNo + delta;
  if (next < 1 || next > _bookState.pageCount) return;
  _showBookPage(next);
}

function jumpToBookPage(value) {
  const page = parseInt(value, 10);
  if (!page || page < 1 || page > _bookState.pageCount) {
    showError(`Enter a page between 1 and ${_bookState.pageCount}`);
    return;
  }
  _showBookPage(page);
}

// Switching language re-reads the same page in the new one; each language
// keeps its own reading position, so the position is saved under the new
// language from here on.
function changeBookLang(lang) {
  _bookState.lang = lang;
  _showBookPage(_bookState.pageNo);
}

// ── Chapters (#864) ──────────────────────────────────────────────────────────
// Table of contents + per-chapter Chinese summary, generated on demand
// (Daniel clicks "Generate", nothing runs automatically — a chapter is a
// real AI call, unlike a page translation which is free Google Translate).
let _bookChaptersTitle = '';
let _bookChaptersFormat = '';

async function openBookChapters(bookId) {
  _bookState.bookId = bookId;
  // Same language the reader is showing this book in — falls back to the
  // active main-page language tab when chapters are opened straight from
  // the book list (reader never opened yet this session), matching openBook().
  const langs = await _loadBookLangs();
  const wanted = _bookState.lang || activeLang();
  _bookState.lang = langs.includes(wanted) ? wanted : (langs[0] || 'zh');
  showView('books');
  document.getElementById('view-books-content').innerHTML =
    '<p class="keymap-hint">Loading chapters…</p>';
  try {
    const data = await api('GET', '/api/books');
    const book = (data.books || []).find(b => b.id === bookId);
    _bookChaptersTitle = book ? book.title : '';
    _bookChaptersFormat = book ? book.format : '';
  } catch (e) { _bookChaptersTitle = ''; _bookChaptersFormat = ''; }

  let data;
  try {
    data = await api('GET', `/api/books/${bookId}/chapters?lang=${encodeURIComponent(_bookState.lang)}`);
  } catch (e) {
    document.getElementById('view-books-content').innerHTML = `
      <div class="keymap-panel">
        <p class="keymap-hint">Could not load chapters: ${_escHtml(e.message || 'error')}</p>
        <button class="btn-secondary" onclick="openBooks()">← Back to books</button>
      </div>`;
    return;
  }
  _renderBookChapters(bookId, data);
}

function _renderBookChapters(bookId, data) {
  const head = `
    <div class="book-reader-head">
      <button class="btn-secondary" onclick="openBook(${bookId})">← Reader</button>
      <span class="book-reader-title">${_escHtml(_bookChaptersTitle)} — Chapters</span>
    </div>`;

  if (!data.available) {
    // Rescan (#881) only makes sense for EPUBs: a PDF's ref_label is its
    // real page number, not a chapter marker, so "no chapters" there is
    // permanent, not a stale pre-#881 upload.
    const rescanBtn = _bookChaptersFormat === 'epub'
      ? `<button class="btn-secondary" id="book-rescan-btn" onclick="doRescanBookChapters(${bookId})">↻ 重新识别章节</button>`
      : '';
    document.getElementById('view-books-content').innerHTML = head + `
      <div class="keymap-panel">
        <p class="keymap-hint">${_escHtml(data.reason || 'No chapters available for this book.')}</p>
        ${rescanBtn}
      </div>`;
    return;
  }

  const rows = data.chapters.map(ch => {
    const label = ch.title_zh || ch.ref_label || `第${ch.number}章`;
    const pages = `p.${ch.start_page}–${ch.end_page}`;
    let action;
    if (ch.status === 'summarized') {
      action = `<button class="word-table-btn" onclick="openBookChapterSummary(${bookId}, ${ch.number})">阅读摘要</button>`;
    } else if (ch.status === 'error') {
      action = `<button class="word-table-btn" id="ch-sum-btn-${ch.number}" onclick="doSummarizeChapter(${bookId}, ${ch.number})">↻ 重试</button>`;
    } else {
      action = `<button class="word-table-btn" id="ch-sum-btn-${ch.number}" onclick="doSummarizeChapter(${bookId}, ${ch.number})">✨ 生成摘要</button>`;
    }
    const errorRow = ch.status === 'error' && ch.error
      ? `<div class="keymap-hint book-chapter-error" id="ch-sum-err-${ch.number}">${_escHtml(ch.error)}</div>` : '';
    const conceptRow = ch.status === 'summarized' && ch.concept_zh
      ? `<div class="keymap-hint book-chapter-concept">${_escHtml(ch.concept_zh)}</div>` : '';
    // rendition_error (#894): the translation into the current reading
    // language failed — the row above still shows the Chinese fields, this
    // just explains why they're not in that language.
    const renditionErrorRow = ch.rendition_error
      ? `<div class="keymap-hint book-chapter-error">译文生成失败：${_escHtml(ch.rendition_error)}</div>` : '';
    return `<div class="book-chapter-row" id="ch-row-${ch.number}">
      <div class="book-chapter-main">
        <span class="book-chapter-number">${ch.number}.</span>
        <span class="book-chapter-title">${_escHtml(label)}</span>
        <span class="keymap-hint">${pages}</span>
        ${action}
      </div>
      ${conceptRow}
      ${errorRow}
      ${renditionErrorRow}
    </div>`;
  }).join('');

  document.getElementById('view-books-content').innerHTML = head + `
    <div class="keymap-panel">${rows}</div>`;
}

async function doRescanBookChapters(bookId) {
  const btn = document.getElementById('book-rescan-btn');
  if (btn) { btn.disabled = true; btn.textContent = '重新识别中…'; }
  try {
    await api('POST', `/api/books/${bookId}/rescan-chapters`);
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = '↻ 重新识别章节'; }
    showError(e.message || 'Could not rescan chapters');
    return;
  }
  openBookChapters(bookId);
}

async function doSummarizeChapter(bookId, number) {
  const btn = document.getElementById(`ch-sum-btn-${number}`);
  if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }
  // A regenerate must not leave the popup showing the previous summary, in
  // any cached language — the server already cleared every rendition too
  // (database.save_chapter_summary → delete_chapter_renditions, #894).
  Object.keys(_bookChapterCache)
    .filter(k => k.startsWith(`${bookId}:${number}:`))
    .forEach(k => delete _bookChapterCache[k]);
  try {
    await api('POST', `/api/books/${bookId}/chapters/${number}/summarize`);
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = '✨ 生成摘要'; }
    showError(e.message || 'Could not start summarization');
    return;
  }
  _pollBookChapter(bookId, number);
}

async function _pollBookChapter(bookId, number, attempt) {
  attempt = attempt || 0;
  let chapter;
  try {
    chapter = await api('GET', `/api/books/${bookId}/chapters/${number}`);
  } catch (e) {
    // Lost track — reload the whole list rather than polling forever.
    openBookChapters(bookId);
    return;
  }
  if (chapter.status === 'pending' && attempt < 120) {
    setTimeout(() => _pollBookChapter(bookId, number, attempt + 1), 2000);
    return;
  }
  // Re-render just this row's data via a full reload — chapter lists are
  // short, and this keeps one source of truth for the row markup.
  try {
    const data = await api('GET', `/api/books/${bookId}/chapters?lang=${encodeURIComponent(_bookState.lang)}`);
    if (_bookState.bookId === bookId) _renderBookChapters(bookId, data);
  } catch (e) { /* the button will just stay in its loading state */ }
}

const _bookChapterCache = {}; // `${bookId}:${number}:${lang}` → full chapter (#894: keyed by
                               // language too, or switching languages would show a stale rendition)

async function openBookChapterSummary(bookId, number) {
  const overlay = document.getElementById('kahneman-examples-overlay');
  const modal   = document.getElementById('kahneman-examples-modal');
  const titleEl = document.getElementById('kahneman-examples-title');
  const bodyEl  = document.getElementById('kahneman-examples-body');
  titleEl.textContent = `第${number}章`;
  bodyEl.innerHTML = '<div class="kahneman-examples-loading">加载中…</div>';
  overlay.style.display = '';
  modal.style.display = '';

  const lang = _bookState.lang || 'zh';
  const key = `${bookId}:${number}:${lang}`;
  let chapter = _bookChapterCache[key];
  if (!chapter) {
    try {
      chapter = await api('GET', `/api/books/${bookId}/chapters/${number}?lang=${encodeURIComponent(lang)}`);
      _bookChapterCache[key] = chapter;
    } catch (e) {
      bodyEl.innerHTML = '';
      bodyEl.appendChild(document.createTextNode('加载失败：' + (e.message || 'error')));
      return;
    }
  }
  if (modal.style.display === 'none') return; // closed while loading

  titleEl.textContent = chapter.title_zh || `第${number}章`;
  bodyEl.innerHTML = '';
  if (chapter.rendition_error) {
    // Translation into the current reading language failed — everything
    // below is still the Chinese original, this just says why (#894).
    const warn = document.createElement('div');
    warn.className = 'keymap-hint book-chapter-error';
    warn.textContent = '译文生成失败，以下为中文原文：' + chapter.rendition_error;
    bodyEl.appendChild(warn);
  }
  if (chapter.title_en) {
    const en = document.createElement('div');
    en.className = 'kahneman-title-en';
    en.textContent = chapter.title_en;
    bodyEl.appendChild(en);
  }
  if (chapter.concept_zh) {
    const concept = document.createElement('div');
    concept.className = 'kahneman-summary';
    concept.textContent = chapter.concept_zh;
    bodyEl.appendChild(concept);
  }
  if (chapter.summary_zh) {
    const label = document.createElement('div');
    label.className = 'kahneman-examples-label';
    label.textContent = '本章摘要';
    bodyEl.appendChild(label);
    const detail = document.createElement('div');
    detail.className = 'kahneman-detail';
    detail.textContent = chapter.summary_zh;
    bodyEl.appendChild(detail);
  }
  if (chapter.examples_zh && chapter.examples_zh.length) {
    const label = document.createElement('div');
    label.className = 'kahneman-examples-label';
    label.textContent = '书中原句';
    bodyEl.appendChild(label);
    chapter.examples_zh.forEach(ex => {
      const p = document.createElement('p');
      p.className = 'kahneman-example';
      p.textContent = ex;
      bodyEl.appendChild(p);
    });
  } else {
    const empty = document.createElement('div');
    empty.className = 'kahneman-examples-loading';
    empty.textContent = '本章暂无原句摘录。';
    bodyEl.appendChild(empty);
  }
}

// ── Chat about a knowledge item (#945) ──────────────────────────────────────
// Follow-up questions about the material, saved server-side so they are still
// there on the next visit. Every message is rendered with textContent — the
// answers are model-written text and never touch innerHTML (same rule /dict
// follows, #746).

// The model dropdown's options. static/index.html already carries two copies
// of this list for the story modals; this is deliberately a third *place* but
// the only one in JS — do not add a fourth by copying it into a template
// string somewhere else.
const KNOWLEDGE_CHAT_MODELS = [
  ['deepseek-v4-flash', 'DeepSeek V4 Flash — cheap, reliable'],
  ['deepseek-v4-pro', 'DeepSeek V4 Pro — higher quality'],
  ['glm-4.7', 'GLM-4.7 — Zhipu, best Chinese value'],
  ['glm-5', 'GLM-5 — Zhipu flagship'],
  ['glm-4.7-flash', 'GLM-4.7-Flash — Zhipu, free'],
  ['qwen-turbo', 'Qwen Turbo — Alibaba, cheap'],
  ['claude-haiku-4-5-20251001', 'Haiku — Anthropic fast'],
  ['claude-sonnet-4-6', 'Sonnet — Anthropic balanced'],
  ['claude-opus-4-6', 'Opus — Anthropic flagship'],
  ['gpt-5-mini', 'GPT-5 Mini — OpenAI, cheap'],
  ['gpt-5.6-luna', 'GPT-5.6 Luna — OpenAI, cheap + newest'],
  ['gpt-5.6-terra', 'GPT-5.6 Terra — OpenAI, balanced'],
  ['gpt-5.6-sol', 'GPT-5.6 Sol — OpenAI flagship'],
];

function _knowledgeChatModel() {
  const saved = localStorage.getItem('knowledgeChatModel');
  return KNOWLEDGE_CHAT_MODELS.some(m => m[0] === saved) ? saved : KNOWLEDGE_CHAT_MODELS[0][0];
}

// The chat panel's markup. Rendered only when the item actually has material
// to talk about — an input box that could only ever produce a 400 is worse
// than no box at all.
function _knowledgeChatHtml(ep) {
  const hasMaterial = !!((ep.transcript_zh || '').trim() || (ep.summary_de || '').trim());
  if (!hasMaterial) return '';
  const current = _knowledgeChatModel();
  const options = KNOWLEDGE_CHAT_MODELS
    .map(([value, label]) => `<option value="${_escHtml(value)}"${value === current ? ' selected' : ''}>${_escHtml(label)}</option>`)
    .join('');
  return `
    <div class="keymap-panel">
      <h2 class="keymap-heading">💬 Chat</h2>
      <div id="knowledge-chat-log" class="knowledge-chat-log"></div>
      <div class="knowledge-chat-input">
        <textarea id="knowledge-chat-text" class="edit-input" rows="2"
                  placeholder="Ask about this text… (Ctrl/⌘+Enter to send)"
                  onkeydown="_knowledgeChatKey(event)"></textarea>
        <div class="knowledge-chat-controls">
          <select id="knowledge-chat-model" class="edit-input" onchange="_knowledgeChatModelChanged(this)">${options}</select>
          <button id="knowledge-chat-send" class="btn-secondary" onclick="doKnowledgeChatSend()">Send</button>
          <button id="knowledge-chat-clear" class="btn-secondary" onclick="doKnowledgeChatClear()">🗑 Clear chat</button>
        </div>
      </div>
    </div>`;
}

function _knowledgeChatModelChanged(sel) {
  localStorage.setItem('knowledgeChatModel', sel.value);
}

function _knowledgeChatKey(ev) {
  if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey)) {
    ev.preventDefault();
    doKnowledgeChatSend();
  }
}

// Append one message bubble. textContent only, so a model that writes markup
// gets displayed, not executed.
function _appendKnowledgeChatMessage(msg) {
  const log = document.getElementById('knowledge-chat-log');
  if (!log) return;
  const empty = log.querySelector('.knowledge-chat-empty');
  if (empty) empty.remove();
  const row = document.createElement('div');
  row.className = 'knowledge-chat-msg knowledge-chat-' + (msg.role === 'assistant' ? 'ai' : 'me');
  const who = document.createElement('div');
  who.className = 'knowledge-chat-who';
  who.textContent = msg.role === 'assistant' ? (msg.model || 'AI') : 'You';
  const body = document.createElement('div');
  body.className = 'knowledge-chat-body';
  body.textContent = msg.content || '';
  row.appendChild(who);
  row.appendChild(body);
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
}

function _renderKnowledgeChatLog(messages) {
  const log = document.getElementById('knowledge-chat-log');
  if (!log) return;
  log.textContent = '';
  if (!messages.length) {
    const hint = document.createElement('p');
    hint.className = 'keymap-hint knowledge-chat-empty';
    hint.textContent = 'No questions asked about this item yet.';
    log.appendChild(hint);
    return;
  }
  messages.forEach(_appendKnowledgeChatMessage);
}

// Loaded after the detail view renders. A failed load shows why instead of an
// empty log that looks like "you never asked anything".
async function _loadKnowledgeChat(episodeId) {
  if (!document.getElementById('knowledge-chat-log')) return;
  try {
    const data = await api('GET', `/api/knowledge/${episodeId}/chat`);
    if (_podcastDetailEpisodeId !== episodeId) return; // navigated away
    _renderKnowledgeChatLog(data.messages || []);
  } catch (e) {
    const log = document.getElementById('knowledge-chat-log');
    if (log) log.textContent = 'Could not load the chat: ' + (e.message || 'error');
  }
}

async function doKnowledgeChatSend() {
  const box = document.getElementById('knowledge-chat-text');
  const btn = document.getElementById('knowledge-chat-send');
  const id = _podcastDetailEpisodeId;
  if (!box || !btn || !id) return;
  const message = box.value.trim();
  if (!message) return;
  const model = document.getElementById('knowledge-chat-model')?.value || undefined;
  btn.disabled = true;
  btn.textContent = 'Thinking…';
  try {
    const data = await api('POST', `/api/knowledge/${id}/chat`, { message, model });
    if (_podcastDetailEpisodeId !== id) return; // navigated away mid-answer
    (data.messages || []).forEach(_appendKnowledgeChatMessage);
    // Only clear the box once the turn is safely stored — a failed call must
    // leave his question where he can just press Send again.
    box.value = '';
  } catch (e) {
    showError(e.message || 'Chat failed');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Send';
  }
}

async function doKnowledgeChatClear() {
  const id = _podcastDetailEpisodeId;
  const log = document.getElementById('knowledge-chat-log');
  if (!id || !log) return;
  // Nothing stored yet: the DELETE would honestly 404 (the route refuses to
  // pretend), which as an error popup would just be confusing noise here.
  if (!log.querySelector('.knowledge-chat-msg')) return;
  if (!await showConfirm('Delete this conversation? This cannot be undone.')) return;
  try {
    await api('DELETE', `/api/knowledge/${id}/chat`);
  } catch (e) {
    showError(e.message || 'Could not clear the chat');
    return;
  }
  _renderKnowledgeChatLog([]);
}

// ---------------------------------------------------------------------------
// 📬 Mailbox (#960)
//
// Daniel subscribes to newsletters with his Gmail address and picks what is
// worth reading here — the same "process" button a podcast episode has. The
// list is a live IMAP view, never a mirror: every render re-fetches, so it
// can't go stale against the real inbox, and nothing about his mail is
// stored on the server unless he presses Process on it.
// ---------------------------------------------------------------------------

const _MAILBOX_PAGE = 50;

const _mailboxState = {
  offset: 0,
  query: '',
  total: 0,
  uidvalidity: '',
  messages: [],
  busy: null,     // uid currently being processed — one at a time, see below
  notice: null,   // {text, error} shown under the toolbar
  range: 'week',  // 'week' | 'month' | 'all' (#968) — this week by default
  hidden: 0,      // mails from blocked senders left out of this page
  senders: null,  // aggregated sender list, loaded on first switch
  sendersBusy: null,
};

// This page has no toast mechanism to borrow (the rest of the app reports
// per-row results in the button itself), so results land in one line under
// the toolbar. Cleared on every navigation so a stale "Processing…" can't
// outlive the page it belonged to.
function _mailboxNotice(text, error) {
  _mailboxState.notice = text ? { text, error: !!error } : null;
  // The same senders are reachable from two screens (#988): the ⚡ button on
  // an inbox row and the Subscriptions list. Re-render whichever is on screen
  // — repainting the inbox over the Subscriptions screen would swap the page
  // out from under the click that caused it.
  if (_knowledgeScreen === 'subs') _renderKnowledgeSubs();
  else _renderMailbox();
}

async function openMailbox() {
  navPush('knowledge:mailbox');
  _mailboxState.offset = 0;
  _mailboxState.query = '';
  _mailboxState.notice = null;
  _clearPodcastPoll();
  _podcastCurrentFeedId = null;
  _knowledgeScreen = 'mailbox';
  showView('knowledge');
  await _loadMailbox();
}

async function _loadMailbox() {
  _mailboxState.notice = null;
  const el = document.getElementById('view-knowledge-content');
  if (!el) return;
  // Keep the chrome around the placeholder: a slow or failing mailbox must
  // still say which screen it is.
  el.innerHTML = `${_mailboxTabs()}<p class="keymap-hint">Loading inbox…</p>`;
  try {
    const params = new URLSearchParams({
      offset: _mailboxState.offset,
      limit: _MAILBOX_PAGE,
      range: _mailboxState.range,
    });
    if (_mailboxState.query) params.set('q', _mailboxState.query);
    const data = await api('GET', `/api/mailbox?${params}`);
    _mailboxState.total = data.total || 0;
    _mailboxState.uidvalidity = data.uidvalidity || '';
    _mailboxState.messages = data.messages || [];
    _mailboxState.hidden = data.hidden || 0;
    _renderMailbox();
  } catch (e) {
    // The server's message says which of the two it is — mailbox
    // unreachable vs. credentials missing — so it is shown verbatim
    // rather than replaced by a generic "could not load".
    el.innerHTML = `${_mailboxTabs()}<p class="keymap-hint">Could not load the inbox: ${_escHtml(e.message || 'error')}</p>`;
  }
}

function _mailboxDate(raw) {
  const d = new Date(raw);
  if (isNaN(d)) return _escHtml(raw || '');
  return d.toLocaleDateString(undefined, { day: '2-digit', month: '2-digit', year: '2-digit' });
}

const _MAILBOX_RANGES = [['week', 'This week'], ['month', '4 weeks'], ['all', 'All']];

function _mailboxRangePicker() {
  return `<select class="opt-input mailbox-range" onchange="setMailboxRange(this.value)">
    ${_MAILBOX_RANGES.map(([v, label]) =>
      `<option value="${v}"${_mailboxState.range === v ? ' selected' : ''}>${label}</option>`).join('')}
  </select>`;
}

function setMailboxRange(range) {
  _mailboxState.range = range;
  _mailboxState.offset = 0;
  // The sender counts are per range, so a cached scan for the old range
  // must not be shown under the new one's label.
  _mailboxState.senders = null;
  if (_knowledgeScreen === 'subs') _loadMailboxSenders();
  else _loadMailbox();
}

// The inbox screen's chrome. Subscribing/unsubscribing is deliberately NOT a
// tab here any more (#988) — it lives on the Subscriptions screen, so that
// "where do I manage what I get" has exactly one answer.
function _mailboxTabs() {
  return `
    <div class="knowledge-header">
      <h2 class="keymap-heading" style="margin:0">Inbox</h2>
      <span style="flex:1"></span>
      <button class="btn-secondary" onclick="openKnowledgeSubs('newsletters')"
              title="Manage which senders are processed automatically">📡 Subscriptions</button>
    </div>`;
}

async function _loadMailboxSenders(refresh) {
  const el = document.getElementById('view-knowledge-content');
  if (el) el.innerHTML = `${_subsTabsHtml()}<p class="keymap-hint">Scanning the mailbox…</p>`;
  try {
    const data = await api('GET',
      `/api/mailbox/senders?range=${_mailboxState.range}${refresh ? '&refresh=1' : ''}`);
    _mailboxState.senders = data.senders || [];
    // A scan failure still returns the configured senders, so unsubscribing
    // works even while IMAP is down — but say so rather than showing a list
    // that silently lost everyone Daniel never subscribed to.
    _mailboxState.notice = data.error
      ? { text: `Could not read the mailbox (${data.error}) — showing subscribed senders only.`, error: true }
      : null;
    _renderMailboxSenders();
  } catch (e) {
    if (el) el.innerHTML = `${_subsTabsHtml()}<p class="keymap-hint">Could not load senders: ${_escHtml(e.message || 'error')}</p>`;
  }
}

function _renderMailboxSenders() {
  if (_knowledgeScreen !== 'subs') return;
  _renderKnowledgeSubs();
}

function _mailboxSendersBodyHtml() {
  const st = _mailboxState;
  const rows = (st.senders || []).map(sn => {
    const busy = st.sendersBusy === sn.address;
    return `
      <div class="mailbox-row${sn.auto_process ? ' mailbox-row-sub' : ''}${sn.blocked ? ' mailbox-row-blocked' : ''}">
        <div class="mailbox-meta">
          <span class="mailbox-from" title="${_escHtml(sn.address)}">${_escHtml(sn.address)}</span>
          <span class="mailbox-date">${sn.count} mail${sn.count === 1 ? '' : 's'}${sn.last_date ? ' · ' + _mailboxDate(sn.last_date) : ''}</span>
        </div>
        <div class="mailbox-subject">${_escHtml(sn.name)}</div>
        <div class="mailbox-actions">
          <button class="btn-secondary mailbox-btn" onclick="filterMailboxBySender('${_escHtml(sn.address)}')" title="Show this sender's mail">📥</button>
          <button class="mailbox-auto${sn.auto_process ? ' on' : ''}"${busy || sn.blocked ? ' disabled' : ''}
                  onclick="toggleMailSender('${_escHtml(sn.address)}', ${sn.auto_process ? 'false' : 'true'}, '${_escHtml(sn.name)}')">
            ${sn.auto_process ? '⚡ Subscribed' : 'Subscribe'}
          </button>
          <button class="mailbox-block${sn.blocked ? ' on' : ''}"
                  title="${sn.blocked ? 'Show this sender again' : 'Hide this sender from the list and never process it'}"
                  onclick="blockMailSender('${_escHtml(sn.address)}', ${sn.blocked ? 'false' : 'true'}, '${_escHtml(sn.name)}')">
            ${sn.blocked ? 'Unblock' : '🚫'}
          </button>
        </div>
      </div>`;
  }).join('');

  return `
    <div class="mailbox-toolbar">
      <span class="keymap-hint" style="flex:1">
        Counts are for the selected period. Subscribing processes every <em>future</em>
        mail from that sender automatically — it does not go back over old ones.
      </span>
      ${_mailboxRangePicker()}
      <button class="btn-secondary" onclick="_loadMailboxSenders(true)" title="Rescan the mailbox">⟳</button>
    </div>
    ${st.notice ? `<p class="mailbox-notice${st.notice.error ? ' error' : ''}">${_escHtml(st.notice.text)}</p>` : ''}
    ${rows || '<p class="keymap-hint">No senders found.</p>'}`;
}

function filterMailboxBySender(address) {
  _mailboxState.query = address;
  _mailboxState.offset = 0;
  _knowledgeScreen = 'mailbox';
  _loadMailbox();
}

function _renderMailbox() {
  if (_knowledgeScreen !== 'mailbox') return;
  const st = _mailboxState;
  const from = st.total === 0 ? 0 : st.offset + 1;
  const to = Math.min(st.offset + _MAILBOX_PAGE, st.total);

  const rows = st.messages.map(m => {
    const busy = st.busy === m.uid;
    // An already-processed mail links to its entry instead of offering to
    // spend the AI call a second time.
    const action = m.processed
      ? `<a class="btn-secondary mailbox-btn" href="#knowledge-${m.episode_id}" onclick="openKnowledgeItem(${m.episode_id});return false;">✓ Open</a>`
      : `<button class="btn-secondary mailbox-btn" onclick="processMail('${_escHtml(m.uid)}')"${busy ? ' disabled' : ''}>${busy ? '…' : 'Process'}</button>`;
    const autoTitle = m.auto_process
      ? 'Every new mail from this sender is processed automatically — click to stop that'
      : 'Process every future mail from this sender automatically';
    return `
      <div class="mailbox-row${m.processed ? ' mailbox-row-done' : ''}">
        <div class="mailbox-meta">
          <span class="mailbox-from" title="${_escHtml(m.from)}">${_escHtml(m.from_name)}</span>
          <span class="mailbox-date">${_mailboxDate(m.date)}</span>
        </div>
        <div class="mailbox-subject">${_escHtml(m.subject)}</div>
        <div class="mailbox-actions">
          <button class="mailbox-auto${m.auto_process ? ' on' : ''}"
                  title="${autoTitle}"
                  onclick="toggleMailSender('${_escHtml(m.from)}', ${m.auto_process ? 'false' : 'true'}, '${_escHtml(m.from_name)}')">
            ${m.auto_process ? '⚡ Auto' : '⚡'}
          </button>
          ${action}
          <button class="mailbox-del" title="Move to the Gmail trash"
                  onclick="deleteMail('${_escHtml(m.uid)}')">🗑</button>
        </div>
      </div>`;
  }).join('');

  document.getElementById('view-knowledge-content').innerHTML = `
    ${_mailboxTabs()}
    <div class="mailbox-toolbar">
      <input id="mailbox-search" class="opt-input" placeholder="Search sender or subject…"
             value="${_escHtml(st.query)}" onkeydown="if(event.key==='Enter')searchMailbox()">
      <button class="btn-secondary" onclick="searchMailbox()">Search</button>
      ${_mailboxRangePicker()}
      <button class="btn-secondary" onclick="_loadMailbox()" title="Refetch from Gmail">⟳</button>
    </div>
    <p class="keymap-hint" style="margin:8px 0 12px">
      ${st.total ? `${from}–${to} of ${st.total}` : 'No mail found'}
      ${st.hidden ? `· ${st.hidden} hidden (blocked sender)` : ''}
      · ⚡ = process every future mail from that sender automatically
    </p>
    ${st.notice ? `<p class="mailbox-notice${st.notice.error ? ' error' : ''}">${_escHtml(st.notice.text)}</p>` : ''}
    ${rows || ''}
    <div class="mailbox-pager">
      <button class="btn-secondary" onclick="pageMailbox(-1)"${st.offset === 0 ? ' disabled' : ''}>← Newer</button>
      <button class="btn-secondary" onclick="pageMailbox(1)"${to >= st.total ? ' disabled' : ''}>Older →</button>
    </div>`;
}

function searchMailbox() {
  _mailboxState.query = (document.getElementById('mailbox-search').value || '').trim();
  _mailboxState.offset = 0;
  _loadMailbox();
}

function pageMailbox(direction) {
  const next = _mailboxState.offset + direction * _MAILBOX_PAGE;
  if (next < 0 || next >= _mailboxState.total) return;
  _mailboxState.offset = next;
  _loadMailbox();
}

async function processMail(uid) {
  // One at a time: each Process is an AI call, and a double tap on a phone
  // would otherwise fire two of them before the first response lands.
  if (_mailboxState.busy) return;
  _mailboxState.busy = uid;
  _renderMailbox();
  try {
    const params = _mailboxState.uidvalidity
      ? `?uidvalidity=${encodeURIComponent(_mailboxState.uidvalidity)}` : '';
    const res = await api('POST', `/api/mailbox/${encodeURIComponent(uid)}/process${params}`);
    const msg = _mailboxState.messages.find(m => m.uid === uid);
    if (msg) {
      msg.processed = true;
      msg.episode_id = res.episode_id;
    }
    _mailboxState.notice = res.status === 'already_exists'
      ? { text: 'Already in the knowledge base — opened from the ✓ button.', error: false }
      // Summarising runs in the background (it can take minutes); the entry
      // exists already, so the row turns into a link right away and the
      // top-bar task indicator reports the rest.
      : { text: 'Processing — the summary will appear under 🧠 Knowledge.', error: false };
  } catch (e) {
    _mailboxState.notice = { text: `Could not process: ${e.message || 'error'}`, error: true };
  } finally {
    _mailboxState.busy = null;
    _renderMailbox();
  }
}

async function toggleMailSender(address, auto, name) {
  try {
    await api('PUT', '/api/mailbox/senders', { address, auto, name });
    // Every row from this sender flips, not just the one clicked — the
    // switch is per sender, and showing two rows of the same sender in
    // different states would be a lie.
    _mailboxState.messages.forEach(m => {
      if (m.from === address) m.auto_process = auto;
    });
    (_mailboxState.senders || []).forEach(sn => {
      if (sn.address === address) {
        sn.auto_process = auto;
        // Subscribing lifts a block server-side (#968) — the row must say so.
        if (auto) sn.blocked = false;
      }
    });
    _mailboxNotice(auto
      ? `Mail from ${address} is now processed automatically.`
      : `Automatic processing for ${address} is off.`);
  } catch (e) {
    _mailboxNotice(`Could not change the setting: ${e.message || 'error'}`, true);
  }
}

async function deleteMail(uid) {
  const msg = _mailboxState.messages.find(m => m.uid === uid);
  const what = msg ? `“${msg.subject}”` : 'this mail';
  // Recoverable for 30 days in Gmail — the confirmation says so, because a
  // dialog that reads like a permanent deletion trains people to hesitate
  // over something that is in fact undoable.
  if (!await showConfirm(`Move ${what} to the Gmail trash? You can restore it there for 30 days.`)) return;
  try {
    const params = _mailboxState.uidvalidity
      ? `?uidvalidity=${encodeURIComponent(_mailboxState.uidvalidity)}` : '';
    await api('DELETE', `/api/mailbox/${encodeURIComponent(uid)}${params}`);
    _mailboxState.messages = _mailboxState.messages.filter(m => m.uid !== uid);
    _mailboxState.total = Math.max(0, _mailboxState.total - 1);
    _mailboxState.notice = { text: 'Moved to the Gmail trash.', error: false };
    _renderMailbox();
  } catch (e) {
    _mailboxNotice(`Could not delete: ${e.message || 'error'}`, true);
  }
}

async function blockMailSender(address, blocked, name) {
  if (blocked && !await showConfirm(
      `Block ${address}? Their mail disappears from this list and is never processed. No mail is deleted.`)) return;
  try {
    await api('PUT', '/api/mailbox/senders/block', { address, blocked, name });
    (_mailboxState.senders || []).forEach(sn => {
      if (sn.address === address) {
        sn.blocked = blocked;
        // Blocking clears the subscription server-side; showing it still on
        // here would be the client telling a different story than the row.
        if (blocked) sn.auto_process = false;
      }
    });
    _mailboxState.notice = { text: blocked
      ? `${address} is blocked — their mail is hidden and never processed.`
      : `${address} is unblocked.`, error: false };
    _renderMailboxSenders();
  } catch (e) {
    _mailboxNotice(`Could not change the setting: ${e.message || 'error'}`, true);
  }
}

// ── Archive (#1023) ──────────────────────────────────────────────────────────
// The app produced two things it never let him read back: every story it wrote,
// and every session he sat through. Both are already in the database (stories /
// review_log) — this view is the reader. It is deliberately NOT part of Browse:
// Browse is the chest of words he owns, this is the record of what happened.
const _ARCHIVE_TABS = [
  { key: 'stories',  icon: '\u{1F4D6}', label: 'Stories'  },
  { key: 'sessions', icon: '\u{1F551}', label: 'Sessions' },
];
const _CAT_ICON = { listening: '\u{1F3A7}', reading: '\u{1F4D6}', creating: '✍️', unified: '\u{1F500}' };
const _RATING_LABEL = { 1: 'Again', 2: 'Hard', 3: 'Good', 4: 'Easy' };

let _archiveState = { tab: 'stories', stories: null, sessions: null, storyId: null, openSession: null };

function _fmtDur(ms) {
  const s = Math.round((ms || 0) / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

function _fmtClock(ts) { return (ts || '').slice(11, 16); }
function _fmtDay(ts)   { return (ts || '').slice(0, 10); }

async function openArchive(tab = 'stories') {
  navPush(`archive:${tab}`);
  _archiveState.tab = tab;
  _archiveState.storyId = null;
  showView('archive');
  _renderArchive();
  // Each tab loads once per visit to this view; switching back is instant.
  try {
    if (tab === 'stories' && !_archiveState.stories) {
      const r = await api('GET', `/api/stories${_langQP('?')}`);
      _archiveState.stories = r.stories;
    } else if (tab === 'sessions' && !_archiveState.sessions) {
      const r = await api('GET', `/api/sessions${_langQP('?')}`);
      _archiveState.sessions = r.sessions;
    }
  } catch (e) {
    _archiveState.error = e.message;
  }
  if (_currentView === 'archive') _renderArchive();
}

function _archiveShell(body) {
  const tabs = _ARCHIVE_TABS.map(t =>
    `<button class="arch-tab${_archiveState.tab === t.key ? ' arch-tab-on' : ''}"
             onclick="openArchive('${t.key}')">${t.icon} ${t.label}</button>`).join('');
  return `<div class="arch-head">
      <div class="arch-title"><h2>\u{1F4DC} Archive</h2>
        <span class="arch-sub">Everything the app wrote, and every session you sat through</span></div>
      <div class="arch-tabs">${tabs}</div>
    </div>${body}`;
}

function _renderArchive() {
  const box = document.getElementById('view-archive-content');
  if (!box) return;
  if (_archiveState.error) {
    box.innerHTML = _archiveShell(`<div class="browse-empty">Could not load: ${_escHtml(_archiveState.error)}</div>`);
    _archiveState.error = null;
    return;
  }
  if (_archiveState.storyId != null) { box.innerHTML = _archiveStoryHtml(); return; }
  const data = _archiveState.tab === 'stories' ? _archiveState.stories : _archiveState.sessions;
  if (!data) { box.innerHTML = _archiveShell('<div class="browse-empty">Loading…</div>'); return; }
  box.innerHTML = _archiveShell(
    _archiveState.tab === 'stories' ? _archiveStoriesHtml(data) : _archiveSessionsHtml(data));
}

// ── Stories tab ──────────────────────────────────────────────────────────────
function _archiveStoriesHtml(stories) {
  if (!stories.length) return '<div class="browse-empty">No stories generated yet.</div>';
  let html = '', lastDay = null;
  for (const s of stories) {
    if (s.date !== lastDay) { html += `<div class="browse-section-label">${_escHtml(s.date)}</div>`; lastDay = s.date; }
    const meta = [
      s.deck_name, `${s.sentence_count} sentences`, s.mode,
      s.model, _fmtClock(s.generated_at),
    ].filter(Boolean).map(x => `<span>${_escHtml(String(x))}</span>`).join('<span class="ss-dot">·</span>');
    html += `<div class="bw-row arch-row" onclick="openArchiveStory(${s.id})">
      <div class="arch-cat" title="${_escHtml(s.category)}">${_CAT_ICON[s.category] || '\u{1F4C4}'}</div>
      <div class="ss-main">
        <div class="arch-row-title">${_escHtml(s.topic || s.category)}</div>
        <div class="ss-meta">${meta}</div>
      </div>
    </div>`;
  }
  return `<div class="bw-list">${html}</div>`;
}

async function openArchiveStory(storyId) {
  navPush(`archive:story:${storyId}`);
  _archiveState.storyId = storyId;
  _archiveState.story = null;
  showView('archive');
  _renderArchive();
  try {
    _archiveState.story = await api('GET', `/api/stories/${storyId}`);
  } catch (e) {
    _archiveState.storyId = null;
    _archiveState.error = e.message;
  }
  if (_currentView === 'archive') _renderArchive();
}

function closeArchiveStory() {
  _archiveState.storyId = null;
  _archiveState.story = null;
  _renderArchive();
}

function _archiveStoryHtml() {
  const st = _archiveState.story;
  const back = `<button class="btn-secondary arch-back" onclick="closeArchiveStory()">← Stories</button>`;
  if (!st) return `${back}<div class="browse-empty">Loading…</div>`;
  const meta = [st.date, st.deck_name, st.category, st.mode, st.model, st.topic]
    .filter(Boolean).map(x => `<span>${_escHtml(String(x))}</span>`).join('<span class="ss-dot">·</span>');
  const prompt = st.has_prompt
    ? `<button class="ss-prompt-btn" onclick="showStoryPrompt(${st.id})">\u{1F4DD} Prompt</button>`
    : `<button class="ss-prompt-btn" disabled title="No prompt stored for this story">\u{1F4DD} Prompt</button>`;
  const rows = (st.sentences || []).map(s => {
    const trans = s.sentence_de || s.sentence_fr || s.sentence_en || '';
    // The target words are what makes an archived story worth reading back:
    // each one opens its entry.
    const words = (s.words || []).map(w =>
      `<button class="arch-word" onclick="openWordDetail(${w.word_id})">${_escHtml(w.word_zh)}</button>`).join('');
    return `<div class="arch-sent">
      <div class="arch-sent-zh">${_escHtml(s.sentence_zh)}</div>
      ${trans ? `<div class="arch-sent-tr">${_escHtml(trans)}</div>` : ''}
      ${words ? `<div class="arch-sent-words">${words}</div>` : ''}
    </div>`;
  }).join('');
  return `${back}
    <div class="arch-story-head">
      <h2>${_escHtml(st.topic || st.category)}</h2>
      <div class="ss-meta">${meta}</div>
      <div class="arch-story-actions">${prompt}</div>
    </div>
    <div class="arch-sentences">${rows || '<div class="browse-empty">This story has no sentences.</div>'}</div>`;
}

// ── Sessions tab ─────────────────────────────────────────────────────────────
function _archiveSessionsHtml(sessions) {
  if (!sessions.length) return '<div class="browse-empty">No reviews logged yet.</div>';
  let html = '', lastDay = null;
  for (const s of sessions) {
    const day = _fmtDay(s.started_at);
    if (day !== lastDay) { html += `<div class="browse-section-label">${_escHtml(day)}</div>`; lastDay = day; }
    const open = _archiveState.openSession === s.started_at;
    // The rating split as one bar: how a session went is a shape, not four numbers.
    const bar = [1, 2, 3, 4].map(r => {
      const n = s.ratings[String(r)] || 0;
      if (!n) return '';
      return `<span class="arch-bar-seg arch-r${r}" style="flex:${n}" title="${_RATING_LABEL[r]}: ${n}"></span>`;
    }).join('');
    const meta = [
      `${_fmtClock(s.started_at)}–${_fmtClock(s.ended_at)}`,
      `${s.count} reviews`,
      `${s.unique_cards} cards`,
      _fmtDur(s.duration_ms || s.elapsed_ms),
      s.retention != null ? `${s.retention}% retention` : null,
      Object.keys(s.categories || {}).map(c => _CAT_ICON[c] || c).join(' '),
    ].filter(Boolean).map(x => `<span>${_escHtml(String(x))}</span>`).join('<span class="ss-dot">·</span>');
    html += `<div class="bw-row arch-row" onclick="toggleArchiveSession('${s.started_at}')">
      <div class="ss-main">
        <div class="ss-meta">${meta}</div>
        <div class="arch-bar">${bar}</div>
      </div>
      <div class="arch-chev">${open ? '▾' : '▸'}</div>
    </div>`;
    if (open) {
      const words = (s.words || []).map(w =>
        `<button class="arch-word arch-r${w.rating}-tint" title="${_RATING_LABEL[w.rating]}"
                 onclick="event.stopPropagation();openWordDetail(${w.word_id})">${_escHtml(w.word_zh)}</button>`).join('');
      html += `<div class="arch-session-words">${words || '<span class="arch-sub">No words recorded.</span>'}</div>`;
    }
  }
  return `<div class="bw-list">${html}</div>`;
}

function toggleArchiveSession(startedAt) {
  _archiveState.openSession = _archiveState.openSession === startedAt ? null : startedAt;
  _renderArchive();
}
