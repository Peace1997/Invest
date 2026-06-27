#!/usr/bin/env bash
# 关掉看板后, 一键全量刷新仓库 —— 省得记一串命令。
#
#   行情日更(daily) → 持仓估值/快照/净值(pf) → 稳健版 质量+估值回填(value-backfill)
#
# 看板(ui)开着会独占锁住 DuckDB, 写入会失败。所以请先关掉看板再跑本脚本;
# 若看板还开着, 第一步 daily 会清楚地提示 "数据库被占用" 并退出。
#
# 用法:
#   bash scripts/refresh.sh           # 估值只回填沪深300(~10-15min)
#   bash scripts/refresh.sh --full    # 估值回填全主板(~数小时, 质量始终全board)
set -euo pipefail
cd "$(dirname "$0")/.."

FULL=""
[[ "${1:-}" == "--full" ]] && FULL="--full"

ts() { date '+%F %T'; }
echo "════════ 全量刷新开始 $(ts) ════════"
echo "（看板必须已关闭; 否则 daily 会因 DB 被锁而退出）"

echo
echo "▶ 1/3 行情日更 daily —— 更新到最新交易日, 修正'现价/评分陈旧'"
uv run python -m ashare.cli daily

echo
echo "▶ 2/3 持仓 pf —— 基金净值 + 估值 + 今日快照(收益曲线)"
uv run python -m ashare.cli pf

echo
echo "▶ 3/3 稳健版 value-backfill —— 全主板质量(一次调用) + 估值(PE/PB) ${FULL:-(默认沪深300)}"
uv run python -m ashare.cli value-backfill ${FULL}

echo
echo "════════ 全量刷新完成 $(ts) ════════"
echo "现在可重开看板:  uv run python -m ashare.cli ui"
