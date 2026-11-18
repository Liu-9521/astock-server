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


## 密钥管理（重要）

所有脚本的密钥都从 **`~/.dsh/secrets.json`** 读取，代码里没有任何硬编码密钥。

格式见 `astock/secrets.example.json`：

```json
{
  "wecom_webhook_key": "企业微信机器人 key（webhook 地址里 ?key= 后面那串）",
  "deepseek_api_key": "DeepSeek 主 Key（sk- 开头）",
  "deepseek_api_key_2": "DeepSeek 备用 Key",
  "quant_password": "量化系统 Web 登录密码",
  "serverchan_sendkey": "Server酱 SendKey（可选）"
}
```

部署时：

```bash
cp astock/secrets.example.json ~/.dsh/secrets.json
chmod 600 ~/.dsh/secrets.json
# 然后编辑填真实值
```

读取工具是 `astock/secrets_lib.py`（部署到 `~/.dsh/secrets_lib.py`），
各脚本通过 `_secret("键名")` 取值。

**换密钥时只改 secrets.json 一处，不用动代码。**

例外：`systemd/astock-web.service` 和 `systemd/dsh-web.service` 里仍有
`Environment=DEEPSEEK_API_KEY=...`（systemd 读不了 json），改这两个要
`systemctl daemon-reload` 并重启服务。

## 每日自动同步

见 `wechat/git_sync.sh`（每天 3:20 自动 commit + push）。
