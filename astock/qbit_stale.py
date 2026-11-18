#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qBittorrent 停滞任务清理：超过 N 小时没有任何活动、且未完成的下载，删除任务和文件。

判定：
- 任务状态是 downloading / queuedDL / stalledDL / metaDL（未完成）
- now - last_activity > STALE_HOURS（默认 3 小时）
- progress < 100%

删除时 deleteFiles=true（未完成的文件没有保留价值）。

cron 每小时跑一次。
"""
import time

import requests

BASE = "http://127.0.0.1:8081"
QB_USER = "astock"
QB_PASS = "astock"
STALE_HOURS = 3
LOG = "/opt/astock/qbit_stale.log"

# 只处理「真正在尝试下载」的状态：
#   downloading / stalledDL（有连接但没速度）/ metaDL（拿不到元数据）
# 明确排除 queuedDL（排队中，还没轮到它，不能算停滞）
ACTIVE_DL = {"downloading", "stalledDL", "metaDL", "forcedDL", "forcedMetaDL"}


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def main():
    try:
        s = requests.Session()
        s.headers.update({"Referer": BASE + "/", "Origin": BASE})
        r = s.post(BASE + "/api/v2/auth/login",
                   data={"username": QB_USER, "password": QB_PASS}, timeout=8)
        if r.text.strip() != "Ok.":
            return
        ts = s.get(BASE + "/api/v2/torrents/info", timeout=15).json()
    except Exception:  # noqa: BLE001
        return

    now = int(time.time())
    stale = []
    for t in ts:
        if t["state"] not in ACTIVE_DL:
            continue
        if (t.get("progress") or 0) >= 0.999:
            continue
        last = t.get("last_activity") or 0
        idle = now - last if last else 999999
        if idle > STALE_HOURS * 3600:
            stale.append((t, idle))

    if not stale:
        return

    log(f"发现 {len(stale)} 个停滞任务（>{STALE_HOURS} 小时无活动）")
    for t, idle in stale:
        log(f"  删除: {t['name'][:50]}  停滞 {idle/3600:.1f}h  "
            f"进度 {t['progress']*100:.1f}%")

    r = s.post(BASE + "/api/v2/torrents/delete",
               data={"hashes": "|".join(t["hash"] for t, _ in stale),
                     "deleteFiles": "true"}, timeout=30)
    log(f"  删除结果: {r.status_code}")


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
            _lj("qbit_stale", "ok" if _rc == 0 else "fail")
        except Exception:
            pass
