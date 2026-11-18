#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期货从业题型样例 → 图片（微信里换行会被吃掉）"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent
FONT = BASE / "assets" / "fonts" / "NotoSansSC.otf"
OUT = BASE / "quiz_card.png"

W = 900
PAD = 40
BG = (247, 248, 250)
CARD = (255, 255, 255)
INK = (28, 30, 34)
SUB = (120, 126, 138)
RED = (192, 0, 0)
BLUE = (28, 78, 160)
GREEN = (22, 120, 70)
LINE = (228, 231, 236)

SECTIONS = [
    ("单选题", [
        ("某商品期货合约到期时仍持仓，一般的了结方式是？",
         ["A. 现金交割", "B. 实物交割或现金交割，按合约规定", "C. 自动平仓", "D. 只能展期"],
         "B", "商品期货多数实物交割，股指等现金交割，以合约为准。"),
        ("沪深300股指期货，合约乘数300元/点。指数3500点时开1手，保证金率12%，需保证金约？",
         ["A. 12.6万", "B. 10.5万", "C. 42万", "D. 126万"],
         "A", "3500×300×12% = 126,000 元。"),
    ]),
    ("多选题（多选、少选都不给分）", [
        ("期货交易的特征包括：",
         ["A. 合约标准化", "B. 保证金交易", "C. 当日无负债结算", "D. 必须实物交割"],
         "ABC", "D 错，多数合约可对冲平仓，不必交割。"),
    ]),
    ("判断题", [
        ("期货交易实行当日无负债结算制度（逐日盯市）。",
         ["A. 正确", "B. 错误"],
         "A", "每日按结算价划转盈亏，保证金不足要追保或强平。"),
    ]),
    ("综合题（接近实操）", [
        ("某铜加工厂3个月后要买入100吨铜，担心铜价上涨，应做的套保是？",
         ["A. 卖出期货（空头套保）", "B. 买入期货（多头套保）", "C. 买入看跌期权", "D. 不做"],
         "B", "未来要买 → 怕涨 → 先买期货锁定成本，即多头套保。"),
    ]),
    ("法规题", [
        ("期货从业人员可以私下代客户进行期货交易吗？",
         ["A. 可以，客户书面同意即可", "B. 不可以", "C. 客户是亲属就可以", "D. 收益归客户就可以"],
         "B", "代客理财是明令禁止的，属于典型违规，会被处罚。"),
    ]),
]


def font(size):
    return ImageFont.truetype(str(FONT), size)


def wrap(draw, text, f, maxw):
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=f) <= maxw:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def build():
    d0 = ImageDraw.Draw(Image.new("RGB", (W, 10)))
    f_q = font(30)
    f_o = font(27)
    f_a = font(26)
    f_h = font(30)
    inner = W - PAD * 2 - 44

    # 先算高度
    blocks = []
    for title, qs in SECTIONS:
        blocks.append(("h", title))
        for i, (q, opts, ans, exp) in enumerate(qs, 1):
            ql = wrap(d0, f"{i}. {q}", f_q, inner)
            ol = []
            for o in opts:
                ol += wrap(d0, o, f_o, inner - 20)
            al = wrap(d0, f"答案 {ans}｜{exp}", f_a, inner)
            blocks.append(("q", ql, ol, al))

    h = 250
    for b in blocks:
        if b[0] == "h":
            h += 66
        else:
            h += len(b[1]) * 42 + len(b[2]) * 38 + len(b[3]) * 36 + 40
    h += 90

    img = Image.new("RGB", (W, h), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([PAD, PAD, W - PAD, h - PAD], 18, fill=CARD)

    y = PAD + 44
    d.rectangle([PAD, PAD, W - PAD, PAD + 8], fill=RED)
    d.text((PAD + 36, y), "期货从业 · 题型样例", font=font(40), fill=INK)
    y += 64
    d.text((PAD + 36, y), "每科 100 分，60 分及格｜单科成绩 2 年有效",
           font=font(25), fill=SUB)
    y += 54
    d.line([PAD + 36, y, W - PAD - 36, y], fill=LINE, width=2)
    y += 30

    for b in blocks:
        if b[0] == "h":
            d.text((PAD + 36, y), b[1], font=font(29), fill=BLUE)
            y += 52
            y += 14
            continue
        _, ql, ol, al = b
        for ln in ql:
            d.text((PAD + 36, y), ln, font=f_q, fill=INK)
            y += 42
        y += 4
        for ln in ol:
            d.text((PAD + 56, y), ln, font=f_o, fill=INK)
            y += 38
        y += 8
        for ln in al:
            d.text((PAD + 36, y), ln, font=f_a, fill=GREEN)
            y += 36
        y += 30

    img.save(OUT)
    return OUT


if __name__ == "__main__":
    print(build())
