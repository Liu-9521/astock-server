#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""现货金自动策略引擎：日线 MA5/20 + 60分钟 MA10/30，只做多、无杠杆。

每 30 秒 tick 一次：多头排列满仓持有，空头排列清仓。
状态写入 gold_algo.json，由 webapp 的 /api/gold/algo 读出展示。
"""

import json
import re
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE = BASE / "gold_algo.json"
LOG = BASE / "gold_algo.log"
POLL = 30
INIT = 50000.0

HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0",
}
DAY_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_AU0=/"
           "InnerFuturesNewService.getDailyKLine?symbol=AU0")
MIN_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_AU0M=/"
           "InnerFuturesNewService.getFewMinLine?symbol=AU0&type=60")

STRATS = {
    "day": {"label": "日线 MA5/20", "url": DAY_URL, "fast": 5, "slow": 20},
    "m60": {"label": "60分钟 MA10/30", "url": MIN_URL, "fast": 10, "slow": 30},
}


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def default_state():
    return {
        "initial": INIT,
        "price": 0.0,
        "updated": "",
        "day": {"cash": INIT, "qty": 0.0, "cost": 0.0, "sig": 0,
                "enabled": True, "trades": []},
        "m60": {"cash": INIT, "qty": 0.0, "cost": 0.0, "sig": 0,
                "enabled": True, "trades": []},
    }


def load_state():
    if STATE.exists():
        try:
            d = json.loads(STATE.read_text(encoding="utf-8"))
            base = default_state()
            for k in ("day", "m60"):
                if k not in d:
                    d[k] = base[k]
            d.setdefault("initial", INIT)
            return d
        except Exception as e:
            log("状态读取失败，重置：%s" % e)
    return default_state()


def save_state(d):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def fetch(url):
    raw = urllib.request.urlopen(
        urllib.request.Request(url, headers=HEADERS), timeout=15
    ).read().decode("gbk", "ignore")
    m = re.search(r"\((\[.*\])\)", raw, re.S)
    if not m:
        return []
    arr = json.loads(m.group(1))
    out = []
    for r in arr:
        try:
            if isinstance(r, dict):
                out.append({"d": str(r["d"]), "c": float(r["c"])})
            else:
                out.append({"d": str(r[0]), "c": float(r[4])})
        except Exception:
            continue
    out.sort(key=lambda x: x["d"])
    return out


def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def tick(d):
    price = None
    for key, cfg in STRATS.items():
        rows = fetch(cfg["url"])
        if len(rows) < cfg["slow"] + 3:
            log("%s 数据不足 %d 根，跳过" % (key, len(rows)))
            continue
        p = rows[-1]["c"]
        price = p
        closes = [r["c"] for r in rows]
        f = sma(closes, cfg["fast"])
        s = sma(closes, cfg["slow"])
        if f is None or s is None:
            continue
        sig = 1 if f > s else -1
        st = d[key]
        st["sig"] = sig
        st["fast"] = round(f, 3)
        st["slow"] = round(s, 3)
        st["fast_n"] = cfg["fast"]
        st["slow_n"] = cfg["slow"]
        if not st.get("enabled", True):
            continue
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if sig == 1 and st["qty"] <= 0 and st["cash"] > p:
            qty = st["cash"] / p
            st["qty"] = round(qty, 4)
            st["cost"] = round(p, 3)
            st["cash"] = 0.0
            st["trades"].insert(0, {"t": now, "act": "买入", "price": round(p, 3),
                                    "qty": round(qty, 4), "pnl": None, "reason": "金叉"})
            log("[%s] 买入 %.4f 克 @ %.2f" % (key, qty, p))
        elif sig == -1 and st["qty"] > 0:
            pnl = (p - st["cost"]) * st["qty"]
            st["trades"].insert(0, {"t": now, "act": "卖出", "price": round(p, 3),
                                    "qty": st["qty"], "pnl": round(pnl, 2), "reason": "死叉"})
            st["cash"] = round(st["qty"] * p, 2)
            st["qty"] = 0.0
            st["cost"] = 0.0
            log("[%s] 卖出 盈亏 %+.2f @ %.2f" % (key, pnl, p))
        st["trades"] = st["trades"][:60]
    d["price"] = round(price if price else (d.get("price") or 0), 3)
    d["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return d


def main():
    log("现货金自动策略启动")
    while True:
        try:
            save_state(tick(load_state()))
        except Exception as e:
            log("tick 异常：%s: %s" % (type(e).__name__, e))
        time.sleep(POLL)


if __name__ == "__main__":
    main()
