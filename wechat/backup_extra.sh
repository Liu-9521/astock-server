#!/bin/bash
# 补充备份：root 版 backup.sh 没覆盖的业务脚本、运行时数据、crontab、新增 systemd 单元
# 产物放 /opt/media/backup-extra/，由 sync_backup_cloud.sh 一并同步到夸克网盘
set -u
OUT_DIR="/opt/media/backup-extra"
DATE="$(date +%Y%m%d_%H%M)"
OUT="$OUT_DIR/extra_backup_$DATE.tar.gz"

mkdir -p "$OUT_DIR"

cd / || exit 1
tar -czf "$OUT" \
  opt/astock/gold_push.py \
  opt/astock/media_cleanup.py \
  opt/astock/rss_push.py \
  opt/astock/qbit_autotune.py \
  opt/astock/proxy_guard.py \
  opt/astock/kuma_guard.py \
  opt/astock/media_organize.py \
  opt/astock/qbit_stale.py \
  opt/astock/gold_spot.json \
  opt/astock/gold_pending.json \
  etc/systemd/system/futures-sim.service \
  2>/dev/null || true

if [ ! -s "$OUT" ]; then
  echo "extra backup failed: $OUT not created" >&2
  exit 1
fi

# crontab 不在文件系统里，丢了最难恢复
crontab -l > "$OUT_DIR/crontab-astock-$DATE.txt" 2>/dev/null || true

# 顺手把 root 版备份的清单也留一份，便于核对漏了什么
systemctl cat astock-backup.service > "$OUT_DIR/astock-backup-unit-$DATE.txt" 2>/dev/null || true

find "$OUT_DIR" -maxdepth 1 -name 'extra_backup_*.tar.gz' -mtime +60 -delete 2>/dev/null || true
find "$OUT_DIR" -maxdepth 1 -name 'crontab-astock-*.txt' -mtime +60 -delete 2>/dev/null || true
find "$OUT_DIR" -maxdepth 1 -name 'astock-backup-unit-*.txt' -mtime +60 -delete 2>/dev/null || true

echo "extra backup done: $OUT ($(du -h "$OUT" 2>/dev/null | cut -f1))"
