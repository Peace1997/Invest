#!/usr/bin/env bash
# 盘中预警 cron 入口: 交易时段每5分钟跑一次。精确时段/交易日门控在 cli alerts 内部
# (查 calendar 表 + 9:30-11:30/13:00-15:00), 这里只负责环境与日志。免 token。
export PATH="/home/mpx/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
uv run python -m ashare.cli alerts >> "logs/alerts-$(date +%Y%m).log" 2>&1
