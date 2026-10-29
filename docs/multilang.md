# 多语言地基：语言族 + entry_forms（#803）

> 法语（fr）模式此前只是"能跑"：导入器、加词、牌组树支持 `lang=fr`，但没人真正
> 用过，也没有法语/西班牙语真正需要的东西——动词变位、名词/形容词词形变化、
> 阴阳性。本文件是这层地基的唯一设计说明，后续各阶段（知识库多语言标注、AI
> 词典多语言、造句多语言）的 Issue 都引用它。

---

## 数据库决定（已定）：单一数据库，不分库

知识库、`review_log`、统计、离线同步全部跨语言共享；分库要靠 `ATTACH`，
`database/` 里每条查询都得改，收益换不回这个成本。语言隔离继续靠 `lang` 列
（`entries.lang` / `decks.lang` / `stories.lang`）。"某语言从主模式继承哪些
行为"这件事，落在 `languages.py` 的语言族配置上，不落在数据库结构上。

## 语言族：sinitic vs. romance

中文和罗曼语族（法语/西班牙语）是根本不同的两类语言——分词方式、语素结构、
要不要存拼音/汉字/量词，全都不一样。而法语和西班牙语彼此极像：都需要动词变位
表、名词/形容词的阴阳性和单复数、都用空格分词、CEFR 分级。

`languages.py` 用两个"语言族基底" dict 表达这层继承：

```python
_SINITIC_BASE = {
    "family": "sinitic",
    "annotator": "zh",
    "tokenizer": "jieba",
    "features": {
        "pinyin": True, "characters": True, "measure_words": True,
        "traditional": True, "extended_story_modes": True,
        "conjugation": False, "gender": False, "inflection": False,
    },
}

_ROMANCE_BASE = {
    "family": "romance",
    "annotator": "romance",
    "tokenizer": "whitespace",
    "features": {
        "pinyin": False, "characters": False, "measure_words": False,
        "traditional": False, "extended_story_modes": False,
        "conjugation": True, "gender": True, "inflection": True,
    },
}
```

具体语言用浅合并继承基底，再覆盖自己独有的字段：

```python
"fr": {**_ROMANCE_BASE, "code": "fr", "tts_voice": "fr-FR-DeniseNeural", ...,
       "features": {**_ROMANCE_BASE["features"]}},
"es": {**_ROMANCE_BASE, "code": "es", "tts_voice": "es-ES-ElviraNeural", ...,
       "features": {**_ROMANCE_BASE["features"]}},
```

**`features` 必须显式重新展开，不能让整个 dict 被子语言的字面量覆盖**——否则
以后某个语言想单独覆盖一个 feature（比如某天西班牙语要区分 `vosotros` 这种
法语没有的东西）就会把其它全部继承来的 feature 一起清空。加新的罗曼语族语言
（比如意大利语）只需要 `{**_ROMANCE_BASE, "code": "it", ...}` 一个条目。

新增字段（本 Issue 起所有语言都必须有）：

| 字段 | 含义 |
|---|---|
| `family` | `"sinitic"` \| `"romance"` —— 未来加新语言族时再扩展这个枚举 |
| `annotator` | 知识库生词标注用哪套实现：`"zh"`（`zh_annotate.py`，HSK 表+jieba+pypinyin，零 AI）\| `"romance"`（尚未实现，留给知识库多语言标注的 Issue） |
| `features.conjugation` | 该语言是否需要动词变位表（`entry_forms` 的 `kind='conjugation'`） |
| `features.gender` | 该语言的名词/形容词是否分阴阳性（`entries.gender` 列 + `entry_forms` 的 `kind='inflection', paradigm='genre'`） |
| `features.inflection` | 该语言是否需要名词单复数等词形变化（`entry_forms` 的 `kind='inflection'`） |

`get_lang_config` / `is_valid_lang` / `deck_root` 三个函数的签名不变，所有
既有调用点（`tts.py`、`translator.py`、`routes/story.py`、`ai.py`、
`database/decks.py`、`static/app.js` 的 features 判断）不用改。

---

## entry_forms：一张表装下变位和词形变化

`entry_conjugations` 只有 `tense × person` 两个维度，装得下动词变位，装不下
名词复数、形容词阴阳性——那些不是"时态×人称"，是"维度×值"。与其为词形变化
另开一张表（`entry_inflections`），不如把 `entry_conjugations` 泛化成一张
更通用的表，两种用法靠 `kind` 区分：

```sql
CREATE TABLE entry_forms (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id  INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL DEFAULT 'conjugation',  -- 'conjugation' | 'inflection'
    paradigm TEXT NOT NULL,   -- 变位: 时态；词形: 维度
    slot     TEXT NOT NULL DEFAULT '',              -- 变位: 人称；词形: 槽位
    form     TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    UNIQUE(word_id, paradigm, slot)
);
CREATE INDEX idx_entry_forms_form ON entry_forms(form);
CREATE INDEX idx_entry_forms_word ON entry_forms(word_id);
```

### 两种用法

**`kind='conjugation'`**（动词变位，与旧 `entry_conjugations` 语义一一对应）：

| 列 | 含义 | 例子 |
|---|---|---|
| `paradigm` | 时态 | `'présent'`、`'passé composé'`、`'participe passé'` |
| `slot` | 人称，无人称形式（分词/不定式）为 `''` | `'je'`、`'tu'`、`'il/elle'`、`''` |
| `form` | 变位后的形式 | `'parle'` |

**`kind='inflection'`**（名词/形容词词形变化，#803 新增）：

| 列 | 含义 | 例子 |
|---|---|---|
| `paradigm` | 维度 | `'nombre'`（数）、`'genre'`（性）|
| `slot` | 该维度下的槽位 | `'pluriel'`、`'féminin'`、`'masculin_pluriel'` |
| `form` | 该槽位对应的表层形式 | `'chevaux'`（cheval 的复数）|

`entries.gender` 列（`'m' \| 'f' \| 'mf' \| NULL`）单独存"这个词条本身的默认
性"——这是词条级别的固定属性，不是"变化出的一个形式"，所以不进 `entry_forms`；
`entry_forms` 里的 `genre` 维度存的是阴阳性变化出的**具体词形**（比如某个
形容词的阴性形式），二者互补，不重复。

### 为什么 `idx_entry_forms_form` 是必需的

知识库标注（zh_annotate 的罗曼语对应实现，后续 Issue）要对一篇文章里的每个
词形问"这是不是 Daniel 已经学过的某个词的一个变化形式"——这是每篇文章几百次
的查询，全表扫描不可接受。`database.forms_lookup(surface_forms, lang)` 就是
这条查询的实现，同时也匹配 `entries.word_zh` 本身（词典形也算已学）：

```python
def forms_lookup(surface_forms: list[str], lang: str) -> set[str]: ...
```

`lang` 参数不可省略：法语和西班牙语共享大量同形词面（`capital`、`animal`、
`total`、`region`……），不按语言过滤会把西班牙语文章里的生词误判成"已经学过
的法语词"。

### 迁移：一次性，不删旧表

`entry_conjugations` 的全部行在 `init_db()` 里被搬进 `entry_forms`
（`kind='conjugation'`、`paradigm=tense`、`slot=person`），用
`app_settings['migrated_entry_conjugations']` 标记保证只跑一次——原因同 #688
的教训：`init_db()` 每次进程启动都会跑，生产上每 2 分钟一次（`deploy.sh`），
不加标记的话每次重启都会重新灌一遍（`INSERT OR IGNORE` 本身是幂等的，但没有
标记就没法安全地允许之后手工修改 `entry_forms` 而不被下次启动覆盖）。

`entry_conjugations` 表**保留但代码不再读写**——`database.insert_word_conjugation`
/ `get_word_conjugations` 现在直接操作 `entry_forms`，只是保持旧的函数签名和
返回形状（`{tense, person, form, position}`）不变，因为 `/api/word/{id}` 和
`static/app.js` 的 `renderConjugationSection` 都依赖这个形状——法语词条详情页
不能因为这次重构而坏掉。

---

## `UNIQUE(word_zh)` → `UNIQUE(word_zh, lang)`

`entries.word_zh` 原来是全局唯一。这在只有中文+法语时凑合能用（文字系统不同，
天然不撞），但法语和西班牙语共享拉丁字母，`capital`、`animal`、`total`、
`region` 这类词两种语言里长得一模一样。全局唯一约束会：

- 要么直接拒绝插入西班牙语的 `capital`（因为法语的 `capital` 已经占了这个
  `word_zh`）；
- 要么更糟——`INSERT OR IGNORE` 静默把它当"已存在"跳过，西班牙语版本的释义/
  变位/例句从未真正写入，用户看到的是法语内容。

`known_words`（#710，"已认识但没学过的词"标记表）有一模一样的问题，同样从
`PRIMARY KEY(word_zh)` 改成 `PRIMARY KEY(word_zh, lang)`。

两张表都是**重建**（SQLite 不支持直接改 `UNIQUE`/主键约束），套用仓库里
`entries` register CHECK 迁移的先例：建新表 → 拷全量数据 → `DROP` → 
`RENAME`，全程 `PRAGMA foreign_keys=OFF`。`schema.sql` 与 `database/core.py`
的迁移代码两处的列定义必须逐字一致——这是新库（走 `schema.sql`）和老库迁移后
（走 `init_db()` 的 ALTER/重建）必须收敛到同一份表结构的唯一保证。

**已知限制*仍然*没有解决**：中文和法语之间仍然共享全局命名空间的风险为零
（文字系统不同），但如果将来加入另一种使用汉字的语言（日语？），
`UNIQUE(word_zh, lang)` 已经是正确答案，不用再迁移一次。

---

## `database/entries.py` 的形态接口

```python
def set_entry_forms(word_id: int, forms: list[dict]) -> None: ...
def get_entry_forms(word_id: int) -> dict: ...          # kind -> paradigm -> slot -> form
def forms_lookup(surface_forms: list[str], lang: str) -> set[str]: ...

# 向后兼容（读写都改成走 entry_forms，返回/参数形状不变）：
def insert_word_conjugation(word_id, tense, person, form, position) -> None: ...
def get_word_conjugations(word_id: int) -> list[dict]: ...   # [{tense, person, form, position}]
```

`set_entry_forms` 是**整表替换**，不是合并——调用方（导入器、AI 生成的词条）
每次都拿到完整的变位表/词形表，不存在"只更新一个时态"的场景，这与
`entry_examples` 用 `delete_word_examples` + 重新插入的模式一致。

`known_words` 相关函数（`database/podcast.py`）全部加了 `lang: str = "zh"`
参数，默认值保证中文侧所有既有调用点（`zh_annotate.py`、
`routes/knowledge.py`）行为完全不变。

---

## 知识库按语言渲染摘要与生词（#804，已实现）

依赖上面的语言族 + `forms_lookup` + 按语言 `known_words`。目标：知识库素材
（播客/视频/文章）只存一份摘要，但每种学习语言看到的"哪些词带翻译"必须不同
——中文模式和法语模式下的生词集合天然不重合，因为两边"已学词"是两套完全不同
的表。

### 设计取舍：翻译一次，不重新调用 AI

`podcast_episodes.summary_de`（德语）是唯一的 AI 生成版本。其他语言的阅读版
是它的**翻译+标注**衍生品，不是第二次 AI 摘要——多语言不该让每篇素材的摘要
成本乘以语言数。中文是例外：`summary_zh` 本来就是 AI 原生生成、由
`zh_annotate.py` 标注的，不走这条派生路径，行为一字不改。

### 新表 `knowledge_renditions`

```sql
CREATE TABLE knowledge_renditions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES podcast_episodes(id) ON DELETE CASCADE,
    lang TEXT NOT NULL,
    summary TEXT NOT NULL,     -- 目标语言摘要，生词已内联标注
    new_words TEXT,            -- JSON [{word, lemma, definition_de}]
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(episode_id, lang)
);
```

懒生成：`GET /api/podcast/episodes/{id}?lang=fr` 第一次访问时才翻译+标注并
写入这张表；之后同一 (episode, lang) 直接读缓存，不重复调用 Google 翻译。
`podcast.regenerate_summary()` 重新生成 `summary_de` 成功后立即
`database.delete_knowledge_renditions(episode_id)` 清空全部旧翻译（否则旧
法语翻译会和新德语摘要说两套话）——这一步是 best-effort（try/except 包裹），
失败只记日志，不会把已经成功的摘要重生成打回失败状态。

`translator.py` 新增 `translate_strict(text, target, source)`：和已有的
`translate_zh` 行为相反——失败时**抛异常**而不是静默返回原文。渲染路径必须
能区分"翻译成功"和"翻译失败"，才能决定要不要写库；`translate_zh`
"失败就返回原文"的契约对已有调用方是对的（少一个词的德语注释不该毁掉整篇
故事），但这里如果吞掉失败，会把德语原文当成"法语摘要"存进库。

`knowledge/rendition.py` 的 `get_or_create_rendition(episode_id, lang)` 是唯一
入口：查缓存 → 没有则取 `summary_de` → `translate_strict` → 失败就抛
`RenditionError`（**不写库**）→ 成功则 `annotate.annotate_summary()` 标注 →
`database.save_knowledge_rendition()` 落盘。`routes/podcast.py` 的
`get_episode()` 接住 `RenditionError`，返回 `rendition: null` +
`rendition_error` 说明原因，绝不用未翻译的德语文本冒充目标语言摘要。

### 标注分派：`annotate/` 包

`annotate/__init__.py` 的 `annotate_summary(text, lang)` 按
`languages.get_lang_config(lang)["annotator"]` 分派：

- `"zh"` → 包一层 `zh_annotate.py`（#638），**逻辑一字不改**，中文路径不存在
  回归的可能——甚至不产生 rendition 行，中文永远读 `summary_zh`。
- `"romance"` → `annotate/romance.py`（#804 新增），法语/西班牙语共用同一套
  实现（这正是语言族存在的意义：两种语言的形态学处理方式相同）。

`annotate/romance.py` 的判定逻辑：

1. 分词：按 Unicode 字母切分，剥离省音前缀（`l'économie` → `économie`）——
   但省音前缀单字母本身（`l`/`d`/`c`/`j`/`m`/`t`/`s`/`qu`/`n`）也在停用词表
   里，双重保险。
2. 跳过：停用词表（`annotate/stopwords_fr.txt` / `stopwords_es.txt`，各
   200+ 条常见虚词）、单字符、句中大写的专有名词。
3. **已学判定 = `database.forms_lookup(tokens, lang) | database.known_words_exists(tokens, lang)`**
   ——**不做词干还原**。`forms_lookup` 精确匹配 `entry_forms.form`：一个词的
   任何存过的变位/词形都算已学，`parlons` 因为在表里所以直接命中，不需要
   还原成 `parler` 再判断。这正是 #803 把所有变位存进 `entry_forms` 的
   原因。
4. 剩下的未知词：`translator.translate_batch()` 批量取德语释义（source=该
   语言的 `translator_source`，target=`de`），行内标成 `mot (gloss)`，每词
   只标首次；每篇最多内联标注 40 个（超出的仍进 `new_words`，只是不内联）。
5. 全程 `try/except` 兜底，任何一步失败都返回原文 + 空生词列表——同
   `zh_annotate` 的"少个释义是小事，丢整篇摘要才是荒唐"原则。

### 前端

`static/app.js`：`openKnowledgeItem` 请求 `?lang=<activeLang()>`；
`_renderKnowledgeDetail` 按 `activeLang()` 分支——`zh` 路径字节不变，其它
语言读 `ep.rendition.summary` / `ep.rendition.new_words`（渲染失败时显示
`ep.rendition_error`）。语言标签切换（`setActiveLang`）时，如果知识库详情页
正开着，自动重新拉取当前素材（`_knowledgeDetailId`）。生词表格的"★ List"
和"✓ Known"按钮沿用已有的 `addWordViaAi()` / `markWordKnown()`（`shared.js`），
两者都已支持可选 `lang` 参数（分别是 #726、#804 加的），按钮点击时传入
`_podcastDetailLang`。

### 未覆盖 / 已知限制

- `summary_de` 是 HTML（`<p>`/`<b>` 标签），`translate_strict` 把整段 HTML
  当纯文本整体丢给 Google 翻译——没有单独验证标签在翻译后是否完整保留；
  如果翻译引擎重排或吞掉标签，渲染出来的法语/西班牙语摘要可能丢失加粗/分段
  样式（内容本身不会丢，是纯 HTML 结构风险）。
- 罗曼语分词是简单的 Unicode 字母正则，不处理连字符复合词（`qu'est-ce` 之类
  会被拆成多个词单独判断）——精度足以标注生词，但不是完整的形态学分析器。
- 播客/邮件/Signal 通知（`podcast.send_mail` / `send_signal_text`）仍然只发
  德语+中文版本，本 Issue 明确不动通知路径。

### 造句（story generation）多语言（本 Issue 仍不实现，只占位）

`ai.py` 的提示词片段（`learner_level` / `background_vocab` /
`sentence_limit`）已经是从 `languages.py` 取的，新语言不需要新的生成逻辑，
只需要语言族的 `features.extended_story_modes` 等标志位控制哪些模式可用。

## 加词与词典：fr/es 的完整形态生成（#805，已实现）

依赖上面的语言族 + `entry_forms`。目标：法语/西班牙语加词生成的词条要和中文
一样完整——动词全部变位、名词/形容词全部词形变化——因为 `database.forms_lookup()`
是知识库判定"这个词形学过没有"的唯一办法，**不做词干还原**，所以形态表不全
就等于知识库标注对罗曼语永远漏词。

### 加词：`ai.generate_word_entry_yaml`

`_ENTRY_YAML_TEMPLATES = {"zh": ..., "fr": ..., "es": ...}` 按 `lang` 选提示词
+ 示例块，新增语言只是往这个字典里加一对，不改函数本身。法语/西班牙语提示词
要求：

- **名词**：`gender: m|f|mf` 字段 + `forms:` 里的复数（`nombre`/`numero`
  维度）。
- **形容词**：`forms:` 里的阴性、复数、阴性复数（`genre`/`genero` +
  `nombre`/`numero`）。
- **动词**：`conjugations:`——法语沿用 #726 已有的 7 个时态；西班牙语额外要求
  `presente`、`pretérito perfecto`、`pretérito indefinido`、`imperfecto`、
  `futuro`、`condicional`、`presente de subjuntivo` + `participio`/`gerundio`
  （西班牙语日常口语区分的过去时比法语这套多，少一个时态就是一整类"这句话
  哪个时态"的生词标注漏判）。

`forms:` 的 YAML 结构与 `conjugations:` 完全同构（`{维度: {槽位: 形式}}`），
这不是巧合——两者最终都写进同一张 `entry_forms` 表，只是 `kind` 不同
（见下）。完整字段规则和示例见 `docs/yaml-format.md` 的"法语格式"/"西班牙语
格式"两节，那里是唯一详细文档，这里不重复。

### 导入器：`_normalize_romance_entry` 泛化，`_process_forms` 新增

`importer._normalize_fr_entry` 改名（旧名保留为别名）为
`_normalize_romance_entry(entry, lang)`，用 `lang` 参数决定例句/相似句读
`fr:` 还是 `es:` 键，其余归一化逻辑（headword、CEFR 等级映射、丢弃中文专属
字段）两种语言完全共享。`gender:` 字段做一次合法性校验（`m`/`f`/`mf`，非法
值静默丢弃为 `None`，不因为一个可选字段拒绝整条词条——同 `register` 字段的
处理姿态）。

`_process_forms(entry, word_id)` 与既有的 `_process_conjugations` 是姊妹函数
（结构几乎一样，`kind='inflection'` 而不是 `'conjugation'`），把 `forms:`
映射逐槽位写进 `entry_forms`。`database/entries.py` 新增
`insert_word_form(word_id, kind, paradigm, slot, form, position)` 作为通用的
单行写入函数，`insert_word_conjugation` 现在是它的薄封装——两个写入路径共享
同一段 SQL，不是各写一份。`get_word_full()` 新增 `word["inflections"]`（形状
同 `word["conjugations"]`：`[{paradigm, slot, form, position}]`）。

### `POST /api/add-word-ai` 的 `lang` 参数

`is_valid_lang()` 已经认得 `es`（#803 把它注册成真语言），`_validate_word_for_lang`
的拉丁字母校验对 fr/es 天然通用（判断逻辑不认字母表具体是哪种，只认"不是
汉字且含拉丁字母"），所以这条路径**不需要改代码**——#805 在这里唯一动的是
错误提示信息，从硬编码"in French"改成按 `languages.get_lang_config(lang)["name_en"]`
取语言名。

### 词典：`DICTIONARY_PROMPT_ROMANCE`

`ai.py` 新增一份罗曼语版提示词（移植自 `de-fr-bot` 技能），用
`{lang_name}`/`{level}` 参数化，法语和西班牙语共用同一份模板——正是语言族存在
的意义：两种语言的词典分析结构应该几乎一样，不需要各写一份。**JSON 契约与
中文版完全一致**（`headline`/`kind`/`sentence`/`groups[].options[]`，字段名
不变）：目标语言的词/例句仍然写进名叫 `"zh"`/`"example_zh"` 的字段（历史命名，
`/dict` 前端不分语言渲染，字段名换了前端就要按语言分支，反而制造出#805明确
要避免的那类耦合）；`pinyin`/`headline_pinyin` 对 fr/es 留空——拉丁字母没有
单独的注音需要。

`ai.dictionary_lookup(query, lang, model)` 按 `lang in {"zh","fr","es"}` 选
`DICTIONARY_PROMPT` 或 `DICTIONARY_PROMPT_ROMANCE`；其他值抛 `ValueError`
（路由层转 400，不是让模型硬答一个错误语言的词典条目——同 #726 加词侧的姿态）。
`routes/dictionary.py` 的 400 校验、`dict_queries.lang` 列同步放开到三个值。

### 前端：`/dict` 语言切换

`static/dict.html` 加了和 `/add`（#726）完全一样的语言选择器模式：拉一次
`/api/langs`，只有多于一种语言在用时才显示切换钮；`?lang=` URL 参数打开即
选定语言。★ 按钮和 Repeat 按钮都要带上"这条历史记录当初是用哪种语言查的"
（`record.lang`，由 `_row_to_result` 新增的 `lang` 字段带出），而不是"选择器
现在显示的语言"——否则切标签页再点旧结果的 Repeat/★，会用错误的语言重新生成。
`/dict` 页面**仍然不加载 `app.js`**（保持原则不变）。

### 浏览与词条详情：`entry_forms` → 词形变化折叠区

`renderConjugationSection` 早已经从 `entry_forms`（经 `get_word_conjugations`
读回旧形状）读数据，这次不用改。新增 `renderInflectionSection`——同样的分组
渲染逻辑，读 `word.inflections` + `word.gender`，中文词条两者都是空/None，
折叠区直接不渲染（`el.innerHTML = ''`），零 UI 差异。挂载点：`wd-inflection-section`
（词条详情弹窗）+ `inflection-section`（复习卡背面），紧跟在各自的
conjugation 挂载点后面。

### 名词冠词（#1294）

`languages.py` 的语言条目可带可选的 `articles`（目前只有西语：`{"m": "el", "f": "la"}`）。
`languages.article_for(word, gender, lang)` 据此给名词词头配冠词：`m`→`el`、`f`→`la`，
以重读 a/ha 开头的阴性名词（显式词表，如 `agua`、`alma`）单数用 `el`，`mf`→`el/la`，
其余一律 `None`，不抛异常。`GET /api/word/{id}` 的 `article` 字段只供显示（复习卡背面和词条详情页）；
`word_zh` 与编辑框仍是裸词。法语需要省音（`l'eau`），暂未支持。

---

## 造句与调度预设（#806，已实现）

### 知识模式对所有语言可用

知识模式（从已摘要的素材造句）**与语言无关**：素材是德语转录还是中文全文都不影响，
决定输出语言的只有提示词。所以它有自己的开关 `features.knowledge_story_mode`（所有语言 True），
**不能**和 `extended_story_modes`（kahneman/paste/briefing，仍然只有中文）捆在一起。

- 中文走 `ai.DEFAULT_PROMPT_TEMPLATES["knowledge"]`（用户可在提示词版本库里改，那个界面本来就只有中文）。
- 其它语言走 `ai._KNOWLEDGE_PROMPT_NON_ZH`：同样的规则（每句恰好一个目标词、每句复述素材里的一个
  具体事实、优先硬数据、不许编造），只是用目标语言写、句长上限取 `languages` 的 `sentence_limit`、
  背景词汇取 CEFR 上限。
- `briefing` / `paste` 仍然只有中文，所以 `generate_briefing_sentences()` **没有**加 `lang` 参数——
  加了就是死代码。知识模式走的是 `generate_podcast_sentences()`。

### 匹配必须接受变位形（这是 entry_forms 的第二个用途）

提示词明确允许模型调整目标词的形态（`réduire` → `a réduit`）——不允许的话法语句子根本写不通顺。
所以匹配也必须接受：`ai._card_surface_forms(card, lang)` = 词典形 + 该词条在 `entry_forms` 里
存的全部变位/词形，每次生成只查一遍。

**漏存变位的代价在这里第二次出现**：匹配不上 → 句子被丢弃 → 该词拿到兜底句。加词时把变位表
生成齐全，既让知识库不把学过的词标成生词（#804），也让造句不白白丢句子。

非中文的兜底句是词本身加句号，不是中文的「我学了X这个词。」——兜底就该看起来像兜底。

### 每种语言一套调度预设

`database.core._ensure_lang_preset()`：某语言第一次建牌组时，复制当前默认预设建一个以牌组树根
命名的预设（`Français` / `Español`），该语言树下所有牌组都绑它。

**中文的绑定一个字节都不许动**——生产库里全部中文牌组绑在 preset id=2（"Anki Default"）上，
改错过一次浪费了三周（见 CLAUDE.md 的 #629）。所以 `get_or_create_deck` 里 `lang == 'zh'` 仍然
走原来的 `_ensure_default_preset`，`_ensure_lang_preset` 只对非中文调用。
