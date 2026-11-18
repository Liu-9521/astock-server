#!/bin/bash
# 把 AList 的 astock 用户密码设为 <QUANT_WEB_PASSWORD>
# （这个 AList 里只有一个用户，用户名就是 astock，它同时是管理员和 rclone 使用的账号）
# 用法：sudo bash ~/.dsh/dsh_wechat/fix_alist_password.sh
set -u
NEW_PW="<QUANT_WEB_PASSWORD>"
ALIST_DIR="/opt/alist"

echo "=== 1. 停止 alist（改密码前先停，避免数据库被占用） ==="
systemctl stop alist
sleep 1

echo
echo "=== 2. 把 astock 的密码设为 ${NEW_PW} ==="
cd "$ALIST_DIR" || exit 1
./alist admin set "$NEW_PW" 2>&1 | grep -v "reading config file"

echo
echo "=== 3. 启动 alist ==="
systemctl start alist
sleep 4
echo -n "服务状态："
systemctl is-active alist

echo
echo "=== 4. 验证登录接口（astock / ${NEW_PW}） ==="
CODE=$(curl -s -o /tmp/alist_login_check.json -w "%{http_code}" -X POST \
  http://127.0.0.1:5244/api/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"astock\",\"password\":\"${NEW_PW}\"}")
echo "登录 HTTP $CODE"
head -c 160 /tmp/alist_login_check.json 2>/dev/null | tr -d '\n'
echo
rm -f /tmp/alist_login_check.json

echo
echo "=== 5. 同步更新 rclone 凭据（它用的正是这个账号） ==="
sudo -u astock rclone config password alist pass "$NEW_PW" 2>&1 | tail -1

echo
echo "=== 6. 验证云盘连通 ==="
sudo -u astock rclone lsd "alist:/夸克/" 2>&1 | head -4

echo
echo "=== 7. 立即跑一次云同步，确认备份链路没断 ==="
sudo -u astock bash /opt/astock/sync_backup_cloud.sh 2>&1 | tail -8

echo
echo "======================================================"
echo " 完成：账号 astock　密码 ${NEW_PW}"
echo " 网页登录 http://192.168.0.104:5244"
echo " rclone 配置已同步更新"
echo "======================================================"
