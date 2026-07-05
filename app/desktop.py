"""觅影桌面窗口壳：pywebview（macOS 用系统 WKWebView）装现有 Web UI。

行为（2026-06-10 拍板的「原生窗口壳」形态）：
- 已有觅影服务在跑 → 直接开窗口连它；没有 → 自己拉起 uvicorn 子进程
- 关窗 = 退出觅影：停掉验明正身的服务进程（绝不误杀其他项目）
- 界面代码零改动，仍可同时用浏览器访问 http://127.0.0.1:8788
- pywebview 装不上/不可用时，启动脚本自动回落浏览器模式（见 启动觅影.command）

跑法：.venv 里 `python -m app.desktop`（启动脚本会自动选择）。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
PORT = int(os.environ.get("YEEHX_PORT", "8788"))
URL = f"http://127.0.0.1:{PORT}"
PIDF = APP_DIR / "out" / "server.pid"

_OWN_PROC: subprocess.Popen | None = None


def _alive() -> bool:
    try:
        import requests
        return bool(requests.get(URL + "/api/health", timeout=2).json().get("ok"))
    except Exception:
        return False


def _is_miying_pid(pid: int) -> bool:
    """ps 验明正身：只认 uvicorn app.main:app，绝不误杀其他项目。"""
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout
        return "uvicorn app.main:app" in out
    except Exception:
        return False


def _spawn() -> subprocess.Popen:
    host = "127.0.0.1"
    try:
        st = json.loads((APP_DIR / "out" / "app_settings.json").read_text(encoding="utf-8"))
        if st.get("lan_access"):
            host = "0.0.0.0"
    except Exception:
        pass
    (APP_DIR / "out").mkdir(parents=True, exist_ok=True)
    log = open(APP_DIR / "out" / "app.log", "ab")
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(PORT)],
        cwd=str(ROOT), stdout=log, stderr=log,
    )
    PIDF.write_text(str(p.pid), encoding="utf-8")
    return p


def _stop_service():
    """关窗即退：先停自己拉起的子进程；连的是现成服务则按 PID 文件验明正身后停。"""
    global _OWN_PROC
    if _OWN_PROC is not None and _OWN_PROC.poll() is None:
        _OWN_PROC.terminate()
        try:
            _OWN_PROC.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _OWN_PROC.kill()
        _OWN_PROC = None
        PIDF.unlink(missing_ok=True)
        return
    try:
        pid = int(PIDF.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if _is_miying_pid(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                if not _is_miying_pid(pid):
                    break
                time.sleep(0.5)
            else:
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    PIDF.unlink(missing_ok=True)


def main() -> int:
    global _OWN_PROC
    if not _alive():
        _OWN_PROC = _spawn()
        for _ in range(120):
            if _alive():
                break
            if _OWN_PROC.poll() is not None:
                print("觅影服务启动失败，请看 app/out/app.log 最后几行。")
                return 1
            time.sleep(0.5)
        else:
            print("60 秒内服务未就绪，请看 app/out/app.log。")
            _stop_service()
            return 1

    # 被 kill（如 停止觅影.command）时尽力带走子进程；孤儿 uvicorn 也能被停止脚本按端口收掉
    signal.signal(signal.SIGTERM, lambda *_: (_stop_service(), os._exit(0)))

    try:
        import webview
    except ImportError:
        print("未安装 pywebview，改用浏览器打开（功能完全一致）。")
        subprocess.run(["open", URL], check=False)
        return 0

    try:
        # WKWebView 默认不接管下载：不开这个开关，点「下载」会被当成处理不了的导航，
        # 窗口直接关闭 → 连带退出服务（用户实测踩坑 2026-07-04）
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:
        pass

    webview.create_window(
        "玩椰 YEEHX · 觅影", URL,
        width=1440, height=900, min_size=(1080, 680),
    )
    webview.start()          # 阻塞到窗口关闭
    _stop_service()          # 关窗即退服务
    return 0


if __name__ == "__main__":
    sys.exit(main())
