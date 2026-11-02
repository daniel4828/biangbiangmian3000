// #1426: listening-hint settings are stored per language
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function setup(saved = {}) {
 const context = {localStorage: {getItem: k => saved[k] ?? null, setItem: (k,v) => saved[k] = String(v)},
  currentCardLang: () => 'zh', activeLang: () => 'zh', document: {getElementById: () => null}};
 vm.createContext(context);
 const source = fs.readFileSync('static/app.js','utf8');
 vm.runInContext(source.slice(source.indexOf('const _HINT_MIN'), source.indexOf('// The new words of the sentence')), context);
 return context;
}
const setLang = (c, lang) => { c.currentCardLang = () => lang; c.activeLang = () => lang; };

test('stage changes are written per language and do not leak', () => {
 const saved = {}; const c = setup(saved);
 const esBefore = (setLang(c, 'es'), Array.from(c._hintEnabledStages()).join(','));
 setLang(c, 'zh');
 c.changeHintStage(1, false);
 assert.ok(saved['listenHintStages:zh']);
 assert.equal(saved['listenHintStages'], undefined);
 assert.ok(!c._hintEnabledStages().includes(1));
 setLang(c, 'es');
 assert.equal(Array.from(c._hintEnabledStages()).join(','), esBefore);
 assert.ok(c._hintEnabledStages().includes(1));
 c.changeHintStage(2, false);
 assert.ok(saved['listenHintStages:es']);
 setLang(c, 'zh');
 assert.ok(c._hintEnabledStages().includes(2));
});

test('saved default is per language', () => {
 const c = setup({'listenHintState:zh': '2'});
 assert.equal(c._hintSavedDefault(), 2);
 setLang(c, 'es');
 assert.equal(c._hintSavedDefault(), 1);
});

test('saveListenHintDefault writes only the card language key', () => {
 const saved = {}; const c = setup(saved);
 c.document = {getElementById: id => id === 'listen-hint-slider' ? {value: 0} : null};
 const source = fs.readFileSync('static/app.js','utf8');
 vm.runInContext(source.slice(source.indexOf('function _updateHintStar'), source.indexOf('// ── Word bank tile count slider')), c);
 setLang(c, 'es');
 c.saveListenHintDefault();
 assert.equal(saved['listenHintState:es'], '0');
 assert.equal(saved['listenHintState:zh'], undefined);
 assert.equal(saved['listenHintState'], undefined);
});

test('legacy shared key is a fallback; a per-language key overrides only its language', () => {
 const c = setup({listenHintState: '2'});
 assert.equal(c._hintSavedDefault(), 2);
 setLang(c, 'es');
 assert.equal(c._hintSavedDefault(), 2);
 const d = setup({listenHintState: '2', 'listenHintState:es': '0'});
 assert.equal(d._hintSavedDefault(), 2);
 setLang(d, 'es');
 assert.equal(d._hintSavedDefault(), 0);
});

test('previous-word setting is per language', () => {
 const saved = {}; const c = setup(saved);
 c.changeHintPreviousWord(false);
 assert.equal(saved['listenHintPreviousWord:zh'], 'false');
 assert.equal(c._hintPreviousWordEnabled('zh'), false);
 assert.equal(c._hintPreviousWordEnabled('es'), true);
 assert.equal(c._hintPreviousWordEnabled(), false);
});
