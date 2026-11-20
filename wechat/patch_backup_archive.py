#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 /opt/astock/backup.sh 加上 MEMORY-ARCHIVE.md 的明文快照（需 sudo 执行）。"""
import io, os, sys, shutil, datetime, subprocess

P = "/opt/astock/backup.sh"
ANCHOR = "find \"$MEM_SNAP\" -maxdepth 1 -name 'AGENTS-*.md' -mtime +60 -delete 2>/dev/null || true"

ADD = """

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
"""

def main():
    if not os.path.exists(P):
        print("找不到", P); return 1
    with io.open(P, "r", encoding="utf-8") as f:
        s = f.read()
    if "MEMORY-ARCHIVE" in s:
        print("已经打过补丁，无需重复")
        return 0
    if ANCHOR not in s:
        print("锚点未找到，脚本可能已改动，请人工检查：")
        print(ANCHOR)
        return 2
    shutil.copy2(P, P + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    s = s.replace(ANCHOR, ANCHOR + ADD, 1)
    with io.open(P, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)
    rc = subprocess.call(["bash", "-n", P])
    print("语法检查 bash -n:", "通过" if rc == 0 else "失败!")
    if rc == 0:
        # 单独验证 ARCHIVE 复制段（不跑整个备份）
        MEM = "/opt/media/backup/memory"
        rc2 = subprocess.call(["bash", "-c", """
set -u
MEM_SNAP=%s
ARCH_SRC=/home/astock/.dsh/MEMORY-ARCHIVE.md
mkdir -p "$MEM_SNAP"
if [ -f "$ARCH_SRC" ]; then
  NEWH=$(md5sum "$ARCH_SRC" | cut -d' ' -f1)
  LASTF=$(ls -t "$MEM_SNAP"/MEMORY-ARCHIVE-*.md 2>/dev/null | head -1)
  if [ -z "$LASTF" ] || [ "$NEWH" != "$(md5sum "$LASTF" | cut -d' ' -f1)" ]; then
    cp -f "$ARCH_SRC" "$MEM_SNAP/MEMORY-ARCHIVE-$(date +%%Y%%m%%d).md"
    echo "test-copied: MEMORY-ARCHIVE-$(date +%%Y%%m%%d).md"
  else
    echo "already up to date"
  fi
fi
ls -l "$MEM_SNAP"/MEMORY-ARCHIVE-*.md 2>/dev/null | tail -3
""" % MEM])
        print("测试复制:", "成功" if rc2 == 0 else "失败")
    return rc

if __name__ == "__main__":
    sys.exit(main())
