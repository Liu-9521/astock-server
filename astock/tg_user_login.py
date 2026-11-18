# -*- coding: utf-8 -*-
"""登录 Telegram user 账号（只需一次），供 tg_bot 下载受保护内容。

分两步执行，方便远程操作（不用本地终端）：
  第 1 步：发验证码
    /opt/astock/venv/bin/python tg_user_login.py --init --phone +8613800138000

  第 2 步：填验证码
    /opt/astock/venv/bin/python tg_user_login.py --code 12345

  若账号开了两步验证，再加 --password：
    /opt/astock/venv/bin/python tg_user_login.py --code 12345 --password 你的密码

API_ID / API_HASH 默认沿用 bot 的 2040（Telegram Desktop 官方 id）。
若报 API_ID_INVALID / 要求额外验证，请到 https://my.telegram.org 申请自己的，再这样跑：
    ... --init --phone +86... --api-id 你的id --api-hash 你的hash
"""
import argparse
import json
import os

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

SESSION = "/opt/astock/tg_user"
CONF = "/opt/astock/tg_user_config.json"
STATE = "/opt/astock/.tg_login_state.json"
PROXY = ("socks5", "127.0.0.1", 7890)

DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"


def _save(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


async def cmd_init(args):
    api_id, api_hash = args.api_id, args.api_hash
    cli = TelegramClient(SESSION, api_id, api_hash, proxy=PROXY)
    await cli.connect()
    if await cli.is_user_authorized():
        me = await cli.get_me()
        _save(CONF, {"api_id": api_id, "api_hash": api_hash, "phone": args.phone})
        print(f"✅ 已经登录过了：{me.first_name or ''} @{me.username or ''}")
        await cli.disconnect()
        return
    sent = await cli.send_code_request(args.phone)
    _save(STATE, {
        "phone": args.phone,
        "api_id": api_id,
        "api_hash": api_hash,
        "phone_code_hash": sent.phone_code_hash,
    })
    await cli.disconnect()
    print(f"✅ 验证码已发送到 {args.phone}")
    print(f"   下一步：/opt/astock/venv/bin/python tg_user_login.py --code 收到的验证码")
    print("   （没收到？等 60 秒后可重新 --init，不要连续猛点）")


async def cmd_code(args):
    st = _load(STATE)
    cli = TelegramClient(SESSION, st["api_id"], st["api_hash"], proxy=PROXY)
    await cli.connect()
    try:
        await cli.sign_in(phone=st["phone"], code=args.code,
                          phone_code_hash=st["phone_code_hash"])
    except SessionPasswordNeededError:
        if not args.password:
            print("⚠️ 该账号开了两步验证，请重跑并加 --password 你的密码")
            await cli.disconnect()
            return 1
        await cli.sign_in(password=args.password)
    me = await cli.get_me()
    _save(CONF, {"api_id": st["api_id"], "api_hash": st["api_hash"], "phone": st["phone"]})
    try:
        os.remove(STATE)
    except OSError:
        pass
    await cli.disconnect()
    print(f"✅ 登录成功：{me.first_name or ''} @{me.username or ''}（id={me.id}）")
    print(f"   配置文件：{CONF}")
    print("   重启 bot 生效：sudo systemctl restart tg-bot")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="发送验证码")
    ap.add_argument("--code", help="验证码")
    ap.add_argument("--password", help="两步验证密码（可选）")
    ap.add_argument("--phone", help="手机号，含国家码")
    ap.add_argument("--api-id", type=int, default=DEFAULT_API_ID)
    ap.add_argument("--api-hash", default=DEFAULT_API_HASH)
    args = ap.parse_args()

    if args.init:
        if not args.phone:
            ap.error("--init 需要 --phone")
        asyncio_run(cmd_init(args))
    elif args.code:
        asyncio_run(cmd_code(args))
    else:
        ap.print_help()


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    raise SystemExit(main())
