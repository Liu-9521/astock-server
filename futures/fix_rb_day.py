import json
import sys
from pathlib import Path

P = Path("/home/astock/.dsh/dsh_wechat/futures/futures_data.json")
d = json.loads(P.read_text(encoding="utf-8"))
rb = d["strategy"].get("RB")
if not rb:
    print("找不到 RB 策略配置")
    sys.exit(1)
old = rb.get("period")
rb["period"] = "day"
P.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"RB.period: {old} -> {rb['period']}")

for k, v in d["strategy"].items():
    print(f"  {k:<8} {v['params']['fast']}/{v['params']['slow']:<3} {v['period']:<4} "
          f"过滤{v.get('filter', 0):<4} {v['stop_win']}/{v['stop_loss']} 启用={v['enabled']}")
