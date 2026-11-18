#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代理守护：检查 mihomo 代理是否通，不通就切到「自动选择」组。

每 5 分钟由 cron 跑一次。mihomo 没运行或 API 不通时静默退出。
"""
import subprocess
import time
import urllib.parse

import requests

MIHOMO_API = "http://127.0.0.1:9090"
PROXY = "socks5h://127.0.0.1:7890"
TEST_URL = "https://api.telegram.org"
TARGET_GROUP = "🚀 节点选择"
FALLBACK = "自动选择"
LOG = "/opt/astock/proxy_guard.log"


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def proxy_ok():
    """用 socks5 访问 telegram，返回 True/False。"""
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-m", "8", "-w", "%{http_code}",
             "-x", PROXY, TEST_URL],
            capture_output=True, text=True, timeout=15)
        code = (r.stdout or "").strip()
        return code not in ("000", "", "502", "503")
    except Exception:  # noqa: BLE001
        return False


def get_now(group):
    try:
        u = f"{MIHOMO_API}/proxies/{urllib.parse.quote(group)}"
        r = requests.get(u, timeout=6)
        return r.json().get("now")
    except Exception:  # noqa: BLE001
        return None


def switch(group, node):
    try:
        u = f"{MIHOMO_API}/proxies/{urllib.parse.quote(group)}"
        r = requests.put(u, json={"name": node}, timeout=8)
        return r.status_code in (200, 204)
    except Exception:  # noqa: BLE001
        return False


def main():
    if not proxy_ok():
        now = get_now(TARGET_GROUP)
        log(f"代理不通（当前节点={now}），切到「{FALLBACK}」")
        if switch(TARGET_GROUP, FALLBACK):
            time.sleep(2)
            if proxy_ok():
                log("切换成功，代理已恢复")
            else:
                log("已切到自动选择，但代理仍不通（可能节点全挂/订阅过期）")
        else:
            log("切换失败（mihomo API 异常）")


if __name__ == "__main__":
    _rc = 0
    try:
        main()
    except Exception as _exc:
        _rc = 1
        raise
    finally:
        try:
            import sys as _s
            _s.path.insert(0, "/opt/astock")
            from db_log import log_job as _lj
            _lj("proxy_guard", "ok" if _rc == 0 else "fail")
        except Exception:
            pass
