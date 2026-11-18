#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每小时同步：自动把新监听的服务端口加到 Uptime Kuma"""
import re
import sqlite3
import subprocess

DB = "/var/lib/docker/volumes/uptime-kuma/_data/kuma.db"
# 这些端口不自动加（代理/内部服务）
SKIP = {7890, 7874, 9090, 6800, 40187, 49220, 7874}


def listening_ports():
    out = subprocess.run(["ss", "-ltn"], capture_output=True, text=True).stdout
    ports = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        addr = parts[3]
        if "127.0.0.1" in addr or "::1" in addr or "%lo" in addr:
            continue
        m = re.search(r":(\d+)$", addr)
        if m:
            ports.add(int(m.group(1)))
    return ports


def main():
    ports = listening_ports()
    if not ports:
        print("no ports")
        return

    subprocess.run(["docker", "stop", "uptime-kuma"], capture_output=True, check=False)
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    nid = cur.execute("SELECT id FROM notification WHERE name='企业微信'").fetchone()
    nid = nid[0] if nid else None

    existing = set()
    for (v,) in cur.execute("SELECT COALESCE(url, hostname || ':' || port) FROM monitor"):
        m = re.search(r":(\d+)/?$", str(v))
        if m:
            existing.add(int(m.group(1)))

    added = 0
    for port in sorted(ports - existing - SKIP):
        name = f"端口 {port}"
        cur.execute(
            """INSERT INTO monitor
               (name, active, user_id, interval, url, type, hostname, port,
                maxretries, retry_interval, accepted_statuscodes_json, method, timeout, description)
               VALUES (?, 1, 1, 120, NULL, 'port', '172.17.0.1', ?, 2, 60, '["200-399"]', 'GET', 10, ?)""",
            (name, port, "自动发现的服务端口"),
        )
        mid = cur.lastrowid
        if nid:
            cur.execute(
                "INSERT INTO monitor_notification (monitor_id, notification_id) VALUES (?, ?)",
                (mid, nid),
            )
        added += 1
        print(f"added port {port}")

    conn.commit()
    conn.close()
    subprocess.run(["docker", "start", "uptime-kuma"], capture_output=True, check=False)
    print(f"done, added {added}")


if __name__ == "__main__":
    main()
