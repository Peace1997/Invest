#!/usr/bin/env bash
# cron 入口: 定时全量刷新仓库, 无人值守。
#   - cron 的 PATH 很干净 → 这里显式补上 uv 的路径
#   - 看板(streamlit)会独占锁住 DuckDB → 刷新前先停掉它(只停本项目的, 不碰其他进程)
#   - 全程写日志到 logs/refresh-YYYYMM.log
# 安装见文件末尾注释 / 由助手写入 crontab。手动测试: bash scripts/cron-refresh.sh
export PATH="/home/mpx/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.." || exit 1   # 本地/服务器通用, 不写死路径
mkdir -p logs
# 模式: full(默认, 20:00)=bars+pf+估值回填(慢); light(收盘后~15:15)=只 cli daily(bars+舆情, 快)
MODE="${1:-full}"
LOG="logs/refresh-$(date +%Y%m).log"
{
    echo
    echo "═══════════ cron 刷新[$MODE] $(date '+%F %T %A') ═══════════"
    if pgrep -f 'streamlit run .*ashare/ui/app.py' >/dev/null; then
        echo "检测到看板在运行 → 先停掉以释放数据库锁…"
        pkill -f 'streamlit run .*ashare/ui/app.py' || true
        sleep 3
    fi
    if [ "$MODE" = light ]; then
        uv run python -m ashare.cli daily      # 只更 bars + 舆情, 不跑慢的估值回填
        rc=$?
    else
        bash scripts/refresh.sh
        rc=$?
    fi
    echo "[cron-refresh] refresh[$MODE] 退出码: $rc"
    # 服务器(装了 tailscale)上刷新完重新拉起看板; 本地无 tailscale 则跳过(手动 cli ui)
    if command -v tailscale >/dev/null 2>&1; then
        setsid bash scripts/dashboard.sh </dev/null >/dev/null 2>&1 &
        echo "[cron-refresh] 看板已重新拉起 (tailscale 内网)"
    fi
} >> "$LOG" 2>&1
