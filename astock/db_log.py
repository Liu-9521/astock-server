#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""astock 数据库记录工具（只用标准库，任何 python 环境都能 import）。

用法：
    from db_log import log_trade, log_alert, log_download, log_job
    log_trade("futures", "AU0", "沪金", "buy", 870.5, 1, reason="突破")
    log_alert("stock", "600519", "upper", "贵州茅台突破 1500")
    log_download("tg", "电影.mp4", "/opt/media/movies/电影.mp4", 1234567890, "ok")
    log_job("media_cleanup", "ok", "清理 3 个")
"""
import sqlite3

DB = "/opt/astock/astock.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    code TEXT,
    name TEXT,
    side TEXT,
    price REAL,
    qty REAL,
    pnl REAL,
    reason TEXT,
    ts TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    code TEXT,
    level TEXT,
    message TEXT,
    ts TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    name TEXT,
    path TEXT,
    size INTEGER,
    status TEXT,
    ts TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_dl_ts ON downloads(ts);

CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    status TEXT,
    detail TEXT,
    ts TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_ts ON job_runs(ts);
"""


def _conn():
    con = sqlite3.connect(DB, timeout=8)
    con.executescript(SCHEMA)
    return con


def _insert(sql, args):
    try:
        con = _conn()
        con.execute(sql, args)
        con.commit()
        con.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def log_trade(source, code, name="", side="", price=None, qty=None,
              pnl=None, reason=None):
    return _insert(
        "INSERT INTO trades(source,code,name,side,price,qty,pnl,reason) VALUES (?,?,?,?,?,?,?,?)",
        (source, code, name, side, price, qty, pnl, reason))


def log_alert(kind, code="", level="", message=""):
    return _insert(
        "INSERT INTO alerts(kind,code,level,message) VALUES (?,?,?,?)",
        (kind, code, level, message))


def log_download(source, name, path="", size=None, status="ok"):
    return _insert(
        "INSERT INTO downloads(source,name,path,size,status) VALUES (?,?,?,?,?)",
        (source, name, path, size, status))


def log_job(job, status="ok", detail=""):
    return _insert(
        "INSERT INTO job_runs(job,status,detail) VALUES (?,?,?)",
        (job, status, detail))


if __name__ == "__main__":
    con = _conn()
    for t in ("trades", "alerts", "downloads", "job_runs"):
        n = con.execute(f"select count(*) from {t}").fetchone()[0]
        print(f"  {t}: {n}")
    con.close()
