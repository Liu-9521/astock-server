#!/bin/bash
# 云备份：只同步"小而关键"的部分  （SKIP_BIG 标记：大 tar 包不同步云端）
# /opt/media/backup 里的 astock_backup_*.tar.gz 每天约 556MB、60 天 33GB，云端扛不住
# 云端只存：① memory 明文快照 ② extra 小包（约 20K/天）③ 云端保留 14 天
set -u
LOG="/opt/astock/sync_backup.log"
say() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

say "===== 云同步开始 ====="

if ! rclone lsd "alist:/夸克/" >/dev/null 2>&1; then
  say "alist 不可达，跳过（systemctl status alist）"
  exit 1
fi

say "同步 memory 快照"
rclone sync /opt/media/backup/memory/ "alist:/夸克/astock-backup/memory/" >>"$LOG" 2>&1
say "memory 结果: $?"

say "同步 extra 小备份"
rclone sync /opt/media/backup-extra/ "alist:/夸克/astock-backup/extra/" >>"$LOG" 2>&1
say "extra 结果: $?"

say "清理云端 14 天前的 extra 包"
rclone delete "alist:/夸克/astock-backup/extra/" --min-age 14d --include "*.tar.gz" >>"$LOG" 2>&1
say "清理结果: $?"

say "同步密钥文件（600 权限，仅本人网盘）"
rclone sync /opt/media/backup-secrets/ "alist:/夸克/astock-backup/secrets/" >>"$LOG" 2>&1
say "secrets 结果: $?"
rclone delete "alist:/夸克/astock-backup/secrets/" --min-age 30d --include "*.json" >>"$LOG" 2>&1

say "===== 云同步结束 ====="
