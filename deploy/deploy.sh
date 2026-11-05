#!/bin/bash
# 自动部署脚本 —— 无需 GitHub secrets/webhook，靠 cron 轮询实现“推送即上线”。
#
# 用法：cron 行由 deploy/crontab.managed 管理，每分钟运行一次（#1454）；
# 不要再手工 `crontab -e`。首次安装仍需在服务器上手工运行一次 `bash deploy/install_cron.sh`，
# 之后每轮 deploy.sh 都会自己同步。
#
# 行为：
#   1. git fetch 获取 origin/main 最新提交
#   2. 若本地 HEAD 已经等于 origin/main，什么都不做（幂等）
#   3. 否则：git pull → 用 .venv 安装依赖 → 重启 systemd 服务
#
# 用 flock 防止上一次部署还没跑完时 cron 又并发触发一次。

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="/tmp/biangbiangmian3000-deploy.lock"

exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 上一次部署仍在进行中，跳过本次运行。"
    exit 0
fi

cd "$REPO_DIR"

# 每轮同步一次仓库管理的 cron 行（#1279）；没变化时不写也不输出。
# 放在"已是最新"判断之前：本次 PR 合并后的第一轮就会生效，不必等下一次提交。
# 失败只记日志，不挡部署。
bash "$REPO_DIR/deploy/install_cron.sh" || echo "[$(date '+%Y-%m-%d %H:%M:%S')] install_cron 失败（见上），继续部署。"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 检查更新..."
git fetch origin main --quiet

LOCAL_REV="$(git rev-parse HEAD)"
REMOTE_REV="$(git rev-parse origin/main)"

if [ "$LOCAL_REV" = "$REMOTE_REV" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 已是最新（$LOCAL_REV），无需部署。"
    exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 发现新提交：$LOCAL_REV -> $REMOTE_REV"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 拉取最新代码..."
git pull --ff-only origin main

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 安装/更新依赖..."
.venv/bin/pip install -q -r requirements.txt

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 重启服务..."
sudo systemctl restart biangbiangmian3000

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 部署完成，当前版本：$(git rev-parse HEAD)"
