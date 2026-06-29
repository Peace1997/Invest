#!/usr/bin/env bash
# 开盘竞价短线分析 cron 入口: 工作日 9:28 跑(竞价临近收敛, 数据更全), 9:30 前推送。
# 交易日门控在 cli premarket 内查 calendar。免抢锁(只读)。短线投机线索, 非验证策略。
# crontab(服务器): 28 9 * * 1-5 bash ~/ashare-tool/scripts/premarket.sh
export PATH="/home/mpx/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
uv run python -m ashare.cli premarket >> "logs/premarket-$(date +%Y%m).log" 2>&1
