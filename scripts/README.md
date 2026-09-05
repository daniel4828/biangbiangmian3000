# scripts/

## morning_pregen.py（issue #420，issue #458 重构）

新闻 / 简报模式的故事生成很慢（多次串行 AI 调用），Daniel 早上打开页面时
不想等。这个脚本对**运行中的服务器**发一次 `POST /api/pregen-today` 请求，
由服务器端"重复最近一天真正用过的故事键"：

- 服务器找到最近一天（今天除外，最多回看 14 天）所有真正生成过的故事键
  `(deck_id, category, lang)`——即 Daniel 昨天实际复习用到的牌组/类别/模式
  组合（包括 briefing/news 等聚合牌组模式），各键沿用上次的生成参数
  （mode/topic/grammar 等；news/briefing 的 articles 被丢弃，重新抓当天新闻）
- 每个键：今天已有缓存故事→跳过；没有到期卡→跳过；否则同步生成故事并
  预热 TTS 音频缓存
- 旧版（#420）遍历全部叶子牌组、一律用默认 `mode="story"` 生成——每天产出
  大量没人看的故事，真正用到的聚合牌组反而漏掉，已废弃

只依赖 Python 标准库（`urllib.request`、`base64`、`json` 等），不需要安装
任何依赖，本地（launchd）和服务器（cron，见 issue #417）都可以直接用同一
份脚本。

### 前提

- 服务器（`bash run.sh` 或对应 systemd 服务）必须已经在运行——脚本只发
  HTTP 请求，不会自己启动服务器。
- 服务器串行处理各键（新闻/简报模式可能耗时数分钟，脚本设置了 15 分钟
  超时），单键失败只记录错误、不中断整体；脚本把返回的汇总
  （generated / skipped_cached / skipped_no_due / failed）逐项打印。

### 用法

```bash
# 默认连接本机 8000 端口
python scripts/morning_pregen.py

# 指定服务器地址
BASE_URL=http://127.0.0.1:8001 python scripts/morning_pregen.py

# 如果服务器加了 HTTP Basic 认证（配合认证相关 issue）
AUTH_USERNAME=daniel AUTH_PASSWORD=xxxx python scripts/morning_pregen.py
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BASE_URL` | `http://127.0.0.1:8000` | 目标服务器地址 |
| `AUTH_USERNAME` | 无 | HTTP Basic 认证用户名（可选，需和 `AUTH_PASSWORD` 一起设置） |
| `AUTH_PASSWORD` | 无 | HTTP Basic 认证密码（可选） |

退出码：全部成功或没有待处理项时为 `0`；只要有一项失败就是 `1`（方便
launchd/cron 的失败通知）。

---

### macOS：用 launchd 每天早上 06:00 自动运行

launchd 是 macOS 的定时任务机制（比 cron 更适合 Mac，因为它能处理系统
休眠/唤醒）。

1. 创建 plist 文件 `~/Library/LaunchAgents/com.biangbiangmian3000.morning-pregen.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.biangbiangmian3000.morning-pregen</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/daniel/Documents/biangbiangmian3000/scripts/morning_pregen.py</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>BASE_URL</key>
        <string>http://127.0.0.1:8000</string>
    </dict>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>6</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>/Users/daniel/Documents/biangbiangmian3000/data/morning-pregen.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/daniel/Documents/biangbiangmian3000/data/morning-pregen.err.log</string>
</dict>
</plist>
```

（路径按实际用户名/项目路径调整；如果生产服务器加了认证，把
`AUTH_USERNAME`/`AUTH_PASSWORD` 也加进 `EnvironmentVariables`。）

2. 加载并启动：

```bash
launchctl load ~/Library/LaunchAgents/com.biangbiangmian3000.morning-pregen.plist
```

3. 常用管理命令：

```bash
# 立即手动触发一次（不用等到 06:00）
launchctl start com.biangbiangmian3000.morning-pregen

# 查看日志
tail -f ~/Documents/biangbiangmian3000/data/morning-pregen.log

# 卸载
launchctl unload ~/Library/LaunchAgents/com.biangbiangmian3000.morning-pregen.plist
```

---

### Linux 服务器：cron + systemd

服务器上假设 FastAPI 服务由 systemd 管理（见 issue #417 的部署文档），
cron 只负责在服务已经运行时定时触发预生成：

```cron
# 每天 06:00 运行早晨预生成脚本（假设服务已由 systemd 常驻运行）
0 6 * * * cd /opt/biangbiangmian3000 && /usr/bin/python3 scripts/morning_pregen.py >> data/morning-pregen.log 2>&1
```

用 `crontab -e` 添加以上一行。如果服务地址/端口非默认，加上环境变量：

```cron
0 6 * * * cd /opt/biangbiangmian3000 && BASE_URL=http://127.0.0.1:8000 /usr/bin/python3 scripts/morning_pregen.py >> data/morning-pregen.log 2>&1
```

---

## podcast_check.py（issue #479，RSS 源 #497，听悟转录 #498）

对运行中的服务器发一次 `POST /api/podcast/check` 请求：服务器遍历配置的
播客 RSS 源（`podcast_config.feeds`，JSON 数组，默认种子为声动早咖啡 +
声东击西两个 feed）看有没有新单集，对每个新单集转录（见下方转录链）、生成
德语摘要 + HSK5+ 生词表（AI，`ai.resolve_briefing_model()`），并给
`podcast_config.email_to` 发邮件通知。同一单集（RSS item guid，存在
`video_id` 列，唯一约束）不会重复处理。风格与 `morning_pregen.py` 一致：
除转录链的可选依赖外不需要额外安装。

**RSS 源（issue #497，取代已死的 YouTube 频道源）：** YouTube 对服务器
数据中心 IP 强制 bot 验证，Cookie 方案（#491）也很快失效，於是改用播客
官方 RSS 的 MP3 enclosure 直链——没有 bot 墙，不需要 Cookie，标题/日期/
时长都在 feed 里现成。`fetch_new_videos()` 遍历 `podcast_config.feeds`
里的每个 feed URL，用标准库 `xml.etree` 解析：单集唯一 id 用 `<guid>`
（没有则退化用 enclosure URL），标题/发布时间/单集网页链接
（`<link>`，存进 `youtube_url` 列，字段名是历史遗留）、MP3 直链
（`<enclosure url=...>`，存 `audio_url`）、时长（`<itunes:duration>`，
支持"秒"/"MM:SS"/"H:MM:SS"三种格式，解析成 `duration_seconds`）都直接来自
feed。每个 feed **首次**被爬到时（该 feed 在库里一集都没有）只回填最新 3
期；之后的每次爬取只收集"比库里已知的最新一集更新"的单集（feed 按惯例
新到旧排列，扫到第一个已知 guid 就停），避免像声动早咖啡这种有上千期历史
的日更节目在某一轮把几百期旧节目全部当作"新"的塞进来。

单集时长（RSS 自带，不用下载音频就知道）作为**下载前**的护栏：超过 3
小时的单集直接跳过（成本/时间护栏），Whisper 另有独立的
`whisper_max_minutes` 门槛（见下）。

**转录链（issue #498 通义听悟为主力，取代 #486/#485 的 NotebookLM/Whisper
两级链）：**

1. **通义听悟（官方 API，主力，issue #498）**：把 RSS 的 MP3 直链原样提交
   给阿里云通义听悟离线转写接口（`CreateTask`，`type=offline`，
   `Input.FileUrl=<直链>`，`Input.SourceLanguage=cn`）——**不需要下载音频**，
   官方 API，约 ¥0.6/小时（比 Whisper 的约 ¥1.3/小时更便宜），新用户有
   90 天每天 2 小时免费额度。轮询 `GetTaskInfo`（间隔 15 秒，上限 20
   分钟）等 `TaskStatus=COMPLETED`，再从 `Result.Transcription`（一个指向
   JSON 转写结果的 URL）下载并拼接成纯文本。需要环境变量
   `ALIBABA_CLOUD_ACCESS_KEY_ID`/`ALIBABA_CLOUD_ACCESS_KEY_SECRET`（SDK
   标准命名）+ `TINGWU_APP_KEY`（控制台创建应用拿到），任一缺失或调用失败
   都只记日志、落到下一级，不会让爬虫报错（见下方一次性开通步骤）。
2. **Whisper（付费，保底，issue #485）**：听悟未配置/失败时落到这里——
   **但仅当单集时长 ≤ `podcast_config.whisper_max_minutes`（默认 30
   分钟，0=不限制）时才会尝试**，否则直接跳过、记日志（issue #495：早咖啡
   类短节目 10-15 分钟，Daniel 不想为 60-90 分钟的长节目付费）。这一级
   才会真正下载音频（`urllib` 直接拉 RSS 的 MP3 直链，不再用 yt-dlp）→
   `ffmpeg` 转 16kHz 单声道 32kbps 单个 mp3（超过 20 分钟按段切分，
   `-c copy` 不重新编码）→ 逐段调用 OpenAI `gpt-4o-mini-transcribe`（需要
   `OPENAI_API_KEY`）转录后拼接。
3. **NotebookLM（免费但非官方，可选，issue #486）**：Whisper 也失败/被
   门槛跳过时落到这里，复用同一份已下载的 mp3 → 上传到专用笔记本
   "biangbiangmian3000 Transcripts"（笔记本 id 缓存进
   `podcast_config.notebooklm_notebook_id`）→ 轮询等索引完成（上限 10
   分钟）→ 读取来源全文（fulltext）作为转录 → 删除该来源（防止笔记本无限
   膨胀）。用的是非官方库 `notebooklm-py`（见下方一次性设置），未认证时
   自动跳过（不报错）。

`transcriber` 可选值：`auto`（默认，免费优先依次尝试 NotebookLM → 听悟 →
Whisper，#510）| `tingwu`（只走听悟；听悟带段级时间戳，摘要可标注大致时间点，
#543）| `whisper`（跳过听悟，只走 Whisper，仍受时长门槛）
| `notebooklm`（只走 NotebookLM）| `off`（整条转录链都不走）。旧键
`whisper_fallback=0` 仍兼容，等价于 `off`。

音频（走 Whisper/NotebookLM 时）用完立即删除，两条路径共用同一份下载+
转码结果，不会重复下载；听悟提交直链完全不下载。**Whisper/NotebookLM 需要
服务器安装 `ffmpeg`**（`apt install ffmpeg`）——缺失时只记警告并跳过这两条
路径（听悟不受影响）。`DISABLE_AI=1`（开发模式）下整条转录链都不会触发，
避免意外调用外部服务。每期转录用了哪条路径（`tingwu`/`whisper`/
`notebooklm`）都会记日志，并存进 `podcast_episodes.transcript_source`。

### 通义听悟一次性开通设置（issue #498）

1. 阿里云控制台开通"通义听悟"服务（新用户 90 天每天 2 小时免费额度）
2. 控制台 [RAM 访问控制] 创建 AccessKey（`AccessKey ID` /
   `AccessKey Secret`），建议用独立的最小权限子账号而非主账号 root key
3. 通义听悟控制台创建一个"应用"，拿到 `AppKey`
4. 把三个值写进服务器的 `.env`（或 systemd 环境文件）：

```bash
ALIBABA_CLOUD_ACCESS_KEY_ID=xxxx
ALIBABA_CLOUD_ACCESS_KEY_SECRET=xxxx
TINGWU_APP_KEY=xxxx
```

未配置这三个变量时 `_transcribe_via_tingwu` 只记 info 日志并返回
`None`（视为"未开通"），整条链自动落到 Whisper/NotebookLM——**不会**让
爬虫报错。失败的单集会被每轮自动重试（7 天内的 error 状态），也可以用
`POST /api/podcast/episodes/{id}/retry` 手动逐集重试。

### NotebookLM 一次性认证设置（issue #486）

NotebookLM 没有公开 API，`notebooklm-py` 用的是非官方的浏览器 Cookie /
master-token 方式，需要在**有浏览器的机器（Daniel 的 Mac）** 上登录一次，
再把凭据文件复制到服务器：

```bash
# 1. 本地装库（含浏览器登录用的 Playwright 支持）
pip install 'notebooklm-py[browser]'

# 2. 本地登录（会弹出浏览器窗口，用 Google 账号登录一次）
notebooklm login

# 3. 认证信息默认存在 ~/.notebooklm/storage_state.json（或
#    ~/.notebooklm/profiles/<profile>/storage_state.json，用了 profile 的话）
#    把这个文件复制到服务器同样的路径（用普通用户权限运行播客爬虫的账号下）：
scp ~/.notebooklm/storage_state.json anki@<server>:~/.notebooklm/storage_state.json

# 4. 服务器上验证凭据可用（不需要浏览器，纯本地校验+可选网络测试）
notebooklm auth check --test
```

服务器无头环境不需要装 `[browser]` extra（`requirements.txt` 里的
`notebooklm-py` 是精简版，浏览器登录只在本地跑一次）。会话过期后
`notebooklm auth refresh` 可自愈（刷新 CSRF/session token，不需要重新走浏览器
登录），**建议服务器 cron 里定期跑一次**：

```cron
# 每天凌晨刷新一次 NotebookLM 会话，防止过期
0 3 * * * NOTEBOOKLM_HOME=/home/anki/.notebooklm /usr/local/bin/notebooklm auth refresh --quiet >> /home/anki/biangbiangmian3000/data/notebooklm-refresh.log 2>&1
```

凭据文件不存在或加载失败时，`_transcribe_via_notebooklm` 只记 info 日志并
返回 `None`（视为"未认证"）——NotebookLM 是转录链最后一级，失败即
`no_transcript`——**不会**让爬虫报错。

### Signal 通知一次性安装/关联设置（issue #521）

播客通知除了邮件还可以发到 Signal（Daniel 自己账号的"给自己的备忘 /
Note to Self"），走 `signal-cli` 以**关联设备**方式挂在 Daniel 的手机号
下——像 Signal Desktop 一样扫码配对，不需要第二个手机号，也不需要额外
付费。以下步骤在**服务器**上、以运行播客爬虫的 `anki` 用户执行：

```bash
# 1. 装依赖：Java 运行时（signal-cli 需要）+ qrencode（终端里显示二维码）
sudo apt install openjdk-21-jre-headless qrencode

# 2. 从 GitHub releases 下载 signal-cli 并解压到 /opt/signal-cli
#    （版本号去 https://github.com/AsamK/signal-cli/releases 查最新的）
SIGNAL_CLI_VERSION=0.13.x
curl -L -o /tmp/signal-cli.tar.gz \
  https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz
sudo tar xf /tmp/signal-cli.tar.gz -C /opt
sudo mv /opt/signal-cli-${SIGNAL_CLI_VERSION} /opt/signal-cli
sudo ln -sf /opt/signal-cli/bin/signal-cli /usr/local/bin/signal-cli

# 3. 以 anki 用户关联设备：打印出一个 sgnl:// URI，
#    用 qrencode 转成终端二维码，然后用手机 Signal App
#    「关联新设备」扫码（必须由 anki 用户运行——关联数据存在
#    anki 用户的 home 目录下，其他用户跑 signal-cli 看不到这个账号）
sudo -u anki signal-cli link -n "biangbiangmian3000" | qrencode -t ansiutf8

# 4. 关联成功后，把号码写进服务器的 .env（或 systemd 环境文件）：
SIGNAL_ACCOUNT=+49xxxxxxxxx
SIGNAL_CLI_PATH=/usr/local/bin/signal-cli   # 可省略，默认就是 "signal-cli"
```

关联数据（session/身份密钥）存在 `/home/anki/.local/share/signal-cli`——
**必须**始终以 `anki` 用户执行 `signal-cli` 命令（包括手动测试），换用户
会看不到这个已关联的账号，需要重新扫码关联。`SIGNAL_ACCOUNT` 未配置时
`send_signal` 只记 info 日志并返回 `False`（视为"未启用"）——**不会**让
爬虫报错，与邮件通知完全独立、互不影响。

🔴 **`/home/anki/.local/share/signal-cli` 必须纳入服务器备份**——丢了这
个目录（换机、误删）唯一的恢复办法是重新扫码关联，没有其他找回途径。
issue #749 的 Signal 知识库入口（见下面 `signal_check.py`）复用的是同一
个关联账号/同一份会话数据，所以这个目录现在同时承载"发通知"和"收链接"
两种用途，重要性比 #521 时更高。

用法与环境变量同 `morning_pregen.py`（`BASE_URL`/`AUTH_USERNAME`/`AUTH_PASSWORD`）。
另外邮件发送需要 SMTP 环境变量（`SMTP_HOST`/`SMTP_PORT`/`SMTP_USERNAME`/
`SMTP_PASSWORD`/`SMTP_FROM`/`PUBLIC_BASE_URL`，见 CLAUDE.md 环境变量表），
Signal 通知需要 `SIGNAL_ACCOUNT`/`SIGNAL_CLI_PATH`（见上）——两者任一未配置时
服务器只是跳过对应通道并记日志，不算失败。

```bash
python scripts/podcast_check.py
```

### 服务器 cron：每小时检查一次

```cron
0 * * * * cd /opt/biangbiangmian3000 && /usr/bin/python3 scripts/podcast_check.py >> data/podcast-check.log 2>&1
```

---

## knowledge_mail_check.py（issue #655）

知识库邮件收件：手机「分享 → 邮件」把链接发到一个专用邮箱，这个脚本用
标准库 `imaplib`/`email` 轮询该邮箱的 UNSEEN 邮件，从**主题和正文**
（`text/plain` + `text/html` 两种 MIME 都扫）里正则提取 URL，每个 URL
都走 `knowledge.ingest.ingest_url()`——和知识页顶部粘贴 URL（
`POST /api/knowledge/add`）用的是**同一条入库管线**（YouTube 视频 or
文章，判定逻辑见 `knowledge/ingest.py`）。

和 `podcast_check.py`/`morning_pregen.py` 不同：这个脚本**不是**对运行
中的服务器发 HTTP 请求（没有对应的 `/api/knowledge/mail-check` 接口），
而是直接 `import database` + `database.init_db()` 后在本进程内完成——
IMAP 轮询和入库都不需要经过网络往返到自己的服务器。SQLite 默认允许多
进程并发读写，和同时运行的 FastAPI 服务进程不冲突。脚本自带一个 PID
锁文件（`data/.knowledge_mail_check.lock`）防止 cron 叠跑（处理邮件里的
URL 可能触发文章抓取/YouTube 字幕下载/AI 翻译标题，慢的话要几十秒）。

一封邮件里的多个 URL 全部处理；**处理成功的邮件才标记已读**——只要有一
个 URL 失败，整封邮件保持 UNSEEN，下一轮自动重试（`ingest_url()` 对已
入库的 URL 会走 `already_exists` 分支，重试部分成功的邮件是安全的，不
会重复造行）。

**安全（#655 的重点）：** `KNOWLEDGE_MAIL_ALLOWED_SENDERS` 是必须项——
这是唯一挡住"任何知道这个邮箱地址的人都能让服务器抓取任意 URL 并触发
付费 AI 调用"的防线。**未配置时脚本直接跳过整个邮箱检查，不建立 IMAP
连接、不读取任何邮件**；已配置时，发件人比对兼容 `Name <addr@x.de>`
这种带显示名的 `From` 头格式，只比较邮箱地址部分，且不区分大小写。不在
白名单里的发件人邮件会被单独跳过（保持 UNSEEN，之后每轮都会再看到、
再跳过，没有副作用）。

凭据（IMAP 主机/端口/账号/密码）只从环境变量读取，绝不写入代码或数据库。

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `KNOWLEDGE_IMAP_HOST` | 无 | IMAP 服务器地址 |
| `KNOWLEDGE_IMAP_PORT` | `993` | IMAP 端口（SSL） |
| `KNOWLEDGE_IMAP_USER` | 无 | 专用邮箱账号 |
| `KNOWLEDGE_IMAP_PASSWORD` | 无 | 专用邮箱密码（Gmail/Outlook 等一般需要"应用专用密码"，不是登录密码） |
| `KNOWLEDGE_MAIL_ALLOWED_SENDERS` | 无（**必须配置**） | 逗号分隔的白名单发件人邮箱地址；留空则整个邮箱检查被跳过 |
| `DB_PATH` | `data/srs.db` | 数据库路径 |

### 用法

```bash
python scripts/knowledge_mail_check.py
```

### 服务器 cron：每 5 分钟检查一次

```cron
*/5 * * * * cd /opt/biangbiangmian3000 && /usr/bin/python3 scripts/knowledge_mail_check.py >> data/knowledge-mail-check.log 2>&1
```

退出码：跳过（未配置白名单/凭据）或全部成功为 `0`；有失败邮件时为 `1`
（方便 cron 邮件通知/监控接入）。

---

## signal_check.py（issue #749）

知识库 Signal 分享入口：手机把链接分享到 Signal 自己的「Note to Self」
（给自己的备忘），服务器用**已经关联好的同一个 signal-cli 设备**（见上
面"Signal 通知一次性安装/关联设置"，#521 建立的那份关联——不需要再关联
一次）把消息收下来，从正文里提取 URL，逐个走
`knowledge.ingest.ingest_url()`——和知识页粘贴框、`knowledge_mail_check.py`
用的是**同一条入库管线**。新入库的链接接着同步跑一遍转录+摘要
（`podcast.retry_episode()`），完成后把结果发回 Note to Self。

**这是"发通知"的反方向**：`send_signal()`（#521）是服务器 → Daniel，这
个脚本是 Daniel → 服务器，走的是同一个关联设备、同一个 `SIGNAL_ACCOUNT`。

**和 IMAP 邮箱收件的关键差异**：`imaplib` 可以把处理失败的邮件留成
UNSEEN，下一轮重新看到；但 `signal-cli receive` 一次调用就会把消息从
Signal 服务器上永久取走，没有"留着不读"这个选项。所以入库失败的 URL
由 `knowledge/signal_inbox.py` 自己存进一个 JSON 重试队列
（`app_settings['signal_retry_queue']`），下一轮优先处理，最多重试 3 次
后放弃，并在回执里告诉 Daniel。

**安全**：只有源账号和目的账号都是 `SIGNAL_ACCOUNT` 自己的消息（Note to
Self）才会被当作入库输入——signal-cli 作为关联设备会同步收到 Daniel 手
机发出的**所有**消息，包括发给别人的；发给别人的消息、以及任何来自其他
号码的消息一律忽略。这条防线和 `KNOWLEDGE_MAIL_ALLOWED_SENDERS` 对邮件
入口的作用相同：挡住"服务器替任何人抓取 URL 并触发付费 AI 调用"。

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SIGNAL_ACCOUNT` | 无（**必须配置**） | 关联设备所属的 Signal 号码；未配置时整个检查被跳过 |
| `SIGNAL_CLI_PATH` | `signal-cli` | 可执行文件路径 |
| `DB_PATH` | `data/srs.db` | 数据库路径 |

### 用法

```bash
python scripts/signal_check.py
```

### 服务器 cron：每 5 分钟检查一次

```cron
*/5 * * * * cd /opt/biangbiangmian3000 && /usr/bin/python3 scripts/signal_check.py >> data/signal-check.log 2>&1
```

退出码：跳过（未配置账号）或全部成功为 `0`；`signal-cli receive` 失败或
有 URL 最终放弃时为 `1`。

---

## Instagram Reel 摄取（issue #750）：yt-dlp + cookies + Groq

知识库现在能吃 Instagram Reel/Post 链接（`knowledge/instagram.py`），走和
YouTube/文章一样的入口——粘贴框、`signal_check.py`、
`knowledge_mail_check.py` 都自动支持，不需要额外配置这三个入口本身。要让
转录真正跑起来，服务器上需要一次性配置以下三样。

### 1. 安装 yt-dlp（需要 nightly 版）

Instagram 的页面结构变化快，稳定版 yt-dlp 经常滞后几周才修复对应的解析
逻辑；nightly 构建修复更快。系统级命令行工具，**不进 `requirements.txt`**
（和 `ffmpeg` 一个处理方式）：

```bash
# 装最新 nightly（pip 方式，跨发行版通用）
python3 -m pip install --upgrade --pre "yt-dlp[default]"

# 验证
yt-dlp --version
```

非默认路径（例如装进虚拟环境）时设置 `YT_DLP_PATH` 指向可执行文件。

### 2. 导出 Instagram cookies.txt

公开 Reel 有时不需要登录也能下载，但大多数情况下（尤其是被限流/需要登录
才能看的账号）需要一份登录态的 cookies：

1. 电脑浏览器登录 Instagram（用一个愿意专门给这个用的账号，不要用 Daniel
   的主账号——cookies 会存在服务器上）
2. 装一个"导出 cookies.txt"的浏览器扩展（Chrome/Firefox 商店搜
   "Get cookies.txt LOCALLY"之类，选支持 Netscape 格式导出的）
3. 在 instagram.com 页面上用扩展导出，文件存到服务器
   `data/instagram_cookies.txt`（或任意路径，配 `INSTAGRAM_COOKIES_FILE`
   指过去）

Cookies **会过期**（登录态失效/被 Instagram 判定异常）。过期后
`knowledge/instagram.py` 的失败信息会明确提示"可能是 cookies 过期"，
Daniel 会在 Signal 回执里读到——看到这条提示就重新导出一份替换旧文件即可，
不需要重启服务。

### 3. Groq API key（转录主力，可选）

Instagram Reel 转录优先用 Groq 的 `whisper-large-v3-turbo`（比 OpenAI
Whisper 便宜约 9 倍、快约 10 倍）：

1. https://console.groq.com 注册账号，创建 API key
2. 服务器 `.env` 加一行：`GROQ_API_KEY=gsk_xxxxxxxx`

`GROQ_API_KEY` **是可选的**——未配置时自动回退到已有的 OpenAI
`whisper-1`（服务器上已配好 `OPENAI_API_KEY`），只是单价贵约 9 倍（一条
60 秒 Reel 约 $0.006，仍然便宜到可以忽略）。两条转录路径都过同一道
幻觉过滤（`podcast._filter_whisper_hallucinations`）——Reel 常常是纯音乐
无人声，过滤器负责把 Whisper 对着音乐编出来的假文本挡掉，不管实际是哪家
转录的。

### 环境变量小结

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GROQ_API_KEY` | 无（可选） | 未配置时自动回退 OpenAI `whisper-1`，只是贵约 9 倍 |
| `INSTAGRAM_COOKIES_FILE` | `data/instagram_cookies.txt` | Instagram cookies.txt 路径；文件不存在时公开 Reel 仍会尝试（不一定成功） |
| `YT_DLP_PATH` | `yt-dlp` | yt-dlp 可执行文件路径，装在非默认位置时指过去 |

---

## audio_worker.py（issue #1053，总议题 #1047）

听读模式（#1047）的本地转录 worker。`audio/asr_local.py` 用 whisper.cpp
在服务器自己的 CPU 上转录音频，免费但慢——`large-v3` 在这台 4 核机器上
大约**比实时慢 1–3 倍**（一小时音频跑一到三小时，三个核全满）。

所以它绝不在 HTTP 请求里同步发生：需要本地转录的请求往 `audio_jobs` 表
排一条，这个脚本由 cron 每 5 分钟触发一次，**只在 Daniel 不用服务器的
时候**取一条来跑。四道闸门任何一道不过就立刻退出：没有排队任务 / 30 分钟
内有过人的操作 / 落在早晨预生成窗口（05:30–09:30，服务器本地时间）/
上一轮还没跑完（PID 锁——8 GB 内存放不下两个 whisper）。

跑起来之后仍然边跑边看活动时间戳：他一回来就 SIGTERM 掉 whisper.cpp，
任务标回 `pending`，下一轮从头再来。**被打断记 pending 不记 error**——
转录是幂等的，重来不花钱；记成 error 的话，他每次坐到电脑前都会把当时
正在跑的那条永久判死。

whisper.cpp 全程带 `nice -n 19` + `ionice -c 3`（`ionice` 是 Linux 独有，
macOS 上自动省略），并且只用 3 个线程，给应用留一个核。

### 一次性安装（服务器）

```bash
# 1. 编译 whisper.cpp（需要 build-essential 和 cmake）
sudo apt install -y build-essential cmake
git clone https://github.com/ggml-org/whisper.cpp /opt/whisper.cpp
cd /opt/whisper.cpp && cmake -B build && cmake --build build -j --config Release

# 2. 下载量化模型（q5_0 精度接近全精度，磁盘和内存占用小得多，约 1.1 GB）
bash /opt/whisper.cpp/models/download-ggml-model.sh large-v3-q5_0

# 3. 让可执行文件能被找到（编译产物在 build/bin/ 下）
sudo ln -sf /opt/whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli

# 4. 验证
whisper-cli -h | head -3
```

### cron

```bash
# 每 5 分钟看一眼有没有可以跑的本地转录（自己会判断该不该跑）
*/5 * * * * cd /home/anki/biangbiangmian3000 && /home/anki/biangbiangmian3000/.venv/bin/python scripts/audio_worker.py >> /home/anki/logs/audio_worker.log 2>&1
```

### 环境变量小结

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `WHISPER_CPP_PATH` | `whisper-cli` | 可执行文件路径；找不到时本地转录路径抛出可读错误，不影响其它三条对齐路径 |
| `WHISPER_CPP_MODEL` | `/opt/whisper.cpp/models/ggml-large-v3-q5_0.bin` | 模型文件路径 |
