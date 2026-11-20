#!/bin/bash
# 记忆文件行数守护：MEMORY.md 超过阈值时写 TODO 并推企微（每天最多一次）
set -u
MD=/home/astock/.dsh/MEMORY.md
ARCH=/home/astock/.dsh/MEMORY-ARCHIVE.md
TODO=/home/astock/.dsh/MEMORY-TODO.md
THRESH=150
LOG=/home/astock/.dsh/dsh_wechat/memory_guard.log
say(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

[ -f "$MD" ] || { say "主文件不存在，跳过"; exit 0; }

LINES=$(wc -l < "$MD")
ALINES=$(wc -l < "$ARCH" 2>/dev/null || echo 0)

if [ "$LINES" -le "$THRESH" ]; then
  if [ -f "$TODO" ]; then
    rm -f "$TODO"
    say "已降到 ${LINES} 行（阈值 ${THRESH}），清除 TODO"
  fi
  exit 0
fi

cat > "$TODO" <<EOF
# 记忆文件需要精简（生成于 $(date '+%F %T')）

- 主文件 \`MEMORY.md\`：**${LINES} 行**（阈值 ${THRESH}）
- 归档 \`MEMORY-ARCHIVE.md\`：${ALINES} 行

## 处理方式
1. 把「已完成且不再改动」的章节剪切到 \`MEMORY-ARCHIVE.md\` 末尾；
2. 在 \`MEMORY.md\` 第八节的索引表补一行（主题 + 行号区间/关键词）；
3. 确认主文件回到 ${THRESH} 行以内；
4. 删掉本文件（下次检查会自动清除）。
EOF
say "主文件 ${LINES} 行超阈值 ${THRESH}，已写 TODO"

STAMP="/tmp/.mem_guard_$(date +%Y%m%d)"
if [ ! -f "$STAMP" ]; then
  if python3 /opt/astock/send_wecom.py "⚠️ 记忆文件 ${LINES} 行（阈值 ${THRESH}），建议精简归档。详见 ~/.dsh/MEMORY-TODO.md" >>"$LOG" 2>&1; then
    touch "$STAMP"
    say "已推送企微提醒"
  else
    say "企微推送失败（不影响流程）"
  fi
fi
