#!/bin/bash
# qBittorrent 下载完成后自动触发：临时拉起 Radarr 识别 + 改名，跑完就关掉
exec 9>/tmp/movie_autorun.lock
flock -n 9 || exit 0

cd /home/astock/.dsh/dsh_wechat || exit 0
export MAI_RECENT=60

# 没有待整理的就不启动 Radarr，省内存
if [ "${FORCE:-0}" != "1" ]; then
  ./.venv_img/bin/python movie_autoimport.py --check || exit 0
fi

sleep 60          # 等下载器收尾 / Jellyfin 不再动文件

STARTED=0
if ! pgrep -f "Radarr -nobrowser" >/dev/null; then
  (
    cd /home/astock/radarr/app || exit 0
    HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 NO_PROXY=localhost,127.0.0.1 DOTNET_GCHeapHardLimit=0xC800000 setsid ./Radarr -nobrowser \
      -data=/home/astock/radarr/config >> /home/astock/radarr/radarr.log 2>&1 < /dev/null &
  )
  STARTED=1
  for i in $(seq 1 40); do
    sleep 2
    curl -s -m 2 -o /dev/null http://127.0.0.1:7878/api/v3/system/status && break
  done
fi

./.venv_img/bin/python movie_autoimport.py >> movie_autorun.log 2>&1

# 只关自己拉起来的那个，不动你手动开着的
if [ "$STARTED" = "1" ]; then
  pkill -f "Radarr -nobrowser" 2>/dev/null
fi
