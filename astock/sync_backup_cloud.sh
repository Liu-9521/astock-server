#!/bin/bash
# 把备份同步到夸克网盘（通过 alist WebDAV）
set -u
rclone sync /opt/media/backup/ "alist:/夸克/astock-backup/" \
  --transfers 2 --checkers 4 --retries 2 2>&1
# 补充备份（业务脚本、运行时数据、crontab）单独一并上传
if [ -d /opt/media/backup-extra ]; then
  rclone sync /opt/media/backup-extra/ "alist:/夸克/astock-backup/extra/" \
    --transfers 2 --checkers 4 --retries 2 2>&1
fi
echo "cloud sync done: $(date '+%Y-%m-%d %H:%M:%S')"
