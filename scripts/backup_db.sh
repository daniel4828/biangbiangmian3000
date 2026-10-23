#!/bin/bash
# 数据库快照 + 保留规则（issue #1279）。由 cron 每 6 小时调用，cron 行由
# deploy/install_cron.sh 从 deploy/crontab.managed 安装——不要手工 crontab -e。
#
# 1. sqlite3 .backup（在线备份，服务运行中也安全）写到一个临时名
# 2. quick_check 通过后用 zstd 压缩（约 160 MB → 40 MB），校验后才改成正式名
# 3. 跑 scripts/backup_retention.py --apply --compress 清理旧快照
#
# 恢复：zstd -d srs_<时间>.db.zst -o /tmp/restore.db，确认无误后再替换 data/srs.db。

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${BACKUP_DB:-$REPO_DIR/data/srs.db}"
BACKUP_DIR="${BACKUP_DIR:-$REPO_DIR/data/backups}"
PYTHON="${BACKUP_PYTHON:-python3}"

mkdir -p "$BACKUP_DIR"
TS="$(date +%Y-%m-%d_%H-%M-%S)"
NAME="srs_${TS}.db"
# 前缀是点、后缀 .partial：保留规则的正则对不上，中途失败也不会被当成快照
TMP="${BACKUP_DIR}/.${NAME}.partial"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

trap 'rm -f "$TMP" "${TMP}.zst"' EXIT

sqlite3 "$DB" ".backup '${TMP}'"
CHECK="$(sqlite3 "$TMP" 'PRAGMA quick_check;')"
if [ "$CHECK" != "ok" ]; then
    log "快照 quick_check 失败：${CHECK}"
    exit 1
fi

if command -v zstd >/dev/null 2>&1; then
    zstd -q -T0 -3 "$TMP" -o "${TMP}.zst"
    zstd -q -t "${TMP}.zst"
    mv "${TMP}.zst" "${BACKUP_DIR}/${NAME}.zst"
    log "快照完成：${NAME}.zst（$(du -h "${BACKUP_DIR}/${NAME}.zst" | cut -f1)）"
else
    mv "$TMP" "${BACKUP_DIR}/${NAME}"
    log "快照完成：${NAME}（没有 zstd，未压缩）"
fi

"$PYTHON" "$REPO_DIR/scripts/backup_retention.py" --dir "$BACKUP_DIR" --apply --compress
