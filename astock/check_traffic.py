#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.expanduser("~/.dsh"))
try:
    from secrets_lib import get as _secret
except Exception:
    def _secret(name, default=""):
        return default

"""夜煞云流量预警：超过阈值推送到企业微信（每小时检查一次）"""
import json
import os
import ssl
import time
import urllib.request

SUB_URL = "https://45.137.180.144/L?ZG93bmxvYWRDb25maWcuanMvMzkzZGRhN2FmYjhhN2FmZTNjN2YxMTIxOTA5ODQzZmQ/dGltZXN0YW1wPTE0MjQ4NA=="
WEBHOOK = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
           "?key=" + _secret("wecom_webhook_key"))
STATE = "/opt/astock/.traffic_warn.json"
THRESHOLD = 80      # 百分比
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
    if not total:
        print("no traffic info")
        return

    pct = used / total * 100
    expire = info.get("expire", 0)
    expire_txt = time.strftime("%Y-%m-%d", time.localtime(expire)) if expire else "—"
    print(f"used {used / GB:.2f} / {total / GB:.2f} GB ({pct:.1f}%)")

    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
        except Exception:  # noqa: BLE001
            state = {}

    # 每跨过一个 10% 档位提醒一次；重新充流量后重置
    level = int(pct // 10) * 10
    last_level = int(state.get("last_level", 0))
    if level >= THRESHOLD and level > last_level:
        content = (
            f"⚠️ 夜煞云流量预警（{pct:.1f}%）\n"
            f"已用：{used / GB:.2f} GB / {total / GB:.2f} GB\n"
            f"剩余：{(total - used) / GB:.2f} GB\n"
            f"到期：{expire_txt}\n"
            "流量快用完了，注意控制下载。"
        )
        try:
            result = send_wecom(content)
            print("wecom:", result)
            state["last_level"] = level
            state["warned_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as exc:  # noqa: BLE001
            print("send failed:", exc)
    elif level < last_level:
        state["last_level"] = level  # 流量重置（充值/新周期）

    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)


if __name__ == "__main__":
    main()
