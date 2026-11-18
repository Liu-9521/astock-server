#!/bin/bash
# 每日早报：天气 + 穿搭建议 + DeepSeek 余额
# 由 cron 每天 08:30 调用，也可手动执行：bash ~/.dsh/dsh_wechat/morning_report.sh --dry-run

export PATH=/usr/local/bin:/usr/bin:/bin
export TZ=Asia/Shanghai
SEND=/home/astock/.dsh/profiles/headless/node_modules/.bin/dsh-wechat-send
CITY="Fuzhou"

WXJ=$(/usr/bin/python3 /home/astock/.dsh/dsh_wechat/weather.py 2>/dev/null)

KEY=$(python3 -c "import re;print(re.search(r'DEEPSEEK_API_KEY:\s*(\S+)',open('/home/astock/.dsh/.credentials.yaml').read()).group(1))" 2>/dev/null)
BAL_JSON=$(curl -s -m 20 https://api.deepseek.com/user/balance -H "Authorization: Bearer $KEY" 2>/dev/null)

if [ "$1" = "--dry-run" ]; then
    PASS=$(python3 -c "import json;print(json.load(open('/home/astock/.dsh/dsh_wechat/quant/auth.json'))['password'])" 2>/dev/null)
else
PASS=$(python3 - <<'PYGEN'
import secrets, json, pathlib, datetime
alpha = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
pw = "".join(secrets.choice(alpha) for _ in range(12))
p = pathlib.Path("/home/astock/.dsh/dsh_wechat/quant/auth.json")
try:
    d = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    d = {}
d["password"] = pw
d["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
print(pw)
PYGEN
)
fi
MSG=$(WXJ="$WXJ" BAL_JSON="$BAL_JSON" PASS="$PASS" python3 - <<'PY'
import os, json, datetime

today = datetime.date.today()
wd = "一二三四五六日"[today.weekday()]

trans = {
    "sunny": "晴", "clear": "晴", "partly cloudy": "多云", "cloudy": "多云",
    "overcast": "阴", "mist": "雾", "fog": "雾", "light rain": "小雨",
    "moderate rain": "中雨", "heavy rain": "大雨", "light drizzle": "毛毛雨",
    "patchy rain possible": "局部有雨", "thundery outbreaks possible": "可能有雷雨",
    "patchy rain nearby": "局部有雨", "light rain shower": "阵雨", "patchy light rain": "零星小雨",
    "patchy light rain with thunder": "雷阵雨", "moderate or heavy rain shower": "强阵雨",
}

def zh(desc):
    d = (desc or "").strip().lower()
    return trans.get(d, desc.strip() or "未知")

lines = []
days = ["一", "二", "三", "四", "五", "六", "日"]
lines.append(f"☀️ 早安！{today.strftime('%Y-%m-%d')} 星期{wd}")
lines.append("📍 福州仓山")
lines.append("")

try:
    w = json.loads(os.environ.get("WXJ") or "{}")
    if not w.get("desc"):
        raise ValueError("no data")
    temp = int(w["temp"]); feels = int(w.get("feels", temp))
    hum = w["hum"]; wind = w["wind"]
    desc = zh(w["desc"])
    lo, hi = int(w["lo"]), int(w["hi"])
    pop = int(w.get("pop") or 0)

    lines.append(f"🌤 天气：{desc} {temp}°C（体感 {feels}°C）")
    wind_txt = f"{int(wind)}km/h" if isinstance(wind, (int, float)) else str(wind)
    lines.append(f"今日 {lo}~{hi}°C，湿度 {hum}%，风 {wind_txt}，降水概率 {pop}%")
    lines.append("")

    t = int(temp)
    if t < 10: wear = "厚外套或羽绒服，注意保暖"
    elif t < 15: wear = "外套+毛衣，早晚温差注意"
    elif t < 20: wear = "薄外套或长袖就够"
    elif t < 25: wear = "长袖/薄衫，舒服的一天"
    elif t < 30: wear = "短袖出门"
    else: wear = "短袖+防晒，注意补水"
    if pop >= 50: wear += "，记得带伞 ☔"
    elif pop >= 30: wear += "，包里塞把折叠伞稳妥些"
    try:
        if int(str(wind).replace("级","")) >= 4: wear += "；风有点大，注意防风"
    except Exception:
        pass
    lines.append(f"👕 出门建议：{wear}")
except Exception:
    lines.append("🌤 天气：暂时取不到（接口异常）")
    lines.append("")

try:
    b = json.loads(os.environ.get("BAL_JSON") or "{}")
    bal = b["balance_infos"][0]["total_balance"]
    lines.append("")
    lines.append(f"💰 DeepSeek 余额：{bal} 元")
except Exception:
    lines.append("")
    lines.append("💰 DeepSeek 余额：查询失败")

lines.append("")
lines.append("🔐 今日看板密码：" + (os.environ.get("PASS") or ""))
lines.append("    https://live.lkyastock.top/live")
print("\n".join(lines))
PY
)

if [ "$1" = "--dry-run" ]; then
    echo "$MSG"
    exit 0
fi

OUT=$("$SEND" "$MSG" 2>&1); RC=$?
echo "$(date '+%F %T') rc=$RC $OUT" >> /home/astock/.dsh/dsh_wechat/morning_report.log
exit $RC
