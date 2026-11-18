#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MEMORY.md 里敏感信息的加解密小工具。

用法：
    python3 mem_crypt.py encrypt "要加密的原文"      # 输出密文 token
    python3 mem_crypt.py decrypt <token>             # 还原原文
    python3 mem_crypt.py replace <旧行前缀> "新原文"  # 直接改 MEMORY.md（自动备份）
    python3 mem_crypt.py show                        # 列出 MEMORY.md 里所有密文并还原

密钥：~/.dsh/dsh_wechat/mem_key（首次自动生成，600 权限）
每次加密都用新的随机 IV，所以同样的内容每次密文都不一样（"多变"）。
"""
import base64
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

BASE = Path(__file__).resolve().parent
KEY = BASE / "mem_key"
MEMORY = Path("/home/astock/.dsh/MEMORY.md")
TAG = "[ENC]"


def get_key(create=True) -> bytes:
    if KEY.exists():
        return KEY.read_bytes()
    if not create:
        raise SystemExit("密钥不存在，先 encrypt 生成")
    k = Fernet.generate_key()
    KEY.write_bytes(k)
    os.chmod(KEY, 0o600)
    return k


def enc(text: str) -> str:
    return Fernet(get_key()).encrypt(text.encode("utf-8")).decode("ascii")


def dec(token: str) -> str:
    try:
        return Fernet(get_key()).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return "<解密失败：密钥不匹配>"


def cmd_encrypt(text):
    print(f"{TAG} {enc(text)}")


def cmd_decrypt(token):
    print(dec(token.replace(TAG, "").strip()))


def cmd_replace(prefix, text):
    if not MEMORY.exists():
        raise SystemExit("MEMORY.md 不存在")
    lines = MEMORY.read_text(encoding="utf-8").splitlines()
    hit = -1
    for i, l in enumerate(lines):
        if prefix in l:
            hit = i
            break
    if hit < 0:
        raise SystemExit(f"没找到包含「{prefix}」的行")
    old = lines[hit]
    head = old.split("：", 1)[0]          # 保留「- 日期：」
    lines[hit] = f"{head}：{TAG} {enc(text)}"
    bak = MEMORY.with_name(f"MEMORY.md.bak.{__import__('time').strftime('%Y%m%d-%H%M%S')}")
    bak.write_text(MEMORY.read_text(encoding="utf-8"), encoding="utf-8")
    MEMORY.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已替换第 {hit+1} 行，备份 {bak.name}")


def cmd_rotate():
    """把所有密文重新加密一遍（新 IV），密文每次都不一样。"""
    lines = MEMORY.read_text(encoding="utf-8").splitlines()
    changed = 0
    for i, l in enumerate(lines):
        if TAG not in l:
            continue
        head, token = l.split(TAG, 1)
        plain = dec(token.strip())
        if plain.startswith("<解密失败"):
            continue
        lines[i] = f"{head}{TAG} {enc(plain)}"
        changed += 1
    if changed:
        bak = MEMORY.with_name(f"MEMORY.md.bak.{__import__('time').strftime('%Y%m%d-%H%M%S')}")
        bak.write_text(MEMORY.read_text(encoding="utf-8"), encoding="utf-8")
        MEMORY.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已轮换 {changed} 条密文")


def cmd_show():
    for i, l in enumerate(MEMORY.read_text(encoding="utf-8").splitlines(), 1):
        if TAG in l:
            head, token = l.split(TAG, 1)
            print(f"{i}: {head.strip()} → {dec(token.strip())}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    c = sys.argv[1]
    if c == "encrypt":
        cmd_encrypt(sys.argv[2])
    elif c == "decrypt":
        cmd_decrypt(sys.argv[2])
    elif c == "replace":
        cmd_replace(sys.argv[2], sys.argv[3])
    elif c == "rotate":
        cmd_rotate()
    elif c == "show":
        cmd_show()
    else:
        raise SystemExit(__doc__)
