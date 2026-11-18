#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄金价格关键位监控：价格穿越关键位时推送微信。

防刷屏规则：
1. 每个关键位单独武装，穿越报警后必须离开该位 REARM 美元才重新武装；
2. 同一关键位 6 小时内最多报一次；
3. 急涨急跌（≥1.5%）2 小时内最多报一次。
"""

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE = BASE / "gold_watch.json"
LOG = BASE / "gold_watch.log"
SEND = "/home/astock/.dsh/profiles/headless/node_modules/.bin/dsh-wechat-send"
SPOT_PY = BASE / ".venv_img" / "bin" / "python"
SPOT = BASE / "gold_spot.py"


def kick_spot():
    """每 10 分钟跑一次，顺手把「整点播报」拉起（gold_spot 自己按小时槽去重，
    同一小时不会重复发）。这样每小时播报不依赖 root 的 crontab 时段。"""
    try:
        if not SPOT_PY.exists() or not SPOT.exists():
            return
        with (BASE / "gold_spot_kick.log").open("a", encoding="utf-8") as f:
            subprocess.Popen([str(SPOT_PY), str(SPOT)], cwd=str(BASE),
                             stdout=f, stderr=f, start_new_session=True,
                             env={**os.environ, "MPLCONFIGDIR": "/tmp/mplcache"})
    except Exception:
        pass

# 关键位（美元/盎司）
LEVELS = [3500, 3600, 3700, 3750, 3800, 3850, 3900,
          3950, 4000, 4050, 4100, 4200, 4300]
BIG_MOVE = 1.5          # 单次波动超过 1.5% 也提醒
REARM = 20.0            # 离开关键位 20 美元才重新武装
LEVEL_HOLD_H = 6        # 同一关键位 6 小时内不重复
BIG_HOLD_H = 2          # 急涨急跌 2 小时内不重复
RATE = 6.664

HEAD = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}


def spot_gold():
    req = urllib.request.Request("https://hq.sinajs.cn/list=hf_XAU", headers=HEAD)
    txt = urllib.request.urlopen(req, timeout=12).read().decode("gbk", "ignore")
    body = txt.split('="', 1)[1].rstrip('";\n')
    return float(body.split(",")[0])


def load_state():
    try:
        s = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        s = {}
    s.setdefault("last", None)
    s.setdefault("levels", {})
    s.setdefault("last_big", None)
    return s


def save_state(s):
    try:
        STATE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def log(msg):
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%F %T} {msg}\n")
    except OSError:
        pass


def send(text):
    try:
        import sys
        sys.path.insert(0, "/opt/astock")
        from db_log import log_alert
        log_alert("gold", "XAU", "", (text or "")[:200])
    except Exception:
        pass
    try:
        r = subprocess.run([SEND, text], capture_output=True, text=True, timeout=40)
        log(f"send rc={r.returncode}")
        return r.returncode in (0, 3)
    except Exception as e:
        log(f"send err {e}")
        return False


def hours_since(ts):
    if not ts:
        return 999.0
    try:
        return (datetime.now() - datetime.strptime(ts, "%Y-%m-%d %H:%M")).total_seconds() / 3600
    except Exception:
        return 999.0


def hold_note():
    return "持仓：华安+博时 各约 5000（合计约 1 万）"


def main():
    dry = "--dry" in sys.argv
    try:
        px = spot_gold()
    except Exception as e:
        log(f"价格获取失败 {e}")
        return 1

    st = load_state()
    prev = st.get("last")
    cny = px * RATE / 31.1035
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
    msgs = []

    for lv in LEVELS:
        key = str(lv)
        info = st["levels"].get(key) or {}
        armed = info.get("armed", True)
        last_alert = info.get("alert")

        # 离开关键位足够远 → 重新武装
        if not armed and abs(px - lv) >= REARM:
            armed = True
            info["armed"] = True

        crossed = prev is not None and (
            (prev > lv >= px) or (prev < lv <= px)
        )
        if crossed and armed and hours_since(last_alert) >= LEVEL_HOLD_H:
            if prev > lv:      # 下破
                tip = "关键支撑失守" if lv >= 3800 else "已到重要低位"
                msgs.append(f"🔻 跌破 {lv}\n现货 {px:.1f}（约 {cny:.0f} 元/克）\n{tip} · {hold_note()}")
            else:              # 上破
                tip = "突破关键位" if lv <= 4050 else "强势上攻"
                msgs.append(f"🔺 站上 {lv}\n现货 {px:.1f}（约 {cny:.0f} 元/克）\n{tip} · {hold_note()}")
            info["armed"] = False
            info["alert"] = now_s

        if info:
            st["levels"][key] = info

    # 急涨急跌
    if prev and abs(px / prev - 1) * 100 >= BIG_MOVE and hours_since(st.get("last_big")) >= BIG_HOLD_H:
        direction = "急涨" if px > prev else "急跌"
        msgs.append(f"⚡ 黄金{direction} {px / prev - 1:+.1%}\n"
                    f"现货 {px:.1f}（约 {cny:.0f} 元/克）\n{hold_note()}")
        st["last_big"] = now_s

    st["last"] = px
    save_state(st)

    text = "\n\n".join(msgs)
    if dry:
        print(f"price={px:.2f} cny={cny:.0f}/g prev={prev}")
        print("将推送:\n" + (text or "（无）"))
        return 0
    if text:
        send(text)
    # kick_spot()
    return 0


if __name__ == "__main__":
    sys.exit(main())
