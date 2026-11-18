#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时模拟盘引擎：跟随行情、按策略自动交易、模拟 A 股 T+1。

只做模拟，不接任何真实券商接口，不产生真实委托。
"""

from __future__ import annotations

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.expanduser("~/.dsh"))
try:
    from secrets_lib import get as _secret
except Exception:
    def _secret(name, default=""):
        return default

import sys as _sys
_sys.path.insert(0, '/opt/astock')
from db_log import log_trade as _log_trade

import json
import math
import threading
import time
from datetime import datetime, date
from pathlib import Path

BASE = Path(__file__).resolve().parent
LIVE_FILE = BASE / "live.json"
STATE_CFG = Path("/opt/astock/config.json")

TICK_SEC = 15          # 交易时段内的刷新间隔
IDLE_SEC = 60          # 非交易时段
SEND = "/home/astock/.dsh/profiles/headless/node_modules/.bin/dsh-wechat-send"
FEE = 0.0003           # 佣金 万三（双边）
STAMP = 0.0005         # 印花税（卖出，仅股票）
INIT_CASH = 35000.0
POS_PCT = 0.10         # 单只最多占总资产比例（POS_AMOUNT>0 时不生效）
MAX_POS = 10          # 最多同时持仓数量（超过不再开新仓）
POS_AMOUNT = 2000.0    # 单只固定买入金额（元），设 0 则按比例
HIST_DAYS = 160

_lock = threading.Lock()
_hist_cache: dict[str, tuple[float, list]] = {}
HIST_TTL = 3600

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://finance.sina.com.cn",
}


# ---------------------------------------------------------------- 基础
def _get(url: str, timeout: int = 10, encoding: str = "utf-8") -> str:
    import urllib.request
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(encoding, "ignore")


def norm_code(code: str) -> str:
    c = (code or "").strip().lower()
    if c.startswith(("sh", "sz", "bj")):
        return c
    if c.startswith(("6", "5", "11", "9")):
        return "sh" + c
    return "sz" + c


def is_fund(code: str) -> bool:
    c = code[2:] if code[:2] in ("sh", "sz", "bj") else code
    return c.startswith(("5", "15", "16", "18"))


def trading_day(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    try:
        cfg = json.loads(STATE_CFG.read_text(encoding="utf-8"))
        if now.strftime("%Y-%m-%d") in {str(h) for h in (cfg.get("holidays") or [])}:
            return False
    except Exception:
        pass
    return True


def session_label(now: datetime | None = None) -> str:
    """更细的市场状态文字：交易中 / 午间休市 / 待开盘 / 已收盘 / 周末休市 / 节假日休市。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return "周末休市"
    try:
        cfg = json.loads(STATE_CFG.read_text(encoding="utf-8"))
        if now.strftime("%Y-%m-%d") in {str(h) for h in (cfg.get("holidays") or [])}:
            return "节假日休市"
    except Exception:
        pass
    t = now.strftime("%H:%M")
    if ("09:30" <= t <= "11:30") or ("13:00" <= t <= "15:00"):
        return "交易中"
    if "11:30" < t < "13:00":
        return "午间休市"
    if t < "09:30":
        return "待开盘"
    return "已收盘"


def in_session(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if not trading_day(now):
        return False
    t = now.strftime("%H:%M")
    return ("09:30" <= t <= "11:30") or ("13:00" <= t <= "15:00")


# ---------------------------------------------------------------- 行情
def fetch_quotes(codes: list[str]) -> dict:
    """批量取最新价。返回 {code: {price, prev_close, name}}"""
    if not codes:
        return {}
    out = {}
    for i in range(0, len(codes), 50):
        chunk = codes[i:i + 50]
        url = "https://qt.gtimg.cn/q=" + ",".join(chunk)
        try:
            txt = _get(url, encoding="gbk")
        except Exception:
            continue
        for line in txt.split(";"):
            line = line.strip()
            if '="' not in line:
                continue
            try:
                head, body = line.split('="', 1)
                code = head.strip().split("v_")[-1].lower()
                if len(code) < 8:
                    continue
                parts = body.rstrip('"').split("~")
                name = parts[1]
                price = float(parts[3]) if parts[3] else 0.0
                prev = float(parts[4]) if parts[4] else 0.0
                if price <= 0:
                    continue
                out[code] = {"price": price, "prev_close": prev or price, "name": name}
            except Exception:
                continue
    return out


def fetch_name(code: str) -> str:
    q = fetch_quotes([code])
    return q.get(code, {}).get("name", "")


def hist_closes(code: str) -> list:
    """日线收盘价序列（含日期），缓存 1 小时。"""
    now = time.time()
    hit = _hist_cache.get(code)
    if hit and now - hit[0] < HIST_TTL:
        return hit[1]
    rows = []
    try:
        url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={HIST_DAYS}")
        arr = json.loads(_get(url))
        for r in arr:
            c = float(r["close"])
            h = float(r["high"]) if r.get("high") else c
            l = float(r["low"]) if r.get("low") else c
            rows.append({"day": r["day"][:10], "close": c, "high": h, "low": l})
    except Exception:
        rows = []
    if not rows:
        try:
            url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{HIST_DAYS},qfq"
            j = json.loads(_get(url))
            d = j["data"][code]
            arr = d.get("qfqday") or d.get("day") or []
            for r in arr:
                c = float(r[2])
                h = float(r[3]) if r[3] else c
                l = float(r[4]) if r[4] else c
                rows.append({"day": r[0][:10], "close": c, "high": h, "low": l})
        except Exception:
            rows = []
    if rows:
        _hist_cache[code] = (now, rows)
    return rows


def hist_ohlc(code: str, n: int = 120) -> list:
    """日线 OHLC（含 open），用于画 K 线。"""
    rows = hist_closes(code)
    if not rows:
        return []
    # hist_closes 未保存 open，单独拉一次完整数据（走缓存里的 high/low/close 兜底）
    out = []
    try:
        url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={n}")
        arr = json.loads(_get(url))
        for r in arr:
            c = float(r["close"])
            out.append({
                "d": r["day"][:10],
                "o": float(r["open"]) if r.get("open") else c,
                "h": float(r["high"]) if r.get("high") else c,
                "l": float(r["low"]) if r.get("low") else c,
                "c": c,
            })
    except Exception:
        out = []
    if not out:
        for r in rows[-n:]:
            out.append({"d": r["day"], "o": r["close"], "h": r["high"],
                        "l": r["low"], "c": r["close"]})
    return out


def hist_60min(code: str, n: int = 240) -> list:
    """60 分钟 K 线（腾讯接口），返回 [{t,o,h,l,c}]，t 为 YYYYMMDDHHMM。"""
    try:
        url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={code},m60,,{n}"
        req = __import__("urllib.request", fromlist=["Request"]).Request(
            url, headers={**HEADERS, "Referer": "https://gu.qq.com/"})
        raw = __import__("urllib.request", fromlist=["urlopen"]).urlopen(
            req, timeout=12).read().decode("utf-8", "ignore")
        j = json.loads(raw)
        arr = j["data"][code]["m60"]
        out = []
        for r in arr:
            o, c, h, l = float(r[1]), float(r[2]), float(r[3]), float(r[4])
            out.append({"t": r[0], "o": o, "c": c, "h": h, "l": l})
        return out[-n:]
    except Exception:
        return []


_mkt_cache = {"ts": 0.0, "state": None}
MKT_TTL = 300
MARKET_PAUSE_PCT = -2.0   # 上证当日跌幅达到该值才暂停开仓


def index_state() -> dict:
    """上证指数状态：判断是否处于弱势（跌破 MA5 且跌破 MA20）。"""
    now = time.time()
    if _mkt_cache["state"] and now - _mkt_cache["ts"] < MKT_TTL:
        return _mkt_cache["state"]
    state = {"weak": False, "level": "未知", "chg": None, "ma20": None}
    try:
        url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "CN_MarketData.getKLineData?symbol=sh000001&scale=240&ma=no&datalen=60")
        arr = json.loads(_get(url))
        closes = [float(r["close"]) for r in arr if r.get("close")]
        if len(closes) >= 25:
            # 用实时价覆盖最后一根（日线接口盘中可能滞后一天）
            live_q = fetch_quotes(["sh000001"]).get("sh000001")
            if live_q and live_q.get("price"):
                prev = closes[-1]
                last = float(live_q["price"])
                chg = (last / prev - 1) * 100 if prev else 0.0
            else:
                last, prev = closes[-1], closes[-2]
                ma5 = sum(closes[-5:]) / 5
            ma20 = sum(closes[-20:]) / 20
            # 注意：上面两个分支各自算过 chg，这里统一重算一次，避免引用未定义变量
            chg = (last / prev - 1) * 100 if prev else 0.0
            weak = chg <= MARKET_PAUSE_PCT
            if weak:
                level = "弱势"
            elif chg < 0:
                level = "偏弱"
            elif last > ma20:
                level = "偏强"
            else:
                level = "中性"
            state = {"weak": weak, "level": level, "chg": round(chg, 2),
                     "ma20": round(ma20, 2), "last": round(last, 2)}
    except Exception:
        pass
    _mkt_cache["ts"] = now
    _mkt_cache["state"] = state
    return state


# ---------------------------------------------------------------- 指标
def _sma(vals: list, i: int, n: int):
    if i + 1 < n:
        return None
    return sum(vals[i + 1 - n:i + 1]) / n


def _rsi(closes: list, period: int) -> list:
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    ag = al = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        ag += max(ch, 0.0)
        al += max(-ch, 0.0)
    ag, al = ag / period, al / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(ch, 0.0)) / period
        al = (al * (period - 1) + max(-ch, 0.0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def signal_of(rows: list, live_price: float, strategy: str, params: dict) -> int:
    """返回 1 买 / -1 卖 / 0 无。把实时价当作当日未完成K线。"""
    if not rows:
        return 0
    today_digits = datetime.now().strftime("%Y%m%d")
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    last_digits = "".join(ch for ch in str(rows[-1]["day"]) if ch.isdigit())[:8]
    if last_digits == today_digits:
        closes[-1] = live_price
        highs[-1] = max(highs[-1], live_price)
        lows[-1] = min(lows[-1], live_price)
    else:
        closes.append(live_price)
        highs.append(live_price)
        lows.append(live_price)

    n = len(closes)
    if n < 30:
        return 0

    if strategy == "ma":
        f = max(2, int(params.get("fast", 5)))
        s = max(3, int(params.get("slow", 20)))
        if f >= s:
            return 0
        f0, f1 = _sma(closes, n - 2, f), _sma(closes, n - 1, f)
        s0, s1 = _sma(closes, n - 2, s), _sma(closes, n - 1, s)
        if None in (f0, f1, s0, s1):
            return 0
        if f0 <= s0 and f1 > s1:
            return 1
        if f0 >= s0 and f1 < s1:
            return -1

    elif strategy == "rsi":
        p = max(2, int(params.get("period", 14)))
        lo = float(params.get("low", 30))
        hi = float(params.get("high", 70))
        rs = _rsi(closes, p)
        if len(rs) < 2 or rs[-1] is None or rs[-2] is None:
            return 0
        if rs[-2] >= lo and rs[-1] < lo:
            return 1
        if rs[-2] <= hi and rs[-1] > hi:
            return -1

    elif strategy == "breakout":
        k = max(2, int(params.get("n", 20)))
        if n < k + 1:
            return 0
        hh = max(highs[-k - 1:-1])
        ll = min(lows[-k - 1:-1])
        if closes[-1] > hh:
            return 1
        if closes[-1] < ll:
            return -1

    return 0


_min_cache: dict = {}
MIN_TTL = 300


def _stock_bars(st: dict) -> list:
    """按标的配置返回信号用的 K 线：日线或 60 分钟。"""
    code = st["code"]
    if (st.get("period") or "day") == "60":
        key = "60|" + code
        now = time.time()
        hit = _min_cache.get(key)
        if hit and now - hit[0] < MIN_TTL:
            return hit[1]
        raw = hist_60min(code, 240)
        rows = [{"day": r["t"], "close": r["c"], "high": r["h"], "low": r["l"]} for r in raw]
        _min_cache[key] = (now, rows)
        return rows
    return hist_closes(code)


def ma_trend(rows: list, n1: int = 15, n2: int = 60) -> int:
    """均线趋势：1=MA15>MA60（多头，可继续持有），-1=空头（该平），0=数据不足。"""
    if not rows or len(rows) < n2:
        return 0
    closes = [r["close"] for r in rows]
    fast = sum(closes[-n1:]) / n1
    slow = sum(closes[-n2:]) / n2
    if fast > slow:
        return 1
    if fast < slow:
        return -1
    return 0


def state_of(rows: list, live_price: float, strategy: str, params: dict) -> int:
    """趋势状态：1=多头(该持有) / -1=空头(该空仓) / 0=中性。

    与 signal_of 的「交叉事件」不同，这里返回的是当前状态，
    因此不会因为错过某一根 K 线而漏掉信号。
    """
    if not rows:
        return 0
    today_digits = datetime.now().strftime("%Y%m%d")
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    last_digits = "".join(ch for ch in str(rows[-1]["day"]) if ch.isdigit())[:8]
    if last_digits == today_digits:
        closes[-1] = live_price
        highs[-1] = max(highs[-1], live_price)
        lows[-1] = min(lows[-1], live_price)
    else:
        closes.append(live_price); highs.append(live_price); lows.append(live_price)

    n = len(closes)
    if n < 30:
        return 0

    if strategy == "ma":
        f = max(2, int(params.get("fast", 5)))
        s = max(3, int(params.get("slow", 20)))
        if f >= s:
            return 0
        f1, s1 = _sma(closes, n - 1, f), _sma(closes, n - 1, s)
        if f1 is None or s1 is None:
            return 0
        return 1 if f1 > s1 else -1

    if strategy == "rsi":
        p = max(2, int(params.get("period", 14)))
        lo = float(params.get("low", 30))
        hi = float(params.get("high", 70))
        rs = _rsi(closes, p)
        if not rs or rs[-1] is None:
            return 0
        if rs[-1] <= lo:
            return 1
        if rs[-1] >= hi:
            return -1
        return 0

    if strategy == "breakout":
        k = max(2, int(params.get("n", 20)))
        if n < k + 1:
            return 0
        hh = max(highs[-k - 1:-1])
        ll = min(lows[-k - 1:-1])
        if closes[-1] > hh:
            return 1
        if closes[-1] < ll:
            return -1
        return 0

    return 0


# ---------------------------------------------------------------- 账户
def default_live() -> dict:
    return {
        "initial": INIT_CASH,
        "cash": INIT_CASH,
        "positions": {},
        "stocks": [],
        "trades": [],
        "logs": [],
        "equity": [],
        "enabled": True,
        "market_filter": True,
        "last_tick": None,
        "last_error": "",
    }


def load_live() -> dict:
    if not LIVE_FILE.exists():
        d = default_live()
        save_live(d)
        return d
    try:
        d = json.loads(LIVE_FILE.read_text(encoding="utf-8"))
    except Exception:
        d = default_live()
    base = default_live()
    base.update(d or {})
    return base


def save_live(d: dict) -> None:
    tmp = LIVE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LIVE_FILE)


def _log(d: dict, msg: str) -> None:
    d["logs"].insert(0, {"t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "msg": msg})
    d["logs"] = d["logs"][:300]


def _equity(d: dict, prices: dict) -> float:
    mv = 0.0
    for code, p in d["positions"].items():
        px = prices.get(code, {}).get("price") or p.get("last_price") or p["cost"]
        mv += p["qty"] * px
    return d["cash"] + mv




WECOM_URL = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
             "?key=" + _secret("wecom_webhook_key"))


def notify_all(md: str, plain: str = "") -> None:
    """企业微信 + 个人微信双发。
    企业微信无限制；个人微信受 48h 窗口限制，窗口外静默失败，不影响企业微信。"""
    try:
        import requests
        requests.post(WECOM_URL, json={"msgtype": "markdown",
                                      "markdown": {"content": md}},
                      timeout=20)
    except Exception:
        pass
    try:
        txt = plain or md.replace("**", "").replace("\n", "；")
        subprocess.run([SEND, txt], timeout=40,
                       capture_output=True, text=True)
    except Exception:
        pass


def notify_buy(name: str, code: str, qty: int, price: float) -> None:
    """买入后双通道通知。"""
    amt = qty * price
    md = (f"**🟢 买入 {name}（{code}）**\n"
          f"{qty} 股 @ {price:.3f}，约 **{amt:,.0f} 元**")
    notify_all(md, f"🟢 买入 {name}（{code}）{qty}股 @ {price:.3f}，约 {amt:,.0f} 元")


def manual_close(code: str) -> dict:
    """手动平仓（用户点按钮），返回 {ok, price, qty, pnl}。"""
    code = norm_code(code)
    with _lock:
        d = load_live()
        pos = (d.get("positions") or {}).get(code)
        if not pos:
            return {"ok": False, "error": "没有该持仓"}
        prices = fetch_quotes([code])
        px = (prices.get(code) or {}).get("price") or pos.get("last_price") or pos["cost"]
        qty = int(pos["qty"])
        amt = qty * px
        fee = amt * FEE + (0 if is_fund(code) else amt * STAMP)
        d["cash"] = round(float(d.get("cash") or 0) + amt - fee, 2)
        pnl = round(amt - fee - pos["cost"] * qty, 2)
        d["trades"].insert(0, {
            "t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": code, "name": pos.get("name"), "side": "卖出",
            "price": round(px, 3), "qty": qty,
            "amount": round(amt, 2), "pnl": pnl, "reason": "手动平仓",
        })
        d["positions"].pop(code, None)
        _log(d, f"手动平仓 {pos.get('name')}({code}) {qty}股 @ {px:.3f}，盈亏 {pnl:+.2f}")
        save_live(d)
        return {"ok": True, "code": code, "price": round(px, 3), "qty": qty, "pnl": pnl}


def tick(force: bool = False) -> dict:
    """一轮：取行情 → 更新持仓市值 → 执行策略。"""
    with _lock:
        d = load_live()
        if not d.get("enabled", True) and not force:
            return d

        codes = [s["code"] for s in d["stocks"]] + list(d["positions"].keys())
        codes = list(dict.fromkeys(codes))
        prices = fetch_quotes(codes) if codes else {}

        # 更新持仓现价
        for code, p in d["positions"].items():
            if code in prices:
                p["last_price"] = prices[code]["price"]

        mkt = index_state()
        buy_blocked = set()
        can_trade = force or in_session()

        def _sell(code: str, st: dict, reason: str, ratio: float = 1.0) -> None:
            pos = d["positions"].get(code)
            if not pos:
                return
            px = (prices.get(code) or {}).get("price") or pos.get("last_price") or pos["cost"]
            held = int(pos["qty"])
            if ratio >= 1:
                qty = held
            else:
                qty = int(math.floor(held * ratio / 100) * 100)
                if qty < 100:      # 不足一手就全平
                    qty = held
            amt = qty * px
            fee = amt * FEE + (0 if is_fund(code) else amt * STAMP)
            d["cash"] += amt - fee
            pnl = round(amt - fee - pos["cost"] * qty, 2)
            d["trades"].insert(0, {
                "t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "code": code, "name": pos.get("name"), "side": "卖出",
                "price": round(px, 3), "qty": int(qty),
                "amount": round(amt, 2), "pnl": pnl, "reason": reason,
            })
            try:
                _log_trade("quant", code, pos.get("name") or code, "sell",
                           round(px, 3), int(qty), pnl=pnl, reason=reason)
            except Exception:
                pass
            left = held - qty
            if left <= 0:
                if reason in ("止盈", "止损"):
                    st["cooldown_ts"] = time.time()   # 1 天内不再买回
                d["positions"].pop(code, None)
            else:
                pos["qty"] = left
            _log(d, f"卖出 {pos.get('name')}({code}) {qty}股 @ {px:.3f}"
                    f"（{reason}），盈亏 {pnl:+.2f}")
            try:
                _emo = "🔵" if pnl < 0 else "🟢"
                _md = (f"**{_emo} 卖出 {pos.get('name')}（{code}）**\n"
                       f"{qty} 股 @ {px:.3f}（{reason}）\n盈亏 **{pnl:+,.2f} 元**")
                notify_all(_md, f"{_emo} 卖出 {pos.get('name')}（{code}）{qty}股 @ {px:.3f}"
                                f"（{reason}）盈亏 {pnl:+,.2f}")
            except Exception:
                pass


        if can_trade:
            # 大盘暴跌保护：上证跌超 2%，全部清仓离场
            mchg = (mkt or {}).get("chg")
            if mchg is not None and mchg <= -2.0 and d["positions"]:
                for code in list(d["positions"].keys()):
                    st = next((x for x in d["stocks"] if x["code"] == code), None)
                    if not st:
                        continue
                    pos = d["positions"].get(code)
                    if not pos:
                        continue
                    if pos.get("buy_date") == datetime.now().strftime("%Y-%m-%d") and not st.get("t0", False):
                        continue           # T+1 今天买的卖不了
                    _sell(code, st, "大盘暴跌清仓")
                _log(d, f"大盘跌 {mchg:.2f}%，触发全部清仓")

            # 0) 先处理「不在自选池」的持仓：只检查止损（用持仓自己的 sl 或默认 3.0%）
            _pool_codes = {s["code"] for s in d["stocks"]}
            for code in [c for c in list(d["positions"].keys()) if c not in _pool_codes]:
                pos = d["positions"].get(code)
                if not pos:
                    continue
                info = prices.get(code)
                if not info:
                    continue
                today_s = datetime.now().strftime("%Y-%m-%d")
                if pos.get("buy_date") == today_s and not pos.get("t0", False):
                    continue   # T+1
                chg = info["price"] / pos["cost"] - 1
                sl = float(pos.get("sl") or 3.0)
                tp = float(pos.get("tp") or 4.0)
                if chg <= -sl / 100:
                    _st_tmp = {"code": code, "name": pos.get("name"),
                               "strategy": pos.get("strategy") or "ma",
                               "params": {}, "tp": 0.0, "sl": sl, "t0": bool(pos.get("t0"))}
                    _sell(code, _st_tmp, "止损（已移出自选池）")
                    _log(d, f"{code} 已移出自选池，按默认止损 {sl}% 平仓")
                elif chg >= tp / 100:
                    _st_tmp = {"code": code, "name": pos.get("name"),
                               "strategy": pos.get("strategy") or "ma",
                               "params": {}, "tp": tp, "sl": sl, "t0": bool(pos.get("t0"))}
                    _sell(code, _st_tmp, "止盈（已移出自选池）")
                    _log(d, f"{code} 已移出自选池，按默认止盈 {tp}% 平仓")

            for st in d["stocks"]:
                code = st["code"]
                info = prices.get(code)
                if not info:
                    continue
                pos = d["positions"].get(code)
                today_s = datetime.now().strftime("%Y-%m-%d")

                # 1) 止损优先（不受 tp_done 影响）→ 然后止盈全平
                if pos:
                    chg = info["price"] / pos["cost"] - 1
                    tp = float(st.get("tp") or 0)
                    sl = float(st.get("sl") or 0)
                    hit_tp = tp > 0 and chg >= tp / 100
                    hit_sl = sl > 0 and chg <= -sl / 100
                    if hit_sl:
                        if pos.get("buy_date") == today_s and not st.get("t0", False):
                            continue
                        _sell(code, st, "止损")
                        continue
                    if hit_tp:
                        if pos.get("buy_date") == today_s and not st.get("t0", False):
                            continue
                        _sell(code, st, "止盈")
                        continue

                rows = _stock_bars(st)
                sig_raw = signal_of(rows, info["price"], st["strategy"], st.get("params") or {})
                state = state_of(rows, info["price"], st["strategy"], st.get("params") or {})
                # 事件信号 或 状态信号 任一成立即动作（状态信号保证不漏）
                if sig_raw == 1 or (sig_raw == 0 and state == 1):
                    sig = 1
                elif sig_raw == -1 or (sig_raw == 0 and state == -1):
                    sig = -1
                else:
                    sig = 0
                if sig == 0:
                    continue
                st["last_signal"] = {"sig": sig, "price": info["price"],
                                     "t": datetime.now().strftime("%H:%M:%S")}
                pos = d["positions"].get(code)
                eq = _equity(d, prices)

                if sig == 1 and not pos:
                    if len(d["positions"]) >= MAX_POS:
                        continue   # 持仓已满，不再开新仓
                    if time.time() - float(st.get("cooldown_ts") or 0) < 86400:
                        continue
                    if mkt.get("weak") and d.get("market_filter", True):
                        key = f"{code}|{datetime.now():%Y-%m-%d}"
                        if key not in buy_blocked:
                            buy_blocked.add(key)
                            _log(d, f"{code} 出现买入信号，但大盘偏弱，按规则暂不开仓")
                        continue
                    per = POS_AMOUNT if POS_AMOUNT > 0 else eq * POS_PCT
                    budget = min(d["cash"], per)
                    px = info["price"]
                    qty = math.floor(budget / (px * (1 + FEE)) / 100) * 100
                    if qty >= 100 and d["cash"] >= qty * px * (1 + FEE):
                        amt = qty * px
                        d["cash"] -= amt + amt * FEE
                        d["positions"][code] = {
                            "name": info["name"] or st.get("name") or code,
                            "qty": qty, "cost": round(px, 3), "last_price": px,
                            "tp": float(st.get("tp") or 4.0), "sl": float(st.get("sl") or 3.0),
                            "buy_date": datetime.now().strftime("%Y-%m-%d"),
                            "strategy": st["strategy"],
                        }
                        d["trades"].insert(0, {
                            "t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "code": code, "name": info["name"], "side": "买入",
                            "price": round(px, 3), "qty": int(qty),
                            "amount": round(amt, 2), "pnl": None,
                            "reason": st["strategy"],
                        })
                        _log(d, f"买入 {info['name']}({code}) {qty}股 @ {px:.3f}")
                        notify_buy(info.get("name") or code, code, qty, px)
                        try:
                            _log_trade("quant", code, info.get("name") or code, "buy",
                                       round(px, 3), int(qty), reason=st.get("strategy"))
                        except Exception:
                            pass

                elif sig == -1 and pos:
                    if pos.get("buy_date") == today_s and not st.get("t0", False):
                        # 同一个标的一天只记一次，避免每 15 秒刷一条
                        if st.get("t1_noted") != today_s:
                            st["t1_noted"] = today_s
                            _log(d, f"{code} 触发卖出信号，但 T+1 限制，今日买入不可卖")
                        continue
                    _sell(code, st, "信号")

        eq = _equity(d, prices)
        now = datetime.now()
        d["last_tick"] = now.strftime("%Y-%m-%d %H:%M:%S")
        if in_session():
            d["equity"].append({"t": now.strftime("%m-%d %H:%M"), "v": round(eq, 2)})
            d["equity"] = d["equity"][-3000:]
        # 收盘后记录当日收盘权益，供第二天算「当日盈亏」用
        if now.hour >= 15:
            dc = d.get("day_close") or {}
            dc[now.strftime("%Y-%m-%d")] = round(eq, 2)
            d["day_close"] = dc

        d["snapshot"] = {
            "equity": round(eq, 2),
            "cash": round(d["cash"], 2),
            "market_value": round(eq - d["cash"], 2),
            "pnl": round(eq - d["initial"], 2),
            "pnl_pct": round((eq / d["initial"] - 1) * 100, 2),
            "prices": {c: prices[c]["price"] for c in prices},
            "prev": {c: prices[c].get("prev_close") or prices[c]["price"] for c in prices},
            "market": mkt,
            "market_filter": bool(d.get("market_filter", True)),
        }
        save_live(d)
        return d


# ---------------------------------------------------------------- 循环
_thread: threading.Thread | None = None
_stop = threading.Event()


def _loop():
    while not _stop.is_set():
        try:
            tick()
            sleep_s = TICK_SEC if in_session() else IDLE_SEC
        except Exception as e:
            try:
                d = load_live()
                d["last_error"] = f"{type(e).__name__}: {e}"
                save_live(d)
            except Exception:
                pass
            sleep_s = 60
        _stop.wait(sleep_s)


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="live-engine")
    _thread.start()
