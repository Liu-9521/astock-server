# -*- coding: utf-8 -*-
"""Telegram 下载 Bot

两种下载来源：
1) 你把文件/视频转发给 bot —— 用 bot 账号直接下载。
2) 受保护内容（禁止转发/保存）—— 发消息链接给 bot，用 user 账号抓取后下载。
   user 账号只需能正常观看该会话，restrict saving 只拦客户端 UI，不拦 API 下载。

user 账号登录（一次性）：
    /opt/astock/venv/bin/python /opt/astock/tg_user_login.py
生成 /opt/astock/tg_user.session 和 /opt/astock/tg_user_config.json。
没登录时 bot 其它功能照常，/get 会提示先登录。
"""
import asyncio
import json
import os
import re
import sys
import time

from telethon import TelegramClient, events, types

TOKEN = "8631214060:AAFGyR7cOvYP84H0LKQbjNC03K41UdIp2lU"
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
OUT = "/opt/media/movies"
PROXY = ("socks5", "127.0.0.1", 7890)
WORKERS = 8   # 并发分块数
# 允许使用的 Telegram user_id；空集合 = 先记录不拦截（拿到自己的 ID 后填进来）
ALLOWED_USERS = {8687318272}

USER_SESSION = "/opt/astock/tg_user"
USER_CONF = "/opt/astock/tg_user_config.json"

# bot 账号：收转发、发状态
BOT = TelegramClient("/opt/astock/tg_bot", API_ID, API_HASH, proxy=PROXY)
# user 账号：按链接抓受保护内容（懒加载）
USER = None

# t.me/c/<channel_id>/<msg_id>  → 私有频道/群
LINK_PRIVATE = re.compile(r"t\.me/c/(\d+)/(\d+)")
# t.me/<username>/<msg_id>      → 公开频道/群
LINK_PUBLIC = re.compile(r"t\.me/([A-Za-z][A-Za-z0-9_]*)/(\d+)")


async def get_user():
    """懒加载 user 客户端；未登录返回 None。"""
    global USER
    if USER is not None:
        return USER
    try:
        with open(USER_CONF, encoding="utf-8") as fh:
            conf = json.load(fh)
    except (OSError, ValueError):
        return None
    cli = TelegramClient(USER_SESSION, int(conf["api_id"]), conf["api_hash"], proxy=PROXY)
    await cli.connect()
    if not await cli.is_user_authorized():
        await cli.disconnect()
        return None
    USER = cli
    return USER


async def resolve_link(user, url):
    """把 t.me 链接解析成消息对象。"""
    m = LINK_PRIVATE.search(url)
    if m:
        peer = types.PeerChannel(int(m.group(1)))
        return await user.get_messages(peer, ids=int(m.group(2)))
    m = LINK_PUBLIC.search(url)
    if m:
        return await user.get_messages(m.group(1), ids=int(m.group(2)))
    raise ValueError("链接格式不对")


async def download_parallel(cli, message, out_path, progress=None, workers=WORKERS):
    """把媒体分成 workers 块并发下载，边下边写，按字节位置截断。"""
    media = message.media
    doc = getattr(media, "document", None)
    if doc is None or not doc.size:
        return None
    size = doc.size
    chunk = size // workers + 1
    done = [0]
    lock = asyncio.Lock()
    write_lock = asyncio.Lock()

    with open(out_path, "wb") as fh:
        fh.truncate(size)   # 预分配

        async def part(i):
            start = i * chunk
            end = min(start + chunk, size)
            pos = start
            async for block in cli.iter_download(media, offset=start):
                take = block[: max(0, end - pos)]
                if not take:
                    break
                async with write_lock:
                    fh.seek(pos)
                    fh.write(take)
                pos += len(take)
                async with lock:
                    done[0] += len(take)
                    if progress:
                        await progress(min(done[0], size), size)

        await asyncio.gather(*[part(i) for i in range(workers)])
    return out_path


# 当前正在下载的任务（供 /cancel 使用）
CURRENT = {"task": None, "file": None}
QUEUE = asyncio.Queue()


def _tg_state(active: bool) -> None:
    """写下载状态文件，供总站重启代理前检查。"""
    try:
        with open("/opt/astock/tg_download.json", "w", encoding="utf-8") as fh:
            fh.write("1\n" if active else "0\n")
    except OSError:
        pass


def _ext_of(doc):
    """按 mime 推断扩展名，只认常见视频类型，其余返回空串。"""
    mime = (doc.mime_type or "").lower()
    known = {
        "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-matroska": ".mkv",
        "video/x-msvideo": ".avi", "video/webm": ".webm", "video/x-flv": ".flv",
        "video/mp2t": ".ts", "video/x-ms-wmv": ".wmv",
    }
    if mime in known:
        return known[mime]
    if mime.startswith("video/") and "/" in mime:
        return "." + mime.split("/")[-1]
    return ""


def _name_of(doc, ext):
    for attr in getattr(doc, "attributes", []):
        fname = getattr(attr, "file_name", None)
        if fname:
            # Telegram 传来的文件名常常没有后缀，Jellyfin 会直接忽略这类文件
            if not os.path.splitext(fname)[1] and ext:
                fname += ext
            return fname
    return f"tg_{int(time.time())}{ext or '.mp4'}"


@BOT.on(events.NewMessage(pattern="/cancel"))
async def on_cancel(event):
    task = CURRENT.get("task")
    if task and not task.done():
        task.cancel()
        path = CURRENT.get("file")
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        await event.reply("⏹ 已取消当前下载（半成品已删除）")
    else:
        await event.reply("没有正在进行的下载")


@BOT.on(events.NewMessage(pattern="/start"))
async def on_start(event):
    await event.reply(
        f"👋 两种用法：\n"
        f"1. 转发视频/文件给我 → 直接下载（{WORKERS} 线程）\n"
        f"2. 发 t.me 消息链接 → 下载禁止转发的内容（需已登录 user 账号）\n"
        f"命令：/queue 看队列、/cancel 取消"
    )


@BOT.on(events.NewMessage(pattern="/queue"))
async def on_queue(event):
    if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS:
        return
    cur = CURRENT.get("file")
    lines = [f"📋 队列里还有 {QUEUE.qsize()} 个"]
    if cur:
        lines.append(f"⏳ 正在下载：{os.path.basename(cur)}")
    if QUEUE.qsize() == 0 and not cur:
        lines.append("空闲中")
    await event.reply("\n".join(lines))


@BOT.on(events.NewMessage(pattern=r"/get(?:\s|$)"))
async def on_get(event):
    if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS:
        return
    await handle_links(event, event.raw_text)


@BOT.on(events.NewMessage(incoming=True))
async def on_message(event):
    sender_id = event.sender_id
    print(f"消息来自 user_id={sender_id}", flush=True)
    if ALLOWED_USERS and sender_id not in ALLOWED_USERS:
        await event.reply("⛔ 这是私人机器人，无权使用。")
        return

    # 1) 媒体消息：转发过来的文件
    if event.message.media:
        ahead = QUEUE.qsize() + (1 if CURRENT.get("task") else 0)
        status = await event.reply(f"📥 已加入队列（前面还有 {ahead} 个）")
        await QUEUE.put({"cli": BOT, "msg": event.message, "status": status})
        _tg_state(True)
        return

    # 2) 文本里的 t.me 链接：受保护内容
    text = event.raw_text or ""
    if text.startswith("/"):
        return   # 命令交给各自的 handler，避免重复处理
    if "t.me/" in text:
        await handle_links(event, text)


async def handle_links(event, text):
    user = await get_user()
    if user is None:
        await event.reply(
            "⚠️ 还没登录 user 账号，下载不了受保护内容。\n"
            "在服务器执行一次：\n"
            "/opt/astock/venv/bin/python /opt/astock/tg_user_login.py"
        )
        return
    urls = re.findall(r"(?:https?://)?t\.me/(?:c/)?[A-Za-z0-9_]+/\d+", text)
    if not urls:
        await event.reply("没识别到 t.me 消息链接")
        return
    ok = 0
    for url in urls:
        try:
            msg = await resolve_link(user, url)
        except Exception as exc:  # noqa: BLE001
            await event.reply(f"❌ 取消息失败：{url}\n{exc}")
            continue
        if not msg or not msg.media:
            await event.reply(f"❌ 这条没有媒体：{url}")
            continue
        status = await BOT.send_message(event.chat_id, f"📥 已加入队列（受保护链接）\n{url}")
        await QUEUE.put({"cli": user, "msg": msg, "status": status})
        _tg_state(True)
        ok += 1
    if ok:
        await event.reply(f"✅ {ok} 个链接已入队")


async def download_one(item):
    """下载一条消息：cli 是真正执行下载的客户端。"""
    _tg_state(True)
    cli = item["cli"]
    message = item["msg"]
    status = item["status"]
    media = message.media
    doc = getattr(media, "document", None)
    started = time.time()
    last = [0.0]

    async def progress(current, total):
        now = time.time()
        if now - last[0] < 3:
            return
        last[0] = now
        pct = current / total * 100 if total else 0
        speed = current / max(1e-6, now - started) / 1024 / 1024
        try:
            await status.edit(
                f"⏳ 下载中 {pct:.1f}%（{WORKERS} 线程）\n"
                f"{current / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB\n"
                f"速度 {speed:.1f} MB/s"
            )
        except Exception:  # noqa: BLE001
            pass

    try:
        if doc and doc.size:
            ext = _ext_of(doc)
            fname = _name_of(doc, ext)
            out_path = os.path.join(OUT, fname)
            if os.path.exists(out_path):
                base, e2 = os.path.splitext(fname)
                out_path = os.path.join(OUT, f"{base}_{int(time.time())}{e2}")
            CURRENT["file"] = out_path
            await download_parallel(cli, message, out_path, progress)
            # 自动归档剧集（识别 剧名 S01E05 / 第5集 / EP5 之类）
            try:
                sys.path.insert(0, "/opt/astock")
                from organize_media import organize
                from pathlib import Path
                dest = organize(Path(out_path))
                if dest:
                    out_path = str(dest)
            except Exception as exc:  # noqa: BLE001
                print("自动归档失败:", exc, flush=True)
            size = os.path.getsize(out_path) / 1024 / 1024
            print(f"✅ 保存: {out_path} ({size:.1f} MB)", flush=True)
            try:
                import sys as _sys
                _sys.path.insert(0, "/opt/astock")
                from db_log import log_download as _ld
                _ld("tg", os.path.basename(out_path), out_path,
                    int(size * 1024 * 1024), "ok")
            except Exception:
                pass
            await status.edit(f"✅ 已下载：{os.path.basename(out_path)}（{size:.1f} MB）")
        else:
            path = await message.download_media(OUT + "/")
            if path:
                size = os.path.getsize(path) / 1024 / 1024
                await status.edit(f"✅ 已下载：{os.path.basename(path)}（{size:.1f} MB）")
            else:
                await status.edit("❌ 下载失败")
    except asyncio.CancelledError:
        out_path = CURRENT.get("file")
        if out_path and os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass
        try:
            await status.edit("⏹ 已取消")
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        CURRENT["file"] = None


async def queue_worker():
    """串行消费队列：一次只下一个，下一个自动开始。"""
    while True:
        item = await QUEUE.get()
        task = asyncio.create_task(download_one(item))
        CURRENT["task"] = task
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            print("下载失败:", exc, flush=True)
            try:
                await item["status"].edit(f"❌ 下载失败：{exc}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            CURRENT["task"] = None
            QUEUE.task_done()
            if QUEUE.empty():
                _tg_state(False)


async def _mihomo_pid():
    """读 mihomo 的 MainPID；读不到返回 None。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "show", "mihomo", "-p", "MainPID", "--value",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return out.decode(errors="ignore").strip() or None
    except Exception:  # noqa: BLE001
        return None


async def self_heal():
    """mihomo 重启（内存守护 / 崩溃自动拉起）会让代理瞬断，Telethon 要等约 2 分钟才重连。

    这里监视 mihomo 的 MainPID，一旦变化就主动退出，交给 systemd 立刻拉起，
    等效于「guard 顺手重启 tg-bot」。不依赖 BOT.is_connected()（断线后它仍返回 True）。
    """
    baseline = await _mihomo_pid()
    while True:
        await asyncio.sleep(10)
        pid = await _mihomo_pid()
        if pid and baseline and pid != baseline:
            print(f"mihomo 已重启（PID {baseline} → {pid}），退出进程交给 systemd", flush=True)
            os._exit(0)
        if pid:
            baseline = pid


async def main():
    await BOT.start(bot_token=TOKEN)
    me = await BOT.get_me()
    print(f"Bot 已启动: @{me.username}", flush=True)
    user = await get_user()
    print("user 账号：" + ("已登录 ✅" if user else "未登录（/get 不可用）"), flush=True)
    asyncio.create_task(queue_worker())
    asyncio.create_task(self_heal())
    await BOT.run_until_disconnected()


asyncio.run(main())
