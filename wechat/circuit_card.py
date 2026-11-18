#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制电路标准画法：启停 + 自锁 + 运行指示灯"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FONT = Path("/home/astock/.dsh/dsh_wechat/assets/fonts/NotoSansSC.otf")
OUT = Path("/home/astock/.dsh/dsh_wechat/circuit_card.png")

W, H = 1000, 1400
BG = (255, 255, 255)
INK = (30, 32, 36)
SUB = (120, 126, 138)
RED = (200, 30, 30)
GRN = (20, 130, 70)
BLU = (30, 80, 170)
ORG = (190, 110, 0)


def f(s):
    return ImageFont.truetype(str(FONT), s)


img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)
d.text((36, 26), "控制电路标准画法：启停 + 自锁 + 运行指示", font=f(34), fill=INK)
d.text((36, 74), "对照国标符号，实际控制柜图纸就是这么画的", font=f(21), fill=SUB)
d.line([36, 110, W - 36, 110], fill=(225, 228, 233), width=2)

L, R = 90, 700
TOP, BOT = 150, 610
d.line([L, TOP, L, BOT], fill=RED, width=4)
d.line([R, TOP, R, BOT], fill=BLU, width=4)
d.text((L - 28, TOP - 40), "L", font=f(26), fill=RED)
d.text((R - 10, TOP - 40), "N", font=f(26), fill=BLU)


def wire(x1, y1, x2, y2, c=INK, w=3):
    d.line([x1, y1, x2, y2], fill=c, width=w)


def dot(x, y, c=INK):
    d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=c)


def fuse(x, y):
    d.rectangle([x, y - 12, x + 40, y + 12], outline=INK, width=3)
    d.line([x, y, x + 40, y], fill=INK, width=2)


def nc(x, y):
    """常闭按钮：一条横线 + 中间向上抬起的小斜杠（表示默认接通）"""
    d.line([x - 20, y, x - 6, y], fill=INK, width=3)
    d.line([x + 6, y, x + 20, y], fill=INK, width=3)
    d.line([x - 6, y, x + 6, y], fill=INK, width=3)
    d.line([x, y - 2, x, y - 18], fill=INK, width=3)
    d.line([x - 8, y - 18, x + 8, y - 18], fill=INK, width=3)


def no(x, y, c=INK):
    d.line([x - 24, y, x - 10, y], fill=c, width=3)
    d.line([x - 10, y, x + 16, y - 13], fill=c, width=3)
    d.line([x + 16, y, x + 26, y], fill=c, width=3)


def no_btn(x, y):
    d.line([x - 20, y, x - 8, y], fill=INK, width=3)
    d.line([x - 8, y, x + 8, y - 12], fill=INK, width=3)
    d.line([x + 8, y, x + 20, y], fill=INK, width=3)
    d.line([x - 4, y - 22, x + 12, y - 22], fill=INK, width=3)
    d.line([x + 4, y - 28, x + 4, y - 16], fill=INK, width=3)


def coil(x, y, r=23):
    d.ellipse([x - r, y - r, x + r, y + r], outline=INK, width=3)
    d.text((x - 12, y - 13), "KM", font=f(18), fill=INK)


def lamp(x, y, r=19):
    d.ellipse([x - r, y - r, x + r, y + r], outline=GRN, width=3)
    d.line([x - r + 5, y - r + 5, x + r - 5, y + r - 5], fill=GRN, width=2)
    d.line([x + r - 5, y - r + 5, x - r + 5, y + r - 5], fill=GRN, width=2)


def nc_vert(x, y):
    """常闭触点（竖直画法）：上下引线中间断开 + 一条向上抬的斜杠"""
    d.line([x, y - 20, x, y - 6], fill=INK, width=3)
    d.line([x, y + 6, x, y + 20], fill=INK, width=3)
    d.line([x, y - 6, x, y + 6], fill=INK, width=3)
    d.line([x, y - 6, x + 16, y - 6], fill=INK, width=3)
    d.line([x + 16, y - 6, x + 16, y - 16], fill=INK, width=3)


Y1 = 190
wire(L, Y1, 130, Y1, INK)
fuse(130, Y1)
d.text((122, Y1 + 20), "FU 保险丝", font=f(18), fill=SUB)
wire(170, Y1, 300, Y1)
NA = 300
dot(NA, Y1)
# 主回路竖段，中间串 SB1（常闭）
wire(NA, Y1, NA, 222)
nc_vert(NA, 244)
wire(NA, 266, NA, 356)
d.text((NA + 30, 236), "SB1 停止（常闭）", font=f(18), fill=SUB)

Y2, Y3 = 270, 356
wire(NA, Y2, 240, Y2)
no_btn(276, Y2)
d.text((220, Y2 + 20), "SB2 启动（常开）", font=f(18), fill=SUB)
wire(304, Y2, 400, Y2)
dot(400, Y2)

wire(NA, Y3, 240, Y3)
no(276, Y3, ORG)
d.text((216, Y3 + 20), "KM 常开触点（自锁）", font=f(18), fill=ORG)
wire(310, Y3, 400, Y3)
dot(400, Y3)
dot(NA, Y2)
dot(NA, Y3)
wire(400, Y2, 400, Y3)

YC = 480
wire(400, Y3, 400, YC - 23)
coil(400, YC)
d.text((354, YC + 28), "KM 线圈", font=f(18), fill=SUB)
wire(400, YC + 23, 400, 590)
dot(400, 590)

XL = 620
wire(400, 590, XL, 590)
dot(XL, 590)
wire(XL, 590, XL, YC + 19)
lamp(XL, YC)
d.text((XL + 28, YC - 12), "HL 运行灯", font=f(18), fill=GRN)
wire(XL, YC + 19, XL, 590)
wire(XL, 590, R, 590, BLU)

d.line([36, 650, W - 36, 650], fill=(225, 228, 233), width=2)
d.text((36, 666), "读图要点", font=f(26), fill=INK)
notes = [
    ("①", "L 出、N 回，构成闭合回路才通电。", RED),
    ("②", "FU 在总路上，过流时先烧它。", INK),
    ("③", "SB1 常闭：不按是通的，一按就断。", INK),
    ("④", "SB2 常开：按住才通，松手就断。", INK),
    ("⑤", "KM 触点与 SB2 并联，就是“自锁”。", ORG),
    ("⑥", "线圈与指示灯并联，同时得电。", GRN),
    ("⑦", "停机只能按 SB1，断一处全停。", INK),
]
yy = 716
for mark, t, c in notes:
    d.text((44, yy), mark, font=f(21), fill=c)
    d.text((80, yy), t, font=f(20), fill=INK)
    yy += 44

d.line([36, 1040, W - 36, 1040], fill=(225, 228, 233), width=2)
d.text((36, 1054), "符号图例", font=f(26), fill=INK)
no_btn(120, 1130)
d.text((58, 1162), "常开按钮 SB2", font=f(18), fill=SUB)
nc(380, 1130)
d.text((318, 1162), "常闭按钮 SB1", font=f(18), fill=SUB)
no(620, 1130)
d.text((572, 1162), "常开触点", font=f(18), fill=SUB)
coil(850, 1130)
d.text((818, 1162), "线圈", font=f(18), fill=SUB)

d.text((36, 1216), "实物接线：L→FU→SB1→公共点→(SB2 并联 KM 触点)→KM 线圈 A1→N；", font=f(20), fill=GRN)
d.text((36, 1248), "灯 HL 一端接 A1，一端接 N。A1/A2 是继电器线圈的标准端子号。", font=f(20), fill=GRN)
d.text((36, 1298), "面试常问：按下启动松手后为什么还在运行？→ KM 常开触点闭合，把启动按钮短路了。",
       font=f(20), fill=(150, 40, 40))

img.save(OUT)
print(OUT)
