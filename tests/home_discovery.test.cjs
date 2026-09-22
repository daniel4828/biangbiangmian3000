const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup(api) {
  const root = { innerHTML: '' };
  const context = { api, Intl, Date, console, _currentView: 'decks',
    document: { getElementById: () => root, visibilityState: 'visible', addEventListener() {} },
    window: { addEventListener() {} }, setTimeout() { return 1; }, clearTimeout() {},
    _escHtml: s => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;') };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('static/home-discovery.js', 'utf8'), context);
  return { root, context };
}

test('renders coffee and exactly three safe internal recommendation links', async () => {
  const {root, context} = setup(async () => ({date: '2026-09-22', podcast: {id: 1, title: 'Coffee', can_listen: true},
    recommendations_visible: true, recommendations: [2,3,4].map(id => ({id, title: '<img onerror=x>', kind: 'article'}))}));
  await context.initHomeDiscovery();
  assert.match(root.innerHTML, /openHomePodcast\(1\)/);
  assert.equal((root.innerHTML.match(/class="home-pick"/g) || []).length, 3);
  assert.ok(!root.innerHTML.includes('<img'));
  assert.match(root.innerHTML, /#knowledge-2/);
});

test('morning hides recommendations and does not label an old episode as today', async () => {
  const {root, context} = setup(async () => ({date: '2026-09-22', podcast: null,
    recommendations_visible: false, recommendations: []}));
  await context.initHomeDiscovery();
  assert.match(root.innerHTML, /No episode for today/);
  assert.ok(!root.innerHTML.includes('From your knowledge'));
});

test('pending coffee opens details without starting an impossible listen request', async () => {
  const {root, context} = setup(async () => ({date: '2026-09-22', podcast: {id: 1, title: 'Coffee', can_listen: false},
    recommendations_visible: false, recommendations: []}));
  await context.initHomeDiscovery();
  assert.match(root.innerHTML, /Listen &amp; read is not ready yet/);
  assert.ok(!root.innerHTML.includes('openHomePodcast(1)'));
});

test('failed request offers retry', async () => {
  const {root, context} = setup(async () => {throw Error('offline');});
  await context.initHomeDiscovery();
  assert.match(root.innerHTML, /Retry/);
});

test('late response cannot replace a newer refresh', async () => {
  let resolveOld;
  let calls = 0;
  const {root, context} = setup(() => ++calls === 1 ? new Promise(r => resolveOld = r) :
    Promise.resolve({date: 'new', podcast: null, recommendations: []}));
  const old = context.initHomeDiscovery();
  await context.initHomeDiscovery();
  resolveOld({date: 'old', podcast: null, recommendations: []});
  await old;
  assert.match(root.innerHTML, /new/);
  assert.ok(!root.innerHTML.includes('old'));
});
