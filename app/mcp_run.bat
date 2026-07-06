@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ── 觅影 MCP server 启动壳（Windows）—— 给 Hermes 等外部 Agent 的 MCP 配置用 ──
rem 注意：stdout 是 MCP 协议通道，所有提示/安装输出一律转到 stderr（1>&2）。
rem mac 版对应 mcp_run.sh，行为一致。

set "VPY=%~dp0.venv\Scripts\python.exe"

if exist "%VPY%" goto DEPS
echo [觅影MCP] 没找到 app\.venv —— 请先双击运行一次「启动觅影.bat」把环境装好 1>&2
exit /b 1

:DEPS
"%VPY%" -c "import fastmcp, requests" >nul 2>&1
if not errorlevel 1 goto RUN
echo [觅影MCP] 依赖不全，正在按 requirements.txt 补装（首次较慢，请等待）… 1>&2
"%VPY%" -m pip install -q -r "%~dp0requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple 1>&2
if not errorlevel 1 goto RUN
"%VPY%" -m pip install -q -r "%~dp0requirements.txt" 1>&2

:RUN
cd /d "%~dp0.."
"%VPY%" -m app.mcp_server
