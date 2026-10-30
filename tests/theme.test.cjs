const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
function boot({ saved, dark = false, blocked = false } = {}) {
  const writes = [];
  const root = { dataset: {}, style: {} };
  const media = { matches: dark, addEventListener: (_, fn) => { media.change = fn; } };
  const document = { documentElement: root };
  const window = { matchMedia: () => media };
  const context = { document, window, localStorage: {
    getItem() { if (blocked) throw new Error('blocked'); return saved ?? null; },
    setItem(key, value) { if (blocked) throw new Error('blocked'); writes.push([key, value]); }
  } };
  const file = path.join(__dirname, '../static/theme.js');
  vm.runInNewContext(fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '', context);
  return { root, writes, window,
    system(value) { media.matches = value; media.change?.(); } };
}
test('system theme applies immediately', () => {
  const app = boot({ dark: true });
  assert.equal(app.root.dataset.theme, 'dark');
  assert.equal(app.root.style.colorScheme, 'dark');
  assert.equal(app.window.getThemeChoice(), 'system');
});
test('saved manual light wins over OS dark', () => {
  const app = boot({ saved: 'light', dark: true });
  assert.equal(app.root.dataset.theme, 'light');
  assert.equal(app.window.getThemeChoice(), 'light');
  app.system(true); assert.equal(app.root.dataset.theme, 'light');
});
test('setThemeChoice persists and system choice resumes OS tracking', () => {
  const app = boot(); app.window.setThemeChoice('dark');
  assert.equal(app.root.dataset.theme, 'dark');
  assert.deepEqual(app.writes.at(-1), ['theme', 'dark']);
  app.window.setThemeChoice('system'); assert.equal(app.root.dataset.theme, 'light');
  app.system(true); assert.equal(app.root.dataset.theme, 'dark');
  assert.equal(app.window.getThemeChoice(), 'system');
});
test('invalid saved value uses system', () => {
  const app = boot({ saved: 'invalid', dark: true });
  assert.equal(app.root.dataset.theme, 'dark'); assert.equal(app.window.getThemeChoice(), 'system');
});
test('storage failures allow immediate and manual theme changes', () => {
  const app = boot({ blocked: true, dark: true });
  assert.equal(app.root.dataset.theme, 'dark');
  assert.doesNotThrow(() => app.window.setThemeChoice('light'));
  assert.equal(app.root.dataset.theme, 'light');
});
test('invalid value to setThemeChoice falls back to system', () => {
  const app = boot({ saved: 'light', dark: true });
  app.window.setThemeChoice('bogus');
  assert.equal(app.window.getThemeChoice(), 'system');
  assert.equal(app.root.dataset.theme, 'dark');
});
test('HTML loads theme before stylesheet; selector lives in settings (app.js)', () => {
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  assert.ok(html.indexOf('/static/theme.js') > 0);
  assert.ok(html.indexOf('/static/theme.js') < html.indexOf('/static/style.css'));
  assert.ok(!html.includes('id="theme-select"'));
  const js = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
  assert.match(js, /id="theme-select"[^>]*aria-label="[^"]+"/);
  assert.ok(js.includes('setThemeChoice('));
});
