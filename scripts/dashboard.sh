#!/usr/bin/env bash
# 启动 Streamlit 看板, 仅绑定 Tailscale 内网 IP(100.x) —— 不暴露公网。
# 取不到 Tailscale IP(最多等 ~60s, 给开机时 tailscaled 起来留时间)就拒绝启动,
# 绝不退化为 0.0.0.0 裸暴露。本地(无 tailscale)不用这个脚本, 用 `cli ui`。
export PATH="/home/mpx/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.." || exit 1

ADDR=""
for _ in $(seq 1 30); do
  ADDR=$(tailscale ip -4 2>/dev/null | head -1)
  [ -n "$ADDR" ] && break
  sleep 2
done
if [ -z "$ADDR" ]; then
  echo "✗ 未取到 Tailscale IP(先 sudo tailscale up)。为安全不绑 0.0.0.0, 退出。"
  exit 1
fi

mkdir -p logs
echo "═══ 看板启动 http://$ADDR:8501  $(date '+%F %T') ═══" >> logs/dashboard.log
exec uv run streamlit run src/ashare/ui/app.py \
  --server.address "$ADDR" --server.port 8501 \
  --server.headless true --browser.gatherUsageStats false >> logs/dashboard.log 2>&1
