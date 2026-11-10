#!/bin/bash
# RPF-Agent 容器启动（幂等：先删旧容器再起）。停服：docker stop rpf-agent（unless-stopped 尊重手动停止）。
# 前置：~/RPF-Agent/.env 与 ~/RPF-Agent/data/ 已就绪；镜像已 build：
#   cd ~/RPF-Agent && docker build -t rpf-agent:latest -f deploy/Dockerfile .
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

docker rm -f rpf-agent 2>/dev/null || true
docker run -d --name rpf-agent \
  --network host \
  --restart unless-stopped \
  --user "$(id -u):$(id -g)" \
  --env-file "$ROOT/.env" \
  -v "$ROOT/data:/app/data" \
  rpf-agent:latest

docker ps --filter name=rpf-agent
echo "日志：docker logs -f rpf-agent   停服：docker stop rpf-agent"
