// Form cards derive conj_ok from the rating instead of showing the richtig/falsch row
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
const dStart = source.indexOf('function _derivedConjOk');
const dEnd = source.indexOf('function _renderConjRatingRow');
vm.runInContext(source.slice(dStart, dEnd), context);

const formCard = {note_type: 'grammar', word_zh: 'tener · Indefinido'};
const sent = {id: 7, conj_rating: true, conj_pattern: 'ind_pret', };

test('form card: Again -> 0, Hard/Good/Easy -> 1', () => {
  assert.equal(context._derivedConjOk(formCard, sent, 1), 0);
  assert.equal(context._derivedConjOk(formCard, sent, 2), 1);
  assert.equal(context._derivedConjOk(formCard, sent, 3), 1);
  assert.equal(context._derivedConjOk(formCard, sent, 4), 1);
});

test('normal card -> null', () => {
  const normal = {note_type: 'vocabulary', word_zh: 'tener'};
  assert.equal(context._derivedConjOk(normal, sent, 3), null);
});

test('sentence without conj_pattern -> null', () => {
  assert.equal(context._derivedConjOk(formCard, {id: 7, conj_rating: true}, 3), null);
  assert.equal(context._derivedConjOk(formCard, null, 3), null);
});

test('_renderConjRatingRow hides the row for form cards', () => {
  const s = source.indexOf('function _renderConjRatingRow');
  const body = source.slice(s, source.indexOf('function setConjChoice'));
  assert.ok(body.includes('formCardLemma'));
});

// Normal verb cards: richtig is preselected, only ✗ needs a click
test('conj rating row defaults to richtig', () => {
  const ctx = {formCardLemma: () => null, _conjChoice: null, _conjPendingRating: null, rated: []};
  const row = {style: {display: 'none'}, classList: {remove() {}, add() {}}};
  const lbl = {textContent: ''};
  ctx.document = {
    getElementById: id => (id === 'conj-rating-row' ? row : id === 'conj-rating-pattern' ? lbl : null),
    querySelectorAll: () => [],
  };
  vm.createContext(ctx);
  const s = source.indexOf('function _setConjPendingRating');
  const e = source.indexOf('// ── Submit rating');
  vm.runInContext(source.slice(s, e).replace(/\blet _conj/g, 'var _conj'), ctx);
  ctx.rate = r => ctx.rated.push(r);
  vm.runInContext('var card = {}; var sentence = {id: 3, conj_rating: true, conj_pattern: "reg:ind_pret:ar"};', ctx);
  ctx._renderConjRatingRow();
  assert.equal(vm.runInContext('_conjChoice', ctx), true);
  ctx.setConjChoice(false);
  assert.equal(vm.runInContext('_conjChoice', ctx), false);
  ctx.setConjChoice(false);   // clicking the active ✗ again returns to ✓
  assert.equal(vm.runInContext('_conjChoice', ctx), true);
  ctx.setConjChoice(true);    // ✓ stays ✓
  assert.equal(vm.runInContext('_conjChoice', ctx), true);
});
