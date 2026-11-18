#!/bin/bash
# 本地大备份：从每天一次改为每 10 天一次（每月 1/11/21 日 03:00）
# 并把现有堆积的旧包清理到只留最近 6 个（约 3.2 GB）
# 用法：sudo bash ~/.dsh/dsh_wechat/backup_reduce.sh
set -u
TIMER=/etc/systemd/system/astock-backup.timer
STAMP=$(date +%Y%m%d-%H%M%S)
KEEP=6

echo "=== 0. 备份 timer 文件 ==="
cp -a "$TIMER" "$TIMER.bak-$STAMP"
echo "  $TIMER.bak-$STAMP"

echo
echo "=== 1. 改触发周期为每月 1/11/21 日 03:00 ==="
sed -i 's|^OnCalendar=.*|OnCalendar=*-*-1,11,21 03:00:00|' "$TIMER"
sed -i 's|^Description=.*|Description=astock backup every 10 days at 03:00|' "$TIMER"
echo "  当前内容："
grep -E "Description|OnCalendar|Persistent" "$TIMER" | sed 's/^/    /'

echo
echo "=== 2. 重新加载并重启 timer ==="
systemctl daemon-reload
systemctl restart astock-backup.timer
systemctl list-timers astock-backup.timer --no-pager | head -3

echo
echo "=== 3. 清理旧包，只留最近 $KEEP 个 ==="
cd /opt/media/backup || exit 1
BEFORE=$(ls -1 astock_backup_*.tar.gz 2>/dev/null | wc -l)
ls -1t astock_backup_*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do
  echo "  删除 $f"
  rm -f "$f"
done
AFTER=$(ls -1 astock_backup_*.tar.gz 2>/dev/null | wc -l)
echo "  包数量：$BEFORE -> $AFTER"

echo
echo "=== 4. 现在的占用 ==="
du -sh /opt/media/backup
echo "  保留的包："
ls -1lh astock_backup_*.tar.gz 2>/dev/null | awk '{print "    "$5"  "$6" "$7" "$8"  "$9}'

echo
echo "=== 5. 磁盘剩余 ==="
df -h / | tail -1

echo
echo "======================================================"
echo " 完成：本地大备份改为每月 1/11/21 日 03:00（约 10 天一次）"
echo " 保留策略：backup.sh 原有 -mtime +60，10 天一次即约 6 个包 ≈ 3.2 GB"
echo " 回滚：sudo cp -a $TIMER.bak-$STAMP $TIMER && sudo systemctl daemon-reload && sudo systemctl restart astock-backup.timer"
echo "======================================================"
