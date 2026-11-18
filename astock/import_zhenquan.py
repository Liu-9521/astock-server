#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导入同花顺导出的持仓表 / 自选股，更新 config.json。

用法：
    python3 import_zhenquan.py table.xls [自选股_xxxx.hev]

规则：
    * 持仓表里的标的 -> 持仓池（带 cost / shares）
    * 自选股里不在持仓中的 -> 自选池（无 cost）
    * 保留 config.json 里其余配置（notify / report / gui 等）
    * 导入前自动备份为 config.json.bak
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "config.json"


def decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gbk", errors="replace")


def prefix(code: str) -> str:
    """给 6 位代码加市场前缀。"""
    c = (code or "").strip()
    if not c:
        return c
    if c[:1].isalpha():          # 已带前缀（sh/sz/bj）
        return c.lower()
    if c.isdigit():
        if c.startswith(("6", "5", "9")):
            return "sh" + c
        if c.startswith(("0", "3", "1", "2")):
            return "sz" + c
        if c.startswith(("4", "8")):
            return "bj" + c
    # 指数（如 1A0001 -> sh000001）
    if c.upper().startswith("1A"):
        return "sh" + "00" + c[2:]
    return c


def parse_holdings(path: Path):
    """解析持仓表（GBK 制表符文本）。"""
    text = decode(path.read_bytes())
    rows = []
    for idx, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        cols = [c.strip() for c in line.split("\t")]
        if idx == 0 or "证券代码" in line or "代码" == cols[0]:
            continue                      # 表头
        if len(cols) < 12:
            continue
        code = cols[0]
        if not code or not (code.isdigit() or code[:1].isalnum()):
            continue
        try:
            shares = float(cols[2] or 0)
            cost = float(cols[9] or 0)
        except ValueError:
            continue
        if shares <= 0:
            continue
        rows.append({
            "code": prefix(code),
            "name": cols[1] or code,
            "shares": shares,
            "cost": cost if cost > 0 else None,
        })
    return rows


def parse_watchlist(path: Path):
    """解析自选股（UTF-8 制表符文本）。"""
    text = decode(path.read_bytes())
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cols = [c.strip() for c in line.split("\t")]
        code = cols[0] if cols else ""
        if not code:
            continue
        name = cols[1] if len(cols) > 1 else ""
        rows.append({"code": prefix(code), "name": name or code})
    return rows


def main(argv=None):
    argv = argv or sys.argv[1:]
    if not argv:
        print("用法: python3 import_zhenquan.py <持仓表> [自选股文件]")
        return 2
    hold_path = Path(argv[0])
    watch_path = Path(argv[1]) if len(argv) > 1 else None
    if not hold_path.exists():
        print(f"找不到持仓表: {hold_path}")
        return 2

    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    groups = cfg.get("gui", {}).get("groups") or ["自选池", "持仓池"]
    watch_group = groups[0]
    hold_group = groups[1] if len(groups) > 1 else groups[0]

    holdings = parse_holdings(hold_path)
    watch = parse_watchlist(watch_path) if watch_path and watch_path.exists() else []
    old_names = {w.get("code"): w.get("name") for w in cfg.get("watchlist", [])}

    merged = {}
    for h in holdings:
        merged[h["code"]] = {
            "code": h["code"],
            "name": old_names.get(h["code"]) or h["name"],
            "group": hold_group,
            "cost": h["cost"],
            "shares": h["shares"],
        }
    for w in watch:
        if w["code"] in merged:
            continue
        merged[w["code"]] = {
            "code": w["code"],
            "name": old_names.get(w["code"]) or w["name"],
            "group": watch_group,
        }

    shutil.copy2(CFG_PATH, str(CFG_PATH) + ".bak")
    cfg["watchlist"] = list(merged.values())
    CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"导入完成：持仓 {len(holdings)} 只，自选 {len(watch)} 只，合计 {len(merged)} 只")
    print(f"已备份原配置: {CFG_PATH}.bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
