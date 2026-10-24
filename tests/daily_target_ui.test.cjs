const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js','utf8');

test('Chinese leech list defaults to frequency and permits timestamp sort', () => {
  const option = {};
  const c = vm.createContext({_browseSort:'newest', DEFAULT_BROWSE_SORT:'newest',
    _browseCardStatus:'leech', _browseMode:'notes', activeLang:()=> 'zh',
    document:{querySelectorAll:s=>s.includes('frequency')?[option]:[],getElementById:()=>({})}});
  vm.runInContext(source.slice(source.indexOf('function _syncSortOptions('),source.indexOf('function _leafDeckIds(')),c);
  c._syncSortOptions();
  assert.equal(option.hidden,false);
  assert.equal(c._browseSort,'frequency');
  c._browseSort='leeched-asc'; c._syncSortOptions();
  assert.equal(c._browseSort,'leeched-asc');
});

function settings(api) {
  const c=vm.createContext({api, renderSettings(){}, showError(){}});
  vm.runInContext(source.slice(source.indexOf('// ── Daily card target'),source.indexOf('// ── Languages on/off')),c);
  return c;
}
test('settings failure is visible and leaves target switch disabled', async () => {
  const c=settings(async()=>{throw Error('offline');});
  await c._loadDailyTarget();
  assert.match(c._dailyTargetHtml(), /Could not load/);
  assert.match(c._dailyTargetHtml(), /disabled/);
});
test('failed save restores prior target value', async () => {
  let calls=0;
  const c=settings(async()=> { if (++calls===1) return {enabled:true,target:50}; throw Error('offline'); });
  await c._loadDailyTarget();
  await c.setDailyTargetEnabled(false);
  const html=c._dailyTargetHtml();
  assert.match(html,/checked/);
  assert.doesNotMatch(html,/<input[^>]*disabled/);
});

test('leech row keeps Unleech and adds restart with frequency rank', () => {
  const c=vm.createContext({_browseSelected:new Set(), _browseCardStatus:'leech',
    activeLang:()=> 'zh', _wordStatus:()=> 'leech'});
  vm.runInContext(source.slice(source.indexOf('function _wordRow('),source.indexOf('// Clear the leech flag')),c);
  const html=c._wordRow({id:7,word_zh:'的',frequency_rank:1,cards:[{is_leech:1}]});
  assert.match(html,/Unleech/);
  assert.match(html,/Set as New Card/);
  assert.match(html,/Frequency #1/);
});

test('restart calls the leech-only endpoint and refreshes browse and counts', async () => {
  const calls=[];
  const c=vm.createContext({api:async(method,path,body)=>{
    calls.push([method,path,body]); return [];
  },_langQP:()=>'', _browseReload:async()=>calls.push('browse'),
  _flattenDecks:d=>d,_renderBrowseDeckSelect(){},renderDecks:()=>calls.push('decks'),
  showQuickAddBanner(){},showError:message=>assert.fail(message)});
  vm.runInContext(source.slice(source.indexOf('async function browseLeechNewWord('),source.indexOf('// Generate AI content for a saved word')),c);
  await c.browseLeechNewWord(7,{textContent:'Set as New Card'});
  assert.equal(calls[0][1],'/api/cards/bulk-leech-new');
  assert.deepEqual(Array.from(calls[0][2].word_ids),[7]);
  assert.ok(calls.includes('browse'));
  assert.ok(calls.includes('decks'));
});

test('leech frequency default does not overwrite later general sorts', () => {
  const c = vm.createContext({_browseSort:'newest', DEFAULT_BROWSE_SORT:'newest',
    _browseCardStatus:'leech', _browseMode:'notes', activeLang:()=> 'zh',
    document:{querySelectorAll:()=>[],getElementById:()=>({})}});
  vm.runInContext(source.slice(source.indexOf('function _syncSortOptions('),source.indexOf('function _leafDeckIds(')),c);
  c._syncSortOptions();
  assert.equal(c._browseSort,'frequency');
  c._browseSort='pinyin-asc'; c._syncSortOptions();
  assert.equal(c._browseSort,'pinyin-asc');
  c._browseCardStatus='all'; c._syncSortOptions();
  c._browseCardStatus='leech'; c._syncSortOptions();
  assert.equal(c._browseSort,'frequency');
  c.activeLang=()=> 'fr'; c._syncSortOptions();
  assert.equal(c._browseSort,'leeched-desc');
  c._browseCardStatus='all'; c._syncSortOptions();
  assert.equal(c._browseSort,'newest');
});
