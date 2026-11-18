#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.expanduser("~/.dsh"))
try:
    from secrets_lib import get as _secret
except Exception:
    def _secret(name, default=""):
        return default

"""企业微信群机器人发送脚本

用法:
    python3 send_wecom.py "消息内容"
    echo "消息" | python3 send_wecom.py
"""
import json
import sys
import urllib.request

WEBHOOK = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
           "?key=" + _secret("wecom_webhook_key"))


def send_text(content: str) -> dict:
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
    if len(sys.argv) > 1:
        content = " ".join(sys.argv[1:])
    else:
        content = sys.stdin.read().strip()
    if not content:
        print("用法: python3 send_wecom.py \"消息内容\"")
        sys.exit(1)
    result = send_text(content)
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result.get("errcode") == 0 else 1)


if __name__ == "__main__":
    main()
