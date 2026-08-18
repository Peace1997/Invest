#!/usr/bin/env bash
# 低估值成长早盘观察 cron 入口: 工作日 9:27 跑(9:25 集合竞价结束后), 9:30 前推送。
# 交易日门控在 cli premarket 内查 calendar。价格来自带交易所时间戳的实时行情。
# crontab(服务器): 27 9 * * 1-5 bash ~/ashare-tool/scripts/premarket.sh
export PATH="/home/mpx/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
uv run python -m ashare.cli premarket >> "logs/premarket-$(date +%Y%m).log" 2>&1
