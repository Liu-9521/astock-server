#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""金银价实时推送：抓新浪金价 + DeepSeek 预测未来一天走势 → 企业微信。

用法：
    python3 gold_push.py                    # 正常推送
    python3 gold_push.py --dry-run          # 只打印不发
    python3 gold_push.py --retry-pending    # 只补发上次失败的（cron 每 10 分钟调用）
    python3 gold_push.py --backfill 20:00   # 补发指定场次（标题会标注）

发送失败的内容会存到 gold_pending.json；在下一场播报到来之前，cron 每 10 分钟
重试一次，通了就补发，到下一场播报时刻仍未发出就丢弃，避免过期内容堆积。
积压超过 30 分钟或连续 3 次失败时，补发消息末尾会附加一条网络异常说明。
"""
import json
import os
import sys
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
SINA = "https://hq.sinajs.cn/list=hf_GC,hf_SI,hf_XAU,hf_XAG"
SINA_FX = "https://hq.sinajs.cn/list=fx_susdcny"
SINA_HEADERS = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn"}
OUNCE_GRAMS = 31.1035   # 1 盎司 = 31.1035 克

WECOM = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
         "?key=<WECOM_YOUR_KEY>")
DEEPSEEK_KEY = "<DEEPSEEK_YOUR_KEY>"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

PENDING = "/opt/astock/gold_pending.json"
# 播报时刻，与 crontab 的 0 8,20 * * * 和 30 12 * * * 对应
PUSH_TIMES = [(8, 0), (12, 30), (20, 0)]

# 名称: (显示名, 单位说明)
NAMES = {
    "hf_GC": ("COMEX 黄金", "美元/盎司"),
    "hf_SI": ("COMEX 白银", "美元/盎司"),
    "hf_XAU": ("伦敦金现", "美元/盎司"),
    "hf_XAG": ("伦敦银现", "美元/盎司"),
}


def fetch_quotes():
    r = requests.get(SINA, headers=SINA_HEADERS, timeout=15)
    text = r.content.decode("gbk", errors="ignore")
    out = {}
    for line in text.splitlines():
        if "hq_str_" not in line:
            continue
        key = line.split('hq_str_')[1].split('=')[0]
        val = line.split('"')[1] if '"' in line else ""
        parts = val.split(",")
        if len(parts) < 8:
            continue
        try:
            price = float(parts[0])
            prev = float(parts[7])
        except ValueError:
            continue
        if price <= 0 or prev <= 0:
            continue
        chg = price - prev
        pct = chg / prev * 100
        out[key] = {"price": price, "prev": prev, "chg": chg, "pct": pct}
    return out


def fetch_rate():
    """美元兑人民币；取新浪在岸价，失败用 7.1 兜底。"""
    try:
        r = requests.get(SINA_FX, headers=SINA_HEADERS, timeout=10)
        text = r.content.decode("gbk", errors="ignore")
        val = text.split('"')[1]
        parts = val.split(",")
        rate = float(parts[1])
        if 5 < rate < 9:
            return rate
    except Exception:  # noqa: BLE001
        pass
    return 7.1


def cny_per_gram(usd_per_oz, rate):
    return usd_per_oz * rate / OUNCE_GRAMS


def predict(quotes, rate=7.1):
    """用 DeepSeek 给出未来一天走势判断。失败则用简单规则。"""
    if not quotes:
        return "（无数据）"
    rows = []
    for k, v in quotes.items():
        name = NAMES.get(k, (k, ""))[0]
        cny = cny_per_gram(v["price"], rate)
        rows.append(f"{name}: {v['price']:.2f} 美元/盎司（约 {cny:.1f} 元/克）, "
                    f"较昨收 {v['pct']:+.2f}%")
    prompt = ("以下是当前金银价格（美元/盎司，括号内为换算人民币/克），请用简体中文给出未来约一天（24 小时）"
              "的走势判断，150 字以内，分点说明：方向（偏多/偏空/震荡）、关键价位（同时给美元/盎司和元/克）、"
              "风险因素。直接判断，不要免责声明。\n\n" + "\n".join(rows))
    try:
        r = requests.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}",
                     "Content-Type": "application/json"},
            json={"model": "deepseek-chat", "temperature": 0.3,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60)
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[预测失败] {exc}")
        # 简单规则兜底
        lines = []
        for k, v in quotes.items():
            name = NAMES.get(k, (k, ""))[0]
            if v["pct"] > 1:
                trend = "偏强，短线有继续上冲动能"
            elif v["pct"] < -1:
                trend = "偏弱，短线可能继续回落"
            else:
                trend = "震荡，方向不明"
            lines.append(f"• {name}：{trend}")
        return "\n".join(lines)


def build_message(backfill=None):
    quotes = fetch_quotes()
    if not quotes:
        return None
    rate = fetch_rate()

    now = time.strftime("%Y-%m-%d %H:%M")
    if backfill:
        title = f"### 💰 金银价快报（补发 {backfill} 场次 · 现价 {now}）"
    else:
        title = f"### 💰 金银价快报（{now}）"
    lines = [title,
             f'<font color="comment">汇率 USD/CNY ≈ {rate:.4f}</font>',
             ""]

    # 主信息：COMEX 黄金/白银（加粗，人民币为主）
    secondary = []
    for k, v in quotes.items():
        name = NAMES.get(k, (k, ""))[0]
        cny = cny_per_gram(v["price"], rate)
        arrow = "🔺" if v["pct"] > 0 else ("🔻" if v["pct"] < 0 else "➖")
        if k in ("hf_GC", "hf_SI"):
            icon = "🥇" if k == "hf_GC" else "🥈"
            lines.append(
                f"{icon} **{name}：{cny:.2f} 元/克** {arrow} {v['pct']:+.2f}%")
            secondary.append(f'{name} {v["price"]:.2f} 美元/盎司')
        else:
            secondary.append(
                f'{name} {v["price"]:.2f} 美元/盎司 · {cny:.2f} 元/克')

    # 次信息：美元报价 + 伦敦金/银（灰色）
    if secondary:
        lines.append("")
        for s in secondary:
            lines.append(f'<font color="comment">{s}</font>')

    lines.append("")
    lines.append("### 🔮 未来 24 小时走势")
    lines.append(predict(quotes, rate))
    return "\n".join(lines)


def send_wecom(md):
    try:
        r = requests.post(WECOM, json={"msgtype": "markdown",
                                       "markdown": {"content": md[:3900]}},
                          timeout=20)
        return r.json().get("errcode") == 0
    except Exception as exc:  # noqa: BLE001
        print("企微发送失败:", exc)
        return False


# ---------------- 失败补发 ----------------

def _save_pending_json(data):
    try:
        with open(PENDING, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        print("写 pending 失败:", exc)


def next_push_ts(now=None):
    """下一个播报时刻的时间戳。"""
    now = now or time.time()
    t = time.localtime(now)
    for h, m in PUSH_TIMES:
        cand = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, -1))
        if cand > now + 60:
            return cand
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 8, 0, 0, 0, 0, -1))


def save_pending(md, reason):
    """把没发出去的内容缓存起来；md 为 None 表示"下次重抓一次再发"。"""
    _save_pending_json({"md": md, "created": time.time(), "reason": reason,
                        "fails": 0, "next_push": next_push_ts()})
    print(f"[已缓存待发] {reason}")


def load_pending():
    try:
        with open(PENDING, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def clear_pending():
    try:
        os.remove(PENDING)
    except FileNotFoundError:
        pass


def retry_pending(attempts=3, gap=20):
    """补发缓存内容；返回 True 表示已发出、已丢弃或本来就没事要做。"""
    p = load_pending()
    if not p:
        return True
    now = time.time()
    if now >= float(p.get("next_push") or 0):
        clear_pending()
        print("[丢弃] 下一场播报已到，上一条不再补发")
        return True

    md = p.get("md")
    if not md:
        # 当初是抓数据就失败了，这里重新生成一次
        md = build_message()
        if not md:
            print("[补发] 仍取不到行情，等下次检测")
            p["fails"] = int(p.get("fails") or 0) + 1
            _save_pending_json(p)
            return False
        p["md"] = md
        p["created"] = now
        _save_pending_json(p)

    fails = int(p.get("fails") or 0)
    age_min = (now - float(p.get("created") or now)) / 60

    # 积压够久或失败够多次，补发时附上说明，让人知道中间断过
    note = ""
    if fails >= 3 or age_min >= 30:
        note = (f'\n\n<font color="warning">⚠️ 本条积压 {age_min:.0f} 分钟，'
                f'期间 {fails} 次补发失败，网络恢复后补发。</font>')

    for i in range(attempts):
        if send_wecom(md + note):
            clear_pending()
            print(f"[补发成功] 积压 {age_min:.0f} 分钟，{fails} 次失败后恢复")
            return True
        if i < attempts - 1:
            time.sleep(gap)

    p["fails"] = fails + 1
    _save_pending_json(p)
    print(f"[补发失败] 已积压 {age_min:.0f} 分钟，累计 {fails + 1} 次，下次再试")
    return False


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args

    if "--retry-pending" in args:
        return 0 if retry_pending() else 1

    backfill = None
    if "--backfill" in args:
        i = args.index("--backfill")
        if i + 1 < len(args):
            backfill = args[i + 1]

    # 有积压先补发，保证消息顺序
    if not dry and load_pending():
        retry_pending()

    md = build_message(backfill)
    if not md:
        print("没取到金价数据")
        if not dry:
            save_pending(None, "没取到金价数据")
        return 1
    if dry:
        print(md)
        return 0
    ok = send_wecom(md)
    print("发送:", "成功" if ok else "失败")
    if not ok:
        save_pending(md, "企微发送失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
