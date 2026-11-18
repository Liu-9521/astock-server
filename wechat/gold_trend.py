#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""沪金趋势反转提醒：短线(MA5/MA10) + 中期(MA20) 拐点，变了才推。

由 cron 每 30 分钟跑一次，状态变化才发微信，避免刷屏。
state: gold_trend.json
"""

import json
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE = BASE / "gold_trend.json"
LOG = BASE / "gold_trend.log"
SEND = "/home/astock/.dsh/profiles/headless/node_modules/.bin/dsh-wechat-send"

HEAD = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
KLINE_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_=/"
             "InnerFuturesNewService.getDailyKLine?symbol=AU0")
QUOTE_URL = "https://hq.sinajs.cn/list=nf_AU0"


def log(msg):
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%F %T} {msg}\n")
    except OSError:
        pass


def send(text):
    try:
        r = subprocess.run([SEND, text], capture_output=True, text=True, timeout=40)
        log(f"send rc={r.returncode}")
        return r.returncode in (0, 3)
    except Exception as exc:  # noqa: BLE001
        log(f"send err {exc}")
        return False


def get(url, enc="utf-8"):
    req = urllib.request.Request(url, headers=HEAD)
    return urllib.request.urlopen(req, timeout=15).read().decode(enc, "ignore")


def daily_closes():
    import re
    txt = get(KLINE_URL)
    m = re.search(r"\((\[.*\])\)", txt, re.S)
    if not m:
        return []
    rows = json.loads(m.group(1))
    return [(r["d"], float(r["c"])) for r in rows]


def spot():
    txt = get(QUOTE_URL, enc="gbk")
    body = txt.split('="', 1)[1].rstrip('";\n')
    f = body.split(",")
    # nf_ 合约字段：0 名称 1 时间 2 开 3 高 4 低 ... 6 现价 ... 10 昨结
    px = float(f[6])
    settle = float(f[10]) if len(f) > 10 and f[10] else px
    return px, settle


def ma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def main():
    dry = "--dry" in sys.argv
    try:
        rows = daily_closes()
        px, prev_settle = spot()
    except Exception as exc:  # noqa: BLE001
        log(f"取数失败 {type(exc).__name__}: {exc}")
        return 1
    if len(rows) < 30:
        log("历史数据不足")
        return 1

    dates = [d for d, _ in rows]
    closes = [c for _, c in rows]
    # 把当日现价并入日线（盘中形成中的那一根）
    today = datetime.now().strftime("%Y-%m-%d")
    if dates[-1] != today:
        dates.append(today)
        closes.append(px)
    else:
        closes[-1] = px

    ma5, ma10, ma20 = ma(closes, 5), ma(closes, 10), ma(closes, 20)
    ma20_prev = sum(closes[-21:-1]) / 20 if len(closes) >= 21 else ma20
    if None in (ma5, ma10, ma20):
        log("均线数据不足")
        return 1

    short = "up" if ma5 > ma10 else "down"          # 短线：MA5 vs MA10
    mid = "bull" if (px > ma20 and ma20 >= ma20_prev) else ("bear" if px < ma20 else "hold")

    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    old_short = st.get("short")
    old_mid = st.get("mid")

    cny = px
    msgs = []
    if old_short and old_short != short:
        if short == "up":
            msgs.append(f"🟢 沪金短线转强\nMA5 上穿 MA10（{ma5:.1f} / {ma10:.1f}）\n"
                        f"现价 {cny:.1f} 元/克，可留意企稳")
        else:
            msgs.append(f"🔴 沪金短线转弱\nMA5 下穿 MA10（{ma5:.1f} / {ma10:.1f}）\n"
                        f"现价 {cny:.1f} 元/克，注意回调")
    if old_mid and old_mid != mid:
        if mid == "bull":
            msgs.append(f"🔺 沪金站上 MA20（{ma20:.1f}）\n现价 {cny:.1f} 元/克，中期趋势转多")
        elif mid == "bear":
            msgs.append(f"🔻 沪金跌破 MA20（{ma20:.1f}）\n现价 {cny:.1f} 元/克，中期趋势转空")

    st.update({"short": short, "mid": mid, "px": px,
               "ma5": round(ma5, 2), "ma10": round(ma10, 2), "ma20": round(ma20, 2),
               "t": datetime.now().strftime("%Y-%m-%d %H:%M")})
    try:
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass

    text = "\n\n".join(msgs)
    if dry:
        print(f"现价 {px:.2f} | MA5 {ma5:.2f} MA10 {ma10:.2f} MA20 {ma20:.2f}")
        print(f"短线 {short}（上次 {old_short}） 中期 {mid}（上次 {old_mid}）")
        print("将推送：\n" + (text or "（无）"))
        return 0
    if text:
        send(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
