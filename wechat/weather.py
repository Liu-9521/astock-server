#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""早报天气数据：多源兜底，输出一行 JSON。

顺序：中国天气(itboy, 国内) → wttr.in → open-meteo
字段：desc 天气 / temp 温度 / feels 体感 / lo~hi 区间 / hum 湿度 / wind 风速km/h / pop 降水概率%
"""
import json
import re
import sys
import urllib.request

CITY_KEY = "101230101"          # 福州市
LAT, LON = 26.0745, 119.2965     # 福州仓山
UA = {"User-Agent": "Mozilla/5.0"}
PROXY = "http://127.0.0.1:7890"

WCODE = {
    0: "晴", 1: "晴", 2: "多云", 3: "阴", 45: "雾", 48: "雾",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪",
    80: "阵雨", 81: "阵雨", 82: "强阵雨", 85: "阵雪", 86: "阵雪",
    95: "雷阵雨", 96: "雷阵雨", 99: "雷阵雨",
}


def _get(url, timeout=12, proxy=None, encoding="utf-8"):
    req = urllib.request.Request(url, headers=UA)
    if proxy:
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        raw = op.open(req, timeout=timeout).read()
    else:
        raw = urllib.request.urlopen(req, timeout=timeout).read()
    return raw.decode(encoding, "ignore")


def from_itboy():
    txt = _get(f"http://t.weather.itboy.net/api/weather/city/{CITY_KEY}")
    d = json.loads(txt)
    if str(d.get("status")) != "200":
        return None
    data = d["data"]
    cur = data["forecast"][0]
    temp = round(float(data["wendu"]))
    hi = int(float(re.search(r"-?\d+(\.\d+)?", cur["high"]).group()))
    lo = int(float(re.search(r"-?\d+(\.\d+)?", cur["low"]).group()))
    return {
        "desc": cur["type"], "temp": temp, "feels": temp,
        "lo": lo, "hi": hi, "hum": data["shidu"].rstrip("%"),
        "wind": cur.get("fl", "").replace("<![CDATA[", "").replace("]]>", ""),
        "pop": 0,
    }


def from_wttr():
    txt = _get("https://wttr.in/Fuzhou?format=j1", timeout=20, proxy=PROXY)
    j = json.loads(txt)
    cur = j["current_condition"][0]
    w0 = j["weather"][0]
    return {
        "desc": cur["weatherDesc"][0]["value"],
        "temp": int(cur["temp_C"]), "feels": int(cur["FeelsLikeC"]),
        "lo": int(w0["mintempC"]), "hi": int(w0["maxtempC"]),
        "hum": int(cur["humidity"]), "wind": int(cur["windspeedKmph"]),
        "pop": max(int(h.get("chanceofrain", 0)) for h in w0["hourly"]),
    }


def from_openmeteo():
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&current=temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,weather_code"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           "&timezone=Asia%%2FShanghai&forecast_days=1" % (LAT, LON))
    j = json.loads(_get(url))
    c, d = j["current"], j["daily"]
    return {
        "desc": WCODE.get(int(c["weather_code"]), "未知"),
        "temp": round(c["temperature_2m"]), "feels": round(c["apparent_temperature"]),
        "lo": round(d["temperature_2m_min"][0]), "hi": round(d["temperature_2m_max"][0]),
        "hum": round(c["relative_humidity_2m"]),
        "wind": round(c["wind_speed_10m"]),
        "pop": int(d.get("precipitation_probability_max", [0])[0] or 0),
    }


def main():
    for name, fn in (("itboy", from_itboy), ("wttr", from_wttr), ("open-meteo", from_openmeteo)):
        try:
            d = fn()
            if d and d.get("desc"):
                d["src"] = name
                print(json.dumps(d, ensure_ascii=False))
                return 0
        except Exception as exc:  # noqa: BLE001
            print(f"[{name} 失败] {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
