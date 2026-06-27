#!/usr/bin/env bash
# 部署代码到云服务器(腾讯云). 在本地运行: bash scripts/deploy.sh
# 关键: 不碰服务器上独立维护的东西 —— 数据仓库(data)、虚拟环境(.venv)、
#   持仓(positions.yaml 服务器为准, 网页可编辑)、密钥(*.key/token)。
# 这样既不会用本地旧持仓覆盖服务器网页改的持仓, 也不会动密钥权限。
set -euo pipefail
SRV="mpx@49.51.75.72"
KEY="/home/mpx/.ssh/id_ed25519_tc"
cd "$(dirname "$0")/.."
rsync -az -e "ssh -i $KEY" \
  --exclude data --exclude .venv --exclude .git --exclude __pycache__ --exclude '*.pyc' \
  --exclude positions.yaml \
  --exclude .anthropic_key --exclude .tushare_token --exclude .serverchan_key \
  ./ "$SRV:~/ashare-tool/"
ssh -i "$KEY" "$SRV" 'cd ~/ashare-tool && ~/.local/bin/uv sync -q && chmod +x scripts/*.sh && echo "=== deploy ok ==="'
