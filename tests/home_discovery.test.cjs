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

function playerSetup(api, existing = {}) {
  const {context} = setup(api);
  const events = [];
  Object.assign(context, {
    _raPlayer: {key: '', ...existing}, _raTrack: null, _raProgress: null,
    _raOwnerForEpisodeListen: ep => ({kind: 'episode', id: ep.id, lang: 'zh', variant: 'fulltext', title: ep.title}),
    _raIsActive: owner => context._raPlayer.ownerId === owner.id,
    _raSaveProgress: () => events.push('save'),
    _raSync: (owner, track) => {
      context._raPlayer = {key: 'loaded', ownerId: owner.id, audioUrl: track.audio_url,
        playing: false, activeIdx: -1, lastMs: 0, resumeMs: 0, resumeConsumed: false};
      return context._raPlayer;
    },
    _raOpenFullscreen: () => events.push('fullscreen'),
    showError: message => events.push('error:' + message),
    openKnowledgeItem: async () => events.push('details'),
    doStartListen: async () => events.push('build'),
    _raPlayAt: () => events.push('play'),
  });
  return {context, events};
}

test('home podcast restores saved progress in paused fullscreen without opening details', async () => {
  const {context, events} = playerSetup(async (method, path) => path.includes('/track?')
    ? {status: 'ready', track_id: 9, audio_url: '/audio.mp3', cues: [], duration_ms: 300000}
    : {position_ms: 123456, finished: false});
  await context.openHomePodcast(7);
  assert.equal(context._raPlayer.lastMs, 123456);
  assert.equal(context._raPlayer.resumeMs, 123456);
  assert.equal(context._raPlayer.playing, false);
  assert.ok(events.includes('fullscreen'));
  assert.ok(!events.includes('play'));
  assert.ok(!events.includes('details'));
});

test('already loaded podcast keeps current live position without refetching', async () => {
  const {context, events} = playerSetup(() => { throw Error('must not refetch'); },
    {key: 'existing', ownerId: 7, lastMs: 65432, playing: false});
  const player = context._raPlayer;
  await context.openHomePodcast(7);
  assert.equal(context._raPlayer, player);
  assert.equal(player.lastMs, 65432);
  assert.deepEqual(events, ['fullscreen']);
});

test('progress lookup failure is visible and does not replace the current player', async () => {
  const {context, events} = playerSetup(async (method, path) => {
    if (path.includes('/track?')) return {status: 'ready'};
    throw Error('progress unavailable');
  }, {key: 'existing', ownerId: 8, lastMs: 65432});
  const player = context._raPlayer;
  await context.openHomePodcast(7);
  assert.equal(context._raPlayer, player);
  assert.ok(events.some(x => x.startsWith('error:')));
  assert.ok(!events.includes('fullscreen'));
});

test('switching podcasts does not save progress from the shared audio a second time', async () => {
  const {context, events} = playerSetup(async (method, path) => path.includes('/track?')
    ? {status: 'ready', track_id: 9, audio_url: '/new.mp3', cues: [], duration_ms: 300000}
    : {position_ms: 12000, finished: false},
  {key: 'old', ownerId: 8, activeIdx: 2, lastMs: 65432, playing: false});
  await context.openHomePodcast(7);
  assert.ok(!events.includes('save'));
});

test('stale absent-track click cannot start building after a newer click', async () => {
  let finishDetails;
  const detailsDone = new Promise(resolve => { finishDetails = resolve; });
  let markDetailsStarted;
  const detailsStarted = new Promise(resolve => { markDetailsStarted = resolve; });
  const {context, events} = playerSetup(async (method, path) => {
    if (path.includes('owner_id=7')) return {status: 'absent'};
    if (path.includes('/track?')) return {status: 'ready', track_id: 10, audio_url: '/new.mp3', cues: []};
    return {position_ms: 0, finished: false};
  });
  context.openKnowledgeItem = async (id, preferView, isCurrent) => {
    events.push('details:' + id);
    markDetailsStarted();
    await detailsDone;
    if (isCurrent && !isCurrent()) return false;
    context._currentView = 'knowledge';
    context._knowledgeDetailId = id;
    context._knowledgeDetailEpisode = {id};
    return true;
  };
  const oldClick = context.openHomePodcast(7);
  await detailsStarted;
  await context.openHomePodcast(8);
  finishDetails();
  await oldClick;
  assert.ok(events.includes('fullscreen'));
  assert.ok(!events.includes('build'));
  assert.notEqual(context._knowledgeDetailId, 7);
});

test('stale progress failure stays silent after navigation changes', async () => {
  let rejectProgress;
  const progress = new Promise((resolve, reject) => { rejectProgress = reject; });
  let markProgressStarted;
  const progressStarted = new Promise(resolve => { markProgressStarted = resolve; });
  const {context, events} = playerSetup(async (method, path) => {
    if (path.includes('/track?')) return {status: 'ready', track_id: 9, audio_url: '/audio.mp3', cues: []};
    markProgressStarted();
    return progress;
  });
  const opening = context.openHomePodcast(7);
  await progressStarted;
  context._currentView = 'settings';
  rejectProgress(Error('late failure'));
  await opening;
  assert.ok(!events.some(x => x.startsWith('error:')));
});

test('navigation away cancels the pending absent-track detail fallback', async () => {
  let finishDetails;
  const detailsDone = new Promise(resolve => { finishDetails = resolve; });
  let markDetailsStarted;
  const detailsStarted = new Promise(resolve => { markDetailsStarted = resolve; });
  const {context, events} = playerSetup(async () => ({status: 'absent'}));
  context.openKnowledgeItem = async (id, preferView, isCurrent) => {
    context._currentView = 'loading';
    markDetailsStarted();
    await detailsDone;
    if (isCurrent && !isCurrent()) return false;
    context._currentView = 'knowledge';
    context._knowledgeDetailId = id;
    context._knowledgeDetailEpisode = {id};
    return true;
  };
  const opening = context.openHomePodcast(7);
  await detailsStarted;
  context._currentView = 'settings';
  finishDetails();
  await opening;
  assert.equal(context._currentView, 'settings');
  assert.ok(!events.includes('build'));
});

test('a newer loading screen invalidates the real knowledge-detail fallback token', async () => {
  const appSource = fs.readFileSync('static/app.js', 'utf8');
  const setLoadingSource = appSource.slice(
    appSource.indexOf('function setLoading('), appSource.indexOf('function setLoadingStep('));
  const openItemSource = appSource.slice(
    appSource.indexOf('async function openKnowledgeItem('), appSource.indexOf('function closeKnowledgeDetail('));
  let resolveEpisode;
  const episode = new Promise(resolve => { resolveEpisode = resolve; });
  const events = [];
  const element = () => ({style: {}, className: '', textContent: '', innerHTML: ''});
  const context = {
    _loadingContextToken: null,
    document: {getElementById: element},
    _renderLoadingSources() {}, showView: name => events.push('view:' + name),
    navPush() {}, _clearListenPoll() {}, activeLang: () => 'zh',
    api: async () => episode, _clearPodcastPoll() {},
    _renderKnowledgeDetail: () => events.push('render'),
    showError: message => events.push('error:' + message), openKnowledge() {},
    _knowledgeEditOpen: false, _knowledgeView: '', _knowledgeFulltext: null,
    _raTrack: null, _listenBuildingId: null, _listenErrors: {}, _knowledgeDetailId: null,
  };
  vm.createContext(context);
  vm.runInContext(setLoadingSource + '\n' + openItemSource, context);
  const opening = context.openKnowledgeItem(7, 'fulltext', () => true);
  await Promise.resolve();
  context.setLoading('Another navigation');
  resolveEpisode({id: 7});
  assert.equal(await opening, false);
  assert.ok(!events.includes('render'));
  assert.ok(!events.includes('view:knowledge'));
});
