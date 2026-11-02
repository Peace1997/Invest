#!/bin/zsh
# macOS launchd entry: refresh all dashboard market datasets after the close.
set -uo pipefail

PROJECT_DIR="${0:A:h:h}"
PYTHON="$PROJECT_DIR/.venv/bin/python"
DASHBOARD_SERVICE="gui/$(id -u)/com.mapeixin.trading.dashboard"
cd "$PROJECT_DIR" || exit 1
mkdir -p logs

echo "════════ market refresh $(date '+%F %T %A') ════════"

dashboard_was_running=0
if pgrep -f 'streamlit run .*ashare/ui/app.py' >/dev/null; then
    dashboard_was_running=1
    echo "Stopping Trading dashboard to release DuckDB…"
    pkill -f 'streamlit run .*ashare/ui/app.py' || true
    for _ in {1..10}; do
        pgrep -f 'streamlit run .*ashare/ui/app.py' >/dev/null || break
        sleep 1
    done
    if pgrep -f 'streamlit run .*ashare/ui/app.py' >/dev/null; then
        echo "Trading dashboard did not stop; refusing to open DuckDB or start a duplicate server."
        exit 4
    fi
fi

"$PYTHON" -m ashare.cli market-refresh
refresh_rc=$?
echo "market-refresh exit code: $refresh_rc"

if (( dashboard_was_running )); then
    # 2026-10-23: launchd 服务 com.mapeixin.trading.dashboard 已被用户删除
    # （它没有 RunAtLoad/KeepAlive/StartCalendarInterval，是"注册了但永远起不来"的死配置）。
    # 所以这里必须先判断服务是否还存在——否则按旧逻辑会 kickstart 失败 → exit 5，
    # 把"界面没重启"误报成"刷新失败"，而且用户手工开的界面已经被 pkill 掉了。
    if launchctl print "$DASHBOARD_SERVICE" >/dev/null 2>&1; then
        launchctl kickstart -k "$DASHBOARD_SERVICE"
        for _ in {1..15}; do
            pgrep -f 'streamlit run .*ashare/ui/app.py' >/dev/null && break
            sleep 1
        done
        if ! pgrep -f 'streamlit run .*ashare/ui/app.py' >/dev/null; then
            echo "Market data refreshed, but the Trading dashboard failed to restart."
            exit 5
        fi
        echo "Trading dashboard restarted at http://127.0.0.1:8501/"
    else
        echo "⚠ 数据已刷新，但检测到手工启动的 Streamlit 且 launchd 服务已不存在（2026-10-23 删除）。"
        echo "  请自行重启界面：cd $(pwd) && .venv/bin/python -m streamlit run src/ashare/ui/app.py"
        echo "  （不算失败：数据刷新本身 rc=$refresh_rc）"
    fi
fi

exit "$refresh_rc"
