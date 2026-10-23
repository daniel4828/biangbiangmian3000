#!/bin/bash
# 把 deploy/crontab.managed 同步进当前用户的 crontab（issue #1279）。
#
# deploy.sh 每轮都调用它；内容没变就什么都不写（幂等）。
# - 只管两个标记之间的那一块，其余 cron 行原样保留
# - 顺带删掉旧的手工备份行（sqlite3 … .backup … data/backups/srs_…），它被 scripts/backup_db.sh 取代
# - crontab -l 出错（不是"还没有 crontab"）时直接放弃，绝不拿空内容覆盖
#
# 用法：install_cron.sh [--dry-run]

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO_DIR/deploy/crontab.managed"
BEGIN="# >>> biangbiangmian3000 managed (deploy/install_cron.sh) >>>"
END="# <<< biangbiangmian3000 managed <<<"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

ERR="$(mktemp)"
trap 'rm -f "$ERR"' EXIT
if ! CURRENT="$(crontab -l 2>"$ERR")"; then
    if grep -qi "no crontab" "$ERR"; then
        CURRENT=""
    else
        echo "install_cron: crontab -l 失败，放弃：$(cat "$ERR")" >&2
        exit 1
    fi
fi

KEPT="$(printf '%s\n' "$CURRENT" \
    | awk -v b="$BEGIN" -v e="$END" '$0==b{skip=1;next} $0==e{skip=0;next} !skip' \
    | grep -vE 'sqlite3 .*\.backup .*data/backups/srs_' || true)"
# 去掉末尾空行，免得每轮多出一行而永远"不一样"
KEPT="$(printf '%s\n' "$KEPT" | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}')"

BLOCK="$(sed "s#@REPO_DIR@#${REPO_DIR}#g" "$TEMPLATE")"
if [ -n "$KEPT" ]; then
    NEW="$(printf '%s\n%s\n%s\n%s' "$KEPT" "$BEGIN" "$BLOCK" "$END")"
else
    NEW="$(printf '%s\n%s\n%s' "$BEGIN" "$BLOCK" "$END")"
fi

if [ "$NEW" = "$CURRENT" ]; then
    exit 0
fi

if [ "$DRY_RUN" = 1 ]; then
    echo "install_cron（dry-run）：将写入以下 crontab："
    printf '%s\n' "$NEW"
    exit 0
fi

printf '%s\n' "$NEW" | crontab -
echo "[$(date '+%Y-%m-%d %H:%M:%S')] install_cron: crontab 已更新（managed 块来自 deploy/crontab.managed）"
