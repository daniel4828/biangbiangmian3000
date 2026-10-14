const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
function boot({ saved, dark = false, blocked = false } = {}) {
  const events = {}, listeners = {}, writes = [];
  const root = { dataset: {}, style: {} };
  const selector = { value: '', addEventListener: (name, fn) => { listeners[name] = fn; } };
  const media = { matches: dark, addEventListener: (_, fn) => { media.change = fn; } };
  const document = { documentElement: root, readyState: 'loading', getElementById: () => selector,
    addEventListener: (name, fn) => { events[name] = fn; } };
  const context = { document, window: { matchMedia: () => media }, localStorage: {
    getItem() { if (blocked) throw new Error('blocked'); return saved ?? null; },
    setItem(key, value) { if (blocked) throw new Error('blocked'); writes.push([key, value]); }
  } };
  const file = path.join(__dirname, '../static/theme.js');
  vm.runInNewContext(fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '', context);
  return { root, selector, writes, ready: () => events.DOMContentLoaded?.(),
    select(value) { selector.value = value; listeners.change?.(); },
    system(value) { media.matches = value; media.change?.(); } };
}
test('system theme applies before DOM readiness', () => {
  const app = boot({ dark: true });
  assert.equal(app.root.dataset.theme, 'dark');
  assert.equal(app.root.style.colorScheme, 'dark');
  app.ready(); assert.equal(app.selector.value, 'system');
});
test('saved manual light wins over OS dark', () => {
  const app = boot({ saved: 'light', dark: true }); app.ready();
  assert.equal(app.root.dataset.theme, 'light');
  assert.equal(app.selector.value, 'light');
  app.system(true); assert.equal(app.root.dataset.theme, 'light');
});
test('selector persists and system selection resumes OS tracking', () => {
  const app = boot(); app.ready(); app.select('dark');
  assert.equal(app.root.dataset.theme, 'dark');
  assert.deepEqual(app.writes.at(-1), ['theme', 'dark']);
  app.select('system'); assert.equal(app.root.dataset.theme, 'light');
  app.system(true); assert.equal(app.root.dataset.theme, 'dark');
  assert.equal(app.selector.value, 'system');
});
test('invalid saved value uses system', () => {
  const app = boot({ saved: 'invalid', dark: true }); app.ready();
  assert.equal(app.root.dataset.theme, 'dark'); assert.equal(app.selector.value, 'system');
});
test('storage failures allow immediate and manual theme changes', () => {
  const app = boot({ blocked: true, dark: true }); app.ready();
  assert.equal(app.root.dataset.theme, 'dark');
  assert.doesNotThrow(() => app.select('light'));
  assert.equal(app.root.dataset.theme, 'light');
});
test('HTML loads theme before stylesheet and has labelled native selector', () => {
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  assert.ok(html.indexOf('/static/theme.js') > 0);
  assert.ok(html.indexOf('/static/theme.js') < html.indexOf('/static/style.css'));
  assert.match(html, /<select[^>]*id="theme-select"[^>]*aria-label="[^"]+"/);
});
