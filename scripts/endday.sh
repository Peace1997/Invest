#!/usr/bin/env bash
# 低估值成长尾盘观察 cron 入口: 工作日 14:30 跑，以实时价复核早盘候选。
# 交易日门控在 cli endday 内查 calendar。价格来自带交易所时间戳的实时行情。
export PATH="/home/mpx/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
uv run python -m ashare.cli endday >> "logs/endday-$(date +%Y%m).log" 2>&1
