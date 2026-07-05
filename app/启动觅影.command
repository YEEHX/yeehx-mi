#!/bin/bash
# 玩椰 YEEHX · 觅影 —— 双击启动（后台运行版）
# 行为：已在运行 → 直接开浏览器；没在运行 → 装/查依赖（没变就跳过）→ 后台起服务
#       → 等健康检查通过 → 开浏览器 → 本窗口自动关闭。
# 服务在后台持续运行（空闲只占几十 MB），日志在 app/out/app.log；
# 要停止 / 重启：双击「停止觅影.command」（再双击本文件即重启）。原素材全程只读。
set -e
cd "$(dirname "$0")"          # app/
ROOT="$(cd .. && pwd)"        # 仓库根（含 app/ 包）
APP_DIR="$(pwd)"
PORT=8788
LOG="$APP_DIR/out/app.log"
PIDF="$APP_DIR/out/server.pid"

# 记住本窗口 id，结束时自动关掉（仅 Terminal.app；其他终端只会停在"已完成"）
WIN_ID=$(osascript -e 'tell application "Terminal" to id of front window' 2>/dev/null || true)

close_window_and_exit() {
  if [ -n "$WIN_ID" ]; then
    ( sleep 0.4; osascript -e "tell application \"Terminal\" to close (every window whose id is $WIN_ID)" ) >/dev/null 2>&1 &
  fi
  exit 0
}

alive() { curl -s -m 2 "http://127.0.0.1:$PORT/api/health" 2>/dev/null | grep -q '"ok"'; }

echo "=== 玩椰 YEEHX · 觅影 ==="

# 0) 已经在运行？直接开浏览器走人
if alive; then
  echo "觅影已在运行 → http://127.0.0.1:$PORT （要重启请先双击「停止觅影.command」）"
  open "http://127.0.0.1:$PORT"
  close_window_and_exit
fi

# 1) Python3，且必须 ≥ 3.10（老 mac 系统自带 3.9 会在别处报错，这里先拦住）
if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3，正在唤起苹果命令行工具安装窗口…"
  xcode-select --install || true
  echo "装完后请再双击一次本文件。"; read -n1 -p "按任意键退出"; exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)'; then
  echo "你的 python3 太老（$(python3 -V 2>&1)），觅影需要 Python ≥ 3.10。"
  echo "安装新版：https://www.python.org/downloads/macos/ 或 brew install python"
  read -n1 -p "按任意键退出"; exit 1
fi

# 2) 虚拟环境 + 依赖（requirements.txt 没变就整段跳过，启动快几秒到几十秒）
VENV="$APP_DIR/.venv"
if [ ! -d "$VENV" ]; then
  echo "首次运行：创建虚拟环境…"
  python3 -m venv "$VENV"
fi
source "$VENV/bin/activate"
REQ_HASH=$(shasum -a 256 "$APP_DIR/requirements.txt" | awk '{print $1}')
HASH_FILE="$VENV/.req_hash"
MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
if [ ! -f "$HASH_FILE" ] || [ "$(cat "$HASH_FILE" 2>/dev/null)" != "$REQ_HASH" ]; then
  echo "检查/安装依赖（首次约 1-3 分钟，取决于网速；之后启动秒开）…"
  pip install --upgrade pip >/dev/null 2>&1 || true
  # 先默认源；失败自动换清华镜像重试一次（国内网络常见问题，不用你操作）
  if ! pip install -r "$APP_DIR/requirements.txt" 2>"$APP_DIR/pip_err.log"; then
    echo "默认源安装失败，自动换国内镜像重试…"
    pip install -i "$MIRROR" -r "$APP_DIR/requirements.txt" 2>>"$APP_DIR/pip_err.log" || {
      echo ""
      echo "✗ 依赖安装失败（两个源都试过了）。常见原因：网络不通 / 代理拦截。"
      echo "  报错细节在 app/pip_err.log，把它发给玩椰即可。"
      read -n1 -p "按任意键退出"; exit 1; }
  fi
  # 可选组件：装不上不影响启动（HEIC=iPhone照片；pywebview=独立窗口，缺了走浏览器）
  pip install pillow-heif >/dev/null 2>&1 || pip install -i "$MIRROR" pillow-heif >/dev/null 2>&1 || true
  pip install "pywebview>=5.0" >/dev/null 2>&1 || pip install -i "$MIRROR" "pywebview>=5.0" >/dev/null 2>&1 || true
  echo "$REQ_HASH" > "$HASH_FILE"
  echo "依赖就绪。"
else
  echo "依赖未变化，跳过安装。"
fi

# 3) 清掉没退干净的旧实例：只按 PID 文件杀自己人，绝不 pkill 误伤其他项目
if [ -f "$PIDF" ]; then
  OLD=$(cat "$PIDF" 2>/dev/null || true)
  if [ -n "$OLD" ] && ps -p "$OLD" -o command= 2>/dev/null | grep -q "uvicorn app.main:app"; then
    echo "发现上次残留的服务进程（PID $OLD），先停掉…"
    kill "$OLD" 2>/dev/null || true; sleep 1
    kill -9 "$OLD" 2>/dev/null || true
  fi
  rm -f "$PIDF"
fi
# 端口被占但不是我们的 PID 文件记录（比如 MCP 自动拉起的实例）：验明正身后接管
PORT_PID=$(lsof -ti tcp:$PORT 2>/dev/null || true)
if [ -n "$PORT_PID" ] && ps -p "$PORT_PID" -o command= 2>/dev/null | grep -q "uvicorn app.main:app"; then
  echo "端口 $PORT 上有一个旧的觅影实例（PID $PORT_PID），先停掉…"
  kill "$PORT_PID" 2>/dev/null || true; sleep 1
  kill -9 "$PORT_PID" 2>/dev/null || true
fi

# 4) 启动（日志进 app/out/app.log，超 5MB 截一刀）
cd "$ROOT"
mkdir -p "$APP_DIR/out"
if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
  tail -c 1048576 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
# 局域网访问（设置页开关）：开了就绑 0.0.0.0，同 WiFi 手机可用带口令地址访问
HOST=127.0.0.1
if grep -q '"lan_access"[[:space:]]*:[[:space:]]*true' "$APP_DIR/out/app_settings.json" 2>/dev/null; then
  HOST=0.0.0.0
  echo "已开启局域网访问：手机地址见 设置页「手机/局域网访问」（带口令）"
fi
# 独立窗口模式（默认开）：设置页关了、或 pywebview 没装上 → 回落浏览器模式
WINDOW=1
grep -q '"window_mode"[[:space:]]*:[[:space:]]*false' "$APP_DIR/out/app_settings.json" 2>/dev/null && WINDOW=0
python -c 'import webview' >/dev/null 2>&1 || WINDOW=0
# 残留的旧窗口进程也验明正身收掉（desktop 会自带服务子进程）
for P in $(ps ax -o pid= -o command= 2>/dev/null | grep "[a]pp.desktop" | awk '{print $1}'); do
  kill "$P" 2>/dev/null || true
done
if [ "$WINDOW" = "1" ]; then
  echo "启动觅影（独立窗口）…（日志：app/out/app.log）"
  YEEHX_PORT=$PORT nohup python -m app.desktop >>"$LOG" 2>&1 &
  APP_PID=$!
  disown "$APP_PID" 2>/dev/null || true
else
  echo "启动服务 http://127.0.0.1:$PORT …（日志：app/out/app.log）"
  nohup python -m uvicorn app.main:app --host $HOST --port $PORT >>"$LOG" 2>&1 &
  SRV_PID=$!
  disown "$SRV_PID" 2>/dev/null || true
  echo "$SRV_PID" > "$PIDF"
fi

# 5) 等服务就绪（最多 60 秒）；窗口模式下窗口会自己弹出，浏览器模式才 open
for i in $(seq 1 60); do
  if alive; then
    if [ "$WINDOW" = "1" ]; then
      echo "觅影已就绪，窗口即将弹出。关闭窗口=退出觅影；本终端窗口自动关闭。"
    else
      echo "服务已就绪，打开浏览器。本窗口将自动关闭；停止服务请双击「停止觅影.command」。"
      open "http://127.0.0.1:$PORT"
    fi
    close_window_and_exit
  fi
  # 窗口模式下 desktop 进程若已死，提前报错（不用干等 60 秒）
  if [ "$WINDOW" = "1" ] && ! ps -p "$APP_PID" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo ""
echo "✗ 60 秒内服务没起来。最近日志："
echo "──────────────────────────────"
tail -n 30 "$LOG" 2>/dev/null || true
echo "──────────────────────────────"
echo "完整日志在 app/out/app.log，把上面的报错发我即可。"
read -n1 -p "按任意键退出"
exit 1
