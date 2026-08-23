# YAML 导入格式说明

> **这是 YAML 词汇文件的唯一事实来源。** AI 生成工具（`de-zh-bot` skill）和手动编写均应遵循此格式。
> `test.yaml` 是经过验证的规范示例——格式有疑问时以它为准。

---

## 目录

1. [通用字段](#通用字段)
2. [类型：`word`（词汇）](#类型-word词汇)
3. [类型：`sentence`（句子）](#类型-sentence句子)
4. [类型：`chengyu` / `expression`（成语 / 惯用表达）](#类型-chengyu--expression成语--惯用表达)
5. [类型：`grammar`（语法点）](#类型-grammar语法点-仅展示不导入)
6. [嵌套结构：`word_analyses`](#嵌套结构-word_analyses)
7. [向后兼容性](#向后兼容性)
8. [法语格式（`lang: fr`）](#法语格式lang-fr)
9. [西班牙语格式（`lang: es`）](#西班牙语格式lang-es)

---

## 通用字段

所有类型都必须包含以下字段：

| 字段 | 必填 | 说明 |
|------|------|------|
| `type` | ✅ | 见下方类型说明 |
| `simplified` | ✅ | 简体中文（作为唯一标识符） |
| `traditional` | — | 繁体中文（如与简体相同可省略） |
| `pinyin` | ✅ | 拼音（带声调） |
| `english` | ✅ | 英文释义 |
| `german` | ✅ | 德文释义 |
| `hsk` | — | HSK 等级，只能填写 `"1"` 到 `"6"` 之一（带引号的单个数字） |
| `date` | — | 添加日期，格式 `"MM/DD"` |

---

## 类型：`word`（词汇）

> 旧格式使用 `type: vocabulary`，两者均被接受。

```yaml
- type: word
  date: "03/26"
  simplified: 生态
  traditional: 生態             # 与简体相同时省略
  pinyin: shēngtài
  english: ecology / ecosystem
  german: Ökologie / Ökosystem
  hsk: "5"
  register: formal_written      # 可选：见下方 register 值说明
  measure_word:                 # 可选，仅名词适用
    - simplified: 种
      pinyin: zhǒng
      meaning: kind or type (for ecosystems)
    - simplified: 个
      pinyin: gè
      meaning: general classifier in figurative contexts
  note: |                       # 可选：英文使用说明与备注
    A noun meaning "ecology"...

    **Common Expressions:**
    - 生态环境 (shēngtài huánjìng) — ecological environment

  examples:                     # 建议 2–4 个，每个例句含 4 个字段
    - zh: 保护生态环境是我们每个人的责任。
      pinyin: Bǎohù shēngtài huánjìng shì wǒmen měi gè rén de zérèn.
      english: Protecting the ecological environment is the responsibility of every one of us.
      de: Den ökologischen Umwelt zu schützen ist die Verantwortung eines jeden von uns.

  word_analyses:                # 组成汉字分析（可选）
    - char_only: 生             # HSK 1–2 的简单字用 char_only
      pinyin: shēng
      hsk: "1"
    - type: word                # HSK 3+ 的字用 type: word，内含 characters
      simplified: 态
      traditional: 態
      pinyin: tài
      english: state, condition
      hsk: "4"
      characters:
        - char: 态
          simplified: 态        # 必须包含，即使与 char 相同
          traditional: 態       # 与简体相同时省略
          pinyin: tài
          hsk: "4"
          detailed_analysis: true  # HSK 3+ 为 true；HSK 1–2 为 false
          meaning_in_context: Zustand, Beschaffenheit
          compounds:
            - simplified: 状态
              pinyin: zhuàngtài
              meaning: Zustand, Verfassung
          etymology: |
            纯散文，不含列表。说明：部首、声符（表音字）、甲骨文/金文来源（如有）、意义演变。
```

### register 字段值

| 值 | 含义 |
|----|------|
| `spoken_colloquial` | 口语，umgangssprachlich |
| `spoken_neutral` | 中性口语 |
| `neutral` | 通用（口语+书面均适用） |
| `formal_written` | 书面语，正式文章 |
| `literary` | 文言，klassisch/literarisch |
| `slang` | 俚语，Jugendsprache |

---

## 类型：`sentence`（句子）

```yaml
- type: sentence
  date: "03/26"
  source_de: Ich werde dir zur passenden Zeit die Wahrheit sagen.  # 德文输入时包含
  simplified: 在适当的时候，我会告诉你真相。
  traditional: 在適當的時候，我會告訴你真相。
  pinyin: Zài shìdàng de shíhou, wǒ huì gàosu nǐ zhēnxiàng.
  english: I will tell you the truth at the appropriate time.
  hsk: "5"
  explanations: |              # 语法与词汇说明（英文），sentence 类型专用
    这句话使用了时间状语从句...

    - 在适当的时候 (zài shìdàng de shíhou) — at the appropriate time
    - 告诉 (gàosu) — to tell

  grammar_structures:          # 语法结构（可选）
    - structure: 在 + 时间状语 + 主语 + 会 + 动词 + 宾语
      explanation: 在适当的时候 is a time adverbial at sentence start.
      example: 在适当的时候，我会告诉你。

  similar_sentences:           # 类似句子（可选）
    - zh: 在合适的时机，我会告诉你。
      pinyin: Zài héshì de shíjī, wǒ huì gàosu nǐ.
      de: Ich werde es dir beim passenden Anlass sagen.

  word_analyses:               # 句中关键词分析（见下方说明）
    - type: word
      simplified: 适当
      ...
```

---

## 类型：`chengyu` / `expression`（成语 / 惯用表达）

两者格式相同，与 `word` 类型结构一致，另加：
- `synonyms` / `antonyms`（带 `word`、`pinyin`、`meaning`，chengyu 必填，expression 可选）
- `word_analyses`（解释各组成词语）

**类型区分：**
- `chengyu`：经典四字成语，有文言出处（如 同心协力、马到成功）
- `expression`：多词短语、固定搭配、口语表达——不是单个词语，也不是完整句子，也不是四字成语（如 说话的方式、愛上了、感到有責任、我快饿死了）

```yaml
- type: chengyu
  simplified: 同心协力
  traditional: 同心協力
  pinyin: tóng xīn xié lì
  english: to work together with one heart
  hsk: "5"
  register: formal_written
  note: |
    ...
  examples:
    - zh: 只有大家同心协力，才能完成这项艰巨的任务。
      pinyin: Zhǐyǒu dàjiā tóngxīn xiélì, cáinéng wánchéng zhè xiàng jiānjù de rènwu.
      english: Only when everyone works together can we complete this arduous task.
      de: Nur wenn alle gemeinsam an einem Strang ziehen, können wir diese Aufgabe bewältigen.
  synonyms:
    - word: 齐心协力
      pinyin: qíxīn xiélì
      meaning: to work together with one heart
  antonyms:
    - word: 一盘散沙
      pinyin: yīpán sǎnshā
      meaning: a sheet of loose sand (disorganized)
  word_analyses:
    - type: word
      simplified: 同心
      ...
```

---

## 类型：`grammar`（语法点，仅展示不导入）

> ⚠️ `grammar` 类型**不会被导入**到数据库，导入器会静默跳过。

```yaml
- type: grammar
  name: 所 (suǒ) – Nominalisierung mit Verb
  level: "5-6"
  structure: "所 + Verb + 的 (+ Nomen)"
  meaning: "das, was ..."
  usage: |
    ...
  examples:
    - zh: 我所知道的
      pinyin: wǒ suǒ zhīdào de
      de: Das, was ich weiß
  common_patterns:
    - pattern: 所 + V + 的
      meaning: das, was V
      example: 所需要的
```

---

## 嵌套结构：`word_analyses`

`word_analyses` 用于**所有类型**，解释组成词语或汉字。

| 类型 | word_analyses 的内容 |
|------|---------------------|
| `word` | 每个组成字：HSK 1–2 用 `char_only`，HSK 3+ 用 `type: word`（内含 `characters`） |
| `chengyu` / `expression` | 每个组成词：`type: word`（内含 `characters`） |
| `sentence` | 2–4 个关键词：`type: word`（内含 `characters`） |

### 形式 1：完整词语（`type: word`）

```yaml
word_analyses:
  - type: word
    simplified: 适当
    traditional: 適當
    pinyin: shìdàng
    english: appropriate, suitable
    hsk: "5"
    characters:
      - char: 适
        simplified: 适
        traditional: 適
        pinyin: shì
        hsk: "4"
        detailed_analysis: true
        meaning_in_context: to fit, to suit
        compounds:
          - simplified: 适合
            pinyin: shìhé
            meaning: to suit, to fit
        etymology: |
          Phono-semantic compound. Traditional form 適 consists of radical 辶 (walk)
          and phonetic 啇 (dí). Original meaning is "to go toward," extended to "to fit."
```

### 形式 2：单字（`char_only`）

用于 HSK 1–2 的简单字，不需要详细解释：

```yaml
word_analyses:
  - char_only: 我
    pinyin: wǒ
    hsk: "1"
```

---

## 语言规则

| 字段 | 语言 |
|------|------|
| `note` | **德语** |
| `explanations`（sentence 类型） | **德语** |
| `etymology` | **德语** |
| `meaning_in_context` | **德语** |
| `compounds[].meaning` | **德语** |
| `examples[].english` | 英语 |
| `examples[].de` | 德语 |
| `similar_sentences[].de` | 德语 |
| `synonyms/antonyms[].meaning` | 德语 |
| `measure_word[].meaning` | 德语 |
| `grammar_structures[].explanation` | 德语 |

---

## 关键字段规则

| 字段 | 规则 |
|------|------|
| `hsk` | 始终为带引号的单个数字：`"1"` `"2"` `"3"` `"4"` `"5"` `"6"` |
| `traditional` | 仅在与 `simplified` 不同时包含（词条级和字符块级均适用） |
| 字符块内的 `simplified` | 始终包含，即使与 `char` 相同 |
| `detailed_analysis` | HSK 3+ 为 `true`；HSK 1–2 为 `false` |
| `etymology` | 始终使用 `\|` 块标量，纯散文——内部不含列表——**德语** |
| `examples` | 始终包含全部 4 个字段：`zh`、`pinyin`、`english`、`de` |
| `note` vs `explanations` | `word`/`chengyu`/`expression` 用 `note`；`sentence` 用 `explanations` |

---

## 向后兼容性

| 旧字段/值 | 当前支持 | 说明 |
|-----------|----------|------|
| `type: vocabulary` | ✅ | 等同于 `type: word` |
| `characters:` (顶层) | ✅ | 旧格式，现已被 `word_analyses:` 取代；现有文件无需迁移 |
| `measure_word` | ✅ | 量词列表键名 |
| `explanations` | ✅ | sentence 类型字段，写入 `entries.notes` |
| `source_de` | ✅ | 存入 `entries.source_sentence` |
| `definition_zh` | ✅ | 存入 `entries.definition_zh` |

---

## 数据库映射

| YAML 字段 | 数据库表 / 列 |
|-----------|--------------|
| `simplified` | `entries.word_zh` |
| `english` | `entries.definition` |
| `register` | `entries.register` |
| `note` / `explanations` | `entries.notes` |
| `synonyms` / `antonyms` | `entry_relations` |
| `measure_word` | `entry_measure_words` |
| `examples` | `entry_examples` (type=`example`) |
| `similar_sentences` | `entry_examples` (type=`similar`) |
| `grammar_structures` | `entry_grammar_structures` |
| `characters` | `entry_characters` → `characters` → `character_compounds`（旧格式，仍支持） |
| `word_analyses` | `entry_components` + 递归导入子词语（所有类型的统一格式） |

---

## 法语格式（`lang: fr`）

文件顶层必须声明 `lang: fr`（或粘贴导入到法语牌组——此时可省略）。法语条目经
`importer._normalize_romance_entry(entry, "fr")` 归一化后复用全部下游逻辑
（`_normalize_fr_entry` 仍作为别名保留，向后兼容）；同一函数按 `lang` 参数
也服务西班牙语（见下方「西班牙语格式」一节）。中文专属模块（汉字分解、
量词、拼音、`word_analyses`）不适用——取而代之的是顶层 `etymology:` 字段（议题
 #906）：法语词没有汉字可拆，复习卡右栏那个位置显示的是词源。

### 与中文格式的字段差异

| 字段 | 说明 |
|------|------|
| `word` / `sentence` / `expression` | 词形字段（代替 `simplified`），按 `type` 取名 |
| `level` | CEFR 等级字符串 `"A1"`–`"C2"`（代替 `hsk`），映射为 1–6 存入 `entries.hsk_level` |
| `english` / `german` | 英/德释义（同中文格式） |
| `note` | 德语使用说明（用法、搭配、假朋友警告）。**不要**再往里塞词源——它有自己的字段 |
| `etymology` | **词源**，德语散文块标量（`\|`），2–4 句，无列表、无 `**Étymologie:**` 标题（界面已有标签）。存入 `entries.etymology`（议题 #906）。内容：源词（拉丁语/希腊语/法兰克语/阿拉伯语…）及其原义、进入现代语言的路径、词义演变、学习者已认识的德语/英语同源词；源词用 `*星号*` 标出。词源不明就直说，不要编 |
| `examples[]` | 每项 `{fr, german, english}`（`fr` 代替 `zh`，`german` 代替 `de`） |
| `similar_sentences[]` | 同上，`{fr, german}`，仅 sentence 类型 |
| `synonyms` / `antonyms` | 同中文格式：`{word, meaning}`（meaning 用德语） |
| `conjugations` | 动词专用，见下——存入 `entry_forms` 表，`kind='conjugation'`（议题 #596/#803） |
| `gender` | 名词专用，`m`\|`f`\|`mf`——存入 `entries.gender`（议题 #805） |
| `forms` | 名词/形容词专用，见下——存入 `entry_forms` 表，`kind='inflection'`（议题 #805） |
| `register` | 同中文格式的取值集合 |

### `conjugations` 结构（仅动词）

映射的两种形态：**有人称的时态**用 `{人称: 形式}` 子映射；**无人称形式**
（分词、不定式）直接写字符串。时态与人称的书写顺序会按原样保留并展示。

```yaml
lang: fr
entries:
  - type: word
    date: "07/21"
    word: parler
    pos: verbe
    english: to speak, to talk
    german: sprechen, reden
    level: "A1"
    register: neutral
    note: |
      Regelmäßiges Verb auf -er.
    etymology: |
      Vom kirchenlateinischen *parabolare* („in Gleichnissen reden"), zu
      griech. *parabolé*. Dasselbe Wort steckt in dt. „Parabel".
    examples:
      - fr: Je parle un peu français.
        english: I speak a little French.
        german: Ich spreche ein wenig Französisch.
    synonyms:
      - word: discuter
        meaning: diskutieren, sich unterhalten
    conjugations:
      présent:
        je: parle
        tu: parles
        il/elle: parle
        nous: parlons
        vous: parlez
        ils/elles: parlent
      passé composé:
        je: ai parlé
        tu: as parlé
        il/elle: a parlé
        nous: avons parlé
        vous: avez parlé
        ils/elles: ont parlé
      participe présent: parlant
      participe passé: parlé (avoir)
```

### `gender` + `forms`（名词/形容词，罗曼语通用，议题 #805）

`gender` 是词条本身固定的语法性别（`entries.gender` 列）：**每个名词必须写**，
形容词/动词/句子不写。`forms` 是词形变化表——存入 `entry_forms`
（`kind='inflection'`），与 `conjugations` 用**同一套 `{维度: {槽位: 形式}}`
映射结构**，只是维度/槽位的名字换成"数""性"而不是"时态""人称"：

```yaml
lang: fr
entries:
  - type: word
    date: "07/21"
    word: le chat
    pos: nom (m)
    english: cat
    german: die Katze
    level: "A1"
    register: neutral
    gender: m
    note: |
      Männliches Substantiv. Für weibliche Katzen sagt man "la chatte".
    examples:
      - fr: Le chat dort sur le canapé.
        english: The cat is sleeping on the couch.
        german: Die Katze schläft auf dem Sofa.
    forms:
      nombre:
        pluriel: chats

  - type: word
    date: "07/21"
    word: vert
    pos: adjectif
    english: green
    german: grün
    level: "A1"
    register: neutral
    note: |
      Regelmäßiges Adjektiv, stimmt in Genus und Numerus überein.
    examples:
      - fr: Le pull vert est à moi.
        english: The green sweater is mine.
        german: Der grüne Pullover gehört mir.
    forms:
      genre:
        féminin: verte
        féminin pluriel: vertes
      nombre:
        pluriel: verts
```

**名词**只需要 `nombre: {pluriel: ...}`（复数）。**形容词**通常两个维度都要：
`genre`（阴性、阴性复数）+ `nombre`（阳性复数——阳性单数就是词条本身，不重复
写）。省音/单复数不变的词形不必写。维度名/槽位名不是强制枚举——代码只按 YAML
里写的原样存入 `paradigm`/`slot` 两列并原样展示，但为了和西班牙语/未来语言
的标注实现（`forms_lookup`）保持词面一致，建议只用上面示例里出现的这几个：
`nombre`（`pluriel`）、`genre`（`féminin`、`féminin pluriel`）。

### sentence 类型（法语）

```yaml
  - type: sentence
    date: "07/21"
    source_de: Ich werde dir zur passenden Zeit die Wahrheit sagen.
    sentence: Je te dirai la vérité au moment opportun.
    english: I will tell you the truth at the appropriate time.
    german: Ich werde dir zur geeigneten Zeit die Wahrheit sagen.
    level: "B1"
    explanations: |
      „au moment opportun" ist eine formelle Zeitangabe.
    similar_sentences:
      - fr: Je te le dirai plus tard.
        german: Ich sage es dir später.
```

### 法语专属数据库映射

| YAML 字段 | 数据库表 / 列 |
|-----------|--------------|
| `word` / `sentence` / `expression` | `entries.word_zh`（列名是历史遗留，存目标语言词形） |
| `level`（CEFR） | `entries.hsk_level`（A1=1 … C2=6） |
| `gender` | `entries.gender`（#805） |
| `conjugations` | `entry_forms`，`kind='conjugation'`（word_id, paradigm=tense, slot=person, form, position；无人称形式 slot=''；#803 起不再写旧的 `entry_conjugations` 表） |
| `forms` | `entry_forms`，`kind='inflection'`（word_id, paradigm=维度, slot=槽位, form, position；#805） |

---

## 西班牙语格式（`lang: es`）

文件顶层必须声明 `lang: es`（或粘贴导入到西班牙语牌组——此时可省略）。与法语
共享同一套罗曼语归一化逻辑（`importer._normalize_romance_entry(entry, "es")`），
唯一区别是例句用 `es:` 键代替 `fr:` 键；字段规则、`forms`/`gender` 结构、
数据库映射与上面的法语格式完全一致，只是动词变位的时态集合更大（西班牙语
日常口语区分的时态比法语这套多）。

### 与法语格式的字段差异

| 字段 | 说明 |
|------|------|
| `examples[].es` / `similar_sentences[].es` | 代替 `fr`，西班牙语原文 |
| `pos` | `verbo`、`sustantivo (m)`、`sustantivo (f)`、`adjetivo`、`adverbio`、`locución` … |
| `conjugations` | 时态键：`presente`、`pretérito perfecto`、`pretérito indefinido`、`imperfecto`、`futuro`、`condicional`、`presente de subjuntivo` + `participio`/`gerundio`（无人称，纯字符串）。人称键固定 `yo`、`tú`、`él/ella`、`nosotros`、`vosotros`、`ellos/ellas` |
| `forms` | 维度命名习惯用 `numero`（`plural`）+ `genero`（`femenino`、`femenino plural`）—— 与法语的 `nombre`/`genre` 同构，只是西班牙语拼写不带重音（数据库不关心具体拼法，`paradigm`/`slot` 原样存储） |
| `etymology` | 同法语，独立的德语散文字段（不是 `note` 里的一行）；例词用西语视角（阿拉伯语借词、`*inšāʾ Allāh*` → `ojalá` 之类） |

### 完整示例

```yaml
lang: es
entries:
  - type: word
    date: "08/18"
    word: hablar
    pos: verbo
    english: to speak
    german: sprechen
    level: "A1"
    register: neutral
    note: |
      Regelmäßiges Verb auf -ar.
    etymology: |
      Vom lateinischen *fabulari* („erzählen, plaudern"), zu *fabula* —
      dieselbe Wurzel wie dt. „Fabel".
    examples:
      - es: Hablo un poco de español.
        english: I speak a little Spanish.
        german: Ich spreche ein wenig Spanisch.
    conjugations:
      presente:
        yo: hablo
        tú: hablas
        él/ella: habla
        nosotros: hablamos
        vosotros: habláis
        ellos/ellas: hablan
      participio: hablado
      gerundio: hablando

  - type: word
    date: "08/18"
    word: el gato
    pos: sustantivo (m)
    english: cat
    german: die Katze
    level: "A1"
    register: neutral
    gender: m
    note: |
      Männliches Substantiv. Für weibliche Katzen sagt man "la gata".
    examples:
      - es: El gato duerme en el sofá.
        english: The cat is sleeping on the couch.
        german: Die Katze schläft auf dem Sofa.
    forms:
      numero:
        plural: gatos
```

### 西班牙语专属数据库映射

与法语一致（同一套 `entry_forms`/`entries.gender` 映射），见上表。
