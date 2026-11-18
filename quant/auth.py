#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公网登录保护：密码 + 微信验证码 + Cookie 会话 + 访问日志。

只被 quant_sim.py 调用；内网/Tailscale 不经过这里（直接放行）。
"""

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
AUTH_FILE = BASE_DIR / "auth.json"
ACCESS_LOG = BASE_DIR / "access.log"

SESSION_TTL = 24 * 3600  # 1 天，过期需重新密码+验证码
CODE_TTL = 300
CODE_MAX_TRIES = 5

WECHAT_BIN = "/home/astock/.dsh/profiles/headless/node_modules/.bin/dsh-wechat-send"

CODES = {}     # ip -> {"code", "exp", "tries"}
SESSIONS = {}  # token -> exp
SESSIONS_FILE = BASE_DIR / "sessions.json"


def _load_sessions() -> None:
    global SESSIONS
    try:
        with open(SESSIONS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return
    now = time.time()
    SESSIONS = {k: float(v) for k, v in data.items()
                if isinstance(v, (int, float)) and float(v) > now}


def _save_sessions() -> None:
    try:
        with open(SESSIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump(SESSIONS, fh)
    except OSError:
        pass


_load_sessions()
FAILS = {}     # ip -> {"count", "first"}  最近 15 分钟内的失败次数
BANS_FILE = BASE_DIR / "bans.json"
BAN_PERMANENT = True   # 永久封禁（只能手动解封）
FAIL_WINDOW = 900
MAX_FAILS = 5


def _load_bans() -> dict:
    try:
        return json.loads(BANS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_bans(data: dict) -> None:
    try:
        BANS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def is_banned(ip: str) -> bool:
    bans = _load_bans()
    rec = bans.get(ip)
    if not rec:
        return False
    until = float(rec.get("until") or 0)
    if until == 0:  # 0 = 永久
        return True
    if until < time.time():
        bans.pop(ip, None)
        _save_bans(bans)
        return False
    return True


def record_fail(ip: str, kind: str, ua: str) -> bool:
    """记一次失败；达到 5 次返回 True 并永久封禁。"""
    rec = FAILS.setdefault(ip, {"count": 0, "first": time.time()})
    if time.time() - rec["first"] > FAIL_WINDOW:
        rec["count"] = 0
        rec["first"] = time.time()
    rec["count"] += 1
    n = rec["count"]
    log_access(ip, "/login", 401, ua, f"{kind}错误（第 {n} 次）")
    if n < MAX_FAILS:
        return False
    bans = _load_bans()
    bans[ip] = {
        "count": n,
        "until": 0,
        "permanent": True,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": f"密码/验证码错误 {n} 次",
    }
    _save_bans(bans)
    FAILS.pop(ip, None)
    log_access(ip, "/login", 403, ua, f"失败 {n} 次，已永久封禁")
    return True



def _load_auth() -> dict:
    if AUTH_FILE.exists():
        try:
            return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    conf = {
        "password": "<QUANT_WEB_PASSWORD>",
        "secret": secrets.token_hex(16),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        AUTH_FILE.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return conf


AUTH = _load_auth()


def password() -> str:
    """每次登录都读文件，早报改完密码立即生效，不用重启服务。"""
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        pw = str(data.get("password") or "").strip()
        if pw:
            return pw
    except Exception:  # noqa: BLE001
        pass
    return str(AUTH.get("password") or "<QUANT_WEB_PASSWORD>")


def log_access(ip: str, path: str, status, ua: str = "", note: str = "") -> None:
    line = json.dumps({
        "t": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ip": ip,
        "path": path,
        "status": status,
        "ua": (ua or "")[:140],
        "note": note,
    }, ensure_ascii=False)
    try:
        with ACCESS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def send_wechat(text: str) -> bool:
    if not os.path.exists(WECHAT_BIN):
        return False
    try:
        proc = subprocess.run([WECHAT_BIN, text], capture_output=True, text=True, timeout=45)
        return proc.returncode == 0
    except Exception:
        return False


def _cookie_token(h) -> str:
    raw = h.headers.get("Cookie") or ""
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "qt":
            return value
    return ""


def is_authed(h) -> bool:
    token = _cookie_token(h)
    if not token:
        return False
    exp = SESSIONS.get(token)
    if not exp or exp < time.time():
        SESSIONS.pop(token, None)
        _save_sessions()
        return False
    return True


def login(h, ip: str, body: dict) -> dict:
    ua = h.headers.get("User-Agent", "")
    if is_banned(ip):
        return {"error": "该 IP 已被永久封禁，请在总站「访问日志」里解封", "banned": True}
    pw = str((body or {}).get("password") or "")
    if not hmac.compare_digest(pw, password()):
        if record_fail(ip, "密码", ua):
            return {"error": "失败 5 次，该 IP 已被永久封禁", "banned": True}
        return {"error": "密码错误"}
    code = f"{secrets.randbelow(1000000):06d}"
    CODES[ip] = {"code": code, "exp": time.time() + CODE_TTL, "tries": 0}
    ok = send_wechat(f"【astock】登录验证码：{code}，5 分钟内有效。")
    log_access(ip, "/login", 200, ua, "验证码已发微信" if ok else "微信发送失败")
    if not ok:
        return {"error": "微信发送失败，请检查微信通道"}
    return {"ok": True, "hint": "验证码已发到微信，5 分钟内输入"}


def verify(h, ip: str, body: dict) -> dict:
    ua = h.headers.get("User-Agent", "")
    if is_banned(ip):
        return {"error": "该 IP 已被永久封禁，请在总站「访问日志」里解封", "banned": True}
    rec = CODES.get(ip)
    if not rec or rec["exp"] < time.time():
        CODES.pop(ip, None)
        return {"error": "验证码已过期，请重新获取"}
    code = str((body or {}).get("code") or "").strip()
    if not hmac.compare_digest(code, rec["code"]):
        if record_fail(ip, "验证码", ua):
            return {"error": "失败 5 次，该 IP 已被永久封禁", "banned": True}
        return {"error": "验证码错误"}
    CODES.pop(ip, None)
    token = secrets.token_urlsafe(24)
    SESSIONS[token] = time.time() + SESSION_TTL
    _save_sessions()
    log_access(ip, "/login", 200, ua, "登录成功")
    return {"ok": True, "cookie": f"qt={token}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; SameSite=Lax"}


def pure_view(html: str) -> str:
    """把实时看板改成纯展示版：只去掉输入框/下拉和写操作按钮。

    带 data-view="1" 的按钮是“查看类”（K 线日线/60分钟切换），保留。
    """
    css = ("<style>"
           "body.ro input,body.ro select,body.ro button:not([data-view]),body.ro .row"
           "{display:none!important;visibility:hidden!important}"
           "</style></head>")
    html = html.replace("<body>", '<body class="ro">', 1)
    html = html.replace("</head>", css, 1)
    js = ("<script>"
          "window.addEventListener('load',function(){"
          "document.querySelectorAll('input,select,button:not([data-view])').forEach(function(e){e.remove()});"
          "document.querySelectorAll('.row').forEach(function(e){e.remove()});"
          "});"
          "</script></body>")
    html = html.replace("</body>", js, 1)
    return html


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>astock · 登录</title>
<style>
  :root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#c9d1d9;--muted:#8b949e;--accent:#388bfd;--green:#0ecb81;--red:#f6465d}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  .box{width:min(92vw,360px);background:var(--panel);border:1px solid var(--border);
       border-radius:14px;padding:26px 24px}
  h1{font-size:17px;margin:0 0 6px}
  .sub{font-size:12.5px;color:var(--muted);margin-bottom:20px;line-height:1.6}
  input{width:100%;padding:11px 12px;border-radius:9px;border:1px solid var(--border);
        background:#0d1117;color:var(--text);font-size:15px;outline:none;letter-spacing:1px}
  input:focus{border-color:var(--accent)}
  button{width:100%;margin-top:14px;padding:11px;border-radius:9px;border:0;
         background:var(--accent);color:#fff;font-size:14px;cursor:pointer}
  button:disabled{opacity:.55;cursor:default}
  .msg{font-size:12.5px;margin-top:12px;min-height:18px}
  .msg.err{color:var(--red)} .msg.ok{color:var(--green)}
  .hide{display:none}
  .tip{font-size:12px;color:var(--muted);margin-top:16px;line-height:1.6}
</style>
</head>
<body>
<div class="box">
  <h1>astock 公网看板</h1>
  <div class="sub">此页面仅允许本人访问。请输入密码，验证码会发到微信。</div>

  <div id="stepPwd">
    <input id="pwd" type="password" placeholder="访问密码" autocomplete="current-password">
    <button id="btnPwd">下一步</button>
  </div>

  <div id="stepCode" class="hide">
    <input id="code" type="text" inputmode="numeric" maxlength="6" placeholder="6 位验证码">
    <button id="btnCode">登录</button>
    <button id="btnBack" style="background:#21262d;border:1px solid var(--border);color:var(--text)">返回重输密码</button>
  </div>

  <div class="msg" id="msg"></div>
  <div class="tip">验证码 5 分钟内有效；登录状态保持 24 小时。</div>
</div>
<script>
const $ = s => document.querySelector(s);
const msg = (t, cls) => { const m = $("#msg"); m.textContent = t || ""; m.className = "msg " + (cls || ""); };

$("#btnPwd").onclick = async () => {
  const pw = $("#pwd").value;
  if (!pw) return;
  $("#btnPwd").disabled = true;
  try {
    const d = await (await fetch("/api/auth/login", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({password: pw})
    })).json();
    if (d.error) { msg(d.error, "err"); }
    else {
      $("#stepPwd").classList.add("hide");
      $("#stepCode").classList.remove("hide");
      msg(d.hint || "验证码已发送", "ok");
      $("#code").focus();
    }
  } catch (e) { msg("网络错误：" + e, "err"); }
  $("#btnPwd").disabled = false;
};

$("#btnCode").onclick = async () => {
  const code = $("#code").value.trim();
  if (code.length !== 6) { msg("请输入 6 位验证码", "err"); return; }
  $("#btnCode").disabled = true;
  try {
    const r = await fetch("/api/auth/verify", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({code})
    });
    const d = await r.json();
    if (d.error) { msg(d.error, "err"); }
    else { msg("登录成功，跳转中…", "ok"); location.href = "/menu"; }
  } catch (e) { msg("网络错误：" + e, "err"); }
  $("#btnCode").disabled = false;
};

$("#btnBack").onclick = () => {
  $("#stepCode").classList.add("hide");
  $("#stepPwd").classList.remove("hide");
  msg("", "");
};

$("#pwd").addEventListener("keydown", e => { if (e.key === "Enter") $("#btnPwd").click(); });
$("#code").addEventListener("keydown", e => { if (e.key === "Enter") $("#btnCode").click(); });
</script>
</body>
</html>
"""
