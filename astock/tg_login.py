# -*- coding: utf-8 -*-
"""Telegram 登录（交互式，只需跑一次）"""
import asyncio
import json

from telethon import TelegramClient

CONF = "/opt/astock/tg_config.json"
SESSION = "/opt/astock/tg"
PROXY = ("socks5", "127.0.0.1", 7890)


async def main():
    print("=== Telegram 登录 ===")
    print("API ID / API HASH 到 https://my.telegram.org 获取（需用代理打开）")
    api_id = int(input("API ID: ").strip())
    api_hash = input("API HASH: ").strip()
    phone = input("手机号（含国家码，如 +8613800138000）: ").strip()

    client = TelegramClient(SESSION, api_id, api_hash, proxy=PROXY)
    await client.start(phone=phone)
    me = await client.get_me()
    print(f"\n✅ 登录成功：{me.first_name or ''} @{me.username or ''}")
    await client.disconnect()

    with open(CONF, "w", encoding="utf-8") as fh:
        json.dump({"api_id": api_id, "api_hash": api_hash, "phone": phone},
                  fh, ensure_ascii=False)
    print("配置已保存:", CONF)


asyncio.run(main())
