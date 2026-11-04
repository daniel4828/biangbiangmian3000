// #1442: form cards must not show definition_de (the answer) on the front
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function formCardLemma');
const end = source.indexOf('async function renderWordBankUI');
const context = {};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);

test('formCardLemma extracts the infinitive of form cards only', () => {
  assert.equal(context.formCardLemma({note_type: 'grammar', word_zh: 'estar · Presente'}), 'estar');
  assert.equal(context.formCardLemma({note_type: 'grammar', word_zh: 'haber · Pretérito perfecto'}), 'haber');
  assert.equal(context.formCardLemma({note_type: 'vocabulary', word_zh: 'estar'}), '');
  assert.equal(context.formCardLemma({note_type: 'grammar', word_zh: '把字句'}), '');
  assert.equal(context.formCardLemma({note_type: 'vocabulary', word_zh: 'a · b'}), '');
  assert.equal(context.formCardLemma(null), '');
});

test('formCardHint gives the first German sense, infinitive as fallback (#1444)', () => {
  const base = {note_type: 'grammar', word_zh: 'ir · Presente', conjugation_row: 'voy, vas, va, vamos, vais, van'};
  assert.equal(context.formCardHint({...base, definition_de: 'gehen, fahren, ziehen'}), 'gehen');
  assert.equal(context.formCardHint({...base, definition_de: ''}), 'ir');
  const noRow = {note_type: 'grammar', word_zh: 'ir · Presente', definition_de: 'voy, vas, va, vamos, vais, van'};
  assert.equal(context.formCardHint(noRow), 'ir');
  assert.equal(context.formCardHint({note_type: 'vocabulary', word_zh: 'ir', definition_de: 'gehen'}), '');
});

test('word bank hint no longer uses definition_de unconditionally', () => {
  assert.ok(start !== -1 && end > start);
  assert.ok(!source.includes("const deHint = (card.definition_de || card.definition || '').trim();"));
});
