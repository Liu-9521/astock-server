# -*- coding: utf-8 -*-
"""RSS 聚合推送：定时抓取 → 新条目推 Telegram。

内存策略：脚本跑完即退出（oneshot），无常驻进程；峰值内存约 30MB，结束即释放。
由 systemd timer 每 30 分钟触发一次，比常驻阅读器省得多。

用法：
    python3 rss_push.py --dry-run     # 只打印，不发 TG
    python3 rss_push.py               # 正常推送
    python3 rss_push.py --init        # 首次：只记录已读，不推送（避免刷屏）
"""
import json
import hashlib
import html
import os
import re
import sys
import time
from datetime import datetime
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

TOKEN = "8631214060:AAFGyR7cOvYP84H0LKQbjNC03K41UdIp2lU"
CHAT_ID = 8687318272          # 你的 Telegram user_id
WECOM_WEBHOOK = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
                 "?key=<WECOM_YOUR_KEY>")
PROXY = "socks5h://127.0.0.1:7890"

# 英文源翻译成中文（DeepSeek 直连，不走代理）
DEEPSEEK_KEY = "<DEEPSEEK_YOUR_KEY>"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DIGEST = "/opt/astock/rss_digest.json"
PUSH_HOURS = {7, 20}
STATE = "/opt/astock/rss_state.json"
DB = "/opt/astock/astock.db"
FEEDS_FILE = "/opt/astock/rss_feeds.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
TIMEOUT = 15
KEEP = 1500                   # 每个源记住最近多少条（arXiv/HF 单次就有几百条）
MAX_PUSH = 6                  # 每个源每次最多推几条

PROXIES = {"http": PROXY, "https": PROXY}

# 仅当 rss_feeds.json 不存在或为空时使用
DEFAULT_FEEDS = {
    "AI": [
        {"name": "arXiv cs.AI", "url": "https://rss.arxiv.org/rss/cs.AI", "on": True},
        {"name": "量子位", "url": "https://www.qbitai.com/feed", "on": True},
        {"name": "Hugging Face", "url": "https://huggingface.co/blog/feed.xml", "on": True},
    ],
    "技术": [
        {"name": "阮一峰", "url": "https://www.ruanyifeng.com/blog/atom.xml", "on": True},
        {"name": "少数派", "url": "https://sspai.com/feed", "on": True},
        {"name": "Hacker News", "url": "https://hnrss.org/frontpage", "on": True},
        {"name": "Solidot", "url": "https://www.solidot.org/index.rss", "on": True},
    ],
    "影视": [],
}


def load_feeds():
    """从 rss_feeds.json 读启用的源；文件缺失/损坏时回退默认。"""
    try:
        with open(FEEDS_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = DEFAULT_FEEDS
    out = {}
    if isinstance(raw, dict):
        for cat, items in raw.items():
            if not isinstance(items, list):
                continue
            picked = [(str(i.get("name") or ""), str(i.get("url") or ""))
                      for i in items
                      if isinstance(i, dict) and i.get("on", True) and i.get("url")]
            if picked:
                out[cat] = picked
    return out or {c: [(i["name"], i["url"]) for i in v if i.get("on", True)]
                   for c, v in DEFAULT_FEEDS.items() if v}

CAT_ICON = {
    "AI 专题": "🤖", "技术社区": "💻", "新闻资讯": "📰", "科技媒体": "🌐",
    "安全资讯": "🛡️", "前端设计": "🎨", "编程语言": "📦", "大厂博客": "🏢",
    "技术周刊": "📮", "版本追踪": "🧰", "学术论文": "📚", "RSS 工具": "🔧",
    "影视": "🎬",
    # 兼容旧分类名
    "AI": "🤖", "技术": "💻", "资讯": "📰",
}


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _text(el, name):
    for child in el:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def parse(content):
    """解析 RSS 2.0 / Atom，返回 [{id,title,link,desc}]，新的在前。"""
    root = ET.fromstring(content)
    out = []
    kind = _local(root.tag)
    wanted = "entry" if kind == "feed" else "item"
    for el in root.iter():
        if _local(el.tag) != wanted:
            continue
        title = _text(el, "title")
        link = _text(el, "link")
        if not link:                                  # Atom 的 link 在属性里
            for c in el:
                if _local(c.tag) == "link" and c.get("href"):
                    link = c.get("href")
                    break
        uid = (_text(el, "guid") or _text(el, "id") or link or title)
        desc = _text(el, "description") or _text(el, "summary") or ""
        desc = " ".join(desc.replace("<", " <").split())
        out.append({"id": uid, "title": title, "link": link, "desc": desc[:160]})
    return out


def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True,
              "parse_mode": "HTML"},
        proxies=PROXIES, timeout=25)
    return r.ok


def strip_html(text):
    """去掉摘要里的 HTML 标签和实体。"""
    text = re.sub(r"<[^>]*>", " ", text or "")
    text = html.unescape(text)
    return " ".join(text.split())


def clean_desc(desc):
    """去掉 arXiv 之类的机器前缀噪声 + HTML。"""
    desc = strip_html(desc)
    desc = re.sub(r"^arXiv:\S+\s+Announce Type:\s*\S+\s*", "", desc)
    desc = re.sub(r"^Abstract:\s*", "", desc)
    return desc.strip()


def _truncate_bytes(text, limit=3900):
    """企微 markdown 限制 4096 字节（中文字符占 3 字节），按字节安全截断。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore") + "…"


def _fit_lines(lines, limit=3900):
    """按整行累积到字节上限，避免把某条截成半截。"""
    out, total = [], 0
    for line in lines:
        size = len(line.encode("utf-8")) + 1
        if total + size > limit:
            out.append("…（后略）")
            break
        out.append(line)
        total += size
    return "\n".join(out)


def _has_cjk(text):
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _md5(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]


def translate_items(items, cache):
    """给英文条目标题/摘要补中文译文，原文保留，供双语对照显示。

    结果写入 it["title_zh"] / it["desc_zh"]；it["title"] / it["desc"] 保持原文。
    中文条目跳过；翻译失败保留原文（title_zh 为空）。
    """
    # 先清洗原文前缀（如 arXiv 的 "Announce Type: new Abstract:"），
    # 否则翻译后会变成「公告类型：新 摘要：」，正则就匹配不到了
    for it in items:
        it["desc"] = clean_desc(it["desc"])
        it["title_zh"] = ""
        it["desc_zh"] = ""

    need = [it for it in items if it["title"] and not _has_cjk(it["title"] + it["desc"])]
    if not need:
        return items

    pending = []
    for it in need:
        key = _md5(it["title"] + "|" + it["desc"])
        if key in cache:
            it["title_zh"], it["desc_zh"] = cache[key]
        else:
            pending.append((key, it))
    if not pending:
        return items

    rows = [f"{i}|{it['title'][:120]}|{it['desc'][:120]}"
            for i, (_, it) in enumerate(pending, 1)]
    prompt = ("把下面每行的英文翻译成简体中文，保持行首数字和竖线分隔，"
              "只输出译文行，不要任何解释：\n" + "\n".join(rows))
    try:
        r = requests.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}",
                     "Content-Type": "application/json"},
            json={"model": "deepseek-chat", "temperature": 0,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=40)
        content = r.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        print(f"[翻译失败] {type(exc).__name__} {exc}")
        return items

    for line in content.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 2:
            continue
        idx = parts[0].strip().rstrip(".").strip()
        if not idx.isdigit() or not (1 <= int(idx) <= len(pending)):
            continue
        key, it = pending[int(idx) - 1]
        zh_title = parts[1].strip()[:120]
        zh_desc = parts[2].strip()[:120] if len(parts) == 3 else ""
        it["title_zh"] = zh_title or ""
        it["desc_zh"] = zh_desc
        cache[key] = (it["title_zh"], it["desc_zh"])
    return items


def send_wecom(md):
    """企微群机器人：markdown 消息，国内直连不走代理。"""
    try:
        r = requests.post(
            WECOM_WEBHOOK,
            json={"msgtype": "markdown",
                  "markdown": {"content": _truncate_bytes(md)}},
            timeout=20)
        return r.json().get("errcode") == 0
    except Exception as exc:  # noqa: BLE001
        print(f"[企微失败] {type(exc).__name__} {exc}")
        return False



def sync_db(state):
    """把 state 同步进 SQLite（rss_items + translations），失败不影响主流程。"""
    import sqlite3
    import time
    try:
        con = sqlite3.connect(DB, timeout=15)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        n_items = 0
        for feed, urls in state.items():
            if feed.startswith("_") or not isinstance(urls, list):
                continue
            for u in urls:
                try:
                    con.execute(
                        "INSERT OR IGNORE INTO rss_items(feed, item_url, pushed_at) VALUES (?,?,?)",
                        (feed, u, now))
                    n_items += con.total_changes
                except sqlite3.Error:
                    pass
        n_tr = 0
        for hkey, val in (state.get("_tr") or {}).items():
            try:
                if isinstance(val, (list, tuple)) and len(val) == 2:
                    zh = json.dumps(list(val), ensure_ascii=False)
                else:
                    zh = str(val)
                con.execute(
                    "INSERT OR IGNORE INTO translations(text_hash, zh) VALUES (?,?)",
                    (hkey, zh))
                n_tr += con.total_changes
            except sqlite3.Error:
                pass
        con.commit()
        con.close()
        print(f"  DB 同步: rss_items +{n_items}, translations +{n_tr}")
    except Exception as exc:  # noqa: BLE001
        print(f"  DB 同步失败（不影响推送）: {type(exc).__name__}: {exc}")


def main():
    dry = "--dry-run" in sys.argv
    force_init = "--init" in sys.argv
    state = {}
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = {}
    first_run = force_init or not state
    tr_cache = state.setdefault("_tr", {})

    total_new = 0
    all_tg, all_wecom, digest_new = [], [], []
    for cat, feeds in load_feeds().items():
        tg_lines = []
        wecom_lines = []
        for name, url in feeds:
            try:
                r = requests.get(url, headers={"User-Agent": UA},
                                 proxies=PROXIES, timeout=TIMEOUT)
                r.raise_for_status()
                items = parse(r.content)
            except Exception as exc:  # noqa: BLE001
                print(f"[失败] {name}: {type(exc).__name__} {exc}")
                continue
            seen = state.get(url) or []
            seen_set = set(seen)
            new_source = not seen          # 第一次见到这个源（刚在网页上启用）
            fresh = [it for it in items if it["id"] not in seen_set][:MAX_PUSH]
            if dry:
                print(f"[预览] {name}（共 {len(items)} 条）")
                for it in items[:2]:
                    print(f"   • {it['title'][:80]}")
                    print(f"     {it['link']}")
            if new_source and not dry:
                print(f"[新增] {name}: 首次记录 {len(items)} 条，不推送")
            if fresh and not first_run and not new_source:
                fresh = translate_items(fresh, tr_cache)
                icon = CAT_ICON.get(cat, "•")
                tg_lines.append(f"<b>{icon} {html.escape(name)}</b>")
                wecom_lines.append(f"### {icon} {name}")
                for it in fresh:
                    zh = (it.get("title_zh") or "").strip()
                    raw = it["title"]
                    shown = zh or raw
                    title = html.escape(shown[:120])
                    if it["link"]:
                        link = html.escape(it["link"], quote=True)
                        tg_lines.append(f'• <a href="{link}">{title}</a>')
                        wecom_lines.append(f"- [{shown[:100]}]({it['link']})")
                    else:
                        tg_lines.append(f"• {title}")
                        wecom_lines.append(f"- {shown[:100]}")
                    # 双语对照：有译文时补一行英文原文
                    if zh and zh != raw:
                        tg_lines.append(f"  <i>{html.escape(raw[:90])}</i>")
                        wecom_lines.append(f"  {raw[:90]}")
                    desc = (it.get("desc_zh") or "").strip() or clean_desc(it["desc"])
                    desc = strip_html(desc)
                    if desc:
                        tg_lines.append(f"  {html.escape(desc[:50])}…")
                        # 企微 markdown 不解析 HTML，且 [] 会破坏链接语法
                        safe = desc[:50].replace("[", "(").replace("]", ")")
                        wecom_lines.append(f"  {safe}…")
                for it in fresh:
                    digest_new.append({
                        "t": datetime.now().strftime("%m-%d %H:%M"),
                        "cat": cat, "src": name,
                        "title": (it.get("title_zh") or it["title"])[:160],
                        "raw": it["title"][:160],
                        "desc": (it.get("desc_zh") or clean_desc(it["desc"]))[:200],
                        "link": it["link"],
                    })
                total_new += len(fresh)
                print(f"[新] {name}: {len(fresh)} 条")
            elif not first_run:
                print(f"[无] {name}")
            # 更新已读
            ids = [it["id"] for it in items][:KEEP]
            merged = ids + [i for i in seen if i not in ids]
            state[url] = merged[:KEEP]
        all_tg += tg_lines
        all_wecom += wecom_lines

    # ---- 合并成一条推送，且只在 08:00 / 20:00 推送 ----
    hour = datetime.now().hour
    should_push = hour in PUSH_HOURS
    if dry:
        print("\n----- DRY RUN 合并推送 -----")
        print("\n".join(all_tg)[:2000])
        print("---------------------------")
    elif all_tg and should_push:
        ok = send("\n".join(all_tg)[:3900])
        print(f"[TG] 合并: {'成功' if ok else '失败'}")
        ok = send_wecom(_fit_lines(all_wecom))
        print(f"[企微] 合并: {'成功' if ok else '失败'}")
    elif all_tg:
        print(f"[静默] 当前 {hour} 点，不在推送时段（08/20），只记录")

    # ---- 累积到当日简报 ----
    if digest_new and not dry:
        try:
            old = json.loads(Path(DIGEST).read_text(encoding="utf-8"))
        except Exception:
            old = {}
        today = datetime.now().strftime("%Y-%m-%d")
        if old.get("date") != today:
            old = {"date": today, "items": []}
        old["items"] = (digest_new + old.get("items", []))[:300]
        old["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            Path(DIGEST).write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            print(f"[简报] 写入失败: {exc}")

    if first_run:
        print("首次运行：只记录不推送。下次起只推新条目。")
        total_new = 0
    if not dry:
        with open(STATE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        sync_db(state)
    print(f"完成，新条目 {total_new} 条，源 "
          f"{len([k for k in state if not k.startswith('_')])} 个")


if __name__ == "__main__":
    import resource
    main()
    print(f"峰值内存 {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.1f} MB")
