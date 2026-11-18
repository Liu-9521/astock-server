#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.expanduser("~/.dsh"))
try:
    from secrets_lib import get as _secret
except Exception:
    def _secret(name, default=""):
        return default

"""每日早报：福州天气 + 穿搭建议 + DeepSeek 余额，推送到企业微信"""
import json
import ssl
import time
import urllib.request

CITY = "Fuzhou"
WECOM = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
         "?key=" + _secret("wecom_webhook_key"))
DEEPSEEK_KEY = _secret("deepseek_api_key")

WEATHER_ZH = {
    "Sunny": "晴", "Clear": "晴", "Partly cloudy": "多云", "Cloudy": "多云",
    "Overcast": "阴", "Mist": "薄雾", "Fog": "雾", "Haze": "霾",
    "Light rain": "小雨", "Moderate rain": "中雨", "Heavy rain": "大雨",
    "Light drizzle": "毛毛雨", "Patchy rain possible": "局部有雨",
    "Patchy rain nearby": "附近有雨", "Thundery outbreaks possible": "可能有雷雨",
    "Light rain shower": "阵雨", "Moderate or heavy rain shower": "中到大阵雨",
    "Light snow": "小雪", "Moderate snow": "中雪", "Heavy snow": "大雪",
}


def zh_weather(desc: str) -> str:
    desc = str(desc or "").strip()
    if desc in WEATHER_ZH:
        return WEATHER_ZH[desc]
    for k, v in WEATHER_ZH.items():
        if k.lower() in desc.lower():
            return v
    return desc


def get_weather():
    url = f"https://wttr.in/{CITY}?format=j1"
    proxy = urllib.request.ProxyHandler({"http": "http://127.0.0.1:7890",
                                         "https": "http://127.0.0.1:7890"})
    opener = urllib.request.build_opener(proxy)
    with opener.open(url, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    cur = (data.get("current_condition") or [{}])[0]
    today = (data.get("weather") or [{}])[0]
    return {
        "temp": cur.get("temp_C"),
        "feels": cur.get("FeelsLikeC"),
        "desc": zh_weather(
            (cur.get("weatherDesc") or [{}])[0].get("value", "")
        ),
        "humidity": cur.get("humidity"),
        "wind": cur.get("windspeedKmph"),
        "max": today.get("maxtempC"),
        "min": today.get("mintempC"),
        "rain_chance": (today.get("hourly") or [{}])[0].get("chanceofrain", ""),
    }


def get_balance():
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        infos = data.get("balance_infos") or []
        return infos[0].get("total_balance") if infos else "?"
    except Exception:  # noqa: BLE001
        return "?"


def outfit_advice(temp, desc, rain, wind):
    tips = []
    try:
        t = float(temp)
    except (TypeError, ValueError):
        t = 20
    if t < 10:
        tips.append("厚外套/羽绒服，注意保暖")
    elif t < 15:
        tips.append("外套 + 毛衣")
    elif t < 20:
        tips.append("薄外套或长袖")
    elif t < 25:
        tips.append("长袖/薄衫")
    elif t < 30:
        tips.append("短袖")
    else:
        tips.append("短袖 + 防晒")

    if "雨" in str(desc) or (str(rain).isdigit() and int(rain) >= 50):
        tips.append("带伞（有雨）")
    if str(wind).isdigit() and int(wind) >= 25:
        tips.append("风大，防风外套")
    return "；".join(tips)


def send_wecom(content):
    payload = {"msgtype": "text", "text": {"content": content}}
    req = urllib.request.Request(
        WECOM,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    day = time.strftime("%Y-%m-%d %A")
    try:
        w = get_weather()
        weather_txt = (
            f"{w['desc']}  {w['temp']}°C（体感 {w['feels']}°C）\n"
            f"今日 {w['min']}~{w['max']}°C，湿度 {w['humidity']}%，风 {w['wind']}km/h，降水概率 {w['rain_chance']}%"
        )
        advice = outfit_advice(w["temp"], w["desc"], w["rain_chance"], w["wind"])
    except Exception as exc:  # noqa: BLE001
        weather_txt = f"天气获取失败：{exc}"
        advice = "（天气数据不可用，自行判断）"
    balance = get_balance()

    content = (
        f"☀️ 早安！{day}\n"
        f"📍 福州仓山\n\n"
        f"🌤 天气：{weather_txt}\n\n"
        f"👕 出门建议：{advice}\n\n"
        f"💰 DeepSeek 余额：{balance} 元"
    )
    try:
        result = send_wecom(content)
        print("sent:", result)
    except Exception as exc:  # noqa: BLE001
        print("send failed:", exc)
    print(content)


if __name__ == "__main__":
    main()
