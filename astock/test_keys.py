#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐项验证 secrets.json 里的密钥是否真的能用。"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

SEC = os.path.expanduser("~/.dsh/secrets.json")
try:
    with open(SEC, encoding="utf-8") as f:
        S = json.load(f)
    print(f"读取 {SEC}：{len(S)} 个字段\n")
except Exception as e:
    print(f"[FAIL] 读不到 {SEC}: {e}")
    sys.exit(1)


def post_json(url, payload, headers=None, timeout=25):
    data = json.dumps(payload).encode()
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def get(url, headers=None, timeout=25, data=None):
    h = dict(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


results = []

# ── 1. 企业微信 webhook ──────────────────────────────
key = S.get("wecom_webhook_key", "")
if not key:
    results.append(("企微 webhook", "SKIP", "字段为空"))
else:
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
    body = {"msgtype": "text",
            "text": {"content": f"【密钥自检】astock 服务器密钥验证\n时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n来源：test_keys.py\n状态：企微通道正常 ✓"}}
    try:
        st, txt = post_json(url, body)
        j = json.loads(txt)
        if j.get("errcode") == 0:
            results.append(("企微 webhook", "OK", f"HTTP {st} errcode=0（消息应已收到）"))
        else:
            results.append(("企微 webhook", "FAIL", f"errcode={j.get('errcode')} {j.get('errmsg')}"))
    except urllib.error.HTTPError as e:
        results.append(("企微 webhook", "FAIL", f"HTTP {e.code} {e.read()[:120]}"))
    except Exception as e:
        results.append(("企微 webhook", "FAIL", f"{type(e).__name__}: {e}"))

# ── 2. DeepSeek 主 Key ───────────────────────────────
def check_deepseek(name, k):
    if not k:
        return (name, "SKIP", "字段为空")
    body = {"model": "deepseek-chat",
            "messages": [{"role": "user", "content": "回复两个字：正常"}],
            "max_tokens": 10, "stream": False}
    try:
        st, txt = post_json("https://api.deepseek.com/chat/completions", body,
                            {"Authorization": f"Bearer {k}"}, timeout=40)
        j = json.loads(txt)
        if "choices" in j:
            reply = j["choices"][0]["message"]["content"].strip()
            usage = j.get("usage", {})
            return (name, "OK", f"HTTP {st} 回复「{reply}」tokens={usage.get('total_tokens')}")
        return (name, "FAIL", f"无 choices：{txt[:140]}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:160]
        return (name, "FAIL", f"HTTP {e.code} {detail}")
    except Exception as e:
        return (name, "FAIL", f"{type(e).__name__}: {e}")

results.append(check_deepseek("DeepSeek 主 Key", S.get("deepseek_api_key", "")))
results.append(check_deepseek("DeepSeek 备用 Key", S.get("deepseek_api_key_2", "")))

# ── 3. 量化系统密码 ──────────────────────────────────
# 注意：不要用 HTTP 试密码！quant/auth.py 里失败 5 次会永久封禁来源 IP。
# 正确做法是直接比对 auth.json（实际生效值）与 secrets（兜底值）是否一致。
pw = S.get("quant_password", "")
AUTH_JSON = os.path.expanduser("~/.dsh/dsh_wechat/quant/auth.json")
if not pw:
    results.append(("量化系统密码", "SKIP", "字段为空"))
elif not os.path.exists(AUTH_JSON):
    results.append(("量化系统密码", "OK", "无 auth.json，走 secrets 兜底值，直接生效"))
else:
    try:
        with open(AUTH_JSON, encoding="utf-8") as f:
            real = json.load(f)
        real_pw = str(real.get("password") or "")
        if real_pw == pw:
            results.append(("量化系统密码", "OK",
                            f"与 auth.json 一致（{len(pw)} 位，更新于 {real.get('updated', '?')}）"))
        else:
            results.append(("量化系统密码", "WARN",
                            f"secrets({len(pw)}位) ≠ auth.json({len(real_pw)}位)，实际以 auth.json 为准"))
    except Exception as e:
        results.append(("量化系统密码", "WARN", f"读 auth.json 失败：{e}"))

# ── 4. Server酱（可选） ──────────────────────────────
sk = S.get("serverchan_sendkey", "")
if not sk:
    results.append(("Server酱", "SKIP", "未配置（可选）"))
else:
    try:
        st, txt = post_json(f"https://sctapi.ftqq.com/{sk}.send",
                            {"title": "astock 密钥自检", "desp": "Server酱通道正常"})
        results.append(("Server酱", "OK" if st == 200 else "FAIL", f"HTTP {st}"))
    except Exception as e:
        results.append(("Server酱", "FAIL", f"{type(e).__name__}: {e}"))

# ── 汇总 ─────────────────────────────────────────────
print("=" * 74)
print(f"{'项目':<20}{'结果':<8}说明")
print("-" * 74)
bad = 0
for name, status, detail in results:
    mark = {"OK": "✓", "FAIL": "✗", "SKIP": "-", "WARN": "!"}.get(status, "?")
    print(f"{mark} {name:<18}{status:<8}{detail}")
    if status == "FAIL":
        bad += 1
print("=" * 74)
print(f"结论：{len(results)} 项中 {sum(1 for r in results if r[1]=='OK')} 项通过，{bad} 项失败")
sys.exit(1 if bad else 0)
