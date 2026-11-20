#!/bin/bash
# astock 服务器自动备份
# 备份内容：监控配置/日志/报表/恢复码、代理配置、服务配置
# 位置：/opt/media/backup（Samba 和文件管理可见，方便下载）

set -u
BACKUP_DIR="/opt/media/backup"
DATE="$(date +%Y%m%d_%H%M)"
OUT="$BACKUP_DIR/astock_backup_$DATE.tar.gz"

mkdir -p "$BACKUP_DIR"

tar -czf "$OUT" --exclude='*.apk' \
  -C / \
  opt/astock/config.json \
  opt/astock/logs \
  opt/astock/reports \
  opt/astock/.secrets \
  opt/astock/web \
  opt/astock/main.py \
  opt/astock/webapp.py \
  opt/astock/tg_bot.py \
  opt/astock/check_traffic.py \
  opt/astock/daily_traffic.py \
  opt/astock/update_sub.py \
  opt/astock/sync_kuma.py \
  opt/mihomo/config.yaml \
  home/astock/.dsh \
  etc/samba/smb.conf \
  etc/minidlna.conf \
  etc/aria2/aria2.conf \
  etc/systemd/system/astock-monitor.service \
  etc/systemd/system/astock-web.service \
  etc/systemd/system/reports-web.service \
  etc/systemd/system/tg-bot.service \
  etc/systemd/system/aria2.service \
  etc/systemd/system/mihomo.service \
  etc/systemd/system/qbittorrent.service \
  etc/systemd/system/traffic-check.service \
  etc/systemd/system/traffic-check.timer \
  etc/systemd/system/traffic-daily.service \
  etc/systemd/system/traffic-daily.timer \
  etc/systemd/system/kuma-sync.service \
  etc/systemd/system/kuma-sync.timer \
  etc/systemd/system/dsh-web.service \
  etc/sudoers.d/astock-mihomo \
  etc/sudoers.d/dsh-web \
  2>/dev/null || true

if [ ! -s "$OUT" ]; then
  echo "backup failed: $OUT not created" >&2
  exit 1
fi

# 保留最近 60 天（每天约 28MB，两个月约 1.7GB）
find "$BACKUP_DIR" -maxdepth 1 -name 'astock_backup_*.tar.gz' -mtime +60 -delete 2>/dev/null || true

# 记忆文件独立明文快照：不依赖 tar 解包，Samba 里直接可见
# 注意：这里只增不删主文件，只按 60 天清理历史快照副本
MEM_SNAP="$BACKUP_DIR/memory"
mkdir -p "$MEM_SNAP"
[ -f /home/astock/.dsh/MEMORY.md ] && cp -f /home/astock/.dsh/MEMORY.md "$MEM_SNAP/MEMORY-$(date +%Y%m%d).md"
[ -f /home/astock/.dsh/AGENTS.md ] && cp -f /home/astock/.dsh/AGENTS.md "$MEM_SNAP/AGENTS-$(date +%Y%m%d).md"
find "$MEM_SNAP" -maxdepth 1 -name 'MEMORY-*.md' -mtime +60 -delete 2>/dev/null || true
find "$MEM_SNAP" -maxdepth 1 -name 'AGENTS-*.md' -mtime +60 -delete 2>/dev/null || true

# 归档文件（MEMORY-ARCHIVE.md）：内容变化时才复制，保留 180 天
ARCH_SRC=/home/astock/.dsh/MEMORY-ARCHIVE.md
if [ -f "$ARCH_SRC" ]; then
  NEWH=$(md5sum "$ARCH_SRC" | cut -d' ' -f1)
  LASTF=$(ls -t "$MEM_SNAP"/MEMORY-ARCHIVE-*.md 2>/dev/null | head -1)
  if [ -z "$LASTF" ] || [ "$NEWH" != "$(md5sum "$LASTF" | cut -d' ' -f1)" ]; then
    cp -f "$ARCH_SRC" "$MEM_SNAP/MEMORY-ARCHIVE-$(date +%Y%m%d).md"
    echo "archived: MEMORY-ARCHIVE-$(date +%Y%m%d).md"
  fi
fi
find "$MEM_SNAP" -maxdepth 1 -name 'MEMORY-ARCHIVE-*.md' -mtime +180 -delete 2>/dev/null || true


echo "backup done: $OUT ($(du -h "$OUT" 2>/dev/null | cut -f1))"
