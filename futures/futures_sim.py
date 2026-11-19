#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期货自动模拟盘（玉米 / 豆粕 / 螺纹钢，主力合约、夜盘、多空、保证金、策略自动交易）。

数据源：
- 实时：新浪 hq.sinajs.cn（nf_C2701 等主力合约，自动识别换月）
- 日线：新浪 InnerFuturesNewService.getDailyKLine

端口 8084，纯标准库，内存约 30~50MB。

自动交易：每个品种绑定策略（双均线 / RSI / 突破），盘中按日线信号自动开平仓。
"""
from __future__ import annotations

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.expanduser("~/.dsh"))
try:
    from secrets_lib import get as _secret
except Exception:
    def _secret(name, default=""):
        return default

import sys
sys.path.insert(0, '/opt/astock')
from db_log import log_trade

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_FILE = BASE / "futures_data.json"
PORT = 8084
POLL = 10
KLINE_TTL = 300

QUOTE_URL = "https://hq.sinajs.cn/list={codes}"
KLINE_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{sym}=/"
             "InnerFuturesNewService.getDailyKLine?symbol={sym}")
KLINE_MIN_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{sym}=/"
                 "InnerFuturesNewService.getFewMinLine?symbol={sym}&type={period}")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://finance.sina.com.cn",
}

FUTURES = {
    "C": {"sym": "C0", "prefix": "C", "name": "玉米", "mult": 10, "tick": 1,
          "margin": 0.10, "night": ("21:00", "23:00")},
    "M": {"sym": "M0", "prefix": "M", "name": "豆粕", "mult": 10, "tick": 1,
          "margin": 0.10, "night": ("21:00", "23:00")},
    "RB": {"sym": "RB0", "prefix": "RB", "name": "螺纹钢", "mult": 10, "tick": 1,
           "margin": 0.09, "night": ("21:00", "23:00")},
    # 同一份玉米合约的第二套策略（日线周期），与 30 分钟版并行对照
    "C_DAY": {"sym": "C0", "prefix": "C", "name": "玉米", "label": "玉米·日线",
              "mult": 10, "tick": 1, "margin": 0.10, "night": ("21:00", "23:00")},
    # 同一份豆粕合约的第二套策略（60 分钟周期），与 30 分钟版并行对照
    "M_60": {"sym": "M0", "prefix": "M", "name": "豆粕", "label": "豆粕·60分",
             "mult": 10, "tick": 1, "margin": 0.10, "night": ("21:00", "23:00")},
}

MAIN = {}          # 品种 -> (ts, 主力合约代码)，例如 "C" -> (ts, "C2701")
MAIN_TTL = 1800    # 主力合约缓存 30 分钟

STRATEGIES = {
    "ma": {"label": "双均线", "params": {"fast": 5, "slow": 20}},
    "rsi": {"label": "RSI", "params": {"period": 14, "low": 30, "high": 70}},
    "breakout": {"label": "突破", "params": {"n": 20}},
}

STATE = {"quotes": {}, "last_tick": "", "last_error": ""}
MAIN_NAME = {}     # 品种 -> 展示名（带月份），例如 "C" -> "玉米2701"
KLINE = {}      # contract -> (ts, rows)
LOCK = threading.Lock()


# ---------------- 数据 ----------------
def default_data() -> dict:
    return {
        "account": {"initial": 12000.0, "realized": 0.0},
        "positions": {},
        "trades": [],
        "logs": [],
        "strategy": {
            # 玉米·30分钟（原参数）：保留观察，不实际下单
            "C": {"name": "ma", "params": {"fast": 5, "slow": 20}, "period": "30",
                  "enabled": False, "stop_win": 2.0, "stop_loss": 1.0, "filter": 120},
            # 玉米·日线：4 年回测前后段都盈利的参数，实际下单
            "C_DAY": {"name": "ma", "params": {"fast": 3, "slow": 15}, "period": "day",
                      "enabled": True, "stop_win": 3.0, "stop_loss": 1.5},
            "M": {"name": "ma", "params": {"fast": 5, "slow": 20}, "period": "30",
                  "enabled": True, "stop_win": 2.0, "stop_loss": 1.0},
            # 豆粕·60分钟：8 个月回测 42 笔 +3996 胜率 57.1%，三档过滤均为正
            "M_60": {"name": "ma", "params": {"fast": 3, "slow": 15}, "period": "60",
                     "enabled": True, "stop_win": 3.0, "stop_loss": 1.5, "filter": 120},
            # 螺纹钢：30 分钟三档过滤全负，日线 5/20 是 +2966，改日线
            "RB": {"name": "ma", "params": {"fast": 5, "slow": 20}, "period": "day",
                   "enabled": True, "stop_win": 2.0, "stop_loss": 1.0},
        },
    }


def load_data() -> dict:
    with LOCK:
        if not DATA_FILE.exists():
            d = default_data()
            DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            return d
        try:
            d = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            d = default_data()
        st = d.setdefault("strategy", {})
        # 老数据迁移：删掉已下架品种（如 AU），补齐新上架品种
        for k in [k for k in st if k not in FUTURES]:
            st.pop(k, None)
        _defaults = default_data()["strategy"]
        for k in FUTURES:
            st.setdefault(k, dict(_defaults.get(k) or _defaults["C"]))
        return d


def save_data(d: dict) -> None:
    with LOCK:
        DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def log(d: dict, msg: str) -> None:
    d.setdefault("logs", []).insert(0, {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "msg": msg})
    d["logs"] = d["logs"][:200]


# ---------------- 行情 ----------------
def _get(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="replace")


def candidate_contracts(prefix: str, months: int = 16) -> list:
    """从当月起往后 N 个月的候选合约代码（C2611、C2612 …）。"""
    t = time.localtime()
    out = []
    for i in range(months):
        mm = t.tm_mon + i
        yy = t.tm_year + (mm - 1) // 12
        mm = (mm - 1) % 12 + 1
        out.append(f"{prefix}{yy % 100:02d}{mm:02d}")
    return out


def pick_main(contract: str, force: bool = False) -> str:
    """返回主力合约代码（如 C2701）：候选月份里持仓量最大的那个，缓存 30 分钟。"""
    f = FUTURES.get(contract)
    if not f:
        return contract
    hit = MAIN.get(contract)
    now = time.time()
    if hit and not force and now - hit[0] < MAIN_TTL:
        return hit[1]
    best = hit[1] if hit else f["sym"]
    best_oi = -1.0
    try:
        raw = _get(QUOTE_URL.format(codes=",".join("nf_" + c for c in candidate_contracts(f["prefix"]))))
        for line in raw.splitlines():
            if '="' not in line:
                continue
            code = line.split("=", 1)[0].replace("var hq_str_nf_", "").strip()
            parts = line.split('="', 1)[1].rstrip('";').split(",")
            if len(parts) < 15 or not parts[0].strip():
                continue
            try:
                oi = float(parts[13] or 0)      # 持仓量
            except ValueError:
                continue
            if oi > best_oi:
                best, best_oi = code, oi
    except Exception:
        pass
    MAIN[contract] = (now, best)
    return best


def main_name(contract: str) -> str:
    """带月份的展示名（玉米2701）；取不到时退回品种名。"""
    f = FUTURES.get(contract)
    if not f:
        return contract
    q = STATE["quotes"].get("nf_" + pick_main(contract)) or {}
    nm = (q.get("name") or "").strip()
    if nm and nm != f["name"] + "连续":
        MAIN_NAME[contract] = nm
        return nm
    return MAIN_NAME.get(contract) or f["name"]


def fetch_quotes() -> dict:
    codes = ",".join("nf_" + pick_main(k) for k in FUTURES)
    raw = _get(QUOTE_URL.format(codes=codes))
    out = {}
    for line in raw.splitlines():
        if '="' not in line:
            continue
        key = line.split("=", 1)[0].replace("var hq_str_", "").strip()
        body = line.split('="', 1)[1].rstrip('";')
        parts = body.split(",")
        if len(parts) < 11:
            continue
        try:
            # 新浪 nf_ 字段：[2]开 [3]高 [4]低 [5]昨收(期货为空) [6]买 [7]卖
            # [8]最新价 [9]结算 [10]昨结算 [13]持仓量 [14]成交量
            price = (float(parts[8] or 0) or float(parts[7] or 0)
                     or float(parts[6] or 0) or float(parts[2] or 0))
            prev = float(parts[10] or 0) or float(parts[5] or 0) or price
        except ValueError:
            continue
        if price <= 0:
            continue
        out[key] = {
            "name": parts[0], "price": price,
            "open": float(parts[2] or 0), "high": float(parts[3] or 0),
            "low": float(parts[4] or 0), "prev": prev,
            "chg": round((price / prev - 1) * 100, 2) if prev else 0.0,
        }
        if len(parts) > 13:
            try:
                out[key]["oi"] = float(parts[13] or 0)
            except ValueError:
                pass
    return out


def fetch_kline(contract: str, period: str = "day", n: int = 160) -> list:
    f = FUTURES[contract]
    if period in ("5", "15", "30", "60"):
        url = KLINE_MIN_URL.format(sym=f["sym"], period=period)
    else:
        url = KLINE_URL.format(sym=f["sym"])
    req = urllib.request.Request(url, headers=HEADERS)
    raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="replace")
    m = re.search(r"\((\[.*\])\)", raw, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))
    rows = []
    for r in data[-n:]:
        try:
            rows.append({
                "d": r["d"], "o": float(r["o"]), "h": float(r["h"]),
                "l": float(r["l"]), "c": float(r["c"]),
            })
        except (KeyError, ValueError):
            continue
    return rows


def get_kline(contract: str, period: str = "day") -> list:
    key = f"{contract}:{period}"
    now = time.time()
    hit = KLINE.get(key)
    if hit and now - hit[0] < KLINE_TTL:
        return hit[1]
    try:
        rows = fetch_kline(contract, period)
    except Exception:
        rows = hit[1] if hit else []
    KLINE[key] = (now, rows)
    return rows


# ---------------- 指标 ----------------
def sma(vals, i, n):
    if i + 1 < n:
        return None
    return sum(vals[i + 1 - n:i + 1]) / n


def rsi_series(closes, period):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    ag, al = gains / period, losses / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(ch, 0.0)) / period
        al = (al * (period - 1) + max(-ch, 0.0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def signal(rows, price, strat, params) -> int:
    """1 多 / -1 空 / 0 无。用日线收盘 + 当前价作为最后一根。"""
    if len(rows) < 30:
        return 0
    closes = [r["c"] for r in rows] + [price]
    n = len(closes)
    if strat == "ma":
        f = max(2, int(params.get("fast", 5)))
        s = max(3, int(params.get("slow", 20)))
        if f >= s:
            return 0
        f0, f1 = sma(closes, n - 2, f), sma(closes, n - 1, f)
        s0, s1 = sma(closes, n - 2, s), sma(closes, n - 1, s)
        if None in (f0, f1, s0, s1):
            return 0
        if f0 <= s0 and f1 > s1:
            return 1
        if f0 >= s0 and f1 < s1:
            return -1
    elif strat == "rsi":
        p = max(2, int(params.get("period", 14)))
        lo, hi = float(params.get("low", 30)), float(params.get("high", 70))
        rs = rsi_series(closes, p)
        if rs[-2] is None or rs[-1] is None:
            return 0
        if rs[-2] >= lo and rs[-1] < lo:
            return 1
        if rs[-2] <= hi and rs[-1] > hi:
            return -1
    elif strat == "breakout":
        k = max(2, int(params.get("n", 20)))
        if n < k + 2:
            return 0
        hh = max(closes[n - 1 - k:n - 1])
        ll = min(closes[n - 1 - k:n - 1])
        if closes[-1] > hh:
            return 1
        if closes[-1] < ll:
            return -1
    return 0


# ---------------- 交易时段 ----------------
def in_session(now=None) -> bool:
    now = now or time.localtime()
    if now.tm_wday >= 5:
        return False
    hm = now.tm_hour * 60 + now.tm_min
    if (9 * 60 <= hm <= 11 * 60 + 30) or (13 * 60 + 30 <= hm <= 15 * 60):
        return True
    if hm >= 21 * 60 or hm <= 2 * 60 + 30:
        return True
    return False


# ---------------- 账户 ----------------
def quote_for(contract: str) -> dict:
    if contract not in FUTURES:
        return {}
    return STATE["quotes"].get("nf_" + pick_main(contract), {})


def position_for(d: dict, contract: str):
    for pid, p in d["positions"].items():
        if p["contract"] == contract:
            return pid, p
    return None, None


def account_view(d: dict) -> dict:
    init = float(d["account"]["initial"])
    realized = float(d["account"]["realized"])
    pos_list, margin, floating = [], 0.0, 0.0
    for pid, p in d["positions"].items():
        f = FUTURES[p["contract"]]
        q = quote_for(p["contract"])
        price = q.get("price") or p["open_price"]
        m = price * f["mult"] * p["qty"] * f["margin"]
        pnl = (price - p["open_price"]) * f["mult"] * p["qty"] * (1 if p["side"] == "long" else -1)
        margin += m
        floating += pnl
        pos_list.append({
            "id": pid, "contract": p["contract"],
            "name": q.get("name") or main_name(p["contract"]),
            "side": "多" if p["side"] == "long" else "空",
            "qty": p["qty"], "open_price": p["open_price"], "price": price,
            "pnl": round(pnl, 2), "margin": round(m, 2), "opened": p["opened"],
        })
    equity = init + realized + floating
    avail = equity - margin
    return {
        "initial": init, "equity": round(equity, 2), "realized": round(realized, 2),
        "floating": round(floating, 2), "margin": round(margin, 2),
        "available": round(avail, 2),
        "risk": round((margin / equity * 100) if equity > 0 else 0, 1),
        "positions": pos_list, "session": in_session(),
        "last_tick": STATE["last_tick"], "last_error": STATE["last_error"],
    }


def last_signal(contract: str) -> str:
    """最近一条该品种的信号（成交或开仓日志）。"""
    d = load_data()
    for t in d.get("trades", []):
        if t.get("contract") == contract:
            return f"平{t['side']}@{t['close_price']} {t['t'][11:]}"
    f = FUTURES.get(contract)
    if not f:
        return ""
    for lg in d.get("logs", []):
        msg = lg.get("msg", "")
        if f["name"] in msg and ("开多" in msg or "开空" in msg):
            return msg.replace(f["name"], "").strip()[:26]
    return ""


def signal_bias(contract: str) -> str:
    """当前趋势方向（不需要成交就能看）：按均线关系给多/空。"""
    d = load_data()
    st = (d.get("strategy") or {}).get(contract, {})
    q = quote_for(contract)
    px = q.get("price")
    if not px:
        return ""
    try:
        rows = get_kline(contract, st.get("period", "day"))
    except Exception:
        return ""
    if not rows:
        return ""
    name = st.get("name", "ma")
    params = st.get("params") or {}
    closes = [r["c"] for r in rows] + [px]
    if name == "ma":
        f = max(2, int(params.get("fast", 5)))
        s_ = max(3, int(params.get("slow", 20)))
        if len(closes) <= s_:
            return ""
        mf = sum(closes[-f:]) / f
        ms = sum(closes[-s_:]) / s_
        gap = (mf / ms - 1) * 100
        return (f"多头 MA{f}>MA{s_}（{gap:+.2f}%）" if mf > ms
                else f"空头 MA{f}<MA{s_}（{gap:+.2f}%）")
    if name == "breakout":
        k = max(2, int(params.get("n", 20)))
        if len(closes) < k + 2:
            return ""
        hh = max(closes[-k - 1:-1]); ll = min(closes[-k - 1:-1])
        return "突破上沿" if closes[-1] > hh else ("跌破下沿" if closes[-1] < ll else "区间内")
    return ""


def quotes_view() -> list:
    d = load_data()
    out = []
    for k, f in FUTURES.items():
        q = quote_for(k)
        st = (d["strategy"] or {}).get(k, {})
        # 该策略的历史成交盈亏 + 当前持仓浮盈
        real = 0.0
        for t in d.get("trades", []):
            if t.get("contract") == k:
                real += float(t.get("pnl") or 0)
        pos_pnl, pos_qty = 0.0, 0
        for p in (d.get("positions") or {}).values():
            if p.get("contract") != k:
                continue
            px = q.get("price") or p["open_price"]
            pos_qty += int(p.get("qty") or 0)
            pos_pnl += (px - p["open_price"]) * f["mult"] * p["qty"] * (
                1 if p["side"] == "long" else -1)
        out.append({
            "contract": k,
            "name": f.get("label") or q.get("name") or main_name(k),
            "main": pick_main(k), "price": q.get("price"), "chg": q.get("chg"),
            "mult": f["mult"], "margin": f["margin"], "night": f["night"],
            "strategy": st.get("name", "ma"), "params": st.get("params", {}), "period": st.get("period", "day"),
            "enabled": st.get("enabled", True),
            "stop_win": st.get("stop_win", 1.5), "stop_loss": st.get("stop_loss", 1.5),
            "filter": st.get("filter", 0),
            "realized": round(real, 0), "pos_pnl": round(pos_pnl, 0), "pos_qty": pos_qty,
            "total_pnl": round(real + pos_pnl, 0),
            "last_signal": last_signal(k), "bias": signal_bias(k),
        })
    return out



WECOM_URL = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
             "?key=" + _secret("wecom_webhook_key"))


SEND = "/home/astock/.dsh/profiles/headless/node_modules/.bin/dsh-wechat-send"


def notify_futures(action: str, name: str, contract: str,
                   side: str, qty: int, price: float, pnl: float = None) -> None:
    """期货开/平仓双通道通知（企业微信 + 个人微信，个人微信窗口外静默）。"""
    emoji = "🟢" if action == "开仓" else "🔵"
    md = f"**{emoji} {action} {name}（{contract}）**\n{side} {qty} 手 @ {price:.2f}"
    if pnl is not None:
        md += f"\n盈亏 **{pnl:+,.0f} 元**"
    try:
        import requests
        requests.post(WECOM_URL, json={"msgtype": "markdown",
                                      "markdown": {"content": md}}, timeout=20)
    except Exception:
        pass
    try:
        import subprocess as _sp
        plain = f"{emoji} {action} {name}（{contract}）{side} {qty}手 @ {price:.2f}"
        if pnl is not None:
            plain += f"，盈亏 {pnl:+,.0f}"
        _sp.run([SEND, plain], timeout=40,
                capture_output=True, text=True)
    except Exception:
        pass


def open_position(d: dict, contract: str, side: str, qty: int, reason: str = "") -> dict:
    if contract not in FUTURES:
        return {"error": "未知合约"}
    if side not in ("long", "short"):
        return {"error": "方向不对"}
    qty = int(qty or 0)
    if qty <= 0:
        return {"error": "手数不对"}
    q = quote_for(contract)
    price = q.get("price")
    if not price:
        return {"error": "行情未就绪"}
    f = FUTURES[contract]
    need = price * f["mult"] * qty * f["margin"]
    view = account_view(d)
    if need > view["available"]:
        return {"error": f"保证金不足（需要 {need:,.0f}，可用 {view['available']:,.0f}）"}
    pid = f"p{int(time.time() * 1000)}"
    d["positions"][pid] = {
        "contract": contract, "side": side, "qty": qty,
        "open_price": price, "opened": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    nm = f.get("label") or q.get("name") or main_name(contract)
    log(d, f"{'买入开多' if side == 'long' else '卖出开空'} {nm} {qty} 手 @ {price}{reason}")
    try:
        notify_futures("开仓", nm, pick_main(contract),
                       "买入开多" if side == "long" else "卖出开空", qty, price)
    except Exception:
        pass
    save_data(d)
    return {"ok": True, "id": pid}


def close_position(d: dict, pid: str, reason: str = "") -> dict:
    p = d["positions"].get(pid)
    if not p:
        return {"error": "找不到持仓"}
    f = FUTURES[p["contract"]]
    q = quote_for(p["contract"])
    price = q.get("price") or p["open_price"]
    pnl = (price - p["open_price"]) * f["mult"] * p["qty"] * (1 if p["side"] == "long" else -1)
    d["account"]["realized"] = float(d["account"]["realized"]) + pnl
    nm = f.get("label") or q.get("name") or main_name(p["contract"])
    d["trades"].insert(0, {
        "t": time.strftime("%Y-%m-%d %H:%M:%S"), "contract": p["contract"],
        "name": nm, "side": "多" if p["side"] == "long" else "空",
        "qty": p["qty"], "open_price": p["open_price"], "close_price": price,
        "pnl": round(pnl, 2),
    })
    d["trades"] = d["trades"][:200]
    d["positions"].pop(pid, None)
    log(d, f"平仓 {nm} {p['qty']} 手 @ {price}，盈亏 {pnl:+,.0f}{reason}")
    try:
        notify_futures("平仓", nm, pick_main(p["contract"]),
                       "多" if p["side"] == "long" else "空",
                       p["qty"], price, pnl)
    except Exception:
        pass
    try:
        log_trade("futures", p["contract"], nm,
                  "sell" if p.get("side") == "多" else "buy",
                  price, p["qty"], pnl=pnl, reason=reason)
    except Exception:
        pass
    save_data(d)
    return {"ok": True, "pnl": round(pnl, 2)}


def auto_trade(d: dict) -> None:
    """按策略信号自动开平仓（每个品种最多 1 个持仓）。"""
    if not in_session():
        return
    strat_map = d.get("strategy") or {}
    for contract, cfg in strat_map.items():
        if contract not in FUTURES:
            continue
        q = quote_for(contract)
        if not q.get("price"):
            continue
        pid, pos = position_for(d, contract)
        # ---- 止盈 / 止损 ----
        if pos:
            pnl_pct = (q["price"] - pos["open_price"]) / pos["open_price"] * 100
            if pos["side"] == "short":
                pnl_pct = -pnl_pct
            sw = float(cfg.get("stop_win") or 0)
            sl = float(cfg.get("stop_loss") or 0)
            if sw and pnl_pct >= sw:
                close_position(d, pid, f"（止盈 {pnl_pct:+.2f}%）")
                continue
            if sl and pnl_pct <= -sl:
                close_position(d, pid, f"（止损 {pnl_pct:+.2f}%）")
                continue
        # 策略停用后不再开新仓，但已有持仓仍靠上面的止盈止损退出
        if not cfg.get("enabled", True):
            continue
        rows = get_kline(contract, cfg.get("period", "day"))
        if not rows:
            continue
        sig = signal(rows, q["price"], cfg.get("name", "ma"), cfg.get("params") or {})
        if not sig:
            continue
        # 趋势过滤：价格必须在 MA(N) 同侧，否则本次信号作废
        filt = int(cfg.get("filter") or 0)
        if filt:
            cl = [r["c"] for r in rows] + [q["price"]]
            if len(cl) < filt:
                continue
            mt = sum(cl[-filt:]) / filt
            if (sig == 1 and q["price"] <= mt) or (sig == -1 and q["price"] >= mt):
                continue
        pid, pos = position_for(d, contract)
        f = FUTURES[contract]
        if sig == 1:  # 多头信号
            if pos and pos["side"] == "short":
                close_position(d, pid, "（策略反手）")
                pid, pos = position_for(d, contract)
            if not pos:
                open_position(d, contract, "long", 1, "（策略自动）")
        elif sig == -1:  # 空头信号
            if pos and pos["side"] == "long":
                close_position(d, pid, "（策略反手）")
                pid, pos = position_for(d, contract)
            if not pos:
                open_position(d, contract, "short", 1, "（策略自动）")


# ---------------- 轮询 ----------------
def tick() -> None:
    try:
        STATE["quotes"] = fetch_quotes()
        STATE["last_tick"] = time.strftime("%Y-%m-%d %H:%M:%S")
        STATE["last_error"] = ""
    except Exception as exc:  # noqa: BLE001
        STATE["last_error"] = f"{type(exc).__name__}: {exc}"
        return
    try:
        d = load_data()
        auto_trade(d)
    except Exception as exc:  # noqa: BLE001
        STATE["last_error"] = f"auto: {exc}"


def loop() -> None:
    while True:
        tick()
        time.sleep(POLL)


# ---------------- HTTP ----------------
def client_ip(handler) -> str:
    peer = handler.client_address[0]
    if peer in ("127.0.0.1", "::1"):
        for h in ("CF-Connecting-IP", "X-Forwarded-For"):
            v = handler.headers.get(h)
            if v:
                return v.split(",")[0].strip()
    return peer


def is_writer(handler) -> bool:
    """内网 / Tailscale 可写；公网只读。"""
    ip = client_ip(handler)
    return ip.startswith(("192.168.", "10.", "172.", "100.", "127."))


RO_SCRIPT = """<script>
window.__RO__ = true;
(function(){
  document.body.classList.add('ro');
  var st = document.createElement('style');
  st.textContent = 'body.ro input,body.ro select:not(#kContract){opacity:.35;filter:grayscale(1);pointer-events:none;color:#8b98a5}body.ro #kContract{opacity:1;filter:none;pointer-events:auto}body.ro .card strong{opacity:.8}';
  document.head.appendChild(st);
  var badge = document.createElement('div');
  badge.id = 'roBadge';
  badge.textContent = '只读模式';
  badge.style.cssText = 'position:fixed;top:12px;right:14px;z-index:999;font-size:12px;color:#f0b429;border:1px solid #7a5c00;background:#2a2200;padding:3px 9px;border-radius:6px';
  document.body.appendChild(badge);
  function roFix(){
    document.querySelectorAll('#openBtn,#resetBtn,[data-save],[data-close]').forEach(function(e){e.remove()});
    document.querySelectorAll('#strategyBox select,#strategyBox input').forEach(function(e){e.remove()});
    document.querySelectorAll('input,select').forEach(function(e){ if(e.id==='kContract') return; if(!e.disabled) e.disabled = true; });
  }
  roFix();
  new MutationObserver(roFix).observe(document.body, {childList:true, subtree:true});
  setInterval(roFix, 800);
})();
</script>"""


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>期货自动模拟盘</title>
<style>
:root{--bg:#0f1419;--card:#171e26;--line:#243040;--txt:#e6edf3;--dim:#8b98a5;--up:#f6465d;--down:#0ecb81;--acc:#3b82f6;--warn:#d29922}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.6 -apple-system,"Microsoft YaHei",sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0 0 2px}.sub{color:var(--dim);font-size:12px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px}
.m{background:#0d1319;border:1px solid var(--line);border-radius:8px;padding:10px}
.m .k{color:var(--dim);font-size:11px}.m .v{font-size:17px;font-weight:600;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 6px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--dim);font-weight:500;font-size:12px}
input,select{background:#0d1319;border:1px solid var(--line);color:var(--txt);border-radius:6px;padding:7px;font-size:13px;outline:none;width:100%}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:8px 12px;font-size:13px;cursor:pointer}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--txt)}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;align-items:end}
.up{color:var(--up)}.down{color:var(--down)}
.hint{color:var(--dim);font-size:12px}.flex{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
details.offbox{margin-top:2px;border-top:1px dashed var(--line);padding-top:2px}
details.offbox>summary{cursor:pointer;color:var(--dim);font-size:12px;padding:9px 0;user-select:none;list-style:none}
details.offbox>summary::-webkit-details-marker{display:none}
details.offbox>summary:before{content:'\25B8';display:inline-block;margin-right:6px;transition:transform .15s}
details.offbox[open]>summary:before{content:'\25BE'}
details.offbox .row{opacity:.7}
.logbox{max-height:140px;overflow:auto;font-size:12.5px;color:#a8b6c5}
.pill{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--dim)}
.pill.on{color:var(--down);border-color:rgba(14,203,129,.4)}
canvas{width:100%;height:300px;display:block}
</style></head><body>
<div style="position:fixed;top:12px;left:14px;z-index:99;font-size:12.5px">
  <a href="/menu" style="color:#8b98a5;text-decoration:none;border:1px solid #243040;border-radius:6px;padding:4px 10px;background:#171e26">← 返回选择</a>
</div>
<div class="wrap">
<div class="flex" style="justify-content:space-between;align-items:flex-start">
  <div><h1>🛢 期货自动模拟盘</h1><div class="sub">玉米 / 豆粕 / 螺纹钢 · 主力合约 · 夜盘 · 多空 · 保证金 · 策略自动交易</div></div>
  <span class="pill" id="session">—</span>
</div>

<div class="card"><div class="metrics" id="overview"></div>
  <div class="flex" style="justify-content:space-between;margin-top:10px">
    <span class="hint" id="tickInfo">—</span>
    <button class="ghost" id="resetBtn">重置账户</button>
  </div>
</div>

<div class="card">
  <div class="flex" style="justify-content:space-between;margin-bottom:8px">
    <strong>策略（自动交易）</strong>
    <span class="hint">按日线信号自动开平仓，每个品种最多 1 手</span>
  </div>
  <div id="strategyBox"></div>
</div>

<div class="card">
  <div class="flex" style="justify-content:space-between;margin-bottom:8px">
    <strong>K 线</strong>
    <span class="flex" style="gap:6px">
      <select id="kContract" style="width:auto"></select>
      <button class="ghost" id="kDay" style="padding:3px 10px;font-size:12px">日线</button>
      <button class="ghost" id="k15" style="padding:3px 10px;font-size:12px">15分</button>
      <button class="ghost" id="k30" style="padding:3px 10px;font-size:12px">30分</button>
      <button class="ghost" id="k60" style="padding:3px 10px;font-size:12px">60分</button>
    </span>
    <span class="hint">▲ 买入　▼ 卖出　黄=MA5　紫=MA20</span>
  </div>
  <canvas id="kline"></canvas>
</div>

<div class="card">
  <strong>持仓</strong>
  <div id="posBox" style="margin-top:8px"></div>
</div>

<div class="card">
  <strong>成交记录</strong>
  <div id="tradeBox" style="margin-top:8px"></div>
</div>

<div class="card">
  <strong>引擎日志</strong>
  <div class="logbox" id="logBox"></div>
</div>
</div>
<script>
const $ = s => document.querySelector(s);
const fmt = (v,d=0) => (v===null||v===undefined||isNaN(v)) ? '-' : Number(v).toFixed(d);
const sign = v => v>=0 ? 'up' : 'down';
let QUOTES = [], KROWS = [], KDATA = null, KZOOM = null;
let KP = localStorage.getItem('fut_kp') || 'day';
let KPRICE = null, KLASTKEY = '';
const NAMES = {ma:'双均线', rsi:'RSI', breakout:'突破'};
const PDEF = {ma:{fast:5,slow:20}, rsi:{period:14,low:30,high:70}, breakout:{n:20}};
const PL = {day:'日线','15':'15分钟','30':'30分钟','60':'60分钟'};

async function load() {
  try {
    const d = await (await fetch('/api/state')).json();
    QUOTES = d.quotes || [];
    const a = d.account || {};
    $('#overview').innerHTML = `
      <div class="m"><div class="k">权益</div><div class="v">${fmt(a.equity,0)}</div></div>
      <div class="m"><div class="k">可用</div><div class="v">${fmt(a.available,0)}</div></div>
      <div class="m"><div class="k">保证金</div><div class="v">${fmt(a.margin,0)}</div></div>
      <div class="m"><div class="k">浮动盈亏</div><div class="v ${sign(a.floating||0)}">${fmt(a.floating,0)}</div></div>
      <div class="m"><div class="k">已实现</div><div class="v ${sign(a.realized||0)}">${fmt(a.realized,0)}</div></div>
      <div class="m"><div class="k">风险度</div><div class="v">${fmt(a.risk,1)}%</div></div>`;
    $('#session').textContent = a.session ? '交易中' : '休市';
    $('#session').className = 'pill ' + (a.session ? 'on' : '');
    $('#tickInfo').textContent = '上次刷新：' + (a.last_tick || '—') + (a.last_error ? ' · 异常：'+a.last_error : '');
    renderStrategy();
    renderPos(a.positions || []);
    renderTrades(d.trades || []);
    $('#logBox').innerHTML = (d.logs||[]).slice(0,50).map(l => `<div><span class="hint">${l.t}</span> ${l.msg}</div>`).join('') || '<div class="hint">暂无</div>';
    if(!$('#kContract').options.length){
      $('#kContract').innerHTML = QUOTES.map(q => `<option value="${q.contract}">${q.name}${/\\d{4}/.test(q.name||'') ? '' : '（'+(q.main||'')+'）'}</option>`).join('');
      loadKline();
    }
    const curQ = QUOTES.find(x => x.contract === ($('#kContract').value || (QUOTES[0]||{}).contract));
    if(curQ && curQ.price){ KPRICE = curQ.price; if(KROWS.length) drawKline(); }
  } catch (e) { /* ignore */ }
}

function renderStrategy(){
  const ro = window.__RO__ === true;
  const paramsTxt = q => Object.entries(q.params||{}).map(function(kv){return kv[0]+'='+kv[1]}).join(',');
  const card = q => ro ? `
    <div class="row" style="margin-bottom:6px;padding-bottom:6px">
      <div><label class="hint">${q.name} 现价 ${fmt(q.price,2)} <span class="${sign(q.chg)}">${q.chg>0?'+':''}${fmt(q.chg,2)}%</span> ｜ 盈亏 <span class="${sign(q.total_pnl)}">${q.total_pnl>0?'+':''}${fmt(q.total_pnl,0)}</span>${q.pos_qty?` <span class="hint">(${q.pos_qty}手 ${q.pos_pnl>0?'+':''}${fmt(q.pos_pnl,0)})</span>`:''}</label></div>
      <div><label class="hint">周期</label><div>${PL[q.period]||'日线'}</div></div>
      <div><label class="hint">策略</label><div>${NAMES[q.strategy]||q.strategy}</div></div>
      <div><label class="hint">参数</label><div>${paramsTxt(q)}</div></div>
      <div><label class="hint">规则</label><div>止盈${fmt(q.stop_win,1)}% 止损${fmt(q.stop_loss,1)}%${q.filter?` 过滤MA${q.filter}`:''}</div></div>
      <div><label class="hint">自动</label><div>${q.enabled?'启用':'停用'}</div></div>
    </div>
    <div class="hint" style="margin:0 0 10px;border-bottom:1px solid var(--line);padding-bottom:8px">最近信号：${q.last_signal||'—'} ｜ 当前：${q.bias||'—'}</div>` : `
    <div class="row" style="margin-bottom:6px">
      <div><label class="hint">${q.name} 现价 ${fmt(q.price,2)} <span class="${sign(q.chg)}">${q.chg>0?'+':''}${fmt(q.chg,2)}%</span> ｜ 盈亏 <span class="${sign(q.total_pnl)}">${q.total_pnl>0?'+':''}${fmt(q.total_pnl,0)}</span>${q.pos_qty?` <span class="hint">(${q.pos_qty}手 ${q.pos_pnl>0?'+':''}${fmt(q.pos_pnl,0)})</span>`:''}</label></div>
      <div><label class="hint">策略</label>
        <select data-ct="${q.contract}" data-k="strategy">
          ${Object.entries(NAMES).map(([k,v])=>`<option value="${k}" ${k===q.strategy?'selected':''}>${v}</option>`).join('')}
        </select></div>
      <div><label class="hint">周期</label>
        <select data-ct="${q.contract}" data-k="period">
          <option value="day" ${q.period==='day'?'selected':''}>日线</option>
          <option value="60" ${q.period==='60'?'selected':''}>60分钟</option>
          <option value="30" ${q.period==='30'?'selected':''}>30分钟</option>
          <option value="15" ${q.period==='15'?'selected':''}>15分钟</option>
        </select></div>
      <div><label class="hint">参数</label>
        <input data-ct="${q.contract}" data-k="params" value="${paramsTxt(q)}"></div>
      <div><label class="hint">止盈%</label>
        <input data-ct="${q.contract}" data-k="stop_win" type="number" step="0.1" value="${q.stop_win||0}"></div>
      <div><label class="hint">止损%</label>
        <input data-ct="${q.contract}" data-k="stop_loss" type="number" step="0.1" value="${q.stop_loss||0}"></div>
      <div><label class="hint">过滤MA</label>
        <input data-ct="${q.contract}" data-k="filter" type="number" step="10" value="${q.filter||0}"></div>
      <div><label class="hint">自动</label>
        <label class="hint" style="display:flex;gap:6px;align-items:center;padding-top:8px">
          <input type="checkbox" data-ct="${q.contract}" data-k="enabled" ${q.enabled?'checked':''} style="width:auto">启用
        </label></div>
      <div style="display:flex;gap:6px;white-space:nowrap"><button class="ghost" data-save="${q.contract}">保存</button><button class="ghost" data-off="${q.contract}" ${q.enabled?'':'disabled'}>停用</button></div>
    </div>
    <div class="hint" style="margin:0 0 10px;border-bottom:1px solid var(--line);padding-bottom:8px">最近信号：${q.last_signal||'—'} ｜ 当前：${q.bias||'—'}</div>`;
  const onQ = QUOTES.filter(function(q){return q.enabled;});
  const offQ = QUOTES.filter(function(q){return !q.enabled;});
  $('#strategyBox').innerHTML = onQ.map(card).join('') +
    (offQ.length ? '<details class="offbox"><summary>已停用 ' + offQ.length + ' 套策略 · 点开可查看参数或重新启用</summary>' + offQ.map(card).join('') + '</details>' : '');
  if(ro) return;
  document.querySelectorAll('[data-save]').forEach(b => b.onclick = async () => {
    const ct = b.dataset.save;
    const get = k => document.querySelector(`[data-ct="${ct}"][data-k="${k}"]`);
    const params = {};
    (get('params').value||'').split(',').forEach(seg => {
      const [k,v] = seg.split('=');
      if(k && v !== undefined && !isNaN(Number(v))) params[k.trim()] = Number(v);
    });
    const body = {
      contract: ct,
      name: get('strategy').value,
      params,
      period: get('period').value,
      stop_win: Number(get('stop_win').value) || 0,
      stop_loss: Number(get('stop_loss').value) || 0,
      filter: Number(get('filter').value) || 0,
      enabled: get('enabled').checked
    };
    await fetch('/api/strategy', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    load();
  });
  document.querySelectorAll('[data-off]').forEach(b => b.onclick = async () => {
    const ct = b.dataset.off;
    const get = k => document.querySelector(`[data-ct="${ct}"][data-k="${k}"]`);
    const params = {};
    (get('params').value||'').split(',').forEach(seg => {
      const [k,v] = seg.split('=');
      if(k && v !== undefined && !isNaN(Number(v))) params[k.trim()] = Number(v);
    });
    await fetch('/api/strategy', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({contract: ct, name: get('strategy').value, params, period: get('period').value,
        stop_win: Number(get('stop_win').value)||0, stop_loss: Number(get('stop_loss').value)||0,
        filter: Number(get('filter').value)||0, enabled: false})});
    load();
  });
}

function renderPos(list) {
  if (!list.length) { $('#posBox').innerHTML = '<div class="hint" style="padding:10px">无持仓</div>'; return; }
  $('#posBox').innerHTML = `<table><thead><tr><th>品种</th><th>方向</th><th>手数</th><th>开仓价</th><th>现价</th><th>浮动盈亏</th><th>保证金</th><th>操作</th></tr></thead><tbody>${
    list.map(p => `<tr>
      <td>${p.name}</td><td class="${p.side==='多'?'up':'down'}">${p.side}</td><td>${p.qty}</td>
      <td>${fmt(p.open_price,2)}</td><td>${fmt(p.price,2)}</td>
      <td class="${sign(p.pnl)}">${fmt(p.pnl,0)}</td><td>${fmt(p.margin,0)}</td>
      <td><button class="ghost" data-close="${p.id}">平仓</button></td></tr>`).join('')
  }</tbody></table>`;
  document.querySelectorAll('[data-close]').forEach(b => b.onclick = async () => {
    if (!confirm('确定平仓？')) return;
    await fetch('/api/close', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id: b.dataset.close})});
    load();
  });
}

function renderTrades(list) {
  if (!list.length) { $('#tradeBox').innerHTML = '<div class="hint" style="padding:10px">还没有成交</div>'; return; }
  $('#tradeBox').innerHTML = `<table><thead><tr><th>时间</th><th>品种</th><th>方向</th><th>手数</th><th>开仓价</th><th>平仓价</th><th>盈亏</th></tr></thead><tbody>${
    list.slice(0,30).map(t => `<tr><td class="hint">${t.t}</td><td>${t.name}</td>
      <td class="${t.side==='多'?'up':'down'}">${t.side}</td><td>${t.qty}</td>
      <td>${fmt(t.open_price,2)}</td><td>${fmt(t.close_price,2)}</td>
      <td class="${sign(t.pnl)}">${fmt(t.pnl,0)}</td></tr>`).join('')
  }</tbody></table>`;
}

async function loadKline(){
  const ct = $('#kContract').value || ((QUOTES[0]||{}).contract || 'C');
  const key = ct + ':' + KP;
  const changed = key !== KLASTKEY; KLASTKEY = key;
  const r = await fetch('/api/kline?contract=' + ct + '&period=' + KP);
  const d = await r.json();
  KDATA = d; KROWS = d.rows || []; KPRICE = d.price || null;
  if(changed) KZOOM = null;
  drawKline();
}

function drawKline(){
  const cv = $('#kline'), dpr = window.devicePixelRatio||1;
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = w*dpr; cv.height = h*dpr;
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,w,h);
  const all = KROWS;
  if(all.length < 2){ ctx.fillStyle='#8b98a5'; ctx.font='13px sans-serif'; ctx.fillText('数据不足', 12, 24); return; }
  if(!KZOOM || KZOOM.b >= all.length) KZOOM = {a:0, b:all.length-1};
  let zs = Math.max(0, Math.min(KZOOM.a, all.length-2));
  let ze = Math.max(zs+1, Math.min(KZOOM.b, all.length-1));
  KZOOM = {a:zs, b:ze};
  const rows = all.slice(zs, ze+1);
  // 用实时价刷新最后一根（K 线数据源只给已收盘的 bar）
  if(KPRICE && rows.length){
    const lr = rows[rows.length-1];
    rows[rows.length-1] = Object.assign({}, lr, {
      c: KPRICE,
      h: Math.max(lr.h, KPRICE),
      l: Math.min(lr.l, KPRICE)
    });
  }
  const pad={l:56,r:58,t:14,b:22};
  const W=w-pad.l-pad.r, H=h-pad.t-pad.b;
  let lo=Math.min(...rows.map(r=>r.l)), hi=Math.max(...rows.map(r=>r.h));
  const p2=(hi-lo)*0.08||1; lo-=p2; hi+=p2;
  const X=i=>pad.l+i/(rows.length-1)*W;
  const Y=v=>pad.t+(1-(v-lo)/(hi-lo))*H;
  ctx.strokeStyle='#1e293b'; ctx.fillStyle='#8b98a5'; ctx.font='11px sans-serif';
  for(let g=0;g<=4;g++){
    const y=pad.t+H*g/4;
    ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(pad.l+W,y); ctx.stroke();
    ctx.fillText((hi-(hi-lo)*g/4).toFixed(0),4,y+4);
  }
  ctx.fillText(rows[0].d, pad.l, h-6);
  ctx.fillText(rows[rows.length-1].d, pad.l+W-70, h-6);
  const bw = Math.max(1.2, W/rows.length*0.62);
  rows.forEach((r,i)=>{
    const up = r.c >= r.o;
    const color = up ? '#f6465d' : '#0ecb81';
    const x = X(i);
    ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, Y(r.h)); ctx.lineTo(x, Y(r.l)); ctx.stroke();
    const y1 = Y(Math.max(r.o,r.c)), y2 = Y(Math.min(r.o,r.c));
    ctx.fillRect(x-bw/2, y1, bw, Math.max(1, y2-y1));
  });
  const ma = (n, color) => {
    ctx.strokeStyle=color; ctx.lineWidth=1.2; ctx.beginPath();
    let started=false;
    rows.forEach((r,i)=>{
      if(i+1<n) return;
      let sum=0; for(let k=i+1-n;k<=i;k++) sum+=rows[k].c;
      const v=sum/n;
      if(!started){ ctx.moveTo(X(i),Y(v)); started=true; } else ctx.lineTo(X(i),Y(v));
    });
    ctx.stroke();
  };
  ma(5,'#e3b341'); ma(20,'#a371f7');
  // 当前价浮标（右侧最新价线）
  const lastBar = rows[rows.length-1];
  const cur = lastBar.c;
  const upNow = rows.length > 1 ? cur >= rows[rows.length-2].c : true;
  const fbCol = upNow ? '#f6465d' : '#0ecb81';
  const fy = Math.max(pad.t, Math.min(pad.t+H, Y(cur)));
  ctx.save();
  ctx.strokeStyle = fbCol; ctx.lineWidth = 1; ctx.setLineDash([4,3]);
  ctx.beginPath(); ctx.moveTo(pad.l, fy); ctx.lineTo(pad.l+W, fy); ctx.stroke();
  ctx.setLineDash([]);
  const label = cur.toFixed(cur >= 1000 ? 0 : 2);
  ctx.font = '11px sans-serif';
  const tw = Math.max(46, ctx.measureText(label).width + 10);
  ctx.fillStyle = fbCol;
  ctx.fillRect(pad.l+W+3, fy-8, tw, 16);
  ctx.fillStyle = '#fff';
  ctx.fillText(label, pad.l+W+8, fy+4);
  ctx.restore();
  // 买卖箭头（成交记录按日期匹配）
  if(KDATA && KDATA.trades){
    const idx = {}; rows.forEach((r,i)=>idx[KP==='60' ? r.d.slice(0,13) : r.d] = i);
    KDATA.trades.forEach(t=>{
      const i = idx[KP==='60' ? (t.t||'').slice(0,13) : (t.t||'').slice(0,10)];
      if(i === undefined) return;
      const x = X(i);
      const buy = t.side === '多';
      const y = buy ? Y(rows[i].l)+12 : Y(rows[i].h)-12;
      ctx.beginPath();
      if(buy){ ctx.moveTo(x, y-7); ctx.lineTo(x-5, y+2); ctx.lineTo(x+5, y+2); }
      else { ctx.moveTo(x, y+7); ctx.lineTo(x-5, y-2); ctx.lineTo(x+5, y-2); }
      ctx.closePath(); ctx.fillStyle = buy ? '#f6465d' : '#0ecb81';
      ctx.fill();
    });
  }
}

$('#kContract').onchange = loadKline;
$('#kDay').onclick = () => { KP = 'day'; localStorage.setItem('fut_kp','day'); loadKline(); };
$('#k15').onclick = () => { KP = '15'; localStorage.setItem('fut_kp','15'); loadKline(); };
$('#k30').onclick = () => { KP = '30'; localStorage.setItem('fut_kp','30'); loadKline(); };
$('#k60').onclick = () => { KP = '60'; localStorage.setItem('fut_kp','60'); loadKline(); };

/* K 线缩放：滚轮 / 双指 / 拖动 / 双击复位 */
function klineZoom(center, factor){
  const all = KROWS;
  if(all.length < 5 || !KZOOM) return;
  const len = KZOOM.b - KZOOM.a + 1;
  const newLen = Math.max(10, Math.min(all.length, Math.round(len * factor)));
  if(newLen === len) return;
  const ratio = (center - KZOOM.a) / Math.max(1, len - 1);
  let a = Math.round(center - ratio * (newLen - 1));
  let b = a + newLen - 1;
  if(a < 0){ a = 0; b = newLen - 1; }
  if(b > all.length - 1){ b = all.length - 1; a = b - newLen + 1; }
  KZOOM = {a, b};
  drawKline();
}
function klinePan(delta){
  const all = KROWS;
  if(all.length < 5 || !KZOOM) return;
  const len = KZOOM.b - KZOOM.a + 1;
  let a = KZOOM.a + delta, b = KZOOM.b + delta;
  if(a < 0){ a = 0; b = len - 1; }
  if(b > all.length - 1){ b = all.length - 1; a = b - len + 1; }
  KZOOM = {a, b};
  drawKline();
}
(function(){
  const cv = $('#kline');
  if(!cv) return;
  cv.style.cursor = 'grab';
  cv.title = '滚轮/双指缩放，拖动平移，双击复位';
  cv.addEventListener('wheel', e => {
    if(!KROWS.length || !KZOOM) return;
    e.preventDefault();
    const rect = cv.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    klineZoom(KZOOM.a + x * (KZOOM.b - KZOOM.a), e.deltaY > 0 ? 1.25 : 0.8);
  }, {passive:false});
  let drag = null;
  cv.addEventListener('mousedown', e => { drag = e.clientX; cv.style.cursor='grabbing'; });
  window.addEventListener('mousemove', e => {
    if(drag === null) return;
    const rect = cv.getBoundingClientRect();
    const perPx = (KZOOM.b - KZOOM.a) / Math.max(1, rect.width);
    const dx = e.clientX - drag; drag = e.clientX;
    klinePan(-dx * perPx);
  });
  window.addEventListener('mouseup', () => { drag = null; cv.style.cursor='grab'; });
  let touch = null;
  cv.addEventListener('touchstart', e => {
    if(e.touches.length === 1) touch = {mode:'pan', x:e.touches[0].clientX};
    else if(e.touches.length === 2) touch = {mode:'pinch', d:Math.hypot(
      e.touches[0].clientX-e.touches[1].clientX, e.touches[0].clientY-e.touches[1].clientY)};
  }, {passive:true});
  cv.addEventListener('touchmove', e => {
    if(!touch || !KROWS.length || !KZOOM) return;
    if(touch.mode === 'pan' && e.touches.length === 1){
      e.preventDefault();
      const rect = cv.getBoundingClientRect();
      const perPx = (KZOOM.b - KZOOM.a) / Math.max(1, rect.width);
      const dx = e.touches[0].clientX - touch.x; touch.x = e.touches[0].clientX;
      klinePan(-dx * perPx);
    } else if(touch.mode === 'pinch' && e.touches.length === 2){
      e.preventDefault();
      const d = Math.hypot(e.touches[0].clientX-e.touches[1].clientX, e.touches[0].clientY-e.touches[1].clientY);
      if(touch.d > 0) klineZoom((KZOOM.a + KZOOM.b) / 2, touch.d / d);
      touch.d = d;
    }
  }, {passive:false});
  cv.addEventListener('touchend', () => { touch = null; });
  cv.addEventListener('dblclick', () => {
    if(!KROWS.length) return;
    KZOOM = {a:0, b:KROWS.length - 1};
    drawKline();
  });
})();
$('#resetBtn').onclick = async () => {
  if (!confirm('重置会清空持仓和成交，确定？')) return;
  await fetch('/api/reset', {method:'POST'});
  load();
};
load();
setInterval(load, 5000);
setInterval(loadKline, 60000);
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "FuturesSim/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            page = PAGE
            if not is_writer(self):
                page = page.replace("</body>", RO_SCRIPT + "</body>")
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/api/state":
            d = load_data()
            return self._json({
                "account": account_view(d), "quotes": quotes_view(),
                "trades": d.get("trades", []), "logs": d.get("logs", []),
            })
        if u.path == "/api/kline":
            contract = (q.get("contract") or ["AU"])[0]
            period = (q.get("period") or ["day"])[0]
            rows = get_kline(contract, period) if contract in FUTURES else []
            d = load_data()
            trades = [t for t in d.get("trades", []) if t.get("contract") == contract]
            return self._json({"contract": contract, "period": period, "rows": rows,
                               "price": quote_for(contract).get("price") if contract in FUTURES else None,
                               "trades": trades[:80]})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if not is_writer(self):
            return self._json({"error": "只读模式，仅内网可操作"}, 403)
        body = self._body()
        d = load_data()
        if u.path == "/api/strategy":
            ct = str(body.get("contract") or "")
            if ct not in FUTURES:
                return self._json({"error": "未知合约"}, 400)
            name = str(body.get("name") or "ma")
            if name not in STRATEGIES:
                return self._json({"error": "未知策略"}, 400)
            params = body.get("params") or dict(STRATEGIES[name]["params"])
            d["strategy"][ct] = {"name": name, "params": params,
                                 "period": str(body.get("period") or "day"),
                                 "enabled": bool(body.get("enabled", True)),
                                 "stop_win": float(body.get("stop_win") or 0),
                                 "stop_loss": float(body.get("stop_loss") or 0),
                                 "filter": int(body.get("filter") or 0)}
            log(d, f"{FUTURES[ct]['name']} 策略改为 {STRATEGIES[name]['label']} {params}")
            save_data(d)
            return self._json({"ok": True})
        if u.path == "/api/close":
            return self._json(close_position(d, str(body.get("id") or "")))
        if u.path == "/api/reset":
            cur = load_data()
            d = default_data()
            d["account"]["initial"] = float(cur["account"]["initial"])
            log(d, "账户已重置")
            save_data(d)
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)


def main():
    tick()
    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"futures sim on http://0.0.0.0:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
