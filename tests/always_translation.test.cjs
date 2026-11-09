const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');
function setup(saved = '0') {
  const elements = {};
  for (const id of ['sentence-fr', 'sentence-de', 'sentence-en-front', 'always-trans-eye', 'toggle-trans-btn-front', 'always-trans-btn-front']) {
    const classes = new Set();
    elements[id] = {textContent: '', style: {display: 'none'}, classList: {toggle(k, on) {on ? classes.add(k) : classes.delete(k);}, contains(k) {return classes.has(k);}}, setAttribute(k, v) {this[k] = v;}};
  }
  const storage = {alwaysTranslation: saved};
  const ctx = {document: {getElementById: id => elements[id]}, localStorage: {getItem: k => storage[k], setItem: (k,v) => storage[k] = v}, _syncCardToggleBarFront() {}, _syncCardToggleBar() {}};
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf('function toggleTranslation()'), source.indexOf('// ── Creating-mode translation hint toggle')), ctx);
  return {ctx, elements, storage};
}
test('creating toolbar offers a persistent translation button before revealing the answer', () => {
  const html = fs.readFileSync('static/index.html', 'utf8');
  assert.match(html, /id="always-trans-btn-front"[^>]*onclick="toggleAlwaysTranslation\(\)"/);
  const {ctx, elements, storage} = setup();
  elements['sentence-en-front'].textContent = 'Ana hat Wasser auf den Tisch gestellt.';
  ctx._syncFrontTransToggle();
  assert.equal(elements['always-trans-btn-front'].style.display, '');
  ctx.toggleAlwaysTranslation();
  assert.equal(storage.alwaysTranslation, '1');
  assert.equal(elements['sentence-en-front'].style.display, 'block');
  assert.equal(elements['always-trans-btn-front']['aria-pressed'], 'true');
  ctx.toggleAlwaysTranslation();
  assert.equal(storage.alwaysTranslation, '0');
  assert.equal(elements['always-trans-btn-front']['aria-pressed'], 'false');
});
test('saved preference is reflected after reload; manually hiding clears it', () => {
  const {ctx, elements, storage} = setup('1');
  elements['sentence-en-front'].textContent = 'Translation';
  elements['sentence-en-front'].style.display = 'block';
  ctx._syncFrontTransToggle();
  assert.equal(elements['always-trans-btn-front'].classList.contains('active'), true);
  ctx.toggleCreatingFrontTranslation();
  assert.equal(storage.alwaysTranslation, '0');
  assert.equal(elements['always-trans-btn-front']['aria-pressed'], 'false');
  elements['sentence-en-front'].textContent = '';
  ctx._syncFrontTransToggle();
  assert.equal(elements['always-trans-btn-front'].style.display, 'none');
});
