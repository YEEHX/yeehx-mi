"""觅影 Windows 启动/停止逻辑（被 启动觅影.bat / 停止觅影.bat 调用）。

设计：.bat 只干两件最笨的事（找 Python、建 .venv），其余全部在这里——
依赖安装（默认源失败自动换清华镜像）、残留实例清理（按 PID/命令行验明正身，
绝不误杀其他项目）、窗口/浏览器模式选择、健康等待、开浏览器、日志截断。

只用标准库：本文件必须在依赖装好**之前**就能跑。
对应的 mac 逻辑在 启动觅影.command / 停止觅影.command（bash 版，行为一致）。

用法（bat 会自动调，一般不用手敲）：
    .venv\\Scripts\\python.exe winlaunch.py           # 启动
    .venv\\Scripts\\python.exe winlaunch.py --stop    # 停止
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent          # app/
ROOT = APP_DIR.parent                              # 仓库根
VENV = APP_DIR / ".venv"
OUT = APP_DIR / "out"
LOG = OUT / "app.log"
PIDF = OUT / "server.pid"
PORT = int(os.environ.get("YEEHX_PORT", "8788"))
URL = f"http://127.0.0.1:{PORT}"
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
_PS_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $ErrorActionPreference='SilentlyContinue'; "


def _say(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:                     # 老终端代码页兜底
        print(msg.encode("gbk", "replace").decode("gbk"), flush=True)


def _ps(script: str, timeout: int = 20) -> str:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", _PS_PREFIX + script],
                           capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        return r.stdout.decode("utf-8", errors="replace")
    except (subprocess.SubprocessError, OSError):
        return ""


def _alive() -> bool:
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=2) as r:
            return bool(json.loads(r.read().decode("utf-8", "replace")).get("ok"))
    except Exception:
        return False


def _pid_command(pid: int) -> str:
    return _ps(f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\").CommandLine").strip()


def _is_miying(pid: int) -> bool:
    cmd = _pid_command(pid)
    return ("uvicorn app.main:app" in cmd) or ("app.desktop" in cmd)


def _taskkill(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)


def _find_miying_pids() -> list[int]:
    """当前所有觅影进程（uvicorn app.main:app / app.desktop），按命令行验明正身。"""
    out = _ps("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
              "Where-Object { $_.CommandLine -match 'uvicorn app\\.main:app|app\\.desktop' } | "
              "Select-Object -ExpandProperty ProcessId")
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def _port_owner() -> int | None:
    out = _ps(f"(Get-NetTCPConnection -LocalPort {PORT} -State Listen).OwningProcess").strip()
    first = out.splitlines()[0].strip() if out else ""
    return int(first) if first.isdigit() else None


# ═══════════════ 依赖 ═══════════════

def _pip(args: list[str], log_err: Path | None = None) -> bool:
    err = open(log_err, "ab") if log_err else subprocess.DEVNULL
    try:
        r = subprocess.run([sys.executable, "-m", "pip", *args],
                           stdout=subprocess.DEVNULL, stderr=err)
        return r.returncode == 0
    finally:
        if log_err:
            err.close()


def ensure_deps() -> bool:
    req = APP_DIR / "requirements.txt"
    req_hash = hashlib.sha256(req.read_bytes()).hexdigest()
    hash_file = VENV / ".req_hash"
    if hash_file.exists() and hash_file.read_text(encoding="utf-8").strip() == req_hash:
        _say("依赖未变化，跳过安装。")
        return True
    _say("检查/安装依赖（共约 80MB，首次约 1-5 分钟；有进度就没卡住。之后启动秒开）…")
    errlog = APP_DIR / "pip_err.log"
    errlog.unlink(missing_ok=True)
    _pip(["install", "-i", MIRROR, "--upgrade", "pip"])
    # 清华镜像优先（用户几乎全在国内；官方源直连常年只有几十 kB/s，
    # "慢"不会触发失败回退，用户会误以为卡死）；镜像挂了才回官方源
    if not _pip(["install", "-i", MIRROR, "-r", str(req)], errlog):
        _say("国内镜像安装失败，自动换官方源重试…")
        if not _pip(["install", "-r", str(req)], errlog):
            _say("")
            _say("✗ 依赖安装失败（两个源都试过了）。常见原因：网络不通 / 代理拦截。")
            _say("  报错细节在 app\\pip_err.log，把它发给玩椰即可。")
            return False
    # 可选组件：装不上不影响启动（HEIC=iPhone照片；pywebview=独立窗口，缺了走浏览器）
    for opt in (["pillow-heif"], ["pywebview>=5.0"]):
        _pip(["install", "-i", MIRROR, *opt]) or _pip(["install", *opt])
    hash_file.write_text(req_hash, encoding="utf-8")
    _say("依赖就绪。")
    return True


# ═══════════════ 启动 ═══════════════

def _settings() -> dict:
    try:
        return json.loads((OUT / "app_settings.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _rotate_log() -> None:
    try:
        if LOG.exists() and LOG.stat().st_size > 5 * 1024 * 1024:
            data = LOG.read_bytes()[-1024 * 1024:]
            LOG.write_bytes(data)
    except OSError:
        pass


def _cleanup_stale() -> None:
    """清掉没退干净的旧实例：只杀验明正身的自己人。"""
    if PIDF.exists():
        try:
            old = int(PIDF.read_text(encoding="utf-8").strip())
            if _is_miying(old):
                _say(f"发现上次残留的服务进程（PID {old}），先停掉…")
                _taskkill(old)
                time.sleep(1)
        except (ValueError, OSError):
            pass
        PIDF.unlink(missing_ok=True)
    owner = _port_owner()
    if owner and _is_miying(owner):
        _say(f"端口 {PORT} 上有一个旧的觅影实例（PID {owner}），先停掉…")
        _taskkill(owner)
        time.sleep(1)
    # 残留的旧窗口/服务进程也验明正身收掉（与 mac 启动脚本行为一致：
    # 能走到这里说明服务不健康，留着的都是僵尸）
    for pid in _find_miying_pids():
        _taskkill(pid)


def _pythonw() -> str:
    w = VENV / "Scripts" / "pythonw.exe"
    return str(w) if w.exists() else sys.executable


def _spawn_detached(args: list[str]) -> subprocess.Popen:
    OUT.mkdir(parents=True, exist_ok=True)
    log = open(LOG, "ab")
    return subprocess.Popen(args, cwd=str(ROOT), stdout=log, stderr=log,
                            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)


def start() -> int:
    _say("=== 玩椰 YEEHX · 觅影 ===")
    if _alive():
        _say(f"觅影已在运行 → {URL} （要重启请先双击「停止觅影.bat」）")
        import webbrowser
        webbrowser.open(URL)
        return 0

    if not ensure_deps():
        return 1
    _cleanup_stale()
    _rotate_log()

    st = _settings()
    window = st.get("window_mode", True) is not False
    if window:
        try:
            r = subprocess.run([sys.executable, "-c", "import webview"],
                               capture_output=True, creationflags=CREATE_NO_WINDOW)
            window = r.returncode == 0
        except OSError:
            window = False
        if not window:
            _say("pywebview 未装上（可能缺 WebView2），本次用浏览器模式，功能完全一致。")

    if window:
        _say("启动觅影（独立窗口）…（日志：app\\out\\app.log）")
        proc = _spawn_detached([_pythonw(), "-m", "app.desktop"])
    else:
        host = "0.0.0.0" if st.get("lan_access") else "127.0.0.1"
        if host != "127.0.0.1":
            _say("已开启局域网访问：手机地址见 设置页「手机/局域网访问」（带口令）")
        _say(f"启动服务 {URL} …（日志：app\\out\\app.log）")
        proc = _spawn_detached([_pythonw(), "-m", "uvicorn", "app.main:app",
                                "--host", host, "--port", str(PORT)])
        try:
            PIDF.write_text(str(proc.pid), encoding="utf-8")
        except OSError:
            pass

    for _ in range(60):
        if _alive():
            if window:
                _say("觅影已就绪，窗口即将弹出。关闭窗口=退出觅影。")
            else:
                _say("服务已就绪，打开浏览器。停止服务请双击「停止觅影.bat」。")
                import webbrowser
                webbrowser.open(URL)
            return 0
        if proc.poll() is not None:
            break
        time.sleep(1)

    _say("")
    _say("✗ 60 秒内服务没起来。最近日志：")
    _say("──────────────────────────────")
    try:
        for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]:
            _say(line)
    except OSError:
        pass
    _say("──────────────────────────────")
    _say("完整日志在 app\\out\\app.log，把上面的报错发我即可。")
    return 1


# ═══════════════ 停止 ═══════════════

def stop() -> int:
    _say("=== 停止觅影 ===")
    stopped = False
    if PIDF.exists():
        try:
            pid = int(PIDF.read_text(encoding="utf-8").strip())
            if _is_miying(pid):
                _taskkill(pid)
                stopped = True
                _say(f"已停止觅影服务（PID {pid}）。")
        except (ValueError, OSError):
            pass
        PIDF.unlink(missing_ok=True)
    for pid in _find_miying_pids():
        _taskkill(pid)
        stopped = True
        _say(f"已停止觅影进程（PID {pid}）。")
    owner = _port_owner()
    if owner and _is_miying(owner):
        _taskkill(owner)
        stopped = True
        _say(f"已停止端口 {PORT} 上的觅影实例（PID {owner}）。")
    if not stopped:
        _say("觅影本来就没在运行。")
    _say("原素材与数据库不受影响；再次双击「启动觅影.bat」即可重新启动。")
    return 0


if __name__ == "__main__":
    sys.exit(stop() if "--stop" in sys.argv else start())
