@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 玩椰 YEEHX · 觅影

rem ── 玩椰 YEEHX · 觅影 —— Windows 双击启动 ──
rem 行为与 mac 版「启动觅影.command」一致：已在运行 → 直接开界面；
rem 没在运行 → 装/查依赖（没变就跳过）→ 后台起服务 → 开窗口/浏览器 → 本窗口自动关闭。
rem 日志在 app\out\app.log；要停止/重启：双击「停止觅影.bat」。原素材全程只读。
rem
rem Python 查找顺序（v2.1.2，用户什么都不用装）：
rem   自带便携版 runtime\python → 系统 py/python（≥3.10）→ 自动下载便携版（国内镜像，
rem   约 21MB，只进觅影自己的文件夹，不碰系统）→ 全失败才打开浏览器引导手动安装

rem ── 1) 找 Python ──
set "PYCMD="
if exist "runtime\python\python.exe" (
  "runtime\python\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
  if not errorlevel 1 set "PYCMD=runtime\python\python.exe"
)
if defined PYCMD goto HAVEPY
py -3 -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD goto HAVEPY
python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=python"
if defined PYCMD goto HAVEPY

rem ── 1b) 没有就自动下载便携版（国内镜像秒下；gzip 自带校验，解不开=没下完整） ──
echo 没找到 Python —— 自动下载便携版（约 21MB，只装进觅影自己的文件夹，不碰你的系统）…
echo 进度条不动也别关：镜像偶尔慢几秒。
if not exist "runtime" mkdir runtime
set "PBS_URL=https://registry.npmmirror.com/-/binary/python-build-standalone/20260623/cpython-3.12.13+20260623-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri '%PBS_URL%' -OutFile 'runtime\pbs.tar.gz' -TimeoutSec 300 } catch { exit 1 }"
if errorlevel 1 goto NOPYTHON
for %%A in ("runtime\pbs.tar.gz") do if %%~zA LSS 10000000 goto NOPYTHON
tar -xzf "runtime\pbs.tar.gz" -C "runtime"
if errorlevel 1 goto NOPYTHON
del "runtime\pbs.tar.gz" >nul 2>&1
"runtime\python\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 goto NOPYTHON
set "PYCMD=runtime\python\python.exe"
echo 便携版 Python 就绪。
:HAVEPY

rem ── 2) 虚拟环境（只建一次；坏了自动重建——比如整个文件夹被手动挪过位置） ──
if not exist ".venv\Scripts\python.exe" goto MAKEVENV
".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
if not errorlevel 1 goto HAVEVENV
echo 检测到虚拟环境失效（文件夹挪过位置？），自动重建…
rmdir /s /q ".venv" >nul 2>&1
:MAKEVENV
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
echo 自动下载没成功（网络或镜像问题）。手动装一次也很快：
echo   即将打开国内镜像下载页 —— 下载后双击安装，
echo   安装第一屏务必勾选 "Add python.exe to PATH"。
echo 装好后请再双击一次本文件。
start https://mirrors.huaweicloud.com/python/3.12.8/python-3.12.8-amd64.exe
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
