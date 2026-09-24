const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function setup(saved = {}) {
 const context = {localStorage: {getItem: k => saved[k] ?? null, setItem: (k,v) => saved[k] = String(v)}, currentCardLang: () => 'zh'};
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
test('reordering preserves current stage, disabling skips it, preferences survive reload', () => {
 const saved={}; const c=setup(saved);
 const slider={value:7}; const options={innerHTML:''};
 c.document={getElementById:id=>id==='listen-hint-slider'?slider:options};
 c.onListenHintSlider=()=>{};
 c.moveHintStage(1,-1);
 assert.equal(c._hintCurrentStage(),1);
 assert.equal(c._hintEnabledStages()[6],1);
 c.changeHintStage(1,false);
 assert.equal(c._hintCurrentStage(),0);
 assert.ok(!setup(saved)._hintEnabledStages().includes(1));
 assert.match(options.innerHTML,/Move HSK 4 up/);
});
test('non-Chinese skips HSK and last available stage cannot be disabled', () => {
 const c=setup(); c.currentCardLang=()=> 'fr';
 assert.equal(Array.from(c._hintEnabledStages()).join(','),'0,1,9,2');
 c.document={getElementById:()=>({value:0,innerHTML:''})}; c.onListenHintSlider=()=>{};
 for(const id of [1,9,2,0]) c.changeHintStage(id,false);
 assert.equal(Array.from(c._hintEnabledStages()).join(','),'0');
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
 c._renderListenHint(2); assert.equal(visible(),'____________。');
});
test('saving a word updates every cached sentence only in its language', () => {
 const c=setup(); const first={word:'就业'},second={word:'就业'},other={word:'就业'};
 c._allWordsResolved=new Map([['zh\na',[first]],['zh\nb',[second]],['fr\nc',[other]]]);
 c.document={getElementById:()=>null};
 c._hintWordSaved('就业','zh',42);
 assert.equal(first.word_id,42); assert.equal(second.word_id,42); assert.equal(other.word_id,undefined);
 assert.equal(c._hintKeepWord(9,first),false);
});
