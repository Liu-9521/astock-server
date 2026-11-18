#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟盘收盘日报 → 微信。交易日 15:05 由 cron 调用。"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
LIVE = Path(os.environ.get("QLIVE") or (BASE / "live.json"))
SEND = "/home/astock/.dsh/profiles/headless/node_modules/.bin/dsh-wechat-send"


def fmt(v, d=2):
    return f"{v:,.{d}f}"


def main():
    if not LIVE.exists():
        return 0
    d = json.loads(LIVE.read_text(encoding="utf-8"))
    today = datetime.now().strftime("%Y-%m-%d")
    today_md = datetime.now().strftime("%m-%d")

    eq_all = d.get("equity") or []
    yday_md = (datetime.now() - timedelta(days=1)).strftime("%m-%d")
    prev_pts = [p for p in eq_all if str(p.get("t") or "").startswith(yday_md)]
    pts = [p for p in eq_all if str(p.get("t") or "").startswith(today_md)]
    if not pts:
        return 0  # 今天没跑（休市/未启动），不发

    snap = d.get("snapshot") or {}
    equity = snap.get("equity") or pts[-1]["v"]
    # 基准：昨天收盘(day_close) > 曲线里昨天的最后一点 > 今天第一个点
    dc = d.get("day_close") or {}
    yday_key = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    open_eq = (dc.get(yday_key)
               or (prev_pts[-1]["v"] if prev_pts else None)
               or (pts[0]["v"] if pts else equity))
    initial = d.get("initial") or 100000

    day_pnl = equity - open_eq
    day_pct = (equity / open_eq - 1) * 100 if open_eq else 0
    total_pnl = equity - initial
    total_pct = (equity / initial - 1) * 100 if initial else 0

    sep = "———————"
    lines = [
        f"🤖 模拟盘日报 · {today[5:]}",
        "",
        f"总资产：{fmt(equity)}",
        f"今日盈亏：{day_pnl:+,.2f} 元（{day_pct:+.2f}%）",
        f"累计盈亏：{total_pnl:+,.2f} 元（{total_pct:+.2f}%）",
        "",
        sep,
        "",
    ]

    pos = d.get("positions") or {}
    prices = (snap.get("prices") or {})
    if pos:
        rows = []
        for code, p in pos.items():
            px = prices.get(code) or p.get("last_price") or p.get("cost")
            pct = (px / p["cost"] - 1) * 100 if p.get("cost") else 0
            rows.append((pct, p.get("name") or code, p["qty"], p["cost"], px))
        rows.sort(key=lambda r: -r[0])
        lines.append(f"持仓 {len(rows)} 只")
        lines.append("")
        show = rows[:5]
        if len(rows) > 5:
            show += rows[-3:] if len(rows) > 8 else rows[5:]
        for pct, name, qty, cost, px in show:
            lines.append(f"· {name}  {pct:+.2f}%")
        if len(rows) > len(show):
            lines.append(f"… 其余 {len(rows)-len(show)} 只省略")
    else:
        lines.append("当前空仓")

    trades = [t for t in (d.get("trades") or []) if t.get("t", "").startswith(today)]
    lines.append("")
    lines.append(sep)
    lines.append("")
    if trades:
        lines.append(f"今日成交 {len(trades)} 笔")
        lines.append("")
        for t in trades[:6]:
            pnl = f" {t['pnl']:+,.2f}" if t.get("pnl") is not None else ""
            lines.append(f"· {t['side']} {t.get('name') or t['code']}{pnl}")
        if len(trades) > 6:
            lines.append(f"… 其余 {len(trades)-6} 笔省略")
    else:
        lines.append("今日无成交")

    lines.append("")
    lines.append(f"自选 {len(d.get('stocks') or [])} 只 · 模拟盘，不构成投资建议")

    msg = "\n".join(lines)
    if "--dry" in sys.argv:
        print(msg)
        return 0
    try:
        import report_card
        png = BASE / "close_card.png"
        report_card.build_close(d, datetime.now()).save(png)
        r = subprocess.run([SEND, "--file", str(png)], capture_output=True, text=True, timeout=60)
        rc = r.returncode
    except Exception as e:
        rc = 1
        r = type("x", (), {"stdout": "", "stderr": str(e)})()
    log = BASE / "push.log"
    with log.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%F %T} rc={rc} {(r.stdout or '').strip()[:200]}\n")
    return 0 if rc in (0, 3) else 1


if __name__ == "__main__":
    sys.exit(main())
