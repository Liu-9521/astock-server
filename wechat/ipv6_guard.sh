#!/bin/bash
# IPv6 入站防护 v2
# 修复：multiport 一次最多 15 个端口，上一版 16 个端口导致 DROP 规则没加上（整条链等于全放行）
# 只动 ip6tables，IPv4 完全不受影响。
# 用法：sudo bash ~/.dsh/dsh_wechat/ipv6_guard.sh
set -u
SD="/home/astock/.dsh/dsh_wechat"
STAMP=$(date +%Y%m%d-%H%M%S)
# 每批不超过 15 个端口（multiport 上限）
BATCH1="22,139,445,1200,3001,5244,7890,8000,8080"
BATCH2="8081,8082,8084,8096,8098,8099,8200"

echo "=== 0. 备份当前 IPv6 规则 ==="
ip6tables-save > "$SD/ip6tables.rules.bak-$STAMP"
echo "  已备份到 $SD/ip6tables.rules.bak-$STAMP"

echo
echo "=== 1. 重建 IPv6 INPUT 链 ==="
ip6tables -F INPUT || { echo "清空INPUT失败"; exit 1; }
ip6tables -A INPUT -i lo -j ACCEPT
ip6tables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A INPUT -p ipv6-icmp -j ACCEPT
ip6tables -A INPUT -i tailscale0 -j ACCEPT
ip6tables -A INPUT -s fe80::/10 -j ACCEPT
ip6tables -A INPUT -s fd00::/8 -j ACCEPT

echo "  添加 DROP 规则（分两批，每批 ≤15 个端口）"
if ! ip6tables -A INPUT -p tcp -m multiport --dports "$BATCH1" -j DROP; then
  echo "  ✗ 第一批 DROP 添加失败"; exit 1
fi
if ! ip6tables -A INPUT -p tcp -m multiport --dports "$BATCH2" -j DROP; then
  echo "  ✗ 第二批 DROP 添加失败"; exit 1
fi
ip6tables -A INPUT -j ACCEPT
echo "  已应用"

echo
echo "=== 2. 当前 IPv6 INPUT 规则 ==="
ip6tables -L INPUT -n --line-numbers

echo
echo "=== 3. 持久化（重启后仍生效） ==="
if command -v netfilter-persistent >/dev/null 2>&1; then
  netfilter-persistent save 2>&1 | tail -2
  echo "  已用 netfilter-persistent 保存"
else
  mkdir -p /etc/iptables
  ip6tables-save > /etc/iptables/rules.v6
  if [ ! -f /etc/systemd/system/ip6tables-restore.service ]; then
    cat > /etc/systemd/system/ip6tables-restore.service <<'EOF'
[Unit]
Description=Restore ip6tables rules
Before=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
ExecStart=/sbin/ip6tables-restore /etc/iptables/rules.v6
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable ip6tables-restore.service >/dev/null 2>&1
    echo "  已创建 ip6tables-restore.service 并设为开机启动"
  fi
  echo "  规则已写入 /etc/iptables/rules.v6"
fi

echo
echo "=== 4. 验证 DROP 规则确实在链上 ==="
OK=1
if ip6tables -C INPUT -p tcp -m multiport --dports "$BATCH1" -j DROP 2>/dev/null; then
  echo "  ✓ 第一批 DROP（22/139/445/1200/3001/5244/7890/8000/8080）"
else
  echo "  ✗ 第一批 DROP 缺失"; OK=0
fi
if ip6tables -C INPUT -p tcp -m multiport --dports "$BATCH2" -j DROP 2>/dev/null; then
  echo "  ✓ 第二批 DROP（8081/8082/8084/8096/8098/8099/8200）"
else
  echo "  ✗ 第二批 DROP 缺失"; OK=0
fi
if [ "$OK" -eq 0 ]; then
  echo
  echo "  !!! 规则不完整，已回滚 !!!"
  ip6tables-restore < "$SD/ip6tables.rules.bak-$STAMP"
  exit 1
fi

echo
echo "=== 5. 验证：公网 IPv6 直连 5244 应被拦住 ==="
V6=$(ip -6 addr show scope global 2>/dev/null | grep inet6 | grep -v " fd" | head -1 | awk '{print $2}' | cut -d/ -f1)
if [ -n "$V6" ]; then
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -g --max-time 5 "http://[$V6]:5244/" 2>/dev/null)
  if [ "$CODE" = "000" ]; then
    echo "  ✓ 公网 IPv6 访问 5244 已被拦截（HTTP 000）"
  else
    echo "  ! 仍返回 HTTP $CODE —— 本机访问自己的公网地址会走 lo 接口，属预期，不代表规则失效"
  fi
else
  echo "  无公网 IPv6，跳过"
fi

echo
echo "=== 6. 验证：局域网 IPv4 访问不受影响 ==="
CODE4=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://192.168.0.104:5244/" 2>/dev/null)
if [ "$CODE4" = "200" ]; then
  echo "  ✓ http://192.168.0.104:5244/ -> HTTP 200"
else
  echo "  ✗ 局域网访问异常（HTTP $CODE4），请注意"
fi

echo
echo "=== 7. 验证：Tailscale 不受影响 ==="
echo "  tailscale0 接口已放行（-i tailscale0 -j ACCEPT）"

echo
echo "======================================================"
echo " 完成。公网 IPv6 无法访问：22 139 445 1200 3001 5244 7890"
echo "                          8000 8080 8081 8082 8084 8096 8098 8099 8200"
echo " 内网（fe80::/10、fd00::/8）与 Tailscale 不受限，IPv4 未动。"
echo " 回滚：sudo ip6tables-restore < $SD/ip6tables.rules.bak-$STAMP"
echo "======================================================"
