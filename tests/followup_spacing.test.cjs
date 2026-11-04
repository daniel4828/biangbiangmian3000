// #1448: Folgesätze are spaced out in a queue; _patternOnly splits tense from pattern
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/app.js', 'utf8');

function load(story) {
  const start = source.indexOf('const _FOLLOWUP_GAP_MIN');
  const end = source.indexOf('function _runFollowups');
  assert.ok(start !== -1 && end > start);
  const context = {story, _followupDone: new Set()};
  vm.createContext(context);
  vm.runInContext(source.slice(start, end) +
    '\nthis.api = {enq:_enqueueFollowups, take:_takeDueFollowups, pending:_pendingFollowups, queue:()=>_followupQueue};',
    context);
  return context.api;
}

test('first item is due after the minimum gap, not before', () => {
  const api = load({});
  api.enq({word_id: 1}, [{id: 10}], () => 0);
  assert.equal(api.take(false).length, 0);
  assert.equal(api.take(false).length, 0);
  const due = api.take(false);
  assert.equal(due.length, 1);
  assert.equal(due[0].item.id, 10);
  assert.equal(api.queue().length, 0);
});

test('items of one card get increasing gaps', () => {
  const api = load({});
  api.enq({word_id: 1}, [{id: 1}, {id: 2}], () => 0);
  const [a, b] = api.queue();
  assert.notEqual(a.dueIn, b.dueIn);
  assert.ok(b.dueIn > a.dueIn);
});

test('flush returns everything', () => {
  const api = load({});
  api.enq({word_id: 1}, [{id: 1}, {id: 2}], () => 0.99);
  assert.equal(api.take(true).length, 2);
  assert.equal(api.queue().length, 0);
});

test('_pendingFollowups excludes queued ids', () => {
  const card = {word_id: 5, category: 'creating'};
  const api = load({followups: [{id: 1, followup_word_id: 5}, {id: 2, followup_word_id: 5}]});
  assert.equal(api.pending(card).length, 2);
  api.enq(card, [{id: 1}], () => 0);
  assert.deepEqual(api.pending(card).map(f => f.id), [2]);
});

test('_patternOnly drops the tense part', () => {
  const start = source.indexOf('function _patternOnly');
  const end = source.indexOf('}\n', start) + 2;
  const context = {};
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  assert.equal(context._patternOnly('Presente · -er (regelmäßig)'), '-er (regelmäßig)');
  assert.equal(context._patternOnly('-er'), '-er');
  assert.equal(context._patternOnly(''), '');
  assert.equal(context._patternOnly(null), '');
});
