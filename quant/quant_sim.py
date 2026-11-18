#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量化模拟回测页（纯标准库，无第三方依赖）

端口 8082。只做历史数据回测，不接实盘、不下单。

写权限：仅允许内网(192.168.0.0/16)、Tailscale(100.64.0.0/10)、本机访问；
        其它来源只读。后续可再加口令。
"""

from __future__ import annotations

import json
import math
import sys
import os
import random
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data.json"
PORT = 8082

sys.path.insert(0, str(BASE_DIR))
import live_engine  # noqa: E402  实时模拟盘引擎
import auth as qa  # noqa: E402  公网登录保护（密码 + 微信验证码）

FUTURES_RO_SCRIPT = """<script>
window.__RO__ = true;
(function(){
  document.body.classList.add('ro');
  var st = document.createElement('style');
  st.textContent = 'body.ro input,body.ro select:not(#kContract){opacity:.35;filter:grayscale(1);pointer-events:none;color:#8b98a5}body.ro #kContract{opacity:1;filter:none;pointer-events:auto}';
  document.head.appendChild(st);
  var badge = document.createElement('div');
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

FUTURES_FETCH_JS = """<script>
(function(){
  var _f = window.fetch;
  window.fetch = function(u, o){
    if (typeof u === "string" && u.charAt(0) === "/" && u.indexOf("/futures") !== 0) {
      u = "/futures" + u;
    }
    return _f(u, o);
  };
})();
</script>"""

MENU_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>选择功能</title>
<style>
:root{--bg:#0f1419;--card:#171e26;--line:#243040;--txt:#e6edf3;--dim:#8b98a5;--acc:#3b82f6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.6 -apple-system,"Microsoft YaHei",sans-serif}
.wrap{max-width:640px;margin:0 auto;padding:40px 16px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--dim);font-size:12px;margin-bottom:22px}
.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:12px;text-decoration:none;color:var(--txt)}
.card:hover{border-color:var(--acc)}
.t{font-size:16px;font-weight:600;margin-bottom:3px}
.d{color:var(--dim);font-size:12.5px}
</style></head><body>
<div class="wrap">
  <h1>选择功能</h1>
  <div class="sub">只读访问 · 数据仅供查看</div>
  <a class="card" href="/live">
    <div class="t">📊 股票实时模拟盘</div>
    <div class="d">持仓、净值曲线、K 线、成交记录</div>
  </a>
  <a class="card" href="/futures">
    <div class="t">🛢 期货模拟盘</div>
    <div class="d">黄金 / 玉米，多空持仓、保证金、K 线</div>
  </a>
  <a class="card" href="/gold">
    <div class="t">🥇 金银实时</div>
    <div class="d">伦敦金/银、沪金/银主连，算盈亏</div>
  </a>
</div>
</body></html>"""

SINA_KLINE = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={n}")
TENCENT_KLINE = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},qfq")
TENCENT_QUOTE = "https://qt.gtimg.cn/q={code}"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://finance.sina.com.cn",
}

_lock = threading.Lock()
_kline_cache: dict[str, tuple[float, list]] = {}
CACHE_TTL = 1800

STRATEGIES = {
    "ma":       {"label": "双均线", "params": {"fast": 5, "slow": 20}},
    "rsi":      {"label": "RSI", "params": {"period": 14, "low": 30, "high": 70}},
    "breakout": {"label": "突破", "params": {"n": 20}},
}


# ---------------------------------------------------------------- 数据存取
def default_data() -> dict:
    return {"stocks": [], "admin_token": "".join(random.choices("abcdefghkmnpqrstuvwxyz23456789", k=10))}


def load_data() -> dict:
    if not DATA_FILE.exists():
        d = default_data()
        save_data(d)
        return d
    try:
        d = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        d = default_data()
    d.setdefault("stocks", [])
    d.setdefault("admin_token", default_data()["admin_token"])
    return d


def save_data(d: dict) -> None:
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)


# ---------------------------------------------------------------- 网络
def http_get(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def norm_code(code: str) -> str:
    code = code.strip().lower()
    if code.startswith(("sh", "sz", "bj")):
        return code
    if code.startswith(("6", "5", "11", "9")):
        return "sh" + code
    return "sz" + code


def fetch_name(code: str) -> str:
    try:
        txt = http_get(TENCENT_QUOTE.format(code=code), timeout=8)
        seg = txt.split('="')
        if len(seg) > 1:
            parts = seg[1].split("~")
            if len(parts) > 1 and parts[1]:
                return parts[1]
    except Exception:
        pass
    return ""


def fetch_kline(code: str, n: int = 500) -> list:
    now = time.time()
    hit = _kline_cache.get(code)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]

    rows: list = []
    # 新浪
    try:
        raw = http_get(SINA_KLINE.format(code=code, n=n))
        arr = json.loads(raw)
        for r in arr:
            rows.append({
                "day": r["day"][:10],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r.get("volume") or 0),
            })
    except Exception:
        rows = []

    # 腾讯兜底
    if not rows:
        try:
            raw = http_get(TENCENT_KLINE.format(code=code, n=n))
            j = json.loads(raw)
            data = j["data"][code]
            arr = data.get("qfqday") or data.get("day") or []
            for r in arr:
                rows.append({
                    "day": r[0][:10],
                    "open": float(r[1]),
                    "close": float(r[2]),
                    "high": float(r[3]),
                    "low": float(r[4]),
                    "volume": float(r[5]) if len(r) > 5 else 0.0,
                })
        except Exception:
            rows = []

    if rows:
        _kline_cache[code] = (now, rows)
    return rows


# ---------------------------------------------------------------- 指标
def sma(vals: list, i: int, n: int) -> float | None:
    if i + 1 < n:
        return None
    return sum(vals[i + 1 - n:i + 1]) / n


def rsi_series(closes: list, period: int) -> list:
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


# ---------------------------------------------------------------- 回测
def is_fund(code: str) -> bool:
    c = code[2:] if code[:2] in ("sh", "sz", "bj") else code
    return c.startswith(("5", "15", "16", "18"))


def backtest(rows: list, strat: str, params: dict, capital: float, fee: float = 0.0003) -> dict:
    closes = [r["close"] for r in rows]
    n = len(closes)
    if n < 30:
        raise ValueError("历史数据太少，至少需要 30 个交易日")

    sig = [0] * n  # 1 买, -1 卖, 0 无

    if strat == "ma":
        f = max(2, int(params.get("fast", 5)))
        s = max(3, int(params.get("slow", 20)))
        if f >= s:
            raise ValueError("快线周期必须小于慢线周期")
        for i in range(1, n):
            f0, f1 = sma(closes, i - 1, f), sma(closes, i, f)
            s0, s1 = sma(closes, i - 1, s), sma(closes, i, s)
            if None in (f0, f1, s0, s1):
                continue
            if f0 <= s0 and f1 > s1:
                sig[i] = 1
            elif f0 >= s0 and f1 < s1:
                sig[i] = -1

    elif strat == "rsi":
        p = max(2, int(params.get("period", 14)))
        lo = float(params.get("low", 30))
        hi = float(params.get("high", 70))
        rs = rsi_series(closes, p)
        for i in range(1, n):
            if rs[i] is None or rs[i - 1] is None:
                continue
            if rs[i - 1] >= lo and rs[i] < lo:
                sig[i] = 1
            elif rs[i - 1] <= hi and rs[i] > hi:
                sig[i] = -1

    elif strat == "breakout":
        k = max(2, int(params.get("n", 20)))
        for i in range(k, n):
            hh = max(closes[i - k:i])
            ll = min(closes[i - k:i])
            if closes[i] > hh:
                sig[i] = 1
            elif closes[i] < ll:
                sig[i] = -1
    else:
        raise ValueError("未知策略")

    stamp = 0.0 if is_fund(rows[0].get("code", "")) else 0.0005
    cash = float(capital)
    shares = 0.0
    cost_basis = 0.0
    equity: list[float] = []
    trades: list[dict] = []
    pending = 0

    for i, r in enumerate(rows):
        if pending == 1 and shares == 0:
            price = r["open"]
            qty = math.floor(cash / (price * (1 + fee)) / 100) * 100
            if qty > 0:
                amt = qty * price
                cash -= amt + amt * fee
                shares = qty
                cost_basis = amt + amt * fee
                trades.append({"date": r["day"], "side": "买", "price": round(price, 3),
                               "qty": int(qty), "amount": round(amt, 2)})
            pending = 0
        elif pending == -1 and shares > 0:
            price = r["open"]
            amt = shares * price
            cash += amt - amt * fee - amt * stamp
            pnl = cash - (capital if not trades else cost_basis)
            trades.append({"date": r["day"], "side": "卖", "price": round(price, 3),
                           "qty": int(shares), "amount": round(amt, 2),
                           "pnl": round(amt - amt * fee - amt * stamp - cost_basis, 2)})
            shares = 0.0
            cost_basis = 0.0
            pending = 0

        equity.append(cash + shares * r["close"])
        if sig[i] == 1 and shares == 0:
            pending = 1
        elif sig[i] == -1 and shares > 0:
            pending = -1

    # 收尾：还持着就按最后收盘价估
    if shares > 0:
        amt = shares * closes[-1]
        trades.append({"date": rows[-1]["day"], "side": "持仓", "price": round(closes[-1], 3),
                       "qty": int(shares), "amount": round(amt, 2),
                       "pnl": round(amt - cost_basis, 2)})

    total_ret = equity[-1] / capital - 1
    days = n
    annual = (1 + total_ret) ** (252 / days) - 1 if total_ret > -1 else -1

    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        peak = max(peak, v)
        max_dd = min(max_dd, v / peak - 1)

    closed = [t for t in trades if t.get("pnl") is not None and t["side"] == "卖"]
    wins = [t for t in closed if t["pnl"] > 0]
    win_rate = (len(wins) / len(closed)) if closed else 0.0

    bench = closes[-1] / closes[0] - 1

    return {
        "dates": [r["day"] for r in rows],
        "equity": [round(v, 2) for v in equity],
        "benchmark": [round(capital * c / closes[0], 2) for c in closes],
        "metrics": {
            "total_return": round(total_ret * 100, 2),
            "annual_return": round(annual * 100, 2),
            "max_drawdown": round(max_dd * 100, 2),
            "win_rate": round(win_rate * 100, 1),
            "trades": len(closed),
            "benchmark_return": round(bench * 100, 2),
            "final_equity": round(equity[-1], 2),
        },
        "trades": trades,
    }


# ---------------------------------------------------------------- HTTP
def client_ip(h) -> str:
    """取真实客户端 IP。

    经 Cloudflare Tunnel / nginx / Tailscale Funnel 转发时，TCP 源是 127.0.0.1，
    此时优先信任反代写入的真实 IP 头；内网直连则用 TCP 源地址。
    只有本机反代才能触发转发头，公网直接伪造 XFF 无效。
    """
    ip = h.client_address[0]
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    if ip in ("127.0.0.1", "::1"):
        cf = (h.headers.get("CF-Connecting-IP") or "").strip()
        if cf:
            return cf
        xff = (h.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if xff:
            return xff
    return ip


def can_write(ip: str) -> bool:
    if ip in ("127.0.0.1", "::1"):
        return True
    if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
        return True
    if ip.startswith("100."):  # Tailscale CGNAT
        return True
    return False


class Handler(BaseHTTPRequestHandler):
    server_version = "QuantSim/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
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

    def _proxy_futures(self, u, body=None):
        """把 /futures/* 反代到期货模拟盘 8084，路径前缀去掉。"""
        import urllib.request as _urlreq
        rest = u.path[len("/futures"):] or "/"
        target = "http://127.0.0.1:8084" + rest
        if u.query:
            target += "?" + u.query
        if body is None:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
        req = _urlreq.Request(target, data=body, method=self.command)
        for h in ("Content-Type", "Cookie", "User-Agent", "CF-Connecting-IP", "X-Forwarded-For"):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)
        try:
            resp = _urlreq.urlopen(req, timeout=30)
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "application/json; charset=utf-8")
            if "text/html" in ctype:
                html = data.decode("utf-8", errors="replace")
                html = html.replace("<body>", "<body>" + FUTURES_FETCH_JS, 1)
                if not can_write(client_ip(self)):
                    html = html.replace("</body>", FUTURES_RO_SCRIPT + "</body>", 1)
                data = html.encode("utf-8")
            self.send_response(resp.status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"代理失败: {exc}"}, 502)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        ip = client_ip(self)
        writer = can_write(ip)

        # 公网只放行只读看板 /live 及其数据；后台、列表、回测仅内网/Tailscale
        if u.path in ("/", "/index.html"):
            if not writer:
                self.send_response(302)
                self.send_header("Location", "/menu" if qa.is_authed(self) else "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            html = (BASE_DIR / "index.html").read_bytes()
            return self._send(200, html, "text/html; charset=utf-8")

        if u.path == "/login":
            if qa.is_authed(self):
                self.send_response(302)
                self.send_header("Location", "/menu")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send(200, qa.LOGIN_HTML.encode("utf-8"), "text/html; charset=utf-8")

        if u.path == "/menu":
            if not writer and not qa.is_authed(self):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send(200, MENU_HTML.encode("utf-8"), "text/html; charset=utf-8")

        if u.path == "/futures" or u.path.startswith("/futures/"):
            if not writer and not qa.is_authed(self):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._proxy_futures(u)

        if u.path == "/live":
            if not writer and not qa.is_authed(self):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not writer:
                qa.log_access(ip, "/live", 200, self.headers.get("User-Agent", ""), "公网访问")
            html = (BASE_DIR / "live.html").read_bytes().decode("utf-8")
            if not writer:
                html = qa.pure_view(html)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

        if u.path in ("/gold", "/gold.html"):
            # 纯行情页，不做登录限制；直接取 8080 渲染好的页面（带服务端数据）
            fp = __import__("pathlib").Path("/opt/astock/web/gold.html")
            if fp.exists():
                if not writer:
                    qa.log_access(ip, "/gold", 200, self.headers.get("User-Agent", ""), "公网访问")
                try:
                    import urllib.request as _ur
                    with _ur.urlopen("http://127.0.0.1:8080/gold", timeout=15) as r:
                        body = r.read()
                except Exception:  # noqa: BLE001
                    body = fp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "not found"}, 404)
            return

        if u.path == "/api/gold":
            try:
                import urllib.request as _ur
                with _ur.urlopen("http://127.0.0.1:8080/api/gold", timeout=20) as r:
                    body = r.read()
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)}, 502)
            return

        if u.path in ("/pdd", "/pdd.html"):
            if not writer and not qa.is_authed(self):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            fp = __import__("pathlib").Path("/home/astock/.dsh/dsh_wechat/pdd_mens/pdd.html")
            if fp.exists():
                if not writer:
                    qa.log_access(ip, "/pdd", 200, self.headers.get("User-Agent", ""), "公网访问")
                self._send(200, fp.read_bytes(), "text/html; charset=utf-8")
            else:
                self._json({"error": "not built"}, 404)
            return
        if u.path == "/api/pdd/status":
            fp = __import__("pathlib").Path("/home/astock/.dsh/dsh_wechat/pdd_mens/status.json")
            try:
                self._json(json.loads(fp.read_text(encoding="utf-8")))
            except Exception:
                self._json({"state": "idle", "msg": "未启动"})
            return

        if u.path == "/api/pdd/ctl":
            if not writer and not qa.is_authed(self):
                self._json({"error": "需要登录"}, 401)
                return
            action = (q.get("action") or [""])[0]
            ctl = __import__("pathlib").Path("/home/astock/.dsh/dsh_wechat/pdd_mens/control.json")
            ctl.parent.mkdir(parents=True, exist_ok=True)
            if action == "start":
                ctl.write_text(json.dumps({"cmd": "run"}), encoding="utf-8")
                import subprocess as sp
                r = sp.run(["pgrep", "-f", "pdd_runner.py"], capture_output=True, text=True)
                if not r.stdout.strip():
                    _out = open("/home/astock/.dsh/dsh_wechat/pdd_mens/runner.out", "a")
                    sp.Popen([ "/home/astock/.dsh/dsh_wechat/.venv_img/bin/python",
                               "/home/astock/.dsh/dsh_wechat/pdd_mens/pdd_runner.py"],
                             stdout=_out, stderr=_out, start_new_session=True)
                self._json({"ok": True, "msg": "已启动"})
                return
            if action == "pause":
                ctl.write_text(json.dumps({"cmd": "pause"}), encoding="utf-8")
                self._json({"ok": True, "msg": "已暂停"})
                return
            if action == "stop":
                ctl.write_text(json.dumps({"cmd": "stop"}), encoding="utf-8")
                import subprocess as sp, time as _t
                _t.sleep(1.5)
                sp.run(["pkill", "-f", "pdd_runner.py"], capture_output=True)
                self._json({"ok": True, "msg": "已停止"})
                return
            self._json({"error": "action 不对"}, 400)
            return

        if u.path.startswith("/pdd_img/"):
            if not writer and not qa.is_authed(self):
                self._json({"error": "需要登录"}, 401)
                return
            fn = u.path.split("/")[-1]
            fp = __import__("pathlib").Path("/home/astock/.dsh/dsh_wechat/pdd_mens/images") / fn
            if fp.exists() and fp.is_file():
                self._send(200, fp.read_bytes(), "image/png")
            else:
                self._json({"error": "not found"}, 404)
            return
        if u.path == "/api/live":
            if not writer and not qa.is_authed(self):
                return self._json({"error": "需要登录"}, 401)
            d = live_engine.load_live()
            out = dict(d)
            out["can_write"] = writer
            out["session"] = live_engine.in_session()
            out["session_text"] = live_engine.session_label()
            return self._json(out)

        if u.path == "/api/live/kline":
            if not writer and not qa.is_authed(self):
                return self._json({"error": "需要登录"}, 401)
            code = live_engine.norm_code((q.get("code") or [""])[0])
            if len(code) < 8:
                return self._json({"error": "代码不对"}, 400)
            period = (q.get("period") or ["day"])[0]
            if period in ("60", "60m", "m60"):
                raw = live_engine.hist_60min(code, 240)
                rows = [{"d": r["t"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]}
                        for r in raw]
                period = "60"
            else:
                rows = live_engine.hist_ohlc(code, 120)
                period = "day"
            d2 = live_engine.load_live()
            trades = [t for t in (d2.get("trades") or []) if t.get("code") == code]
            name = ""
            if code in (d2.get("positions") or {}):
                name = d2["positions"][code].get("name") or ""
            if not name:
                name = next((s.get("name") for s in d2.get("stocks") or []
                             if s["code"] == code), "") or live_engine.fetch_name(code)
            return self._json({"code": code, "name": name, "period": period, "rows": rows,
                               "trades": [{"d": t["t"][:10], "dt": t["t"], "side": t["side"],
                                           "price": t.get("price")} for t in trades]})

        if u.path == "/api/list":
            if not writer:
                return self._json({"error": "只读模式：数据仅限内网/Tailscale"}, 403)
            d = load_data()
            return self._json({"stocks": d["stocks"], "can_write": writer,
                               "strategies": STRATEGIES})

        if u.path == "/api/run":
            if not writer:
                return self._json({"error": "只读模式：回测仅限内网/Tailscale"}, 403)
            sid = q.get("id", [""])[0]
            d = load_data()
            item = next((s for s in d["stocks"] if s["id"] == sid), None)
            if not item:
                return self._json({"error": "找不到该股票"}, 404)
            try:
                res = run_one(item)
                return self._json(res)
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        ip = client_ip(self)
        body = self._body()

        if u.path == "/futures" or u.path.startswith("/futures/"):
            if not can_write(ip) and not qa.is_authed(self):
                return self._json({"error": "需要登录"}, 401)
            if not can_write(ip):
                return self._json({"error": "只读模式"}, 403)
            return self._proxy_futures(u, body)

        # 公网登录接口（不需要内网权限）
        if u.path == "/api/auth/login":
            return self._json(qa.login(self, ip, body))
        if u.path == "/api/auth/verify":
            res = qa.verify(self, ip, body)
            cookie = res.pop("cookie", None)
            if cookie:
                data = json.dumps(res, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Set-Cookie", cookie)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            return self._json(res)

        if not can_write(ip):
            return self._json({"error": "只读模式：该操作仅限内网/Tailscale"}, 403)

        d = load_data()

        if u.path == "/api/add":
            code = norm_code(str(body.get("code", "")))
            if len(code) < 8:
                return self._json({"error": "代码格式不对"}, 400)
            name = str(body.get("name") or "").strip() or fetch_name(code) or code
            item = {
                "id": "s%d" % int(time.time() * 1000),
                "code": code,
                "name": name,
                "strategy": body.get("strategy") or "ma",
                "params": body.get("params") or dict(STRATEGIES["ma"]["params"]),
                "capital": float(body.get("capital") or 100000),
                "days": int(body.get("days") or 400),
            }
            d["stocks"].append(item)
            save_data(d)
            return self._json({"ok": True, "item": item})

        if u.path == "/api/update":
            sid = body.get("id")
            item = next((s for s in d["stocks"] if s["id"] == sid), None)
            if not item:
                return self._json({"error": "找不到"}, 404)
            for k in ("name", "strategy", "params", "capital", "days"):
                if k in body:
                    item[k] = body[k]
            save_data(d)
            return self._json({"ok": True, "item": item})

        if u.path == "/api/delete":
            sid = body.get("id")
            d["stocks"] = [s for s in d["stocks"] if s["id"] != sid]
            save_data(d)
            return self._json({"ok": True})

        if u.path == "/api/live/close":
            code = norm_code(str(body.get("code", "")))
            return self._json(live_engine.manual_close(code))

        if u.path == "/api/live/add":
            code = norm_code(str(body.get("code", "")))
            if len(code) < 8:
                return self._json({"error": "代码格式不对"}, 400)
            name = str(body.get("name") or "").strip() or live_engine.fetch_name(code) or code
            d2 = live_engine.load_live()
            if any(x["code"] == code for x in d2["stocks"]):
                return self._json({"error": "已在列表里"}, 400)
            d2["stocks"].append({
                "code": code, "name": name,
                "strategy": body.get("strategy") or "ma",
                "params": body.get("params") or dict(STRATEGIES["ma"]["params"]),
                "t0": bool(body.get("t0", False)),
                "period": body.get("period") or "day",
                "tp": float(body.get("tp") or 0),
                "sl": float(body.get("sl") or 0),
                "last_signal": None,
            })
            live_engine._log(d2, f"加入自选 {name}({code})")
            live_engine.save_live(d2)
            return self._json({"ok": True})

        if u.path == "/api/live/update":
            code = norm_code(str(body.get("code", "")))
            d2 = live_engine.load_live()
            st = next((x for x in d2["stocks"] if x["code"] == code), None)
            if st is None:
                return self._json({"error": "不在列表里"}, 404)
            strat = str(body.get("strategy") or st.get("strategy") or "ma")
            if strat not in STRATEGIES:
                return self._json({"error": "未知策略"}, 400)
            params = body.get("params")
            if not isinstance(params, dict) or not params:
                params = st.get("params") or dict(STRATEGIES[strat]["params"])
            st["strategy"] = strat
            st["params"] = params
            if "t0" in body:
                st["t0"] = bool(body.get("t0"))
            if "period" in body:
                st["period"] = "60" if str(body.get("period")) == "60" else "day"
            for k in ("tp", "sl"):
                if k in body:
                    st[k] = float(body[k] or 0)
            live_engine._log(d2, f"修改 {st['name']}({code})：{st.get('period','day')} "
                                 f"{strat} {params} 止盈{st.get('tp',0)}% 止损{st.get('sl',0)}%")
            live_engine.save_live(d2)
            return self._json({"ok": True, "item": st})

        if u.path == "/api/live/del":
            d2 = live_engine.load_live()
            d2["stocks"] = [x for x in d2["stocks"] if x["code"] != body.get("code")]
            live_engine.save_live(d2)
            return self._json({"ok": True})

        if u.path == "/api/live/cash":
            try:
                new_cash = float(body.get("cash"))
            except Exception:
                return self._json({"error": "金额格式不对"}, 400)
            if new_cash < 0:
                return self._json({"error": "现金不能为负"}, 400)
            d2 = live_engine.load_live()
            old = float(d2.get("cash") or 0)
            delta = new_cash - old
            d2["cash"] = round(new_cash, 2)
            # 入金/出金同步调整本金基数，避免盈亏被"充值"污染
            d2["initial"] = round(float(d2.get("initial") or 0) + delta, 2)
            live_engine._log(d2, f"调整现金 {old:,.2f} → {new_cash:,.2f}（{'入金' if delta>=0 else '出金'} {delta:+,.2f}）")
            live_engine.save_live(d2)
            return self._json({"ok": True, "cash": d2["cash"], "initial": d2["initial"]})

        if u.path == "/api/live/reset":
            cur = live_engine.load_live()
            d2 = live_engine.default_live()
            d2["stocks"] = cur.get("stocks") or []          # 保留标的池
            d2["market_filter"] = cur.get("market_filter", True)
            live_engine._log(d2, "账户已重置（标的池保留）")
            live_engine.save_live(d2)
            return self._json({"ok": True})

        if u.path == "/api/live/mfilter":
            d2 = live_engine.load_live()
            d2["market_filter"] = not d2.get("market_filter", True)
            live_engine._log(d2, "大盘过滤已" + ("开启" if d2["market_filter"] else "关闭"))
            live_engine.save_live(d2)
            return self._json({"ok": True, "market_filter": d2["market_filter"]})

        if u.path == "/api/live/toggle":
            d2 = live_engine.load_live()
            d2["enabled"] = not d2.get("enabled", True)
            live_engine._log(d2, "引擎已" + ("启动" if d2["enabled"] else "暂停"))
            live_engine.save_live(d2)
            return self._json({"ok": True, "enabled": d2["enabled"]})

        return self._json({"error": "not found"}, 404)


def run_one(item: dict) -> dict:
    rows = fetch_kline(item["code"], int(item.get("days") or 400))
    for r in rows:
        r["code"] = item["code"]
    if not rows:
        raise ValueError("拉不到历史数据（代码可能有误或接口异常）")
    res = backtest(rows, item["strategy"], item.get("params") or {}, float(item.get("capital") or 100000))
    res["stock"] = {"id": item["id"], "code": item["code"], "name": item.get("name"),
                    "strategy": item["strategy"], "params": item.get("params")}
    return res


def _pdd_watchdog():
    """常驻线程：control.json 说 run 且没有 runner 进程时，把它拉起来"""
    import subprocess as sp, time as _t
    ctl = "/home/astock/.dsh/dsh_wechat/pdd_mens/control.json"
    runner = "/home/astock/.dsh/dsh_wechat/pdd_mens/pdd_runner.py"
    py = "/home/astock/.dsh/dsh_wechat/.venv_img/bin/python"
    out = "/home/astock/.dsh/dsh_wechat/pdd_mens/runner.out"
    while True:
        try:
            cmd = ""
            try:
                cmd = json.load(open(ctl, encoding="utf-8")).get("cmd", "")
            except Exception:
                pass
            if cmd == "run":          # 只在页面点「开始」后的那一次拉起
                with open(ctl, "w", encoding="utf-8") as f:
                    json.dump({"cmd": "running"}, f)
                r = sp.run(["pgrep", "-f", "pdd_runner.py"], capture_output=True, text=True)
                if not r.stdout.strip():
                    with open(out, "a") as f:
                        sp.Popen([py, runner], stdout=f, stderr=f, start_new_session=True)
        except Exception:
            pass
        _t.sleep(30)


def main():
    os.chdir(BASE_DIR)
    load_data()
    live_engine.start()
    import threading as _th
    _th.Thread(target=_pdd_watchdog, daemon=True, name="pdd-watchdog").start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"quant sim on http://0.0.0.0:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
