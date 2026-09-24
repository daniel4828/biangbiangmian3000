const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');

function setup() {
  const listeners = [];
  const nodes = new Map();
  const document = {
    addEventListener(type, fn, capture = false) { listeners.push({type, fn, capture}); },
    removeEventListener(type, fn, capture = false) {
      const i = listeners.findIndex(l => l.type === type && l.fn === fn && l.capture === capture);
      if (i >= 0) listeners.splice(i, 1);
    },
    getElementById: id => nodes.get(id),
    createElement() { return { querySelector: () => ({}), remove() { nodes.delete(this.id); } }; },
    body: { appendChild(node) { nodes.set(node.id, node); } },
  };
  const word = {word: '促销', definition_de: 'Werbung'};
  const ctx = vm.createContext({document, _raFsOpen: true, _raFsKeysBound: false,
    _wordTableWords: [word], _glossWordIndex: new Map([['word', word]]),
    _escHtml: s => s, _placeWordActions() {}, setTimeout() {},
    _raCloseFullscreen() { ctx._raFsOpen = false; },
  });
  for (const name of ['_raBindFsKeys', '_openWordActions', '_openKnownWordActions',
    '_wordActionsOutside', '_wordActionsEscape', 'closeWordActions']) {
    const start = source.indexOf(`function ${name}(`);
    const end = source.indexOf('\n}', start) + 2;
    vm.runInContext(source.slice(start, end), ctx);
  }
  ctx._raBindFsKeys();
  function press(key) {
    const event = {key, target: {}, stopped: false, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopImmediatePropagation() { this.stopped = true; }};
    // DOM capture listeners run before bubble listeners, regardless of registration order.
    for (const capture of [true, false]) {
      for (const l of [...listeners]) {
        if (!event.stopped && l.type === 'keydown' && l.capture === capture) l.fn(event);
      }
    }
    return event;
  }
  return {ctx, nodes, press};
}

for (const opener of ['_openWordActions', '_openKnownWordActions']) {
  test(`${opener}: Escape closes popup before player, and removes its listener`, () => {
    const {ctx, nodes, press} = setup();
    ctx[opener](opener === '_openWordActions' ? 0 : 'word', {});
    const event = press('Escape');
    assert.equal(nodes.has('word-actions'), false);
    assert.equal(ctx._raFsOpen, true);
    assert.equal(event.defaultPrevented, true);
    press('Escape');
    assert.equal(ctx._raFsOpen, false);
  });
}

test('non-Escape leaves popup open; closing and reopening keeps Escape working', () => {
  const {ctx, nodes, press} = setup();
  ctx._openWordActions(0, {});
  ctx._key = () => '';
  press('x');
  assert.equal(nodes.has('word-actions'), true);
  ctx.closeWordActions();
  ctx._openKnownWordActions('word', {});
  press('Escape');
  assert.equal(nodes.has('word-actions'), false);
  assert.equal(ctx._raFsOpen, true);
  press('Escape');
  assert.equal(ctx._raFsOpen, false);
});
