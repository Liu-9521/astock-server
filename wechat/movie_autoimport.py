#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qB 下载完成后，自动把电影重命名成「中文名 (年份)」并归好目录。

识别引擎用本机 Radarr 的 TMDB 查询接口（不需要额外 API key）。
匹配不确定的一律不动，只记日志，避免改错名字。

用法：
  python3 movie_autoimport.py --dry      # 只看会怎么改
  python3 movie_autoimport.py            # 真改
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path("/home/astock/.dsh/dsh_wechat")
MOVIES = Path("/opt/media/movies")
LOG = BASE / "movie_autoimport.log"
RADARR = "http://127.0.0.1:7878"
RADARR_CFG = Path("/home/astock/radarr/config/config.xml")
JF_DB = "/opt/jellyfin/config/data/jellyfin.db"
JF_URL = "http://127.0.0.1:8096"
QB_URL = "http://127.0.0.1:8081"
QB_USER, QB_PASS = "astock", "astock"
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".ts", ".mov", ".m4v", ".rmvb", ".wmv"}
MIN_SIZE = 200 * 1024 * 1024
RECENT_SECONDS = int(os.environ.get("MAI_RECENT", "900"))  # 改过没多久的当作还在下载
MIN_SCORE = 70

NOISE = [
    r"6v电影", r"6v123", r"yingku", r"电影港", r"地址发布页", r"收藏不迷路",
    r"www\.[a-z0-9._-]+", r"[a-z0-9]+\.(com|net|cc|top|tv)",
    r"最新电影", r"dyg7", r"高清", r"蓝光", r"bluray", r"bd", r"web-?dl",
    r"hdrip", r"remux", r"h\.?264", r"h\.?265", r"x264", r"x265", r"hevc",
    r"\d{3,4}p", r"\d{3,4}x\d{3,4}", r"4k", r"60fps", r"高码版", r"60帧",
    r"hd", r"国粤双语", r"国英双语", r"中英双字", r"中法双字", r"中英字幕",
    r"国语中字", r"中文字幕", r"内封", r"内嵌", r"外挂", r"字幕", r"简体",
    r"繁体", r"中字", r"国语", r"双语", r"无水印", r"ddp?\d(\.\d)?", r"aac",
    r"atmos", r"truehd", r"10bit", r"hdr", r"dv", r"sdr", r"imax", r"dts",
    r"收藏版", r"导演剪辑", r"加长版",
]
NOISE_RE = re.compile("|".join(NOISE), re.I)
YEAR_RE = re.compile(r"(19|20)\d{2}")


def log(msg, echo=True):
    line = f"{time.strftime('%F %T')}  {msg}"
    if echo:
        print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def radarr_key():
    try:
        txt = RADARR_CFG.read_text(encoding="utf-8")
        m = re.search(r"<ApiKey>([^<]+)</ApiKey>", txt)
        return m.group(1) if m else ""
    except OSError:
        return ""


def r_get(path, key, timeout=60):
    req = urllib.request.Request(RADARR + path, headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def lookup(term, key):
    try:
        return r_get("/api/v3/movie/lookup?term=" + urllib.parse.quote(term), key) or []
    except Exception as exc:  # noqa: BLE001
        log(f"  查询失败 {term!r}: {type(exc).__name__}")
        return []


def clean_name(stem):
    s = NOISE_RE.sub(" ", stem)
    s = re.sub(r"[\[\]【】()（）._\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def norm(s):
    return re.sub(r"[\s·:：,，.。!！?？\-_'\"“”]", "", s or "").lower()


def pick(name, results):
    """在 TMDB 结果里挑最靠谱的一个，返回 (movie, score)"""
    n = norm(name)
    if not n:
        return None, 0
    best, best_score = None, 0
    for m in results:
        titles = [m.get("title"), m.get("originalTitle")]
        titles += [a.get("title") for a in (m.get("alternateTitles") or [])][:5]
        score = 0
        for t in [x for x in titles if x]:
            tn = norm(t)
            if not tn:
                continue
            if tn == n:
                score = max(score, 100)
            elif n in tn or tn in n:
                score = max(score, 80)
        if score:
            y = YEAR_RE.search(name)
            if y and str(m.get("year") or "") == y.group(0):
                score += 10
            if score > best_score:
                best, best_score = m, score
    return best, best_score


def qbit_active():
    try:
        s = urllib.request.build_opener()
        import http.cookiejar
        cj = http.cookiejar.CookieJar()
        s = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        data = urllib.parse.urlencode({"username": QB_USER, "password": QB_PASS}).encode()
        s.open(QB_URL + "/api/v2/auth/login", data=data, timeout=8).read()
        ts = json.loads(s.open(QB_URL + "/api/v2/torrents/info", timeout=10).read())
        return {t["name"] for t in ts}
    except Exception:  # noqa: BLE001
        return None


def jf_scan():
    try:
        con = sqlite3.connect(f"file:{JF_DB}?mode=ro", uri=True)
        row = con.execute("select AccessToken from ApiKeys limit 1").fetchone()
        con.close()
        if not row:
            return
        req = urllib.request.Request(JF_URL + "/Library/Refresh", method="POST",
                                     headers={"Authorization": f'MediaBrowser Token="{row[0]}"'})
        urllib.request.urlopen(req, timeout=15).read()
        log("  已触发 Jellyfin 扫描")
    except Exception as exc:  # noqa: BLE001
        log(f"  Jellyfin 扫描失败: {type(exc).__name__}")


def candidates(active):
    out = []
    for f in MOVIES.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXT:
            continue
        if f.name.lower().endswith((".part", ".!qb")) or f.suffix.lower() == ".!qb":
            continue
        try:
            if f.stat().st_size < MIN_SIZE:
                continue
            if time.time() - f.stat().st_mtime < RECENT_SECONDS:
                continue
        except OSError:
            continue
        if active and any(f.name in a or a in f.name for a in active):
            continue
        # 已经在「片名 (年份)」目录里且文件名也规范的，跳过
        if f.parent != MOVIES and re.search(r"\((\d{4})\)", f.parent.name) \
                and re.search(r"\((\d{4})\)", f.stem):
            continue
        out.append(f)
    return sorted(set(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--check", action="store_true", help="只检查有没有待整理的，3=没有")
    args = ap.parse_args()

    key = radarr_key()
    if not key:
        log("读不到 Radarr API key，退出")
        return 1
    active = qbit_active()
    if active is None:
        log("qB API 不可用（无法确认任务状态），本次跳过")
        return 1

    files = candidates(active)
    if args.check:
        return 0 if files else 3
    if args.limit:
        files = files[:args.limit]
    log(f"待处理 {len(files)} 个文件{'（演练）' if args.dry else ''}")

    done = skipped = 0
    for f in files:
        name = clean_name(f.stem)
        if len(norm(name)) < 2:
            skipped += 1
            continue
        res = lookup(name, key)
        m, score = pick(name, res)
        if not m or score < MIN_SCORE:
            log(f"  跳过（没把握 {score}）: {f.name}  ← 清理后「{name}」")
            skipped += 1
            continue
        title = (m.get("title") or "").strip()
        year = m.get("year")
        safe = re.sub(r'[\\/:*?"<>|]', " ", title).strip()
        tagname = f"{safe} ({year})" if year else safe
        dest_dir = MOVIES / tagname
        dest = dest_dir / f"{tagname}{f.suffix}"
        if dest.exists():
            log(f"  跳过（目标已存在）: {dest.relative_to(MOVIES)}")
            skipped += 1
            continue
        log(f"  {'将要' if args.dry else ''}归位: {f.name} → {dest.relative_to(MOVIES)}  [score {score}]")
        if args.dry:
            done += 1
            continue
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dest))
            done += 1
        except OSError as exc:
            log(f"  移动失败 {f.name}: {exc}")

    # 清理空目录（下载器留下的垃圾文件夹）
    if not args.dry:
        for d in sorted(MOVIES.rglob("*"), key=lambda x: -len(str(x))):
            if d.is_dir():
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                except OSError:
                    pass
    log(f"完成：处理 {done}，跳过 {skipped}")
    if done and not args.dry:
        jf_scan()
    return 0


if __name__ == "__main__":
    sys.exit(main())
