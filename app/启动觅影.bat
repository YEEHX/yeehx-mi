@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 玩椰 YEEHX · 觅影

rem ── 玩椰 YEEHX · 觅影 —— Windows 双击启动 ──
rem 行为与 mac 版「启动觅影.command」一致：已在运行 → 直接开界面；
rem 没在运行 → 装/查依赖（没变就跳过）→ 后台起服务 → 开窗口/浏览器 → 本窗口自动关闭。
rem 日志在 app\out\app.log；要停止/重启：双击「停止觅影.bat」。原素材全程只读。

rem ── 1) 找 Python（需要 3.10 及以上）：先试 py 启动器，再试 python ──
set "PYCMD="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD goto HAVEPY
python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=python"
if not defined PYCMD goto NOPYTHON
:HAVEPY

rem ── 2) 虚拟环境（只建一次）──
if exist ".venv\Scripts\python.exe" goto HAVEVENV
echo 首次运行：创建虚拟环境…
%PYCMD% -m venv .venv
if errorlevel 1 goto VENVFAIL
:HAVEVENV

rem ── 3) 其余交给 winlaunch.py（装依赖/清残留/启动/开浏览器）──
".venv\Scripts\python.exe" winlaunch.py %*
if errorlevel 1 goto FAIL
exit /b 0

:NOPYTHON
echo.
echo 未找到 Python —— 觅影需要 Python 3.10 及以上（推荐 3.12/3.13）。
echo.
echo 安装方法（二选一）：
echo   1. 即将打开的 python.org 下载页里下载安装
echo      —— 安装第一屏务必勾选 "Add python.exe to PATH"
echo   2. 或在终端执行：winget install Python.Python.3.12
echo.
echo 装好后请再双击一次本文件。
start https://www.python.org/downloads/windows/
pause
exit /b 1

:VENVFAIL
echo.
echo 创建虚拟环境失败，请把上面的报错截图发给玩椰。
pause
exit /b 1

:FAIL
echo.
pause
exit /b 1
