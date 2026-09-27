const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appSource = fs.readFileSync('static/app.js', 'utf8');
const htmlSource = fs.readFileSync('static/index.html', 'utf8');

function loadFunction(context, name) {
  const start = appSource.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `${name} must exist`);
  const end = appSource.indexOf('\n}', start) + 2;
  vm.runInContext(appSource.slice(start, end), context);
}

function setup({ stepPaused }) {
  const audio = {
    src: 'https://example.test/audio.mp3',
    readyState: 1,
    currentTime: 0,
    playbackRate: 1,
    playCalls: 0,
    play() { this.playCalls += 1; return Promise.resolve(); },
    addEventListener() {},
  };
  const player = {
    key: 'story:1',
    audioUrl: '/audio.mp3',
    cues: [
      { start_ms: 0, end_ms: 1000 },
      { start_ms: 1000, end_ms: 2000 },
      { start_ms: 2000, end_ms: 3000 },
    ],
    activeIdx: 1,
    lastMs: 1200,
    playing: false,
    stepPaused,
    map: null,
    follow: false,
  };
  const context = vm.createContext({
    _raPlayer: player,
    _sharedAudio: audio,
    _RA_RESTART_MS: 1000,
    _playSeq: 0,
    _kTtsRate: 1,
    _getAudioEl: () => audio,
    _kTtsStopPlayback() {},
    _raOnTimeUpdate() {},
    _raUpdateBar() {},
    _raSaveProgress() {},
    _raAdvanceQueue() {},
    _raHighlight() {},
    _raScrollToActive() {},
    location: { href: 'https://example.test/' },
    URL,
  });
  for (const name of ['_raCueIndexForMs', '_raCurrentIdx', '_raPlayAt', '_raSeekTo', '_raSkipSentence']) {
    loadFunction(context, name);
  }
  return { context, player, audio };
}

for (const stepPaused of [false, true]) {
  test(`next sentence starts playback when paused (stepPaused=${stepPaused})`, () => {
    const { context, player, audio } = setup({ stepPaused });
    context._raSkipSentence(1);
    assert.equal(player.activeIdx, 2);
    assert.equal(player.lastMs, 2000);
    assert.equal(player.playing, true);
    assert.equal(player.stepPaused, false);
    assert.equal(audio.currentTime, 2);
    assert.equal(audio.playCalls, 1);
  });
}

test('previous sentence starts playback from the selected sentence', () => {
  const { context, player, audio } = setup({ stepPaused: false });
  context._raSkipSentence(-1);
  assert.equal(player.activeIdx, 0);
  assert.equal(player.lastMs, 0);
  assert.equal(player.playing, true);
  assert.equal(audio.currentTime, 0);
  assert.equal(audio.playCalls, 1);
});

test('previous sentence restarts the current sentence after its first second', () => {
  const { context, player, audio } = setup({ stepPaused: false });
  player.lastMs = 2200;
  context._raSkipSentence(-1);
  assert.equal(player.activeIdx, 1);
  assert.equal(player.lastMs, 1000);
  assert.equal(player.playing, true);
  assert.equal(audio.currentTime, 1);
  assert.equal(audio.playCalls, 1);
});

for (const id of ['ra-fs-skip-wrap', 'ra-fs-back', 'ra-fs-fwd']) {
  test(`${id} is hidden by default`, () => {
    const tag = htmlSource.match(new RegExp(`<[^>]+id=["']${id}["'][^>]*>`));
    assert.ok(tag, `${id} must remain available for a future setting`);
    assert.match(tag[0], /\bhidden\b/);
  });
}
