# 知识库（Knowledge Base）总体设计

> 把现有的「播客」功能泛化成一个统一的知识库：播客单集、YouTube 视频、报刊文章
> 三类素材走**同一条流水线**（获取 → 转录/正文 → 中文+德语摘要 → 生词标注 →
> 通知 → 造卡）。本文件是这个功能的唯一设计说明，各阶段 Issue 都引用它。

---

## 核心判断：不新建表，泛化现有表

`podcast_episodes` 已经承载了整条链路所需的全部列（`transcript_zh`、
`summary_de`、`summary_zh`、`hsk_words`、`status`、`transcript_source`…）。
YouTube 视频和文章的**唯一区别是「怎么拿到中文正文」**，下游 100% 相同。

因此：

- **表名保持 `podcast_episodes`**，加一列 `kind`（`podcast` | `video` | `article`）。
  改表名要重建表 + 迁移生产库，风险远大于收益。本仓库已有多处同类先例
  （`youtube_url` 列现在存播客网页链接、`word_zh` 对法语存法语词形）。
- **`database.get_episode(id)` 保持不变** → 造卡侧（`routes/story.py` 的
  podcast 模式）几乎零改动就能吃到视频和文章。
- 列名的历史含义在 `schema.sql` 注释里写清楚，不要重命名。

### 列的新含义

| 列 | podcast | video | article |
|---|---|---|---|
| `kind` | `podcast` | `video` | `article` |
| `video_id` | RSS guid | YouTube video id | 规范化后的 URL（去 utm 参数）|
| `channel_id` | RSS feed URL | YouTube 频道 id（拿得到就存）| 站点域名 |
| `youtube_url` | 单集网页 | 视频链接 | 文章链接 |
| `audio_url` | mp3 直链 | NULL | NULL |
| `transcript_zh` | 转录 | 字幕全文 | 正文全文 |
| `transcript_source` | tingwu/whisper/notebooklm | `captions` | `article` |
| `title_en`（新列）| AI 译的英文标题 | 同左 | 同左 |

## 素材来源与投递渠道

三个渠道，**分阶段做，不要一次全上**：

1. **界面输入框**（阶段 B 一起做）：知识页顶部粘贴 URL → 「添加」。零新依赖、
   零故障点，手机浏览器也能用（已有 HTTPS + Basic Auth）。
2. **邮件收件**（阶段 F，最后做）：手机「分享 → 邮件」发到专用邮箱，服务器
   cron 用 `imaplib` 轮询取 URL 入库。
3. **Signal 接收**：**不做**。`signal-cli` 需要常驻 `receive` 轮询，关联设备掉线
   会静默停摆，比发送脆弱得多。

## 生词：只信 `zh_annotate`，不信 AI（修 Daniel 报的「不准」）

现状有**两套并存**的生词逻辑，这就是不准的根因：

- 正文标注 → `zh_annotate.py`（#638，确定性代码，已查 `entries.word_zh`）✅
- 底部表格 `hsk_words` → **AI 在摘要提示词里自己挑的**，漏词、挑已学过的词 ❌

阶段 A 统一到 `zh_annotate`：表格由代码从 `summary_zh` + `summary_de` 里的中文
扫描生成，与正文标注**同源同规则**，看到的括号注释和表格里的行一一对应。

**生词判定规则**（维持 #638 现状，Daniel 2026-08-09 确认）：
不在 `entries.word_zh` **且**（HSK ≥ 5 **或** 不在 4991 词的 HSK 表里）；
表外词再过一道「每个字都是 HSK≤4 就跳过」的透明组合过滤，
挡掉 `十年`/`巨大变化` 这类刷屏噪音。

## 成本（Daniel 问的）：不用担心

一小时素材 ≈ 1.5 万字 ≈ 1.1 万 token。DeepSeek 输入 $0.27/M：

- 摘要一次 ≈ **$0.003**
- 造卡再发一次全文 ≈ **$0.003**

整篇转录直接喂给模型完全不心疼，**不需要为省钱做截断优化**。现有的 15000 字
截断保留即可（那是为了上下文窗口，不是为了钱）。

---

## 阶段划分

| 阶段 | Issue | 内容 | 依赖 |
|---|---|---|---|
| A | #650 | 数据模型泛化（`kind`/`title_en` 列）+ 生词统一到 `zh_annotate` | — |
| B | #651 | YouTube 摄取（字幕 API + oEmbed 标题）+ `POST /api/knowledge/add` | A |
| C | #652 | 文章摄取（正文抽取） | B |
| D | #653 | 前端：播客页 → 知识页（播客/视频/文章三个子标签） | A |
| E | #654 | 造卡：故事的 podcast 模式 → 知识库模式（按类型筛选选素材） | A、D |
| F | #655 | 邮件收件（IMAP 轮询） | B、C |

每阶段一个分支、一个 PR、CI 绿了才合并。

---

# 第二轮：素材库化（大方向 #934，2026-08-25）

第一轮（#650–#655）解决的是"各种素材都能进来，走同一条流水线"。素材攒到一定
数量之后，问题变成了**怎么找回来**：按 kind 分的四个标签页要求你先猜它属于哪
一类，没有搜索、没有标签、没有"待读"，标题作者错了也改不了。

Daniel 2026-08-25 提出的目标：一个按处理日期排序、排序可换、能按作者/平台/标
签筛、能全文搜索、能建 Read Later 这类列表、并且元数据可以手改的素材库。

## 六个阶段

| 阶段 | Issue | 内容 |
|---|---|---|
| 1 | #935 | 数据层：`processed_at`/`author`/`platform`/`manual_fields`/`archived_at` 列 + 标签表 + 列表表 |
| 2 | #936 | 统一素材列表：排序 + 筛选栏 + 一个 Add 按钮（四个 kind 子标签取消） |
| 3 | #937 | 元数据可手改 + `manual_fields` 保护 |
| 4 | #938 | AI 自动打标签 + 标签管理（合并/删除） |
| 5 | #939 | 全文搜索（FTS5，覆盖转录 + 摘要 + 各语言 rendition） |
| 6 | #940 | 自定义列表 + Read Later + 左右滑动手势 + 归档 |

## 几条贯穿全局的决定

**`kind` 和 `platform` 是正交的两个轴。** `kind` 是"它是什么"（podcast /
video / article / newsletter），`platform` 是"它从哪来"（youtube /
instagram / podcast / web / upload / paste / email / signal）。两个都要能
筛，而且 `platform` **不能由 `kind` 推出来**——上传的文件、newsletter、
Signal 分享全都是 `kind='article'/'newsletter'` 的粘贴正文，来源却完全不同。
所以 `ingest_text(platform=...)` 由每个调用方自己传。

**`author` 没有复用 `channel_id`。** 后者已经同时表示 RSS 源 URL / YouTube
频道 id / 网站域名 / 粘贴时填的作者，四种含义并存——按它做作者筛选是不可能
的。新列只在**真的知道作者**时才写；网站域名不是作者，宁可留空。

**人写的东西优先于机器写的东西。** `manual_fields`（列）和
`knowledge_item_tags.source`（行）是同一条规则的两种形态：Daniel 改过的字段
和打过的标签，任何 AI 路径都不许覆盖。反过来，他编辑标签时也不会误删 AI 的
建议——`set_item_tags()` 只替换同 source 的行。

**索引和缓存跟着数据走，但绝不用触发器。** 搜索索引挂在
`database.update_episode()` 的 `_SEARCHABLE_COLUMNS` 判断、
`save/delete_knowledge_rendition()` 和 `update_episode_metadata()` 上。写在
`schema.sql` 里的触发器，之后改这些列的人根本看不见它；而且两个数据源要解析
JSON，SQL 做不到。

**回填要么幂等、要么带一次性标记。** 生产每 2 分钟重启一次就跑一次
`init_db()`（#688 的教训）。`processed_at`/`platform` 的回填限定在目标列仍为
NULL 的行上；全量建搜索索引要读遍所有转录，所以用
`app_settings.knowledge_fts_built` 标记。

## 还没做

**"问 AI 关于我的知识库"**（Daniel 提到但当时没想好怎么做）。#939 的 FTS5
索引是它的地基：先有能搜的索引，之后加一层"检索 + 喂给 AI"即可，不用推倒重
来。留待单独设计。
