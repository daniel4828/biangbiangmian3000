# 部署指南 —— Hetzner VPS（Ubuntu 24.04）

本文档假设你从一台全新的 Hetzner VPS（Ubuntu 24.04）开始，一步步把 biangbiangmian3000
部署到线上，最终可以用手机浏览器通过 HTTPS 访问。任何人（或任何 AI 代理）照着做即可上线，
不需要额外背景知识。

---

## 1. 服务器初始化

用 root 登录服务器（`ssh root@<服务器IP>`），然后：

```bash
apt update && apt upgrade -y
```

### 创建非 root 用户

不要用 root 长期运行服务。创建一个专用用户（这里叫 `anki`，你可以改名，但要和后面
所有配置文件中的 `anki` 保持一致）：

```bash
adduser anki
usermod -aG sudo anki
```

设置好密码后，把 SSH 公钥加入新用户（如果你已经用密钥登录 root，可以复制过去）：

```bash
rsync --archive --chown=anki:anki ~/.ssh /home/anki
```

之后用 `ssh anki@<服务器IP>` 登录，后续步骤都在 `anki` 用户下进行。

### 防火墙（ufw）

只开放 SSH（22）、HTTP（80）、HTTPS（443）：

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

---

## 2. 安装依赖软件

### Python

```bash
sudo apt install -y python3 python3-venv python3-pip lsof git sqlite3 ffmpeg
```

（`lsof` 供 `run.sh` 清理 8000 端口使用；`sqlite3` CLI 供备份 `.backup` 命令使用；
`ffmpeg` 供播客 Whisper 音频转录回退使用——issue #485，`@shengfm` 频道无字幕时
把下载的音频转码/分段后送 OpenAI 转录；缺失时该功能只记警告并跳过，不影响
其余功能。）

### Caddy（反向代理 + 自动 HTTPS）

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | \
    sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | \
    sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
```

---

## 3. 获取代码

```bash
cd /home/anki
git clone https://github.com/daniel4828/biangbiangmian3000.git
cd biangbiangmian3000
```

（如果仓库是私有的，用 `git clone git@github.com:daniel4828/biangbiangmian3000.git`，
并提前在服务器上配置好部署用的 SSH key 或 GitHub Personal Access Token。）

### 创建虚拟环境并安装依赖

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

---

## 4. 配置 `.env`

在项目根目录创建 `.env` 文件（**不要提交到 git**）：

```bash
nano .env
```

内容（把下面所有变量抄进去，按需填写；见 CLAUDE.md「启动与环境变量」表）：

```bash
# ===== AI 提供商密钥 =====
ANTHROPIC_API_KEY=       # 必填。Claude API 密钥
DEEPSEEK_API_KEY=        # 可选。DeepSeek API 密钥
ZHIPU_API_KEY=           # 可选。ZhipuAI GLM 密钥
QWEN_API_KEY=            # 可选。阿里云 Qwen 密钥
OPENAI_API_KEY=          # 可选。OpenAI 密钥；新闻模式默认用它（gpt-5-mini），
                         # 因为 DeepSeek 会审查新闻内容

# ===== 数据库与运行参数 =====
DB_PATH=data/srs.db      # 数据库路径。生产环境务必用 srs.db，不要用 dev.db
DISABLE_AI=0             # 设为 1 可跳过 AI 故事生成（调试用，生产环境应为 0）
LOG_LEVEL=INFO           # 日志级别，调试时可设为 DEBUG
DEV_CLEAR_DB=            # 留空。设为任意值会在启动时清空数据库 —— 生产环境绝不要设置

# ===== 访问认证（即将加入，见相关 issue）=====
# 用于给整个站点加一层 HTTP Basic Auth，防止手机浏览器直接访问时被陌生人发现。
# 目前 main.py 尚未读取这两个变量，加入后请提前写好，方便功能上线即可生效。
AUTH_USERNAME=           # HTTP Basic Auth 用户名
AUTH_PASSWORD=           # HTTP Basic Auth 密码
```

保存后确认权限收紧：

```bash
chmod 600 .env
```

### 初始化数据库并导入词汇

```bash
.venv/bin/python main.py import
```

---

## 5. 配置 systemd 服务

复制 service 文件并按需修改（如果用户名/路径与示例不同，需要先编辑 `deploy/biangbiangmian3000.service`）：

```bash
sudo cp deploy/biangbiangmian3000.service /etc/systemd/system/biangbiangmian3000.service
sudo systemctl daemon-reload
sudo systemctl enable biangbiangmian3000
sudo systemctl start biangbiangmian3000
sudo systemctl status biangbiangmian3000
```

查看日志：

```bash
journalctl -u biangbiangmian3000 -f
```

---

## 6. 配置 Caddy 反向代理

```bash
sudo cp deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile   # 把 your-domain.example.com 换成你自己的域名
sudo systemctl restart caddy
sudo systemctl enable caddy
```

确认域名的 DNS A 记录已指向服务器 IP。Caddy 会自动申请 Let's Encrypt 证书，
首次访问 `https://your-domain.example.com` 时可能需要等待几十秒完成证书签发。

### 可选：同一台服务器托管个人主页（子域名）

DuckDNS 支持通配符解析——`*.你的域名.duckdns.org` 全部自动指向同一个 IP，
不需要注册新域名。仓库 `homepage/` 目录里的静态主页这样上线：

```caddyfile
# 追加到 /etc/caddy/Caddyfile（完整示例见 deploy/Caddyfile.example）
home.your-domain.duckdns.org {
	root * /home/anki/biangbiangmian3000/homepage
	file_server
}
```

```bash
sudo systemctl reload caddy
```

主页不经过 FastAPI，因此**没有 Basic Auth，公开可见**。之后修改主页走正常
PR 流程即可——自动部署会连同主页一起拉取更新，详见 `homepage/README.md`。

---

## 7. 配置自动部署（cron + deploy.sh）

`deploy/deploy.sh` 会定期检查 `origin/main` 是否有新提交，有的话自动拉取、装依赖、重启服务。
不需要 GitHub Actions/webhook，只需要服务器主动轮询。

```bash
crontab -e
```

添加一行（每 2 分钟检查一次）：

```
*/2 * * * * /home/anki/biangbiangmian3000/deploy/deploy.sh >> /home/anki/deploy.log 2>&1
```

`deploy.sh` 内部用 `flock` 防止并发运行；如果两分钟内上一次部署还没跑完，会自动跳过。

要让 `deploy.sh` 里的 `sudo systemctl restart biangbiangmian3000` 免密码执行，
给 `anki` 用户加一条 sudoers 规则（用 `sudo visudo -f /etc/sudoers.d/biangbiangmian3000`）：

```
anki ALL=(ALL) NOPASSWD: /bin/systemctl restart biangbiangmian3000
```

查看部署日志：

```bash
tail -f /home/anki/deploy.log
```

---

## 8. 数据库备份（#1279：脚本入库，cron 由部署自动安装）

**不要再手工 `crontab -e` 加备份行。** 备份由仓库里的 `scripts/backup_db.sh` 完成，它的 cron 行写在
`deploy/crontab.managed`，`deploy/deploy.sh` 每轮调用 `deploy/install_cron.sh` 把这块同步进 `anki` 的
crontab（两行标记之间的块，幂等；其余 cron 行不动；顺带删掉旧的手工 `sqlite3 … .backup …` 行）。

- 每 6 小时：`sqlite3 .backup` → `PRAGMA quick_check` → `zstd -3` 压缩（约 160 MB → 40 MB）→ `data/backups/srs_<时间>.db.zst`
- 然后跑 `scripts/backup_retention.py --apply --compress`：最近 2 天全留；14 天内每天留最新一份；60 天内每 ISO 周留最新一份；更早的每个日历月永久留最新一份。
  只认 `srs_YYYY-MM-DD_HH-MM-SS.db[.zst]`，手工的一次性备份（`pre-728-*.db` 等）一律不碰
- 日志：`data/backup.log`
- 先看计划不删：`python3 scripts/backup_retention.py --dir data/backups`（默认 dry-run）
- 恢复：`zstd -d data/backups/srs_<时间>.db.zst -o /tmp/restore.db`，检查后停服务再替换 `data/srs.db`（同时删掉旧的 `-wal`/`-shm`）
- 需要 `apt install zstd`（没有时脚本照常备份，只是不压缩）

**强烈建议做异地备份**：定期把服务器上的备份 `rsync` 回本地 Mac，防止服务器本身出问题导致数据全部丢失：

```bash
# 在本地 Mac 上运行（可以加进 backup.sh 或单独写一个 cron）
rsync -avz anki@<服务器IP>:/home/anki/biangbiangmian3000/data/backups/ ~/Documents/biangbiangmian3000/data/remote_backups/
```

---

## 9. 验证部署

1. 浏览器访问 `https://your-domain.example.com`，确认页面加载。
2. `curl -s https://your-domain.example.com/api/decks` 应返回 200 和牌组 JSON。
3. 修改本地代码，`git push` 到 `main` 后，最多等 2 分钟，`tail -f /home/anki/deploy.log`
   应能看到自动部署日志，且网站已更新（可以先在无关文件里加一行注释验证流程通不通）。

---

## 故障排查

| 现象 | 排查方向 |
|------|---------|
| 服务启动失败 | `journalctl -u biangbiangmian3000 -e`，检查 `.env` 路径/权限、`.venv` 是否存在 |
| Caddy 无法签发证书 | 确认 80/443 端口已在 ufw 放行，且域名 DNS 已生效 |
| deploy.sh 报 sudo 密码错误 | 检查第 7 节的 sudoers NOPASSWD 配置 |
| 8000 端口被占用 | `run.sh` 会自动 `lsof -ti :8000 | xargs kill -9`，确认服务器已安装 `lsof` |
