#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""uptime-kuma 守护：如果数据库里又出现「端口 7892」监控，就停 kuma 删掉再启动。

背景：7892 是 mihomo 的 redir 端口（已禁用不监听），但 kuma 里若存在这个端口监控，
会持续探测 172.17.0.1:7892，历史上一探测就触发 mihomo 自循环（5 万连接、CPU 100%）。

每 10 分钟由 cron 跑一次；kuma 没运行时跳过。
"""
import subprocess
import sqlite3
import time

DB = "/var/lib/docker/volumes/uptime-kuma/_data/kuma.db"
LOG = "/opt/astock/kuma_guard.log"


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def kuma_running():
    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}",
                            "uptime-kuma"],
                           capture_output=True, text=True, timeout=10)
        # 普通用户可能没权限，退化为状态检查
        return "true" in (r.stdout or "").lower()
    except Exception:  # noqa: BLE001
        return False


def has_7892():
    try:
        con = sqlite3.connect(DB)
        n = con.execute("select count(*) from monitor where port=7892").fetchone()[0]
        con.close()
        return n > 0
    except Exception:  # noqa: BLE001
        return False


def main():
    if not has_7892():
        return
    log("⚠️ kuma 数据库里又出现 port=7892 监控，开始清理")
    try:
        subprocess.run(["docker", "stop", "uptime-kuma"], capture_output=True, timeout=60)
        time.sleep(2)
        con = sqlite3.connect(DB)
        n = con.execute("delete from monitor where port=7892").rowcount
        con.commit()
        con.close()
        log(f"  已删除 {n} 条 7892 监控")
        subprocess.run(["docker", "start", "uptime-kuma"], capture_output=True, timeout=60)
        log("  kuma 已重启")
    except Exception as exc:  # noqa: BLE001
        log(f"  清理失败: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    _rc = 0
    try:
        main()
    except Exception as _exc:
        _rc = 1
        raise
    finally:
        try:
            import sys as _s
            _s.path.insert(0, "/opt/astock")
            from db_log import log_job as _lj
            _lj("kuma_guard", "ok" if _rc == 0 else "fail")
        except Exception:
            pass
