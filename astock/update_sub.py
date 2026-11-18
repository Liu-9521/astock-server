#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""更换夜煞云订阅链接：更新 mihomo 配置 + 所有脚本里的 SUB_URL

用法:
    sudo /opt/astock/venv/bin/python update_sub.py "新的订阅链接"
"""
import os
import re
import ssl
import subprocess
import sys
import urllib.request

CONFIG = "/opt/mihomo/config.yaml"
SCRIPTS = [
    "/opt/astock/check_traffic.py",
    "/opt/astock/daily_traffic.py",
    "/opt/astock/webapp.py",
]


def download_sub(url: str) -> bytes:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "clash-verge/1.7.7"})
    with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
        return resp.read()


def main():
    if len(sys.argv) < 2:
        print('用法: python3 update_sub.py "新的订阅链接"')
        sys.exit(1)
    new_url = sys.argv[1].strip()

    # 1) 下载新配置
    print("下载新订阅…")
    data = download_sub(new_url)
    text = data.decode("utf-8", "replace")
    if "proxies:" not in text:
        print("❌ 订阅内容不像 Clash yaml，请确认链接")
        sys.exit(1)

    # 2) 确保有控制接口（去掉旧的，重新加）
    text = re.sub(r"(?m)^external-controller:.*\n?", "", text)
    text = re.sub(r"(?m)^secret:.*\n?", "", text)
    text = "external-controller: 127.0.0.1:9090\n" + text

    # 3) 写配置（备份旧的）
    if os.path.exists(CONFIG):
        subprocess.run(["cp", CONFIG, CONFIG + ".bak"], check=False)
    with open(CONFIG, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"✅ 配置已写入 {CONFIG}")

    # 4) 更新脚本里的 SUB_URL
    old_patterns = [
        r'SUB_URL = \([^)]*\)',
        r'SUB_URL = "[^"]*"',
    ]
    new_line = f'SUB_URL = "{new_url}"'
    for path in SCRIPTS:
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        for pat in old_patterns:
            src, n = re.subn(pat, new_line, src, count=1)
            if n:
                break
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        print(f"✅ 已更新 {path}")

    # 5) 重启 mihomo 和 web（让新 SUB_URL 生效）
    subprocess.run(["systemctl", "restart", "mihomo"], check=False)
    subprocess.run(["systemctl", "restart", "astock-web"], check=False)
    print("✅ mihomo / astock-web 已重启")
    print("\n完成。稍后用 /proxy 页面确认节点和流量正常。")


if __name__ == "__main__":
    main()
