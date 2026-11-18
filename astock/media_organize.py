#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体自动整理：把 tg-bot 下到 movies 里的剧集移到正确的电视剧目录，
然后触发 Jellyfin 扫描。

判定：
- 文件名含 SxxExx（如 S01E15）→ 剧集
- 剧名 = SxxExx 前面的部分（清洗广告词）
- 移动到 /opt/media/电视剧/{剧名}/Season 01/{剧名} SxxExx.mp4
- 其余文件留在 movies（由 media_cleanup.py 负责清洗电影名）

安全：
- 跳过 qBittorrent 任务中的文件
- 跳过 .!qB / .part
- 跳过最近 15 分钟内修改过的（可能还在下载）
- 目标已存在则跳过（不覆盖）

由 cron 每 2 小时跑一次。
"""
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import requests

MOVIES = Path("/opt/media/movies")
TV = Path("/opt/media/电视剧")
RECENT_SECONDS = 900
LOG = "/opt/astock/media_organize.log"

QB_URL = "http://127.0.0.1:8081"
QB_USER = "astock"
QB_PASS = "astock"

JF_DB = "/opt/jellyfin/config/data/jellyfin.db"
JF_URL = "http://127.0.0.1:8096"

# 剧集：名字里带 S01E15 / S1E15 这种
EPISODE_RE = re.compile(r"^(.*?)[\s._\-]*[Ss](\d{1,2})[Ee](\d{1,3})\b", re.I)
# 兜底：<缩写|剧名>-<集数>-00.00.00.xxx（TG/网盘命名）
NUM_RE = re.compile(r"^(?P<t>.{1,24}?)[-_ .]+(?P<ep>\d{1,3})[-_ .]+00[. ]00[. ]00")
# 中文剧名直接连集数：尚24 00 00 00 ...
NUM_CJK_RE = re.compile(r"^(?P<t>[\u4e00-\u9fa5]{1,12})(?P<ep>\d{1,3})[\s\-_]+00[. ]00[. ]00")
# 已知剧名缩写 → 电视剧目录名
ALIASES = {"SGZBRMY": "尚公主", "SGZ": "尚公主", "尚": "尚公主"}
# 清洗剧名里的广告
NOISE_RE = re.compile(
    r"6v电影|6v123|电影港|地址发布页|收藏不迷路|www[._][a-z0-9._-]+|"
    r"国语中字|国语|中字|简繁|双语|无水印|高码版|更新\d+",
    re.I,
)


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def qbit_active():
    """返回 qB 中任务名集合；无法确认返回 None。"""
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


def jf_token():
    """从 Jellyfin 数据库取 API key。"""
    try:
        con = sqlite3.connect(f"file:{JF_DB}?mode=ro", uri=True)
        row = con.execute("select AccessToken from ApiKeys limit 1").fetchone()
        con.close()
        return row[0] if row else ""
    except Exception:  # noqa: BLE001
        return ""


def trigger_scan():
    token = jf_token()
    if not token:
        log("  没找到 Jellyfin API key，跳过扫描触发")
        return False
    try:
        r = requests.post(f"{JF_URL}/Library/Refresh",
                          headers={"Authorization": f'MediaBrowser Token="{token}"'},
                          timeout=15)
        ok = r.status_code in (200, 204)
        log(f"  Jellyfin 扫描: {'已触发' if ok else f'失败 {r.status_code}'}")
        return ok
    except Exception as exc:  # noqa: BLE001
        log(f"  扫描触发异常: {type(exc).__name__}: {exc}")
        return False


def clean_title(s):
    s = NOISE_RE.sub(" ", s)
    s = re.sub(r"[._\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .-_")
    return s


def is_recent(p):
    try:
        return time.time() - p.stat().st_mtime < RECENT_SECONDS
    except OSError:
        return False




def _idx_write(path):
    """把媒体文件写进 media_index（失败静默）。"""
    try:
        import sqlite3
        con = sqlite3.connect("/opt/astock/astock.db", timeout=5)
        st = path.stat()
        con.execute(
            "INSERT OR REPLACE INTO media_index(path, title, size, matched) VALUES (?,?,?,1)",
            (str(path), path.name, st.st_size))
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass

def main():
    if not MOVIES.is_dir():
        return
    active = qbit_active()
    if active is None:
        log("qB API 不可用，跳过整理（避免误动正在下载的文件）")
        return

    moved = 0
    for f in sorted(MOVIES.iterdir()):
        if not f.is_file():
            continue
        if f.suffix.lower() not in (".mp4", ".mkv", ".avi", ".ts", ".mov", ".m4v", ".rmvb"):
            continue
        if f.suffix.lower() == ".!qb" or f.name.lower().endswith((".part", ".!qb")):
            continue
        if f.name in active:
            continue
        if is_recent(f):
            continue
        m = EPISODE_RE.match(f.stem)
        if m:
            raw_title = m.group(1).strip()
            season = int(m.group(2))
            ep = int(m.group(3))
            title = (ALIASES.get(raw_title.upper()) or ALIASES.get(raw_title)
                     or clean_title(raw_title))
        else:
            hit = None
            for rx in (NUM_CJK_RE, NUM_RE):
                cand = rx.match(f.stem)
                if not cand:
                    continue
                raw = cand.group("t").strip()
                cep = int(cand.group("ep"))
                ctitle = (ALIASES.get(raw.upper()) or ALIASES.get(raw)
                          or clean_title(raw))
                # 只有对得上已有剧集目录才动，避免给电影乱建目录
                if cep >= 1 and ctitle and (TV / ctitle).is_dir():
                    hit, title, ep = cand, ctitle, cep
                    break
            if not hit:
                continue   # 不是剧集，留给电影流程
            season = 1
        if not title:
            continue

        target_dir = TV / title / f"Season {season:02d}"
        target = target_dir / f"{title} S{season:02d}E{ep:02d}{f.suffix}"
        if target.exists():
            log(f"跳过（目标已存在）: {target.name}")
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(f), str(target))
            log(f"剧集归位: {f.name} → {target.relative_to(TV)}")
            _idx_write(target)
            moved += 1
        except OSError as exc:
            log(f"移动失败 {f.name}: {exc}")

    log(f"整理完成：移动 {moved} 个")
    if moved:
        trigger_scan()


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
            _lj("media_organize", "ok" if _rc == 0 else "fail")
        except Exception:
            pass
