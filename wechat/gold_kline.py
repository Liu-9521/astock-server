#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成黄金 60 分钟 K 线图（带 MA5/10/20），供微信播报附图。"""
import os
os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplcache")

import json
import re
import urllib.request
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager

BASE = Path("/home/astock/.dsh/dsh_wechat")
FONT = BASE / "assets" / "fonts" / "NotoSansSC.otf"
OUT = BASE / "gold_kline.png"
PROXY = "http://127.0.0.1:7890"


def _font():
    if FONT.exists():
        font_manager.fontManager.addfont(str(FONT))
        return font_manager.FontProperties(fname=str(FONT)).get_name()
    return "sans-serif"


def fetch_60m(code="AU0", n=60):
    """新浪内盘 60 分钟 K 线"""
    url = ("https://stock.finance.sina.com.cn/futures/api/jsonp.php/"
           f"var%20_{code}60=/InnerFuturesNewService.getFewMinLine?symbol={code}&type=60")
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://finance.sina.com.cn"})
    raw = op.open(req, timeout=15).read().decode("utf-8", "ignore")
    m = re.search(r"\((\[.*\])\)", raw, re.S)
    if not m:
        return []
    rows = json.loads(m.group(1))
    out = []
    for r in rows[-n:]:
        try:
            out.append({"t": r["d"], "o": float(r["o"]), "h": float(r["h"]),
                        "l": float(r["l"]), "c": float(r["c"])})
        except Exception:
            continue
    return out


def http_get(url, timeout=15):
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://finance.sina.com.cn"})
    return op.open(req, timeout=timeout).read().decode("utf-8", "ignore")


def usdcny():
    """美元兑人民币，取不到就用固定值"""
    try:
        txt = http_get("https://hq.sinajs.cn/list=fx_susdcny", timeout=8)
        m = re.search(r'fx_susdcny="([^"]+)"', txt)
        if m:
            v = float(m.group(1).split(",")[1])
            if 5 < v < 9:
                return v
    except Exception:
        pass
    return 6.664


EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
            "?secid=101.GC00Y&fields1=f1,f2,f3,f4,f5"
            "&fields2=f51,f52,f53,f54,f55,f56&klt=60&fqt=0&lmt={n}&end=20500101")


def fetch_60m_gold(n=60):
    """COMEX 黄金 60 分钟 K 线（≈伦敦金，24 小时几乎不停），换算成元/克"""
    raw = http_get(EM_KLINE.format(n=n))
    d = json.loads(raw)
    kl = ((d or {}).get("data") or {}).get("klines") or []
    rate = usdcny()
    k = rate / 31.1035
    out = []
    for line in kl[-n:]:
        p = line.split(",")
        try:
            out.append({"t": p[0], "o": float(p[1]) * k, "c": float(p[2]) * k,
                        "h": float(p[3]) * k, "l": float(p[4]) * k})
        except Exception:
            continue
    # 用伦敦金现货校准：COMEX 通常比现货贵 1~2%，直接画会和你看到的
    # 积存金报价差几块钱，这里按现货对齐，只保留 K 线形状
    try:
        txt = http_get("https://hq.sinajs.cn/list=hf_XAU", timeout=8)
        m = re.search(r'hf_XAU="([^"]+)"', txt)
        if m and out:
            spot_yuan = float(m.group(1).split(",")[0]) * k
            delta = spot_yuan - out[-1]["c"]
            for r in out:
                for key in ("o", "h", "l", "c"):
                    r[key] += delta
    except Exception:
        pass
    return out


def ma(vals, n):
    out = []
    for i in range(len(vals)):
        if i + 1 < n:
            out.append(None)
        else:
            out.append(sum(vals[i + 1 - n:i + 1]) / n)
    return out


def draw(rows, cost=None, save=OUT, title="沪金 60分钟", info=None):
    if len(rows) < 10:
        return None
    fam = _font()
    plt.rcParams["font.sans-serif"] = [fam]
    plt.rcParams["axes.unicode_minus"] = False

    xs = list(range(len(rows)))
    closes = [r["c"] for r in rows]
    fig, ax = plt.subplots(figsize=(7.4, 4.4), dpi=130)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#fbfbfd")

    w = 0.62
    for i, r in enumerate(rows):
        up = r["c"] >= r["o"]
        col = "#d23b3b" if up else "#1a8a4f"
        ax.plot([i, i], [r["l"], r["h"]], color=col, linewidth=0.9, zorder=2)
        lo, hi = min(r["o"], r["c"]), max(r["o"], r["c"])
        ax.add_patch(plt.Rectangle((i - w / 2, lo), w, max(hi - lo, 0.01),
                                   facecolor=col, edgecolor=col, zorder=3))

    for n, col, name in ((5, "#e08c00", "MA5"), (10, "#2b6cb0", "MA10"), (20, "#7b5ea7", "MA20")):
        vals = ma(closes, n)
        ax.plot(xs, vals, color=col, linewidth=1.3, label=name, zorder=4)

    if cost:
        ax.axhline(cost, color="#c00", linestyle="--", linewidth=1.2, zorder=5)
        ax.text(0.5, cost, f"成本 {cost:.1f}", color="#c00",
                fontsize=9, va="bottom", ha="left")

    last = closes[-1]
    ax.scatter([len(rows) - 1], [last], color="#c00", s=26, zorder=6)
    ax.text(len(rows) - 1, last, f" {last:.2f}", color="#c00", fontsize=10,
            fontweight="bold", va="center", ha="left")

    # X 轴标出「日期 时:分」，方便一眼看出涨跌用了多久
    step = max(1, len(rows) // 5)
    ticks = list(range(0, len(rows), step))
    if ticks[-1] != len(rows) - 1:
        ticks.append(len(rows) - 1)
    ax.set_xticks(ticks)
    labels = []
    for i in ticks:
        t = str(rows[i]["t"])          # 形如 2026-11-06 13:45:00
        labels.append(t[5:16].replace(" ", "\n") if len(t) >= 16 else t[-5:])
    ax.set_xticklabels(labels, fontsize=7, color="#666")
    ax.tick_params(axis="y", labelsize=8, colors="#666")
    ax.grid(True, linestyle=":", linewidth=0.6, color="#dcdde0", zorder=0)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.set_title(f"{title}  ·  {rows[-1]['t']}", fontsize=12, color="#222", pad=8)
    ax.legend(loc="upper left", fontsize=8, frameon=False, ncol=3)
    if info:
        txt = str(info).replace("\n", " ")
        fig.subplots_adjust(bottom=0.22)
        fig.text(0.5, 0.015, txt, ha="center", va="bottom",
                 fontsize=8.5, color="#444444")
    else:
        fig.tight_layout()
    fig.savefig(save, facecolor="#ffffff")
    plt.close(fig)
    return save


def build(cost=None, info=None):
    try:
        rows = fetch_60m_gold()
    except Exception:
        rows = []
    if len(rows) >= 10:
        return draw(rows, cost, title="黄金 60分钟（元/克）", info=info)
    return draw(fetch_60m(), cost, title="沪金 60分钟", info=info)


if __name__ == "__main__":
    import sys
    c = float(sys.argv[1]) if len(sys.argv) > 1 else None
    print(build(c))
