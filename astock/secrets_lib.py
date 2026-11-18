#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一密钥读取模块。

所有脚本从这里取密钥，不再硬编码。
真实值只存在 ~/.dsh/secrets.json（权限 600，不入 git）。
"""
import json
import os

_PATH = os.path.expanduser("~/.dsh/secrets.json")
_cache = None


def _load():
    global _cache
    if _cache is None:
        try:
            with open(_PATH, encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
    return _cache


def get(name, default=""):
    """读取一个密钥，缺失时返回 default。"""
    v = _load().get(name)
    return str(v) if v else default


def wecom_url(default_key=""):
    """企业微信机器人 webhook 完整地址。"""
    key = get("wecom_webhook_key", default_key)
    if not key:
        return ""
    return "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + key


def deepseek_key(which="main"):
    """DeepSeek API key（main / alt）。"""
    return get("deepseek_api_key" if which == "main" else "deepseek_api_key_2")
