#!/bin/bash
# 视频首页 MP4 生成器 WebUI 启动脚本
# 用法: ./start_webui.sh [端口]   默认 8090
cd "$(dirname "$0")"
PORT="${1:-8090}"
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 $PORT 已被占用，服务可能已在运行：http://127.0.0.1:$PORT"
  exit 1
fi
nohup python3 webui.py --port "$PORT" > webui.log 2>&1 &
echo "✅ WebUI 已启动：http://127.0.0.1:$PORT  (PID $!)"
echo "日志：$(pwd)/webui.log"
