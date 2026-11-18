#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把盘前简报渲染成图片（微信里换行会被吃掉，图片最稳）"""
import json
import os
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent
FONT = BASE.parent / "assets" / "fonts" / "NotoSansSC.otf"
LIVE = Path(os.environ.get("QLIVE") or (BASE / "live.json"))

W = 760
PAD = 28
BG = (247, 248, 250)
CARD = (255, 255, 255)
INK = (28, 30, 34)
SUB = (124, 130, 142)
RED = (209, 51, 51)
GREEN = (22, 138, 79)
LINE = (232, 234, 238)
BRAND = (192, 0, 0)


def font(size, bold=False):
    return ImageFont.truetype(str(FONT), size)


def money(v, d=2):
    return f"{v:,.{d}f}"


def build(d, now):
    snap = d.get("snapshot") or {}
    equity = snap.get("equity") or d.get("initial") or 100000
    cash = snap.get("cash") or 0
    init = d.get("initial") or 1
    pos = d.get("positions") or {}
    total_pct = (equity / init - 1) * 100
    pnl_all = equity - init

    rows = []
    for code, p in pos.items():
        px = p.get("last_price") or p.get("cost") or 0
        cost = p.get("cost") or 0
        pct = (px / cost - 1) * 100 if cost else 0
        rows.append({
            "name": p.get("name") or code,
            "qty": p.get("qty", 0),
            "cost": cost,
            "px": px,
            "pct": pct,
            "sellable": p.get("buy_date") != now.strftime("%Y-%m-%d"),
        })

    head_h = 250
    row_h = 62
    body_h = max(1, len(rows)) * row_h
    h = head_h + body_h + 220
    img = Image.new("RGB", (W, h), BG)
    dr = ImageDraw.Draw(img)

    f_title = font(34, True)
    f_sub = font(20)
    f_big = font(52, True)
    f_lab = font(19)
    f_name = font(22, True)
    f_num = font(21)
    f_tag = font(17)

    # 顶部品牌条
    dr.rectangle([0, 0, W, 8], fill=BRAND)

    y = 34
    dr.text((PAD, y), "模拟盘 · 盘前简报", font=f_title, fill=INK)
    y += 48
    wd = "一二三四五六日"[now.weekday()]
    dr.text((PAD, y), f"{now:%Y年%m月%d日}  周{wd}", font=f_sub, fill=SUB)
    y += 44

    # 总资产卡
    dr.rounded_rectangle([PAD, y, W - PAD, y + 112], 16, fill=CARD)
    dr.text((PAD + 24, y + 18), "总资产", font=f_lab, fill=SUB)
    dr.text((PAD + 24, y + 46), money(equity), font=f_big, fill=INK)
    color = RED if total_pct >= 0 else GREEN
    sign = "+" if total_pct >= 0 else ""
    dr.text((W - PAD - 24, y + 74), f"{sign}{total_pct:.2f}%", font=font(30, True),
            fill=color, anchor="ra")
    y += 130

    mk2 = (d.get("snapshot") or {}).get("market") or {}
    if mk2.get("last"):
        mc2 = RED if (mk2.get("chg") or 0) >= 0 else GREEN
        dr.text((PAD, y), f"上证 {mk2['last']:.2f}", font=f_lab, fill=INK)
        dr.text((PAD + 130, y), f"{mk2.get('chg') or 0:+.2f}%", font=f_lab, fill=mc2)
        dr.text((W - PAD, y), str(mk2.get("level") or ""), font=f_lab, fill=SUB, anchor="ra")
        y += 32

    dr.text((PAD, y), f"现金 {money(cash)}    ·    持仓 {len(pos)} 只     ·    "
                      f"总体盈亏 {pnl_all:+,.0f} 元", font=f_lab, fill=SUB)
    y += 44

    # 持仓标题
    if rows:
        dr.text((PAD, y), "持仓明细", font=font(24, True), fill=INK)
        dr.text((W - PAD, y + 4), "T+1 今日可卖", font=f_tag, fill=SUB, anchor="ra")
        y += 44

        for r in rows:
            dr.rounded_rectangle([PAD, y, W - PAD, y + row_h - 10], 12, fill=CARD)
            dr.text((PAD + 20, y + 9), r["name"], font=f_name, fill=INK)

            c = RED if r["pct"] >= 0 else GREEN
            sg = "+" if r["pct"] >= 0 else ""
            dr.text((W - PAD - 20, y + 9), f"{sg}{r['pct']:.2f}%", font=font(23, True),
                    fill=c, anchor="ra")

            dr.text((PAD + 20, y + 37), f"{r['qty']}股   {r['cost']:.3f} → {r['px']:.3f}",
                    font=font(19), fill=SUB)

            if not r["sellable"]:
                dr.text((W - PAD - 20, y + 38), "今日买入", font=font(18), fill=(196, 138, 30),
                        anchor="ra")
            y += row_h
    else:
        dr.text((PAD, y), "当前空仓，等待信号", font=font(24), fill=SUB)
        y += 60

    y += 12
    dr.line([PAD, y, W - PAD, y], fill=LINE, width=1)
    y += 22
    dr.text((PAD, y), f"引擎：{'运行中，9:30 开盘自动交易' if d.get('enabled', True) else '已暂停'}",
            font=f_lab, fill=SUB)

    return img.crop((0, 0, W, min(h, y + 60)))


def main():
    now = datetime.now()
    d = json.loads(LIVE.read_text(encoding="utf-8"))
    img = build(d, now)
    out = BASE / "morning_card.png"
    img.save(out)
    print(out, img.size)


if __name__ == "__main__":
    main()


def build_close(d, now):
    """收盘日报卡片"""
    snap = d.get("snapshot") or {}
    prices = snap.get("prices") or {}
    pts = [p for p in (d.get("equity") or []) if (p.get("t") or "").startswith(now.strftime("%m-%d"))]
    equity = snap.get("equity") or (pts[-1]["v"] if pts else d.get("initial") or 0)
    open_eq = pts[0]["v"] if pts else equity
    initial = d.get("initial") or 1
    day_pnl = equity - open_eq
    day_pct = (equity / open_eq - 1) * 100 if open_eq else 0
    total_pnl = equity - initial
    total_pct = (equity / initial - 1) * 100 if initial else 0

    rows = []
    for code, p in (d.get("positions") or {}).items():
        px = prices.get(code) or p.get("last_price") or p.get("cost") or 0
        cost = p.get("cost") or 0
        rows.append({"name": p.get("name") or code, "qty": p.get("qty", 0),
                     "cost": cost, "px": px, "pct": (px / cost - 1) * 100 if cost else 0})
    rows.sort(key=lambda r: -r["pct"])
    show = rows                      # 全部显示

    trades = [t for t in (d.get("trades") or []) if (t.get("t") or "").startswith(now.strftime("%Y-%m-%d"))]

    f_title, f_sub = font(34, True), font(20)
    f_big, f_lab = font(56, True), font(19)
    f_name, f_num, f_tag = font(22, True), font(19), font(17)

    head = 300
    body = len(show) * 62 + 90
    tail = 70 + min(len(trades), 6) * 40 + 90
    h = head + body + tail + 120
    img = Image.new("RGB", (W, h), BG)
    dr = ImageDraw.Draw(img)
    dr.rectangle([0, 0, W, 8], fill=BRAND)

    y = 34
    dr.text((PAD, y), "模拟盘 · 收盘日报", font=f_title, fill=INK)
    y += 48
    dr.text((PAD, y), f"{now:%Y年%m月%d日}  周{'一二三四五六日'[now.weekday()]}", font=f_sub, fill=SUB)
    y += 44

    dr.rounded_rectangle([PAD, y, W - PAD, y + 128], 16, fill=CARD)
    dr.text((PAD + 24, y + 18), "今日盈亏", font=f_lab, fill=SUB)
    c = RED if day_pnl >= 0 else GREEN
    dr.text((PAD + 24, y + 44), f"{day_pnl:+,.0f}", font=f_big, fill=c)
    dr.text((PAD + 24 + 190, y + 74), "元", font=f_lab, fill=SUB)
    dr.text((W - PAD - 24, y + 78), f"{day_pct:+.2f}%", font=font(30, True), fill=c, anchor="ra")
    y += 148

    mk = (d.get("snapshot") or {}).get("market") or {}
    if mk.get("last"):
        mc = RED if (mk.get("chg") or 0) >= 0 else GREEN
        dr.text((PAD, y), f"上证 {mk['last']:.2f}", font=f_num, fill=INK)
        dr.text((PAD + 150, y), f"{mk.get('chg') or 0:+.2f}%", font=f_num, fill=mc)
        dr.text((W - PAD, y), str(mk.get("level") or ""), font=f_num, fill=SUB, anchor="ra")
        y += 40

    dr.text((PAD, y), f"总资产 {money(equity)}", font=f_num, fill=INK)
    dr.text((W - PAD, y), f"累计 {total_pct:+.2f}%  ({total_pnl:+,.0f} 元)",
            font=f_num, fill=(RED if total_pct >= 0 else GREEN), anchor="ra")
    y += 46

    dr.text((PAD, y), f"持仓涨跌 · 共 {len(rows)} 只", font=font(24, True), fill=INK)
    y += 42

    if show:
        for r in show:
            dr.rounded_rectangle([PAD, y, W - PAD, y + 52], 12, fill=CARD)
            dr.text((PAD + 20, y + 14), r["name"], font=f_name, fill=INK)
            cc = RED if r["pct"] >= 0 else GREEN
            sg = "+" if r["pct"] >= 0 else ""
            dr.text((W - PAD - 20, y + 14), f"{sg}{r['pct']:.2f}%", font=font(22, True),
                    fill=cc, anchor="ra")
            dr.text((W - PAD - 130, y + 16), f"{r['qty']}股", font=f_tag, fill=SUB, anchor="ra")
            y += 62
    else:
        dr.text((PAD, y), "当前空仓", font=font(22), fill=SUB)
        y += 40

    y += 10
    dr.line([PAD, y, W - PAD, y], fill=LINE, width=1)
    y += 22

    if trades:
        dr.text((PAD, y), f"今日成交 {len(trades)} 笔", font=font(22, True), fill=INK)
        y += 40
        for t in trades[:6]:
            side = t.get("side") or ""
            nm = t.get("name") or t.get("code") or ""
            pnl = t.get("pnl")
            dr.text((PAD + 8, y), f"{side}  {nm}", font=f_tag, fill=SUB)
            if pnl is not None:
                dr.text((W - PAD, y), f"{pnl:+,.2f}", font=f_tag,
                        fill=(RED if pnl >= 0 else GREEN), anchor="ra")
            y += 36
    else:
        dr.text((PAD, y), "今日无成交", font=f_tag, fill=SUB)
        y += 36

    y += 14
    dr.text((PAD, y), f"自选 {len(d.get('stocks') or [])} 只 · 模拟盘，不构成投资建议",
            font=font(17), fill=(160, 165, 175))
    return img.crop((0, 0, W, min(h, y + 50)))
