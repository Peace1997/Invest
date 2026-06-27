#!/usr/bin/env bash
# 尾盘半小时短线 cron 入口: 工作日 14:30 跑(收盘前半小时), 推送当日尾盘强势候选。
# 交易日门控在 cli endday 内查 calendar。免抢锁(只读)。短线投机线索, 非验证策略。
export PATH="/home/mpx/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
uv run python -m ashare.cli endday >> "logs/endday-$(date +%Y%m).log" 2>&1
