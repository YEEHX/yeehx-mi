@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 停止觅影

rem ── 玩椰 YEEHX · 觅影 —— 双击停止后台服务 ──
rem 只按 PID/命令行验明正身的觅影进程来停，绝不误杀其他项目（逻辑在 winlaunch.py --stop）。

if exist ".venv\Scripts\python.exe" goto RUN
echo 觅影环境还没装过（没有 app\.venv），本来就没在运行。
timeout /t 3 >nul
exit /b 0

:RUN
".venv\Scripts\python.exe" winlaunch.py --stop
timeout /t 3 >nul
exit /b 0
