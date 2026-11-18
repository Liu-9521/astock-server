# astock 服务器代码仓库

astock 服务器的**代码与配置**备份（不含媒体、数据、真实密钥）。

## 目录

| 目录 | 原路径 |
|---|---|
| `astock/` | `/opt/astock/` 的 Python/shell 脚本 |
| `wechat/` | `/home/astock/.dsh/dsh_wechat/` 的脚本 |
| `futures/` | 期货模拟盘 `futures_sim.py` + 数据 |
| `quant/` | 量化推送脚本 |
| `systemd/` | systemd 单元与 sudoers 规则 |
| `crontab.txt` | astock 用户定时任务 |

## 密钥说明（重要）

仓库中所有密钥已替换为占位符：

- `<WECOM_YOUR_KEY>` —— 企业微信机器人 webhook key
- `<DEEPSEEK_YOUR_KEY>` —— DeepSeek API key
- `<SCT_YOUR_SENDKEY>` —— Server酱 SendKey

**部署前必须填回真实值**，真实值只存在于服务器配置和每日备份 tar 中。

## 恢复流程

1. 新机器装 Python3 + systemd
2. `git clone` 本仓库，把 `astock/`、`wechat/`、`futures/`、`quant/` 放回对应路径
3. 从备份 tar / 网盘恢复 `config.json`、`futures_data.json`、日志、报表
4. 若从仓库代码启动，先把占位符替换成真实密钥
5. `cp systemd/*.service /etc/systemd/system/ && systemctl daemon-reload`
6. `crontab crontab.txt`

## 每日自动同步

见 `wechat/git_sync.sh`（每天 3:20 自动 commit + push）。
