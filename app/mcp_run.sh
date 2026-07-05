#!/bin/bash
# 觅影 MCP server 启动壳 —— 给 Hermes 等外部 Agent 的 mcp_servers.command 用。
# 注意：stdout 是 MCP 协议通道，这里所有提示/安装输出一律转到 stderr。
set -e
cd "$(dirname "$0")"            # app/
ROOT="$(cd .. && pwd)"

if [ ! -d ".venv" ]; then
  echo "[觅影MCP] 没找到 app/.venv —— 请先双击运行一次「启动觅影.command」把环境装好" >&2
  exit 1
fi
source ".venv/bin/activate"

# 依赖自检：fastmcp 或 requests 任一缺失（首次/上次装一半）→ 整套补齐
# requirements.txt 里已含 fastmcp，补一次全好。输出全走 stderr，stdout 留给 MCP 协议。
if ! python -c "import fastmcp, requests" 2>/dev/null; then
  echo "[觅影MCP] 依赖不全，正在按 requirements.txt 补装（首次较慢，请等待）…" >&2
  pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple 1>&2 \
    || pip install -q -r requirements.txt 1>&2
fi

cd "$ROOT"
exec python -m app.mcp_server
