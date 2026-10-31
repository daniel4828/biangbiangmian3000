const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function setup(saved = {}, lang = 'zh') {
 const context = {localStorage: {getItem: k => saved[k] ?? null, setItem: (k,v) => saved[k] = String(v)}, currentCardLang: () => lang, activeLang: () => lang};
 vm.createContext(context);
 const source = fs.readFileSync('static/app.js','utf8');
 vm.runInContext(source.slice(source.indexOf('const _HINT_MIN'), source.indexOf('// The new words of the sentence')), context);
 return context;
}
test('legacy default remains new words; all ten stages available', () => {
 const c=setup(); assert.equal(c._hintSavedDefault(),1); assert.equal(c._hintEnabledStages().length,10);
});
test('custom order excludes disabled stages and falls back if default disabled', () => {
 const c=setup({listenHintStages:JSON.stringify([{id:2,enabled:true},{id:0,enabled:true},{id:1,enabled:false}])});
 assert.equal(c._hintEnabledStages()[0],2); assert.ok(!c._hintEnabledStages().includes(1)); assert.equal(c._hintSavedDefault(),2);
});
test('invalid or all-disabled preferences recover safely', () => {
 assert.ok(setup({listenHintStages:'broken'})._hintEnabledStages().length);
 assert.ok(setup({listenHintStages:JSON.stringify(Array.from({length:10},(_,id)=>({id,enabled:false})))})._hintEnabledStages().length);
});
test('HSK threshold includes harder and unlisted words; unsaved ignores known status', () => {
 const c=setup();
 assert.equal(c._hintKeepWord(6,{hsk:4}),false);
 assert.equal(c._hintKeepWord(6,{hsk:5}),true);
 assert.equal(c._hintKeepWord(6,{hsk:null}),true);
 assert.equal(c._hintKeepWord(9,{word_id:12}),false);
 assert.equal(c._hintKeepWord(9,{word_id:null}),true);
});
test('reordering and disabling persist from settings', () => {
 const saved={}; const c=setup(saved);
 const options={innerHTML:''};
 c.document={getElementById:()=>options};
 c.moveHintStage(1,-1); // #1372: jumps over the four hidden CEFR stages, swaps with HSK 6
 assert.equal(c._hintEnabledStages()[6],1);
 c.changeHintStage(1,false);
 assert.ok(!setup(saved)._hintEnabledStages().includes(1));
 assert.match(options.innerHTML,/Always show word before target/);
 assert.match(options.innerHTML,/Move HSK 4 up/);
});
test('non-Chinese skips HSK and last available stage cannot be disabled', () => {
 const c=setup(); c.currentCardLang=()=> 'fr';
 assert.equal(Array.from(c._hintEnabledStages()).join(','),'0,1,9,2');
 c.document={getElementById:()=>({value:0,innerHTML:''})}; c.onListenHintSlider=()=>{};
 for(const id of [1,9,2,0]) c.changeHintStage(id,false);
 assert.equal(Array.from(c._hintEnabledStages()).join(','),'0');
});
// #1369: CEFR stages for Spanish
test('Spanish gets CEFR stages and no HSK; Chinese gets HSK and no CEFR', () => {
 assert.equal(Array.from(setup({}, 'es')._hintEnabledStages()).join(','),'0,10,11,12,13,1,9,2');
 assert.equal(Array.from(setup({}, 'zh')._hintEnabledStages()).join(','),'0,3,4,5,6,7,8,1,9,2');
 assert.equal(Array.from(setup({}, 'fr')._hintEnabledStages()).join(','),'0,1,9,2');
});
test('CEFR threshold keeps harder and unlisted words', () => {
 const c=setup({}, 'es');
 assert.equal(c._hintKeepWord(10,{cefr:1}),false);
 assert.equal(c._hintKeepWord(10,{cefr:2}),true);
 assert.equal(c._hintKeepWord(13,{cefr:4}),false);
 assert.equal(c._hintKeepWord(13,{cefr:5}),true);
 assert.equal(c._hintKeepWord(13,{cefr:null}),true);
 assert.equal(c._hintKeepWord(13,{}),true);
 assert.equal(c._hintKeepWord(11,{cefr:1,function_word:true}),true);
});
test('legacy saved config gets CEFR stages inserted after HSK 6, not at the end', () => {
 const legacy=[0,3,4,5,6,7,8,1,9,2].map(id=>({id,enabled:true}));
 const c=setup({listenHintStages:JSON.stringify(legacy)}, 'es');
 assert.equal(Array.from(c._hintStageConfig().map(s=>s.id)).join(','),'0,3,4,5,6,7,8,10,11,12,13,1,9,2');
 const custom=setup({listenHintStages:JSON.stringify([{id:2,enabled:true},{id:0,enabled:false}])});
 const ids=Array.from(custom._hintStageConfig().map(s=>s.id));
 assert.equal(ids.slice(0,2).join(','),'2,0');
 assert.ok(ids.indexOf(10)>ids.indexOf(8) && ids.indexOf(13)<ids.indexOf(1));
 assert.ok(custom._hintStageConfig().every(s=>s.id===0?!s.enabled:true));
});
test('_getTargetPositions finds the conjugated form on non-Chinese cards', () => {
 const source=fs.readFileSync('static/app.js','utf8');
 const run=lang=>{
  const c=setup({}, lang);
  vm.runInContext(source.slice(source.indexOf('const _ROMANCE_ARTICLE_PREFIXES'),source.indexOf('// ── Word bank: locate the target word')),c);
  vm.runInContext(source.slice(source.indexOf('function _getTargetPositions'),source.indexOf('// Every position covered by any of')),c);
  c.card={word_zh:'ser',word_forms:['ser','eres','es']}; c.sentence={words:[]};
  return Array.from(c._getTargetPositions('Ana, eres una buena amiga.')).sort((a,b)=>a-b);
 };
 assert.deepEqual(run('es'),[5,6,7,8]);
 assert.deepEqual(run('zh'),[]); // Chinese stays a literal search only
});
test('render hides basic HSK and saved words while never revealing answer', () => {
 const c=setup(); const el={innerHTML:''};
 Object.assign(c, {document:{getElementById:()=>el}, card:{word_zh:'答案'}, sentence:{sentence_zh:'你好就业答案Musk20。'},
 _allWordsSync:()=>[{word:'你好',hsk:1,word_id:5},{word:'就业',hsk:5},{word:'答案',hsk:4}],
 _allWordsErrors:new Set(), _allWordsKey:()=>'', _glossWordIndex:new Map(),
 _escHtml:s=>s, setWordTable(){}, _makeWordsTappable(){}});
 const source=fs.readFileSync('static/app.js','utf8');
 vm.runInContext(source.slice(source.indexOf('// The new words of the sentence'),source.indexOf('// ── Render sentence (with target word highlighted)')),c);
 const visible=()=>el.innerHTML.replace(/<[^>]*>/g,'');
 c._renderListenHint(6); assert.equal(visible(),'__就业__Musk20。');
 c._renderListenHint(9); assert.equal(visible(),'__就业__Musk20。');
 c._renderListenHint(0); assert.equal(visible(),'你好就业__Musk20。');
 c._renderListenHint(2); assert.equal(visible(),'__就业________。');
});
test('every masked stage shows the word before the target by default and setting can disable it', () => {
 const render = saved => {
  const c=setup(saved); const el={innerHTML:''};
  Object.assign(c, {document:{getElementById:()=>el}, card:{word_zh:'答案'}, sentence:{sentence_zh:'你好就业答案Musk20。'},
  _allWordsSync:()=>[{word:'你好',hsk:1,word_id:5},{word:'就业',hsk:5},{word:'答案',hsk:4}],
  _allWordsErrors:new Set(), _allWordsKey:()=>'', _glossWordIndex:new Map(),
  _escHtml:s=>s, setWordTable(){}, _makeWordsTappable(){}});
  const source=fs.readFileSync('static/app.js','utf8');
  vm.runInContext(source.slice(source.indexOf('// The new words of the sentence'),source.indexOf('// ── Render sentence (with target word highlighted)')),c);
  return level => {
   c._renderListenHint(level);
   return el.innerHTML.replace(/<[^>]*>/g,'');
  };
 };
 const enabled=render({});
 for (const level of [1,2,6,9]) {
  assert.match(enabled(level),/就业/);
  assert.doesNotMatch(enabled(level),/答案/);
 }
 const disabled=render({listenHintPreviousWord:'false'});
 assert.doesNotMatch(disabled(2),/就业/);
});
test('saving a word updates every cached sentence only in its language', () => {
 const c=setup(); const first={word:'就业'},second={word:'就业'},other={word:'就业'};
 c._allWordsResolved=new Map([['zh\na',[first]],['zh\nb',[second]],['fr\nc',[other]]]);
 c.document={getElementById:()=>null};
 c._hintWordSaved('就业','zh',42);
 assert.equal(first.word_id,42); assert.equal(second.word_id,42); assert.equal(other.word_id,undefined);
 assert.equal(c._hintKeepWord(9,first),false);
});

test('HSK masks simple grammar combinations using hint level without changing other stages', () => {
 const c=setup();
 for (const word of [{word:'他们',hsk:null,hint_hsk:1},{word:'交给',hsk:null,hint_hsk:4}]) {
  assert.equal(c._hintKeepWord(8,word),false);
  assert.equal(c._hintKeepWord(3,word),word.word==='交给');
  assert.equal(c._hintKeepWord(9,word),true);
 }
 assert.equal(c._hintKeepWord(8,{hsk:null,hint_hsk:null}),true);
 assert.equal(c._hintKeepWord(6,{hsk:5,hint_hsk:5}),true);
});

test('HSK 6 renders screenshot combinations as blanks and keeps target hidden', () => {
 const c=setup({listenHintPreviousWord:'false'}); const el={innerHTML:''};
 Object.assign(c, {document:{getElementById:()=>el}, card:{word_zh:'答案'}, sentence:{sentence_zh:'旧答案交给他们。'},
 _allWordsSync:()=>[{word:'旧',hsk:5,hint_hsk:5},{word:'答案',hsk:4,hint_hsk:4},
 {word:'交给',hsk:null,hint_hsk:4},{word:'他们',hsk:null,hint_hsk:1}],
 _allWordsErrors:new Set(), _allWordsKey:()=>'', _glossWordIndex:new Map(),
 _escHtml:s=>s, setWordTable(){}, _makeWordsTappable(){}});
 const source=fs.readFileSync('static/app.js','utf8');
 vm.runInContext(source.slice(source.indexOf('// The new words of the sentence'),source.indexOf('// ── Render sentence (with target word highlighted)')),c);
 c._renderListenHint(8);
 assert.equal(el.innerHTML.replace(/<[^>]*>/g,''),'_______。');
});

// #1254: previous-word hint must never be empty when segmentation glues it to the target
test('previous-word hint falls back when jieba merges it with the target or the word list is missing', () => {
 const render = (saved, words) => {
  const c=setup(saved); const el={innerHTML:''};
  Object.assign(c, {document:{getElementById:()=>el}, card:{word_zh:'掌权'}, sentence:{sentence_zh:'他说重新掌权了。'},
  _allWordsSync:()=>words,
  _allWordsErrors:new Set(), _allWordsKey:()=>'', _glossWordIndex:new Map(),
  _escHtml:s=>s, setWordTable(){}, _makeWordsTappable(){}});
  const source=fs.readFileSync('static/app.js','utf8');
  vm.runInContext(source.slice(source.indexOf('// The new words of the sentence'),source.indexOf('// ── Render sentence (with target word highlighted)')),c);
  c._renderListenHint(2);
  return el.innerHTML.replace(/<[^>]*>/g,'');
 };
 const merged=render({}, [{word:'他'},{word:'说'},{word:'重新掌权'},{word:'了'}]);
 assert.match(merged,/重新/); assert.doesNotMatch(merged,/掌权/);
 const empty=render({}, []);
 assert.match(empty,/新/); assert.doesNotMatch(empty,/掌权/);
 const off=render({listenHintPreviousWord:'false'}, [{word:'他'},{word:'说'},{word:'重新掌权'},{word:'了'}]);
 assert.doesNotMatch(off,/重新/);
});

test('settings list only the active language stages (#1372)', () => {
 const options={innerHTML:''};
 const zh=setup({}, 'zh'); zh.document={getElementById:()=>options};
 const zhHtml=zh._hintStageOptionsHtml();
 assert.match(zhHtml,/Move HSK 1 up/); assert.doesNotMatch(zhHtml,/Move A1 up/);
 const saved={}; const es=setup(saved, 'es'); es.document={getElementById:()=>options};
 const esHtml=es._hintStageOptionsHtml();
 assert.match(esHtml,/Move A1 up/); assert.doesNotMatch(esHtml,/Move HSK/);
 es.moveHintStage(10,-1); // over six hidden HSK stages, swaps with Show all
 assert.equal(Array.from(es._hintEnabledStages()).join(','),'10,0,11,12,13,1,9,2');
 assert.equal(Array.from(setup(saved,'zh')._hintEnabledStages()).join(','),'0,3,4,5,6,7,8,1,9,2'); // zh order untouched
});
