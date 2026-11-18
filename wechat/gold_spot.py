#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄金位置播报 → 微信。每 90 分钟一次，单行文本（微信换行会被吃掉）。

cron 每 15 分钟触发，脚本自己判断距上次满 90 分钟才发。
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE = BASE / "gold_spot.json"
LOCK = BASE / "gold_spot.lock"
LOG = BASE / "gold_spot.log"
SEND = "/home/astock/.dsh/profiles/headless/node_modules/.bin/dsh-wechat-send"
PROXY = "http://127.0.0.1:7890"
GAP_SEC = 3 * 60 * 60   # 3 小时（配合 cron 的 9/12/15/21）
_LOCK_FH = None   # 持锁到进程结束，自动释放


def get(url, timeout=12):
    import urllib.request
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    req = urllib.request.Request(url, headers={
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0",
    })
    return op.open(req, timeout=timeout).read()


def spot_price():
    """返回伦敦金与沪金主连的实时价"""
    raw = get("https://hq.sinajs.cn/list=hf_XAU,nf_AU0").decode("gbk", "ignore")
    out = {}
    m = re.search(r'hf_XAU="([^"]+)"', raw)
    if m:
        f = m.group(1).split(",")
        out["xau"] = {"price": float(f[0]), "prev": float(f[7] or 0), "t": f[6]}
    m = re.search(r'nf_AU0="([^"]+)"', raw)
    if m:
        f = m.group(1).split(",")
        out["au"] = {"price": float(f[6]), "prev": float(f[10] or 0)}
    return out or None


def au_daily():
    raw = get("https://stock.finance.sina.com.cn/futures/api/jsonp.php/"
              "var%20_AU0=/InnerFuturesNewService.getDailyKLine?symbol=AU0")
    s = raw.decode("utf-8", "ignore")
    m = re.search(r"\((\[.*\])\)", s, re.S)
    if not m:
        return []
    return json.loads(m.group(1))


def main():
    force = "--force" in sys.argv

    # 时段限制：伦敦金（浙商积存金跟踪的就是它）23 小时连续交易，
    # 所以 9:00–23:30 全程播报，不再按沪金收盘时间 15:00 停。
    t = datetime.now()
    if not force:
        if t.weekday() >= 5:
            return 0
        in_night = t.hour == 23 and t.minute <= 30
        in_day = 9 <= t.hour <= 22
        if not (in_day or in_night):
            return 0
        # 防并发：整点 cron 与 gold_watch 的 kick 可能同时触发
        global _LOCK_FH
        _LOCK_FH = open(LOCK, "w")
        try:
            fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

    st = {}
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            st = {}

    now = time.time()
    last = float(st.get("last_sent") or 0)
    slot = t.strftime("%Y%m%d%H")
    if not force:
        # 按「小时槽」判断：同一小时只发一次，不受几十秒时差影响
        if st.get("last_slot") == slot:
            return 0
        if now - last < 20 * 60:   # 本小时内刚补发过，不重复
            return 0

    try:
        sp = spot_price()
        rows = au_daily()
    except Exception as e:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%F %T} 取数失败 {e}\n")
        return 1
    if not sp or len(rows) < 21:
        return 1

    closes = [float(r["c"]) for r in rows]
    ma20 = sum(closes[-20:]) / 20
    hi20 = max(float(r["h"]) for r in rows[-20:])
    lo20 = min(float(r["l"]) for r in rows[-20:])
    last_row = rows[-1]
    au_prev = float(last_row["c"])
    xau = sp["xau"]
    px = xau["price"]
    chg = (px / xau["prev"] - 1) * 100 if xau["prev"] else 0

    # 位置判断：直接用沪金主连实时价
    au_now = (sp.get("au") or {}).get("price") or au_prev
    dev = (au_now / ma20 - 1) * 100

    if au_now >= hi20 * 0.99:
        pos = "接近20日高点"
    elif au_now <= lo20 * 1.01:
        pos = "接近20日低点"
    elif dev >= 3:
        pos = "高位"
    elif dev <= -3:
        pos = "低位"
    elif dev >= 0:
        pos = "中位偏上"
    else:
        pos = "中位偏下"

    arrow = "↑" if chg >= 0 else "↓"
    # 15:00–21:00 沪金休市，标注"收盘"避免误以为行情卡住
    au_tag = "沪金收盘" if 15 <= t.hour < 21 else "沪金≈"
    msg = (f"🟡 黄金 {px:.1f}{arrow}{chg:+.2f}%｜"
           f"{au_tag}{au_now:.0f} MA20 {ma20:.0f}({dev:+.1f}%)｜"
           f"20日 {lo20:.0f}~{hi20:.0f}｜{pos}｜{xau['t'][:5]}")

    if "--dry" in sys.argv:
        print(msg)
        return 0
    # 附图：60 分钟 K 线（带成本线）
    png = None
    try:
        import gold_kline
        png = gold_kline.build(cost=float(os.environ.get("GOLD_COST", "877.47")), info=msg)
    except Exception as e:
        LOG.open("a", encoding="utf-8").write(f"{datetime.now():%F %T} 绘图失败 {e}\n")

    try:
        if png and os.path.exists(png):
            # 文字已画进图里，只发一张图
            r = subprocess.run([SEND, "--file", png],
                               capture_output=True, text=True, timeout=90)
        else:
            r = subprocess.run([SEND, msg], capture_output=True, text=True, timeout=40)
        rc = r.returncode
    except Exception as e:
        rc = 1
    if rc in (0, 3):
        st["last_sent"] = now
        st["last_slot"] = slot
        st["last_msg"] = msg
        # 写入历史库（失败不影响主流程）
        try:
            import sqlite3
            _con = sqlite3.connect("/opt/astock/astock.db", timeout=5)
            _rows = []
            if isinstance(st.get("last_msg"), str):
                pass
            try:
                _x = (sp or {}).get("xau") or {}
                if _x.get("price"):
                    _p = float(_x["price"])
                    _prev = float(_x.get("prev") or 0)
                    _chg = round((_p - _prev) / _prev * 100, 3) if _prev else None
                    _rows.append(("XAU", _p, _chg))
                _a = (sp or {}).get("au") or {}
                if _a.get("price"):
                    _p = float(_a["price"])
                    _prev = float(_a.get("prev") or 0)
                    _chg = round((_p - _prev) / _prev * 100, 3) if _prev else None
                    _rows.append(("AU0", _p, _chg))
            except Exception:
                pass
            for _sym, _p, _chg in _rows:
                _con.execute("INSERT INTO gold_history(symbol,price,chg) VALUES (?,?,?)",
                             (_sym, _p, _chg))
            _con.commit()
            _con.close()
        except Exception:
            pass
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%F %T} rc={rc} {msg}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
