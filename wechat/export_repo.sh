#!/bin/bash
# 把服务器上的代码/配置汇总到一个 git 仓库目录（强制脱敏）
REPO="/home/astock/astock-repo"
DSH="/home/astock/.dsh/dsh_wechat"

mkdir -p "$REPO"/{astock,wechat,futures,quant,systemd}
rm -f "$REPO"/astock/* "$REPO"/wechat/* "$REPO"/futures/* "$REPO"/quant/* "$REPO"/systemd/* 2>/dev/null

cp -f /opt/astock/*.py "$REPO/astock/" 2>/dev/null
cp -f /opt/astock/*.sh "$REPO/astock/" 2>/dev/null
cp -f /opt/astock/backup.sh "$REPO/astock/" 2>/dev/null
cp -f "$DSH"/*.py "$REPO/wechat/" 2>/dev/null
cp -f "$DSH"/*.sh "$REPO/wechat/" 2>/dev/null
cp -f "$DSH"/futures/*.py "$REPO/futures/" 2>/dev/null
cp -f "$DSH"/futures/futures_data.json "$REPO/futures/" 2>/dev/null
cp -f "$DSH"/quant/*.py "$REPO/quant/" 2>/dev/null
cp -f /etc/systemd/system/*.service /etc/systemd/system/*.timer "$REPO/systemd/" 2>/dev/null
cp -f /etc/sudoers.d/astock-* /etc/sudoers.d/dsh-web "$REPO/systemd/" 2>/dev/null
crontab -l > "$REPO/crontab.txt" 2>/dev/null

# ---- 脱敏 1：config.json → config.example.json ----
python3 - "$REPO" <<'PYEOF'
import re, sys, pathlib
repo = pathlib.Path(sys.argv[1])
src = pathlib.Path("/opt/astock/config.json")
if src.exists():
    txt = src.read_text(encoding="utf-8", errors="replace")
    # 键名敏感的字符串值 → 整体替换为 ***
    txt = re.sub(
        r'("([A-Za-z_]*(?:password|passwd|token|secret|apikey|api_key|sendkey|webhook|corpsecret|key)[A-Za-z_]*)"\s*:\s*)"[^"]*"',
        lambda m: f'{m.group(1)}"***"', txt, flags=re.IGNORECASE)
    # URL 里的 key=... 参数
    txt = re.sub(r'(key=)[0-9a-fA-F-]{12,}', r'\1***', txt)
    txt = re.sub(r'(sk-)[A-Za-z0-9]{16,}', r'\1***', txt)
    (repo / "astock" / "config.example.json").write_text(txt, encoding="utf-8")
    print("config.example.json 已生成（全部打码）")
PYEOF

# ---- 脱敏 2：所有脚本里的硬编码密钥 ----
echo "=== 打码脚本中的硬编码密钥 ==="
BEFORE=$(grep -rlE "key=[0-9a-f]{8}-[0-9a-f]{4}|sk-[A-Za-z0-9]{20,}" "$REPO" 2>/dev/null | wc -l)
find "$REPO" -type f \( -name "*.py" -o -name "*.sh" -o -name "*.json" -o -name "*.txt" \) -print0 2>/dev/null \
  | xargs -0 sed -i \
      -e 's/key=[0-9a-f]\{8\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{12\}/key=<WECOM_YOUR_KEY>/g' \
      -e 's/sk-[A-Za-z0-9]\{20,\}/<DEEPSEEK_YOUR_KEY>/g' \
      -e 's/SCT[0-9]\{5,\}/<SCT_YOUR_SENDKEY>/g' 2>/dev/null
AFTER=$(grep -rlE "key=[0-9a-f]{8}-[0-9a-f]{4}|sk-[A-Za-z0-9]{20,}" "$REPO" 2>/dev/null | wc -l)
echo "打码前含密钥文件数: $BEFORE → 打码后: $AFTER"

# ---- .gitignore / README ----
cat > "$REPO/.gitignore" <<'EOF'
*.key
*.pem
.secrets/
.env
__pycache__/
*.pyc
*.log
logs/
reports/
*.tar.gz
iptv/
媒体/
bin/
tools/
assets/
node_modules/
venv/
.venv*/
EOF

cat > "$REPO/README.md" <<'EOF'
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
EOF

# FALLBACK_MASK 兜底：全仓库所有文本文件再打一遍（含 .service / .conf 等）
find "$REPO" -type f -not -path "*/.git/*" -print0 2>/dev/null | xargs -0 sed -i \
  -e 's/key=[0-9a-f]\{8\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{12\}/key=<WECOM_YOUR_KEY>/g' \
  -e 's/sk-[A-Za-z0-9]\{20,\}/<DEEPSEEK_YOUR_KEY>/g' \
  -e 's/SCT[0-9]\{5,\}/<SCT_YOUR_SENDKEY>/g' \
  -e 's/<QUANT_WEB_PASSWORD>/<QUANT_WEB_PASSWORD>/g' 2>/dev/null

echo "=== 统计 ==="
cd "$REPO" || exit 1
find . -type f -not -path "./.git/*" | wc -l
du -sh "$REPO" 2>/dev/null
