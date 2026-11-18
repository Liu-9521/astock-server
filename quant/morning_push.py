#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟盘盘前简报 → 微信。交易日 09:25 由 cron 调用。"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
LIVE = Path(os.environ.get("QLIVE") or (BASE / "live.json"))
CONFIG = Path(os.environ.get("QCONFIG") or "/opt/astock/config.json")
SEND = "/home/astock/.dsh/profiles/headless/node_modules/.bin/dsh-wechat-send"

STRAT = {"ma": "双均线", "rsi": "RSI", "breakout": "突破"}


def is_trading_day(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        return now.strftime("%Y-%m-%d") not in {str(h) for h in (cfg.get("holidays") or [])}
    except Exception:
        return True


def fmt(v, d=2):
    return f"{v:,.{d}f}"


def main():
    now = datetime.now()
    if not is_trading_day(now):
        return 0
    if not LIVE.exists():
        return 0
    d = json.loads(LIVE.read_text(encoding="utf-8"))
    snap = d.get("snapshot") or {}
    equity = snap.get("equity") or d.get("initial") or 100000
    cash = snap.get("cash") or d.get("cash") or 0

    lines = [f"🤖 模拟盘盘前 · {now:%Y-%m-%d} 周{'一二三四五六日'[now.weekday()]}"]

    pos = d.get("positions") or {}
    total_pct = (equity / (d.get("initial") or 1) - 1) * 100
    lines.append(f"总资产：{fmt(equity)}（累计 {total_pct:+.2f}%）")
    lines.append(f"现金：{fmt(cash)} · 持仓 {len(pos)} 只")

    if pos:
        lines.append("")
        lines.append("持仓（T+1 今日可卖）：")
        for code, p in pos.items():
            px = p.get("last_price") or p.get("cost")
            pct = (px / p["cost"] - 1) * 100 if p.get("cost") else 0
            sellable = "可卖" if p.get("buy_date") != now.strftime("%Y-%m-%d") else "今日买入"
            lines.append(f"· {p.get('name') or code} {p['qty']}股 "
                         f"{fmt(p['cost'],3)}→{fmt(px,3)} {pct:+.2f}% [{sellable}]")
    else:
        lines.append("当前空仓，等待信号")

    stocks = d.get("stocks") or []

    # ---- 盈亏统计 ----
    from collections import defaultdict

    def day_key(ts: str) -> str:
        return (ts or "")[:10]

    eq = d.get("equity") or []
    days = []
    for e in eq:
        dk = (e.get("t") or "")[:5]
        if not days or days[-1][0] != dk:
            days.append([dk, e.get("v")])
        else:
            days[-1][1] = e.get("v")
    today_key = now.strftime("%m-%d")
    if days and days[-1][0] == today_key and len(days) >= 2:
        days = days[:-1]          # 盘前若已有今日数据，按上一交易日算
    y_pnl = 0.0
    y_label = "昨日"
    if len(days) >= 2:
        y_pnl = (days[-1][1] or 0) - (days[-2][1] or 0)
        y_label = "昨日"
    elif len(days) == 1:
        y_pnl = (days[-1][1] or 0) - (d.get("initial") or 0)

    trades = d.get("trades") or []
    last_day_full = ""
    if days:
        last_day_full = days[-1][0]          # 形如 11-03
    y_by = defaultdict(float)
    all_by = defaultdict(float)
    for t in trades:
        pnl = t.get("pnl")
        if pnl is None:
            continue
        nm = t.get("name") or t.get("code")
        all_by[nm] += pnl
        if last_day_full and (t.get("t") or "")[5:10] == last_day_full:
            y_by[nm] += pnl

    lines.append("")
    lines.append(f"{y_label}盈亏：{y_pnl:+,.2f} 元")
    lines.append(f"总体盈亏：{equity - (d.get('initial') or 0):+,.2f} 元"
                 f"（{total_pct:+.2f}%）")

    if y_by:
        best = max(y_by.items(), key=lambda kv: kv[1])
        lines.append(f"{y_label}最赚：{best[0]} {best[1]:+,.2f} 元")
    else:
        lines.append(f"{y_label}最赚：无平仓记录")

    if all_by:
        best_all = max(all_by.items(), key=lambda kv: kv[1])
        lines.append(f"总体最赚：{best_all[0]} {best_all[1]:+,.2f} 元")
    elif pos:
        fl = []
        for code, p in pos.items():
            px = p.get("last_price") or p.get("cost") or 0
            fl.append(((p.get("name") or code), (px - (p.get("cost") or px)) * p.get("qty", 0)))
        b = max(fl, key=lambda kv: kv[1])
        lines.append(f"总体最赚（浮盈）：{b[0]} {b[1]:+,.2f} 元")
    else:
        lines.append("总体最赚：暂无")

    lines.append(f"监控：{len(stocks)} 只" if stocks else "⚠️ 自选为空，今天不会有任何交易")

    lines.append("")
    lines.append("引擎：" + ("运行中，9:30 开盘自动交易" if d.get("enabled", True) else "⏸ 已暂停"))

    msg = "\n".join(lines)
    if "--dry" in sys.argv:
        print(msg)
        return 0

    try:
        import report_card
        png = BASE / "morning_card.png"
        report_card.build(d, now).save(png)
        r = subprocess.run([SEND, "--file", str(png)], capture_output=True, text=True, timeout=60)
        rc = r.returncode
    except Exception as e:
        rc = 1
        r = type("x", (), {"stdout": "", "stderr": str(e)})()
    with (BASE / "push.log").open("a", encoding="utf-8") as f:
        f.write(f"{now:%F %T} morning rc={rc}\n")
    return 0 if rc in (0, 3) else 1


if __name__ == "__main__":
    sys.exit(main())
