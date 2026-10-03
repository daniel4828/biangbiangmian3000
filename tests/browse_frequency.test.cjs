const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
test('frequency sort puts unknown words last and preserves input', () => {
  const source = fs.readFileSync('static/app.js', 'utf8');
  const ctx = vm.createContext({_browseSort: 'frequency', activeLang: () => 'zh'});
  vm.runInContext(source.slice(source.indexOf('function _sortWords('), source.indexOf('function onBrowseSort(')), ctx);
  const words = [{id:1, frequency_rank:null}, {id:2, frequency_rank:90}, {id:3, frequency_rank:1}];
  assert.deepEqual(Array.from(ctx._sortWords(words), w => w.id), [3,2,1]);
  assert.deepEqual(words.map(w => w.id), [1,2,3]);
});
test('frequency option is scoped to Chinese saved words and resets on exit', () => {
  const source = fs.readFileSync('static/app.js', 'utf8');
  const option = {};
  const ctx = vm.createContext({
    _browseSort: 'frequency', DEFAULT_BROWSE_SORT: 'newest',
    _browseCardStatus: 'saved', _browseMode: 'notes', activeLang: () => 'zh',
    document: {querySelectorAll: selector => selector.includes('frequency') ? [option] : [], getElementById: () => ({})},
  });
  vm.runInContext(source.slice(source.indexOf('function _syncSortOptions('), source.indexOf('function _leafDeckIds(')), ctx);
  ctx._syncSortOptions();
  assert.equal(option.hidden, false);
  ctx.activeLang = () => 'fr';
  ctx._syncSortOptions();
  assert.equal(option.hidden, true);
  assert.equal(ctx._browseSort, 'newest');
});
