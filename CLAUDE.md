# 中文间隔重复系统 — 项目说明

> 🔴 **【每条消息必须做的第一件事】** 在写任何内容之前，先判断本次的**回答语言**（Daniel 写法语 → 法语；其余一律中文），再把 Daniel 的消息改写为该语言的正确版本。这是不可跳过的步骤——见下方"语言指令"章节。上下文压缩、对话长度、话题切换均不是跳过的理由。

> **所有 AI 代理（dàilǐ - agent）和开发者必须遵守（zūnshǒu - comply with）以下规则。这些是固定规则，不是建议。**

## 目录

**工作方式（每次会话都要遵守）**
1. [固定规则](#固定规则mandatory-rules) · 2. [语言指令](#语言指令) · 3. [代理编排：Opus 编排，Sonnet 执行](#代理编排opus-编排sonnet-执行) · 4. [Git & GitHub 工作流](#git--github-工作流)

**项目总览**
5. [项目简介与技术栈](#项目简介与技术栈) · 6. [项目结构](#项目结构)

**运行环境**
7. [生产环境](#生产环境2026-07-07-上线) · 8. [笔记本模式：本地 + 离线](#笔记本模式本地--离线议题-612625) · 9. [启动、CLI 与环境变量](#启动cli-与环境变量)

**核心机制**
10. [数据库模式](#数据库模式概述) · 11. [调度算法 FSRS-5](#调度算法--fsrs-5默认--sm-2-回退) · 12. [队列设计](#队列设计) · 13. [多语言支持](#多语言支持)

**功能详解**
14. [数据与导入 / 界面内加词](#数据与导入) · 15. [故事生成](#故事生成) · 16. [加星句子](#加星句子改进提示词的正例样本692) · 17. [知识库](#知识库knowledge-base650655) · 18. [生词标注](#生词标注代码做不用-aizh_annotatepy638) · 19. [复习收尾提醒](#复习收尾提醒701) · 19b. [顶栏后台任务指示器](#顶栏后台任务指示器821) · 20. [AI 词典页 /dict](#ai-词典页-dict746) · 21. [书籍阅读器](#书籍阅读器836)

**参考**
22. [API 接口](#api-接口) · 23. [测试](#测试tests-pytest-tests-全套约-11-秒) · 24. [规范与约束](#规范与约束)

---

## 固定规则（MANDATORY RULES）

| # | 规则 | 说明 |
|---|------|------|
| R1 | **永远不要直接推送到 `main`** | 所有工作必须通过 PR |
| R2 | **每个功能都需要一个 Issue** | 先创建 Issue，再写代码；Issue/PR/提交信息全部用中文 |
| R3 | **CI 通过后 Claude 自行合并 PR** | （2026-07-05 起，由 Daniel 授权）Claude 完成整个流程：Issue → 分支 → PR → CI 通过 → `gh pr merge`。CI 失败绝不合并；Daniel 随时可事后审查或回滚 |
| R4 | **按 Daniel 的语言回答** | 他用法语写 → 全程法语；用中文/德语/英语/混合写 → 全程中文（2026-08-23 起，他开始学法语）。见"语言指令" |
| R5 | **Claude 自己执行 Git/gh 步骤** | 直接运行 `gh issue create`、`git checkout -b`、`gh pr create` 等，无需等待 Daniel |
| R6 | **CLAUDE.md 是唯一事实来源** | 所有架构决策都在这里记录 |
| R7 | **任何新代理都能接手** | 每个 Issue/PR 必须自给自足，不依赖聊天记录 |
| R8 | **开始任务前必须先查看 Issue** | 运行 `gh issue view <编号>` 读取完整背景 |
| R9 | **所有代码修改必须在分支上进行** | 先 `git checkout -b <分支名>`，再改文件 |
| R10 | **永远不要使用日语** | 即使话题涉及汉字，也只用中文或英文表达 |
| R11 | **每条消息开头必须改写 Daniel 的输入** | 先定回答语言（见 R4），再把他的消息改写为**该语言**的正确版本，画分割线，再回答。这是帮助 Daniel 学语言的核心机制，绝不可跳过 |
| R12 | **调查问题时定期汇报进展** | 每读几个文件就向 Daniel 汇报发现了什么、还缺什么 |
| R13 | **给 Daniel 的终端命令一律打包成临时脚本，且必须带分步日志** | 让 Daniel 复制粘贴多行命令经常因格式问题（换行/引号）失败。凡需要他在终端执行的操作，写成一个简短的临时脚本文件让他 `bash xxx.sh` 运行，用完删除。脚本必须：① 分步编号的 `echo` 进度提示（`== 步骤 2/4：… ==`）；② 每步说明在做什么、慢的步骤注明预计耗时；③ 结尾提示"把以上全部输出发给 Claude"——日志既让 Daniel 实时看到进展，也是 Claude 事后诊断的唯一依据（Daniel 2026-07-12 确认此做法很好，保持） |
| R14 | **Opus 编排，Sonnet 执行** | （2026-08-14 起，由 Daniel 决定）主会话是编排者：读代码、定方案、写施工图、审查结果、跑 Git/gh 流程；**实现代码派给 Sonnet 子代理**。见"代理编排"章节 |
| R15 | **回答又短又简单** | （2026-08-14 起）中文不是 Daniel 的母语，长段中文读起来吃力，写长了他就不看了。默认 3–5 句话讲完，能用列表就不用段落，不复述过程、不写总结段。见"语言指令"末尾 |

---

## 语言指令

### 第零步 - 决定回答语言（2026-08-23 起，Daniel 开始学法语）

| Daniel 的消息 | 整条回答的语言 |
|---------------|----------------|
| 法语 | **法语** |
| 中文 | 中文 |
| 德语 / 英语 / 混合 / 只有代码或报错 | 中文（默认） |

- **判断依据是他写的语言，不是话题**：他用中文问法语功能，照样用中文回答
- **一条消息只用一种语言回答**，不做双语对照——两种语言并排等于都不读
- 中法混合时看**主体句子**是哪种语言；分不出来就用中文
- 定下来之后，下面三步全部用这门语言执行

---

### 第一步 - 用回答语言重写用户的消息

> ⚠️ **这是最高优先级规则。无论对话持续多久、上下文多长，每一条消息都必须执行此步骤，绝无例外。**
>
> 🔴 **自检：在写任何回答之前，问自己："我是否已经把 Daniel 的消息改写为正确的中文（或法语）？"如果没有，立刻回去做这一步。**

- Daniel 会用中文、法语、英语、德语（déyǔ - German）或几种混合来写消息——全部都要改写为**第零步定下的那门语言**的干净、正确的版本
- 如果他的原文有错误，在重写时纠正，并在改写句子的**正下方**用以下格式列出所有纠正：
  ```
  📝 ~~错误写法~~ → 正确写法（拼音或读音 - 解释，可选）
  ```
  - 中文例子：📝 ~~调强~~ → 加强（jiāqiáng - strengthen）
  - 法语例子：📝 ~~je suis allé à le magasin~~ → je suis allé **au** magasin（à + le = au）
- 已经完美时，按原样重写，不加纠正行
- 他用了别的语言的词时，替换为回答语言的说法，并在第一次出现时加注释：拉取请求（lāqǔ qǐngqiú - Pull Request）/ la pull request（*Pull Request*）
- **例外：** 粘贴的终端输出、报错信息、代码片段跳过纠正，直接回答

**例子：**
| 用户输入 | 第一步输出 |
|----------|------------|
| `how do I fix this bug?` | 我怎么修复这个错误？ |
| `我如何implement这个feature？` | 我怎么实现这个功能？ |
| `这个function为什么return了None` | 这个函数为什么返回了None？ |
| `kannst du meine Anfrage korrigieren?` | 你能纠正我的提问（tíwèn - question）吗？ |
| `数据库的schema是什么` | 数据库的模式是什么？ *(已正确，按原样重写)* |
| `est-ce que tu peux corriger ce bug ?` | Est-ce que tu peux corriger ce bug ? *(已正确，按原样重写；下面整条回答用法语)* |
| `je veux ajouter un mot dans la base de donnée` | Je veux ajouter un mot dans la base de **données**. 📝 ~~base de donnée~~ → base de données（这个词组里 *données* 恒为复数） |

### 第二步 - 绘制分割线

在重写的问题下面画一条分割线，然后在下面开始回答。

### 第三步 - 回答

**用中文回答时：** HSK5 级及以上的词这样写：文件（wénjiàn - file）

> ❌ 错误：这个函数返回了一个异步生成器。
> ✅ 正确：这个函数返回了一个异步（yìbù - asynchronous）生成器（shēngchéng qì - generator）。

**用法语回答时（Daniel 法语 B1）：** 同样的道理，B2 级及以上的词第一次出现时加德语注释：une contrainte（*Einschränkung*）

> ❌ 错误：Cette fonction renvoie un générateur asynchrone.
> ✅ 正确：Cette fonction renvoie（*gibt zurück*）un générateur asynchrone.

- 技术术语和代码标识符（`queue_manager.py`、`FSRS`、`commit`）一律保持原样，两种语言都不翻译
- **R10 依然成立**：永远不用日语
- Issue / PR / 提交信息**永远用中文**，与回答语言无关——那是仓库的语言，不是对话的语言

#### 回答必须又短又简单（R15，Daniel 2026-08-14 要求）

**中文和法语都不是 Daniel 的母语，长段外语读起来很吃力。回答长了他就不看了——那等于没回答。这条对两种回答语言同样成立。**

- **默认 3–5 句话讲完**。做完一件事就说：改了什么、结果如何、有没有要他决定的
- **能用列表就不用段落**；一条一行，不写长句套长句
- **不要复述过程**：他不需要知道我读了哪些文件、试了哪些命令。只要结论
- **不要写总结性的收尾段**（"总的来说…"）——那是纯粹的重复
- **代码/命令/报错原文照贴**，不用改写成中文叙述——这些他看得懂，反而是最快的沟通方式
- **例外**：他明确要求详细解释时，或者需要他做决定、必须列清取舍时。**即便如此也先给结论，细节放后面**

> ❌ 错误：写了三段话解释某个函数的历史演变、当初为什么这么设计、以及我如何验证。
> ✅ 正确：「已修复：`queue_manager.py` 少了失效缓存的调用。测试通过。要我合并吗？」

---

## 代理编排：Opus 编排，Sonnet 执行

（R14，2026-08-14 起）主会话（Opus）是编排者（biānpái zhě - orchestrator），不是打字员。**实现代码交给 Sonnet 子代理**——Opus 的上下文用来理解系统、做决定、把关，不该消耗在敲重复的代码上。

### 分工边界

**Opus 自己做（不派）：**
- 读代码、定位问题、设计方案——需要全局上下文的事
- 写施工图（要改哪些文件、每处改什么、怎么验收）
- 审查子代理交回的改动
- **全部 Git/gh 操作**：Issue、分支、提交、PR、合并
- 与 Daniel 的所有对话：R11 改写、R12 进展汇报、最终结论

**派给 Sonnet 子代理：**
- 按施工图实现功能 / 修复缺陷
- 写测试、跑测试、修测试
- 机械性重构、批量改名、格式统一
- 大范围代码搜索（用 `Explore` 代理，它只回结论不回文件全文）

**判断标准：** 这件事需要"知道整个项目为什么这样设计"吗？需要 → Opus 做；只需要"照着说明改这几个文件" → 派 Sonnet。

### 施工图必须自给自足

子代理**拿不到本次对话历史**（R7 的同一条道理，只是对象从"未来的代理"变成"现在的子代理"）。任务描述里必须写全：

1. **背景**：这是在解决什么问题，关联哪个 Issue
2. **精确路径**：`routes/story.py` 的 `_generate_and_store()`，不是"故事生成那块"
3. **现有约束**：比如"数据库访问只能走 `database/` 包，别处不写原始 SQL"、"前端无构建步骤，直接改 `static/`"——子代理不会自己读 CLAUDE.md 全文
4. **验收方式**：跑哪条 `pytest tests/test_xxx.py`，或者具体检查什么

一句含糊的"实现 X"换回来的是一份要重写的代码，比自己写还慢。

### 硬性约束

- **子代理不碰 Git**：它们只改文件；`git commit`、`git push`、`gh pr create` 全部由 Opus 做。否则并行会话会互相切分支（#573/#574 曾因此撞车）
- **交回后必须审查再提交**：至少读一遍 diff，确认没有偏离施工图、没有引入项目禁止的写法（原始 SQL、静默吞异常、假装成功）。**审查不通过就打回重做，不要自己默默补救**——那等于施工图没写清楚，下次还会犯
- **并行任务各用 worktree**：多个子代理同时改同一个工作目录会互相覆盖。`.claude/worktrees/` 里的目录由 Claude Code 自动管理，不要手动编辑里面的文件
- **一次只派一个方向的任务**：两个子代理改同一个文件必然冲突，拆任务时按文件边界拆

---

## Git & GitHub 工作流

标准 **GitHub Flow**：议题（yìtí - Issue）→ 分支 → 拉取请求 → CI → 合并。**永远不要直接提交到 `main`。**

```
1. 创建议题（中文标题/描述；标签用中文：新功能/程序错误/数据库/前端/后端/ai/设计/文档）
2. git checkout main && git pull && git checkout -b feat/42-短名称
3. 频繁提交（每个原子单元一次）
4. gh pr create（中文描述，引用议题：Closes #42）
5. CI 通过 → gh pr merge <编号> --merge --delete-branch
```

**分支命名：** `feat/42-db-migrations`、`fix/55-review-parent-deck`、`docs/...`、`chore/...`

**提交信息（Conventional Commits，中文）：** `feat:` | `fix:` | `refactor:` | `chore:` | `docs:` | `test:`
每完成一个原子单元就提交——判断标准：提交后代码仍可运行，且无法再拆分而不丢失意义。提交太频繁的代价是零，丢失工作的代价是几天（我们曾因 `git reset --hard` 丢过 6 天工作）。

**CI（`.github/workflows/ci.yml`）：** Python 语法检查 → 导入检查 → 服务器启动检查（`/api/decks` 返回 200）。CI 失败的 PR 不可合并；`main` 受保护，只能通过 PR 修改。

**合并前自检清单：** ① CI 全绿；② PR 引用议题（Closes #N）；③ 本地做过语法/导入检查；④ 有功能改动的 PR 已测试。

**可交接（jiāojiē - handoff）原则：** 每个 Issue 描述背景、目标、完成标准；每个 PR 说明改了什么、为什么、怎么测试。开始新任务前先问："如果我现在离开，另一个 AI 能从 Issue/PR 历史完全理解项目状态吗？"否定就先补文档。

**Worktree：** `.claude/worktrees/` 里的目录是代理的临时隔离工作空间，由 Claude Code 自动管理，不要手动编辑里面的文件。并行任务必须各用一个（见"代理编排"）。

### 网络问题应急方案

`gh` 命令报 `EOF` 错误、或 `curl -sv https://api.github.com` 返回 `198.18.x.x` 这类假 IP 时，说明有代理/VPN 在拦截：**不要反复重试**，立刻把所有 `gh` 命令写入脚本（含 `echo` 进度提示，见 R13），让 Daniel 关闭代理后运行 `bash script.sh`，完成后删除脚本。

> 2026-08-12 起 Daniel 不在中国、日常不开 VPN，GitHub 连不上**不要再默认归因于 VPN**——先看具体报错。

---

## 项目简介与技术栈

供个人使用的间隔重复（jiàngé chóngfù - Spaced Repetition）系统，为一位用户（Daniel，中文 HSK 4–5，法语 B1）打造。它用 AI 驱动的复习体验取代 Anki：每天根据到期词汇生成上下文故事。

**技术栈：**
- **后端：** Python + FastAPI；**数据库：** SQLite（标准库 `sqlite3`，无 ORM）
- **前端：** `static/index.html` + `app.js` + `style.css`，FastAPI 直接提供，**无构建步骤**（无 npm）
- **AI：** 多提供商（`ai.py`）——默认 `deepseek-chat`；也支持 ZhipuAI GLM、Qwen、Claude、OpenAI
- **语音合成（TTS）：** `edge-tts`（中文 `zh-CN-XiaoxiaoNeural`）
- **语言：** 界面标签英文，内容中文/法文

---

## 项目结构

```
├── CLAUDE.md              # 本文件
├── main.py                # CLI 入口 + FastAPI 应用（含 Basic Auth 中间件）
├── languages.py           # 语言注册表（每种语言的 TTS/翻译源/分词/AI 提示词参数/功能开关）
├── schema.sql             # 数据库模式
├── database/              # 所有数据库访问（其他文件不写原始 SQL）
│   ├── core.py            # 连接管理、迁移
│   └── cards.py / decks.py / entries.py / presets.py / stories.py / browse.py / stats.py / podcast.py
├── routes/                # FastAPI 路由模块
│   ├── browse.py / decks.py / imports.py / review.py / story.py / podcast.py / knowledge.py（`POST /api/knowledge/add`，#651/#652）
│   ├── tasks.py           # 顶栏后台任务指示器（#821）：`GET /api/tasks` 聚合各子系统已有的进度状态
│   ├── books.py           # 书籍阅读器 API（#836）：上传/列表/删除/取一页/存进度
│   ├── sync.py            # 一键同步（#625，只在笔记本实例注册）
│   ├── queue_manager.py   # Anki v3 风格持久会话队列
│   └── utils.py           # 共用工具（DISABLE_AI, leaf_ids, queue_manager 单例）
├── static/                # 前端（index.html + app.js + style.css；shared.js = 两页共用的 api()/addWordViaAi()，add.html = 独立加词页 #668，save.html = 独立收藏页 #681，dict.html = 独立 AI 词典页 #746，login.html = 登录页 #666；这三个独立页都故意不加载 app.js）
│
│   # ── 调度与导入 ──
├── srs.py                 # 调度编排：学习步骤、状态转换，调用 fsrs.py
├── fsrs.py                # FSRS-5 纯算法模块（DSR 记忆模型，无依赖）
├── importer.py            # YAML 词汇导入器（中文 + 法语格式）
├── yaml_fixer.py          # 修复 AI 生成的格式错误 YAML
│
│   # ── AI、内容与语音 ──
├── ai.py                  # AI 提供商调用（每种提示词类型一个函数）
├── news_fetcher.py        # 新闻抓取（Tagesschau API + RSS；按天缓存 data/news_cache/）
├── podcast.py             # 播客爬虫（#479）：播客 RSS 直链发现新单集（#497，退役 YouTube/yt-dlp）、每源 auto_process 开关+非自动源只入库元数据（#502，podcast_feeds 表）、转录链 NotebookLM 免费主力+听悟+Whisper 保底、单步异常不中止整链（#510 重排，链式降级，原 #498/#485/#486）、摘要 NotebookLM chat.ask 免费优先+DeepSeek/gpt API 链回退（api 路径内部 DeepSeek 优先省钱，#532；勾了 china-kritisch 的素材跳过 DeepSeek 直接用 OpenAI，#731）、邮件通知+Signal 通知（signal-cli 关联设备，发 Note to Self，#521，二者独立可选、互不影响；消息抬头播客名·星期·日期、链接在末尾，单集日期按 Europe/Berlin 显示，#532）、摘要 table.media 风格（`<p>` 段落+每段首句 `<b>` 加粗总结，#567）+详情页 Regenerate summary 按钮、邮件主题=`播客名 - 单集标题`（查不到播客名只用标题，不要退回死前缀）+ `summary_zh` 开头中文总结（#708 起是 `summary_de` 的**完整翻译**：同段落数、同顺序、同事实，同样 `<p>`+段首 `<b>`，HSK4-5 用词；提示词里德语先写、中文后译，JSON 里 `summary_de` 排在前面；渲染三处——邮件 `podcast._summary_zh_html`、详情页 `app.js._summaryZhHtml` 均"先全转义再放行 `<p>/<b>/<strong>/<em>/<i>/<br>`"，Signal 用 `_summary_to_plain_text` 剥标签；#708 之前的旧条目是纯文本，两处渲染都按空行补 `<p>`；**是增量不是必需**——成功判定只看 `summary_de`，模型漏掉中文总结不能让整集失败）+ 摘要里任何 HSK5+ 中文概念都标 `pinyin/汉字`（不限于提取的词表，宁多勿少，#631）；已泛化为知识库存储层，见 `knowledge/` 包
├── annotate/              # 知识库生词标注分派（#804）：__init__.py 按 languages 的 annotator 字段分派，zh 走 zh_annotate（原样不动），romance.py 是法语/西语实现（entry_forms 精确匹配，零词干还原）+ stopwords_fr/es.txt 功能词表
├── knowledge/             # 知识库摄取（#650–#655，播客功能泛化，见「知识库」节）：rendition.py（按语言渲染摘要，#804）、youtube.py（字幕摄取）、article.py（正文抽取）、instagram.py（Reel/Post 摄取，yt-dlp 元数据+音频下载，#750）、files.py（上传的 txt/md/pdf/docx 抽文本，#835）、ingest.py（唯一入库管线）、mailbox.py（IMAP 邮件收件）、newsletter.py（已知邮件通讯的发件人注册表+样板清洗，#925）、signal_inbox.py（Signal Note to Self 分享收件，含 text 前缀粘贴正文，#749/#834）
├── books/                 # 书籍阅读器（#836）：epub.py（纯标准库 zipfile+ElementTree）、
│                          #   pdf.py（pypdf，按页抽文字层+记真实页码，不做 OCR）、paginate.py（定长切页）、
│                          #   __init__.py 的 ingest_file() 是唯一入库入口
├── zh_annotate.py         # 生词标注（#638，零 AI）：HSK 表+词库+jieba+pypinyin+谷歌翻译
├── translator.py          # 翻译（Google Translate 免费网页端点，纯标准库；UA 必须伪装成浏览器，#890）
├── tts.py                 # edge-tts 封装（离线模式下只读缓存，#612）
├── routes/dictionary.py   # AI 词典 API（#746）：/api/dict/lookup + 历史；结果存 dict_queries（database/dictionary.py）
├── review_notify.py       # 复习收尾提醒（#701）：去重 + 发信，判定在 database.due_notification_status()
│
│   # ── 本地 / 离线模式（#612、#625）──
├── offline.py             # OFFLINE_MODE / LOCAL_MODE + 联网探测
├── sync_offline.sh        # 同步 sync/pull/push
├── run.local.sh           # 本地模式启动，日常用（#625）
├── run.offline.sh         # 硬离线启动，飞机用（#612）
│
│   # ── 运维与文档 ──
├── requirements.txt       # Python 依赖清单
├── DEPLOY.md              # 服务器从零到上线的部署教程
├── deploy/                # systemd 单元、Caddyfile 示例、deploy.sh（自动部署）
├── scripts/               # morning_pregen.py（早晨预生成故事+TTS）、podcast_check.py（播客爬虫定时脚本）、due_check.py（复习收尾提醒定时脚本，#701）、knowledge_mail_check.py（知识库邮件收件定时脚本，#655）、signal_check.py（知识库 Signal 分享收件定时脚本，#749）、offline_sync_server.py + offline_tts_manifest.py（离线同步，#612）、fsrs_optimize.py（用 review_log 训练个人 FSRS 权重，#629）+ README
├── docs/yaml-format.md    # YAML 词条格式完整文档
├── docs/knowledge-base.md # 知识库功能总体设计（#650–#655 各 Issue 引用的唯一设计说明）
└── data/
    ├── srs.db             # SQLite 数据库（生产版在服务器上！）
    ├── books/             # 上传的 EPUB/PDF 原件（#836，不进离线同步）
    ├── news_sources.json  # 新闻来源配置（不在 git 里，服务器上已有）
    └── tts/               # TTS 音频缓存
```

---

## 生产环境（2026-07-07 上线）

系统运行在一台 Linux VPS 上，Daniel 通过手机/电脑浏览器访问 `https://powerdaniel3000.duckdns.org`（登录保护；凭据不入库——仓库是公开的）。

### 登录与认证

- **登录是 HTML 表单 + 长期签名 Cookie（#666）**，不是 HTTP Basic Auth：iOS 钥匙串只保存**表单**登录，原生 Basic 弹窗它一律不存也不自动填，Daniel 每次进都要手打密码。`GET/POST /login`（`static/login.html`，两个输入框必须带 `autocomplete="username"` / `current-password`，这正是钥匙串识别的前提）→ 校验通过下发 `anki_session` Cookie（HMAC 签名，含过期时间戳，有效期一年，HttpOnly/SameSite=Lax，HTTPS 下 Secure）。签名密钥由 `AUTH_USERNAME`+`AUTH_PASSWORD` 派生 —— 改密码即自动作废全部旧会话，无需存密钥
- **Basic Auth 保留作回退**（curl/脚本），中间件顺序是 Cookie → Basic → 拒绝
- **未认证时 `/api/*` 必须返回 401 JSON，不能重定向**：给 `fetch()` 一个 200 的 HTML 登录页会让所有前端请求"成功"拿到垃圾。页面路径才 303 跳 `/login`；`app.js` 的 `api()` 收到 401 自动跳登录页
- Caddy 反代后应用自己收到的是明文 HTTP，Cookie 的 `secure` 标志按 `X-Forwarded-Proto` 判断

### 数据库与部署

- **唯一生产数据库在服务器上**（`/home/anki/biangbiangmian3000/data/srs.db`）。本地开发只用 `run.dev.sh` + `data/dev.db`。**本地的 `data/srs.db` 已过时，绝不要把它当作现状或复制回服务器。**
- **自动部署：** 服务器 cron 每 2 分钟运行 `deploy/deploy.sh`——**PR 合并到 main ≈ 2 分钟后自动上线**（拉取、装依赖、重启 systemd 服务 `biangbiangmian3000`）
- **自动备份：** 服务器 cron 每 6 小时把数据库快照到 `data/backups/`
- HTTPS 由 Caddy 反向代理提供（证书自动续期）；从零搭建教程见 `DEPLOY.md`

### Claude 可以直接 SSH 上服务器

**Claude 有服务器的 SSH 免密访问权限，排查线上问题时直接连上去看，不要让 Daniel 手动跑命令转述结果。**

- **排查线上故障第一步永远是看日志**：`journalctl -u biangbiangmian3000` —— 播客卡死（#565/#566）、早晨预生成失败（#517）这些事故，日志里都写着原因，靠猜要多花几小时
- 常用途径：读 systemd 服务状态与日志、看 cron 是否在跑、确认部署是否真的上线了（`deploy/deploy.sh` 每 2 分钟一轮）、查生产数据库的**只读**问题
- **服务器时区是 `Asia/Shanghai`（UTC+8），不是德国时间**（2026-08-14 实测确认）：`anki_today()` 用 `datetime.now()` 即服务器本地时间，所以 Anki 日分界按中国时间算。诊断"今天有多少卡到期"时按德国时间推算必然对不上
- 🔴 **生产库上的写操作要先问 Daniel**：`/home/anki/biangbiangmian3000/data/srs.db` 是唯一真库。只读查询（`SELECT`）随便跑；`UPDATE`/`DELETE`/迁移必须先说清楚要改什么、影响多少行，得到同意再动。备份是每 6 小时一次，最坏能丢 6 小时
- **改代码仍然走 PR，不要 SSH 上去直接改文件**：服务器每 2 分钟 `git pull`，手改的文件下一轮就被覆盖，而且改动不进版本库、没人知道（R1/R9 在服务器上同样成立）
- **具体连接方式（主机、用户名、密钥路径）保存在 Claude 的项目记忆中，不写入公开仓库** —— 本仓库是公开的

---

## 笔记本模式：本地 + 离线（议题 #612、#625）

笔记本上跑一份完整的应用，像 Anki 桌面版。**服务器永远是主库**，笔记本是副本。

```bash
bash run.local.sh            # 日常（2026-08-04 起，#625）：有网全功能，断网自动降级
bash run.offline.sh          # 飞机上：硬离线，连探测都不做
# 同步：界面顶栏 ⟳ 按钮，或者命令行
bash sync_offline.sh sync    # 日常：先推后拉，一步到位
bash sync_offline.sh pull    # 只拉不推（放弃本地改动）
bash sync_offline.sh push    # 只推不拉，推完归档本地库
```

两种模式共用 `data/offline.db`，在家同步好直接带上飞机。

### 两种模式的区别

- **`LOCAL_MODE=1`（`run.local.sh`，#625）**：日常实例。有网时 AI 故事、edge-tts 生成、翻译全部可用；断网后自动降级成下面 `OFFLINE_MODE` 的行为，**不用重启**。判断靠 `offline.network_available()`：带缓存的 TCP 探测（在线缓存 60 秒、离线 10 秒，超时 1.5 秒，探测目标 `LOCAL_MODE_PROBE_HOSTS`，默认 DeepSeek + Bing 语音端点——在中国不用 VPN 就能连）
- **`ai_disabled()` 必须是函数不能是常量**：原来 `routes/utils.DISABLE_AI` 是模块级常量，进程启动时就冻死了，本地模式下 Wi-Fi 回来也解不开。改函数时 story/review/podcast 的调用点都要跟着改
- **`OFFLINE_MODE=1`（`run.offline.sh`）**：飞机专用的硬开关，保留不变——需要"绝不发出站连接"的保证，连探测都不做。隐含 `DISABLE_AI`；TTS 只读 `data/tts/` 缓存，未命中抛 `tts.NotCachedOffline` → `/api/tts-file` 返回 404。**关键**：无网时 edge-tts 的 WebSocket 会挂到超时，拖死整个请求，所以缓存未命中必须立刻失败
- 故事沿用同步下来的那一份（`DISABLE_AI` 分支本来就是"有缓存就返回、没有就返回 None"）；Again 单句重生成自动跳过
- `GET /api/mode` → `{offline, local, hard_offline}`；`offline` 是**实时值**，本地模式下前端每 60 秒轮询一次，翻转时重放 `showView(_currentView)` 让 Regenerate 按钮跟着出现/消失
- `PORT` 环境变量（默认 8000）让离线实例跑 8001，不占用 Daniel 浏览器连的端口

### 同步

- **界面一键同步（#625）**：顶栏 ⟳ 按钮 →`POST /api/sync/start[?mode=sync|pull]` 起后台线程跑 `sync_offline.sh`，`GET /api/sync/progress` 逐行回传脚本的中文分步日志，成功后失效队列缓存 + 重读日界点 + 清空探测缓存，前端整页刷新（库文件已经被换掉了）。**这两个路由只在 `LOCAL_MODE`/`OFFLINE_MODE` 下注册**（`main.py`）——服务器上必须根本不存在，误调一次就会用某份笔记本快照覆盖生产
- **合并被拒时要有出路**：无令牌（手工拷贝的库）或令牌已轮换（推过一次了）时 merge 会拒绝，这是对的，但用户会永远卡住。`mode=pull` 是逃生口：只下载、覆盖本地。前端只在日志里出现 `sync token` 时才显示 ⤓ 按钮，并用 `showConfirm` 明说未同步的复习会丢失
- **同步只合并 `cards` + `review_log` 两张表**（`scripts/offline_sync_server.py`，纯标准库，通过 ssh stdin 管道执行，不依赖服务器已部署该版本）。离线期间服务器 cron 仍在写入（播客单集、预生成故事、成本日志），整库对拷会毁掉这些数据——**绝不整库覆盖**
- **同步令牌**（`app_settings.offline_sync_token`）：`pull` 时写入服务器并随快照带走，`push` 时两边必须一致，合并后轮换 → 同一份离线库无法重复 push；手工拷贝的库没有令牌，直接拒绝
- **冲突按 `cards.last_review` 谁晚谁赢**（#625）：原来笔记本无条件覆盖服务器，飞机上没问题，但本地模式变日常后，手机上复习完再一同步就静默丢进度。`review_log` 两边的记录始终都保留，所以调度输了的那次复习仍然进统计
- **下载先落 `.incoming` 再 `mv` 就位**（#625）：应用可能正开着这个库，`scp` 直接覆盖会让它好几秒读到写了一半的文件；rename 是原子的。换库后还要删掉可能残留的 `-wal`/`-shm`/`-journal`，旧日志套新库会直接损坏数据
- **传输量优化**（实测链路 ~2.7 MB/s，整个 pull 十几秒；这两项优化仍然保留，只是并非为了救命）：
  - 快照**瘦身**：`prepare` 在快照上（不动生产库）清空 `api_call_log`、`podcast_episodes.transcript_*`、`stories.prompt_text` 再 VACUUM → 29.6 MB 降到 18.5 MB。`prepare … --full` 可保留全部
  - 音频**按需**：`scripts/offline_tts_manifest.py` 从拉下来的库算出真正会用到的语音（故事句子 `sentence_zh` + 到期词 `word_zh`，前端只请求这两种），再与服务器实际存在的文件取交集 → 一百多个文件 / 几 MB，而不是整个 118 MB 的 `data/tts/`。**必须取交集**，否则 rsync 会为缺失文件返回非零码，`set -e` 会中断整个脚本

### 改这些脚本时的坑

- **中文提示里紧跟变量必须写 `${VAR}`**：`"…下载到 $LOCAL_DB（约 7 MB）"` 会让 bash 把全角括号的字节也算进变量名，配合 `set -u` 直接退出（#619）。这个仓库的脚本提示全是中文，极易踩
- **脚本改动必须实机跑一遍，`bash -n` 不够**：它只查语法，`set -u` 的 unbound variable、选项不被支持（#617）都要到运行时才暴露。离线同步脚本用 `ANKI_REMOTE_DIR=/tmp/xxx`（服务器上的副本靶子）+ `ANKI_LOCAL_DB=/tmp/yyy.db`（临时本地库）就能安全演练，**两个都要设**——只设前者会给真的 `data/offline.db` 盖上演练令牌，之后它再也推不回生产库了
- **rsync 只能用两边都支持的选项**：脚本跑在 Daniel 的 macOS 上，系统自带的是 **openrsync**，不认 GNU 的 `--info=progress2`（#617 因此挂掉）。用 `--progress`。凡是只在服务器上验证过的命令，都要想一想 Mac 上是不是同一个实现
- 测试见 `tests/test_offline_sync.py`

---

## 启动、CLI 与环境变量

```bash
bash run.sh          # 生产启动（读取 .env，清理 8000 端口）——服务器上由 systemd 代替
bash run.dev.sh      # 开发启动（DB_PATH=data/dev.db，DISABLE_AI=1）
bash run.local.sh    # 本地模式启动（LOCAL_MODE=1，端口 8001）——见"笔记本模式"
bash run.offline.sh  # 硬离线启动（OFFLINE_MODE=1，DB_PATH=data/offline.db，端口 8001）——见"笔记本模式"
python main.py import                # 导入 imports/ 下的 YAML（目录需存在）
python main.py status [--deck X]     # 显示每个牌组/类别的到期数量
```

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ANTHROPIC_API_KEY` | 必填 | Claude API 密钥 |
| `DEEPSEEK_API_KEY` / `ZHIPU_API_KEY` / `QWEN_API_KEY` | 可选 | 其他 AI 提供商密钥 |
| `OPENAI_API_KEY` | 可选 | 新闻/briefing 模式（DeepSeek 会审查新闻内容，故用 OpenAI），以及 china-kritisch 素材的摘要（#731）。模型由 `BRIEFING_MODEL` 决定，默认 `gpt-5.6-luna`，回退链 luna → terra → `gpt-5-mini`。**`gpt-5.1` 自 #731 起全应用停用**（同样的活贵六倍），价格表里保留它只为解析历史成本记录 |
| `DB_PATH` | `data/srs.db` | 数据库路径（开发用 `data/dev.db`） |
| `DISABLE_AI` | `0` | 设为 `1` 跳过 AI 故事生成 |
| `OFFLINE_MODE` | `0` | 设为 `1` 进入硬离线模式（#612）：隐含 `DISABLE_AI`，TTS 只读缓存，零网络请求，连探测都不做 |
| `LOCAL_MODE` | `0` | 设为 `1` 进入本地模式（#625）：有网全功能，断网自动降级为离线行为 |
| `LOCAL_MODE_PROBE_HOSTS` | `api.deepseek.com,speech.platform.bing.com` | 本地模式判断有没有网时探测的主机（443 端口，任一连通即算在线） |
| `ANKI_LOCAL_DB` | `data/offline.db` | 同步脚本的本地库路径；配合 `ANKI_REMOTE_DIR` 做演练时必须一起设 |
| `PORT` | `8000` | 服务监听端口（`run.offline.sh` 用 8001） |
| `LOG_LEVEL` | `INFO` | 日志级别（`DEBUG` 输出详细日志） |
| `DEV_CLEAR_DB` | `` | 设为任意值启动时清空数据库——生产环境绝不要设置 |
| `AUTH_USERNAME` / `AUTH_PASSWORD` | 可选 | 两者都设置时启用登录保护（保护所有路径，`/login` 除外）。主流程是 HTML 表单登录 + 一年期签名 Cookie（#666），Basic Auth 保留作 curl/脚本的回退 |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM` | 可选 | 播客爬虫（#479）邮件通知用；`SMTP_PORT` 默认 587（STARTTLS）；未配置时跳过发信，记日志，不算失败 |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | 可选 | 播客爬虫用 Spotify Web API 搜索单集链接；未配置时退化为 Spotify 搜索链接 |
| `PUBLIC_BASE_URL` | `https://powerdaniel3000.duckdns.org` | 播客邮件/Signal 通知里转录页链接的域名前缀 |
| `SIGNAL_ACCOUNT` / `SIGNAL_CLI_PATH` | 可选 | 播客爬虫（#521）Signal 通知用，**以及**知识库 Signal 分享入口（#749）的收件用；`SIGNAL_ACCOUNT` 是 Daniel 关联设备所属号码（如 `+49…`），`SIGNAL_CLI_PATH` 默认 `signal-cli`；`SIGNAL_ACCOUNT` 未配置时发送/收件都跳过，记日志，不算失败。一次性 signal-cli 安装/扫码关联步骤见 `scripts/README.md`（两个用途共用同一次关联，不用配两遍） |
| `ALIBABA_CLOUD_ACCESS_KEY_ID` / `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | 可选 | 播客爬虫（#498）通义听悟（转录主力）用的阿里云 AccessKey；未配置时自动跳过，落到 Whisper/NotebookLM |
| `TINGWU_APP_KEY` | 可选 | 播客爬虫（#498）通义听悟控制台创建的应用 AppKey；与上面两个 AccessKey 变量任一缺失都会跳过听悟。一次性开通步骤见 `scripts/README.md` |
| `KNOWLEDGE_IMAP_HOST` / `KNOWLEDGE_IMAP_PORT` | 可选 | 知识库邮件收件（#655）的 IMAP 服务器；端口默认 `993`（SSL） |
| `KNOWLEDGE_IMAP_USER` / `KNOWLEDGE_IMAP_PASSWORD` | 可选 | 知识库邮件收件（#655）专用邮箱的登录凭据；三者（含上面两个变量）任一未配置时 `scripts/knowledge_mail_check.py` 直接跳过，不连接 |
| `KNOWLEDGE_MAIL_ALLOWED_SENDERS` | 可选 | 知识库邮件收件（#655）发件人白名单，逗号分隔的邮箱地址（不区分大小写，兼容 `Name <addr@x.de>` 格式）；**留空则整个邮箱检查被跳过，不处理任何邮件**——这是防止任何知道邮箱地址的人往服务器塞 URL 触发 AI 调用的唯一防线。已知邮件通讯的发件人（`newsletter@nl.faz.net`，#925）也必须列在这里 |
| `GROQ_API_KEY` | 可选 | Instagram Reel 转录（#750）的主力，Groq `whisper-large-v3-turbo`（约比 OpenAI 便宜 9 倍、快 10 倍）；未配置时自动回退已有的 OpenAI `whisper-1`（`OPENAI_API_KEY`），只是单价贵约 9 倍——不是本功能能否使用的前提。获取方式见 `scripts/README.md` |
| `INSTAGRAM_COOKIES_FILE` | `data/instagram_cookies.txt` | Instagram Reel 摄取（#750）用的登录态 cookies（Netscape 格式，`yt-dlp --cookies`）；文件不存在时公开 Reel 仍会尝试下载，不一定成功。会过期，过期时的错误信息会明说，一次性导出步骤见 `scripts/README.md` |
| `YT_DLP_PATH` | `yt-dlp` | Instagram Reel 摄取（#750）用的 yt-dlp 可执行文件路径；系统级命令行工具（同 `ffmpeg` 的处理方式），装在非默认位置时指过去 |

注意：uvicorn 直接启动不建表——测试前先手动 `database.init_db()`（`run.sh`/`main.py` 会自动处理）。

---

## 数据库模式（概述）

```
deck_presets → decks（自引用 parent_id，支持嵌套）→ entries
                                                      ├── entry_examples
                                                      ├── entry_measure_words（量词）
                                                      ├── entry_conjugations（动词变位，时态×人称，#596）
                                                      ├── entry_relations（同义词等关系）
                                                      ├── entry_components（sentence 类型的组成词）
                                                      ├── entry_characters → characters → character_compounds
                                                      └── cards → review_log

decks → stories → story_sentences → entries（外键）
```

- **entries.note_type：** `vocabulary` | `sentence` | `chengyu` | `expression` | `grammar`
- **entries 主要字段：** `definition`（英）、`definition_zh`（中）、`definition_de`（德）、`notes`、`source_sentence`、`grammar_notes`、`register`
- `cards.due` 是单一 TEXT 字段：学习/重学状态为 ISO 日期时间，复习状态为 ISO 日期
- `cards.state`：`new` | `learning` | `review` | `relearn` | `suspended`
- `cards` 的 FSRS 字段：`stability`、`difficulty`、`last_review`；另有 `step_index`（学习步骤位置）、`lapses`、`learning_again_count`、`is_leech`
- `stories` 没有唯一约束——同一（日期、类别、牌组）可有多条记录；最新 `generated_at` 为活跃故事，永不自动删除
- `story_sentences` 按位置将故事与词汇 1:1 关联

---

## 调度算法 —— FSRS-5（默认）+ SM-2 回退

复习阶段的调度自 2026-06（PR #343）起使用 **FSRS-5**（`fsrs.py`，DSR 记忆模型：Difficulty/Stability/Retrievability）。`enable_fsrs=0` 时回退到旧 SM-2（`srs.py` 的 `calc_review`）。FSRS 的难度向均值回归，消除了 SM-2 的"ease 地狱"。

> 🔴 **改任何调度参数前先跑 `SELECT preset_id, COUNT(*) FROM decks GROUP BY 1`。** Daniel 全部牌组都绑在 `deck_presets` 的 **id=2（"Anki Default"）**，preset 1（"Default"）上一张卡都没有。2026-07-15 那次调优改的是 preset 1，等于什么都没做，白白浪费三周才发现（#629）。

**生产实测参数（2026-08-08，#629）：** 内置默认权重对 Daniel 系统性乐观——实测遗忘率 `creating` 31%、`listening` 21%，且遗忘率随间隔的曲线是**平的**（1-3 天就忘 20.7%）。平坦曲线 = 整体校准偏差，不是长期衰退；连 1-3 天都记不住说明卡片毕业时根本没学扎实。已把 preset 2 改为 `desired_retention=0.95`、`learning_steps='1m 10m 1d 3d'`、`relearning_steps='10m 1d'`，并写入用 `scripts/fsrs_optimize.py` 训练出的个人权重（验证集 log loss +3.0%）。想重训直接跑该脚本（默认只读，`--write` 才写库）。

### 状态机
`new` → `learning`（学习步骤）→ `review`（毕业）；`review` 评 Again → `relearn` → 完成步骤后回 `review`。

### 学习/重学阶段（步骤制；默认 learning_steps=`1 10` 分钟，relearning_steps=`10`）
- **Again** → 回 step 0
- **Hard** → `learning_hard_1d` 开关（默认开）：任意步一律延迟 `learning_hard_days`（默认 1 天，可为小数）——让半记住的卡明天再现；开关关闭时用 Anki 经典行为（步骤均值/×1.5）
- **Good** → 推进一步；最后一步 → 毕业
- **Easy** → 立即毕业
- **短期记忆（#470）**：FSRS 开启时，步骤阶段每次作答都更新 S/D——新卡第一次作答即播种，之后 Again ×0.50 / Hard ×0.84 / 推进步骤的 Good ×1.41（短期公式 w17/w18）；毕业间隔与按钮预览因此随作答历史自适应（先 Again 再 Good ≈ 1 天，纯 Good 仍 ≈ 3 天）

### 毕业间隔
FSRS 用毕业评分播种初始 stability/difficulty：默认权重下 **Good ≈ 3 天，Easy ≈ 16 天**（`fsrs.init_stability`）。FSRS 关闭时用预设的 `graduating_interval`（1）/ `easy_interval`（4）。

### 复习阶段（FSRS）
- 每次复习按已过天数计算可提取性 R，更新 S/D；下次间隔 = R 衰减到 `desired_retention`（默认 0.9）所需的天数
- **Again** = 遗忘：lapses+1，温和降低 stability，进入 relearn 步骤
- 间隔上限 `maximum_interval`（默认 36500）；预览确定性显示、提交时才加 Anki 风格随机模糊（fuzz）；强制 Again<Hard≤Good<Easy 单调
- **Shift+S** 打开调度检查器面板（当前卡的 S/D/R 与每个按钮的结果）

### 难词（leech）
- 复习态：lapses ≥ `leech_threshold`（默认 3）→ 暂停并标记 `is_leech`
- 学习态：Again 次数 ≥ `learning_leech_threshold`（默认 6）→ 同上
- **Shift+L** 复习时手动标记难词

---

## 队列设计

`routes/queue_manager.py` 实现 Anki v3 风格的持久会话队列（SessionQueue/QueueManager）：
- 每个 Anki 日（凌晨 4 点为界）首次访问时构建一次，之后在内存中维护
- **主队列**：预先交错排列的卡片 ID（日内学习 + 复习 + 新建混合）；**日内学习队列**：按时间戳排序，每次取卡前检查
- 失效条件：Anki 日期变更、撤销、队列耗尽、删除/埋藏卡片；缓存键 `(mode, deck_id_or_ids, category)`
- `POST /api/review` 返回 `{next_card, counts}`——无需额外请求
- **有故事时的排序一律走 `database.story_sort_key()`，不许再写 `story_pos.get(word_id, <最大值>)`（#732）**：故事只覆盖**生成那一刻到期**的词，所以复习到一半加词再重新生成，早上剩下的卡（尤其按了 Again 的）必然被甩出新故事。原来三处排序都把它们赋成 9999 / `len(sentences)` / `inf`，于是排在**所有新卡之后**——新卡多的日子永远轮不到，而牌组角标照常显示，看起来就像卡片丢了。没有故事时走的是 learning-first（Anki 默认），这条保障在有故事时必须同样成立。分三组：① 不在故事里的**非新卡**（到期欠账，且没有句子可读）→ ② 故事内按叙事顺序 → ③ 不在故事里的**新卡**（生成之后才加进来或提升的词）
- **重新生成故事会覆盖当时全部到期的卡**，剩下没做完的卡因此和新词一起拿到新句子；生成之后才到期的卡（例如随后按了 Again）沿用旧句子，由 `_attach_again_sentence` 单独重生成

---

## 多语言支持

同一个软件、同一个数据库里学习多种语言（2026-07-06 起，议题 #428–#431）。当前：中文（zh，默认）+ 法语（fr，CEFR B1）+ 西班牙语（es，CEFR A2）——两种罗曼语的释义都以德语为主。

> **不分库是有意的决定（#803）**：知识库、`review_log`、统计、离线同步全部跨语言共享，分库要靠 `ATTACH`，`database/` 里每条查询都得改。语言隔离靠 `lang` 列 + `languages.py` 的语言族配置。

- **`languages.py` 是语言注册表**：每种语言定义 TTS 语音、翻译源、分词方式（jieba/空格）、AI 提示词参数、牌组树根名（`deck_root`，#726）、功能开关（拼音/汉字/量词仅中文）。加新语言 = 加一个条目
- **`decks.lang` / `entries.lang`**（默认 `'zh'`）：目标语言；子牌组创建时继承父牌组的 lang
- `word_zh` 对所有语言存"目标语言词形"（`_zh` 后缀是历史遗留）；法语词条的 pinyin/characters 留空
- **等级共用 1–6 整数**（#596）：`entries.hsk_level` 对中文存 HSK 1–6，对法语存 CEFR（A1=1 … C2=6，YAML 里写 `level: "B1"`）；故事设置弹窗的背景词汇难度滑块两种语言通用（1–6），前端按 `languages.py` 的 `level_system` 显示 "HSK 3" 或 "B1"，法语故事提示词用滑块值生成 "CEFR A1-X" 上限（原来写死 A1-A2）
- **动词变位**（#596）：`entry_conjugations` 表按"时态 × 人称"通用存储（person='' 表示分词等无人称形式，position 保留 YAML 顺序）；法语 YAML 用 `conjugations:` 映射导入；`/api/word/{id}` 返回 `conjugations`，词条详情页和复习卡背面渲染 Conjugation 折叠区（每时态一张小卡片）；同义反义词/相似句处理对法语也启用
- **词条按语言唯一**（#803）：`UNIQUE(word_zh, lang)`。原来是全局唯一，加西班牙语必撞车——法语和西语共享大量同形词（`capital`/`animal`/`total`），全局唯一会把新西语词静默认成已有的法语词条
- **语言族（#803）：** `languages.py` 里 `_SINITIC_BASE` / `_ROMANCE_BASE` 两个基底 dict，各语言用 `{**_ROMANCE_BASE, ...}` 继承再覆盖差异（`features` 必须显式合并，否则子语言会整块顶掉基底的开关）。中文是自成一族；法语、西班牙语同族，以后加葡语/意语就是再加一个条目。每种语言带 `family` / `annotator` / `features.conjugation|gender|inflection`
- **形态表 `entry_forms`（#803）：** 统一存动词变位和名词/形容词词形，取代只有"时态 × 人称"的 `entry_conjugations`（该表保留但代码不再读写，**唯一事实来源是 `entry_forms`**）。`kind='conjugation'` 时 `paradigm`=时态、`slot`=人称；`kind='inflection'` 时 `paradigm`=维度（`nombre`/`genre`）、`slot`=值（`pluriel`/`féminin`）。`idx_entry_forms_form` 是必需的：知识库标注靠 `database.forms_lookup(词形, lang)` 判断"这个词形属不属于我学过的词"，每篇文章要查几百次——**罗曼语的生词判定不做词干还原，全靠这张表**，所以加词时必须把全部变位/词形都生成出来
- **`entries.gender`（#803）：** 名词的阴阳性（`m`/`f`/`mf`），中文恒为 NULL
- **`entries.etymology`（#906）：词源取代罗曼语的 Word Analysis**：法语词没有汉字可拆，那个区块只剩一行重复词头。现在 `lang != 'zh'` 时复习卡右栏与词条详情页**不渲染** Word Analysis，改渲染 Etymology（德语散文，`renderEtymologySection()`）。中文侧一个字节不变——中文的词源在 `characters.etymology`，是**按字**的，两者不是一回事，所以列名分开、AI 字段名也分开（`entry_etymology` vs `etymology`），否则 `routes/browse.py` 里每个分支都会变成二义的
  - fr/es 的加词提示词输出**顶层 `etymology:` 块标量**，不再往 `note` 里塞 `**Étymologie:**` 行——藏在散文里的东西没法单独渲染，也没法单独重新生成
  - ↺ 走 `ai.generate_entry_etymology()`（纯文本提问，不是 JSON）：`regenerate_entry_fields()` 是**中文词典**提示词，它的 "etymology" 指的是字级的，拿它去问 `parler` 的汉字组成纯属烧钱。模型返回空 → 抛 `ValueError`，不拿空白盖掉好答案
  - **复习卡的行记录（`database/cards.py`）不 SELECT `etymology`**，所以复习中打开编辑框时该字段取自 `wordDetails`；两处都没有就整个字段隐藏、保存时也不发送——空文本框存回去会静默清空词源
- **`known_words` 按语言（#803）：** 主键改为 `(word_zh, lang)`；`database` 里的四个函数都加了 `lang` 参数（默认 `zh`，中文调用点行为不变）
- **总体设计见 `docs/multilang.md`** —— 为什么不分库、语言族模型、各阶段接口约定都在那里
- **中文专属：** 汉字分解、量词、拼音、kahneman/paste/briefing 故事模式
- **主页语言标签页**（#436）：`GET /api/langs` 返回使用中的语言；前端多于一种语言才显示标签栏，选择存 `localStorage`。所有主页/复习/故事/统计接口支持可选 `?lang=`（默认不过滤，向后兼容）；解析规则统一为 `lang 参数 或 get_deck_lang(deck_id)`
- **故事按语言隔离：** `stories.lang`（NULL = 中文旧数据）；聚合牌组（如 All）在各语言标签下维护独立的活跃故事；后台生成的 progress_key 含 lang

---

## 数据与导入

**数据库是唯一事实来源。** 原来的 `imports/` YAML 目录已于 2026-07-07 删除——所有历史词条都已在数据库里，生产数据库在服务器上。

导入机制本身仍然存在（`importer.py`、`POST /api/import`、`python main.py import`，读取 `imports/<Source>/*.yaml`）：需要批量导入新词汇时，重新创建该目录放入 YAML 即可。日常添加单个词条：界面顶栏 `＋` 按钮（见下），或 `de-zh-bot` 技能生成 YAML 后导入。

- **YAML 格式完整文档：** `docs/yaml-format.md`（中文格式：词性/例句/词源/汉字分解；法语格式：`lang: fr` + `type: word|sentence`，经 `importer._normalize_fr_entry` 适配后复用全部下游逻辑）
- 文件顶部可选 `lang:` 字段（默认 `zh`）决定导入到哪个语言的牌组
- AI 在故事提示词（tíshící - prompt）中被告知"非目标词汇只使用 HSK 1–2 的词汇"
- 汉字分解、量词、同义反义词、语法结构、`word_analyses` 组件处理**仅中文**执行

### 界面内添加生词（#627、#636、#643、#715、#726）

顶栏 `＋` → 输入词 → 回车或点"加" → 后台 DeepSeek 生成完整词条 → 进 **★ List**（`Saved` 牌组，挂起，不进任何复习队列）。

> **界面上只有 ★ List 这一个去处（#715，Daniel 2026-08-12 决定）**：顶栏 ＋、知识库详情页的生词表格、`/add` 页面三处的 Today/Tomorrow 按钮全部撤掉，一律 `day='list'`。新词先攒着，之后在 Browse 的 saved 视图主动提升（那个「→ Add to Daily」按钮**保留**——★ List 是暂存区，总得有出口）。**后端 `day` 参数照旧支持 `today|tomorrow|list`**：Daniel 已有的 iOS 快捷指令 `/add?word=X&day=today` 不该突然改行为，而且以后想恢复某个按钮只是加回一个按钮。`/add` 因此仍尊重 URL 里的 `day`，只是无参数时默认 `list`。

**复用导入器，不写特例建卡代码：**

- `ai.generate_word_entry_yaml()` 把 `de-zh-bot` 技能的中文词条规则移植成服务端提示词，输出 **YAML** 而不是 JSON —— 这样能直接交给 `importer.import_yaml_content()`，例句/量词/同义反义词/汉字分解/词源全部复用既有下游逻辑，**没有一行特例建卡代码**。这也意味着卡片与手工导入的词条完全一致（`importer._create_cards` 的 due 就是 `anki_today()`，`_make_leaf_decks` 建的子牌组名与 `get_or_create_category_decks` 相同）
- 模型没返回 YAML 列表项时抛 `ValueError`；导入数为 0 的"完成"任务前端也报错 —— **绝不把垃圾内容悄悄写进库，也不假装成功**
- 生成约 30 秒，所以走后台线程 + `job_id` 轮询（复用 `_import_jobs` 机制）
- 只接受中文输入：德/英输入需要 AI 反问是哪个意思（`de-zh-bot` 就是这么做的），一个无交互的输入框做不到，猜错会静默存进一个错词
- **不阻塞连续输入**（#636）：提交后立刻清空输入框，每个词进弹窗里的队列各自轮询自己的 job

**已有的词：移动，不是新增**

- `cards` 有 `UNIQUE(word_id, category)`，一个词终身只有三张卡，`insert_card` 对它是静默无效果的 —— 不存在"再加一张到今天"。已有词**不调 AI**（重新生成的内容会被 importer 当重复丢掉，纯烧钱）
- **再次添加已学过的词 = 重置为新卡**（#675，Daniel 2026-08-10 明确要求，此前是拒绝并返回 `already_exists`）：`database.reset_word_to_new(word_id, leaf_decks, due, from_deck_id=None)` 把该词的三张卡搬进今天/明天的 Daily 叶子牌组并清空全部调度状态。`promote_saved_word` 现在只是它 `from_deck_id=Saved` 的一层薄封装
  - **这是不可撤销的破坏性操作**：stability/difficulty/interval/lapses 是这个词的全部 FSRS 记忆模型。所以接口返回 `status="reset"` + `previous_decks` + `reviews_discarded`，前端如实显示"↺ reset from X → Y, N reviews discarded"，**不报一句平淡的成功**。卡片原本只在 `Saved`（没有进度可丢）时返回 `status="promoted"`，措辞不同
  - `review_log` 的历史记录不删，所以统计不受影响

**三个去处：★ List / 今天 / 明天**

- **★ List**（#677）：`day="list"` —— 照常花钱生成完整词条，但卡片直接进 `Saved` 牌组并挂起，**不进任何复习队列**，直到在 Browse 的 saved 视图点 "→ Add to Daily" 提升。`database.stage_word_in_saved()` 是 `promote_saved_word` 的逆操作
  - **导入仍走今天的 Daily 牌组，之后再搬**：直接以 `Saved` 为父牌组导入会建出 `Saved::listening` 等叶子牌组，而 Browse 的 saved 过滤器认的是 `deck_name === 'Saved'`
  - **挂起靠 `state`，不靠 `due`**：`cards.due` 是 `NOT NULL DEFAULT date('now')`，写 NULL 直接违反约束。停留在 `Saved` 的卡照样有 due 值，是 `state='suspended'` 把它挡在队列外
  - 停放**不清空** FSRS 字段（挂起已经够了；真要激活时 `promote_saved_word` 会重置），所以 `reviews_discarded` 返回 0 —— 与上面的 `reset` 不同，这不是破坏性操作
  - Browse 的 saved 行只在词条**没有释义**时才显示 "✨ Generate"，★ List 进来的词内容已齐全，再生成纯烧钱
- **今天/明天（#636，#715 起界面不再提供，接口保留）**：`day` 参数同时决定 Daily 牌组、`promote_saved_word` 的 due 和 `importer.import_yaml_content(due_offset_days=)`。**牌组和 due 必须一起后移** —— 未来日期的 Daily 牌组被 `parse_daily_deck_date` 锁住不可复习，卡片若还 due 今天就永远够不到

**单一入口与独立页面**

- **`/api/add-word-ai` 是全应用唯一的加词入口**（#643）：顶栏 ＋、播客单集的 HSK 生词表格、复习界面长按词的菜单、以及 `/add` 页面，全部共用 `shared.js` 的 `addWordViaAi()`。原来播客/长按走的 `/api/quick-add-word` 已删除 —— 它只让 AI 填四个字段（无例句/汉字分解/量词/同义反义词），而且 `added_to_deck` 分支在词已学过时被 `INSERT OR IGNORE` + `UNIQUE(word_id, category)` 全部静默丢弃，却照样返回成功。**加词只能有一条管线**，否则修好的坑会在第二条路上重新出现
- **独立加词页 `/add`（#668）+ URL 参数（#686）**：可收藏的网址，打开就是输入框（存 iPhone 主屏当图标用）。`?word=生态`（或 `?w=`）+ 可选 `&day=today|tomorrow|list` → 打开即自动提交，供 iOS 快捷指令在任意 App 里一键加词；`day` 非法值回落 today（词才是重点，不该为此失败）。**提交后必须 `history.replaceState` 抹掉参数** —— 否则刷新或 iOS 恢复标签页会静默再花一次 AI 钱。`static/add.html` 是自包含页面，**故意不加载 `app.js`** —— 5.5 KB vs 完整应用的 77 KB + 491 KB JS，秒开就是这个功能的全部意义。为此把 `addWordViaAi()` 和 `api()` 从 `app.js` 抽到 `static/shared.js` 两页共用，**不复制第二份**（理由同上条；`tests/test_add_word.py` 有一条测试专门守着 `app.js` 里不能再出现这两个定义）

**加西班牙语词与罗曼语形态（#805）**：`lang` 支持 `zh|fr|es`

- **加词必须把全部变位/词形生成出来**，这不是锦上添花：知识库判定"这个词形我学过没有"靠的是 `entry_forms` 的精确匹配（`database.forms_lookup()`，零词干还原），漏掉的变位就等于这个词在阅读里永远显示为生词。提示词对法语/西语强制要求：动词给完整变位表、名词给 `gender:` + 复数、形容词给阴性/复数
- **词头一律是词典形，输入的变位形自动还原（#924）**：Daniel 是从阅读里挑词的，敲进来的多半是 `mangeons`/`réduites`/`chats`。fr/es 的加词提示词和 `/dict` 提示词都有 `DICTIONARY FORM` 段：动词还原成不定式、名词单数、形容词阳性单数（`expression`/`sentence` 和真正词汇化的分词是例外），输入的那个形式写进 `note` 说明，不当词头。否则整棵 `entry_forms` 会围着一个变位形展开，上一条的精确匹配就全落空
  - **输入变位形先查库再决定花不花钱**：`database.get_word_by_form(词形, lang)`（`entries.word_zh` ∪ `entry_forms.form`，同样零词干还原）命中就走既有的"已存在的词"分支，`/api/add-word-ai` 和 `/api/save-word` 都查。原来 `get_word_by_zh("mangeons")` 查不到 → 二次付费生成 + 第二个词头，`UNIQUE(word_zh, lang)` 因为拼写不同拦不住
  - **导入回来的词头可能和输入的不是同一个字符串**，所以 `importer` 的返回值带 `imported_words`，`day='list'` 的入 Saved 和任务汇报都用它——用 Daniel 敲的字符串去 `get_word_by_zh()` 会找不到，卡片静默留在 Daily 牌组里
- YAML 里 `conjugations:` 进 `entry_forms(kind='conjugation')`，`forms:`（`{维度: {槽位: 形式}}`）进 `kind='inflection'`，`gender:` 进 `entries.gender`。格式见 `docs/yaml-format.md`
- **词典 `/dict` 支持 fr/es**（`ai.DICTIONARY_PROMPT_ROMANCE`，移植自 `de-fr-bot`）：**返回的 JSON 契约与中文版完全一致**，所以前端渲染代码不分语言。★ 加词仍然走 `/api/add-word-ai`——词典不得成为第二条加词管线（#643）
- **`GET /api/langs?available=1` 返回全部已注册语言**，`/add` 和 `/dict` 用它；主页标签栏仍用"在用语言"。区别是有意的：**一门新语言还没有牌组，按"在用"过滤就永远加不了它的第一个词**

**加法语词（#726）**：`lang` 参数（`zh` 默认 / `fr`）同时决定提示词和牌组树

- **每种语言一棵平行的牌组树**，根名在 `languages.py` 的 `deck_root`（zh → `Daily`，fr → `Français`）：`Français::<日期> · Listening/…` + `Français::Saved`，牌组本身 `lang='fr'`。**必须如此**——全应用的语言过滤（`_lang_subquery_clause`、`get_descendant_leaf_deck_ids`）筛的是 `decks.lang` 而不是 `entries.lang`，法语卡放进中文牌组会在 fr 标签下消失、反而混进中文复习队列（生产库里 #726 之前导入的那 7 个法语词正是这个状态）
- `Français::Saved` 的**牌组名仍是 `Saved`**（路径末段），所以 Browse 的 saved 视图（认 `deck_name === 'Saved'`）一行都不用改
- **已存在的词按 `entries.lang` 落点，不听请求参数**：`word_zh` 是全局唯一的，lang 传错会把同一个词的三张卡撒进两棵语言树，两个标签下都看不见它。`promote_saved`（Browse 的 → Add to Daily）同理
- **按语言校验输入文字体系**（zh 必须含汉字；fr 不能含汉字且要有拉丁字母）：加词框没有反问的机会（`de-zh-bot`/`de-fr-bot` 技能是靠追问"你指哪个意思"的），把德语词喂给法语提示词会静默生成一个错词条。查文字体系分不出法语和德语，但至少挡住 `生态` 走法语提示词
- 法语输出**自带 `lang: fr` 头**，不靠目标牌组的语言推断——条目格式（`word:`/`level:`/`examples[].fr`）和 lang 必须一致，写在文档里能整类消灭"法语词条被当中文导入"
- 复习界面长按菜单的两个按钮（`+ Add to Daily`、`★ Save for later`）按**当前卡片的语言**走（`currentCardLang()`），跟主页在哪个标签无关——词是从这张卡里挑出来的
- `/add` 支持 `?lang=fr`（每种语言一个 iOS 快捷指令）；页面自己拉一次 `/api/langs` 决定是否显示语言切换钮，**不加载 `app.js`** 的原则不变

---

## 故事生成

- 每个类别（阅读/听力/写作——界面顺序也是这个）独立生成自己的故事
- 每个目标词汇恰好对应一个句子（1:1 按位置映射）；`create_story()` 每次插入新行——重新生成 = 新增一行，旧故事永久保留
- 提示词要求：连贯叙事、相同人物、每句 ≤15 字、背景词汇 HSK 1–2

**模式（mode）：**
- `story`（叙事）| `qa`（问答）| `expository`（说明文）
- `kahneman` ——《思考，快与慢》认知偏误风格（`data/kahneman_chapters.json`）
- `paste` ——用户在设置弹窗粘贴任意内容（#396）；自 #481 起复用 briefing 管线（`generate_briefing_sentences(generic=True)`，内容摘要框架措辞），因此同样有上下文句、Python 校验与事实核查；**模型下拉框可选（#910）**，默认仍是占位项「Server: BRIEFING_MODEL」——那是这条管线唯一验证过的配置，但锁死的理由（DeepSeek 审查**新闻**）对粘贴的任意素材不成立，同 `knowledge` 在 #561/#640 的解锁。占位值 `routes/story.SERVER_MODEL_SENTINEL`（前端 `SERVER_MODEL_VALUE`，两处拼写必须一致）不在 `ALLOWED_MODELS` 里，只有 paste/briefing 两个分支认得它，所以路由层的 `_requested_model()` 只为这两个模式放行它——别的模式收到它会当无效模型回落，绝不会被当成模型名发出去。`briefing` **仍然锁死**（那确实是新闻）；不做自动抓取回退
- `briefing`（News flow，#399）——AI 写一篇**连贯的新闻总结**，目标词各恰好出现一次，但**不是每句都含目标词**——目标词句之间允许纯上下文句（承载数字/事实，不受 15 字限制）。含目标词的句子成为卡片；前面的上下文句用 Google Translate（非 AI）译成德语存 `story_sentences.context_de`（显示在卡片正面），中文原文存 `reasoning_zh`（背景弹窗）。briefing 卡片没有标题（concept_zh 为空）。自动抓取当日新闻（`news_fetcher.fetch_all()`：Tagesschau API + RSS，按天缓存）：两步 AI，`summarize_news_items` 挑最重要的 8 条（平衡德国/国际/中国相关）→ `generate_briefing_sentences` 生成连贯中文简报（模型固定服务器端 BRIEFING_MODEL，因 DeepSeek 会审查新闻内容）。抓取全部失败时报明确错误，不静默降级为普通故事
- **取消生成（#828）**：加载页的 Cancel 按钮 → `POST .../cancel` 把 progress_key 记进 `ai._cancelled_keys`。**检查点挂在 `ai._set_progress()` 里，不是在 `routes/story.py` 里散布几处显式检查**：每种生成模式本来就在每个阶段（重试、翻译开始、逐句翻译进度）调它，挂在那里等于所有模式的每一步都免费获得中断能力，以后新增的模式也自动继承。另外 `_generate_and_store` 在 `database.create_story()` **之前**再查一次——上面的活儿白干无所谓，写进库的故事不是。标志必须在生成线程的 `finally:` 里 `ai.clear_cancel()`，否则下一次同一牌组的生成一启动就被旧标志掐死。`_fill_translations` 的兜底 `except Exception` 要放行 `StoryCancelled`（取消不是翻译失败）。取消后 `_story_progress` 条目被删掉，所以 #821 任务指示器和「Story ready」横幅都不会再提这次生成
- **旧 `news` 模式已移除**（#512，界面曾叫 "News briefing"）：新故事生成拒绝 `mode='news'`（`_generate_and_store` 直接抛 `ValueError`）；但历史 `news` 故事仍能正常展示，且 Again 单句重生成仍复用 `ai.generate_news_sentences`（`generate_sentence_for_word` 保留该分支）——不影响旧数据
- briefing/paste 共同点：每句带 `source_url`（背景弹窗"打开原文"链接），复用 kahneman 的概念框/背景弹窗 UI；文章内容存 `stories.gen_params.articles` 供 Again 重生成复现同一批内容（paste 的文章通过 regenerate 的 POST body 传输）
- **知识模式对所有语言可用（#806）**：素材是什么语言无所谓，决定输出语言的只有提示词。所以它有独立开关 `features.knowledge_story_mode`（所有语言 True），**不和 `extended_story_modes`（kahneman/paste/briefing，仍只有中文）捆在一起**。中文走可自定义的 `DEFAULT_PROMPT_TEMPLATES["knowledge"]`，其它语言走 `ai._KNOWLEDGE_PROMPT_NON_ZH`（同样的规则，目标语言输出）。`briefing`/`paste` 仍只有中文，所以 `generate_briefing_sentences()` 没加 `lang`——加了是死代码
  - **匹配必须接受变位形**：提示词允许模型调整词形（`réduire` → `a réduit`），不允许法语句子根本写不通顺。`ai._card_surface_forms()` = 词典形 + `entry_forms` 里的全部变位/词形。**漏存变位的代价在这里第二次出现**：匹配不上 → 句子被丢弃 → 该词拿到兜底句。非中文的兜底句是词本身加句号，不是「我学了X这个词。」
- **每种语言一套调度预设（#806）**：`_ensure_lang_preset()` 在某语言首次建牌组时复制默认预设，命名为牌组树根（`Français`/`Español`）。**中文仍然走 `_ensure_default_preset`，绑定一个字节都不许动**（preset id=2，见 #629 的教训）
- `knowledge`（#482，原 `podcast` 模式，#654 改名以配合「知识库」泛化）——从已摘要的知识库素材（播客/视频/文章均可，`database.get_episode(episode_id)`）生成句子：素材文本优先取该条目的 `transcript_zh`（截断到 15000 字），无转录时才降级用 `summary_de`（#661；#561 曾改用摘要纯为省成本/延迟，实测一次约 $0.003 后确认没必要，副作用是输入变回中文全文更容易触发 DeepSeek 内容过滤，敏感话题走设置弹窗下拉框换 GPT 重新生成）。同样走 briefing 管线（`generate_briefing_sentences(generic=True, include_context=False)`），但**不允许上下文句**——每句都必须含一个目标词。`episode_id` 沿用 kahneman 的 `chapter_ids` 传参模式（GET 查询参数/regenerate POST body/gen_params），设置弹窗提供单选条目选择器（仅列 `status=summarized` 的条目）；不在早晨预生成 `_PREGEN_MODES` 里，因为选素材是一次性的。**历史 `mode='podcast'` 故事仍能展示和做 Again 单句重生成**——只在生成*新*故事时拒绝旧标识符（`ValueError`）

---

## 加星句子：改进提示词的正例样本（#692）

复习时读到写得好的句子，就地按 **Shift+F** 或点卡背工具条的 ☆ 加星；之后在 Browse 的 **⭐ Sentences** 视图集中回看。判断一句话好不好只有读到它的那一秒才可能，事后翻故事历史回忆不起来 —— 这是这个功能存在的全部理由。

- `story_sentences.starred` / `starred_at` 两列，**不新建表**：加星是句子的属性，多一张表只是多一次 JOIN
- **Again 重生成的句子自动通吃**：它们本来就是 `story_sentences` 行（存在 sentinel category `again` 下，见 `store_again_sentence`），无需特例 —— 而新生成的句子恰恰最需要被评判
- **列表必须带出生成上下文**：`get_starred_sentences()` 从 `stories.gen_params` 解出 `mode`/`model`/`episode_id`，再带上牌组名与 `source_title`/`source_url`。一句脱离了"是哪个提示词生成的"的好句子，对改提示词毫无用处
- **提示词只链接不复制**（#697）：提示词已经在 `stories.prompt_text` 里，列表只带 `story_id` + `has_prompt`，全文由 `GET /api/story-prompt/{id}` 按需取。**绝不内联全文** —— 一份 knowledge 提示词含上万字转录，500 行列表会变成几十 MB
  - `knowledge` 模式原来存的是占位符 `"knowledge mode — item N (kind=video)"`，正在调的那个模式恰恰读不回提示词。现在 `ai.generate_podcast_sentences()` 返回 `(sentences, prompt)`（与本模块其它生成函数一致），存的是**实际发出去的**提示词并跨轮拼接 —— 补漏轮带 `extra_hint`，事后重建的版本并不是写出这些句子的那一份
  - **提示词为空是正常状态**，不是错误：旧故事早于该列，且离线快照会主动清空它（`scripts/offline_sync_server.py`）。界面必须说明原因，不能显示空白框
- **前端是独立渲染分支**：Browse 其余所有过滤（`_filteredBrowseWords()`）都是**按词**过滤的，加星是**句子**级实体，塞不进那条链。切到任何按词的过滤/搜索/排序时用 `_leaveStarredView()` 自动退出，免得标签高亮和列表内容说两套话
- 复习时的加星是**乐观更新**：复习途中按钮卡住比星标没存上更打断节奏；失败回滚并报错
- 没有句子的卡（还没生成故事，正面只显示单词）不显示该按钮

---

## 知识库（Knowledge Base，#650–#655）

播客爬虫（#479）泛化成一个统一的知识库：播客单集、YouTube 视频、报刊文章三类素材走**同一条流水线**（获取 → 转录/正文 → 中文+德语摘要 → 生词标注 → 通知 → 造卡）。总体设计见 `docs/knowledge-base.md`（各阶段 Issue 都引用它）。

- **不新建表，泛化 `podcast_episodes`**：加两列 `kind`（`podcast`|`video`|`article`|`newsletter`，最后一个 #925 加的）、`title_en`。**表名和历史列名故意不改**——改名要重建表+迁移生产库，风险远大于收益，本仓库已有同类先例（`youtube_url` 现在也存文章/播客链接，`word_zh` 对法语存法语词形）。`video_id` 对文章存 `normalize_url()` 去掉跟踪参数后的规范化 URL（`podcast_episodes` 的去重键），`transcript_source` 存 `youtube_captions`/`article`/`tingwu`/`whisper`/`notebooklm`
- **`knowledge/` 包，`ingest.py` 是唯一入库管线**：`ingest_url()` 判断 YouTube 链接走 `youtube.py`（oEmbed 拿标题 + `youtube-transcript-api` 拿字幕，语言优先级 zh-Hans→zh-CN→zh→zh-TW→de→en，找不到任何字幕轨直接 `no_transcript`，**不跑 Whisper**；**YouTube 封锁云服务商 IP，所以服务器上字幕 API 恒返回 `RequestBlocked`，实际走的是 NotebookLM 兜底**，见下条），否则当文章走 `article.py`（`trafilatura` 抽正文，不足 200 字视为失败并抛 `ArticleExtractionError`——付费墙/登录墙/JS 页面绝不能存进库冒充正文，见其 docstring）。界面「粘贴 URL」框（`POST /api/knowledge/add`）和邮件收件（`mailbox.py`）**共用这一个函数**——理由同 #643 加词单一入口：两条平行管线迟早会让修好的坑在另一条上复活
- **邮件收件（`knowledge/mailbox.py`，#655）**：IMAP 轮询 UNSEEN 邮件，标题+正文都扫 URL（手机分享到邮件，链接位置因 App 而异）。`KNOWLEDGE_MAIL_ALLOWED_SENDERS` 未配置时**整个邮箱检查被跳过，不读取也不标已读**——这是防止任何知道邮箱地址的人远程触发付费 AI 调用的唯一防线；处理失败的邮件同样不标已读，留给下一轮重试（`ingest_url()` 对已入库 URL 幂等返回 `already_exists`，重试安全）
- **邮件通讯（`knowledge/newsletter.py`，#925）**：Daniel 每天早上收到 F.A.Z. Frühdenker，Gmail 规则转发到上面那个收件邮箱（`newsletter@nl.faz.net` 必须在 `KNOWLEDGE_MAIL_ALLOWED_SENDERS` 里，否则整封被白名单挡掉）。新 `kind='newsletter'`（该列**没有 CHECK 约束**，加值不需要迁移生产库），知识页有独立的 📰 标签且**没有粘贴框**——通讯只从邮箱进来
  - **必须排在 `mailbox.py` 的 URL 分支之前**：通讯正文里有几十个 faz.net 付费墙链接，走 URL 分支等于每轮对每个链接做一次注定失败的网络往返，而真正的内容就在邮件正文里。入库仍走**同一个** `ingest_text()`，只是多传一个 `kind`
  - **入库后立即同步处理**（转录+摘要+通知），同 `signal_inbox.py`——"早上就要读"的语义
  - **`_HTMLTextExtractor` 必须在块级标签处插入换行**（#925 改的）：原来 `"".join(chunks)` 一个换行都不产生，压缩成一行的营销邮件因此整封是**一行**，而 `clean_body()` 是按行删样板的——那一行里只要有 "Abbestellen"，整封正文就被删光。#668 的粘贴正文路径同样受益（原来段落会被粘成一坨喂给 AI）
  - **`clean_body()` 删掉超过 60%（`_MIN_KEEP_RATIO`）时放弃清洗、原样返回**：留几行页脚只让摘要略脏，删光正文是静默失效。宁脏勿空
  - **`IngestError`（正文太短）是永久失败 → 标已读放弃**，其它异常才留着不读重试。cron 每 5 分钟一轮，留着一封永远不可能成功的邮件等于每轮白跑一次（同 `signal_inbox.py` 对粘贴正文失败不进重试队列的判断）
  - **通知里额外带法语**（`podcast._rendition_fr_html`）：中文是 AI 原生的 `summary_zh`，法语复用 `knowledge/rendition.py`。**只对 `kind='newsletter'` 生效**，播客/视频/文章的邮件与 Signal 消息一个字节不变；法语失败只记日志，照常发德/中两份
- **Signal 分享入口（`knowledge/signal_inbox.py`，#749）**：手机把链接分享到 Signal 自己的「Note to Self」，服务器用 #521 早就关联好的**同一个** signal-cli 设备（`SIGNAL_ACCOUNT`）把消息收下来，正文里的 URL 同样走 `ingest_url()`。与邮件收件方向相反、账号相同——`send_signal()` 是服务器→Daniel，这个是 Daniel→服务器
  - **安全防线**：只收下**源账号和目的账号都等于 `SIGNAL_ACCOUNT` 自己**的消息（真正的 Note to Self）——关联设备会同步收到 Daniel 手机发出的所有消息，包括发给别人的，那些一律忽略。作用等同于 `KNOWLEDGE_MAIL_ALLOWED_SENDERS` 之于邮件入口
  - **粘贴正文入口（#834）**：消息第一行只写 `text`（小写，大小写不敏感；也接受 `text:` / `文本`）→ 剩下的整条消息当文章正文，走 `ingest_text()`。**关键字必须独占第一行**，否则 "Text von gestern, siehe Link" 这种普通句子会被误认；正文里的**第一个链接自动存为 `source_url`**；标题/作者交给 #833 的服务端 AI 抽取。**粘贴正文的失败不进重试队列**——那个队列在 `app_settings` 里存 JSON（是给 URL 用的），而且正文失败的方式是"太短"，重试一百次结果一样；回执说明原因，重发一次即可。🔴 正文绝不进日志/错误信息/回执（下面 Privacy 那条同样适用）
  - **失败重试靠自己存队列，不是靠"留着不读"**：`signal-cli receive` 一次调用就把消息从 Signal 服务器上取走，不像 IMAP 能把邮件留成 UNSEEN 等下一轮。入库失败的 URL 存进 `app_settings['signal_retry_queue']`（JSON 列表），下一轮优先处理，满 3 次放弃并在回执里说明
  - **新链接入库后立即同步处理**（转录+摘要），不像邮件/网页粘贴那样只入库、等前端另外调 `.../process`——Signal 分享的语义就是"现在就要"。处理复用 `podcast.retry_episode()`（`routes/podcast.py` 的 process 端点背后那个同步函数，脚本进程里直接调用，不起后台线程——脚本跑完就退出，线程会被杀掉）
  - **`podcast.send_signal_text(text, context=...)`（#749 从 `send_signal()` 抽出）是发 Signal 消息的唯一函数**：`send_signal()`（摘要通知）和 `signal_inbox.send_receipt()`（收件回执）都调它，不重复写 subprocess 调用。处理成功时**不重复发一遍摘要**——`send_signal()` 已经在摘要成功后自动发了完整版，回执只发一行简短结果
  - **`receive` 必须带 `-t`，且超时要给足（#755）**：不带 `-t` 的 `signal-cli receive` **不会**"取空就退"，它会一直监听等新消息直到被杀——cron 一次性调用因此永远等到 subprocess 超时，一条都收不到。另外**首轮特别慢**：这个账号自 #521 起只发不收，Signal 服务端攒了大量待投递消息，实测消化超过 2 分钟（原来 120 秒的上限就是这么被撑爆的），所以 `_RECEIVE_TIMEOUT=300`。之后每 5 分钟一轮只有零星消息，秒级返回
  - **`receive` 会把 Daniel 与所有人的对话都同步下来**（#755 实测）：别人的消息正文、附件元数据、已读回执、正在输入指示，全都在返回的 envelope 流里。上面那道安全门挡住了它们入库，但它们**经过了本进程的内存**。所以：**永远不要把 envelope 原文写进日志**，也不要放进错误信息或 Signal 回执——只记 URL 和处理结果
  - `scripts/signal_check.py`：cron 入口，结构照抄 `knowledge_mail_check.py`（同款 PID 锁 + `database.init_db()` 直连）
- **前端：统一素材列表（#936，取代 #653 的四个 kind 子标签）**：一个列表 + 排序栏 + 筛选栏 + **一个 Add 按钮**。kind 从"你放在哪个桶里"降级成众多筛选之一——因为找东西的方式是"上周处理的、那个作者的那篇"，不是"它属于四类中的哪一类"；而且两套并行的列表实现意味着以后每个排序和筛选都要写两遍
  - 三个屏幕（`_knowledgeScreen`）：`list` 统一列表 / `feed` 某个 RSS 源的单集（**保留独立屏幕**，因为"Load more"是按源翻页的动作，在混合列表里没有意义）/ `feeds` RSS 源管理（从旧播客标签页搬到顶栏 📡 按钮后面）
  - **Reels 不再是"虚拟标签"**（#764 那套按 URL 判 Instagram 的前端拆分）：`platform='instagram'` 现在是库里的真列，`#knowledge-reel` 链接翻译成 `platform=instagram` 筛选。`_isInstagramEpisode()` 优先读 `platform`，URL 判断只作为老行的兜底
  - **筛选状态存 `localStorage.knowledgeFilters`**，读出来时**合并到默认值上**而不是直接信任存的对象——以后新增的筛选轴对老用户不能是 `undefined`
  - 从素材详情返回时 `openKnowledge()` **不传参数**：传了就会重置筛选栏，而"看完一篇回到列表发现筛选没了"是最糟的时机
  - **旧的 `#podcast-<id>` hash 链接永久保留**——已发出去的邮件/Signal 消息里全是这种链接；`#knowledge-<kind>` / `/knowledge/<kind>`（#704）同样保留，落到"该 kind 已预选"的统一列表
- **列表接口的排序与筛选（#936）**：`GET /api/podcast/episodes` 的 `sort`/`order` 走 `database.EPISODE_SORTS` **白名单**（它要拼进 ORDER BY），**未知值回落默认顺序而不是 400**——过期的书签也该看得到列表。筛选轴 `kind`/`platform`/`author`/`tag`/`status` 都**可重复**（轴内 OR、轴间 AND），`kind` 仍接受单值字符串（#936 之前的调用方和书签都是这么写的）
  - **未处理的素材在默认排序下永远排最前**（`processed_at IS NOT NULL ASC` 这一项**写死 ASC、不跟随 `order`**）："还没处理"不是一个日期，翻转方向时不该把它埋到另一头
  - `GET /api/knowledge/facets` 一次返回筛选栏所有下拉的选项（实际出现过的 kind/platform/author/status + 源/标签/列表目录）：筛选栏是一个整体，五个并行请求就是五倍的"画到一半"概率。选项**从数据里推**，不写死——只存在于 Daniel 库里的平台也能出现，不存在的永远不会给出必然为空的选项
  - 归档素材（#940）在 **HTTP 层**默认隐藏（`include_archived=false`）；`database.list_episodes()` 本身保持包含，免得动到既有调用方
- **独立收藏页 `/save`（#681，#835 起三个标签）**：`/add` 的素材版 —— 可收藏的网址，🔗 Link / 📋 Text / 📎 File，同样**不加载 `app.js`**（手机上从别的 App 分享文章时秒开）。入库逻辑抽到 `shared.js` 的 `ingestKnowledge()` / `ingestKnowledgeFile()`，应用的知识页和本页共用一份。**两处都不许直接调 `/api/knowledge/add*`**，有测试守着
- **粘贴正文的元数据由 AI 补全（#833）**：Text 表单是「正文（必填）+ 链接 / 标题 / 作者（都可选）」，留空的由 `ai.extract_article_metadata()` 一次便宜的 DeepSeek 调用从正文**前 3000 字**读出来（元数据都在开头，喂全文是白花钱）。author 落 `channel_id`（文章行本来就用这一列存来源）
  - **三个都填好了就完全不调 AI**；**AI 绝不覆盖用户手填的值**（他看着原文，模型是猜的）
  - **去重在 AI 调用之前**（同 `_ingest_article` 先去重再下载），重复正文不二次付费；去重键仍只是正文哈希，换个标题重贴仍命中已有行
  - 失败一律返回 `{}` 不抛异常（同 `ai.translate_title` 的契约）—— 为一个锦上添花的标题丢掉已经粘好的正文是荒唐的；`published_at` 只收 `YYYY-MM-DD`，模型答「上周三」一律丢弃
  - 标题兜底链：手填 → AI → `fallback_title`（上传时是文件名）→ 正文首行（**截断 120 字**：不换行的粘贴整篇就是「一行」）→ `(untitled)`。原来客户端的 `knowledgeTitleFor()` 已删除，规则只留服务端一份
- **上传文件（`knowledge/files.py`，#835）**：`.txt`/`.md`/`.pdf`/`.docx` → 抽纯文本 → 走**同一条** `ingest_text()`。`POST /api/knowledge/add-file` 只负责「文件 → 文本」，返回契约与 `add-text` 完全一致
  - **未知扩展名报错，绝不「当纯文本读读看」**：一个 .zip 解码出的替换字符看着像内容，会被摘要、入库、做成卡片
  - **抽不出文字也报错**：扫描版 PDF（无文字层）错误信息明说需要 OCR —— 同 `knowledge/article.py` 拒绝付费墙残页
  - Markdown 原样当正文，不渲染成 HTML（摘要提示词吃得下，剥掉之后还得加回来）；10 MB 上限
  - 新依赖 `pypdf`、`python-docx`
- **素材元数据层（#935，大方向 #934）**：知识库正在从"按 kind 分四个标签页"改造成"一个可排序/筛选/搜索/分类的素材库"。这一阶段只加数据层：
  - `podcast_episodes` 新增 `processed_at`（摘要**成功完成**的时刻，统一列表的默认排序键）、`author`、`platform`、`manual_fields`、`archived_at`
  - **`author` 不复用 `channel_id`**：后者已经同时表示 RSS 源 URL / YouTube 频道 id / 网站域名 / 粘贴时填的作者，四种含义并存，按它做作者筛选是不可能的。所以只在**真的知道作者**时才写（频道名、uploader、Daniel 手填、播客节目名），网站域名不是作者——宁可留空，等 #937/#938 填
  - `platform` = 素材**从哪来**（`youtube`/`instagram`/`podcast`/`web`/`upload`/`paste`/`email`/`signal`，白名单在 `database.KNOWLEDGE_PLATFORMS`），和 `kind`（它**是什么**）是两个正交的轴，两个都要能筛。**不能由 kind 推出来**：上传的文件、newsletter、Signal 分享全都是 `kind='article'/'newsletter'` 的粘贴正文，所以 `ingest_text(platform=...)` 由每个调用方自己传
  - 四张新表 `knowledge_tags` / `knowledge_item_tags` / `knowledge_lists` / `knowledge_list_items`，访问函数在**新模块 `database/knowledge.py`**（`database/podcast.py` 已经 440 行，而且标签/列表是"怎么组织素材"，不是"怎么抓素材"）
  - **标签名大小写不敏感唯一**（`idx_knowledge_tags_name ... COLLATE NOCASE`）：筛选栏里 `Politik` 和 `politik` 各管一半，比没有标签更糟。`rename_tag()` 改成已存在的名字 = **合并**，这是收拾 AI 造出的近义标签的唯一出口
  - **`knowledge_item_tags.source`（`user`/`ai`）是硬边界**：`set_item_tags(source=...)` 只替换**同 source** 的行，所以 AI 重新打标签永远删不掉 Daniel 手打的，反之亦然。手动打一个 AI 已经猜到的标签会把它**升级成 `user`**（他认领了它）。别处不许直接写 `knowledge_item_tags`
  - **回填是幂等的，不是靠一次性标记**：每条 UPDATE 都限定在目标列仍为 NULL 的行上——生产每 2 分钟重启一次，`init_db()` 就跑一次（#688 的教训）。`processed_at` 回填 `COALESCE(email_sent_at, created_at)` 且只填 `summarized` 行；`author` **不回填**（错的作者比空的更糟）
  - 内置的 `Read Later` 列表按 **`is_builtin` 而不是名字**判断存在：否则 Daniel 一改名，下次重启就又冒出一个 `Read Later`
  - `idx_episodes_processed_at` 建在 `core.py` 的迁移里而不是 `schema.sql`：schema 在**第 2 阶段**执行，那时旧库上 `ALTER TABLE`（第 3 阶段）还没跑，列还不存在，`CREATE INDEX` 会直接报错
- **china-kritisch 复选框（#731）**：摘要默认走便宜的 DeepSeek —— Daniel 的博客素材绝大多数不批评中国，没必要为它们付 OpenAI 的钱。少数确实批评中国的素材勾选后存 `podcast_episodes.china_critical=1`，摘要时把 DeepSeek 从候选模型里**彻底删掉**（不是排后面）：它对这类内容会悄悄弱化或拒答，而弱化后的摘要照样能解析出 `summary_de`，任何"解析失败就回退"的机制都永远不会触发
  - **免费的 NotebookLM 路径不受影响，照旧第一优先**：它是 Google 的，没理由审查这个话题，而且不花钱。勾选只改变它失败之后 API 兜底那一层选谁
  - **标记必须在粘贴那一刻打上**：摘要发生在之后独立的 `POST .../process` 调用里（甚至是 cron 里），那时已经没人在旁边说明这是什么素材
  - 三个入口（应用知识页的链接框/正文框、独立收藏页 `/save`）都有该复选框，**每次提交后自动复位**——粘性复选框会在之后所有素材上静默烧 GPT 的钱。请求字段可选且默认 false，所以不发这个字段的 iOS 快捷指令和邮件收件行为完全不变
- **`summary_de` 必须真的是德语（#904）**：提示词要求德语在先、中文翻译在后（#708），模型偶尔把两个键**都**写成中文。这种回答非空、能解析、照样通过"成功判定只看 `summary_de`"，然后被 `annotate_de_summary` 加上拼音、再被 `knowledge/rendition.py` 当德语原文翻成法语（谷歌翻译对中文输入基本原样返回）—— 法语阅读版于是变成一堆粘连拼音。现在按**字形**判定：`zh_annotate.cjk_ratio()` ≥ `NON_CHINESE_TEXT_MAX_CJK`（0.10）即视为不是德语。实测生产库分得很干净：正常德语摘要 ≤ 0.023（合法的中文注释如 `(bólínqiáng/柏林墙)` 占比极低），写错语言的 ≥ 0.225
  - **两层防线**：① `ai.summarize_podcast_transcript()` 的候选模型循环里判定失败 → 换下一个模型（NotebookLM 路径同理，返回 None 落到 API 链）；② `knowledge/rendition.get_or_create_rendition()` 发现存量坏数据时抛 `RenditionError` 并说明原因、**不写库** —— 页面显示可读的原因，不显示乱码。已有坏条目靠详情页 Regenerate summary 修
  - 判定原语（`cjk_ratio`）住在 `zh_annotate.py`（零项目内依赖，谁都能 import），`podcast._is_chinese_text`（#750/#772 判转录方向）改为调它 —— 同一个比值不许有第三份

- **按语言渲染摘要（#804）**：知识源全语言共享，**AI 摘要只生成一次**（`summary_de` 是主版本），其它语言的阅读版本是它的谷歌翻译 + 生词标注派生物，存 `knowledge_renditions(episode_id, lang)`，第一次打开时懒生成并缓存 —— 不为每种语言再花一次 AI 的钱。中文侧**完全不走这条路**（`summary_zh` 本来就是 AI 原生的，由 `zh_annotate` 标注），零回归
  - **翻译按 HTML 文本节点分块**（`knowledge/rendition.py._translate_html_strict`）：摘要是 `<p>`/`<b>` 标记的 HTML，整块丢给谷歌翻译会两头出错——免费端点超过约 5000 字直接拒绝，而且标签会被吃掉或挪位。所以只送文本节点、标签原样保留；行数对不上就逐节点重译
  - **失败绝不写库**：`translator.translate_strict()` 是为此新加的（`translate_zh` 的契约是"失败返回原文"，那在这里等于把德语原文冒充成法语存进库）。失败时详情页显示原因，不静默退回德语
  - **重新生成摘要会清空该素材的全部 rendition**，否则旧译文会留在库里和新摘要说两套话
  - **罗曼语的生词判定不做词干还原**（`annotate/romance.py`）：靠 `database.forms_lookup()` 精确匹配 `entry_forms` 里存好的全部变位/词形 —— 这正是 #803 要求加词时把变位表生成齐全的原因。判定 = 词形表 ∪ `known_words(lang)` ∪ 功能词表；标注每篇最多 40 个词，标签内的词（`<strong>` 里的 `strong`）必须跳过，否则会毁掉标记
  - `GET /api/podcast/episodes/{id}?lang=fr` 返回 `rendition`/`rendition_error`；`known-words` 三个接口都加了 `lang`（默认 `zh`）
- **`GET /api/podcast/episodes` 加 `?kind=` 过滤**，`POST /api/knowledge/add` 只负责入库，**不在请求里做转录/摘要**——前端拿到 `episode_id` 后照常调用既有的 `POST /api/podcast/episodes/{id}/process`，造卡侧（`routes/story.py` 的 `knowledge` 模式，见「故事生成」）几乎零改动就能吃到播客以外的素材
- **YouTube 字幕在服务器上必须走 NotebookLM（#681）**：YouTube 整片封锁云服务商 IP，Contabo 的服务器调字幕 API 永远得到 `RequestBlocked`。而 `RequestBlocked`/`IpBlocked`/`PoTokenRequired`/`AgeRestricted` **全是 `CouldNotRetrieveTranscript` 的子类** —— 原来只 `except` 基类，于是"被 YouTube 拒绝"被静默写成 `no_transcript`，一个有 9833 字中文字幕的视频在界面上显示"没有字幕"。现在拒绝类异常必须在基类**之前**捕获（`_blocked_error_types()`），先转 `podcast.transcribe_url_via_notebooklm()`（`sources.add_url()` 自动识别 YouTube 链接，由 Google 自己去取，绕开我们的出口 IP，免费、不下音频），兜底也空则抛 `CaptionsUnavailable` → `status='error'` + 可读原因。**真没字幕的视频不进兜底**，仍走廉价的 `no_transcript`，不浪费几分钟的 NotebookLM 轮次
  - **代价**：NotebookLM 不能指定字幕语言，返回的是视频原声轨（那条视频拿回来的是英文 32633 字，不是本地能拿到的中文翻译轨 9833 字）。摘要提示词本来就容忍任意输入语言，所以功能通；要中文轨只能给字幕 API 配付费代理（`WebshareProxyConfig`），暂不做
- 新依赖 `youtube-transcript-api`、`trafilatura`（已在 `requirements.txt`）
- **一次性数据清理必须真的只跑一次（#688）**：`init_db()` 里 #497 那段"删除卡住的遗留 YouTube 行"按 `video_id` 是 11 位 + `status != 'summarized'` 判断，既没有一次性标记（每次启动都跑），又正好命中知识库摄取的视频 —— 生产 cron 每 2 分钟重启一次服务，于是每个新视频在 NotebookLM 转录完成前必被删除，界面上表现为"加完就消失"。现在两道保护：限定 `kind='podcast'` + `app_settings.purged_legacy_youtube_rows` 标记（标记写在"表已存在"迁移块之外，全新库首次启动也写）。**往 `init_db()` 里加任何 DELETE 之前，先想清楚它在第 100 次启动时会删掉什么**
- **Instagram Reel 摄取（`knowledge/instagram.py`，#750）**：`kind='video'`，`video_id` 存 Instagram 的短码（shortcode）。没有字幕 API 可用，`ingest_url()` 只存元数据（`yt-dlp --dump-json` 拿标题/作者/时长，标题缺失时退回 `description` 首行再退回短码），下载音频+转录都推迟到 `.../process`（`podcast._transcribe_instagram`）。**转录是降级链**（本仓库转录链一贯的风格，同 `fetch_transcript()`）：① `GROQ_API_KEY` 已配置 → Groq `whisper-large-v3-turbo`（$0.04/小时，约比 OpenAI 便宜 9 倍、快 10 倍）；② 否则/失败 → 已有的 `podcast._transcribe_via_whisper()`（OpenAI `whisper-1`，$0.006/分钟，一条 60 秒 Reel ≈ $0.006）；③ 都不行 → `status='no_transcript'`。`GROQ_API_KEY` 因此是**可选**的，不是本功能能否用的前提
  - **幻觉过滤两条转录路径都要过（`podcast._filter_whisper_hallucinations`）**：Reel 常是纯音乐无人声，Whisper 系模型会对着音乐编造整段文本。两条路径都请求 `response_format="verbose_json"` 拿到真正的分段元数据（`no_speech_prob`/`avg_logprob`）——OpenAI 这边为此把模型从播客路径默认的 `gpt-4o-mini-transcribe` 换成 `whisper-1`（前者不接受 `verbose_json`，只有 `whisper-1` 支持）。过滤三道：`no_speech_prob`/`avg_logprob` 超阈值的段落丢弃 → 同一段文本连续重复 ≥3 次判整条作废（比单看概率更强的信号）→ 剩余不足 20 词判 `no_transcript`。**绝不能把幻觉文本存进库假装成功**
  - **Instagram cookies 会过期**：下载失败时错误信息明说"可能是 cookies 过期"（`knowledge/instagram.py._yt_dlp_error_message`），这是 Daniel 唯一能看到的诊断线索（走 #749 的 Signal 回执通道）。一次性安装/cookies 导出步骤见 `scripts/README.md`
  - **短文本一度跳过 AI 摘要，现已撤销（#750 → #772）**：#750 曾给短转录（< 1000 词）加过一条零 AI 成本的路径（`_zero_cost_summary()`，靠 Google Translate 互译代替摘要）。Daniel 实际用过 Reel 之后明确改主意（2026-08-16）：**短素材也要和播客完全一致的详细 AI 摘要**。`SUMMARY_WORD_THRESHOLD`/`_zero_cost_summary()` 已删除——`_process_episode` 现在无条件调用 `summarize()`，长短素材走同一条路径。免费的全文翻译没有消失，只是搬到了下面的双语对照转录里
  - **`build_transcript_de()` 双向翻译，`zh` 槽永远放中文（#772）**：`transcript_zh`/`build_transcript_de()` 历史上假设 zh→de 单向；Reel 常是德语/英语音频，方向可能反过来。`podcast._is_chinese_text()`（CJK 字符占比 ≥0.2）判断转录语言：中文转录 → 照旧翻成德语（`_translate_segments_de`，行为与 #750 之前字节不差）；非中文转录 → 翻成中文（`_translate_segments(target="zh-CN")`）。**返回结构 `[{"zh","de"}]` 不变，约定是 `zh` 槽永远放中文那一侧、`de` 槽放非中文那一侧**——两个方向都成立，`_bilingual_transcript_html`（邮件）和 `static/app.js` 的详情页转录块因此不用关心谁是原文谁是译文，只管并排显示两栏。**不改列名、不建新表**——`transcript_zh` 存"任意语言源文本"是本仓库已有先例（同 `word_zh` 对法语存法语词形）

---

## 生词标注：代码做，不用 AI（`zh_annotate.py`，#638）

#631 靠提示词让模型标 `pinyin/汉字`，模型经常漏（德语总结里出现光秃秃的 `(浙江)`），中文总结更是一个都没标。所有材料仓库里都有，所以改成确定性代码，**零 AI 调用**：`static/hsk_levels.json`（4991 词的 HSK 1-6 表）+ `entries.word_zh`（Daniel 的词库）+ `jieba` + `pypinyin` + `translator.py`。

- **生词判定**：不在词库 **且**（HSK ≥ 5 **或** 根本不在 HSK 表里）
- **中文总结**（`annotate_zh_summary`）→ 行内 `词（pīnyīn - 德语释义）`，每词只标首次；跳过人名地名（jieba 词性 `nr`/`ns`）和单字词
- **德语总结**（`annotate_de_summary`）→ 只在中文片段前补拼音（`(浙江)` → `(Zhèjiāng/浙江)`），**不加释义**（德语原文就在旁边），也**不过滤人名地名**——德语文里的中文恰恰最需要读音；前面已经是 `/` 的说明模型自己标过了，不重复标
- **透明组合过滤只用于中文总结**：HSK 表只有 4991 个词条，`十年`/`巨大变化`/`死掉` 这类普通组合全都"不在表里"，直接标会淹没真生词。规则是"表外词若每个字都是 HSK≤4 就跳过"，字的等级由**词表反推**（`_char_levels`：字出现在任何 HSK≤4 的词里就算基础字）——表里单字词只有 696 个，直接查单字覆盖太薄
- **接入点在 `podcast.summarize()` 的返回处**（`_annotate_summary`）：NotebookLM 和 API 两条摘要路径、`_process_episode` 和 `regenerate_summary` 两个调用方全都经过这里，标注后的文本才写库，邮件/Signal/详情页三处自然一致，也不会各自重跑谷歌翻译
- **全程吞异常**：HSK 表读不到、jieba 挂了、翻译超时，一律返回原文（翻译失败降级为只有拼音）。少个拼音是小事，为它丢掉整集摘要是荒唐的
- **`extract_new_words()`（#650）统一了详情页底部生词表格**：原来表格由 AI 在摘要提示词里自己挑词，会漏词、也会挑 Daniel 已学过的词。改成代码从 `summary_zh` + `summary_de` 扫描，和正文括号标注**同源同规则**，两者现在保证一一对应，不再各说各话
- **已认识词库 `known_words`（#710）**：Daniel 认识但从没进过词库的词。详情页生词表格的 **✓ Known** 按钮 → `POST /api/known-words`（`shared.js` 的 `markWordKnown()`），纯后台请求，行标灰不刷新页面。**判定入口只有 `zh_annotate._known_words()` 一处**：`word_zh_exists(words) | known_words_exists(words)` —— 行内标注、生词表格、德语总结的拼音标注三处因此自动一致，别处不许再写第二份"已知"判定。**不建卡、不进队列**：这是"我认识了，别再给我看"，与加词恰好相反。已存库的摘要文本里的旧标注**不会**消失（生成时就写死了），变的是下一篇
- **基线词表 `annotate/baseline_*.txt`（#922）**：Daniel 进本系统之前就会的词——法语 CEFR A1-A2、中文 HSK 3.0 1-4。`annotate/baseline.py` 的 `baseline_words(lang)` 读文件（进程内缓存），并入 `zh_annotate._known_words()` 和 `annotate/romance.py` 的 `known`。同样**不建卡不进队列**，与 `known_words` 取并集，✓ Known 按钮行为不变
  - **放仓库文件不放 `known_words` 表**：① 离线同步只合并 `cards` + `review_log`，往服务器库灌 1.7 万行笔记本永远拿不到，文件随代码走；② 这是"某个等级的词表"这一静态语言学事实，不是 Daniel 逐词点出来的个人标记——后者才是 `known_words` 的语义；③ 生产库零写操作
  - **法语表必须存全部屈折形式**（13.8k 条，不是 1926 个词元）：罗曼语标注器精确匹配、零词干还原（#803），只存词元的话 `mangeons` 照样算生词。来源 FLELex（CC BY-NC-SA）取 A1/A2 词元 × Lexique 3.83（CC BY-SA）展开，配方写在文件头
  - **中文表和 `static/hsk_levels.json` 并存，不互相取代**：后者是**旧的 HSK 2.0** 表，1-4 级只有 1193 词（`KNOWN_HSK_MAX = 4`），而且还要供生词表格显示 `hsk` 列；基线表是 HSK 3.0（2021）1-4 级 3172 词，补上的是那 2100 词的缺口
  - 读不到文件降级为空集合（同 `stopwords()`），西班牙语暂无表，返回空集不报错

---

## 顶栏后台任务指示器（#821）

在故事加载页点「Continue in background」之后，主页看起来完全空闲，但 AI 调用、翻译、TTS 预加载还在跑一分钟；加词（约 30 秒）、知识库素材处理（可达十几分钟）同样是"提交完就没影了"。顶栏右侧的 ⚙ 徽标回答"现在服务器在干什么"，点开是明细列表。

- **`routes/tasks.py` 只做聚合，绝不新建第二套记账**：每个长任务都已经把进度写在某处（`ai._story_progress`、`tts._preload_progress`、`routes/imports._import_jobs`、`routes/podcast._PROCESSING_IDS`），平行的注册表迟早和它们漂移，然后开始撒谎 —— 与 #643 坚持单一加词管线是同一条道理
- **唯一例外是 Again 单句重生成**（`routes/review._spawn_again_regen`）：它此前没有任何记账，`tasks.register()`/`finish()` 就是补上的那个最小注册表。**别的地方不要用它** —— 除非同样确实没有自己的状态。`finish()` 必须写在 `finally:` 里，泄漏一条 = 一个永远转不完的任务
- **终态不是任务**：`_story_progress` 里 `done`/`error`/`idle` 的条目是留给加载页最后读一次的历史，聚合时必须滤掉；TTS 同理（`done >= total`）
- **单个采集器抛异常不许拖垮整个列表**：半份列表仍然告诉 Daniel 有东西在跑，500 什么都不告诉他。牌组/单集被删时标题回落成 id，不是报错
- **前端轮询是自适应的**（`static/app.js`）：有任务或面板打开时 3 秒，空闲时 15 秒 —— 每个打开的标签页都在轮询，闲置装机不该被打满。**请求失败不清空指示器**：请求掉了不等于活儿停了
- 无任务时整个按钮隐藏 —— 常驻的「0 tasks」只是噪音

## 复习收尾提醒（#701）

清空队列（0 open cards）后，被评为 Again 的卡片还留在学习步骤里（`1m 10m 1d 3d`）分批回来，Daniel 离开界面就无从知道它们什么时候到期。服务器 cron 每 5 分钟跑 `scripts/due_check.py` → `POST /api/review/due-notify-check`，条件成立时发一封邮件。

- **三条同时成立才发**（`database.due_notification_status()`）：`due_now > 0`（有已到期的 learning/relearn 卡）、`later_today == 0`（现在→明天日界点之间没有还在等的学习卡）、`other_due == 0`（new + review 到期卡已清零，即队列确实空过）
- **`later_today == 0` 是这个功能的全部意义**：第一张卡回来就通知，等于把 Daniel 叫回去做两张卡再干等十分钟，那还不如不提醒
- **1d/3d 步骤的卡刻意不参与判断**：它们到期在明天日界之后，属于别的一天；等它们就永远发不出去
- **计数复用 `count_due_all_decks()`**，不手写 COUNT(*)：新卡每日上限、锁定的未来 Daily 牌组、禁用的 reading 类别它都处理过了，自己写一份迟早和界面角标说两套话
- **"明天日界点"必须算成完整 ISO datetime**（`get_day_cutoff_hour()`）：`cards.due` 对 learning 卡存的是 datetime，拿日期字符串比会把凌晨 0–5 点的卡算成明天
- **每个 Anki 日最多一封**，标记存 `app_settings.due_notify_last_day`；`?force=true` 只跳过去重，条件不成立照样不发
- **SMTP 未配置 = 跳过不是失败，且此时不写标记** —— 否则等配置好了当天再也收不到
- 发信走 `podcast.send_mail()`（从 `send_email()` 抽出的通用函数，播客/知识库通知与本提醒共用一份 SMTP 逻辑）

---

## AI 词典页 `/dict`（#746）

Daniel 长期把 DeepSeek 聊天当中文词典用，再手工把结果复制进加词框。这个页面把词典搬进应用：输入德语/英语/中文的词、词组或句子 → AI 返回**结构化**查词结果 → 每个候选译法旁边一个 `★` 按钮直接加进 ★ List。**单轮问答，不做追问**（Daniel 明确说他从不追问，查完直接输下一个）。

- **结构化 JSON 而非自由 Markdown**：这是整个功能成立的前提——只有结构化才能让选项变成可点的按钮。契约见 `ai.DICTIONARY_PROMPT` 里嵌的示例（`headline`/`kind`/`sentence`/`groups[].options[]`）
- **提示词移植自 `de-zh-bot` 技能**（`ai.DICTIONARY_PROMPT`）：中文输入**直接给全部义项、不给选择**；德/英输入一律先分析 + `a/b/c` 选项（**不用数字**）+ 明确推荐、**倾向口语**（Daniel 反复强调的偏好）；整句先整句翻译再逐词拆解，每个成分单独可加词；解释语言德语，单词可附法语
- **加词仍走 `/api/add-word-ai`**（`shared.js` 的 `addWordViaAi()`）：`★` 之后照常花 30 秒生成完整词条（例句/汉字分解/量词/同义反义词齐全）。词典**不得**成为第二条加词管线（#643），哪怕它手里已经有译文——省下的那一次调用换来的是内容简陋、与其它词条不一致的条目
- **解析失败不写库**：`ai.dictionary_lookup()` 解析不出 JSON 或缺 `groups`/`zh` 就抛 `ValueError` → 500 带原始回复前 500 字。空壳词典条目比报错更糟
- **↻ Repeat 按钮（#777）**：结果标题行右侧，用**同样的 query 原样再问一次**（AI 有随机性，多半会换个说法）。`POST /api/dict/lookup` 的可选 `replace_id` 让这次结果 **UPDATE 覆盖那一行**而不是新增 —— 重查的语义是"刚才那个答案不好"，不是"两个都留着"。三条规矩：① `replace_id` 指向不存在的行 → 404，绝不静默降级成新增；② 解析失败（`ValueError`）时什么都不写，**旧的好答案原样保留** —— 一次失败的重试不能毁掉它本想改进的东西；③ 覆盖时 `query`/`lang` 不动，只换答案，`created_at` 刷新（历史仍按"最后回答时间"排序）。从历史里点开的旧记录同样能 Repeat
- **`dict_queries.headline` 是反范式的一列**：历史列表不该为了显示一行标题去解析 50 份 `result_json`
- **`lang` 目前只支持 `zh`**，传别的值返回 400——拿中文提示词去答法语请求会静默生成一个**看起来合理但错误**的词条（同 #726 在加词侧的教训）
- **页面不加载 `app.js`**（同 `/add` #668、`/save` #681）；`?q=` 打开即查询，随后 `history.replaceState` 抹掉参数——否则刷新或 iOS 恢复标签页会静默再花一次 AI 钱（#686 踩过）
- AI 返回的文本一律 `textContent`/`createElement` 渲染，全页无 `innerHTML` 拼接

---

## 书籍阅读器（#836）

上传一本德语/英语的 EPUB 或 PDF，用正在学的语言逐页阅读：每页翻译成目标语言，HSK 4 以上、又不在词库里的词就地标上 `词（pīnyīn - 德语释义）`——和知识库素材一模一样的读法。界面在主页 `📚 Books`。

Daniel 2026-08-21 定的三件事：**源书是德/英原版**（不是上传中文书只做标注）、**「一页」是定长字数块**（EPUB 根本没有页码）、**翻译走谷歌翻译**（免费；AI 翻译是以后的事）。

- **不新建管线，复用知识库那条**：`knowledge/rendition.py` 抽出 `render_html(html, lang, source)` —— 翻译（`_translate_html_strict`，按文本节点分块、标签原样保留、行数对不上就逐节点重译）+ `annotate.annotate_summary()`。`get_or_create_rendition()` 现在只是它的一层薄封装，知识库行为零变化。**一页书和一篇摘要因此按同一套规则标注**，理由同 #643 的单一加词入口
- **切页只在上传时做一次**（`books/paginate.py`）：约 1200 字一页，**只在段落边界切**，输出 `<p>` HTML —— 这正是翻译器吃的格式，源文里的 `<` 必须转义。重切会让所有已缓存的 rendition 和阅读进度整体错位，所以代码里根本没有「重新切页」这条路
- **PDF 的真实页码存 `book_pages.ref_label`**（EPUB 存章节标题），显示在页码旁边：读者要找的「PDF 第 214 页」还在，只是不再是翻页单位
- **抽不出正文必须报错，不能存一本空书**：扫描版 PDF（无文字层，本功能不做 OCR）、DRM 的 EPUB 一律抛 `BookExtractionError`，前端把服务端给的原因原样显示 —— 同 `knowledge/article.py` 200 字下限那条教训
- **EPUB 用标准库解析**（`zipfile` + `xml.etree` 走 container.xml → OPF → spine），不加 `ebooklib`/`beautifulsoup4`；也**不用 `trafilatura`** —— 它是为「在嘈杂网页里找出唯一一篇文章」调的，对干净的书籍 XHTML 会连章节标题和短段落一起丢掉
- **翻译失败绝不写库**：`render_html` 抛 `RenditionError` → 接口 502 带原因。半页德语顶着中文的名字存进去，读者要读到一半才发现
- **四张表**（`schema.sql`）：`books` / `book_pages`（源文）/ `book_renditions`（某页某语言的译文+标注，结构与 `knowledge_renditions` 有意一致）/ `book_progress`（**按 (书, 语言) 各记一份进度** —— 同一本书用中文读和用法语读是两条独立的进度线）
- **前端预取下一页**（`static/app.js` 的 `_prefetchBookPage`）：谷歌翻译免费，唯一挡在阅读前面的就是这几秒等待。←/→ 翻页；页码输入框可跳页
- **生词表格是和知识库详情页共用的组件**（`setWordTable()` / `wordTableHtml()` / `doWordTableAdd()` / `doWordTableKnown()`）：原来那套 `doPodcastAddWord`/`doPodcastKnownWord` 已改名泛化，**不复制第二份** —— 否则下次修加词的坑只会修好其中一处
- **语言下拉用 `GET /api/langs?available=1`**（全部已注册语言，不是「在用语言」）：同 `/add`、`/dict` 的理由 —— 一门还没建牌组的语言，按「在用」过滤就永远读不了第一本书
- **上传原件放 `data/books/`，不进离线同步**；上传解析走后台线程 + `job_id` 轮询，并由 `routes/tasks.py` 的 `_book_tasks` 采集器（读上传 job 已有的状态，不新建记账）出现在顶栏任务指示器里
- 测试见 `tests/test_books.py`

---

## API 接口

```
# 牌组 & 预设
GET    /api/decks                                    → 带到期数量的牌组树（?lang= 过滤）
POST   /api/decks ；PUT/DELETE /api/decks/{id}       → 创建 / 重命名 / 软删除（进垃圾桶）
GET/PUT /api/decks/{id}/preset                       → 预设（yùshè - preset）设置
GET/POST /api/presets ；DELETE /api/presets/{id}
GET    /api/langs                                    → 当前使用的语言列表
GET    /api/mode                                     → {offline, local, hard_offline}（#612/#625；offline 是实时值，本地模式下每 60 秒轮询）
GET    /api/tasks                                    → 当前正在跑的后台任务（#821）：{tasks[{id,kind,icon,label,detail,percent,started_at}], count}

# 一键同步（#625，只在 LOCAL_MODE/OFFLINE_MODE 下注册；服务器上返回 404）
POST   /api/sync/start[?mode=sync|pull]              → 后台跑 sync_offline.sh，立即返回；重复提交 409
GET    /api/sync/progress                            → {running, lines[], ok, error, started_at, finished_at}

# 垃圾桶
GET  /api/trash ；POST /api/trash/{deck_id}/restore
DELETE /api/trash/{deck_id} ；DELETE /api/trash      → 永久删除 / 清空

# 复习（均支持可选 ?lang= 过滤/隔离队列）
GET  /api/today/{deck_id}/{category}                 → {card, counts}
GET  /api/today-mixed/{deck_id}                      → 混合复习模式
GET  /api/today-unfinished ；/api/today-unfinished-decks
POST /api/review                                     → {card_id, rating, user_response?} → {next_card, counts}
POST /api/review/undo ；POST /api/review/requeue
POST /api/cards/{card_id}/bury | unbury | leech
POST /api/review/due-notify-check[?force=true]       → 复习收尾提醒检查（#701）；条件不满足时不发信也不算失败，返回判定明细

# 暂停
POST /api/decks/{id}/creating/toggle-suspension
POST /api/decks/{id}/categories/{cat}/toggle-suspension
POST /api/decks/{id}/toggle-all-suspension

# 故事 & 语音（均支持可选 ?lang=）
GET  /api/story/{deck_id}/{category}                 → 今日活跃故事（如无则生成）
POST /api/story/{deck_id}/{category}/regenerate ；GET .../history ；GET .../count
POST /api/story/{deck_id}/{category}/cancel          → 中止正在跑的生成（#828）；没有在跑返回 {cancelled:false}（不假装取消过）
POST /api/speak ；POST /api/speak-multi ；GET /api/speak-status ；POST /api/speak-stop
GET  /api/tts-file ；POST /api/preload ；POST /api/preload-session/{deck_id}/{category}
GET  /api/tts-progress/{deck_id}/{category} ；GET /api/story-progress/{deck_id}/{category}
GET  /api/news/status                                → 当日新闻缓存状态 {cached, count}（briefing 模式设置弹窗仍在用；旧 news 模式已移除，#512）
POST /api/story-sentence/{id}/star                   → 给句子加星/取消，body {starred: bool}（#692）；句子不存在 404
GET  /api/starred-sentences[?lang=&limit=]           → 全部加星句子，附生成模式/模型/episode_id/牌组/来源 + story_id/has_prompt（#692、#697）
GET  /api/story-prompt/{story_id}                    → 生成该故事的完整提示词（#697）；故事不存在 404
                                                       **不能**写成 /api/story/{id}/prompt——GET /api/story/{deck_id}/{category} 注册在前会把它当 category='prompt' 吃掉

# 知识库（播客爬虫 #479 泛化，#650–#655；详见「知识库」节）
POST /api/knowledge/add                               → body {url, china_critical?}（#731，默认 false）→ 新素材 {episode_id}；已存在 {status:"already_exists", episode_id}；不转录不摘要，前端拿 id 后另调 .../process
POST /api/knowledge/add-text                          → body {text, title?, author?, source_url?, china_critical?}（#668；#833 起除 text 外全可选，留空由 AI 从正文抽取）→ 同上契约
POST /api/knowledge/add-file                          → multipart：file（.txt/.md/.pdf/.docx，≤10 MB）+ title?/author?/source_url?/china_critical?（#835）→ 同上契约；未知类型 / 抽不出文字 / 正文太短均 400
GET/POST /api/known-words ；DELETE /api/known-words/{word} → 已认识词库（#710）：标记后 zh_annotate 不再当生词；不建卡不排程；DELETE 词不在表里返回 404（不假装成功）
POST /api/podcast/check                              → 跑一轮抓取，返回汇总 {new, summarized, emailed, failed}
GET  /api/podcast/episodes                            → 统一素材列表（不含转录全文；手动处理中的单集 status 显示为 processing）
                                                       #936 起：?sort=processed_at|published_at|created_at|title|author|duration & ?order=asc|desc（白名单，未知值回落默认而不是 400）
                                                       筛选轴 ?kind= ?platform= ?author= ?tag= ?status= 均**可重复**（轴内 OR、轴间 AND）；?since=YYYY-MM-DD、?feed_id=、?list_id=
                                                       ?include_archived=（默认 false，归档素材默认不出现）
GET  /api/knowledge/facets                            → 筛选栏所有下拉的选项，一次拿全（#936）：{kinds,platforms,authors,statuses,feeds,tags,lists,archived_count}
GET  /api/podcast/episodes/{id}                       → 详情（摘要 + 转录 + HSK 生词）
POST /api/podcast/episodes/{id}/retry                 → 同步重跑单集（error/no_transcript/pending；#491/#500）
POST /api/podcast/episodes/{id}/process               → 手动触发单集转录+摘要（后台线程，立即返回；重复提交 409；#502）
POST /api/podcast/episodes/{id}/notify                → 按需重发通知，body {channel: signal|email}（同步；仅 summarized；重发不更新 email_sent_at；返回 {sent}，失败时 sent:false 带 detail；#530）
POST /api/podcast/episodes/{id}/regenerate-summary    → 仅重跑摘要步骤（后台线程，复用已存转录，不重发通知；仅 summarized 且有转录；失败不动旧摘要/状态；#567）
GET/POST /api/podcast/feeds ；PUT/DELETE /api/podcast/feeds/{id} → RSS 源管理（#502；POST 抓取验证并提取节目标题；PUT 改 auto_process/title）
GET/PUT /api/podcast/config                           → 读/改设置（email_to/detail_level/enabled/transcriber/whisper_max_minutes/summarizer[auto|api]（#510）；feeds 已迁到 podcast_feeds 表（#502），whisper_fallback、channel_url、channel_id、whisper_title_filter 已废弃但兼容，#497）

# 提示词版本库（#581 单份自定义 → #610 每模式多个命名版本；story/qa/expository/podcast，仅中文）
# prompt_presets 表：每 mode 可存多个命名版本，最多一行 is_active=1；无生效版本 = ai.DEFAULT_PROMPT_TEMPLATES 内置默认
GET    /api/prompt-template/{mode}                   → {template, default, is_custom, variables, presets[], active_id}
DELETE /api/prompt-template/{mode}                   → 取消生效（回内置默认；#610 起不再删除已保存版本）
POST   /api/prompt-presets/{mode}                    → 新建命名版本并设为生效（body {name, template}，须含 {words}；重名 409）
PUT    /api/prompt-presets/{id}                      → 改名/改内容（body {name?, template?}）
POST   /api/prompt-presets/{id}/activate             → 设为该 mode 唯一生效版本
DELETE /api/prompt-presets/{id}                      → 删除该版本
# 旧的 PUT /api/prompt-template/{mode} 已由 POST/PUT /api/prompt-presets/* 取代（#610）

# 成本
GET  /api/costs                                      → 成本历史（动作分组；balances 列出各提供商余额，#580；Again 单句重生成有正式标签且相邻同标签 30 分钟内合并，#578）
GET  /api/costs/call/{id}                            → {prompt, response}（完整提示词含 [system] 段 + AI 回答，各截断 3 万字符，#579）

# 其他
POST /api/import                                     → 触发 YAML 导入
GET  /add[?word=生态&day=today|tomorrow|list&lang=zh|fr] → 独立加词页（#668，可收藏/存主屏；不加载 app.js）；带 word 参数时打开即自动提交（#686，供 iOS 快捷指令用），提交后从地址栏抹掉该参数以免刷新重复扣费；lang（#726）每种语言一个快捷指令
GET  /save                                           → 独立素材收藏页（#681，🔗 Link / 📋 Text / 📎 File 三个标签，#835；同样不加载 app.js）

# 书籍阅读器（#836，详见「书籍阅读器」节）
GET    /api/books                                    → 书列表（每本带 progress = {语言: 页码}）
POST   /api/books                                    → 上传 EPUB/PDF（multipart：file + 可选 title/source_lang/char_budget）
                                                       → {job_id}；解析+切页在后台线程，抽不出正文时任务失败且不入库
GET    /api/books/upload-progress/{job_id}           → 轮询上传解析结果；未知 job → 404
DELETE /api/books/{id}                               → 删书（级联删页/rendition/进度 + 磁盘原件）；不存在 → 404
GET    /api/books/{id}/page/{page_no}[?lang=zh]      → 该页译文+生词 {text, new_words, ref_label, page_count, cached}
                                                       未缓存时同步翻译+标注（几秒）；翻译失败 → 502 且**不写库**；越界 → 404
POST   /api/books/{id}/progress                      → body {lang, page_no}；进度按 (书, 语言) 各存一份

# AI 词典（#746，详见「AI 词典页」节）
GET    /dict[?q=anordnen]                            → 独立词典页（不加载 app.js）；带 q 时打开即查询并抹掉参数
POST   /api/dict/lookup                              → body {query, lang?='zh', model?, replace_id?} → {id, created_at, query, result}
                                                       同步约 5–15 秒；query 空 / lang 非 zh / AI 关闭 → 400；解析失败 → 500 且**不写库**
                                                       replace_id（#777，Repeat 按钮）→ 覆盖该行而非新增；行不存在 → 404；失败时旧答案不动
GET    /api/dict/history[?q=&limit=]                 → 历史列表（不含 result_json；q 对 query/headline 做 LIKE）
GET    /api/dict/history/{id} ；DELETE /api/dict/history/{id}  → 单条 / 删除；不存在均 404（不假装成功）
POST /api/add-word-ai                                → 界面内添加生词（#627）；body {word_zh, day?:today|tomorrow|list, lang?:zh|fr}（#636、#677；#715 起界面一律传 list，接口三值仍有效；lang 见 #726，已存在的词按 entries.lang 落点、不听该参数）；新词返回 {job_id}，已有词直接返回 {status}。**全应用唯一的加词入口**（#643）：顶栏 ＋、播客生词表格、复习界面长按菜单都走它
GET  /api/add-word-ai/progress/{job_id}              → 轮询后台生成+导入的结果
GET  /api/browse                                     → {deck_id?, category?, state?, q?, lang?}
GET  /api/browse-words[?lang=] ；GET /api/search-words?q=[&lang=]  → Browse 页的词表/搜索（#815）：按 **entries.lang** 过滤（不是 decks.lang——无卡片的 reference 词条也要出现），不传则返回全部；前端还按语言过滤侧栏牌组树、非中文时隐藏 Hanzi 区
DELETE /api/word/{id}                                → Browse 单行 🗑：硬删除词条及其全部卡片（级联，不进垃圾桶）；不存在返回 404（不假装成功）
GET  /api/stats ；/api/retention ；/api/card-evolution（均支持 ?lang=）
```

---

## 测试（`tests/`，`pytest tests/` 全套约 11 秒）

- **隔离数据库只能打 `database.core.DB_PATH` 这个补丁**：`database/__init__.py` 是 `from .core import *`，`database.DB_PATH` 只是一份名字副本，`get_db()` 读的是 core 的模块全局。打错位置不会报错，测试会**静默写进真实的 `data/srs.db`**（#615，`test_importer`/`test_api` 曾这样跑了很久）。`tests/conftest.py` 还额外把 `DB_PATH` 指向临时目录兜底
- `conftest.py` 有个 autouse 夹具把 `tts._ensure_cached` 打桩——否则故事相关的测试会真的去连 edge-tts（曾让 `test_api` 从 2 秒变成 85 秒，且断网必挂）
- **AI 要打桩在 `ai._call_api` 上**，不要打在某个提供商的客户端上：默认模型换过（Claude → DeepSeek），打在 `anthropic.Anthropic` 上的补丁会静默失效（#615）
- 导入器建的牌组嵌套在 `All` 下面。用 `get_or_create_deck("Kouyu")` 查找会因为默认 `parent_id=None` **新建一个空牌组**，查什么都是空——按名字查已存在的牌组（见 `test_importer.deck_id_by_name`）
- 测试替身的签名/返回格式会随生产代码漂移而悄悄作废。桩函数尽量吃 `**kwargs`，断言写在稳定的契约上

---

## 规范与约束

- 所有数据库访问通过 `database/` 包——其他文件不写原始 SQL（`import database` 仍然有效）
- 保持 `ai.py` 简洁——每种提示词类型对应一个函数；AI 返回的格式错误 JSON 始终用 try/except + 回退处理
- 允许的外部依赖：`fastapi`、`uvicorn`、`anthropic`、`openai`、`edge-tts`、`pyyaml`、`python-multipart`、`jieba`、`pypinyin`、`alibabacloud_tingwu20230930`、`zhconv`（NotebookLM 转录繁转简，#500）（播客通义听悟转录主力，#498，官方 SDK）、`notebooklm-py`（播客 NotebookLM 可选转录，#486，非官方库，凭据文件一次性从本地拷到服务器，见 `scripts/README.md`）、`youtube-transcript-api`（知识库 YouTube 字幕摄取，#651）、`trafilatura`（知识库文章正文抽取，#652）、`pypdf` + `python-docx`（知识库文件上传，#835；`pypdf` 同时供书籍阅读器读 PDF，#836）。**EPUB 走标准库解析，不加 `ebooklib`/`beautifulsoup4`**（#836）。新增依赖必须同步更新 `requirements.txt`。播客转录链的 Whisper/NotebookLM 两条路径（听悟提交直链不需要）需要系统级 `ffmpeg`（`apt install ffmpeg`，不是 Python 依赖，缺失时该功能自动跳过）
- 前端无构建步骤——直接编辑 `static/` 下的文件
- API 密钥只从环境变量读取，绝不写入代码或仓库
- **不要在 8000 端口跑测试服务器**——Daniel 的浏览器连着它
- API 价格表在 `database/stats.py` 的 `_MODEL_PRICING`（含 `_PRICING_AS_OF` 生效日期）；各提供商都没有价格查询 API，价格变动或新模型上线时需手动更新该表，并同步 `static/index.html` 里的静态价格表（设置弹窗 `price-table-popup`）
