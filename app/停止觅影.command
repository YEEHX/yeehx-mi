#!/bin/bash
# 玩椰 YEEHX · 觅影 —— 双击停止后台服务。
# 只按 PID 文件 / 端口上验明正身的 uvicorn app.main:app 来停，绝不误杀其他项目。
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
PORT=8788
PIDF="$APP_DIR/out/server.pid"

WIN_ID=$(osascript -e 'tell application "Terminal" to id of front window' 2>/dev/null || true)
close_window_and_exit() {
  if [ -n "$WIN_ID" ]; then
    ( sleep 1.2; osascript -e "tell application \"Terminal\" to close (every window whose id is $WIN_ID)" ) >/dev/null 2>&1 &
  fi
  exit 0
}

is_miying() { ps -p "$1" -o command= 2>/dev/null | grep -q "uvicorn app.main:app"; }

stop_pid() {
  kill "$1" 2>/dev/null || true
  for i in 1 2 3 4 5; do
    ps -p "$1" >/dev/null 2>&1 || return 0
    sleep 0.5
  done
  kill -9 "$1" 2>/dev/null || true
}

echo "=== 停止觅影 ==="
STOPPED=0

# 1) 按 PID 文件停
if [ -f "$PIDF" ]; then
  PID=$(cat "$PIDF" 2>/dev/null || true)
  if [ -n "$PID" ] && is_miying "$PID"; then
    stop_pid "$PID"; STOPPED=1
    echo "已停止觅影服务（PID $PID）。"
  fi
  rm -f "$PIDF"
fi

# 2) 独立窗口进程（python -m app.desktop）：验明正身后停（它会顺手带走自己的服务子进程）
for P in $(ps ax -o pid= -o command= 2>/dev/null | grep "[a]pp.desktop" | awk '{print $1}'); do
  stop_pid "$P"; STOPPED=1
  echo "已停止觅影窗口进程（PID $P）。"
done

# 3) 兜底：端口上还有别的觅影实例（比如 MCP 自动拉起的），验明正身后一并停
for P in $(lsof -ti tcp:$PORT 2>/dev/null); do
  if is_miying "$P"; then
    stop_pid "$P"; STOPPED=1
    echo "已停止端口 $PORT 上的觅影实例（PID $P）。"
  fi
done

if [ "$STOPPED" = "0" ]; then
  echo "觅影本来就没在运行。"
fi
echo "原素材与数据库不受影响；再次双击「启动觅影.command」即可重新启动。"
close_window_and_exit
