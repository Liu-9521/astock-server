#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""沪金关键位提醒：站上 900 / 跌破 880，状态变化才推，带回差防抖。

cron 每 10 分钟跑一次。
"""
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE = BASE / "gold_signal.json"
LOG = BASE / "gold_signal.log"
SEND = "/home/astock/.dsh/profiles/headless/node_modules/.bin/dsh-wechat-send"
PROXY = "http://127.0.0.1:7890"

UP = 900.0        # 站上就提醒
UP_BACK = 895.0   # 回落到这里才重置

# 已经买过第一批，往下按档提醒：加仓 870 / 860，止损 855
LEVELS = [
    (830.0, 845.0, "🟢 到 830 了——第二批可以接"),
    (780.0, 800.0, "🔴 跌破 780——判断失误，先全部退出，重新等入场信号"),
]


def get(url, timeout=12):
    import urllib.request
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    req = urllib.request.Request(url, headers={
        "Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"})
    return op.open(req, timeout=timeout).read()


def au_price():
    raw = get("https://hq.sinajs.cn/list=nf_AU0").decode("gbk", "ignore")
    m = re.search(r'nf_AU0="([^"]+)"', raw)
    if not m:
        return None
    f = m.group(1).split(",")
    return {"price": float(f[6]), "prev": float(f[10] or 0)}


def push(msg):
    try:
        r = subprocess.run([SEND, msg], capture_output=True, text=True, timeout=40)
        return r.returncode
    except Exception:
        return 1


def main():
    st = {}
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            st = {}
    mode = st.get("mode", "normal")

    try:
        p = au_price()
    except Exception as e:
        LOG.open("a", encoding="utf-8").write(f"{datetime.now():%F %T} 取数失败 {e}\n")
        return 1
    if not p:
        return 1
    px = p["price"]
    chg = (px / p["prev"] - 1) * 100 if p["prev"] else 0

    msgs = []
    if mode != "up" and px >= UP:
        msgs.append(f"🚀 沪金站上 900！现价 {px:.2f}（{chg:+.2f}%）"
                    f"——按计划可追 1/3 仓位，别等回踩了")
        mode = "up"
    elif mode == "up" and px < UP_BACK:
        mode = "normal"

    # 退出后重新武装：站回 885 以上说明又回到震荡区，可以重新看
    if px >= 885.0 and st.get("exited"):
        msgs.append(f"🔄 沪金回到 885 上方（{px:.2f}）——可以重新评估入场时机了")
        st["exited"] = False

    hit = st.get("hit") or {}          # 记录每一档是否已经提醒过
    for lvl, back, text in LEVELS:
        key = str(int(lvl))
        if px <= lvl and not hit.get(key):
            msgs.append(f"{text}｜现价 {px:.2f}（{chg:+.2f}%）")
            hit[key] = True
            if lvl == 780.0:
                st["exited"] = True
        elif px > back and hit.get(key):
            hit.pop(key, None)         # 回到回差上方，这一档重新武装
    st["hit"] = hit

    rc = 0
    for m in msgs:
        rc = push(m)
        LOG.open("a", encoding="utf-8").write(f"{datetime.now():%F %T} rc={rc} {m}\n")

    st["mode"] = mode
    st["price"] = px
    st["t"] = datetime.now().strftime("%F %T")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    if "--dry" in sys.argv:
        print(px, mode, msgs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
