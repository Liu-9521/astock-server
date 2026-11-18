#!/bin/bash
# 每日把服务器代码导出并提交到 git 仓库（有 remote 才 push）
DSH="/home/astock/.dsh/dsh_wechat"
REPO="/home/astock/astock-repo"
LOG="$DSH/git_sync.log"
ts() { date '+%Y-%m-%d %H:%M:%S'; }
echo "=== $(ts) 开始同步 ===" >> "$LOG"
bash "$DSH/export_repo.sh" >> "$LOG" 2>&1
cd "$REPO" || { echo "$(ts) 仓库目录不存在" >> "$LOG"; exit 1; }
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -q -m "auto: $(date '+%Y-%m-%d %H:%M') 自动同步" >> "$LOG" 2>&1
  echo "$(ts) 已提交" >> "$LOG"
  if git remote get-url origin >/dev/null 2>&1; then
    if git push origin main >> "$LOG" 2>&1; then
      echo "$(ts) 已推送到 GitHub" >> "$LOG"
    else
      echo "$(ts) 推送失败（检查公钥或网络）" >> "$LOG"
    fi
  else
    echo "$(ts) 尚未配置 remote，仅本地提交" >> "$LOG"
  fi
else
  echo "$(ts) 无变化" >> "$LOG"
fi
# 日志保留最近 200 行
tail -200 "$LOG" > "$LOG.tmp" && mv -f "$LOG.tmp" "$LOG"
