#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 OPML 补充 RSS 源：抓取 → 并发测可用性 → 按 URL 去重合并进 rss_feeds.json。

用法：
    /opt/astock/venv/bin/python /opt/astock/add_rss_from_opml.py
    /opt/astock/venv/bin/python /opt/astock/add_rss_from_opml.py <opml_url>
    /opt/astock/venv/bin/python /opt/astock/add_rss_from_opml.py --dry-run

规则：
- 已存在的源（按 URL）保留原启用状态，不会被覆盖
- 新源默认停用（on=false），由用户在总站 /rss 页面自己挑
- 分类名优先用 CAT_MAP 映射，未知分类去掉 emoji 后直接使用
"""
import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

FEEDS = "/opt/astock/rss_feeds.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
PROXIES = {"http": "socks5h://127.0.0.1:7890", "https": "socks5h://127.0.0.1:7890"}
DEFAULT_OPML = "https://jackyst0.github.io/awesome-rsshub-routes/feeds.opml"

CAT_MAP = {
    "🤖 AI 专题": "AI 专题",
    "💻 技术社区": "技术社区",
    "📰 新闻资讯": "新闻资讯",
    "🌐 科技媒体": "科技媒体",
    "🛡️ 安全资讯": "安全资讯",
    "🎨 前端 & 设计": "前端设计",
    "🎨 前端 &amp; 设计": "前端设计",
    "📦 编程语言官方博客": "编程语言",
    "🏢 大厂技术博客": "大厂博客",
    "📮 技术周刊": "技术周刊",
    "🧰 开发工具版本追踪": "版本追踪",
    "📚 学术论文": "学术论文",
    "🔧 RSS 工具更新": "RSS 工具",
}
OLD_CAT_MAP = {"AI": "AI 专题", "技术": "技术社区", "资讯": "新闻资讯"}


def norm_cat(raw):
    if raw in CAT_MAP:
        return CAT_MAP[raw]
    s = re.sub(r"^[^0-9A-Za-z\u4e00-\u9fff]+", "", (raw or "").strip())
    return s.strip() or "其他"


def parse_opml(content):
    root = ET.fromstring(content)
    body = root.find("body")
    feeds = []
    for cat in body:
        for f in cat:
            if f.get("type") == "rss" and f.get("xmlUrl"):
                feeds.append({
                    "cat": (cat.get("text") or "").strip(),
                    "name": (f.get("text") or "").strip(),
                    "url": f.get("xmlUrl").strip(),
                })
    return feeds


def probe(item):
    try:
        r = requests.get(item["url"], headers={"User-Agent": UA},
                         proxies=PROXIES, timeout=15)
        root = ET.fromstring(r.content)
        n = sum(1 for el in root.iter()
                if el.tag.rsplit("}", 1)[-1].lower() in ("item", "entry"))
        return {**item, "ok": n > 0, "count": n}
    except Exception as exc:  # noqa: BLE001
        return {**item, "ok": False, "count": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:50]}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("opml", nargs="?", default=DEFAULT_OPML)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    print(f"[1/3] 抓取 OPML: {args.opml}")
    r = requests.get(args.opml, timeout=30, proxies=PROXIES)
    feeds = parse_opml(r.content)
    print(f"      解析到 {len(feeds)} 个源")

    print(f"[2/3] 并发测试（{args.workers} 线程）...")
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(probe, f) for f in feeds]
        for i, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            results.append(res)
            if not res["ok"]:
                print(f"      [{i}/{len(feeds)}] 失败: {res['name']}  {res.get('error', '空响应')}")
    ok = [x for x in results if x["ok"]]
    print(f"      可用 {len(ok)} / {len(results)}")

    if args.dry_run:
        print("\n[dry-run] 不写入文件。可用源：")
        for x in ok:
            print(f"  {norm_cat(x['cat']):8s} {x['name']}")
        return 0

    print(f"[3/3] 合并进 {FEEDS}")
    with open(FEEDS, encoding="utf-8") as fh:
        old = json.load(fh)
    old_map = {}
    for cat, items in old.items():
        for it in items:
            old_map[it["url"]] = {"cat": cat, **it}

    result = OrderedDict()
    seen = set()
    added = kept = 0

    for x in ok:
        cat = norm_cat(x["cat"])
        url = x["url"]
        if url in seen:
            continue
        seen.add(url)
        if url in old_map:
            on = old_map[url]["on"]
            kept += 1
        else:
            on = False
            added += 1
        result.setdefault(cat, []).append({"name": x["name"], "url": url, "on": on})

    for url, it in old_map.items():
        if url in seen:
            continue
        seen.add(url)
        cat = OLD_CAT_MAP.get(it["cat"], it["cat"])
        result.setdefault(cat, []).append(
            {"name": it["name"], "url": url, "on": it.get("on", False)})

    # 备份后写入
    try:
        with open(FEEDS, encoding="utf-8") as fh:
            prev = fh.read()
        with open(FEEDS + ".bak", "w", encoding="utf-8") as fh:
            fh.write(prev)
    except OSError:
        pass

    with open(FEEDS, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    total = sum(len(v) for v in result.values())
    on = sum(1 for v in result.values() for i in v if i["on"])
    print(f"\n完成：新增 {added} 个，已有 {kept} 个，共 {len(result)} 分类 {total} 源（启用 {on}）")
    for c, items in result.items():
        print(f"  {c}：{len(items)}（启用 {sum(1 for i in items if i['on'])}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
