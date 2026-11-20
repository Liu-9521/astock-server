#!/usr/bin/env python3
"""全市场 ETF 日线采集器：拉列表 → 逐个取日线 → 写入 kline.db。

- 每天收盘后跑一次（crontab: 30 15 * * 1-5）
- bars 表主键 (symbol, period, dt) 天然去重，INSERT OR REPLACE 增量更新
- 失败逐只重试 2 次，限速 0.1s 避免被限流
"""
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "kline.db"
LOG = BASE / "etf_collector.log"

HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
LIST_URL = ("https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1"
            "&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f12"
            "&fs=b:MK0021,b:MK0022&fields=f12,f14,f6")
KLINE = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService"
         ".getKLineData?symbol={sym}&scale=240&ma=no&datalen=1023")


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def etf_list():
    """返回 [(sym, name, amount)]，按成交额降序。"""
    first = None
    for _ in range(3):
        try:
            req = urllib.request.Request(LIST_URL.format(pn=1), headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                first = json.loads(r.read().decode("utf-8", "ignore"))
            break
        except Exception as e:
            log(f"列表首页失败 {e}")
            time.sleep(3)
    if not first or not first.get("data"):
        return []
    total = first["data"].get("total", 0)
    items = list(first["data"].get("diff") or [])
    pn = 2
    while len(items) < total and pn <= 25:
        try:
            req = urllib.request.Request(LIST_URL.format(pn=pn), headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode("utf-8", "ignore"))
            page = (d.get("data") or {}).get("diff") or []
            if not page:
                break
            items += page
        except Exception:
            break
        pn += 1
        time.sleep(0.05)

    out = []
    for it in items:
        code = str(it.get("f12") or "").strip()
        name = str(it.get("f14") or "").strip()
        if len(code) != 6:
            continue
        if code[0] in ("5", "6"):
            sym = "sh" + code
        elif code[0] in ("1", "2", "3"):
            sym = "sz" + code
        else:
            continue
        try:
            amt = float(it.get("f6") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        out.append((sym, name, amt))
    out.sort(key=lambda x: x[2], reverse=True)
    return out


def bars(sym):
    for attempt in range(3):
        try:
            req = urllib.request.Request(KLINE.format(sym=sym), headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode("utf-8", "ignore")
            d = json.loads(raw)
            if not isinstance(d, list):
                return None
            rows = []
            for b in d:
                try:
                    rows.append((sym, "day", b["day"][:10], float(b["open"]),
                                 float(b["high"]), float(b["low"]), float(b["close"])))
                except Exception:
                    continue
            return rows or None
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1.0)
    return None


def main():
    t0 = time.time()
    log("===== ETF 日线采集开始 =====")
    pool = etf_list()
    if not pool:
        log("!! 取不到 ETF 列表，退出")
        return 1
    log(f"ETF 列表 {len(pool)} 只")

    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS bars(
        symbol TEXT, period TEXT, dt TEXT,
        o REAL, h REAL, l REAL, c REAL,
        PRIMARY KEY(symbol, period, dt))""")

    ok = fail = 0
    rows_total = 0
    for idx, (sym, name, _amt) in enumerate(pool, 1):
        rows = bars(sym)
        if rows and len(rows) >= 60:
            cur.executemany(
                "INSERT OR REPLACE INTO bars(symbol,period,dt,o,h,l,c) "
                "VALUES(?,?,?,?,?,?,?)", rows)
            rows_total += len(rows)
            ok += 1
        else:
            fail += 1
        if idx % 200 == 0:
            con.commit()
            log(f"  进度 {idx}/{len(pool)}  成功 {ok} 失败 {fail} 入库 {rows_total:,} 行")
        time.sleep(0.1)

    con.commit()
    n_sym = cur.execute("SELECT COUNT(DISTINCT symbol) FROM bars WHERE period='day'").fetchone()[0]
    n_row = cur.execute("SELECT COUNT(*) FROM bars WHERE period='day'").fetchone()[0]
    con.close()
    log(f"完成：成功 {ok} 失败 {fail}，本次写入 {rows_total:,} 行，"
        f"库内日线 {n_sym} 只 / {n_row:,} 行，耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
