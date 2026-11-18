#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体文件自动整理：从文件名解析剧名/季/集，归档到 Jellyfin 能识别的目录结构。

只认明确的剧集标记：
    剧名 S01E05 / 剧名.s01e05 / 剧名_第05集 / 剧名 EP05
其它一律跳过（电影、时间戳命名、模糊命名），避免误判。

归档到：
    /opt/media/电视剧/<剧名>/Season 01/<剧名> S01E05.mp4

用法：
    python3 organize_media.py --dry-run      # 只看会怎么整理，不动文件
    python3 organize_media.py                # 真正执行
    python3 organize_media.py <单个文件>      # 只处理一个文件（TG bot 用）
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

MEDIA = Path("/opt/media")
MOVIES = MEDIA / "movies"
TV = MEDIA / "电视剧"

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".ts", ".mov", ".flv", ".wmv", ".m4v", ".rmvb", ".webm"}

# 噪声词：清晰度/编码/来源
NOISE = re.compile(
    r"(4k|8k|2160p|1080p|720p|60fps|web[\s._-]?dl|bluray|hdtv|h\.?26[45]|hevc|"
    r"x26[45]|10bit|aac|ddp|dd5[\s._-]?1|dts|adweb|webrip|remux|proper|repack|"
    r"国语|中字|简中|繁中|内嵌|外挂|未删减)",
    re.I,
)

# 时间戳/毫秒噪声，如 00.00.00.000
TIMESTAMP = re.compile(r"\d{2}[.:]\d{2}[.:]\d{2}[.:]\d{3}")


def clean_title(raw: str) -> str:
    t = NOISE.sub(" ", raw)
    t = TIMESTAMP.sub(" ", t)
    t = re.sub(r"[._\-]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # 去掉纯英文尾巴（"早春晴朗 The Early Spring" → "早春晴朗"）
    m = re.match(r"^([\u4e00-\u9fff0-9A-Za-z]+?)\s+[A-Za-z][A-Za-z\s]*$", t)
    if m and re.search(r"[\u4e00-\u9fff]", m.group(1)):
        t = m.group(1)
    return t


def parse_name(filename: str):
    """解析文件名 → (剧名, 季, 集)；不是明确的剧集返回 None。"""
    path = Path(filename)
    if path.suffix.lower() not in VIDEO_EXT:
        return None
    base = path.stem
    if TIMESTAMP.search(base):
        return None

    title, season, ep = "", 1, None

    m = re.search(r"[Ss](\d{1,2})[\s._-]?[Ee][Pp]?(\d{1,3})", base)
    if m:
        season, ep = int(m.group(1)), int(m.group(2))
        title = base[: m.start()]
    else:
        m = re.search(r"第\s*(\d{1,3})\s*[集话期]", base)
        if m:
            ep = int(m.group(1))
            title = base[: m.start()]
        else:
            m = re.search(r"(?:^|[._\s-])EP?(\d{1,3})(?:[._\s-]|$)", base, re.I)
            if m:
                ep = int(m.group(1))
                title = base[: m.start()]

    if ep is None or ep > 2000:
        return None

    title = clean_title(title)
    # 剧名至少 1 个有效字符（允许"尚"这类缩写），但不能是纯数字
    if not title or not re.search(r"[A-Za-z\u4e00-\u9fff]", title):
        return None
    if re.fullmatch(r"\d+", title):
        return None
    return title, season, ep


def organize(path: Path, dry: bool = False) -> Path | None:
    """整理单个文件；返回目标路径（不是剧集返回 None）。"""
    parsed = parse_name(path.name)
    if not parsed:
        return None
    title, season, ep = parsed
    dest_dir = TV / title / f"Season {season:02d}"
    dest = dest_dir / f"{title} S{season:02d}E{ep:02d}{path.suffix}"
    if dest.exists():
        dest = dest_dir / f"{dest.stem}_dup{path.suffix}"
    if dry:
        return dest
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    files = [Path(f) for f in args.files] if args.files else sorted(
        p for p in MOVIES.glob("*") if p.is_file()
    )
    moved = 0
    for p in files:
        dest = organize(p, dry=args.dry_run)
        if dest:
            moved += 1
            print(f"{'[dry] ' if args.dry_run else ''}{p.name}  ->  {dest}")
        else:
            print(f"跳过（不是明确剧集）：{p.name}")
    print(f"\n共处理 {len(files)} 个，识别成功 {moved} 个")


if __name__ == "__main__":
    sys.exit(main())
