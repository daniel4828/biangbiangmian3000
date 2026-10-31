const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');

function load() {
  const start = source.indexOf('function renderMarkdown(');
  const end = source.indexOf('\n}', start) + 2;
  const ctx = vm.createContext({});
  vm.runInContext(source.slice(start, end), ctx);
  return ctx.renderMarkdown;
}

const count = (html, re) => (html.match(re) || []).length;

test('hard-wrapped prose becomes a single paragraph (#1350)', () => {
  const html = load()(
    'Spanisch rendir geht auf das lateinische *reddere* zurück, „zurückgeben,\n' +
    'übergeben“; die reflexive Form rendirse entwickelte daraus die Bedeutung\n' +
    '„sich ergeben“.');
  assert.equal(count(html, /<p>/g), 1);
  assert.ok(html.includes('„zurückgeben, übergeben“'));
  assert.ok(html.includes('<em>reddere</em>'));
});

test('structured Chinese notes keep their layout', () => {
  const html = load()([
    'Ein Substantiv für den Aktienmarkt. Wird in Wirtschaftsnachrichten verwendet.',
    '',
    '**Häufige Ausdrücke:**',
    '- 股市行情 (gǔshì hángqíng) — Börsenkurs',
    '- 股市大跌 (gǔshì dà diē) — Aktienmarkt stürzt ab',
    '',
    '**Kulturelle Anmerkung:**',
    'Die chinesischen Aktienmärkte sind sehr aktiv.',
  ].join('\n'));
  assert.equal(count(html, /<p>/g), 4);
  assert.ok(html.includes('<p><strong>Häufige Ausdrücke:</strong></p>'));
  assert.ok(html.includes('<p><strong>Kulturelle Anmerkung:</strong></p>'));
  assert.ok(html.includes('<p>Die chinesischen Aktienmärkte sind sehr aktiv.</p>'));
  assert.equal(count(html, /<ul>/g), 1);
  assert.equal(count(html, /<li>/g), 2);
  assert.equal(count(html, /<br>/g), 2);
});

test('labeled field starts a paragraph; next label starts another', () => {
  const html = load()('**Gebrauch:** foo\nbar\n**Register:** baz');
  assert.equal(count(html, /<p>/g), 2);
  assert.ok(html.startsWith('<p><strong>Gebrauch:</strong> foo bar</p>'));
  assert.ok(html.includes('<p><strong>Register:</strong> baz</p>'));
});

test('HTML in the text is still escaped', () => {
  const html = load()('a <script>x</script>\nb');
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});
