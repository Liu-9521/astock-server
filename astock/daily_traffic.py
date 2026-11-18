#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.expanduser("~/.dsh"))
try:
    from secrets_lib import get as _secret
except Exception:
    def _secret(name, default=""):
        return default

"""每天早上发送代理流量日报到企业微信"""
import json
import ssl
import time
import urllib.request

SUB_URL = "https://45.137.180.144/L?ZG93bmxvYWRDb25maWcuanMvMzkzZGRhN2FmYjhhN2FmZTNjN2YxMTIxOTA5ODQzZmQ/dGltZXN0YW1wPTE0MjQ4NA=="
WEBHOOK = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
           "?key=" + _secret("wecom_webhook_key"))
GB = 1024 ** 3


def fetch_userinfo():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(SUB_URL, headers={"User-Agent": "clash-verge/1.7.7"})
    with urllib.request.urlopen(req, timeout=40, context=ctx) as resp:
        raw = resp.headers.get("Subscription-Userinfo") or ""
    data = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                data[k.strip()] = int(v.strip())
            except ValueError:
                pass
    return data


def send_wecom(content):
    payload = {"msgtype": "text", "text": {"content": content}}
    req = urllib.request.Request(
        WEBHOOK,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    try:
        info = fetch_userinfo()
    except Exception as exc:  # noqa: BLE001
        print("fetch failed:", exc)
        return

    total = info.get("total", 0)
    used = info.get("upload", 0) + info.get("download", 0)
    pct = used / total * 100 if total else 0
    expire = info.get("expire", 0)
    expire_txt = time.strftime("%Y-%m-%d", time.localtime(expire)) if expire else "—"

    content = (
        "☀️ 早安，今日代理流量日报\n"
        f"已用：{used / GB:.2f} / {total / GB:.2f} GB（{pct:.1f}%）\n"
        f"剩余：{(total - used) / GB:.2f} GB\n"
        f"到期：{expire_txt}"
    )
    try:
        result = send_wecom(content)
        print("sent:", result)
    except Exception as exc:  # noqa: BLE001
        print("send failed:", exc)


if __name__ == "__main__":
    main()
