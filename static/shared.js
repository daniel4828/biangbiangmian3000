// Shared by index.html (the full app) and add.html (the standalone /add page).
// Loaded as a plain script in both, so everything here lives on the global
// scope exactly like app.js does.

async function api(method, path, body) {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  // Session cookie expired or was cleared (#666): send the user to the login
  // form instead of letting every view fail with an unexplained error.
  if (r.status === 401) {
    location.href = '/login';
    throw new Error(`${method} ${path} → 401 (redirecting to login)`);
  }
  if (!r.ok) {
    // Carry the server's own explanation through (#937): "PATCH … → 400" tells
    // Daniel nothing, "published_at must be YYYY-MM-DD" tells him exactly what
    // to fix. Best-effort — an error body that isn't JSON just doesn't add one.
    let detail = '';
    try { detail = (await r.json())?.detail || ''; } catch (e) { /* not JSON */ }
    throw new Error(detail || `${method} ${path} → ${r.status}`);
  }
  return r.json();
}

// The one way to add a word anywhere in the app (#643): the full DeepSeek
// pipeline behind /api/add-word-ai, which writes a complete de-zh-bot entry
// (examples, character breakdown, measure words, synonyms, etymology) through
// the ordinary importer. The old /api/quick-add-word only filled four fields
// and — worse — reported success even when cards has UNIQUE(word_id, category)
// silently dropped the insert for a word already studied elsewhere.
//
// Lives here rather than in app.js because /add (#668) is a standalone page
// that must not pull in the 9000-line app bundle: two copies of this polling
// logic would drift, and every fix would have to be made twice.
//
// Confirmation modal shown when /api/add-word-ai reports the word already
// exists and is about to move real cards (possibly wiping FSRS progress).
// Deliberately NOT app.js's showConfirm(): /add (#668) and /dict (#746) load
// shared.js WITHOUT app.js — being small is the whole point of those pages —
// so this has to be self-contained: plain DOM, inline styles, and colours read
// through var() fallback chains because the four pages name their theme
// variables differently (--fg vs --text, --line vs --border).
//
// info is the GET /api/word/{id} response (or null if that lookup failed —
// the confirmation still shows, just without the card overview). Resolves
// true (continue) or false (cancel).
function confirmExistingWord(info, action, wordZh, previousDecks, deckPath, reviewsDiscarded) {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.style.cssText =
      'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;' +
      'display:flex;align-items:center;justify-content:center;padding:1rem;';

    const box = document.createElement('div');
    box.style.cssText =
      'background:var(--card, #fff);color:var(--fg, var(--text, #1c1e21));' +
      'max-width:min(560px, 94vw);max-height:86vh;overflow:auto;' +
      'border-radius:12px;padding:1rem;box-shadow:0 10px 40px rgba(0,0,0,.3);' +
      'font-family:inherit;';

    const title = document.createElement('div');
    title.style.cssText = 'font-weight:600;font-size:1.05em;margin-bottom:.5rem;';
    title.textContent = `${wordZh} — already in your collection`;
    box.appendChild(title);

    const warnLines = {
      reset: `↺ Reset to new: cards move from ${previousDecks.join(', ')} → ${deckPath}. ` +
             `This discards all scheduling progress (${reviewsDiscarded} review${reviewsDiscarded === 1 ? '' : 's'}).`,
      listed: `★ Move to list: cards move from ${previousDecks.join(', ')} → Saved and are suspended. ` +
              `Scheduling progress is kept.`,
      promoted: `✓ Promote: cards move from Saved → ${deckPath}.`,
    };
    const warn = document.createElement('div');
    warn.style.cssText = 'color:#c0392b;font-weight:500;margin-bottom:.75rem;';
    warn.textContent = warnLines[action] || `Cards move to ${deckPath}.`;
    box.appendChild(warn);

    if (info) {
      const head = document.createElement('div');
      head.style.cssText = 'margin-bottom:.5rem;';
      const wordLine = document.createElement('div');
      wordLine.style.cssText = 'font-weight:600;';
      let wordText = info.word_zh || wordZh;
      if (info.pinyin) wordText += `  (${info.pinyin})`;
      wordLine.textContent = wordText;
      head.appendChild(wordLine);
      const def = info.definition_de || info.definition;
      if (def) {
        const defLine = document.createElement('div');
        defLine.style.cssText = 'color:var(--muted, #6b7075);font-size:.92em;';
        defLine.textContent = def;
        head.appendChild(defLine);
      }
      box.appendChild(head);

      const cards = Array.isArray(info.cards) ? info.cards : [];
      if (cards.length) {
        const table = document.createElement('table');
        table.style.cssText =
          'width:100%;border-collapse:collapse;font-size:.85em;margin-bottom:.75rem;';
        const cols = [
          ['category', 'category'], ['deck_path', 'deck'], ['state', 'state'],
          ['due', 'due'], ['interval', 'interval (d)'], ['lapses', 'lapses'],
          ['repetitions', 'reps'],
        ];
        const thead = document.createElement('tr');
        for (const [, label] of cols) {
          const th = document.createElement('th');
          th.style.cssText =
            'text-align:left;border-bottom:1px solid var(--line, var(--border, #d7d9dd));' +
            'padding:.25rem .4rem;font-weight:600;';
          th.textContent = label;
          thead.appendChild(th);
        }
        table.appendChild(thead);
        for (const card of cards) {
          const tr = document.createElement('tr');
          for (const [key] of cols) {
            const td = document.createElement('td');
            td.style.cssText =
              'padding:.25rem .4rem;border-bottom:1px solid var(--line, var(--border, #d7d9dd));';
            const v = card[key];
            td.textContent = (v === null || v === undefined || v === '') ? '–' : String(v);
            tr.appendChild(td);
          }
          table.appendChild(tr);
        }
        box.appendChild(table);
      }
    }

    const btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex;justify-content:flex-end;gap:.5rem;margin-top:.5rem;';

    const cancelBtn = document.createElement('button');
    cancelBtn.textContent = 'Cancel';
    cancelBtn.style.cssText =
      'padding:.5rem 1rem;border-radius:8px;border:1px solid var(--line, var(--border, #d7d9dd));' +
      'background:transparent;color:inherit;cursor:pointer;';

    const okBtn = document.createElement('button');
    okBtn.textContent = action === 'reset' ? 'Reset anyway' : 'Continue';
    okBtn.style.cssText = action === 'reset'
      ? 'padding:.5rem 1rem;border-radius:8px;border:none;background:#c0392b;color:#fff;cursor:pointer;'
      : 'padding:.5rem 1rem;border-radius:8px;border:none;background:var(--fg, var(--text, #1c1e21));' +
        'color:var(--card, #fff);cursor:pointer;';

    btnRow.appendChild(cancelBtn);
    btnRow.appendChild(okBtn);
    box.appendChild(btnRow);
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    const cleanup = (result) => {
      document.removeEventListener('keydown', onKeydown);
      overlay.remove();
      resolve(result);
    };
    const onKeydown = (e) => { if (e.key === 'Escape') cleanup(false); };
    document.addEventListener('keydown', onKeydown);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(false); });
    cancelBtn.addEventListener('click', () => cleanup(false));
    okBtn.addEventListener('click', () => cleanup(true));
  });
}

// onUpdate(state, text) is called with 'running' | 'done' | 'error' | 'idle'.
// 'idle' (#888) means the user cancelled a confirmation and nothing happened
// at all — callers must reset any "Generating…" UI back to its start state.
// lang (#726) picks the language's prompt and deck tree; omitted means Chinese,
// so every pre-#726 call site keeps behaving exactly as before. A word already
// in the database ignores it and moves inside its own language's tree.
async function addWordViaAi(wordZh, day, onUpdate, lang) {
  const post = (confirm) => {
    const body = { word_zh: wordZh, day };
    if (lang) body.lang = lang;
    if (confirm) body.confirm = true;
    return api('POST', '/api/add-word-ai', body);
  };

  let result;
  try {
    result = await post(false);
  } catch (e) {
    onUpdate('error', e.message || 'Failed to add word');
    return;
  }

  if (result.status === 'needs_confirmation') {
    const info = await api('GET', `/api/word/${result.entry_id}`).catch(() => null);
    const proceed = await confirmExistingWord(
      info, result.action, result.word_zh, result.previous_decks,
      result.deck_path, result.reviews_discarded,
    );
    if (!proceed) {
      onUpdate('idle', '');
      return;
    }
    try {
      result = await post(true);
    } catch (e) {
      onUpdate('error', e.message || 'Failed to add word');
      return;
    }
  }

  // The deck list only exists in the full app; /add has nothing to refresh.
  // keepView (#695): generation finishes ~30s later, quite possibly mid-review
  // — refresh the due counts, never switch the view out from under the user.
  const refreshDecks = () => {
    if (typeof loadDecks === 'function') loadDecks({ keepView: true });
  };

  // Known words come back finished — no AI call, no job to poll.
  if (!result.job_id) {
    if (result.status === 'already_listed') {
      onUpdate('done', '★ already on your list', result.deck_path);
    } else if (result.status === 'listed') {
      // Parked from a real deck: suspended, so it stops coming up for review.
      onUpdate('done', `★ moved to list from ${result.previous_decks.join(', ')}`,
               result.deck_path);
    } else if (result.status === 'reset') {
      // The cards were moved here from somewhere they had real progress
      // (#675). That progress is gone for good, so name what was thrown away
      // instead of reporting a bland success.
      const from = result.previous_decks.join(', ');
      const lost = result.reviews_discarded
        ? `, ${result.reviews_discarded} review${result.reviews_discarded === 1 ? '' : 's'} discarded`
        : '';
      onUpdate('done', `↺ reset from ${from} → ${result.deck_path}${lost}`, result.deck_path);
    } else {
      onUpdate('done', `✓ moved from Saved → ${result.deck_path}`, result.deck_path);
    }
    refreshDecks();
    return;
  }

  onUpdate('running', 'Generating…');
  const poll = async () => {
    const job = await api('GET', `/api/add-word-ai/progress/${result.job_id}`).catch(() => null);
    if (!job || job.status === 'running') {
      setTimeout(poll, 1500);
      return;
    }
    if (job.status === 'error') {
      onUpdate('error', job.error || 'Failed to add word');
      return;
    }
    // A "done" job that imported nothing means the AI produced an entry the
    // importer rejected — say so instead of claiming success.
    if (!job.summary || !job.summary.imported) {
      onUpdate('error', 'could not be imported — check the logs');
      return;
    }
    onUpdate('done', day === 'list' ? '★ added to your list' : `✓ ${result.deck_path}`,
             result.deck_path);
    refreshDecks();
  };
  setTimeout(poll, 1500);
}

// Knowledge-base ingestion, shared by the app's Knowledge tab and the
// standalone /save page (#681). Same reasoning as addWordViaAi above: one
// client-side path, so a fix lands in both places at once.
//
// payload is either {url} or {text, title?, author?, source_url?} (#833 —
// everything but the body is optional; the server fills the blanks with one
// cheap AI pass and falls back to the body's first line for the title. The
// client deliberately does NOT pre-compute a title any more: two copies of
// that rule would eventually disagree). Returns the server's response
// ({episode_id} or {status:'already_exists', episode_id}) and, for anything
// newly ingested, kicks off transcription/summarising in the background —
// POST /api/knowledge/add deliberately only stores the row.
//
// confirmFn (#1054), only relevant when payload.as_audiobook is true: called
// with the server's {"status":"confirm_required", duration_seconds, title}
// response when a YouTube video's length couldn't be confirmed short — must
// return/resolve a boolean. Returning false (or omitting confirmFn entirely,
// which should never happen for as_audiobook but must not crash if it does)
// resolves to {status:'cancelled'} without downloading anything; true
// re-submits the same payload with confirm_long:true, which is what actually
// triggers the download + local-ASR queueing server side.
async function ingestKnowledge(payload, confirmFn) {
  const path = payload.url ? '/api/knowledge/add' : '/api/knowledge/add-text';
  let res = await api('POST', path, payload);
  if (res?.status === 'confirm_required') {
    const proceed = confirmFn ? await confirmFn(res) : false;
    if (!proceed) return { status: 'cancelled' };
    res = await api('POST', path, { ...payload, confirm_long: true });
  }
  if (res?.status !== 'already_exists' && res?.status !== 'cancelled' && res?.episode_id != null) {
    api('POST', `/api/podcast/episodes/${res.episode_id}/process`).catch(() => {});
  }
  return res;
}

// File upload counterpart of ingestKnowledge (#835): .txt/.md/.pdf/.docx.
// Same response contract and the same "kick off processing" follow-up, so a
// caller treats an upload exactly like a paste. `fields` carries the same
// optional {title, author, source_url, china_critical} the paste box sends.
async function ingestKnowledgeFile(file, fields) {
  const form = new FormData();
  form.append('file', file);
  for (const [key, value] of Object.entries(fields || {})) {
    if (value) form.append(key, value === true ? 'true' : value);
  }
  // Not api(): that helper sends JSON. A multipart body must go out with the
  // browser's own boundary header, so it is built by hand here — including
  // the same 401 -> login redirect api() does.
  const res = await fetch('/api/knowledge/add-file', { method: 'POST', body: form });
  if (res.status === 401) {
    window.location.href = '/login';
    throw new Error('Not logged in');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  if (data?.status !== 'already_exists' && data?.episode_id != null) {
    api('POST', `/api/podcast/episodes/${data.episode_id}/process`).catch(() => {});
  }
  return data;
}

// Direct audio file upload (#1068): .mp3/.m4a/.wav, straight to
// POST /api/knowledge/add-audio which streams it to disk and queues it for
// local ASR transcription — see knowledge/audio_upload.py's docstring for
// why this does not go through ingest_text() the way ingestKnowledgeFile()
// does. Unlike that function, there is no follow-up .../process call: the
// upload is queued for TRANSCRIPTION, not summarization, and summarizing
// only makes sense once a transcript exists.
//
// Uses XMLHttpRequest instead of fetch() because fetch has no upload
// progress event at all — an audiobook can take minutes to upload, and with
// no feedback that looks exactly like a hang. onProgress(fraction 0..1) is
// called as the browser reports it; pass null/undefined to skip it.
function ingestKnowledgeAudio(file, fields, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('file', file);
    for (const [key, value] of Object.entries(fields || {})) {
      if (value) form.append(key, value === true ? 'true' : value);
    }
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/knowledge/add-audio');
    if (xhr.upload && onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded / e.total);
      };
    }
    xhr.onerror = () => reject(new Error('Upload failed (network error)'));
    xhr.onload = () => {
      if (xhr.status === 401) {
        window.location.href = '/login';
        reject(new Error('Not logged in'));
        return;
      }
      let data = {};
      try { data = JSON.parse(xhr.responseText || '{}'); } catch (e) { /* not JSON */ }
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new Error(data.detail || `HTTP ${xhr.status}`));
        return;
      }
      resolve(data);
    };
    xhr.send(form);
  });
}

// Mark a word as already known (#710) so zh_annotate stops flagging it in
// future summaries. Shared by the HSK word table and the in-text word menu
// (#711) for the same reason as addWordViaAi above: one client-side path.
//
// Deliberately NOT a card: this is the "I know this, stop showing it to me"
// action, the opposite of adding it to a deck. Fire-and-await — the caller
// updates its own UI optimistically and reports a rejected promise as an
// error rather than silently leaving a wrong ✓ on screen.
//
// lang (#804) keys known_words per language, same reasoning as addWordViaAi's
// lang param — omitted means Chinese, so every pre-#804 call site is unaffected.
async function markWordKnown(word, lang) {
  return api('POST', '/api/known-words', lang ? { word, lang } : { word });
}

// ---------------------------------------------------------------------------
// Dictionary result rendering (#746, extracted for #1055).
//
// Originally lived only in dict.html — the header search box now needs the
// exact same rendering (candidate options, the ★ add button, the sentence
// card, ↻ Repeat), and this repo's rule is one implementation per feature
// (#643/#668): two copies would drift, and a fix to how an option renders
// would only land in whichever page someone happened to touch.
//
// Styling lives in dict-result.css under the .dr-root/.dr-* classes; the
// caller's page must load that stylesheet and give `container` the dr-root
// class (renderDictResult does this itself, defensively).

// ★ button: adds a candidate's zh to the ★ List via the one shared add-word
// pipeline (#643). Each button tracks its own state so several can be
// "running" (~30s each) at once without blocking one another — addWordViaAi
// already polls its own job, we just need to not share state.
function _dictStarButton(zh, wordLang) {
  const btn = document.createElement('button');
  btn.className = 'dr-star';
  btn.type = 'button';
  btn.textContent = '★';
  btn.title = `Add "${zh}" to your ★ List`;
  const errMsg = document.createElement('div');
  errMsg.className = 'dr-star-err-msg';
  errMsg.style.display = 'none';

  btn.onclick = () => {
    btn.disabled = true;
    btn.classList.add('dr-running');
    btn.textContent = '…';
    errMsg.style.display = 'none';
    addWordViaAi(zh, 'list', (state, text) => {
      if (state === 'running') {
        btn.classList.add('dr-running');
        btn.textContent = '…';
      } else if (state === 'done') {
        btn.classList.remove('dr-running');
        btn.classList.add('dr-done');
        btn.textContent = '✓';
        btn.title = text || 'Added';
      } else if (state === 'error') {
        btn.classList.remove('dr-running');
        btn.classList.add('dr-error');
        btn.textContent = '⚠';
        btn.disabled = false;
        errMsg.textContent = text || 'Failed to add';
        errMsg.style.display = '';
      } else if (state === 'idle') {
        // Cancelled the "already in your collection" confirmation (#888) —
        // nothing happened, so put the button back to its start state.
        btn.classList.remove('dr-running');
        btn.textContent = '★';
        btn.disabled = false;
      }
    }, wordLang);
  };

  const wrap = document.createElement('div');
  wrap.style.flexShrink = '0';
  wrap.append(btn, errMsg);
  return wrap;
}

function _dictTextEl(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

function _dictOption(opt, wordLang) {
  const row = document.createElement('div');
  row.className = 'dr-option' + (opt.recommended ? ' dr-recommended' : '');

  row.append(_dictTextEl('div', 'dr-key', opt.key || ''));

  const body = document.createElement('div');
  body.className = 'dr-body';

  const zhRow = _dictTextEl('div', 'dr-zh-row');
  zhRow.append(_dictTextEl('span', 'dr-zh', opt.zh || ''));
  if (opt.pinyin) zhRow.append(_dictTextEl('span', 'dr-pinyin', opt.pinyin));
  if (opt.recommended) zhRow.append(_dictTextEl('span', 'dr-star-mark', '⭐'));
  if (opt.register) zhRow.append(_dictTextEl('span', 'dr-register', opt.register));
  body.append(zhRow);

  if (opt.de) {
    const t = _dictTextEl('div', 'dr-trans');
    t.append(_dictTextEl('span', 'dr-lbl', 'de: '), document.createTextNode(opt.de));
    body.append(t);
  }
  if (opt.fr) {
    const t = _dictTextEl('div', 'dr-trans');
    t.append(_dictTextEl('span', 'dr-lbl', 'fr: '), document.createTextNode(opt.fr));
    body.append(t);
  }
  if (opt.usage) body.append(_dictTextEl('div', 'dr-usage', opt.usage));

  if (opt.example_zh) {
    const ex = document.createElement('div');
    ex.className = 'dr-example';
    ex.append(_dictTextEl('div', 'dr-ex-zh', opt.example_zh));
    if (opt.example_pinyin) ex.append(_dictTextEl('div', 'dr-ex-pinyin', opt.example_pinyin));
    if (opt.example_de) ex.append(_dictTextEl('div', 'dr-ex-de', opt.example_de));
    body.append(ex);
  }

  row.append(body);
  if (opt.zh) row.append(_dictStarButton(opt.zh, wordLang));
  return row;
}

// Renders one {id, created_at, query, lang, result} record — the exact
// response shape of POST /api/dict/lookup and GET /api/dict/history/{id} —
// into `container`. Everything from the AI is untrusted text: textContent
// and createElement only, never innerHTML, all the way through this
// function and its helpers above.
//
// opts.onRepeat(record), if given, renders a ↻ Repeat button that re-asks
// the exact same query and is expected to overwrite this stored answer
// (replace_id, #777) — omit it where "ask again" doesn't make sense.
// opts.addLang picks the language the ★ button adds the word in when the
// record itself doesn't already say (record.lang wins when present, same
// as dict.html's old `wordLang || lang` fallback).
function renderDictResult(container, record, opts) {
  opts = opts || {};
  container.classList.add('dr-root');
  container.replaceChildren();

  const r = record.result || {};
  // The record's own lang (the language this answer was given in) wins —
  // exactly dict.html's pre-extraction `wordLang || lang` behaviour, where
  // wordLang was always record.lang and `lang` was the page-level picker
  // variable. opts.addLang plays that page-level fallback role now, for
  // records that (e.g. pre-#805) don't carry a lang at all.
  const wordLang = record.lang || opts.addLang || 'zh';

  const head = document.createElement('div');
  const hrow = _dictTextEl('div', 'dr-headline-row');
  hrow.append(_dictTextEl('span', 'dr-headline', r.headline || record.query));
  if (r.headline_pinyin) hrow.append(_dictTextEl('span', 'dr-headline-pinyin', r.headline_pinyin));
  if (opts.onRepeat) {
    const repeat = document.createElement('button');
    repeat.className = 'dr-repeat';
    repeat.type = 'button';
    repeat.textContent = '↻ Repeat';
    repeat.title = 'Ask again — this stored answer is replaced';
    repeat.onclick = () => opts.onRepeat(record);
    hrow.append(repeat);
  }
  head.append(hrow);
  if (r.headline_de) head.append(_dictTextEl('div', 'dr-headline-de', r.headline_de));
  if (r.notes) head.append(_dictTextEl('div', 'dr-notes', r.notes));
  container.append(head);

  if (r.kind === 'sentence' && r.sentence) {
    const card = document.createElement('div');
    card.className = 'dr-sentence-card';
    const text = document.createElement('div');
    text.className = 'dr-sentence-text';
    text.append(_dictTextEl('div', 'dr-sent-zh', r.sentence.zh || ''));
    if (r.sentence.pinyin) text.append(_dictTextEl('div', 'dr-sent-pinyin', r.sentence.pinyin));
    if (r.sentence.de) text.append(_dictTextEl('div', 'dr-sent-de', r.sentence.de));
    card.append(text);
    if (r.sentence.zh) card.append(_dictStarButton(r.sentence.zh, wordLang));
    container.append(card);
  }

  for (const group of (r.groups || [])) {
    const g = document.createElement('div');
    g.className = 'dr-group';
    if (group.label) g.append(_dictTextEl('div', 'dr-group-label', group.label));
    for (const opt of (group.options || [])) {
      g.append(_dictOption(opt, wordLang));
    }
    container.append(g);
  }
}
