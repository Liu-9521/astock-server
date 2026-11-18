#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qBittorrent 限速自动调节：服务器空闲时放开，繁忙时收紧。

判断依据：1 分钟负载 + 可用内存。
- 空闲（load < 2.0 且可用 > 1500MB）：下载 10MB/s、上传 2MB/s
- 繁忙：下载 2MB/s、上传 500KB/s

由 cron 每 5 分钟跑一次；qB 没运行或 API 失败时静默退出。
"""
import json
import time

import requests

BASE = "http://127.0.0.1:8081"
QB_USER = "astock"
QB_PASS = "astock"

IDLE_DL = 10 * 1024 * 1024      # 10 MB/s
IDLE_UP = 512 * 1024            # 500 KB/s
BUSY_DL = 2 * 1024 * 1024       # 2 MB/s
BUSY_UP = 200 * 1024            # 200 KB/s

LOG = "/opt/astock/qbit_autotune.log"


def read_load():
    try:
        return float(open("/proc/loadavg").read().split()[0])
    except (OSError, ValueError):
        return 99.0


def read_avail_mb():
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def main():
    load = read_load()
    avail = read_avail_mb()
    idle = load < 2.0 and avail > 1500

    pref = {
        "dl_limit": IDLE_DL if idle else BUSY_DL,
        "up_limit": IDLE_UP if idle else BUSY_UP,
    }
    mode = "空闲放开" if idle else "繁忙收紧"

    try:
        s = requests.Session()
        s.headers.update({"Referer": BASE + "/", "Origin": BASE})
        r = s.post(BASE + "/api/v2/auth/login",
                   data={"username": QB_USER, "password": QB_PASS}, timeout=8)
        if r.text.strip() != "Ok.":
            return
        cur = s.get(BASE + "/api/v2/app/preferences", timeout=8).json()
        if cur.get("dl_limit") == pref["dl_limit"] and cur.get("up_limit") == pref["up_limit"]:
            return   # 已经是目标值，不重复写
        s.post(BASE + "/api/v2/app/setPreferences",
               data={"json": json.dumps(pref)}, timeout=8)
        log(f"{mode}: load={load:.2f} avail={avail}MB → "
            f"下载 {pref['dl_limit']//1048576}MB/s 上传 {pref['up_limit']//1024}KB/s")
    except Exception:  # noqa: BLE001
        pass   # qB 没运行 / API 不通，静默


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
            _lj("qbit_autotune", "ok" if _rc == 0 else "fail")
        except Exception:
            pass
