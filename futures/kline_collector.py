#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期货 K 线增量落库：把 30 分钟与日线写入 SQLite，供长期回测使用。

数据库：/home/astock/.dsh/dsh_wechat/futures/kline.db
表 bars(symbol, period, dt, o, h, l, c)，主键 (symbol, period, dt) 天然去重。
crontab：交易时段每 30 分钟抓一次。
"""
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import futures_sim as fs

DB = BASE / "kline.db"

# 需要长期积累的品种（新浪合约代码前缀）
PRODUCTS = {
    "C": ("玉米", 10, 1),
    "M": ("豆粕", 10, 1),
    "RB": ("螺纹钢", 10, 1),
    "P": ("棕榈油", 10, 2),
    "Y": ("豆油", 10, 2),
    "MA": ("甲醇", 10, 1),
    "I": ("铁矿石", 100, 0.5),
    "SR": ("白糖", 10, 1),
    "RM": ("菜粕", 10, 1),
}
PERIODS = ("30", "day")


def ensure_products():
    for code, (name, mult, tick) in PRODUCTS.items():
        if code not in fs.FUTURES:
            fs.FUTURES[code] = {
                "sym": code + "0", "prefix": code, "code": "nf_" + code + "0",
                "name": name, "mult": mult, "tick": tick, "margin": 0.10,
                "night": ("21:00", "23:00"),
            }


def init_db(con):
    con.execute("""CREATE TABLE IF NOT EXISTS bars(
        symbol TEXT NOT NULL,
        period TEXT NOT NULL,
        dt     TEXT NOT NULL,
        o REAL, h REAL, l REAL, c REAL,
        PRIMARY KEY (symbol, period, dt))""")
    con.commit()


def upsert(con, symbol, period, bars):
    rows = [(symbol, period, str(b["d"]),
             float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]))
            for b in bars if b.get("d")]
    if not rows:
        return 0
    con.executemany(
        "INSERT OR REPLACE INTO bars(symbol, period, dt, o, h, l, c) VALUES (?,?,?,?,?,?,?)",
        rows)
    con.commit()
    return len(rows)


def main():
    ensure_products()
    con = sqlite3.connect(DB, timeout=15)
    init_db(con)

    ok = fail = wrote = 0
    for code in PRODUCTS:
        for period in PERIODS:
            n = 1100 if period != "day" else 1000
            try:
                bars = fs.fetch_kline(code, period, n)
                wrote += upsert(con, code, period, bars)
                ok += 1
            except Exception as e:
                fail += 1
                print(f"[{code} {period}] 失败: {e}", file=sys.stderr)

    total = con.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    span = con.execute(
        "SELECT MIN(dt), MAX(dt) FROM bars WHERE period='30'").fetchone()
    print(f"{fs.now_str() if hasattr(fs, 'now_str') else ''}"
          f"成功 {ok} 项 / 失败 {fail} 项，写入 {wrote} 根，库中共 {total} 根"
          f"（30分钟区间 {span[0]} ~ {span[1]}）")
    con.close()


if __name__ == "__main__":
    main()
