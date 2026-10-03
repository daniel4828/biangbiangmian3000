const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');
function load(name, context) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} must exist`);
  const end = source.indexOf('\n}', start) + 2;
  vm.runInContext(source.slice(start, end), context);
}
test('original copy preserves source paragraphs and excludes bilingual translations', () => {
  for (const original of ['Guten Morgen!\n\nZweiter Absatz.', '中文原文。\n下一行。', 'Original English.']) {
    const context = vm.createContext({_knowledgeDetailEpisode: {
      transcript_zh: original, transcript_de: [{zh: '额外的中文翻译', de: 'Other translation'}],
    }, _copyToClipboard: (text, button) => {context.copied = text; context.button = button;}});
    load('_knowledgeOriginalTranscriptText', context);
    load('doKnowledgeCopyOriginalTranscript', context);
    const button = {};
    context.doKnowledgeCopyOriginalTranscript(button);
    assert.equal(context.copied, original);
    assert.equal(context.button, button);
  }
});
test('missing original does not substitute translated segments', () => {
  const context = vm.createContext({});
  load('_knowledgeOriginalTranscriptText', context);
  assert.equal(context._knowledgeOriginalTranscriptText(null), '');
  assert.equal(context._knowledgeOriginalTranscriptText({transcript_de: [{zh:'译文'}]}), '');
  assert.equal(context._knowledgeOriginalTranscriptText({transcript_zh:'  \n '}), '');
});
test('detail offers original copy above content and labels the existing bilingual button', () => {
  const root = {innerHTML: ''};
  const context = vm.createContext({document: {getElementById: () => root},
    activeLang: () => 'fr', _knowledgeView: 'fulltext', _knowledgeEditOpen: false,
    _knowledgeFulltextBusy: false, _knowledgeFulltextChecked: () => true,
    _escHtml: String, _localDate: () => '', _knowledgeFulltextFor: () => null,
    setWordTable() {}, wordTableHtml: () => '', _knowledgeSummaryText: () => '',
  });
  for (const name of ['_knowledgePlatformLabel', '_knowledgeDiskUsageLabel', '_knowledgeTagRowHtml',
    '_knowledgeViewTabs', '_audioBarHtml', '_knowledgeFulltextHtml', '_knowledgeSummaryHtml',
    '_knowledgeChatHtml', '_raOwnerForEpisode', '_raAfterRender', '_makeWordsTappable', '_loadKnowledgeChat']) {
    context[name] = () => '';
  }
  load('_knowledgeOriginalTranscriptText', context);
  load('_renderKnowledgeDetail', context);
  context._renderKnowledgeDetail({id:1, transcript_zh:'Original', transcript_de:[{zh:'译文', de:'Original'}]});
  assert.match(root.innerHTML, /Copy Transcript in Original Language/);
  assert.ok(root.innerHTML.indexOf('knowledge-copy-original-transcript') < root.innerHTML.indexOf('knowledge-copy-transcript"'));
  assert.match(root.innerHTML, /Copy transcript \(bilingual\)/);
  context._renderKnowledgeDetail({id:2, transcript_zh:'Original'});
  assert.match(root.innerHTML, /Copy transcript \(original language\)/);
  context._renderKnowledgeDetail({id:3, transcript_zh:'  ', transcript_de:[{zh:'译文'}]});
  assert.ok(!root.innerHTML.includes('knowledge-copy-original-transcript'));
});
