#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A 股监控 Web 面板 —— 复用 main.py 的抓取逻辑，零第三方依赖。"""
from __future__ import annotations

import glob
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import main as mon  # noqa: E402

CFG_PATH = BASE / "config.json"
WEB_DIR = BASE / "web"
IPTV_DIR = Path("/home/astock/.dsh/dsh_wechat/iptv")
PORT = 8080

_sig_cache = {"ts": 0.0, "data": None, "running": False}
_sig_lock = threading.Lock()

ARIA2_RPC = "http://127.0.0.1:6800/jsonrpc"
ARIA2_SECRET = "astock"
MEDIA_DIR = "/opt/media/movies"
FAV_DIR = "/opt/media/收藏"
FILES_ROOT = "/"
UPLOAD_TMP = "/opt/media/.upload"   # 分片临时目录（磁盘，不用 /tmp 内存盘）

_ffmpeg_tasks = {}
_ffmpeg_lock = threading.Lock()


def _ffmpeg_worker(task_id, url, out_path):
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", url, "-c", "copy", out_path],
            capture_output=True, text=True, timeout=7200,
        )
        with _ffmpeg_lock:
            task = _ffmpeg_tasks.get(task_id)
            if task:
                task["status"] = "complete" if proc.returncode == 0 else "error"
                if proc.returncode != 0:
                    task["error"] = (proc.stderr or "")[-300:]
    except Exception as exc:  # noqa: BLE001
        with _ffmpeg_lock:
            task = _ffmpeg_tasks.get(task_id)
            if task:
                task["status"] = "error"
                task["error"] = str(exc)


def start_ffmpeg_task(url, name):
    task_id = f"ff{int(time.time() * 1000)}"
    out_path = os.path.join(MEDIA_DIR, name)
    with _ffmpeg_lock:
        _ffmpeg_tasks[task_id] = {
            "gid": task_id, "name": name, "status": "active",
            "total": 0, "done": 0, "speed": 0, "pct": 0, "kind": "ffmpeg",
        }
    threading.Thread(target=_ffmpeg_worker, args=(task_id, url, out_path), daemon=True).start()
    return task_id



def aria2_call(method, params=None, timeout=10):
    payload = {
        "jsonrpc": "2.0",
        "id": "astock",
        "method": method,
        "params": [f"token:{ARIA2_SECRET}"] + (params or []),
    }
    req = urllib.request.Request(
        ARIA2_RPC,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


QB_URL = "http://127.0.0.1:8081"

# ---- 代理（mihomo）----
MIHOMO_API = "http://127.0.0.1:9090"
MIHOMO_GROUP = "🚀 节点选择"
SUB_URL = ("https://45.137.180.144/L?ZG93bmxvYWRDb25maWcuanMvMzkzZGRhN2Fm"
           "YjhhN2FmZTNjN2YxMTIxOTA5ODQzZmQ/dGltZXN0YW1wPTE0MjQ4NA==")
_sub_cache = {"ts": 0.0, "data": None}


def mihomo_get(path, timeout=8):
    with urllib.request.urlopen(MIHOMO_API + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def mihomo_put(path, data, timeout=8):
    req = urllib.request.Request(
        MIHOMO_API + path,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def fetch_sub_headers():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(SUB_URL, headers={"User-Agent": "clash-verge/1.7.7"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        return resp.headers


def parse_multipart(body, content_type):
    """标准库解析 multipart/form-data，返回 [(filename, data), ...]。"""
    from email.parser import BytesParser
    from email.policy import default
    raw = b"Content-Type: " + content_type.encode("utf-8") + b"\r\n\r\n" + body
    msg = BytesParser(policy=default).parsebytes(raw)
    files = []
    if msg.is_multipart():
        for part in msg.iter_parts():
            filename = part.get_filename()
            if not filename:
                continue
            data = part.get_payload(decode=True) or b""
            files.append((filename, data))
    return files


def stream_upload(rfile, length, content_type, target_dir):
    """流式解析 multipart/form-data，边读边写，不占内存。返回保存的文件名列表。"""
    import re
    m = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not m:
        return []
    delim = b"--" + m.group(1).encode()
    base = os.path.realpath(target_dir)
    state = "preamble"
    buf = b""
    fh = None
    current = None
    saved = []
    remaining = length

    while remaining > 0:
        chunk = rfile.read(min(256 * 1024, remaining))
        if not chunk:
            break
        remaining -= len(chunk)
        buf += chunk
        progressed = True
        while progressed:
            progressed = False
            if state == "preamble":
                idx = buf.find(delim)
                if idx >= 0:
                    buf = buf[idx + len(delim):]
                    state = "headers"
                    progressed = True
            elif state == "headers":
                idx = buf.find(b"\r\n\r\n")
                if idx >= 0:
                    block = buf[:idx]
                    buf = buf[idx + 4:]
                    fname = None
                    for line in block.split(b"\r\n"):
                        if b"filename=" in line.lower():
                            raw = line.split(b"filename=", 1)[1].strip().strip(b'"')
                            fname = raw.decode("utf-8", "replace")
                            break
                    current = None
                    if fname:
                        safe = os.path.basename(fname.replace("\\", "/")).strip()
                        if safe and safe not in (".", ".."):
                            dest = os.path.join(base, safe)
                            if os.path.realpath(dest).startswith(base + os.sep):
                                fh = open(dest, "wb")
                                current = safe
                    state = "body"
                    progressed = True
            elif state == "body":
                idx = buf.find(b"\r\n" + delim)
                if idx >= 0:
                    if fh:
                        fh.write(buf[:idx])
                        fh.close()
                        fh = None
                        if current:
                            saved.append(current)
                    buf = buf[idx + 2 + len(delim):]
                    if buf.startswith(b"--"):
                        state = "done"
                    else:
                        if buf.startswith(b"\r\n"):
                            buf = buf[2:]
                        state = "headers"
                    progressed = True
                else:
                    keep = len(delim) + 4
                    if len(buf) > keep:
                        if fh:
                            fh.write(buf[:-keep])
                        buf = buf[-keep:]
                    break
            else:
                break
        if state == "done":
            break
    if fh:
        fh.close()
        if current:
            saved.append(current)
    return saved


def qb_get(path, timeout=10):
    with urllib.request.urlopen(QB_URL + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def qb_post(path, data, timeout=15):
    req = urllib.request.Request(
        QB_URL + path,
        data=urllib.parse.urlencode(data).encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _cfg():
    return mon.load_config(CFG_PATH)


def compute_signal(bars, unit: str = "日"):
    """由 K 线计算 5 档多空信号（与 monitor_gui.py 同规则）。"""
    if not bars or len(bars) < 30:
        return None
    closes = [b.close for b in bars]
    n = len(bars)

    def ma(period: int, index: int):
        if index < 0 or index + 1 < period:
            return None
        window = closes[index + 1 - period:index + 1]
        return sum(window) / period

    last = n - 1
    close = closes[last]
    ma5, ma10, ma20 = ma(5, last), ma(10, last), ma(20, last)
    score = 0
    reasons = []

    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            score += 2
            reasons.append(f"多头排列 MA5({ma5:.2f})>MA10({ma10:.2f})>MA20({ma20:.2f})")
        elif ma5 < ma10 < ma20:
            score -= 2
            reasons.append(f"空头排列 MA5({ma5:.2f})<MA10({ma10:.2f})<MA20({ma20:.2f})")
        elif ma5 > ma10:
            score += 1
            reasons.append(f"MA5({ma5:.2f}) 在 MA10({ma10:.2f}) 上方")
        elif ma5 < ma10:
            score -= 1
            reasons.append(f"MA5({ma5:.2f}) 在 MA10({ma10:.2f}) 下方")

        if close > ma20:
            score += 1
            reasons.append(f"收盘价在 MA20({ma20:.2f}) 上方")
        elif close < ma20:
            score -= 1
            reasons.append(f"收盘价在 MA20({ma20:.2f}) 下方")

        for offset in range(1, 4):
            idx = last - offset
            if idx < 20:
                break
            prev5, prev10 = ma(5, idx - 1), ma(10, idx - 1)
            cur5, cur10 = ma(5, idx), ma(10, idx)
            if None in (prev5, prev10, cur5, cur10):
                continue
            if prev5 <= prev10 and cur5 > cur10:
                score += 2
                reasons.append(f"{bars[idx].date} MA5 上穿 MA10（金叉）")
                break
            if prev5 >= prev10 and cur5 < cur10:
                score -= 2
                reasons.append(f"{bars[idx].date} MA5 下穿 MA10（死叉）")
                break

    if n > 5 and closes[last - 5]:
        momentum = (close - closes[last - 5]) / closes[last - 5] * 100
        if momentum >= 3:
            score += 1
        elif momentum <= -3:
            score -= 1
        reasons.append(f"近 5 {unit} {momentum:+.2f}%")

    if n >= 20:
        vol5 = sum(b.volume for b in bars[-5:]) / 5
        vol20 = sum(b.volume for b in bars[-20:]) / 20
        if vol20 > 0:
            ratio = vol5 / vol20
            if ratio >= 1.5:
                reasons.append(f"近 5 {unit}放量（{ratio:.2f} 倍于 20 {unit}均量）")
            elif ratio <= 0.6:
                reasons.append(f"近 5 {unit}缩量（{ratio:.2f} 倍于 20 {unit}均量）")
            else:
                reasons.append(f"成交量平稳（{ratio:.2f} 倍于 20 {unit}均量）")

    if score >= 4:
        label = "看涨"
    elif score >= 2:
        label = "偏多"
    elif score <= -4:
        label = "看跌"
    elif score <= -2:
        label = "偏空"
    else:
        label = "中性"
    return {"score": score, "label": label, "reasons": reasons}


class Handler(BaseHTTPRequestHandler):
    server_version = "AStockWeb/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, data: bytes, ctype: str, code: int = 200, filename: str = ""):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", code)

    @staticmethod
    def _dsh_token():
        """从 dsh-web 的 journal 里读最新启动的访问 token。"""
        try:
            out = subprocess.run(
                ["journalctl", "-u", "dsh-web", "--no-pager", "-n", "300"],
                capture_output=True, text=True, timeout=6,
            ).stdout
            found = re.findall(r"token=([A-Za-z0-9_-]{10,})", out)
            return found[-1] if found else ""
        except Exception:  # noqa: BLE001
            return ""

    # ---------------- GET ----------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/portal", "/index.html"):
                self._send((WEB_DIR / "portal.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/tv":
                self._send((IPTV_DIR / "tv.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/tv/status":
                self._json(self.api_tv_status())
            elif u.path == "/api/tv/list":
                self._json(self.api_tv_list())
            elif u.path == "/monitor":
                self._send((WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/status":
                self._send((WEB_DIR / "status.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/files":
                self._send((WEB_DIR / "files.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/media":
                self._send((WEB_DIR / "media.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path in ("/gold", "/gold2"):
                fp = WEB_DIR / "gold.html"
                if fp.exists():
                    # 服务端把当前行情直接塞进页面，前端 fetch 挂了也能看到数
                    try:
                        _d = self.api_gold()
                        _d["_ts"] = time.strftime("%H:%M:%S")
                        seed = json.dumps(_d, ensure_ascii=False)
                    except Exception:
                        seed = "{}"
                    html = fp.read_text(encoding="utf-8").replace(
                        "window.GOLD_SEED = null;", "window.GOLD_SEED = " + seed + ";")
                    self._send(html.encode("utf-8"), "text/html; charset=utf-8")
                else:
                    self._json({"error": "not built"}, 404)
            elif u.path == "/db":
                fp = WEB_DIR / "db.html"
                if fp.exists():
                    self._send(fp.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._json({"error": "db.html not built"}, 404)
            elif u.path == "/api/db/tables":
                try:
                    self._json(self.api_db_tables())
                except Exception as e:
                    self._json({"error": str(e)}, 500)
            elif u.path == "/api/db/query":
                try:
                    self._json(self.api_db_query(body))
                except Exception as e:
                    self._json({"ok": False, "error": str(e)})
            elif u.path == "/api/gold":
                try:
                    self._json(self.api_gold())
                except Exception as e:
                    self._json({"error": str(e)}, 500)
            elif u.path == "/api/gold/kline":
                try:
                    self._json(self.api_gold_kline(q))
                except Exception as e:
                    self._json({"error": str(e)}, 500)
            elif u.path == "/api/gold/kline.png":
                try:
                    cost = float((q.get("cost") or ["877.47"])[0])
                except (TypeError, ValueError):
                    cost = 877.47
                try:
                    self._gold_kline_png(cost)
                except Exception as e:
                    self._json({"error": str(e)}, 500)
            elif u.path == "/sai.apk":
                fp = WEB_DIR / "sai.apk"
                if fp.exists():
                    self._send(fp.read_bytes(), "application/vnd.android.package-archive", filename="SAI-4.5.apk")
                else:
                    self._json({"error": "not found"}, 404)
            elif u.path == "/x-universal.apk":
                fp = WEB_DIR / "x-universal.apk"
                if fp.exists():
                    self._send(fp.read_bytes(), "application/vnd.android.package-archive",
                               filename="X-universal.apk")
                else:
                    self._json({"error": "not found"}, 404)
            elif u.path == "/x.apk":
                fp = WEB_DIR / "x-base.apk"
                if fp.exists():
                    self._send(fp.read_bytes(), "application/vnd.android.package-archive", filename="X-10.apk")
                else:
                    self._json({"error": "not found"}, 404)
            elif u.path == "/x-xapk":
                fp = WEB_DIR / "x.apk"
                if fp.exists():
                    self._send(fp.read_bytes(), "application/octet-stream", filename="X-10.xapk")
                else:
                    self._json({"error": "not found"}, 404)
            elif u.path == "/paypal-universal.apk":
                fp = WEB_DIR / "paypal-universal.apk"
                if fp.exists():
                    self._send(fp.read_bytes(), "application/vnd.android.package-archive",
                               filename="PayPal-universal.apk")
                else:
                    self._json({"error": "not found"}, 404)
            elif u.path == "/paypal.apk":
                fp = WEB_DIR / "paypal-base.apk"
                if fp.exists():
                    self._send(fp.read_bytes(), "application/vnd.android.package-archive", filename="PayPal.apk")
                else:
                    self._json({"error": "not found"}, 404)
            elif u.path == "/paypal-xapk":
                fp = WEB_DIR / "paypal.apk"
                if fp.exists():
                    self._send(fp.read_bytes(), "application/octet-stream", filename="PayPal.xapk")
                else:
                    self._json({"error": "not found"}, 404)
            elif u.path == "/tailscale.apk":
                p = WEB_DIR / "tailscale.apk"
                if not p.exists():
                    self._json({"error": "apk not found"}, 404)
                else:
                    data = p.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.android.package-archive")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition", 'attachment; filename="tailscale.apk"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
            elif u.path in ("/tv-ysk.apk", "/tv-tvbox.apk"):
                fn = "tv-ysk.apk" if u.path == "/tv-ysk.apk" else "tv-tvbox.apk"
                ap = WEB_DIR / fn
                if not ap.exists():
                    self._json({"error": "apk not found"}, 404)
                else:
                    data = ap.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.android.package-archive")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition", f'attachment; filename="{fn}"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
            elif u.path == "/tvbro.apk":
                p = WEB_DIR / "tvbro.apk"
                if not p.exists():
                    self._json({"error": "apk not found"}, 404)
                else:
                    data = p.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.android.package-archive")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition", 'attachment; filename="tvbro.apk"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
            elif u.path == "/quark.apk":
                p = WEB_DIR / "quark.apk"
                if not p.exists():
                    self._json({"error": "apk not found"}, 404)
                else:
                    data = p.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.android.package-archive")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition", 'attachment; filename="quark.apk"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
            elif u.path == "/jellyfin-tv.apk":
                p = WEB_DIR / "jellyfin-tv.apk"
                if not p.exists():
                    self._json({"error": "apk not found"}, 404)
                else:
                    data = p.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.android.package-archive")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition", 'attachment; filename="jellyfin-tv.apk"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
            elif u.path == "/aegis.apk":
                p = WEB_DIR / "aegis.apk"
                if not p.exists():
                    self._json({"error": "apk not found"}, 404)
                else:
                    data = p.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.android.package-archive")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition", 'attachment; filename="aegis.apk"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
            elif u.path == "/dsh":
                token = self._dsh_token()
                host = (self.headers.get("Host") or "").split(":")[0] or "192.168.0.104"
                target = (f"http://{host}:3100/?token={token}" if token
                          else f"http://{host}:3100/")
                self.send_response(302)
                self.send_header("Location", target)
                self.end_headers()
            elif u.path == "/quant":
                host = (self.headers.get("Host") or "").split(":")[0] or "192.168.0.104"
                self.send_response(302)
                self.send_header("Location", f"http://{host}:8082/live")
                self.end_headers()
            elif u.path == "/qbit":
                host = (self.headers.get("Host") or "").split(":")[0] or "192.168.0.104"
                self.send_response(302)
                self.send_header("Location", f"http://{host}:8081/")
                self.end_headers()
            elif u.path == "/jf":
                host = (self.headers.get("Host") or "").split(":")[0] or "192.168.0.104"
                self.send_response(302)
                self.send_header("Location", f"http://{host}:8096/")
                self.end_headers()
            elif u.path == "/api/files/list":
                self._json(self.api_files_list((q.get("path") or [""])[0]))
            elif u.path == "/proxy":
                self._send((WEB_DIR / "proxy.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/proxy/status":
                self._json(self.api_proxy_status())
            elif u.path == "/api/deepseek/balance":
                self._json(self.api_deepseek_balance())
            elif u.path == "/api/proxy/traffic":
                self._json(self.api_proxy_traffic())
            elif u.path == "/api/proxy/nodes":
                self._json(self.api_proxy_nodes())
            elif u.path == "/api/downloads":
                self._json(self.api_downloads())
            elif u.path == "/api/media/list":
                self._json(self.api_media_list())
            elif u.path == "/api/meta":
                self._json(self.api_meta())
            elif u.path == "/api/sysinfo":
                self._json(api_sysinfo())
            elif u.path == "/api/jellyfin/status":
                self._json(self.api_jellyfin_status())
            elif u.path == "/rss":
                self._send((WEB_DIR / "rss.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/rss/digest":
                self._send(_rss_digest_html().encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/api/rss/digest":
                self._json(_rss_digest_data())
            elif u.path == "/api/rss/feeds":
                self._json(self.api_rss_feeds())
            elif u.path == "/api/rss/import/status":
                self._json(self.api_rss_import_status())
            elif u.path == "/api/dsh/status":
                self._json(self.api_dsh_status())
            elif u.path == "/api/qbit/status":
                self._json(self.api_qbit_status())
            elif u.path == "/api/mihomo/limit":
                self._json(self.api_mihomo_limit())
            elif u.path == "/api/domain":
                self._json(api_domain_info())
            elif u.path == "/quant/logs":
                self._send((WEB_DIR / "quant_logs.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/quant/logs":
                self._json(api_quant_logs())
            elif u.path == "/api/quant/bans":
                self._json(api_quant_bans())
            elif u.path == "/api/quotes":
                self._json(self.api_quotes())
            elif u.path == "/api/signals":
                self._json(self.api_signals())
            elif u.path == "/api/kline":
                code = (q.get("code") or [""])[0]
                period = (q.get("period") or ["day"])[0]
                count = int((q.get("count") or ["120"])[0])
                bars = mon.fetch_kline(code, count, period)
                self._json({"code": code, "period": period, "bars": [asdict(b) for b in bars]})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---------------- POST ----------------
    def do_POST(self):
        u = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            ctype = self.headers.get("Content-Type") or ""
            # 文件上传走流式 multipart，边读边写，不占内存
            if u.path == "/api/files/upload":
                self._json(self.api_files_upload_stream(self.rfile, length, ctype, u))
                return
            # 分片上传（大文件），每片 raw body
            if u.path == "/api/upload/chunk":
                self._json(self.api_upload_chunk(self.rfile, length, u))
                return
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8") or "{}")
            if u.path == "/api/watchlist":
                self._json(self.api_watchlist(body))
            elif u.path == "/api/db/query":
                self._json(self.api_db_query(body))
            elif u.path == "/api/cost":
                self._json(self.api_cost(body))
            elif u.path == "/api/download":
                self._json(self.api_download(body))
            elif u.path == "/api/download/cancel":
                self._json(self.api_download_cancel(body))
            elif u.path == "/api/media/delete":
                self._json(self.api_media_delete(body))
            elif u.path == "/api/media/favorite":
                self._json(self.api_media_favorite(body))
            elif u.path == "/api/files/delete":
                self._json(self.api_files_delete(body))
            elif u.path == "/api/files/rename":
                self._json(self.api_files_rename(body))
            elif u.path == "/api/jellyfin/toggle":
                self._json(self.api_jellyfin_toggle(body))
            elif u.path == "/api/mihomo/limit":
                self._json(self.api_mihomo_limit_set(body))
            elif u.path == "/api/proxy/toggle":
                self._json(self.api_proxy_toggle(body))
            elif u.path == "/api/proxy/select":
                self._json(self.api_proxy_select(body))
            elif u.path == "/api/proxy/speedtest":
                self._json(self.api_proxy_speedtest())
            elif u.path == "/api/upload/finish":
                self._json(self.api_upload_finish(body))
            elif u.path == "/api/domain":
                self._json(api_domain_set(body))
            elif u.path == "/api/quant/unban":
                self._json(api_quant_unban(body))
            elif u.path == "/api/tv/toggle":
                self._json(self.api_tv_toggle(body))
            elif u.path == "/api/tv/refresh":
                self._json(self.api_tv_refresh(body))
            elif u.path == "/api/rss/feeds":
                self._json(self.api_rss_feeds_set(body))
            elif u.path == "/api/rss/run":
                self._json(self.api_rss_run())
            elif u.path == "/api/rss/import":
                self._json(self.api_rss_import(body))
            elif u.path == "/api/dsh/toggle":
                self._json(self.api_dsh_toggle(body))
            elif u.path == "/api/qbit/toggle":
                self._json(self.api_qbit_toggle(body))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---------------- 业务 ----------------
    def api_meta(self):
        cfg = _cfg()
        trading, label = mon.market_status(cfg)
        return {
            "trading": trading,
            "status": label,
            "groups": cfg.get("gui", {}).get("groups") or ["自选池"],
            "interval": int(cfg.get("poll_interval") or 15),
            "watchlist": cfg.get("watchlist", []),
            "source": cfg.get("source", "auto"),
        }

    def api_quotes(self):
        cfg = _cfg()
        watchlist = cfg.get("watchlist", [])
        feed = mon.build_feed(cfg.get("source", "auto"), watchlist, cfg)
        quotes = feed.fetch([w["code"] for w in watchlist])
        rows = []
        for item in watchlist:
            code = item.get("code")
            q = quotes.get(code)
            if not q:
                continue
            row = asdict(q)
            row["group"] = item.get("group", "")
            cost = item.get("cost")
            shares = item.get("shares")
            row["cost"] = cost
            row["shares"] = shares
            pnl = None
            pnl_amount = None
            if cost and row.get("price"):
                try:
                    pnl = (float(row["price"]) - float(cost)) / float(cost) * 100.0
                    if shares:
                        pnl_amount = (float(row["price"]) - float(cost)) * float(shares)
                except (TypeError, ValueError, ZeroDivisionError):
                    pnl = None
            row["pnl"] = pnl
            row["pnl_amount"] = pnl_amount
            rows.append(row)
        trading, label = mon.market_status(cfg)
        return {
            "time": datetime.now().strftime("%H:%M:%S"),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "trading": trading,
            "status": label,
            "quotes": rows,
        }

    def api_signals(self):
        """信号计算很重（每只 2 次 K 线请求），改为后台线程算，接口立即返回缓存。"""
        now = time.time()
        with _sig_lock:
            if _sig_cache["data"] and now - _sig_cache["ts"] < 600:
                return _sig_cache["data"]
            if not _sig_cache["running"]:
                _sig_cache["running"] = True
                threading.Thread(target=_compute_signals, daemon=True).start()
            return _sig_cache["data"] or {}

    def api_watchlist(self, body):
        action = str(body.get("action") or "")
        cfg = _cfg()
        watchlist = cfg.setdefault("watchlist", [])
        if action == "add":
            raw = str(body.get("code") or "").strip().lower()
            code = mon.guess_market_prefix(raw)
            if any(w.get("code") == code for w in watchlist):
                return {"ok": False, "msg": "已存在"}
            group = str(body.get("group") or (cfg.get("gui", {}).get("groups") or ["自选池"])[0])
            watchlist.append({"code": code, "name": body.get("name") or code, "group": group})
        elif action == "remove":
            code = str(body.get("code") or "").strip().lower()
            cfg["watchlist"] = [w for w in watchlist if w.get("code") != code]
        elif action == "move":
            code = str(body.get("code") or "").strip().lower()
            group = str(body.get("group") or "")
            for w in watchlist:
                if w.get("code") == code:
                    w["group"] = group
        else:
            return {"ok": False, "msg": "unknown action"}
        _save_cfg(cfg)
        return {"ok": True}

    def api_cost(self, body):
        code = str(body.get("code") or "").strip().lower()
        cfg = _cfg()
        groups = cfg.get("gui", {}).get("groups") or ["自选池", "持仓池"]
        watch_group = groups[0] if groups else "自选池"
        hold_group = groups[1] if len(groups) > 1 else (groups[0] if groups else "持仓池")
        for w in cfg.get("watchlist", []):
            if w.get("code") == code:
                try:
                    cost = body.get("cost")
                    shares = body.get("shares")
                    w["cost"] = float(cost) if cost not in (None, "", 0, "0") else None
                    w["shares"] = float(shares) if shares not in (None, "") else None
                except (TypeError, ValueError):
                    return {"ok": False, "msg": "invalid number"}
                # 成本驱动分栏：有成本 -> 持仓池；无成本 -> 自选池
                w["group"] = hold_group if w["cost"] else watch_group
                _save_cfg(cfg)
                return {"ok": True, "group": w["group"]}
        return {"ok": False, "msg": "not found"}

    # ---------------- 下载 ----------------
    def api_download(self, body):
        url = str(body.get("url") or "").strip()
        if not url:
            return {"ok": False, "msg": "链接为空"}
        name = str(body.get("name") or "").strip()
        # m3u8 是流索引，交给 ffmpeg 下载分片并合并
        if ".m3u8" in url.lower():
            if not name:
                name = f"video_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
            if not name.lower().endswith(".mp4"):
                name += ".mp4"
            name = name.replace("/", "_").replace("\\", "_")
            task_id = start_ffmpeg_task(url, name)
            return {"ok": True, "gid": task_id, "mode": "ffmpeg", "name": name}
        # 磁力 / BT 种子 -> qBittorrent
        low = url.lower()
        if low.startswith("magnet:") or low.endswith(".torrent"):
            try:
                qb_post("/api/v2/torrents/add", {"urls": url, "savepath": MEDIA_DIR})
                return {"ok": True, "mode": "qbittorrent"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "msg": f"qBittorrent: {exc}"}
        try:
            res = aria2_call("aria2.addUri", [[url]])
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": f"{type(exc).__name__}: {exc}"}
        if "error" in res:
            return {"ok": False, "msg": res["error"].get("message", "aria2 error")}
        return {"ok": True, "gid": res.get("result")}

    # ---------------- 终止下载 ----------------
    def api_download_cancel(self, body):
        kind = str(body.get("kind") or "")
        ident = str(body.get("id") or "")
        if not ident:
            return {"ok": False, "msg": "缺少任务 ID"}
        if kind == "aria2":
            try:
                aria2_call("aria2.remove", [ident])
                return {"ok": True}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "msg": str(exc)}
        if kind == "bt":
            try:
                qb_post("/api/v2/torrents/delete",
                        {"hashes": ident, "deleteFiles": "false"})
                return {"ok": True}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "msg": str(exc)}
        return {"ok": False, "msg": "未知任务类型"}

    def api_downloads(self):
        def slim(item):
            files = item.get("files") or [{}]
            path = files[0].get("path") or ""
            total = int(item.get("totalLength") or 0)
            done = int(item.get("completedLength") or 0)
            return {
                "gid": item.get("gid"),
                "name": path.split("/")[-1] or "(等待中)",
                "status": item.get("status"),
                "total": total,
                "done": done,
                "speed": int(item.get("downloadSpeed") or 0),
                "pct": round(done / total * 100, 1) if total else 0,
            }
        try:
            active = aria2_call("aria2.tellActive").get("result", [])
            waiting = aria2_call("aria2.tellWaiting", [0, 20]).get("result", [])
            stopped = aria2_call("aria2.tellStopped", [0, 10]).get("result", [])
        except Exception as exc:  # noqa: BLE001
            return {"error": f"aria2 不可用: {type(exc).__name__}: {exc}"}
        with _ffmpeg_lock:
            ff = [dict(t) for t in _ffmpeg_tasks.values()]
        bt = []
        try:
            for t in qb_get("/api/v2/torrents/info"):
                size = int(t.get("size") or 0)
                progress = float(t.get("progress") or 0)
                bt.append({
                    "gid": t.get("hash"),
                    "name": t.get("name") or "",
                    "status": "active" if progress < 1 else "complete",
                    "total": size,
                    "done": int(size * progress),
                    "speed": int(t.get("dlspeed") or 0),
                    "pct": round(progress * 100, 1),
                    "kind": "bt",
                })
        except Exception:  # noqa: BLE001
            bt = []
        return {
            "active": [slim(i) for i in active],
            "waiting": [slim(i) for i in waiting],
            "stopped": [slim(i) for i in stopped],
            "ffmpeg": ff,
            "bt": bt,
        }

    # ---------------- 影视库 ----------------
    def api_media_list(self):
        files = []
        for folder, base in (("movies", MEDIA_DIR), ("fav", FAV_DIR)):
            try:
                for name in os.listdir(base):
                    path = os.path.join(base, name)
                    if not os.path.isfile(path):
                        continue
                    st = os.stat(path)
                    files.append({
                        "name": name,
                        "folder": folder,
                        "size": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    })
            except OSError:
                pass
        files.sort(key=lambda x: x["mtime"], reverse=True)
        return {"files": files}

    def api_media_favorite(self, body):
        """收藏：movies <-> 收藏 之间移动。action=favorite / unfavorite。"""
        name = str(body.get("name") or "").strip()
        action = str(body.get("action") or "favorite").strip()
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            return {"ok": False, "msg": "非法文件名"}
        src_dir, dst_dir = (MEDIA_DIR, FAV_DIR) if action == "favorite" else (FAV_DIR, MEDIA_DIR)
        src = os.path.realpath(os.path.join(src_dir, name))
        if not src.startswith(os.path.realpath(src_dir) + os.sep):
            return {"ok": False, "msg": "路径非法"}
        if not os.path.isfile(src):
            return {"ok": False, "msg": "文件不存在"}
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, name)
        if os.path.exists(dst):
            return {"ok": False, "msg": "目标已存在同名文件"}
        try:
            shutil.move(src, dst)
            return {"ok": True, "msg": "已移到 " + ("收藏" if action == "favorite" else "电影目录")}
        except OSError as exc:
            return {"ok": False, "msg": str(exc)}

    def api_media_delete(self, body):
        name = str(body.get("name") or "").strip()
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            return {"ok": False, "msg": "非法文件名"}
        base = os.path.realpath(MEDIA_DIR)
        target = os.path.realpath(os.path.join(MEDIA_DIR, name))
        if not target.startswith(base + os.sep):
            return {"ok": False, "msg": "路径非法"}
        try:
            os.remove(target)
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "msg": str(exc)}

    # ---------------- 文件管理 ----------------
    def api_files_list(self, path):
        rel = str(path or "").strip()
        if not rel:
            rel = "/"
        if not rel.startswith("/"):
            rel = "/" + rel
        target = os.path.realpath(rel)
        if not target.startswith("/"):
            return {"error": "路径非法"}
        if not os.path.isdir(target):
            return {"error": "目录不存在或不是目录"}
        items = []
        try:
            for name in os.listdir(target):
                full = os.path.join(target, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                is_dir = os.path.isdir(full)
                items.append({
                    "name": name,
                    "is_dir": is_dir,
                    "size": 0 if is_dir else st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "rel": full,
                })
        except OSError as exc:
            return {"error": str(exc)}
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {"path": target, "items": items}

    def api_files_delete(self, body):
        rel = str(body.get("path") or "").strip()
        if not rel:
            return {"ok": False, "msg": "路径为空"}
        if not rel.startswith("/"):
            rel = "/" + rel
        target = os.path.realpath(rel)
        if target == "/":
            return {"ok": False, "msg": "不能删除根目录"}
        if not target.startswith("/"):
            return {"ok": False, "msg": "路径非法"}
        try:
            if os.path.isdir(target):
                os.rmdir(target)   # 只删空目录
            else:
                os.remove(target)
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "msg": str(exc)}

    def api_files_rename(self, body):
        rel = str(body.get("path") or "").strip()
        new_name = str(body.get("name") or "").strip()
        if not rel or not new_name:
            return {"ok": False, "msg": "路径或新名称为空"}
        if "/" in new_name or "\\" in new_name or new_name in (".", ".."):
            return {"ok": False, "msg": "新名称不能包含路径分隔符"}
        if not rel.startswith("/"):
            rel = "/" + rel
        src = os.path.realpath(rel)
        if not os.path.exists(src):
            return {"ok": False, "msg": "文件不存在"}
        dst = os.path.join(os.path.dirname(src), new_name)
        if os.path.exists(dst):
            return {"ok": False, "msg": "同名目标已存在"}
        try:
            os.rename(src, dst)
            return {"ok": True, "path": dst}
        except OSError as exc:
            return {"ok": False, "msg": str(exc)}

    # ---------------- 上传（流式，支持大文件） ----------------
    def api_files_upload_stream(self, rfile, length, content_type, u):
        path = (parse_qs(u.query).get("path") or ["/"])[0]
        if not path.startswith("/"):
            path = "/" + path
        target_dir = os.path.realpath(path)
        if not os.path.isdir(target_dir):
            return {"ok": False, "msg": "目标目录不存在"}
        if length <= 0:
            return {"ok": False, "msg": "没有数据"}
        saved = stream_upload(rfile, length, content_type, target_dir)
        if not saved:
            return {"ok": False, "msg": "没有收到文件"}
        return {"ok": True, "saved": saved}

    # ---------------- 分片上传（大文件） ----------------
    def api_upload_chunk(self, rfile, length, u):
        q = parse_qs(u.query)
        name = (q.get("name") or [""])[0]
        path = (q.get("path") or ["/"])[0]
        try:
            index = int((q.get("index") or ["0"])[0])
        except ValueError:
            return {"ok": False, "msg": "分片序号非法"}
        safe = os.path.basename(str(name).replace("\\", "/")).strip()
        if not safe or safe in (".", ".."):
            return {"ok": False, "msg": "文件名非法"}
        if not path.startswith("/"):
            path = "/" + path
        target_dir = os.path.realpath(path)
        if not os.path.isdir(target_dir):
            return {"ok": False, "msg": "目标目录不存在"}
        tmp = os.path.join(UPLOAD_TMP, f"{safe}.part")
        mode = "wb" if index == 0 else "ab"
        try:
            os.makedirs(UPLOAD_TMP, exist_ok=True)
            with open(tmp, mode) as fh:
                remaining = length
                while remaining > 0:
                    chunk = rfile.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)
            return {"ok": True, "index": index}
        except OSError as exc:
            return {"ok": False, "msg": str(exc)}

    def api_upload_finish(self, body):
        name = str(body.get("name") or "").strip()
        path = str(body.get("path") or "/").strip()
        safe = os.path.basename(name.replace("\\", "/")).strip()
        if not safe or safe in (".", ".."):
            return {"ok": False, "msg": "文件名非法"}
        if not path.startswith("/"):
            path = "/" + path
        target_dir = os.path.realpath(path)
        if not os.path.isdir(target_dir):
            return {"ok": False, "msg": "目标目录不存在"}
        tmp = os.path.join(UPLOAD_TMP, f"{safe}.part")
        if not os.path.exists(tmp):
            return {"ok": False, "msg": "没有找到上传的分片"}
        dest = os.path.join(target_dir, safe)
        if not os.path.realpath(dest).startswith(target_dir + os.sep):
            return {"ok": False, "msg": "路径非法"}
        try:
            shutil.move(tmp, dest)
            return {"ok": True, "saved": safe, "size": os.path.getsize(dest)}
        except OSError as exc:
            return {"ok": False, "msg": str(exc)}

    # ---------------- DeepSeek 余额 ----------------
    def api_deepseek_balance(self):
        key = os.environ.get("DEEPSEEK_API_KEY") or ""
        if not key:
            return {"error": "未配置 DEEPSEEK_API_KEY"}
        try:
            req = urllib.request.Request(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            infos = data.get("balance_infos") or []
            if infos:
                info = infos[0]
                return {
                    "ok": True,
                    "currency": info.get("currency"),
                    "total": info.get("total_balance"),
                    "topped_up": info.get("topped_up_balance"),
                    "granted": info.get("granted_balance"),
                    "available": data.get("is_available"),
                }
            return {"ok": True, "total": "0", "currency": "CNY"}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    # ---------------- 代理 ----------------
    def api_proxy_status(self):
        try:
            v = mihomo_get("/version", timeout=5)
            return {"running": True, "version": v.get("version", "")}
        except Exception:  # noqa: BLE001
            return {"running": False}

    def api_mihomo_limit(self):
        try:
            with open("/opt/astock/mihomo_guard_limit", encoding="utf-8") as fh:
                n = int((fh.readline() or "600").strip())
        except (OSError, ValueError):
            n = 600
        return {"limit": n}

    def api_mihomo_limit_set(self, body):
        try:
            n = int(body.get("limit") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "请输入数字"}
        if n < 200 or n > 3000:
            return {"ok": False, "msg": "范围 200 ~ 3000 MB"}
        try:
            with open("/opt/astock/mihomo_guard_limit", "w", encoding="utf-8") as fh:
                fh.write(f"{n}\n")
            return {"ok": True, "limit": n}
        except OSError as exc:
            return {"ok": False, "msg": str(exc)}

    def api_jellyfin_status(self):
        try:
            r = subprocess.run(
                ["sudo", "-n", "docker", "inspect", "-f", "{{.State.Status}}", "jellyfin"],
                capture_output=True, text=True, timeout=20,
            )
            st = (r.stdout or "").strip()
            return {"running": st == "running", "status": st or "unknown"}
        except Exception as exc:  # noqa: BLE001
            return {"running": False, "status": f"error: {exc}"}

    def api_jellyfin_toggle(self, body):
        action = str(body.get("action") or "").strip()
        if action not in ("start", "stop", "restart"):
            return {"ok": False, "msg": "invalid action"}
        try:
            r = subprocess.run(
                ["sudo", "-n", "docker", action, "jellyfin"],
                capture_output=True, text=True, timeout=90,
            )
            ok = r.returncode == 0
            return {"ok": ok, "msg": "" if ok else (r.stderr or r.stdout or "")[-200:]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": str(exc)}

    # ---------------- 电视直播 ----------------
    TV_META = IPTV_DIR / "meta.json"

    def _tv_meta(self):
        try:
            return json.loads(self.TV_META.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _tv_meta_set(self, **kw):
        d = self._tv_meta()
        d.update(kw)
        try:
            self.TV_META.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _tv_cmd(self, *args, timeout=20):
        try:
            r = subprocess.run(["/bin/bash", str(IPTV_DIR / "tvserve.sh"), *args],
                               capture_output=True, text=True, timeout=timeout)
            return (r.stdout or "").strip() or (r.stderr or "").strip()
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"

    _gold_cache = {"ts": 0, "data": None}
    _gold_lock = threading.Lock()

    _kline_cache = {"ts": 0.0, "cost": None, "png": b""}
    _kline_lock = threading.Lock()

    def api_gold_kline(self, q):
        """黄金 60 分钟 K 线（OHLC + MA5/10/20），供前端 canvas 画图。"""
        import json as _json
        import re as _re
        import urllib.request

        sym = (q.get("symbol") or ["AU0"])[0]
        if sym not in ("AU0", "AG0"):
            sym = "AU0"
        url = ("https://stock.finance.sina.com.cn/futures/api/jsonp.php/"
               f"var%20_{sym}60=/InnerFuturesNewService.getFewMinLine?symbol={sym}&type=60")
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0",
                          "Referer": "https://finance.sina.com.cn"})
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
        m = _re.search(r"\((\[.*\])\)", raw, _re.S)
        rows = []
        if m:
            for r in _json.loads(m.group(1))[-120:]:
                try:
                    rows.append({"d": r["d"], "o": float(r["o"]), "h": float(r["h"]),
                                 "l": float(r["l"]), "c": float(r["c"])})
                except Exception:
                    continue

        def ma(vals, n):
            out = []
            for i in range(len(vals)):
                if i + 1 < n:
                    out.append(None)
                else:
                    out.append(round(sum(vals[i + 1 - n:i + 1]) / n, 3))
            return out

        closes = [r["c"] for r in rows]
        try:
            cost = float((q.get("cost") or ["877.47"])[0])
        except (TypeError, ValueError):
            cost = 877.47
        return {"symbol": sym, "rows": rows, "ma5": ma(closes, 5),
                "ma10": ma(closes, 10), "ma20": ma(closes, 20), "cost": cost,
                "price": closes[-1] if closes else None}

    def _gold_kline_png(self, cost):
        """生成/缓存黄金 60 分钟 K 线 PNG（带 MA5/10/20 + 成本线）。

        matplotlib 装在 .venv_img 里，webapp 用的是系统 python，所以走子进程。
        """
        import subprocess
        import time as _time

        PY = "/home/astock/.dsh/dsh_wechat/.venv_img/bin/python"
        SCRIPT = "/home/astock/.dsh/dsh_wechat/gold_kline.py"
        OUT = "/home/astock/.dsh/dsh_wechat/gold_kline.png"

        with self._kline_lock:
            now = _time.time()
            same = self._kline_cache["cost"] == cost
            if same and self._kline_cache["png"] and now - self._kline_cache["ts"] < 120:
                png = self._kline_cache["png"]
            else:
                r = subprocess.run([PY, SCRIPT, str(cost)],
                                   capture_output=True, text=True, timeout=90)
                if r.returncode != 0:
                    raise RuntimeError(((r.stderr or r.stdout or "")[-300:]) or "生成失败")
                png = open(OUT, "rb").read()
                self._kline_cache.update(ts=now, cost=cost, png=png)
        if not png:
            raise RuntimeError("K线图为空")
        self._send(png, "image/png")

    DB_PATH = "/opt/astock/astock.db"

    def _db(self):
        import sqlite3
        con = sqlite3.connect(self.DB_PATH, timeout=15)
        con.row_factory = sqlite3.Row
        return con

    def api_db_tables(self):
        con = self._db()
        try:
            rows = con.execute(
                "select name, sql from sqlite_master where type in ('table','view') "
                "and name not like 'sqlite_%' order by name").fetchall()
            out = []
            for r in rows:
                try:
                    cnt = con.execute(f'select count(*) from "{r["name"]}"').fetchone()[0]
                except Exception:
                    cnt = -1
                out.append({"name": r["name"], "rows": cnt, "sql": (r["sql"] or "")[:200]})
            return {"ok": True, "tables": out, "db": self.DB_PATH}
        finally:
            con.close()

    def api_db_query(self, body):
        import sqlite3
        sql = str((body or {}).get("sql") or "").strip()
        if not sql:
            return {"ok": False, "error": "SQL 为空"}
        low = sql.lower()
        for bad in ("attach ", "detach ", "pragma "):
            if low.startswith(bad):
                return {"ok": False, "error": f"禁止执行 {bad.strip()}"}
        con = self._db()
        try:
            import time as _t
            t0 = _t.time()
            cur = con.execute(sql)
            elapsed = round((_t.time() - t0) * 1000)
            if cur.description:
                cols = [d[0] for d in cur.description]
                rows = [list(r) for r in cur.fetchmany(300)]
                return {"ok": True, "columns": cols, "rows": rows,
                        "returned": len(rows), "ms": elapsed}
            con.commit()
            return {"ok": True, "affected": cur.rowcount, "ms": elapsed}
        except sqlite3.Error as e:
            return {"ok": False, "error": str(e)}
        finally:
            con.close()

    def api_gold(self):

        """黄金白银实时价（新浪，30 秒缓存）"""
        import time as _time
        with self._gold_lock:
            now = _time.time()
            if self._gold_cache["data"] and now - self._gold_cache["ts"] < 30:
                return dict(self._gold_cache["data"])
        import urllib.request, re as _re
        # 直连新浪（国内可达，不走代理；代理断了页面会没数据）
        op = urllib.request.build_opener()
        req = urllib.request.Request(
            "https://hq.sinajs.cn/list=hf_XAU,hf_XAG,nf_AU0,nf_AG0",
            headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"})
        raw = op.open(req, timeout=12).read().decode("gbk", "ignore")
        out = {}
        for key, name in (("xau", "伦敦金"), ("xag", "伦敦银"),
                          ("au0", "沪金主连"), ("ag0", "沪银主连")):
            m = _re.search(key + r'="([^"]+)"', raw, _re.I)
            if key in ("xau", "xag"):
                m = _re.search(("hf_XAU" if key == "xau" else "hf_XAG") + r'="([^"]+)"', raw)
            else:
                m = _re.search(("nf_AU0" if key == "au0" else "nf_AG0") + r'="([^"]+)"', raw)
            if not m:
                continue
            f = m.group(1).split(",")
            try:
                if key in ("xau", "xag"):
                    price, prev = float(f[0]), float(f[7] or 0)
                else:
                    price, prev = float(f[6]), float(f[10] or 0)
                out[key] = {"name": name, "price": price, "prev": prev,
                            "chg": round((price / prev - 1) * 100, 2) if prev else 0}
            except Exception:
                continue
        if out:
            with self._gold_lock:
                self._gold_cache["data"] = dict(out)
                self._gold_cache["ts"] = _time.time()
        return out

    def _tv_listening(self):
        return self._tv_cmd("status", timeout=8).startswith("running")

    def _tv_ensure(self):
        """desired=on 且没在跑就拉起来（15 秒内不重复尝试）。"""
        meta = self._tv_meta()
        if meta.get("desired", "on") != "on":
            return False
        if self._tv_listening():
            return True
        if time.time() - float(meta.get("last_try") or 0) < 15:
            return False
        self._tv_meta_set(last_try=time.time())
        self._tv_cmd("start", timeout=25)
        return self._tv_listening()

    def api_tv_status(self):
        running = self._tv_ensure()
        meta = self._tv_meta()
        channels, mtime = 0, 0
        try:
            f = IPTV_DIR / "cctv.m3u"
            channels = f.read_text(encoding="utf-8", errors="ignore").count("#EXTINF")
            mtime = f.stat().st_mtime
        except Exception:  # noqa: BLE001
            pass
        return {"running": running, "port": 8099, "channels": channels,
                "updated": meta.get("updated") or mtime,
                "refreshing": bool(meta.get("refreshing")),
                "desired": meta.get("desired", "on")}

    def api_tv_list(self):
        groups, cur = [], None
        try:
            for line in (IPTV_DIR / "cctv.m3u").read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("#EXTINF"):
                    g = re.search(r'group-title="([^"]+)"', line)
                    name = line.rsplit(",", 1)[-1].strip()
                    title = g.group(1) if g else "其他"
                    if not cur or cur["name"] != title:
                        cur = {"name": title, "channels": []}
                        groups.append(cur)
                    cur["channels"].append(name)
        except Exception:  # noqa: BLE001
            pass
        return {"groups": groups}

    def api_tv_toggle(self, body):
        action = str(body.get("action") or "").strip()
        if action not in ("start", "stop"):
            return {"ok": False, "msg": "invalid action"}
        msg = self._tv_cmd(action, timeout=25)
        ok = any(k in msg for k in ("已启动", "已停止", "已在运行", "未在运行"))
        if ok:
            self._tv_meta_set(desired="on" if action == "start" else "off", last_try=time.time())
        return {"ok": ok, "msg": msg}

    def api_tv_refresh(self, body=None):
        return {"ok": True, "msg": self._tv_cmd("refresh", timeout=15)}

    # ---------------- RSS 订阅 ----------------
    RSS_FEEDS = "/opt/astock/rss_feeds.json"
    RSS_STATE = "/opt/astock/rss_state.json"
    RSS_LOG = "/opt/astock/rss_push.log"
    RSS_PY = "/opt/astock/venv/bin/python"
    RSS_IMPORT = "/opt/astock/add_rss_from_opml.py"
    RSS_DEFAULT_OPML = "https://jackyst0.github.io/awesome-rsshub-routes/feeds.opml"
    # 共享的导入任务状态（类属性，所有实例共用同一个 dict）
    _rss_import = {"running": False, "result": "", "started": ""}

    def _rss_load(self):
        try:
            with open(self.RSS_FEEDS, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def _rss_save(self, data):
        tmp = self.RSS_FEEDS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.RSS_FEEDS)

    def api_rss_feeds(self):
        feeds = self._rss_load()
        log = []
        try:
            with open(self.RSS_LOG, encoding="utf-8", errors="ignore") as fh:
                log = fh.read().splitlines()[-40:]
        except OSError:
            pass
        last_run = ""
        try:
            last_run = time.strftime("%m-%d %H:%M",
                                     time.localtime(os.path.getmtime(self.RSS_STATE)))
        except OSError:
            pass
        last_result = log[-1].strip() if log else ""
        return {"feeds": feeds, "log": log,
                "last_run": last_run, "last_result": last_result}

    def api_rss_feeds_set(self, body):
        action = str(body.get("action") or "")
        cat = str(body.get("cat") or "").strip()
        url = str(body.get("url") or "").strip()
        name = str(body.get("name") or "").strip()
        data = self._rss_load()

        if action == "add":
            if not (cat and name and url):
                return {"ok": False, "msg": "分类/名称/地址都必填"}
            if not url.lower().startswith(("http://", "https://")):
                return {"ok": False, "msg": "地址必须以 http(s) 开头"}
            data.setdefault(cat, [])
            if any(i.get("url") == url for i in data[cat]):
                return {"ok": False, "msg": "该地址已存在"}
            data[cat].append({"name": name, "url": url, "on": True})
        elif action in ("del", "toggle"):
            found = False
            for c in list(data):
                for i, it in enumerate(list(data[c])):
                    if it.get("url") == url:
                        if action == "del":
                            data[c].pop(i)
                        else:
                            it["on"] = not it.get("on", True)
                        found = True
                        break
                if not data[c]:
                    data.pop(c, None)
                if found:
                    break
            if not found:
                return {"ok": False, "msg": "没找到该源"}
        else:
            return {"ok": False, "msg": "未知操作"}

        self._rss_save(data)
        return {"ok": True}

    def api_rss_run(self):
        py = self.RSS_PY if os.path.exists(self.RSS_PY) else sys.executable

        def worker():
            try:
                with open(self.RSS_LOG, "a", encoding="utf-8") as fh:
                    fh.write(f"\n--- 手动触发 {time.strftime('%m-%d %H:%M:%S')} ---\n")
                    fh.flush()   # 先落盘，否则子进程输出会跑到这行前面
                    subprocess.run([py, "/opt/astock/rss_push.py"],
                                   stdout=fh, stderr=fh, timeout=300)
            except Exception as exc:  # noqa: BLE001
                try:
                    with open(self.RSS_LOG, "a", encoding="utf-8") as fh:
                        fh.write(f"[手动触发失败] {exc}\n")
                except OSError:
                    pass

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    def api_rss_import_status(self):
        return dict(self._rss_import)

    def api_rss_import(self, body):
        """从 OPML 链接导入源：后台跑 add_rss_from_opml.py，前端轮询状态。"""
        url = str(body.get("url") or "").strip() or self.RSS_DEFAULT_OPML
        if self._rss_import["running"]:
            return {"ok": False, "msg": "已有导入任务在跑，请等它结束"}
        if not url.lower().startswith(("http://", "https://")):
            return {"ok": False, "msg": "需要 http(s) 链接"}
        py = self.RSS_PY if os.path.exists(self.RSS_PY) else sys.executable
        if not os.path.exists(self.RSS_IMPORT):
            return {"ok": False, "msg": "导入脚本不存在"}
        self._rss_import.update(running=True, result="", started=time.strftime("%H:%M:%S"))

        def worker():
            try:
                r = subprocess.run([py, self.RSS_IMPORT, url],
                                   capture_output=True, text=True, timeout=900)
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                if r.returncode != 0:
                    out = "❌ 失败：\n" + out[-1200:]
                else:
                    out = "✅ 完成：\n" + out[-1200:]
                self._rss_import["result"] = out
            except Exception as exc:  # noqa: BLE001
                self._rss_import["result"] = f"❌ 失败：{type(exc).__name__} {exc}"
            finally:
                self._rss_import["running"] = False

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    # ---------------- DSH Web ----------------
    def api_dsh_status(self):
        try:
            r = subprocess.run(["systemctl", "is-active", "dsh-web"],
                               capture_output=True, text=True, timeout=10)
            st = (r.stdout or "").strip()
            return {"running": st == "active", "status": st or "unknown"}
        except Exception as exc:  # noqa: BLE001
            return {"running": False, "status": f"error: {exc}"}

    def api_dsh_toggle(self, body):
        action = str(body.get("action") or "").strip()
        if action not in ("start", "stop", "restart"):
            return {"ok": False, "msg": "invalid action"}
        try:
            r = subprocess.run(["sudo", "-n", "systemctl", action, "dsh-web"],
                               capture_output=True, text=True, timeout=90)
            ok = r.returncode == 0
            return {"ok": ok, "msg": "" if ok else (r.stderr or r.stdout or "")[-300:]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": str(exc)}

    # ---------------- qBittorrent ----------------
    def api_qbit_status(self):
        try:
            r = subprocess.run(["systemctl", "is-active", "qbittorrent"],
                               capture_output=True, text=True, timeout=10)
            st = (r.stdout or "").strip()
            return {"running": st == "active", "status": st or "unknown"}
        except Exception as exc:  # noqa: BLE001
            return {"running": False, "status": f"error: {exc}"}

    def api_qbit_toggle(self, body):
        action = str(body.get("action") or "").strip()
        if action not in ("start", "stop", "restart"):
            return {"ok": False, "msg": "invalid action"}
        try:
            r = subprocess.run(["sudo", "-n", "systemctl", action, "qbittorrent"],
                               capture_output=True, text=True, timeout=90)
            ok = r.returncode == 0
            return {"ok": ok, "msg": "" if ok else (r.stderr or r.stdout or "")[-300:]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": str(exc)}

    def api_proxy_toggle(self, body):
        action = str(body.get("action") or "").strip()
        if action not in ("start", "stop", "restart"):
            return {"ok": False, "msg": "invalid action"}
        if action == "restart" and not body.get("force"):
            try:
                with open("/opt/astock/tg_download.json", encoding="utf-8") as fh:
                    if fh.readline().strip() == "1":
                        return {"ok": False, "msg": "TG bot 正在下载，重启代理会中断且不会续传；确认强制重启请再点一次"}
            except OSError:
                pass
        try:
            r = subprocess.run(
                ["sudo", "-n", "systemctl", action, "mihomo"],
                capture_output=True, text=True, timeout=30,
            )
            ok = r.returncode == 0
            return {"ok": ok, "msg": "" if ok else (r.stderr or "")[-200:]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": str(exc)}

    def api_proxy_traffic(self):
        now = time.time()
        if _sub_cache["data"] and now - _sub_cache["ts"] < 300:
            return _sub_cache["data"]
        try:
            headers = fetch_sub_headers()
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        raw = {}
        for part in (headers.get("Subscription-Userinfo") or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                try:
                    raw[k.strip()] = int(v.strip())
                except ValueError:
                    pass
        total = raw.get("total", 0)
        upload = raw.get("upload", 0)
        download = raw.get("download", 0)
        used = upload + download
        data = {
            "upload": upload,
            "download": download,
            "used": used,
            "total": total,
            "remain": max(0, total - used),
            "pct": round(used / total * 100, 2) if total else 0,
            "expire": raw.get("expire", 0),
        }
        _sub_cache.update(ts=now, data=data)
        return data

    def api_proxy_nodes(self):
        try:
            d = mihomo_get("/proxies")
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        proxies = d.get("proxies", {})
        group = proxies.get(MIHOMO_GROUP) or {}
        return {
            "group": MIHOMO_GROUP,
            "now": group.get("now"),
            "nodes": group.get("all", []),
        }

    def api_proxy_select(self, body):
        name = str(body.get("name") or "").strip()
        if not name:
            return {"ok": False, "msg": "节点为空"}
        try:
            mihomo_put("/proxies/" + urllib.parse.quote(MIHOMO_GROUP), {"name": name})
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": str(exc)}

    def api_proxy_speedtest(self):
        """测所有节点速度，切到最快的。"""
        try:
            d = mihomo_get("/proxies")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": f"{type(exc).__name__}: {exc}"}
        group = d.get("proxies", {}).get(MIHOMO_GROUP, {})
        candidates = [n for n in group.get("all", [])
                      if any(k in n for k in ("香港", "日本", "新加坡", "台湾"))]
        if not candidates:
            return {"ok": False, "msg": "没有可测速的节点"}
        candidates = candidates[:12]

        results = []
        for node in candidates:
            try:
                mihomo_put("/proxies/" + urllib.parse.quote(MIHOMO_GROUP), {"name": node})
                time.sleep(1.5)
                r = subprocess.run(
                    ["curl", "-s", "-x", "http://127.0.0.1:7890", "-o", "/dev/null",
                     "-m", "15", "-w", "%{speed_download}",
                     "https://speed.cloudflare.com/__down?bytes=3000000"],
                    capture_output=True, text=True, timeout=20,
                )
                speed = float(r.stdout.strip() or 0) / 1024 / 1024
            except Exception:  # noqa: BLE001
                speed = 0.0
            results.append({"node": node, "speed": round(speed, 2)})

        results.sort(key=lambda x: -x["speed"])
        best = results[0]
        try:
            mihomo_put("/proxies/" + urllib.parse.quote(MIHOMO_GROUP), {"name": best["node"]})
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "best": best["node"], "speed": best["speed"], "results": results}


def _compute_signals():
    """后台计算全部标的的信号（串行，避免被行情接口限流）。"""
    try:
        cfg = _cfg()
        out = {}
        for w in cfg.get("watchlist", []):
            code = w.get("code")
            if not code:
                continue
            item = {}
            for period, unit in (("day", "日"), ("m60", "根")):
                try:
                    bars = mon.fetch_kline(code, 130, period)
                    item[period] = compute_signal(bars, unit)
                except Exception:  # noqa: BLE001
                    item[period] = None
            out[code] = item
        with _sig_lock:
            _sig_cache.update(ts=time.time(), data=out)
    finally:
        with _sig_lock:
            _sig_cache["running"] = False


_cpu_prev = {"idle": 0, "total": 0}


def _cpu_times():
    try:
        with open("/proc/stat") as fh:
            vals = [int(x) for x in fh.readline().split()[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return idle, sum(vals)
    except (OSError, ValueError, IndexError):
        return 0, 0


def _cpu_usage():
    global _cpu_prev
    idle, total = _cpu_times()
    di = idle - _cpu_prev["idle"]
    dt = total - _cpu_prev["total"]
    _cpu_prev = {"idle": idle, "total": total}
    if dt <= 0:
        return None
    return round((1 - di / dt) * 100, 1)


def _service_state(name):
    try:
        out = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=4,
        )
        return (out.stdout or "").strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _services_info():
    """动态收集运行中的服务 + 本机关心的服务（含已停止的）。"""
    result = {}
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=running",
             "--no-legend", "--no-pager", "--plain"],
            capture_output=True, text=True, timeout=8,
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if not parts:
                continue
            name = parts[0]
            if name.endswith(".service"):
                name = name[: -len(".service")]
            if name and name not in result:
                result[name] = "active"
    except Exception:  # noqa: BLE001
        pass
    wanted = [
        "astock-monitor", "astock-web", "reports-web", "aria2", "qbittorrent",
        "minidlna", "smbd", "nmbd", "alist", "ssh",
    ]
    for name in wanted:
        if name not in result:
            result[name] = _service_state(name)
    return result


def _fmt_uptime(seconds):
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分"


CLOUDFLARED_CONFIG = Path("/etc/cloudflared/config.yml")
TUNNEL_NAME = "astock"
QUANT_ACCESS_LOG = Path("/home/astock/.dsh/dsh_wechat/quant/access.log")
QUANT_BANS = Path("/home/astock/.dsh/dsh_wechat/quant/bans.json")


def api_quant_bans() -> dict:
    """公网看板的封禁 IP 列表。"""
    try:
        data = json.loads(QUANT_BANS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    now = time.time()
    bans = []
    for ip, rec in data.items():
        until = float(rec.get("until") or 0)
        bans.append({
            "ip": ip,
            "count": rec.get("count"),
            "at": rec.get("at"),
            "reason": rec.get("reason"),
            "permanent": until == 0,
            "remaining": 0 if until == 0 else max(0, int(until - now)),
        })
    bans.sort(key=lambda x: x.get("remaining") or 0, reverse=True)
    return {"bans": bans}


def api_quant_unban(body: dict) -> dict:
    """解除某个 IP 的封禁。"""
    ip = str((body or {}).get("ip") or "").strip()
    if not ip:
        return {"error": "缺少 IP"}
    try:
        data = json.loads(QUANT_BANS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    if ip not in data:
        return {"ok": True, "ip": ip, "note": "该 IP 不在封禁列表"}
    data.pop(ip, None)
    try:
        QUANT_BANS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        return {"error": f"写文件失败：{exc}"}
    return {"ok": True, "ip": ip}



def api_quant_logs() -> dict:
    """读公网看板的访问日志（最后 300 条，新的在前）。"""
    rows = []
    try:
        lines = QUANT_ACCESS_LOG.read_text(encoding="utf-8").splitlines()[-300:]
    except OSError:
        lines = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows.reverse()
    return {"logs": rows}



def _tunnel_domain() -> str:
    """读 /etc/cloudflared/config.yml 里第一条 hostname。"""
    try:
        for line in CLOUDFLARED_CONFIG.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("- hostname:"):
                return s.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def _tunnel_state() -> str:
    try:
        out = subprocess.run(["systemctl", "is-active", "cloudflared"],
                             capture_output=True, text=True, timeout=5)
        return (out.stdout or "").strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def api_domain_info() -> dict:
    domain = _tunnel_domain()
    return {
        "domain": domain,
        "url": f"https://{domain}/live" if domain else "",
        "tunnel": _tunnel_state(),
    }


def api_domain_set(body: dict) -> dict:
    """更换隧道域名：改 config.yml → 重建 DNS 路由 → 重启 cloudflared。"""
    new = str((body or {}).get("domain") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+", new):
        return {"error": "域名格式不对，例如 live.example.com"}
    try:
        text = CLOUDFLARED_CONFIG.read_text(encoding="utf-8")
    except OSError as exc:
        return {"error": f"读配置失败：{exc}"}
    new_text = re.sub(r"(?m)^(\s*-\s*hostname:\s*)\S+", lambda m: m.group(1) + new, text, count=1)
    if new_text == text:
        return {"error": "配置里没找到 hostname 行"}
    try:
        CLOUDFLARED_CONFIG.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        return {"error": f"写配置失败：{exc}"}
    steps = [
        ["sudo", "-n", "cloudflared", "tunnel", "route", "dns", TUNNEL_NAME, new],
        ["sudo", "-n", "systemctl", "restart", "cloudflared"],
    ]
    for cmd in steps:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"执行失败：{exc}"}
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            return {"error": f"{' '.join(cmd)} 失败：{detail}"}
    return {"ok": True, "domain": new, "url": f"https://{new}/live"}


def api_sysinfo():
    info = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "hostname": socket.gethostname(),
        "kernel": platform.release(),
    }
    info["cpu_pct"] = _cpu_usage()
    try:
        cores = os.cpu_count()
        model = ""
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
        info["cpu"] = {"cores": cores, "model": model}
    except OSError:
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, val = line.split(":", 1)
                mem[key.strip()] = int(val.split()[0])
        total = mem.get("MemTotal", 0) or 0
        avail = mem.get("MemAvailable", mem.get("MemFree", 0)) or 0
        used = max(0, total - avail)
        info["mem"] = {
            "total_mb": round(total / 1024),
            "used_mb": round(used / 1024),
            "pct": round(used / total * 100, 1) if total else 0,
        }
    except OSError:
        pass
    try:
        du = shutil.disk_usage("/opt/astock")
        info["disk"] = {
            "total_gb": round(du.total / 1024 ** 3, 1),
            "used_gb": round(du.used / 1024 ** 3, 1),
            "free_gb": round(du.free / 1024 ** 3, 1),
            "pct": round(du.used / du.total * 100, 1) if du.total else 0,
        }
    except OSError:
        pass
    try:
        with open("/proc/loadavg") as fh:
            info["load"] = fh.read().split()[:3]
        with open("/proc/uptime") as fh:
            up = float(fh.read().split()[0])
        info["uptime"] = {"seconds": int(up), "text": _fmt_uptime(up)}
    except OSError:
        pass
    temps = []
    for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            with open(path) as fh:
                temps.append(int(fh.read().strip()) / 1000)
        except (OSError, ValueError):
            continue
    # 笔记本没有 thermal_zone 时，从 hwmon 兜底读取（k10temp / dell_smm / 核显）
    if not temps:
        wanted = ("k10temp", "coretemp", "zenpower", "cpu_thermal",
                  "acpitz", "dell_smm", "radeon", "amdgpu")
        for hw in glob.glob("/sys/class/hwmon/hwmon*"):
            try:
                with open(hw + "/name") as fh:
                    name = fh.read().strip().lower()
            except OSError:
                continue
            if name not in wanted:
                continue
            for path in glob.glob(hw + "/temp*_input"):
                try:
                    with open(path) as fh:
                        val = int(fh.read().strip()) / 1000
                    if 10 <= val <= 120:   # 过滤掉无效/未连接传感器
                        temps.append(val)
                except (OSError, ValueError):
                    continue
    if temps:
        info["temp_c"] = round(max(temps), 1)
    info["services"] = _services_info()
    return info


RSS_DIGEST = BASE / "rss_digest.json"


def _rss_digest_data():
    try:
        d = json.loads(RSS_DIGEST.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    return d or {"date": "", "items": []}


def _rss_digest_html():
    d = _rss_digest_data()
    items = d.get("items") or []
    date = d.get("date") or "—"
    upd = d.get("updated") or "—"
    rows = []
    for it in items:
        title = (it.get("title") or "").replace("<", "&lt;").replace(">", "&gt;")
        link = it.get("link") or ""
        desc = (it.get("desc") or "").replace("<", "&lt;").replace(">", "&gt;")
        cat = it.get("cat") or ""
        src = it.get("src") or ""
        head = f'<a href="{link}" target="_blank" rel="noopener">{title}</a>' if link else title
        rows.append(
            f'<div class="item"><div class="meta">{it.get("t","")} · {cat} · {src}</div>'
            f'<div class="title">{head}</div>'
            + (f'<div class="desc">{desc}</div>' if desc else "")
            + "</div>"
        )
    body = "".join(rows) or '<div class="empty">今天还没有新条目</div>'
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>RSS 每日简报</title>
<style>
body{{margin:0;background:#0d1117;color:#e6edf3;font:15px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:16px 14px 40px}}
a{{color:#58a6ff;text-decoration:none}}
h1{{font-size:19px;margin:8px 0 4px}}
.sub{{color:#8b949e;font-size:13px;margin-bottom:14px}}
.item{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 14px;margin-bottom:10px}}
.meta{{color:#8b949e;font-size:12px;margin-bottom:4px}}
.title{{font-size:15px;font-weight:600;line-height:1.5}}
.desc{{color:#9aa4af;font-size:13px;margin-top:5px}}
.empty{{color:#8b949e;text-align:center;padding:40px 0}}
.back{{font-size:13px;color:#8b949e}}
</style></head><body>
<a class="back" href="/rss">← 返回 RSS 管理</a>
<h1>📰 RSS 每日简报</h1>
<div class="sub">{date} · 共 {len(items)} 条 · 更新于 {upd}</div>
{body}
</body></html>"""


def _save_cfg(cfg):
    tmp = CFG_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    tmp.replace(CFG_PATH)


def _tv_daily_loop():
    """看门狗：每 2 分钟确保直播源服务在期望状态；每天 08:00 后自动测源一次。"""
    while True:
        try:
            meta_p = IPTV_DIR / "meta.json"
            meta = {}
            if meta_p.exists():
                try:
                    meta = json.loads(meta_p.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    meta = {}
            if meta.get("desired", "on") == "on":
                r = subprocess.run(["/bin/bash", str(IPTV_DIR / "tvserve.sh"), "status"],
                                   capture_output=True, text=True, timeout=10)
                if not (r.stdout or "").strip().startswith("running"):
                    subprocess.run(["/bin/bash", str(IPTV_DIR / "tvserve.sh"), "start"],
                                   capture_output=True, timeout=30)
            last = meta.get("updated") or 0
            now = datetime.now()
            fresh = datetime.fromtimestamp(last).date() == now.date()
            if now.hour >= 8 and not fresh and not meta.get("refreshing"):
                subprocess.run(["/bin/bash", str(IPTV_DIR / "refresh_run.sh")],
                               capture_output=True, timeout=2400)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(120)


def main(argv=None):
    port = PORT
    if argv and len(argv) > 1:
        port = int(argv[1])
    threading.Thread(target=_tv_daily_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"web panel listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main(sys.argv)
