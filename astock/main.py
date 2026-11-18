#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A 股 / 指数 / ETF 实时行情监控与报警工具（纯标准库，零第三方依赖）

功能一览
--------
* 实时抓取沪深京 A 股、指数、ETF 的行情（腾讯行情为主源，新浪行情为备用源）
* 按自定义周期轮询，自动识别交易时段（开盘 / 午休 / 收盘 / 周末）
* 报警规则：涨跌幅超限、瞬时异动、触及涨停跌停、自定义价格上下限
* 控制台彩色表格 + 滚动日志，报警额外落盘到 logs/alerts.csv
* 报警可推送：邮件（SMTP）、微信（Server酱 / PushPlus）、企业微信、钉钉、自定义 Webhook
* 盘后自动生成当日收盘统计报表，并可选把摘要推送到上述渠道
* 退出（或 --once）时导出行情快照报表，支持 CSV / Excel(.xlsx)
* --demo 用内置模拟行情离线自测，非交易时段也能验证整套逻辑

常用命令
--------
    python main.py                  # 按 config.json 持续监控
    python main.py --once           # 只抓一次，打印 + 导出报表后退出
    python main.py --demo           # 模拟行情，验证报警流程
    python main.py --force          # 忽略交易时段，收盘后也照抓
    python main.py -i 5             # 每 5 秒刷新一次
    python main.py --daily          # 立刻生成当日收盘统计报表（可配推送）
    python main.py --test-notify    # 发一条测试通知，检查邮件/微信配置
    python main.py --no-report      # 不导出报表

作者：为 D:\\python\\B 工作区生成
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import queue
import random
import smtplib
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------------------
# 基础常量
# --------------------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_NAME = "config.json"
VERSION = "1.3.0"
DEFAULT_GROUP = "自选池"        # watchlist 里未指定 group 的标的默认归入这个分组

TENCENT_URL = "https://qt.gtimg.cn/q={codes}"
SINA_URL = "https://hq.sinajs.cn/list={codes}"
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{count},qfq"
TENCENT_MINUTE_KLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={code},{period},,{count}"
MINUTE_PERIODS = ("m5", "m15", "m30", "m60")
SINA_KLINE_URL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                  "CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={count}")
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn",
}
BATCH_SIZE = 60          # 单次请求的代码数量
HTTP_TIMEOUT = 10        # 秒
HTTP_RETRY = 3           # 每个数据源的重试次数

DEFAULT_CONFIG: Dict = {
    "source": "auto",                       # auto / tencent / sina / demo
    "watchlist": [
        {"code": "sh000001", "name": "上证指数"},
        {"code": "sz399001", "name": "深证成指"},
        {"code": "sz399006", "name": "创业板指"},
        {"code": "sh000300", "name": "沪深300"},
        {"code": "sh510300", "name": "沪深300ETF"},
        {"code": "sh600519", "name": "贵州茅台"},
        {"code": "sz300750", "name": "宁德时代"},
    ],
    "poll_interval": 15,                     # 交易时段轮询间隔（秒）
    "off_hours_interval": 60,                # 非交易时段的等待间隔（秒）
    "market_hours_only": True,               # 只在交易时段抓取
    "sessions": [["09:30", "11:30"], ["13:00", "15:00"]],
    "holidays": [],                          # 额外休市日，如 ["2026-10-01"]
    "alert": {
        "change_pct": 3.0,                   # 涨跌幅绝对值超过该值报警（%）
        "rapid_move_pct": 0.8,               # 窗口内瞬时异动报警（%）
        "rapid_move_window": 60,             # 瞬时异动统计窗口（秒）
        "limit_updown": True,                # 触及涨停 / 跌停报警
        "cooldown_seconds": 300,             # 同一标的同一规则的最小报警间隔（秒）
        "alert_on_index": True,              # 指数是否参与报警
    },
    "targets": {                             # 自定义价格上下限，key 为代码，default 为兜底
        "default": {"upper": None, "lower": None},
        "sh600519": {"upper": 1500.0, "lower": 1200.0},
    },
    "report": {
        "auto_export": True,                 # 是否导出报表
        "export_on_exit": True,              # 退出时导出
        "every_minutes": 0,                  # >0 表示每 N 分钟自动导出一次
        "format": "auto",                    # auto / csv / xlsx
        "dir": "reports",
    },
    "log": {"dir": "logs", "level": "INFO"},
    "display": {"color": True, "table": True, "max_rows": 50, "show_summary": True},
    # 常驻窗口版（monitor_gui.py）的设置，控制台版不受影响
    "gui": {
        "interval": 6,                       # 界面刷新间隔（秒）
        "session_only": True,                # 是否只在交易时段刷新
        "window_geometry": "1120x640",       # 窗口大小，程序会记住上次的位置
        "mark_alert_seconds": 60,            # 报警行高亮保持秒数
        "color_by_pnl": True,                # true = 整行按持仓盈亏着色（盈利红/亏损绿），false = 按当日涨跌
        "signal_minutes": 10,                # 技术信号（看涨/看跌）每多少分钟按日线重算一次
    },
    # 报警推送：邮件 + 微信/机器人 + 本机弹窗。全部留空时不影响程序运行，只是不推送。
    "notify": {
        "enabled": True,
        "max_per_hour": 30,                  # 每小时最多推送多少条（防刷屏，0 表示不限）
        "desktop": {
            "enabled": True,                 # 本机弹窗 + 提示音（Windows 桌面右下角）
            "duration": 6,                   # 每条弹窗停留秒数
            "max_popups": 3,                 # 最多同时堆叠几个弹窗
            "sound": True,                   # 是否播放提示音
            "sound_file": "",                # 自定义 wav 文件路径，留空用系统提示音
            "position": "bottom-right",      # bottom-right / top-right
        },
        "email": {
            "enabled": False,
            "smtp_host": "smtp.qq.com",
            "smtp_port": 465,
            "use_ssl": True,
            "starttls": False,
            "username": "",                  # 你的邮箱账号
            "password": "",                  # QQ/163 邮箱填「授权码」，不是登录密码
            "sender": "",                    # 留空则用 username
            "receivers": [],                 # 留空则发给自己
        },
        "push": {
            "enabled": False,
            "provider": "serverchan",        # serverchan / pushplus / wecom / dingtalk / custom
            "token": "",                     # serverchan、pushplus 用
            "webhook": "",                   # wecom、dingtalk、custom 用
        },
    },
    # 盘后自动生成当日收盘统计报表
    "daily": {
        "enabled": True,
        "trigger_time": "15:05",             # 每个交易日到这个点自动生成一次
        "notify": True,                      # 生成后是否推送摘要
    },
}


# --------------------------------------------------------------------------------------
# 小工具：控制台颜色 / 中英文宽度对齐 / 数值格式化
# --------------------------------------------------------------------------------------

class Color:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    GREY = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def enable_windows_ansi() -> bool:
    """在 Windows 控制台开启 ANSI 转义支持，失败则返回 False（退化为无颜色）。"""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def display_width(text: str) -> int:
    """计算字符串在终端中的显示宽度（中日韩全角字符按 2 计）。"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text: str, width: int, align: str = "<") -> str:
    """按显示宽度补空格，保证中英文混排表格对齐。"""
    gap = max(0, width - display_width(text))
    if align == ">":
        return " " * gap + text
    if align == "^":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def safe_float(value) -> Optional[float]:
    """把接口返回的字符串安全转成 float，失败返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if text in ("", "-", "--", "null", "None"):
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def fmt_money(amount_yuan: Optional[float]) -> str:
    """把成交额（元）格式化为易读中文单位。"""
    if amount_yuan is None or amount_yuan <= 0:
        return "-"
    if amount_yuan >= 1e8:
        return f"{amount_yuan / 1e8:.2f}亿"
    if amount_yuan >= 1e4:
        return f"{amount_yuan / 1e4:.2f}万"
    return f"{amount_yuan:.0f}"


def fmt_signed(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:+.{digits}f}"


def fmt_price(value: Optional[float]) -> str:
    """价格格式化：低价（ETF、低价股）保留 3 位小数，其余保留 2 位，避免丢失精度或长短不齐。"""
    if value is None:
        return "-"
    return f"{value:.3f}" if abs(value) < 5 else f"{value:.2f}"


def truncate(text: str, width: int) -> str:
    """按显示宽度截断字符串，超出部分用 .. 代替。"""
    if display_width(text) <= width:
        return text
    out, used = "", 0
    for ch in text:
        char_width = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + char_width > width - 2:
            break
        out += ch
        used += char_width
    return out + ".."


# --------------------------------------------------------------------------------------
# 行情数据结构
# --------------------------------------------------------------------------------------

@dataclass
class Quote:
    """单只标的的行情快照。"""

    code: str
    name: str = ""
    price: Optional[float] = None
    prev_close: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    change: Optional[float] = None
    change_pct: Optional[float] = None
    volume_hand: Optional[float] = None       # 成交量（手）
    amount_yuan: Optional[float] = None       # 成交额（元）
    turnover: Optional[float] = None          # 换手率 %
    pe: Optional[float] = None
    pb: Optional[float] = None
    amplitude: Optional[float] = None         # 振幅 %
    vol_ratio: Optional[float] = None         # 量比
    float_cap: Optional[float] = None         # 流通市值（亿）
    total_cap: Optional[float] = None         # 总市值（亿）
    limit_up: Optional[float] = None
    limit_down: Optional[float] = None
    quote_time: str = ""
    source: str = ""
    is_index: bool = False

    @property
    def direction(self) -> int:
        """1 涨 / -1 跌 / 0 平。"""
        if not self.change_pct:
            return 0
        return 1 if self.change_pct > 0 else (-1 if self.change_pct < 0 else 0)


@dataclass
class KLine:
    """一根日 K 线。"""

    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float = 0.0          # 成交量（手）


def format_minute_time(raw: str) -> str:
    """把分钟线的 202606181030 形式转成 2026-06-18 10:30。"""
    text = str(raw).strip()
    if len(text) >= 12 and text.isdigit():
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}"
    return text


def compute_signal(bars: Sequence[KLine], unit: str = "日") -> Optional[Dict]:
    """由 K 线计算 5 档多空信号（与 GUI/Web 同规则）。"""
    if not bars or len(bars) < 30:
        return None
    closes = [b.close for b in bars]
    n = len(bars)

    def ma(period: int, index: int):
        if index < 0 or index + 1 < period:
            return None
        window = closes[index + 1 - period:index + 1]
        return sum(window) / period

    last = n - 1
    close = closes[last]
    ma5, ma10, ma20 = ma(5, last), ma(10, last), ma(20, last)
    score = 0
    reasons = []

    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            score += 2
            reasons.append(f"多头排列 MA5({ma5:.2f})>MA10({ma10:.2f})>MA20({ma20:.2f})")
        elif ma5 < ma10 < ma20:
            score -= 2
            reasons.append(f"空头排列 MA5({ma5:.2f})<MA10({ma10:.2f})<MA20({ma20:.2f})")
        elif ma5 > ma10:
            score += 1
            reasons.append(f"MA5({ma5:.2f}) 在 MA10({ma10:.2f}) 上方")
        elif ma5 < ma10:
            score -= 1
            reasons.append(f"MA5({ma5:.2f}) 在 MA10({ma10:.2f}) 下方")

        if close > ma20:
            score += 1
            reasons.append(f"收盘价在 MA20({ma20:.2f}) 上方")
        elif close < ma20:
            score -= 1
            reasons.append(f"收盘价在 MA20({ma20:.2f}) 下方")

        for offset in range(1, 4):
            idx = last - offset
            if idx < 20:
                break
            prev5, prev10 = ma(5, idx - 1), ma(10, idx - 1)
            cur5, cur10 = ma(5, idx), ma(10, idx)
            if None in (prev5, prev10, cur5, cur10):
                continue
            if prev5 <= prev10 and cur5 > cur10:
                score += 2
                reasons.append(f"{bars[idx].date} MA5 上穿 MA10（金叉）")
                break
            if prev5 >= prev10 and cur5 < cur10:
                score -= 2
                reasons.append(f"{bars[idx].date} MA5 下穿 MA10（死叉）")
                break

    if n > 5 and closes[last - 5]:
        momentum = (close - closes[last - 5]) / closes[last - 5] * 100
        if momentum >= 3:
            score += 1
        elif momentum <= -3:
            score -= 1
        reasons.append(f"近 5 {unit} {momentum:+.2f}%")

    if n >= 20:
        vol5 = sum(b.volume for b in bars[-5:]) / 5
        vol20 = sum(b.volume for b in bars[-20:]) / 20
        if vol20 > 0:
            ratio = vol5 / vol20
            reasons.append(f"近 5 日量比 {ratio:.2f}")

    if score >= 4:
        label = "看涨"
    elif score >= 2:
        label = "偏多"
    elif score <= -4:
        label = "看跌"
    elif score <= -2:
        label = "偏空"
    else:
        label = "中性"
    return {"score": score, "label": label, "reasons": reasons}


def _logs_dir() -> Path:
    return Path(__file__).resolve().parent / "logs"


def prediction_path(day: str = "") -> Path:
    stamp = day or datetime.now().strftime("%Y%m%d")
    return _logs_dir() / f"prediction_{stamp}.json"


def load_today_prediction(day: str = "") -> Dict:
    """读取当日盘前预测（strong / likely / codes），没有则返回空 dict。"""
    try:
        path = prediction_path((day or datetime.now().strftime("%Y-%m-%d")).replace("-", ""))
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def run_premarket_prediction(codes: Sequence[str], cfg: Dict, notifier=None,
                             reporter=None, dry_run: bool = False) -> Dict:
    """9:30 盘前预测：强看涨（日线+60分共振）+ 预计会涨（日线看涨）。"""
    day = datetime.now().strftime("%Y-%m-%d")
    pool = cfg.get("premarket_pool") or {}
    extra = []
    if isinstance(pool, dict):
        for key in ("etfs", "indexes", "stocks"):
            extra.extend(pool.get(key) or [])
    elif isinstance(pool, list):
        extra = list(pool)
    all_codes = list(dict.fromkeys(list(codes) + extra))
    if reporter:
        reporter.info(f"预测范围：自选 {len(codes)} + 附加 {len(extra)} = {len(all_codes)} 只")

    # 批量拿行情，补全名字（自选外的标的也能显示名称）
    name_map = {w.get("code"): w.get("name") for w in (cfg.get("watchlist") or [])}
    try:
        quotes = TencentFeed().fetch(all_codes)
        for code, q in quotes.items():
            if q.name:
                name_map[code] = q.name
    except Exception:  # noqa: BLE001
        pass

    mood = market_mood()
    idx_ret20 = mood.get("ret20")

    strong = []   # 强看涨：日线 + 60分 都看涨/偏多
    likely = []   # 预计会涨：日线看涨，但 60分不满足

    def _ret20(bars) -> Optional[float]:
        closes = [b.close for b in bars if b.close]
        if len(closes) < 21 or not closes[-21]:
            return None
        return (closes[-1] / closes[-21] - 1) * 100

    for code in all_codes:
        try:
            day_bars = fetch_kline(code, 120, "day")
            min_bars = fetch_kline(code, 120, "m60")
            sig_day = compute_signal(day_bars, "日")
            sig_min = compute_signal(min_bars, "60分")
            if not sig_day:
                continue
            r20 = _ret20(day_bars)
            rs = (r20 - idx_ret20) if (r20 is not None and idx_ret20 is not None) else None
            item = {"code": code, "name": name_map.get(code) or code,
                    "ret20": None if r20 is None else round(r20, 2),
                    "rs": None if rs is None else round(rs, 2)}
            if sig_day["label"] == "看涨":
                if sig_min and sig_min["label"] in ("看涨", "偏多"):
                    strong.append(item)
                else:
                    likely.append(item)
        except Exception:  # noqa: BLE001
            continue

    # --- 相对强弱排序 + 大盘过滤 ---
    weak = mood.get("level") == "偏弱"
    def _sort_key(x):
        return (x.get("rs") if x.get("rs") is not None else -999)
    strong.sort(key=_sort_key, reverse=True)
    likely.sort(key=_sort_key, reverse=True)

    if weak:
        # 弱势日：只留跑赢大盘的，且大幅缩减数量
        strong = [x for x in strong if (x.get("rs") or 0) > 0][:3]
        likely = [x for x in likely if (x.get("rs") or 0) > 0][:2]
    else:
        strong = strong[:6]
        likely = likely[:4]

    if dry_run:
        return {"day": day, "strong": strong, "likely": likely, "mood": mood}

    path = prediction_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "day": day,
            "strong": strong,
            "likely": likely,
            "codes": [b["code"] for b in strong + likely],
            "names": {b["code"]: b["name"] for b in strong + likely},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        if reporter:
            reporter.error(f"保存预测失败：{exc}")

    mood_txt = f"📊 大盘情绪：{mood.get('level')}"
    if mood.get("chg") is not None:
        mood_txt += f"（上证 {mood['chg']:+.2f}%，{'站上' if mood.get('above_ma20') else '跌破'}MA20）"
    lines = [f"🔮 {day} 盘前预测", mood_txt]
    if mood.get("advice"):
        lines.append(f"　{mood['advice']}")
    lines.append("")
    if strong:
        lines.append(f"【强看涨｜日线+60分共振】{len(strong)} 只")
        for b in strong:
            tag = f"　RS {b['rs']:+.2f}%" if b.get("rs") is not None else ""
            lines.append(f"· {b['code']} {b['name']}{tag}")
        lines.append("")
    if likely:
        lines.append(f"【预计会涨｜日线看涨】{len(likely)} 只")
        for b in likely:
            tag = f"　RS {b['rs']:+.2f}%" if b.get("rs") is not None else ""
            lines.append(f"· {b['code']} {b['name']}{tag}")
        lines.append("")
    if not strong and not likely:
        lines.append("今日没有符合条件（日线看涨）的标的")
    if mood.get("level") == "偏弱":
        lines.append("※ 已按相对强弱过滤：只保留跑赢大盘的标的")
    lines.append(f"合计 {len(strong) + len(likely)} 只，收盘后核对")
    title = f"[盘前] {day} 强看涨 {len(strong)} / 预计涨 {len(likely)}"
    if notifier is not None and getattr(notifier, "enabled", False):
        notifier.send(title, "\n".join(lines))
    if reporter is not None:
        reporter.info(f"盘前预测完成：强看涨 {len(strong)}、预计涨 {len(likely)}")


def reload_config_if_changed(cfg: Dict, path: "Path", state: Dict) -> bool:
    """config.json 被改动时热更新（原地更新，保持引用有效）。"""
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return False
    if state.get("mtime") == stamp:
        return False
    try:
        fresh = load_config(path)
    except Exception:  # noqa: BLE001
        return False
    cfg.clear()
    cfg.update(fresh)
    state["mtime"] = stamp
    return True


def market_mood() -> Dict[str, object]:
    """用上证指数判断当日市场情绪（强弱 + 建议）。"""
    try:
        bars = fetch_kline("sh000001", 120, "day")
    except Exception:  # noqa: BLE001
        return {"level": "未知", "chg": None, "advice": "指数数据获取失败"}
    if len(bars) < 25:
        return {"level": "未知", "chg": None, "advice": "指数数据不足"}
    closes = [b.close for b in bars if b.close]
    if len(closes) < 25:
        return {"level": "未知", "chg": None, "advice": "指数数据不足"}
    last, prev = closes[-1], closes[-2]
    chg = (last / prev - 1) * 100 if prev else 0.0
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    above = last >= ma20
    if last > ma5 > ma20:
        level, advice = "偏强", "可以正常参与，注意个股分化"
    elif last < ma20 and last < ma5:
        level, advice = "偏弱", "⚠️ 弱势日追涨风险高，预测仅供参考，建议控制仓位"
    else:
        level, advice = "中性", "震荡格局，建议轻仓试探"
    ret20 = (last / closes[-21] - 1) * 100 if len(closes) > 21 and closes[-21] else None
    return {"level": level, "chg": chg, "above_ma20": above,
            "ret20": ret20, "advice": advice}


REVIEW_HEADER = ["核对日", "预测日", "只数", "上涨数", "命中率%",
                 "平均涨跌%", "上证涨跌%", "超额%"]


def review_prediction(cfg: Dict, notifier=None, reporter=None) -> Optional[Dict]:
    """核对最近一次盘前预测的实际表现，落 CSV 并推送。"""
    logs = _logs_dir()
    today = datetime.now().strftime("%Y%m%d")
    # 收盘后核对「当天」的盘前预测；当天没有文件时回退到最近一次
    files = sorted(p for p in logs.glob("prediction_*.json")
                   if p.stem.split("_", 1)[1] <= today)
    if not files:
        return None
    path = files[-1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    codes = [c for c in (data.get("codes") or []) if c]
    if not codes:
        return None
    pred_day = data.get("day") or path.stem
    try:
        quotes = TencentFeed().fetch(list(dict.fromkeys(list(codes) + ["sh000001"])))
    except Exception:  # noqa: BLE001
        return None

    chgs = [quotes[c].change_pct for c in codes
            if c in quotes and quotes[c].change_pct is not None]
    if not chgs:
        return None
    idx_q = quotes.get("sh000001")
    idx_chg = idx_q.change_pct if idx_q and idx_q.change_pct is not None else None
    total = len(chgs)
    up = sum(1 for v in chgs if v > 0)
    avg = sum(chgs) / total
    excess = (avg - idx_chg) if idx_chg is not None else None
    hit = up / total * 100

    csv_path = logs / "prediction_review.csv"
    is_new = not csv_path.exists()
    already = False
    if not is_new:  # 同一天重复执行时不重复记账
        try:
            with csv_path.open(encoding="utf-8", newline="") as f:
                today_s = datetime.now().strftime("%Y-%m-%d")
                for row in csv.DictReader(f):
                    if row.get("核对日") == today_s and row.get("预测日") == pred_day:
                        already = True
                        break
        except OSError:
            pass
    try:
        with csv_path.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(REVIEW_HEADER)
            if not already:
                w.writerow([
                datetime.now().strftime("%Y-%m-%d"), pred_day, total, up,
                f"{hit:.1f}", f"{avg:+.2f}",
                "" if idx_chg is None else f"{idx_chg:+.2f}",
                    "" if excess is None else f"{excess:+.2f}",
                ])
    except OSError as exc:
        if reporter:
            reporter.error(f"写入预测回顾失败：{exc}")

    # 累计统计
    hist_hits, hist_ex = [], []
    try:
        with csv_path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    hist_hits.append(float(row["命中率%"]))
                    if row.get("超额%"):
                        hist_ex.append(float(row["超额%"]))
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        pass
    n = len(hist_hits)
    avg_hit = sum(hist_hits) / n if n else 0.0
    avg_ex = sum(hist_ex) / len(hist_ex) if hist_ex else None

    lines = [
        f"📋 预测回顾 · {pred_day}",
        f"上涨 {up}/{total}（命中率 {hit:.0f}%）",
        f"平均涨跌 {avg:+.2f}%",
    ]
    if idx_chg is not None:
        lines.append(f"上证 {idx_chg:+.2f}%，超额 {excess:+.2f}%")
    if n:
        tail = f"累计 {n} 次，平均命中率 {avg_hit:.0f}%"
        if avg_ex is not None:
            tail += f"，平均超额 {avg_ex:+.2f}%"
        lines.append(tail)

    if already:
        if reporter:
            reporter.info(f"预测回顾已存在（{pred_day}），跳过重复推送")
    elif notifier is not None and getattr(notifier, "enabled", False):
        notifier.send(f"[预测回顾] {pred_day} 命中 {up}/{total}", "\n".join(lines))
    if reporter is not None:
        reporter.info("预测回顾：" + "；".join(lines[1:]))

    return {"pred_day": pred_day, "total": total, "up": up,
            "hit": hit, "avg": avg, "index": idx_chg, "excess": excess,
            "cum_n": n, "cum_hit": avg_hit}


def fetch_kline(code: str, count: int = 160, period: str = "day") -> List[KLine]:
    """获取 K 线，返回按时间升序的列表。

    period="day" 取日线（前复权，腾讯为主源、新浪为备用）；
    period="m60" / "m30" / "m15" / "m5" 取分钟线（腾讯分钟线接口）。
    """
    count = max(20, min(800, int(count)))
    logger = logging.getLogger("stock_monitor")

    # --- 分钟线 ---
    if period in MINUTE_PERIODS:
        try:
            url = TENCENT_MINUTE_KLINE_URL.format(code=code, period=period, count=count)
            raw = BaseFeed._http_get(url, encoding="utf-8")
            node = ((json.loads(raw).get("data") or {}).get(code)) or {}
            rows = node.get(period) or []
            bars = [
                # 分钟线字段顺序与日线一致：时间(YYYYMMDDHHMM) / 开 / 收 / 高 / 低 / 量(手)
                KLine(format_minute_time(str(row[0])), safe_float(row[1]) or 0.0,
                      safe_float(row[2]) or 0.0, safe_float(row[3]) or 0.0,
                      safe_float(row[4]) or 0.0, safe_float(row[5]) or 0.0)
                for row in rows if len(row) >= 6
            ]
            if bars:
                return bars[-count:]
            logger.warning("分钟 K 线返回空数据（%s %s）", code, period)
        except Exception as exc:
            logger.error("分钟 K 线获取失败（%s %s）：%s", code, period, exc)
        return []

    # --- 日线 ---
    try:
        raw = BaseFeed._http_get(TENCENT_KLINE_URL.format(code=code, count=count), encoding="utf-8")
        node = ((json.loads(raw).get("data") or {}).get(code)) or {}
        rows = node.get("qfqday") or node.get("day") or []
        bars = [
            # 腾讯字段顺序：日期 / 开 / 收 / 高 / 低 / 成交量(手)
            KLine(str(row[0]), safe_float(row[1]) or 0.0, safe_float(row[2]) or 0.0,
                  safe_float(row[3]) or 0.0, safe_float(row[4]) or 0.0, safe_float(row[5]) or 0.0)
            for row in rows if len(row) >= 6
        ]
        if bars:
            return bars[-count:]
    except Exception as exc:
        logger.warning("腾讯 K 线获取失败（%s）：%s", code, exc)

    try:
        raw = BaseFeed._http_get(SINA_KLINE_URL.format(code=code, count=count), encoding="utf-8")
        rows = json.loads(raw)
        bars = [
            # 新浪成交量单位是「股」，这里统一换算成「手」
            KLine(str(row["day"]), safe_float(row["open"]) or 0.0, safe_float(row["close"]) or 0.0,
                  safe_float(row["high"]) or 0.0, safe_float(row["low"]) or 0.0,
                  (safe_float(row["volume"]) or 0.0) / 100)
            for row in rows if isinstance(row, dict) and row.get("day")
        ]
        return bars[-count:]
    except Exception as exc:
        logger.error("K 线获取失败（%s）：%s", code, exc)
        return []


def guess_market_prefix(code: str) -> str:
    """根据 6 位数字代码推断交易所前缀（建议在配置中直接写全前缀）。"""
    code = code.strip()
    if code.startswith(("sh", "sz", "bj")):
        return code
    if not code.isdigit():
        return code
    if code.startswith(("6", "9", "5")):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def is_index_code(code: str) -> bool:
    """粗判是否为指数（沪市 000xxx 指数、深市 399xxx 指数）。"""
    code = code.lower()
    return code.startswith(("sh000", "sz399", "sh950", "sz980"))


def calc_limit_prices(code: str, name: str, prev_close: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    """备用源没有涨跌停字段时，按板块规则估算涨跌停价。"""
    if not prev_close or prev_close <= 0 or is_index_code(code):
        return None, None
    code = code.lower()
    if "ST" in name.upper():
        ratio = 0.05
    elif code.startswith(("sz30", "sh688", "sh689")):
        ratio = 0.20
    elif code.startswith("bj"):
        ratio = 0.30
    else:
        ratio = 0.10
    return round(prev_close * (1 + ratio), 2), round(prev_close * (1 - ratio), 2)


# --------------------------------------------------------------------------------------
# 行情数据源
# --------------------------------------------------------------------------------------

class FeedError(RuntimeError):
    """行情抓取失败。"""


class BaseFeed:
    name = "未知源"

    def fetch(self, codes: Sequence[str]) -> Dict[str, Quote]:
        raise NotImplementedError

    # --- HTTP 辅助 ---
    @staticmethod
    def _http_get(url: str, encoding: str = "gb18030") -> str:
        last_error: Optional[Exception] = None
        for attempt in range(1, HTTP_RETRY + 1):
            try:
                request = urllib.request.Request(url, headers=HTTP_HEADERS)
                with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                    return response.read().decode(encoding, errors="replace")
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last_error = exc
                if attempt < HTTP_RETRY:
                    time.sleep(0.6 * attempt)
        raise FeedError(f"请求失败：{last_error}")

    @staticmethod
    def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
        for i in range(0, len(items), size):
            yield items[i:i + size]


class TencentFeed(BaseFeed):
    """腾讯行情接口：字段齐全（含涨跌停价、市值、量比）。"""

    name = "腾讯行情"

    def fetch(self, codes: Sequence[str]) -> Dict[str, Quote]:
        result: Dict[str, Quote] = {}
        for chunk in self._chunks(list(codes), BATCH_SIZE):
            text = self._http_get(TENCENT_URL.format(codes=",".join(chunk)))
            for line in text.split(";"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                key, _, body = line.partition("=")
                fields = body.strip().strip('"').split("~")
                if len(fields) < 40:
                    continue
                code = key.split("_", 1)[-1].strip().lower()
                quote = self._parse(code, fields)
                if quote is not None:
                    result[quote.code] = quote
        if not result:
            raise FeedError("腾讯行情未返回任何有效数据")
        return result

    @staticmethod
    def _parse(code: str, f: Sequence[str]) -> Optional[Quote]:
        price = safe_float(f[3])
        if price is None:
            return None
        amount_wan = safe_float(f[37]) if len(f) > 37 else None
        quote = Quote(
            code=code,
            name=f[1].strip(),
            price=price,
            prev_close=safe_float(f[4]),
            open=safe_float(f[5]),
            high=safe_float(f[33]) if len(f) > 33 else None,
            low=safe_float(f[34]) if len(f) > 34 else None,
            change=safe_float(f[31]) if len(f) > 31 else None,
            change_pct=safe_float(f[32]) if len(f) > 32 else None,
            volume_hand=safe_float(f[6]),
            amount_yuan=amount_wan * 1e4 if amount_wan else None,
            turnover=safe_float(f[38]) if len(f) > 38 else None,
            pe=safe_float(f[39]) if len(f) > 39 else None,
            amplitude=safe_float(f[43]) if len(f) > 43 else None,
            float_cap=safe_float(f[44]) if len(f) > 44 else None,
            total_cap=safe_float(f[45]) if len(f) > 45 else None,
            pb=safe_float(f[46]) if len(f) > 46 else None,
            limit_up=safe_float(f[47]) if len(f) > 47 else None,
            limit_down=safe_float(f[48]) if len(f) > 48 else None,
            vol_ratio=safe_float(f[49]) if len(f) > 49 else None,
            quote_time=TencentFeed._parse_time(f[30] if len(f) > 30 else ""),
            source=TencentFeed.name,
            is_index=is_index_code(code),
        )
        # 指数返回 -1 表示不适用
        if quote.limit_up is not None and quote.limit_up <= 0:
            quote.limit_up = None
        if quote.limit_down is not None and quote.limit_down <= 0:
            quote.limit_down = None
        # 接口未给涨跌时自行推算，保证报警逻辑一致
        if quote.change is None and quote.prev_close:
            quote.change = quote.price - quote.prev_close
        if quote.change_pct is None and quote.prev_close:
            quote.change_pct = (quote.price - quote.prev_close) / quote.prev_close * 100
        return quote

    @staticmethod
    def _parse_time(raw: str) -> str:
        raw = raw.strip()
        if len(raw) >= 14 and raw.isdigit():
            try:
                return datetime.strptime(raw[:14], "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                return raw
        return raw


class SinaFeed(BaseFeed):
    """新浪行情接口（备用）：不含涨跌停价与市值，按板块规则估算。"""

    name = "新浪行情"

    def fetch(self, codes: Sequence[str]) -> Dict[str, Quote]:
        result: Dict[str, Quote] = {}
        for chunk in self._chunks(list(codes), BATCH_SIZE):
            text = self._http_get(SINA_URL.format(codes=",".join(chunk)))
            for line in text.split(";"):
                line = line.strip()
                if "=" not in line or "hq_str_" not in line:
                    continue
                key, _, body = line.partition("=")
                code = key.split("hq_str_", 1)[-1].strip().lower()
                fields = body.strip().strip('"').split(",")
                if len(fields) < 32 or not fields[0]:
                    continue
                price = safe_float(fields[3])
                prev_close = safe_float(fields[2])
                if price is None or not price:
                    continue
                amount = safe_float(fields[9])
                limit_up, limit_down = calc_limit_prices(code, fields[0], prev_close)
                quote = Quote(
                    code=code,
                    name=fields[0].strip(),
                    price=price,
                    prev_close=prev_close,
                    open=safe_float(fields[1]),
                    high=safe_float(fields[4]),
                    low=safe_float(fields[5]),
                    volume_hand=(safe_float(fields[8]) or 0) / 100,
                    amount_yuan=amount,
                    limit_up=limit_up,
                    limit_down=limit_down,
                    quote_time=f"{fields[30].strip()} {fields[31].strip()}",
                    source=SinaFeed.name,
                    is_index=is_index_code(code),
                )
                if prev_close:
                    quote.change = price - prev_close
                    quote.change_pct = quote.change / prev_close * 100
                result[quote.code] = quote
        if not result:
            raise FeedError("新浪行情未返回任何有效数据")
        return result


class DemoFeed(BaseFeed):
    """内置模拟行情：随机游走，用于离线自测与演示报警。"""

    name = "模拟行情"

    def __init__(self, names: Dict[str, str], targets: Optional[Dict] = None, seed: Optional[int] = None):
        self._random = random.Random(seed)
        self._names = dict(names)
        self._base: Dict[str, float] = {}
        for code in names:
            spec = (targets or {}).get(code) or {}
            upper, lower = safe_float(spec.get("upper")), safe_float(spec.get("lower"))
            if upper and lower and upper > lower:
                # 有自定义价格区间时，模拟价围绕区间中值波动，避免演示中误触发价格限额
                self._base[code] = (upper + lower) / 2
            else:
                self._base[code] = self._random.uniform(8, 180)
        self._prev_close = {code: price * self._random.uniform(0.97, 1.03) for code, price in self._base.items()}
        self._drift = {code: 0.0 for code in names}

    def fetch(self, codes: Sequence[str]) -> Dict[str, Quote]:
        now = datetime.now()
        result: Dict[str, Quote] = {}
        for code in codes:
            prev_close = self._prev_close.setdefault(code, 100.0)
            price = self._base.setdefault(code, prev_close)
            # 让走势带一点惯性，更容易触发异动规则；用 get 兼容运行中新加入的代码
            drift = self._drift.get(code, 0.0) * 0.7 + self._random.gauss(0, 0.0035)
            self._drift[code] = drift
            price = max(0.5, price * (1 + drift))
            self._base[code] = price
            high = max(price, prev_close * 1.01)
            low = min(price, prev_close * 0.99)
            quote = Quote(
                code=code,
                name=self._names.get(code, code),
                price=round(price, 2),
                prev_close=round(prev_close, 2),
                open=round(prev_close * (1 + self._random.gauss(0, 0.004)), 2),
                high=round(high, 2),
                low=round(low, 2),
                volume_hand=round(self._random.uniform(1e4, 9e5)),
                amount_yuan=self._random.uniform(1e8, 9e9),
                turnover=round(self._random.uniform(0.2, 9.0), 2),
                amplitude=round((high - low) / prev_close * 100, 2),
                vol_ratio=round(self._random.uniform(0.5, 3.0), 2),
                float_cap=round(self._random.uniform(50, 2000), 2),
                total_cap=round(self._random.uniform(50, 3000), 2),
                quote_time=now.strftime("%Y-%m-%d %H:%M:%S"),
                source=self.name,
                is_index=is_index_code(code),
            )
            limit_up, limit_down = calc_limit_prices(code, quote.name, prev_close)
            quote.limit_up, quote.limit_down = limit_up, limit_down
            quote.change = round(price - prev_close, 2)
            quote.change_pct = round(quote.change / prev_close * 100, 2) if prev_close else 0.0
            result[code] = quote
        return result


def build_feed(source: str, watchlist: Sequence[Dict], cfg: Optional[Dict] = None) -> BaseFeed:
    names = {item["code"]: item.get("name", item["code"]) for item in watchlist}
    if source == "demo":
        return DemoFeed(names, (cfg or {}).get("targets"))
    if source == "tencent":
        return TencentFeed()
    if source == "sina":
        return SinaFeed()
    return TencentFeed()  # auto 模式下以腾讯为主源


# --------------------------------------------------------------------------------------
# 交易时段判定
# --------------------------------------------------------------------------------------

def is_trading_day(cfg: Dict, now: Optional[datetime] = None) -> bool:
    """是否交易日（排除周末与配置的节假日）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    holidays = set(str(d) for d in (cfg.get("holidays") or []))
    return now.strftime("%Y-%m-%d") not in holidays


def market_status(cfg: Dict, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """返回 (是否处于交易时段, 状态说明)。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False, "周末休市"
    holidays = set(str(d) for d in (cfg.get("holidays") or []))
    if now.strftime("%Y-%m-%d") in holidays:
        return False, "节假日休市"

    sessions: List[Tuple[str, str]] = []
    for item in cfg.get("sessions") or DEFAULT_CONFIG["sessions"]:
        try:
            sessions.append((str(item[0]), str(item[1])))
        except (TypeError, IndexError, KeyError):
            continue
    if not sessions:
        sessions = [("09:30", "11:30"), ("13:00", "15:00")]

    current = now.strftime("%H:%M")
    for start, end in sessions:
        if start <= current <= end:
            return True, "交易中"
    if current < sessions[0][0]:
        return False, f"待开盘（{sessions[0][0]} 开盘）"
    if current > sessions[-1][1]:
        return False, "已收盘"
    return False, "午间休市"


# --------------------------------------------------------------------------------------
# 报警规则引擎
# --------------------------------------------------------------------------------------

@dataclass
class Alert:
    timestamp: str
    code: str
    name: str
    price: Optional[float]
    change_pct: Optional[float]
    rule: str
    message: str

    def as_row(self) -> List[str]:
        return [
            self.timestamp,
            self.code,
            self.name,
            "" if self.price is None else f"{self.price:.3f}".rstrip("0").rstrip("."),
            "" if self.change_pct is None else f"{self.change_pct:+.2f}%",
            self.rule,
            self.message,
        ]


class RuleEngine:
    """把行情快照喂进来，返回本轮新产生的报警。"""

    def __init__(self, cfg: Dict):
        self.cfg = cfg
        alert_cfg = cfg.get("alert") or {}
        self.change_pct_limit = safe_float(alert_cfg.get("change_pct")) or 0.0
        self.rapid_move_pct = safe_float(alert_cfg.get("rapid_move_pct")) or 0.0
        self.rapid_window = int(safe_float(alert_cfg.get("rapid_move_window")) or 60)
        self.limit_updown = bool(alert_cfg.get("limit_updown", True))
        self.cooldown = int(safe_float(alert_cfg.get("cooldown_seconds")) or 300)
        self.alert_on_index = bool(alert_cfg.get("alert_on_index", True))
        self.targets = cfg.get("targets") or {}
        self.per_item = {
            item["code"]: item for item in cfg.get("watchlist", []) if isinstance(item, dict)
        }
        self._history: Dict[str, deque] = defaultdict(deque)
        self._last_alert: Dict[str, float] = {}     # 按股票代码冷却（同一股票冷却期内只报一次）

    # --- 阈值合并：单标的配置覆盖全局 ---
    def _thresholds(self, code: str) -> Dict[str, Optional[float]]:
        merged = {"upper": None, "lower": None, "change_pct": self.change_pct_limit}
        for source in (self.targets.get("default"), self.targets.get(code), self.per_item.get(code)):
            if not isinstance(source, dict):
                continue
            for key in ("upper", "lower", "change_pct"):
                if key in source and source[key] is not None:
                    merged[key] = safe_float(source[key])
        return merged

    def evaluate(self, quotes: Dict[str, Quote], now: Optional[datetime] = None) -> List[Alert]:
        now = now or datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        epoch = now.timestamp()
        fired: List[Alert] = []
        triggered_keys = set()

        for code, quote in quotes.items():
            if quote.price is None:
                continue
            if quote.is_index and not self.alert_on_index:
                continue
            thresholds = self._thresholds(code)

            # 1) 自定义价格上下限
            upper, lower = thresholds.get("upper"), thresholds.get("lower")
            if upper is not None and quote.price >= upper:
                fired.append(self._make(stamp, quote, "价格上限",
                                        f"现价 {quote.price:.3f} 触及/突破上限 {upper:.3f}"))
            if lower is not None and quote.price <= lower:
                fired.append(self._make(stamp, quote, "价格下限",
                                        f"现价 {quote.price:.3f} 触及/跌破下限 {lower:.3f}"))

            # 2) 涨跌幅超限
            limit = thresholds.get("change_pct")
            if limit and quote.change_pct is not None and abs(quote.change_pct) >= limit:
                direction = "涨幅" if quote.change_pct > 0 else "跌幅"
                fired.append(self._make(stamp, quote, "涨跌幅超限",
                                        f"{direction} {quote.change_pct:+.2f}% 超过阈值 {limit:.2f}%"))

            # 3) 涨停 / 跌停
            if self.limit_updown:
                if quote.limit_up and quote.price >= quote.limit_up - 1e-6:
                    fired.append(self._make(stamp, quote, "涨停", f"现价 {quote.price:.3f} 触及涨停价 {quote.limit_up:.3f}"))
                elif quote.limit_down and quote.price <= quote.limit_down + 1e-6:
                    fired.append(self._make(stamp, quote, "跌停", f"现价 {quote.price:.3f} 触及跌停价 {quote.limit_down:.3f}"))

            # 4) 瞬时异动（窗口内相对变动）
            history = self._history[code]
            history.append((epoch, quote.price))
            while history and epoch - history[0][0] > self.rapid_window:
                history.popleft()
            if self.rapid_move_pct and len(history) >= 2:
                base_price = history[0][1]
                if base_price:
                    move = (quote.price - base_price) / base_price * 100
                    if abs(move) >= self.rapid_move_pct:
                        fired.append(self._make(
                            stamp, quote, "瞬时异动",
                            f"{self.rapid_window} 秒内变动 {move:+.2f}%（{base_price:.3f} → {quote.price:.3f}）",
                        ))

        # 冷却过滤：按「股票」级别冷却（同一股票在冷却期内只报一次，跨规则共享）
        result: List[Alert] = []
        for alert in fired:
            key = alert.code
            triggered_keys.add(key)
            last = self._last_alert.get(key)
            if last is not None and epoch - last < self.cooldown:
                continue
            self._last_alert[key] = epoch
            result.append(alert)

        # 只清理已过期的冷却记录（不再因为"条件回落"就立刻重置，避免反复报警）
        for key in list(self._last_alert):
            if epoch - self._last_alert[key] > self.cooldown * 3:
                self._last_alert.pop(key, None)
        return result

    @staticmethod
    def _make(stamp: str, quote: Quote, rule: str, message: str) -> Alert:
        return Alert(stamp, quote.code, quote.name, quote.price, quote.change_pct, rule, message)


# --------------------------------------------------------------------------------------
# 输出：控制台表格、日志、报警文件
# --------------------------------------------------------------------------------------

class Reporter:
    """统一负责控制台输出、日志文件、报警 CSV 与快照报表。"""

    # 表格列：代码 / 名称 / 现价 / 涨跌 / 涨跌幅 / 最高 / 最低 / 成交额 / 换手率 / 量比 / 行情时间
    COLUMN_TITLES = ["代码", "名称", "现价", "涨跌", "涨跌幅", "最高", "最低",
                     "成交额", "换手率", "量比", "行情时间"]
    COLUMN_WIDTHS = [10, 16, 10, 10, 9, 10, 10, 11, 8, 6, 21]
    COLUMN_ALIGNS = ["<", "<", ">", ">", ">", ">", ">", ">", ">", ">", "<"]
    NAME_MIN_WIDTH = 10
    NAME_MAX_WIDTH = 26

    def __init__(self, cfg: Dict, use_color: bool, quiet: bool = False):
        self.cfg = cfg
        self.quiet = quiet
        display_cfg = cfg.get("display") or {}
        self.use_color = bool(use_color and display_cfg.get("color", True))
        self.show_table = bool(display_cfg.get("table", True))
        self.max_rows = int(display_cfg.get("max_rows", 50) or 50)
        self.show_summary = bool(display_cfg.get("show_summary", True))

        log_cfg = cfg.get("log") or {}
        self.log_dir = self._resolve(log_cfg.get("dir", "logs"))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        report_cfg = cfg.get("report") or {}
        self.report_dir = self._resolve(report_cfg.get("dir", "reports"))
        self.report_dir.mkdir(parents=True, exist_ok=True)

        self.logger = logging.getLogger("stock_monitor")
        self.logger.setLevel(getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO))
        self.logger.handlers.clear()
        log_file = self.log_dir / f"monitor_{datetime.now():%Y%m%d}.log"
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self.logger.addHandler(handler)
        self.log_file = log_file

        self.alerts_file = self.log_dir / "alerts.csv"
        self.session_alerts: List[Alert] = []
        self._ensure_alerts_header()

    @staticmethod
    def _resolve(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else BASE_DIR / path

    def _ensure_alerts_header(self) -> None:
        if not self.alerts_file.exists():
            with self.alerts_file.open("w", newline="", encoding="utf-8-sig") as fh:
                csv.writer(fh).writerow(["时间", "代码", "名称", "现价", "涨跌幅", "规则", "说明"])

    def paint(self, text: str, direction: int) -> str:
        if not self.use_color:
            return text
        color = Color.RED if direction > 0 else (Color.GREEN if direction < 0 else Color.GREY)
        return f"{color}{text}{Color.RESET}"

    # --- 控制台 ---
    def print_banner(self, cfg: Dict, feed: BaseFeed, watchlist: Sequence[Dict], args: argparse.Namespace) -> None:
        if self.quiet:
            return
        line = "=" * 78
        print(line)
        print(f"  A 股行情监控 v{VERSION}   数据源：{feed.name}   标的：{len(watchlist)} 个")
        print(f"  轮询间隔：{cfg['poll_interval']}s（非交易时段 {cfg['off_hours_interval']}s）   "
              f"交易时段限制：{'开启' if cfg['market_hours_only'] and not args.force else '关闭'}")
        alert_cfg = cfg.get("alert") or {}
        print(f"  报警规则：涨跌幅≥{alert_cfg.get('change_pct')}% | "
              f"异动≥{alert_cfg.get('rapid_move_pct')}%/{alert_cfg.get('rapid_move_window')}s | "
              f"涨跌停 {'开' if alert_cfg.get('limit_updown', True) else '关'} | "
              f"冷却 {alert_cfg.get('cooldown_seconds')}s")
        print(f"  日志：{self.log_file}")
        print(f"  报警记录：{self.alerts_file}")
        if args.demo:
            print("  >>> 演示模式：使用内置模拟行情，不访问网络 <<<")
        print(line)

    def print_table(self, quotes: Dict[str, Quote], round_no: int, status: str) -> None:
        if self.quiet:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self.use_color:
            print(f"\n{Color.CYAN}{Color.BOLD}── 第 {round_no} 轮  {stamp}  [{status}] ──{Color.RESET}")
        else:
            print(f"\n-- 第 {round_no} 轮  {stamp}  [{status}] --")

        if not self.show_table:
            return

        widths = list(self.COLUMN_WIDTHS)
        # 名称列按本轮实际名称长度自适应，避免长名称（如 ETF 全称）挤乱表格
        name_width = self.NAME_MIN_WIDTH
        for quote in quotes.values():
            name_width = max(name_width, display_width(quote.name))
        widths[1] = min(self.NAME_MAX_WIDTH, name_width)

        header = " ".join(
            pad(title, width, align)
            for title, width, align in zip(self.COLUMN_TITLES, widths, self.COLUMN_ALIGNS)
        )
        print(self.paint(header, 0) if self.use_color else header)
        print("-" * display_width(header))

        items = sorted(quotes.values(), key=lambda q: (q.change_pct is None, -(q.change_pct or 0)))
        for quote in items[:self.max_rows]:
            cells = [
                pad(quote.code, widths[0]),
                pad(truncate(quote.name, widths[1]), widths[1]),
                pad(fmt_price(quote.price), widths[2], ">"),
                pad(fmt_signed(quote.change), widths[3], ">"),
                pad("-" if quote.change_pct is None else f"{quote.change_pct:+.2f}%", widths[4], ">"),
                pad(fmt_price(quote.high), widths[5], ">"),
                pad(fmt_price(quote.low), widths[6], ">"),
                pad(fmt_money(quote.amount_yuan), widths[7], ">"),
                pad("-" if quote.turnover is None else f"{quote.turnover:.2f}%", widths[8], ">"),
                pad("-" if quote.vol_ratio is None else f"{quote.vol_ratio:.2f}", widths[9], ">"),
                pad(quote.quote_time or "-", widths[10]),
            ]
            row = " ".join(cells)
            print(self.paint(row, quote.direction))
        if len(items) > self.max_rows:
            print(f"  ... 其余 {len(items) - self.max_rows} 个标的已省略（可在 config.json 调整 display.max_rows）")

        if self.show_summary:
            print(self.summarize(quotes))

    def summarize(self, quotes: Dict[str, Quote]) -> str:
        values = [q for q in quotes.values() if q.change_pct is not None]
        if not values:
            return ""
        up = sum(1 for q in values if q.change_pct > 0)
        down = sum(1 for q in values if q.change_pct < 0)
        flat = len(values) - up - down
        avg = sum(q.change_pct for q in values) / len(values)
        best = max(values, key=lambda q: q.change_pct)
        worst = min(values, key=lambda q: q.change_pct)
        return (f"  涨 {up} / 跌 {down} / 平 {flat}    均值 {avg:+.2f}%    "
                f"最强 {best.name} {best.change_pct:+.2f}%    最弱 {worst.name} {worst.change_pct:+.2f}%")

    # --- 报警 ---
    def emit_alerts(self, alerts: Sequence[Alert]) -> None:
        if not alerts:
            return
        # 表头已由 _ensure_alerts_header 用带 BOM 的编码写过一次，
        # 这里追加必须用无 BOM 编码，否则每次追加都会在文件中插入 BOM 字符
        first = not self.alerts_file.exists()
        with self.alerts_file.open("w" if first else "a", newline="",
                                   encoding="utf-8-sig" if first else "utf-8") as fh:
            if first:
                csv.writer(fh).writerow(["时间", "代码", "名称", "现价", "涨跌幅", "规则", "说明"])
            csv.writer(fh).writerows(alert.as_row() for alert in alerts)
        for alert in alerts:
            text = (f"[报警] {alert.timestamp}  {alert.code} {alert.name}  "
                    f"现价 {alert.price if alert.price is not None else '-'}  "
                    f"{alert.rule}：{alert.message}")
            if not self.quiet:
                colored = text if not self.use_color else f"{Color.YELLOW}{Color.BOLD}{text}{Color.RESET}"
                print(f"  {colored}")
            self.logger.warning(text)
            self.session_alerts.append(alert)
            try:
                import sys as _s
                _s.path.insert(0, "/opt/astock")
                from db_log import log_alert as _la
                _la("stock", str(alert.code or ""), str(alert.rule or ""),
                    f"{alert.name} {alert.message}"[:200])
            except Exception:
                pass

    def info(self, message: str) -> None:
        self.logger.info(message)
        if not self.quiet:
            print(f"  {message}")

    def error(self, message: str) -> None:
        self.logger.error(message)
        print(f"  [错误] {message}")

    # --- 报表 ---
    def export_report(self, quotes: Dict[str, Quote], reason: str = "") -> Optional[Path]:
        report_cfg = self.cfg.get("report") or {}
        if not report_cfg.get("auto_export", True):
            return None
        fmt = str(report_cfg.get("format", "auto")).lower()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        written: Optional[Path] = None

        if fmt in ("auto", "xlsx"):
            path = self._export_xlsx(quotes, stamp)
            if path is not None:
                written = path
            elif fmt == "xlsx":
                self.error("未安装 openpyxl，无法导出 Excel，请改用 format=csv")
        if written is None and fmt in ("auto", "csv"):
            written = self._export_csv(quotes, stamp)

        if written is not None:
            self.info(f"已导出报表（{reason or '快照'}）：{written}")
        return written

    def _export_xlsx(self, quotes: Dict[str, Quote], stamp: str) -> Optional[Path]:
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError:
            return None

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "行情快照"
        headers = ["代码", "名称", "现价", "涨跌", "涨跌幅%", "今开", "昨收", "最高", "最低",
                   "成交量(手)", "成交额(元)", "换手率%", "量比", "振幅%", "市盈率", "市净率",
                   "流通市值(亿)", "总市值(亿)", "涨停价", "跌停价", "行情时间", "数据源"]
        sheet.append(headers)
        head_font = Font(bold=True, color="FFFFFF")
        head_fill = PatternFill("solid", fgColor="4472C4")
        for cell in sheet[1]:
            cell.font = head_font
            cell.fill = head_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")

        for quote in quotes.values():
            sheet.append([
                quote.code, quote.name, quote.price, quote.change, quote.change_pct,
                quote.open, quote.prev_close, quote.high, quote.low, quote.volume_hand,
                quote.amount_yuan, quote.turnover, quote.vol_ratio, quote.amplitude,
                quote.pe, quote.pb, quote.float_cap, quote.total_cap,
                quote.limit_up, quote.limit_down, quote.quote_time, quote.source,
            ])
            row = sheet.max_row
            color = "C00000" if quote.direction > 0 else ("008000" if quote.direction < 0 else "808080")
            for col in (3, 4, 5):
                sheet.cell(row=row, column=col).font = Font(color=color, bold=True)
        widths = [10, 16, 10, 9, 10, 9, 9, 9, 9, 13, 16, 10, 8, 9, 9, 8, 13, 13, 9, 9, 20, 11]
        for index, width in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        sheet.freeze_panes = "A2"

        if self.session_alerts:
            sheet2 = workbook.create_sheet("本次报警记录")
            sheet2.append(["时间", "代码", "名称", "现价", "涨跌幅", "规则", "说明"])
            for cell in sheet2[1]:
                cell.font = head_font
                cell.fill = head_fill
            for alert in self.session_alerts:
                sheet2.append(alert.as_row())
            for index, width in enumerate([21, 10, 16, 10, 10, 12, 52], start=1):
                sheet2.column_dimensions[get_column_letter(index)].width = width
            sheet2.freeze_panes = "A2"

        path = self.report_dir / f"quotes_{stamp}.xlsx"
        workbook.save(path)
        return path

    def _export_csv(self, quotes: Dict[str, Quote], stamp: str) -> Path:
        path = self.report_dir / f"quotes_{stamp}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["代码", "名称", "现价", "涨跌", "涨跌幅%", "今开", "昨收", "最高", "最低",
                             "成交量(手)", "成交额(元)", "换手率%", "量比", "振幅%", "流通市值(亿)",
                             "总市值(亿)", "涨停价", "跌停价", "行情时间", "数据源"])
            for quote in quotes.values():
                writer.writerow([
                    quote.code, quote.name, quote.price, quote.change, quote.change_pct,
                    quote.open, quote.prev_close, quote.high, quote.low, quote.volume_hand,
                    quote.amount_yuan, quote.turnover, quote.vol_ratio, quote.amplitude,
                    quote.float_cap, quote.total_cap, quote.limit_up, quote.limit_down,
                    quote.quote_time, quote.source,
                ])
        return path

    # --- 盘后收盘统计报表 ---
    DAILY_HEADERS = ["代码", "名称", "收盘价", "涨跌", "涨跌幅%", "今开", "最高", "最低",
                     "振幅%", "成交额(元)", "换手率%", "成交量(手)", "涨停价", "跌停价", "行情时间"]

    def read_today_alerts(self) -> List[List[str]]:
        """从 alerts.csv 中读取当天全部报警（跨进程，重启程序也不会丢）。"""
        if not self.alerts_file.exists():
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        rows: List[List[str]] = []
        try:
            with self.alerts_file.open("r", encoding="utf-8-sig", newline="") as fh:
                reader = csv.reader(fh)
                next(reader, None)
                rows = [row for row in reader if row and row[0].startswith(today)]
        except OSError as exc:
            self.logger.error("读取报警记录失败：%s", exc)
        return rows

    @staticmethod
    def _daily_rows(quotes: Dict[str, Quote]) -> List[List]:
        return [
            [q.code, q.name, q.price, q.change, q.change_pct, q.open, q.high, q.low,
             q.amplitude, q.amount_yuan, q.turnover, q.volume_hand, q.limit_up, q.limit_down,
             q.quote_time]
            for q in sorted(quotes.values(), key=lambda q: (q.change_pct is None, -(q.change_pct or 0)))
        ]

    @staticmethod
    def _daily_footer(quotes: Dict[str, Quote]) -> List:
        values = [q for q in quotes.values() if q.change_pct is not None]
        up = sum(1 for q in values if q.change_pct > 0)
        down = sum(1 for q in values if q.change_pct < 0)
        average = round(sum(q.change_pct for q in values) / len(values), 2) if values else 0.0
        return ["汇总", f"涨{up}/跌{down}/平{len(values) - up - down}", None, None, average]

    def export_daily(self, quotes: Dict[str, Quote], notifier: Optional["Notifier"] = None,
                     reason: str = "收盘统计") -> Optional[Path]:
        """生成当日收盘统计报表（Excel/CSV），并可选推送摘要。"""
        daily_cfg = self.cfg.get("daily") or {}
        if not daily_cfg.get("enabled", True) or not quotes:
            return None
        alert_rows = self.read_today_alerts()
        title, body = build_daily_summary(quotes, alert_rows, self.cfg)
        day = datetime.now().strftime("%Y%m%d")
        fmt = str((self.cfg.get("report") or {}).get("format", "auto")).lower()

        path: Optional[Path] = None
        if fmt in ("auto", "xlsx"):
            path = self._daily_xlsx(quotes, alert_rows, day)
        if path is None and fmt in ("auto", "csv"):
            path = self._daily_csv(quotes, alert_rows, day)
        if path is not None:
            self.info(f"已生成{reason}报表：{path}（当日报警 {len(alert_rows)} 条）")
        if daily_cfg.get("notify", True) and notifier is not None and notifier.enabled:
            notifier.send(title, body)
        # 同步发一份到个人微信（通过微信插件的 CLI，失败不影响主流程）
        if daily_cfg.get("notify_wechat", True):
            ok = send_to_wechat(body)
            self.info("收盘日报已发个人微信" if ok else "收盘日报发个人微信失败（忽略）")
        return path

    def _daily_xlsx(self, quotes: Dict[str, Quote], alert_rows: Sequence[Sequence[str]],
                    day: str) -> Optional[Path]:
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError:
            return None

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "收盘统计"
        sheet.append(self.DAILY_HEADERS)
        head_font = Font(bold=True, color="FFFFFF")
        head_fill = PatternFill("solid", fgColor="4472C4")
        for cell in sheet[1]:
            cell.font = head_font
            cell.fill = head_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")

        for row in self._daily_rows(quotes):
            sheet.append(row)
            index = sheet.max_row
            direction = (row[4] or 0)
            color = "C00000" if direction > 0 else ("008000" if direction < 0 else "808080")
            for column in (3, 4, 5):
                sheet.cell(row=index, column=column).font = Font(color=color, bold=True)
        sheet.append(self._daily_footer(quotes))
        for cell in sheet[sheet.max_row]:
            cell.font = Font(bold=True)

        widths = [10, 16, 11, 9, 10, 9, 9, 9, 9, 16, 10, 12, 9, 9, 20]
        for index, width in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        sheet.freeze_panes = "A2"

        sheet2 = workbook.create_sheet("当日报警")
        sheet2.append(["时间", "代码", "名称", "现价", "涨跌幅", "规则", "说明"])
        for cell in sheet2[1]:
            cell.font = head_font
            cell.fill = head_fill
        for row in alert_rows:
            sheet2.append(list(row))
        for index, width in enumerate([21, 10, 16, 10, 10, 12, 52], start=1):
            sheet2.column_dimensions[get_column_letter(index)].width = width
        sheet2.freeze_panes = "A2"

        path = self.report_dir / f"daily_{day}.xlsx"
        workbook.save(path)
        return path

    def _daily_csv(self, quotes: Dict[str, Quote], alert_rows: Sequence[Sequence[str]],
                   day: str) -> Path:
        path = self.report_dir / f"daily_{day}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(self.DAILY_HEADERS)
            writer.writerows(self._daily_rows(quotes))
            writer.writerow(self._daily_footer(quotes))
            writer.writerow([])
            writer.writerow(["当日报警记录"])
            writer.writerow(["时间", "代码", "名称", "现价", "涨跌幅", "规则", "说明"])
            writer.writerows(alert_rows)
        return path


# --------------------------------------------------------------------------------------
# 本机桌面弹窗提醒
# --------------------------------------------------------------------------------------

class DesktopNotifier:
    """在 Windows 桌面角落弹出置顶小窗口 + 提示音，用于报警的本机即时提醒。

    tkinter 只在独立线程里创建和使用，主线程通过 queue 投递消息，
    因此弹窗不会阻塞行情抓取循环；没有图形界面时该渠道自动失效，不影响其它功能。
    """

    def __init__(self, cfg: Dict, logger: logging.Logger, quiet: bool = False):
        self.cfg = cfg or {}
        self.logger = logger
        self.quiet = quiet
        self.duration = max(1, int(safe_float(self.cfg.get("duration")) or 6))
        self.max_popups = max(1, int(safe_float(self.cfg.get("max_popups")) or 3))
        self.sound = bool(self.cfg.get("sound", True))
        self.sound_file = str(self.cfg.get("sound_file") or "")
        self.position = str(self.cfg.get("position") or "bottom-right").lower()
        self._queue: Optional[queue.Queue] = None
        self._root = None
        self._available = False
        self.shown = 0                      # 已实际显示的弹窗数量（供自检判断）
        self.pending = 0                    # 已投递但尚未显示的弹窗数量
        self._ready = threading.Event()
        if self.cfg.get("enabled"):
            self._start()

    @property
    def enabled(self) -> bool:
        return self._available

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        text = str(text)
        return text if len(text) <= limit else text[:limit].rstrip() + " …"

    def _start(self) -> None:
        try:
            import tkinter  # noqa: F401  仅检测 tkinter 是否可用
        except Exception as exc:
            self.logger.error("桌面弹窗不可用（tkinter 缺失）：%s", exc)
            return
        self._queue = queue.Queue()
        threading.Thread(target=self._run, name="desktop-notifier", daemon=True).start()
        if not self._ready.wait(timeout=8):
            self.logger.error("桌面弹窗初始化超时，已跳过该渠道")

    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception as exc:
            self.logger.error("桌面弹窗不可用：%s", exc)
            self._ready.set()
            return
        try:
            root = tk.Tk()
        except Exception as exc:
            self.logger.error("桌面弹窗不可用（当前环境没有图形界面？）：%s", exc)
            self._ready.set()
            return

        root.withdraw()
        root.title("股市监控")
        self._root = root
        windows: List = []

        def close_window(win) -> None:
            try:
                win.destroy()
            except Exception:
                pass
            if win in windows:
                windows.remove(win)

        def show(title: str, body: str) -> None:
            while len(windows) >= self.max_popups:
                close_window(windows[0])
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            try:
                win.attributes("-alpha", 0.96)
            except Exception:
                pass
            win.configure(bg="#1f2430")

            outer = tk.Frame(win, bg="#1f2430", highlightbackground="#f0b429",
                             highlightthickness=1, padx=12, pady=10)
            outer.pack(fill="both", expand=True)
            tk.Label(outer, text=self._clip(title, 46), bg="#1f2430", fg="#f0b429",
                     font=("Microsoft YaHei UI", 11, "bold"), anchor="w",
                     justify="left", wraplength=340).pack(fill="x")
            tk.Label(outer, text=self._clip(body, 420), bg="#1f2430", fg="#e6e6e6",
                     font=("Microsoft YaHei UI", 9), anchor="w", justify="left",
                     wraplength=340).pack(fill="x", pady=(6, 0))
            tk.Label(outer, text="点击关闭", bg="#1f2430", fg="#6b7280",
                     font=("Microsoft YaHei UI", 8), anchor="e").pack(fill="x", pady=(6, 0))

            win.update_idletasks()
            width = max(320, win.winfo_reqwidth())
            height = win.winfo_reqheight()
            x = win.winfo_screenwidth() - width - 20
            stack = len(windows) * (height + 8)
            if self.position.startswith("top"):
                y = 40 + stack
            else:
                y = win.winfo_screenheight() - height - 60 - stack
            win.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
            win.bind("<Button-1>", lambda _event: close_window(win))
            for child in outer.winfo_children():
                child.bind("<Button-1>", lambda _event: close_window(win))
            windows.append(win)
            win.after(self.duration * 1000, lambda: close_window(win))
            self.shown += 1
            self.pending = max(0, self.pending - 1)
            self.logger.info("桌面弹窗已显示：%s", title)
            self._play_sound()

        def pump() -> None:
            try:
                while True:
                    title, body = self._queue.get_nowait()
                    show(title, body)
            except queue.Empty:
                pass
            except Exception as exc:      # 单个弹窗出错不应终止整个提醒线程
                self.logger.error("弹窗显示失败：%s", exc)
            try:
                root.after(150, pump)
            except Exception:
                pass

        self._available = True
        self._ready.set()
        root.after(150, pump)
        try:
            root.mainloop()
        except Exception as exc:
            self.logger.error("桌面弹窗线程退出：%s", exc)
        self._available = False

    def _play_sound(self) -> None:
        if not self.sound:
            return
        try:
            import winsound
            if self.sound_file and Path(self.sound_file).exists():
                winsound.PlaySound(str(self.sound_file),
                                   winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass    # 没有声卡或被禁用时静默忽略

    def notify(self, title: str, body: str) -> bool:
        """把弹窗投递给提醒线程（非阻塞，立刻返回）。"""
        if not self._available or self._queue is None:
            return False
        try:
            self._queue.put_nowait((title, body))
            self.pending += 1
            return True
        except Exception:
            return False

    def close(self) -> None:
        root = self._root
        if root is not None:
            try:
                root.after(0, root.destroy)
            except Exception:
                pass


# --------------------------------------------------------------------------------------
# 通知推送：桌面弹窗 / 邮件 / 微信 / 机器人
# --------------------------------------------------------------------------------------

class Notifier:
    """把报警或收盘摘要推送到邮件、微信（Server酱 / PushPlus）、企业微信、钉钉或自定义 Webhook。

    任何推送失败都只记录日志，绝不打断监控主流程；凭据留空时自动视为未启用。
    """

    def __init__(self, cfg: Dict, logger: logging.Logger, quiet: bool = False):
        self.cfg = cfg.get("notify") or {}
        self.logger = logger
        self.quiet = quiet
        self.email_cfg = self.cfg.get("email") or {}
        self.push_cfg = self.cfg.get("push") or {}
        self.max_per_hour = int(safe_float(self.cfg.get("max_per_hour")) or 0)
        self._sent: deque = deque()
        self.desktop = DesktopNotifier(self.cfg.get("desktop") or {}, logger, quiet=quiet)
        self.channels: List[str] = []
        if self.desktop_enabled:
            self.channels.append("桌面弹窗")
        if self.email_enabled:
            self.channels.append("邮件")
        if self.push_enabled:
            self.channels.append(f"推送({self.push_cfg.get('provider')})")

    # --- 是否启用 ---
    @property
    def desktop_enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True)) and self.desktop.enabled
    @property
    def email_enabled(self) -> bool:
        cfg = self.email_cfg
        if not (self.cfg.get("enabled", True) and cfg.get("enabled") and cfg.get("smtp_host")):
            return False
        sender = cfg.get("username") or cfg.get("sender")
        return bool(sender and (cfg.get("receivers") or sender))

    @property
    def push_enabled(self) -> bool:
        cfg = self.push_cfg
        if not (self.cfg.get("enabled", True) and cfg.get("enabled")):
            return False
        provider = str(cfg.get("provider", "")).lower()
        if provider in ("serverchan", "pushplus"):
            return bool(cfg.get("token"))
        if provider in ("wecom", "dingtalk", "custom"):
            return bool(cfg.get("webhook"))
        return False

    @property
    def enabled(self) -> bool:
        return self.email_enabled or self.push_enabled or self.desktop_enabled

    def close(self) -> None:
        """退出时关闭弹窗线程。"""
        self.desktop.close()

    def describe(self) -> str:
        return "、".join(self.channels) if self.channels else "未配置"

    # --- 限流 ---
    def _allow(self) -> bool:
        if not self.max_per_hour:
            return True
        now = time.time()
        while self._sent and now - self._sent[0] > 3600:
            self._sent.popleft()
        if len(self._sent) >= self.max_per_hour:
            return False
        self._sent.append(now)
        return True

    # --- 对外发送 ---
    def send(self, title: str, body: str) -> bool:
        return any(self.send_detailed(title, body).values())

    def send_detailed(self, title: str, body: str) -> Dict[str, bool]:
        """逐渠道发送，返回每个渠道的成功情况（供自检展示）。"""
        result: Dict[str, bool] = {}
        if not self.enabled:
            return result
        if not self._allow():
            self.logger.warning("推送已达到每小时上限（%d 条），本条通知被丢弃", self.max_per_hour)
            return result
        if self.desktop_enabled:
            result["desktop"] = self.desktop.notify(title, body)
        if self.email_enabled:
            result["email"] = self._send_email(title, body)
        if self.push_enabled:
            result["push"] = self._send_push(title, body)
        return result

    @staticmethod
    def format_alerts(alerts: Sequence[Alert]) -> Tuple[str, str]:
        """把一批报警整理成推送标题与正文。"""
        if len(alerts) == 1:
            one = alerts[0]
            title = f"[股市报警] {one.name} {one.rule}"
            body = (f"时间：{one.timestamp}\n"
                    f"标的：{one.code} {one.name}\n"
                    f"现价：{one.price}\n"
                    f"涨跌幅：{'' if one.change_pct is None else f'{one.change_pct:+.2f}%'}\n"
                    f"规则：{one.rule}\n"
                    f"详情：{one.message}")
            return title, body
        title = f"[股市报警] {len(alerts)} 条新提醒"
        body = "\n".join(
            f"{a.timestamp[11:]} {a.name}({a.code}) {a.rule}：{a.message}" for a in alerts
        )
        return title, body

    # --- 邮件 ---
    def _send_email(self, title: str, body: str) -> bool:
        from email.message import EmailMessage

        cfg = self.email_cfg
        sender = str(cfg.get("sender") or cfg.get("username") or "")
        receivers = [str(x).strip() for x in (cfg.get("receivers") or []) if str(x).strip()] or [sender]
        use_ssl = bool(cfg.get("use_ssl", True))
        port = int(safe_float(cfg.get("smtp_port")) or (465 if use_ssl else 25))
        host = str(cfg.get("smtp_host"))
        try:
            message = EmailMessage()
            message["From"] = sender
            message["To"] = ", ".join(receivers)
            message["Subject"] = title
            message.set_content(body)

            if use_ssl:
                connection: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=HTTP_TIMEOUT)
            else:
                connection = smtplib.SMTP(host, port, timeout=HTTP_TIMEOUT)
            with connection:
                if cfg.get("starttls") and not use_ssl:
                    connection.starttls()
                if cfg.get("username"):
                    connection.login(str(cfg["username"]), str(cfg.get("password") or ""))
                connection.send_message(message)
            self.logger.info("邮件通知已发送 → %s：%s", ", ".join(receivers), title)
            return True
        except Exception as exc:
            self.logger.error("邮件发送失败：%s", exc)
            if not self.quiet:
                print(f"  [推送失败] 邮件：{exc}")
            return False

    # --- 微信 / 机器人 ---
    def _send_push(self, title: str, body: str) -> bool:
        cfg = self.push_cfg
        provider = str(cfg.get("provider", "")).lower()
        token = str(cfg.get("token") or "")
        webhook = str(cfg.get("webhook") or "")
        plain = f"{title}\n\n{body}"
        try:
            if provider == "serverchan":
                reply = self._post_form(f"https://sctapi.ftqq.com/{token}.send",
                                        {"title": title, "desp": body})
            elif provider == "pushplus":
                reply = self._post_json("https://www.pushplus.plus/send",
                                        {"token": token, "title": title,
                                         "content": body.replace("\n", "<br>"), "template": "html"})
            elif provider in ("wecom", "dingtalk"):
                reply = self._post_json(webhook, {"msgtype": "text", "text": {"content": plain}})
            else:  # custom：推给你自己的接口，收到 {"title": ..., "content": ...}
                reply = self._post_json(webhook, {"title": title, "content": plain})
            error = self._reply_error(provider, reply)
            if error:
                self.logger.error("推送接口返回错误（%s）：%s", provider, error)
                if not self.quiet:
                    print(f"  [推送失败] {provider}：{error}")
                return False
            self.logger.info("推送通知已发送（%s）：%s | 返回 %s", provider, title, reply[:120])
            return True
        except Exception as exc:
            self.logger.error("推送失败（%s）：%s", provider, exc)
            if not self.quiet:
                print(f"  [推送失败] {provider}：{exc}")
            return False

    @staticmethod
    def _reply_error(provider: str, reply: str) -> Optional[str]:
        """HTTP 200 也可能带业务错误码（如 PushPlus 的无效 token），这里解析出来。"""
        try:
            data = json.loads(reply)
        except (json.JSONDecodeError, TypeError):
            return None  # 非 JSON 响应，无法判断，按成功处理
        if not isinstance(data, dict):
            return None
        if provider == "pushplus":
            if data.get("code") not in (0, 200, "0", "200", None):
                return str(data.get("msg") or data)
        elif provider == "serverchan":
            if data.get("code") not in (0, "0", None):
                return str(data.get("message") or data)
        elif provider in ("wecom", "dingtalk"):
            if data.get("errcode") not in (0, "0", None):
                return str(data.get("errmsg") or data)
        return None

    @staticmethod
    def _post_json(url: str, payload: Dict) -> str:
        headers = dict(HTTP_HEADERS)
        headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.read().decode("utf-8", errors="replace")

    @staticmethod
    def _post_form(url: str, payload: Dict) -> str:
        headers = dict(HTTP_HEADERS)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            url, data=urllib.parse.urlencode(payload).encode("utf-8"),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.read().decode("utf-8", errors="replace")


def wait_desktop(notifier: "Notifier", timeout: float = 3.0, minimum: float = 0.0) -> bool:
    """等待尚未显示的桌面弹窗画出来。

    弹窗由独立线程绘制，如果调用方紧接着退出进程，弹窗会随线程一起消失，
    所以「发一条就退出」的场景（--test-notify / --daily / 程序收尾）需要在这里稍等一下。
    队列为空时立即返回，不会造成无谓等待。
    """
    if not notifier.desktop_enabled:
        return False
    before = notifier.desktop.shown
    deadline = time.time() + timeout
    while time.time() < deadline and notifier.desktop.pending > 0:
        time.sleep(0.05)
    appeared = notifier.desktop.shown > before
    if appeared and minimum > 0:
        time.sleep(minimum)
    return appeared


def build_daily_summary(quotes: Dict[str, Quote], alert_rows: Sequence[Sequence[str]],
                        cfg: Optional[Dict] = None) -> Tuple[str, str]:
    """合并日报：大盘 + 涨停 + 持仓 + 预判 + 验证（版式对齐手机端收盘汇报）。"""
    cfg = cfg or {}
    day = datetime.now().strftime("%Y-%m-%d")

    watch = {w.get("code"): w for w in (cfg.get("watchlist") or []) if w.get("code")}
    positions = []
    for code, q in quotes.items():
        w = watch.get(code)
        if not w:
            continue
        try:
            shares = float(w.get("shares") or 0)
            cost = float(w.get("cost") or 0)
        except (TypeError, ValueError):
            continue
        if shares <= 0 or cost <= 0 or q.price is None:
            continue
        pnl = (q.price - cost) * shares
        positions.append((q, shares, cost, pnl))

    index_q = quotes.get("sh000001")
    limit_ups = [q for q in quotes.values()
                 if q.limit_up and q.price is not None and q.price >= q.limit_up - 1e-6]

    pred = load_today_prediction(day)
    preds = (pred.get("strong") or []) + (pred.get("likely") or [])

    verified = []
    hit = 0
    for b in preds:
        q = quotes.get(b["code"])
        if q is not None and q.change_pct is not None:
            ok = q.change_pct > 0
            hit += 1 if ok else 0
            verified.append(f"{b.get('name') or b['code']} {'✅' if ok else '❌'} {q.change_pct:+.1f}%")

    total_pnl = sum(p[3] for p in positions) if positions else 0.0
    lines = [f"📊 {day} 收盘"]
    if index_q is not None and index_q.price is not None and index_q.change_pct is not None:
        lines.append(f"大盘：上证 {index_q.price:.2f}（{index_q.change_pct:+.2f}%）")
    else:
        lines.append("大盘：上证 数据缺失")
    lines.append(f"涨停： {len(limit_ups)} 家")

    if positions:
        lines.append(f"持仓：{total_pnl:+.0f} 元（{len(positions)} 只）")
        for q, shares, cost, pnl in sorted(positions, key=lambda x: -x[3])[:8]:
            pct = (q.price / cost - 1) * 100 if cost else 0.0
            lines.append(f"· {q.name} {pnl:+.0f} 元（{pct:+.2f}%）")
    else:
        lines.append("持仓：无")

    if preds:
        body_preds = []
        for b in preds[:6]:
            q = quotes.get(b["code"])
            if q is not None and q.change_pct is not None:
                body_preds.append(f"· {b.get('name') or b['code']} {b['code']} {q.change_pct:+.1f}%")
        if body_preds:
            lines.append("")
            lines.append("【今日预判】")
            lines.extend(body_preds)
            if verified:
                lines.append(f"预判命中：{hit}/{len(verified)}（{' '.join(verified)}）")
            else:
                lines.append("预判命中：行情未取到，待核对")

    title = f"[收盘] {day} 涨停{len(limit_ups)} 持仓{len(positions)}只"
    return title, "\n".join(lines)


WECHAT_SEND_BIN = "/home/astock/.dsh/profiles/headless/node_modules/.bin/dsh-wechat-send"


def _split_text(text: str, limit: int = 180) -> List[str]:
    """按行把长文本拆成多条（微信单条有长度限制）。"""
    chunks: List[str] = []
    cur = ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def send_to_wechat(text: str, limit: int = 400) -> bool:
    """用微信插件的 CLI 把文本发到个人微信（超长才分段，失败不影响主流程）。

    收盘汇报整条约 200~300 字，limit 放宽到 400 可以一次发完，保持一条气泡的版式。
    """
    bin_path = Path(WECHAT_SEND_BIN)
    if not bin_path.exists():
        return False
    logger = logging.getLogger("stock_monitor")
    ok_all = True
    for chunk in _split_text(text, limit):
        try:
            proc = subprocess.run([str(bin_path), chunk], capture_output=True,
                                  text=True, timeout=40)
            if proc.returncode != 0:
                logger.warning(f"微信发送失败（rc={proc.returncode}）：{(proc.stderr or '').strip()[:200]}")
                ok_all = False
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"微信发送异常：{exc}")
            ok_all = False
    return ok_all


# --------------------------------------------------------------------------------------
# 配置加载
# --------------------------------------------------------------------------------------

def deep_merge(base: Dict, override: Dict) -> Dict:
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path) -> Dict:
    if path.exists():
        try:
            # utf-8-sig：兼容记事本 / VS Code 等保存的“UTF-8 带 BOM”文件
            with path.open("r", encoding="utf-8-sig") as fh:
                user_cfg = json.load(fh)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"配置文件解析失败：{path}\n{exc}")
        cfg = deep_merge(DEFAULT_CONFIG, user_cfg)
    else:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        with path.open("w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        print(f"未找到配置文件，已生成默认配置：{path}")

    # 规范化自选清单（group 字段用于 GUI 分组，缺省为「自选池」）
    cleaned: List[Dict] = []
    for item in cfg.get("watchlist") or []:
        if isinstance(item, str):
            item = {"code": item}
        if not isinstance(item, dict) or not item.get("code"):
            continue
        entry = dict(item)
        entry["code"] = guess_market_prefix(str(entry["code"]))
        entry.setdefault("name", entry["code"])
        group = str(entry.get("group") or "").strip() or DEFAULT_GROUP
        entry["group"] = group
        cleaned.append(entry)
    if not cleaned:
        cleaned = [dict(item) for item in DEFAULT_CONFIG["watchlist"]]
    cfg["watchlist"] = cleaned

    # 汇总分组顺序：先取 gui.groups 里预先声明的（允许空分组），再补上实际出现的
    groups: List[str] = []
    for name in (cfg.get("gui") or {}).get("groups") or []:
        name = str(name).strip()
        if name and name not in groups:
            groups.append(name)
    for item in cleaned:
        if item["group"] not in groups:
            groups.append(item["group"])
    if not groups:
        groups = [DEFAULT_GROUP]
    cfg["groups"] = groups

    cfg["poll_interval"] = max(3, int(safe_float(cfg.get("poll_interval")) or 15))
    cfg["off_hours_interval"] = max(10, int(safe_float(cfg.get("off_hours_interval")) or 60))
    return cfg


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="A 股 / 指数 / ETF 实时行情监控与报警工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：python main.py --once    python main.py --demo -i 3    python main.py --force",
    )
    parser.add_argument("-c", "--config", default=str(BASE_DIR / DEFAULT_CONFIG_NAME), help="配置文件路径")
    parser.add_argument("-i", "--interval", type=int, help="轮询间隔（秒），覆盖配置")
    parser.add_argument("--once", action="store_true", help="只抓取一次，打印并导出报表后退出")
    parser.add_argument("--rounds", type=int, default=0, help="抓取 N 轮后正常退出（0 表示一直运行）")
    parser.add_argument("--demo", action="store_true", help="使用内置模拟行情（离线自测）")
    parser.add_argument("--force", action="store_true", help="忽略交易时段限制，始终抓取")
    parser.add_argument("--no-report", action="store_true", help="不导出报表")
    parser.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    parser.add_argument("--quiet", action="store_true", help="只写日志，不打印表格")
    parser.add_argument("--daily", action="store_true",
                        help="立即抓取并生成当日收盘统计报表（可配推送），完成后退出")
    parser.add_argument("--test-notify", action="store_true",
                        help="发送一条测试通知，用于检查邮件/微信推送配置是否正确")
    parser.add_argument("--version", action="version", version=f"stock-monitor {VERSION}")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------

def fetch_with_fallback(feed: BaseFeed, codes: Sequence[str], logger: Reporter) -> Dict[str, Quote]:
    """抓取行情；auto 模式下腾讯失败自动切新浪。"""
    try:
        return feed.fetch(codes)
    except FeedError as exc:
        if isinstance(feed, TencentFeed):
            logger.error(f"{feed.name} 抓取失败（{exc}），切换备用源……")
            try:
                return SinaFeed().fetch(codes)
            except FeedError as exc2:
                logger.error(f"备用源同样失败：{exc2}")
        else:
            logger.error(f"{feed.name} 抓取失败：{exc}")
        return {}


def run(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (BASE_DIR / config_path).resolve()
    cfg = load_config(config_path)

    if args.interval:
        cfg["poll_interval"] = max(3, args.interval)
    if args.demo:
        cfg["source"] = "demo"
    if args.no_report:
        cfg["report"]["auto_export"] = False

    # 无论自选里有没有，指数必须常驻（收盘统计/大盘情绪都靠它）
    codes = list(dict.fromkeys(
        [item["code"] for item in cfg["watchlist"]] + ["sh000001"]))
    feed = build_feed(str(cfg.get("source", "auto")).lower(), cfg["watchlist"], cfg)
    if isinstance(feed, DemoFeed):
        cfg["market_hours_only"] = False

    use_color = (not args.no_color) and enable_windows_ansi()
    reporter = Reporter(cfg, use_color=use_color, quiet=args.quiet)
    engine = RuleEngine(cfg)
    notifier = Notifier(cfg, reporter.logger, quiet=args.quiet)
    reporter.print_banner(cfg, feed, cfg["watchlist"], args)
    reporter.info(f"通知推送：{notifier.describe()}")

    started = time.time()
    last_export = time.time()
    last_quotes: Dict[str, Quote] = {}
    round_no = 0
    off_ticks = 0
    fetched_once = False      # 启动后至少抓一次，避免盘后启动时一片空白
    failures = 0
    report_every = int(safe_float((cfg.get("report") or {}).get("every_minutes")) or 0) * 60
    daily_cfg = cfg.get("daily") or {}
    daily_enabled = bool(daily_cfg.get("enabled", True)) and not args.no_report
    daily_trigger = str(daily_cfg.get("trigger_time") or "15:05")
    daily_done_day = ""
    review_done_day = ""
    premarket_cfg = cfg.get("premarket") or {}
    premarket_enabled = bool(premarket_cfg.get("enabled", True))
    premarket_trigger = str(premarket_cfg.get("trigger_time") or "09:30")
    premarket_done_day = ""

    def do_daily(force_fetch: bool = False) -> Dict[str, Quote]:
        """生成当日收盘统计报表（并按配置推送摘要）。"""
        nonlocal last_quotes
        quotes = last_quotes
        if force_fetch or not quotes:
            quotes = fetch_with_fallback(feed, codes, reporter)
            if quotes:
                last_quotes = quotes
        if not quotes:
            reporter.error("未获取到行情数据，无法生成收盘统计报表")
            return {}
        # 收盘汇报要核对盘前预判，而预判标的往往不在自选里，这里补抓一次
        quotes = dict(quotes)
        pred_codes = [c for c in (load_today_prediction().get("codes") or []) if c]
        missing = [c for c in pred_codes
                   if c not in quotes or quotes[c].change_pct is None]
        if missing:
            quotes.update(fetch_with_fallback(feed, missing, reporter))
        reporter.export_daily(quotes, notifier)
        wait_desktop(notifier, minimum=1.5)   # 让收盘摘要弹窗有机会显示出来
        return quotes

    # --- 仅测试推送配置 ---
    if args.test_notify:
        if not notifier.enabled:
            reporter.error("没有启用任何推送渠道：请检查 config.json 的 notify（desktop / email / push）")
            return 1
        results = notifier.send_detailed(
            "[股市监控] 测试通知",
            f"这是一条测试通知，发送时间 {datetime.now():%Y-%m-%d %H:%M:%S}。\n"
            f"收到它说明「{notifier.describe()}」配置正确。\n"
            f"当前监控 {len(codes)} 个标的，数据源 {feed.name}。",
        )
        desktop_visible = wait_desktop(notifier, timeout=3.0, minimum=2.0)
        labels = {"desktop": "桌面弹窗", "email": "邮件", "push": "微信/机器人推送"}
        print("\n自检结果：")
        for key in ("desktop", "email", "push"):
            if key not in results:
                continue
            detail = ""
            if key == "desktop" and results[key]:
                detail = "（窗口已显示）" if desktop_visible else "（已投递，未确认显示）"
            print(f"  {labels[key]}：{'成功' if results[key] else '失败'}{detail}")
        ok = any(results.values())
        print("自检完成。" if ok else "存在失败渠道，详见日志与上方错误信息。")
        return 0 if ok else 1

    # --- 立即生成当日收盘统计报表 ---
    if args.daily:
        reporter.info("正在生成当日收盘统计报表……")
        return 0 if do_daily(force_fetch=True) else 1

    try:
        cfg_state: Dict = {"mtime": None}
        while True:
            if reload_config_if_changed(cfg, config_path, cfg_state):
                if reporter:
                    reporter.info("检测到 config.json 变更，已重新加载")
                # 配置热更新后必须同步刷新：行情列表 codes 与告警阈值 engine
                codes = list(dict.fromkeys(
                    [item["code"] for item in cfg["watchlist"]] + ["sh000001"]))
                engine = RuleEngine(cfg)
                reporter.info(f"已按新配置重建：{len(codes)} 个标的")
            now = datetime.now()
            in_session, status = market_status(cfg, now)

            # 每个交易日到点自动生成一次收盘统计报表（推送可选）
            if (daily_enabled and is_trading_day(cfg, now)
                    and now.strftime("%H:%M") >= daily_trigger
                    and daily_done_day != now.strftime("%Y-%m-%d")):
                reporter.info(f"已到收盘统计时间（{daily_trigger}），生成当日收盘报表……")
                if do_daily(force_fetch=not last_quotes):
                    daily_done_day = now.strftime("%Y-%m-%d")
                if review_done_day != now.strftime("%Y-%m-%d"):
                    try:
                        review_prediction(cfg, notifier, reporter)
                        review_done_day = now.strftime("%Y-%m-%d")
                    except Exception as exc:  # noqa: BLE001
                        reporter.error(f"预测回顾失败：{exc}")
                if args.once:
                    break
            # 盘前预测（9:30，每天一次；只在上午窗口内补发，避免晚上重启时补发）
            if (premarket_enabled and is_trading_day(cfg, now)
                    and premarket_trigger <= now.strftime("%H:%M") <= "11:30"
                    and premarket_done_day != now.strftime("%Y-%m-%d")):
                reporter.info(f"已到盘前预测时间（{premarket_trigger}），开始预测……")
                run_premarket_prediction(codes, cfg, notifier, reporter)
                premarket_done_day = now.strftime("%Y-%m-%d")
            if (cfg.get("market_hours_only", True) and not args.force and not in_session
                    and fetched_once):
                off_ticks += 1
                if off_ticks == 1 or off_ticks % 10 == 1:
                    reporter.info(f"{status}，等待开盘（每 {cfg['off_hours_interval']}s 检查一次，"
                                  f"可加 --force 忽略时段限制）")
                if args.rounds:
                    # 有限轮次模式：收盘后拿不到更多行情，直接结束而不是空等开盘
                    reporter.info(f"当前{status}，已抓取 {round_no} 轮后退出"
                                  f"（想继续抓可加 --force 忽略时段限制）")
                    break
                time.sleep(cfg["off_hours_interval"])
                continue

            round_no += 1
            quotes = fetch_with_fallback(feed, codes, reporter)
            if quotes:
                failures = 0
                fetched_once = True
                last_quotes = quotes
                show_status = status if (in_session or args.force) else f"{status}/强制"
                reporter.print_table(quotes, round_no, show_status)
                # 非交易时段/休市日不评估报警，避免用上一交易日的旧行情重复提醒
                if in_session or args.force:
                    alerts = engine.evaluate(quotes)
                    reporter.emit_alerts(alerts)
                else:
                    alerts = []
                if alerts and notifier.enabled:
                    notifier.send(*Notifier.format_alerts(alerts))
                reporter.logger.info(
                    "第 %d 轮：%d 个标的，%d 条报警", round_no, len(quotes), len(alerts)
                )
                if report_every and time.time() - last_export >= report_every:
                    reporter.export_report(quotes, "定时导出")
                    last_export = time.time()
            else:
                failures += 1
                if failures >= 5:
                    reporter.error("连续 5 次抓取失败，请检查网络或数据源配置")
                    failures = 0

            if args.once or (args.rounds and round_no >= args.rounds):
                break
            time.sleep(cfg["poll_interval"])
    except KeyboardInterrupt:
        print()
        reporter.info("收到停止信号（Ctrl+C），正在收尾……")

    # 退出收尾
    if last_quotes and (cfg.get("report") or {}).get("export_on_exit", True):
        reporter.export_report(last_quotes, "退出快照")

    elapsed = time.time() - started
    summary = (f"本次运行：{elapsed / 60:.1f} 分钟，共 {round_no} 轮，"
               f"报警 {len(reporter.session_alerts)} 条，日志 {reporter.log_file}")
    print()
    print(summary)
    reporter.logger.info(summary)
    wait_desktop(notifier, minimum=1.5)   # 收尾前让最后一条弹窗显示出来
    notifier.close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # 防止个别生僻字导致输出崩溃
    except (AttributeError, ValueError):
        pass
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
