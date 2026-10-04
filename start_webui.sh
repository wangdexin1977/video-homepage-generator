#!/bin/bash
# 视频首页 MP4 生成器：使用独立环境，确认服务健康后才报告成功。
set -eu
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
PORT="${1:-8090}"
URL="http://127.0.0.1:$PORT"
PYTHON="$(pwd)/.venv/bin/python"
healthy() {
  curl --noproxy '*' -fsS --max-time 2 "$URL/api/jobs" 2>/dev/null |
    "$PYTHON" -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if isinstance(d.get("jobs"),list) else 1)' 2>/dev/null
}
if [ ! -x "$PYTHON" ]; then
  echo "启动失败：缺少独立环境 $PYTHON，请先创建 .venv 并安装 requirements.txt。" >&2
  exit 1
fi
if ! "$PYTHON" -c 'import flask, PIL, markupsafe' > /dev/null 2>&1; then
  echo "启动失败：Python 依赖不完整，请使用 .venv/bin/python -m pip install -r requirements.txt 修复。" >&2
  exit 1
fi
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  if healthy; then
    echo "WebUI 已在运行：$URL"
    exit 0
  fi
  echo "启动失败：端口 $PORT 被其他服务占用或服务未正常响应。" >&2
  exit 1
fi
for bin in ffmpeg ffprobe; do
  command -v "$bin" >/dev/null || { echo "启动失败：找不到 $bin。" >&2; exit 1; }
done
nohup "$PYTHON" -u webui.py --port "$PORT" > webui.log 2>&1 < /dev/null &
PID=$!
for ((i=0; i<30; i++)); do
  if ! kill -0 "$PID" 2>/dev/null; then
    break
  fi
  if healthy; then
    echo "WebUI 已启动：$URL (PID $PID)"
    exit 0
  fi
  sleep 0.5
done
echo "启动未成功：请检查 $(pwd)/webui.log" >&2
tail -20 webui.log >&2
exit 1
