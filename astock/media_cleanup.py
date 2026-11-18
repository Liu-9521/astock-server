#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体目录维护：文件名/目录名清洗 + 空间清理。

清洗：
- 目录名去掉 6v/电影港等站点广告词
- 目录内的视频文件也清洗（如果是单文件）
- 字幕、nfo 等非视频文件不动
清理：
- 按顶层条目（文件或目录）的 mtime 从旧到新删除，直到 <= LIMIT_GB
- 跳过：含 [KEEP] 的、含未完成文件（.!qB / .part）的
"""
import argparse
import os
import re
import time
from pathlib import Path

import requests

MOVIES = Path("/opt/media/movies")
LIMIT_GB = 260
KEEP_TAG = "[KEEP]"
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".ts", ".mov", ".flv", ".wmv", ".m4v", ".rmvb", ".webm"}
PARTIAL_EXT = {".!qb", ".part", ".!ut"}
LOG = "/opt/astock/media_cleanup.log"

QB_URL = "http://127.0.0.1:8081"
QB_USER = "astock"
QB_PASS = "astock"
# 最近 N 秒内修改过的文件跳过（可能是 tg-bot 正在写入的下载文件）
RECENT_SECONDS = 900


def is_recent(p: Path) -> bool:
    try:
        return time.time() - p.stat().st_mtime < RECENT_SECONDS
    except OSError:
        return False


def qbit_active_names():
    """返回 qBittorrent 里现有任务的名字集合。

    返回 None 表示**无法确认**（qB 没运行或 API 失败）；返回空 set 表示确认没有任务。
    调用方必须区分这两种情况：无法确认时不要冒险改名/删除。
    """
    try:
        s = requests.Session()
        s.headers.update({"Referer": QB_URL + "/", "Origin": QB_URL})
        r = s.post(QB_URL + "/api/v2/auth/login",
                   data={"username": QB_USER, "password": QB_PASS}, timeout=8)
        if r.text.strip() != "Ok.":
            return None
        ts = s.get(QB_URL + "/api/v2/torrents/info", timeout=10).json()
        return {t["name"] for t in ts}
    except Exception:  # noqa: BLE001
        return None


def _w(p):
    """包装成「前后不是英文字母/数字」的边界，中文旁也能命中。"""
    return rf"(?<![A-Za-z0-9]){p}(?![A-Za-z0-9])"


NOISE_PATTERNS = [
    r"6v电影", r"6v123", r"6v", r"电影港", r"dygang", r"地址发布页", r"收藏不迷路",
    r"最新电影",
    _w("2160p"), _w("1080p"), _w("720p"), _w("4k"), _w("hd"), _w("60fps"),
    _w("bd"), _w("bluray"), _w("web[- ]?dl"), _w("hdtv"), _w("hevc"),
    _w("x26[45]"), _w("h[._]?26[45]"), _w("10bit"), _w("dts"), _w("aac"),
    r"国语中字", r"国语", r"中字", r"简繁", r"简日", r"双语", r"国粤", r"无水印",
    r"国语音轨", r"简繁英字幕", r"内嵌", r"外挂",
    r"更新\d+", r"未完待续", r"第\d+[-~]?\d*集?",
]
NOISE_RE = re.compile("|".join(NOISE_PATTERNS), re.I)
BRACKETS_RE = re.compile(r"[\[【(（][^\]】)）]*[\]】)）]")
# 域名类：www.xxx.com / www_xxx_com / xxx.com
DOMAIN_RE = re.compile(
    r"www[._][a-z0-9._-]+|[a-z0-9-]+\.(?:com|net|cc|me|top|xyz|org)", re.I)


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def clean_text(stem: str) -> str | None:
    """清洗一段名字；无变化或清洗后无有效字符返回 None。"""
    if KEEP_TAG in stem:
        return None
    s = BRACKETS_RE.sub(" ", stem)
    s = DOMAIN_RE.sub(" ", s)              # 先删域名（下划线形式也能命中）
    s = re.sub(r"[._\-]+", " ", s)         # 再统一分隔符，让 \b 边界生效
    s = NOISE_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(" .-_")
    if not s or s == stem:
        return None
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", s):
        return None
    if len(s) > 80:
        return None
    return s


def is_partial(path: Path) -> bool:
    return path.suffix.lower() in PARTIAL_EXT


def has_partial(directory: Path) -> bool:
    for root, _, files in os.walk(directory):
        for f in files:
            if Path(f).suffix.lower() in PARTIAL_EXT:
                return True
    return False


def rename_pass(dry=False):
    changed = 0
    active = qbit_active_names()
    if active is None:
        log("qB API 不可用，跳过重命名（避免误改正在做种的文件）")
        return 0
    if active:
        log(f"qBittorrent 有 {len(active)} 个任务，对应文件将跳过重命名")

    # 1) 目录名
    for d in sorted(MOVIES.iterdir()):
        if not d.is_dir():
            continue
        if d.name in active:
            log(f"跳过（qB 任务中）: {d.name}")
            continue
        if is_recent(d):
            log(f"跳过（最近有改动，可能正在下载）: {d.name}")
            continue
        if has_partial(d):
            log(f"跳过（有未完成文件）: {d.name}")
            continue
        new = clean_text(d.name)
        if not new:
            continue
        target = d.with_name(new)
        if target.exists():
            continue
        log(f"{'[dry] ' if dry else ''}目录重命名: {d.name}  ->  {new}")
        if not dry:
            try:
                d.rename(target)
            except OSError as exc:
                log(f"  失败: {exc}")
                continue
        changed += 1

    # 2) 目录内/根目录的视频文件
    for f in sorted(MOVIES.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXT:
            continue
        if f.parent.name in active:
            continue
        if f.name in active:
            continue
        if is_recent(f):
            log(f"跳过（最近有改动，可能正在下载）: {f.name}")
            continue
        new = clean_text(f.stem)
        if not new:
            continue
        target = f.with_name(new + f.suffix)
        if target.exists():
            continue
        log(f"{'[dry] ' if dry else ''}文件重命名: {f.name}  ->  {target.name}")
        if not dry:
            try:
                f.rename(target)
            except OSError as exc:
                log(f"  失败: {exc}")
                continue
        changed += 1

    log(f"名称清洗完成：{changed} 个")
    return changed


def entry_size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def cleanup_pass(dry=False):
    entries = [p for p in MOVIES.iterdir()]
    active = qbit_active_names()
    if active is None:
        log("qB API 不可用，跳过空间清理（无法确认哪些文件正在使用）")
        return 0
    limit = LIMIT_GB * 1024 ** 3
    total = 0
    for p in entries:
        try:
            total += entry_size(p)
        except OSError:
            pass
    log(f"当前 {total / 1024**3:.1f} GB，上限 {LIMIT_GB} GB，条目 {len(entries)} 个"
        f"{f'，qB 任务 {len(active)} 个' if active else ''}")

    if total <= limit:
        return 0

    def mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0

    entries.sort(key=mtime)
    removed = 0
    for p in entries:
        if total <= limit:
            break
        if KEEP_TAG in p.name:
            log(f"保留（收藏）: {p.name}")
            continue
        if p.name in active:
            log(f"跳过（qB 任务中）: {p.name}")
            continue
        if is_recent(p):
            log(f"跳过（最近有改动，可能正在下载）: {p.name}")
            continue
        if p.is_dir() and has_partial(p):
            log(f"跳过（下载中）: {p.name}")
            continue
        if p.is_file() and is_partial(p):
            continue
        # 目录含多个视频（剧集）时，按文件删最旧的，而不是删整个目录
        if p.is_dir():
            vids = [x for x in p.rglob("*") if x.is_file() and x.suffix.lower() in VIDEO_EXT]
            if len(vids) > 1:
                vids.sort(key=lambda x: x.stat().st_mtime)
                victim = vids[0]
                try:
                    vsize = victim.stat().st_size
                except OSError:
                    vsize = 0
                log(f"{'[dry] ' if dry else ''}删除（目录内最旧文件）: {victim.name}  ({vsize / 1024**3:.2f} GB)")
                if not dry:
                    try:
                        victim.unlink()
                    except OSError as exc:
                        log(f"  删除失败: {exc}")
                        continue
                total -= vsize
                removed += 1
                continue

        size = entry_size(p)
        log(f"{'[dry] ' if dry else ''}删除: {p.name}  ({size / 1024**3:.2f} GB)")
        if not dry:
            try:
                if p.is_dir():
                    import shutil
                    shutil.rmtree(p)
                else:
                    p.unlink()
            except OSError as exc:
                log(f"  删除失败: {exc}")
                continue
        total -= size
        removed += 1

    log(f"空间清理完成：删除 {removed} 个，剩余 {total / 1024**3:.1f} GB")
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rename-only", action="store_true")
    ap.add_argument("--clean-only", action="store_true")
    args = ap.parse_args()

    MOVIES.mkdir(parents=True, exist_ok=True)
    if not args.clean_only:
        rename_pass(args.dry_run)
    if not args.rename_only:
        cleanup_pass(args.dry_run)


if __name__ == "__main__":
    _rc = 0
    try:
        main()
    except Exception as _exc:
        _rc = 1
        raise
    finally:
        try:
            import sys as _s
            _s.path.insert(0, "/opt/astock")
            from db_log import log_job as _lj
            _lj("media_cleanup", "ok" if _rc == 0 else "fail")
        except Exception:
            pass
