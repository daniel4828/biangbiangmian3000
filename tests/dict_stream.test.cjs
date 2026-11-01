const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// shared.js only declares functions/consts at load time, so a bare context
// with a few stubs is enough.
function load() {
  const context = { document: {}, window: {}, localStorage: { getItem() { return null; } },
                    location: {}, console };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/shared.js'), 'utf8'), context);
  return context;
}
const { parsePartialJson } = load();
// Objects from the vm context have another realm's prototypes: compare via JSON.
const p = text => { const r = parsePartialJson(text); return r === null ? null : JSON.parse(JSON.stringify(r)); };

const FULL = JSON.stringify({
  input_lang: 'de', kind: 'phrase', headline: '派任务', headline_pinyin: 'pài rènwu',
  notes: 'He said \"hi\"\nand left \\ ok',
  groups: [{ label: 'assign', options: [
    { key: 'a', zh: '派', pinyin: 'pài', recommended: true, example_zh: '老师又给我派任务了。' },
    { key: 'b', zh: '安排', pinyin: 'ānpái', recommended: false },
  ] }],
});

test('empty and whitespace give null', () => {
  assert.equal(p(''), null);
  assert.equal(p('   \n'), null);
});

test('open brace gives empty object', () => {
  assert.deepEqual(p('{'), {});
});

test('half-written string value is kept', () => {
  assert.deepEqual(p('{"headline":"Zuck'), { headline: 'Zuck' });
});

test('dangling key/colon is dropped', () => {
  assert.deepEqual(p('{"headline":'), {});
  assert.deepEqual(p('{"headline":"a","no'), { headline: 'a' });
});

test('half literal is dropped', () => {
  assert.deepEqual(p('{"a":tru'), {});
});

test('key-less partial key inside nested option is dropped', () => {
  assert.deepEqual(p('{"groups":[{"options":[{"zh":"派","pin'),
    { groups: [{ options: [{ zh: '派' }] }] });
});

test('lone trailing backslash and short unicode escape are trimmed', () => {
  assert.deepEqual(p('{"a":"x\\'), { a: 'x' });
  assert.deepEqual(p('{"a":"x\\u00'), { a: 'x' });
  assert.deepEqual(p('{"a":"x\\\\'), { a: 'x\\' });
});

test('full valid JSON and fenced JSON', () => {
  assert.deepEqual(p(FULL), JSON.parse(FULL));
  assert.deepEqual(p('```json\n' + FULL + '\n```'), JSON.parse(FULL));
  assert.deepEqual(p('```json\n{"a":"b'), { a: 'b' });
});

test('every prefix parses without throwing; full text equals JSON.parse', () => {
  for (let i = 0; i <= FULL.length; i++) {
    const r = parsePartialJson(FULL.slice(0, i));
    assert.ok(r === null || typeof r === 'object', `prefix ${i}`);
  }
  assert.deepEqual(p(FULL), JSON.parse(FULL));
});
