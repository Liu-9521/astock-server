#!/bin/bash
# 月度大备份：每月 1 号把最新的全量包传到夸克，云端保留 2 个月（60 天）
# 正常由 crontab 触发；手动测试可加 --dry-run
set -u
REMOTE="alist:/夸克/astock-backup/monthly"
SRC="/opt/media/backup"
LOG="/opt/astock/sync_monthly.log"
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="--dry-run"

say() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

LATEST=$(ls -t "$SRC"/astock_backup_*.tar.gz 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
  say "没有找到全量包，跳过"
  exit 0
fi
SIZE=$(du -h "$LATEST" | cut -f1)
say "开始上传 $LATEST ($SIZE)"
if rclone copy "$LATEST" "$REMOTE/" --bwlimit 4M $DRY 2>>"$LOG"; then
  say "上传完成 $LATEST"
else
  say "上传失败 $LATEST"
  exit 1
fi

# 只保留最近 2 个月（60 天）的大包
if rclone delete "$REMOTE/" --min-age 60d --include "*.tar.gz" $DRY 2>>"$LOG"; then
  say "已清理 60 天前的旧包"
fi
say "当前云端 monthly 目录："
rclone lsl "$REMOTE/" 2>&1 | tail -5 | tee -a "$LOG"
