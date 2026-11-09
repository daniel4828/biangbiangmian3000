// #1499: review mode opens the conjugation tense the current card actually uses
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('function _conjOpenTense');
const end = source.indexOf('function renderConjugationSection');
assert.ok(start !== -1 && end > start);
const context = {};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);

const TENSES = {
  'Presente': ['hago', 'haces'],
  'Pretérito Perfecto': ['he hecho', 'has hecho', 'hecho'],
  'Pretérito Indefinido': ['hice', 'hiciste'],
  'Presente de Subjuntivo': ['haga', 'hagas'],
};
const rows = ts => [new Map(Object.entries(ts).map(([t, fs_]) => [t, fs_.map(form => ({form}))]))][0];
const byTense = rows(TENSES);
const tenses = Object.keys(TENSES);
const open = (hint, text) => context._conjOpenTense(tenses, byTense, hint, text);

test('hint picks the tense, fewest extra tokens wins', () => {
  assert.equal(open('Presente · 1. Sg.', ''), 'Presente');
  assert.equal(open('Indefinido · 3. Sg.', ''), 'Pretérito Indefinido');
  assert.equal(open('Subjuntivo presente · 2. Sg.', ''), 'Presente de Subjuntivo');
  assert.equal(open('Perfecto · 1. Sg.', ''), 'Pretérito Perfecto');
});

test('without hint the sentence decides, longest form wins', () => {
  assert.equal(open('', 'Ayer hice la tarea.'), 'Pretérito Indefinido');
  assert.equal(open('', 'Ya he hecho todo.'), 'Pretérito Perfecto');
});

test('title match is accent-insensitive', () => {
  const bt = new Map([['Preterito Indefinido', [{form: 'hice'}]], ['Presente', [{form: 'hago'}]]]);
  assert.equal(context._conjOpenTense(['Preterito Indefinido', 'Presente'], bt, 'Indefinido', ''), 'Preterito Indefinido');
});

test('nothing matches -> null', () => {
  assert.equal(open('', 'Nada que ver.'), null);
  assert.equal(open('', ''), null);
  assert.equal(open('Condicional · 1. Sg.', ''), null);
});
