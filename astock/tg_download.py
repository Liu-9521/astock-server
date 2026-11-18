# -*- coding: utf-8 -*-
"""下载 Telegram 视频到 /opt/media/movies"""
import asyncio
import json
import sys

from telethon import TelegramClient

CONF = "/opt/astock/tg_config.json"
SESSION = "/opt/astock/tg"
OUT = "/opt/media/movies"
PROXY = ("socks5", "127.0.0.1", 7890)


async def main(link):
    conf = json.load(open(CONF, encoding="utf-8"))
    client = TelegramClient(SESSION, conf["api_id"], conf["api_hash"], proxy=PROXY)
    await client.start(phone=conf["phone"])

    if not link.startswith("http"):
        link = "https://t.me/" + link.lstrip("/")

    msg = await client.get_messages(link)
    if not msg:
        print("❌ 找不到这条消息")
        return
    if not msg.media:
        print("❌ 这条消息没有媒体（视频/文件）")
        return

    print("开始下载…")
    path = await client.download_media(msg, file=OUT + "/")
    if path:
        import os
        size = os.path.getsize(path) / 1024 / 1024
        print(f"✅ 已保存：{path}（{size:.1f} MB）")
    else:
        print("❌ 下载失败")
    await client.disconnect()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 tg_download.py <链接>")
        print("示例: python3 tg_download.py https://t.me/频道名/123")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
