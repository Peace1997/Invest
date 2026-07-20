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
fi

exit "$refresh_rc"
