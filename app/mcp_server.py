"""觅影 MCP server —— 给外部 Agent（Hermes 等）用的标准 MCP 接口（stdio）。

挂法：Hermes 的 ~/.hermes/config.yaml 里把 command 指到 app/mcp_run.sh
（Windows：app/mcp_run.bat），详见 app/Hermes接入觅影.md。

设计原则：
- 薄壳：所有数据操作都打觅影自己的 HTTP API（127.0.0.1:8788），单一数据源，
  不绕过任何业务规则（标签继承、离线卷、导出白名单等都由觅影本体说了算）。
- 自动拉起：每次工具调用前探活，觅影没开就用当前 venv 起 uvicorn（日志照旧
  进 app/out/app.log），起来后接着干活。Hermes 不需要懂任何启动逻辑。
- 只读 + 拷贝：能力清单 = 搜索 / 预览帧 / 取原片 / 导出复制 / 看状态。
  对原始素材零写入：取原片只给路径或转码出新文件，导出底层是 shutil.copy2。
- 大文件：超过微信上限（默认 950MB，env MIYING_WECHAT_LIMIT_MB 可调）的视频
  自动转 1080p H.264 代理片，缓存在 app/out/mcp/proxy/，原片永不动。

stdout 是 MCP 协议通道——本文件里任何调试输出必须走 stderr。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

# 必须在 import fastmcp 之前设：fastmcp 启动横幅会连 PyPI 查新版本，
# 在离线/挂系统代理（Clash 等 SOCKS）的环境下可能直接把 stdio server 炸死。
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")
os.environ.setdefault("FASTMCP_SHOW_CLI_BANNER", "false")

try:
    from fastmcp import FastMCP            # fastmcp 2.x
except ImportError:                         # 兜底：官方 SDK 自带的 FastMCP
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        sys.stderr.write("[觅影MCP] 缺依赖：pip install fastmcp（装进 app/.venv）\n")
        raise

# ── 常量 ────────────────────────────────────────────────────────────────
APP_DIR = Path(__file__).resolve().parent          # app/
ROOT = APP_DIR.parent                              # 仓库根
PORT = int(os.environ.get("YEEHX_MI_PORT", "8788"))
BASE = f"http://127.0.0.1:{PORT}"
LIMIT_MB = float(os.environ.get("MIYING_WECHAT_LIMIT_MB", "950"))  # 微信安全上限
MCP_OUT = APP_DIR / "out" / "mcp"                  # out/ 不进 git、不进发布包
PREVIEW_DIR = MCP_OUT / "preview"
PROXY_DIR = MCP_OUT / "proxy"
for d in (PREVIEW_DIR, PROXY_DIR):
    d.mkdir(parents=True, exist_ok=True)

KIND_CN = {"video": "视频", "photo": "照片", "image": "照片", "audio": "音频"}

mcp = FastMCP("觅影素材库")


# ── 基础设施 ─────────────────────────────────────────────────────────────
def _alive() -> dict | None:
    try:
        r = requests.get(f"{BASE}/api/health", timeout=2)
        if r.ok:
            return r.json()
    except requests.RequestException:
        pass
    return None


def _bind_host() -> str:
    """与启动脚本同一套规则：设置里开了局域网访问就绑 0.0.0.0（手机可连）。"""
    try:
        import json as _json
        s = _json.loads((APP_DIR / "out" / "app_settings.json").read_text(encoding="utf-8"))
        if s.get("lan_access"):
            return "0.0.0.0"
    except (OSError, ValueError):
        pass
    return "127.0.0.1"


def ensure_app() -> dict:
    """觅影没开就拉起来（detach 进程，日志进 app/out/app.log），最多等 90 秒。
    3-5：抢 launch.lock 防并发双起——两个工具同时调用时只有一个去 spawn，
    另一个等健康检查；锁超过 120 秒视为残留（spawn 方崩了），可抢占。"""
    h = _alive()
    if h:
        return h
    lock = MCP_OUT / "launch.lock"
    got = False
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        got = True
    except FileExistsError:
        try:
            if time.time() - lock.stat().st_mtime > 120:
                lock.unlink(missing_ok=True)
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                got = True
        except (OSError, FileExistsError):
            pass
    try:
        if got:
            log = APP_DIR / "out" / "app.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "ab") as lf:
                lf.write(b"\n[mcp] auto-start uvicorn\n")
                from app.core import osplat
                proc = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "app.main:app",
                     "--host", _bind_host(), "--port", str(PORT)],
                    cwd=str(ROOT), stdout=lf, stderr=subprocess.STDOUT,
                    **osplat.detached_popen_kwargs(),
                )
            try:   # 写 server.pid，停止脚本（.command/.bat）按它精确停
                (APP_DIR / "out" / "server.pid").write_text(str(proc.pid), encoding="utf-8")
            except OSError:
                pass
        for _ in range(90):
            time.sleep(1)
            h = _alive()
            if h:
                return h
        raise RuntimeError("觅影启动失败：90 秒内服务没起来，请看 app/out/app.log")
    finally:
        if got:
            lock.unlink(missing_ok=True)


def _get(path: str, timeout: float = 60, **kw):
    r = requests.get(BASE + path, timeout=timeout, **kw)
    _raise_api(r, path)
    return r


def _post(path: str, payload: dict, timeout: float = 60) -> dict:
    r = requests.post(BASE + path, json=payload, timeout=timeout)
    _raise_api(r, path)
    return r.json()


def _raise_api(r: requests.Response, path: str):
    if r.status_code < 400:
        return
    try:
        j = r.json()
        msg = j.get("detail") or j.get("error") or r.text
    except ValueError:
        msg = r.text[:200]
    raise RuntimeError(f"觅影接口 {path} 报错（{r.status_code}）：{msg}")


def _ids(s: str) -> list[str]:
    return [x for x in re.split(r"[,\s，、;；]+", (s or "").strip()) if x]


def _mb(n: int | float | None) -> str:
    n = float(n or 0)
    return f"{n / 1024 / 1024 / 1024:.2f}GB" if n >= 1024 ** 3 else f"{n / 1024 / 1024:.1f}MB"


def _ffmpeg() -> str:
    p = shutil.which("ffmpeg")
    if p:
        return p
    import imageio_ffmpeg                   # 觅影必装依赖，自带二进制
    return imageio_ffmpeg.get_ffmpeg_exe()


def _enforce_cache_cap(keep: Path | None = None):
    """3-4：preview/proxy 只写不清会无限涨（1080p 代理一个几百 MB）。
    每次写入后按 LRU（mtime 最旧先删）压回上限；keep 指定刚产出的文件不动。
    上限默认 20GB，env MIYING_MCP_CACHE_GB 可调。"""
    try:
        cap = float(os.environ.get("MIYING_MCP_CACHE_GB", "20")) * 1024 ** 3
    except ValueError:
        cap = 20 * 1024 ** 3
    files = []
    for d in (PREVIEW_DIR, PROXY_DIR):
        for p in d.glob("*"):
            if p.is_file() and (keep is None or p != keep):
                try:
                    st = p.stat()
                    files.append((st.st_mtime, st.st_size, p))
                except OSError:
                    pass
    total = sum(s for _, s, _ in files)
    if keep is not None:
        try:
            total += keep.stat().st_size
        except OSError:
            pass
    files.sort()                            # mtime 最旧在前
    for _, size, p in files:
        if total <= cap:
            break
        try:
            p.unlink()
            total -= size
            sys.stderr.write(f"[觅影MCP] 缓存超限，按 LRU 清理：{p.name}\n")
        except OSError:
            pass


def _fmt_asset(i: int, a: dict) -> str:
    kind = KIND_CN.get(a.get("kind") or "", a.get("kind") or "?")
    loc = "/".join(x for x in [a.get("volume") or "", a.get("rel_path") or ""] if x)
    lines = [f"{i}. {a.get('name')}（{kind}）  id={a.get('asset_id')}", f"   位置：{loc or '?'}"]
    tags = a.get("tags") or []
    if tags:
        lines.append("   标签：" + "、".join(map(str, tags[:10])))
    desc = (a.get("desc") or a.get("desc_ai") or "").strip()
    if desc:
        lines.append("   描述：" + desc[:80])
    return "\n".join(lines)


# ── 工具 ────────────────────────────────────────────────────────────────
@mcp.tool()
def miying_search(query: str, limit: int = 8) -> str:
    """用自然语言在觅影素材库找素材。query 例：「雨中鹦鹉洲长江大桥的航拍视频」。
    返回命中列表（文件名 / id / 位置 / 标签）。拿到 id 后：发预览图用 miying_preview，
    取原片发给用户用 miying_get_original，批量拷到文件夹用 miying_export。"""
    ensure_app()
    limit = max(1, min(int(limit), 30))
    parsed_note = ""
    try:
        out = _post("/api/ai_search", {"q": query, "limit": limit}, timeout=300)
        p = out.get("parsed") or {}
        bits = []
        if p.get("tags"):
            bits.append("标签=" + "、".join(str(x) for x in p["tags"] if x))
        if p.get("keywords"):
            bits.append("关键词=" + p["keywords"])
        if p.get("scope") and p["scope"].get("label"):
            bits.append("范围=" + p["scope"]["label"])
        if bits:
            parsed_note = "AI 解析：" + "；".join(bits) + "\n"
        if p.get("note"):
            parsed_note += "（" + str(p["note"]) + "）\n"
    except RuntimeError as e:
        # 本地模型不可用等 → 退回纯关键词检索（FTS）
        out = _post("/api/search", {"q": query, "limit": limit})
        parsed_note = f"（AI 解析不可用，已用关键词匹配。原因：{e}）\n"

    assets = out.get("assets") or []
    total = out.get("total", len(assets))
    if not assets:
        return (parsed_note + "没有命中素材。建议：换近义词（如 大桥/桥梁）、去掉限定词再试，"
                "或用 miying_status 看看素材是否还在打标中。")
    body = "\n".join(_fmt_asset(i + 1, a) for i, a in enumerate(assets))
    return (f"{parsed_note}共命中 {total} 条，显示前 {len(assets)} 条：\n{body}\n\n"
            "→ 发预览图：miying_preview(asset_ids=\"id1,id2\")；"
            "取原片：miying_get_original(asset_id)；"
            "拷贝到文件夹：miying_export(target_dir, asset_ids=...)")


@mcp.tool()
def miying_preview(asset_ids: str) -> str:
    """取素材的代表帧 JPG（直接当图片消息发给用户看效果）。
    asset_ids：逗号分隔的素材 id，一次最多 9 个。
    返回本地图片文件路径——请把这些文件作为图片发送。"""
    ensure_app()
    ids = _ids(asset_ids)[:9]
    if not ids:
        return "没给 asset_id。先用 miying_search 找到素材。"
    ok_paths, errs = [], []
    for aid in ids:
        try:
            r = _get(f"/api/asset/{aid}/download", timeout=120)
            dest = PREVIEW_DIR / f"{aid}.jpg"
            dest.write_bytes(r.content)
            ok_paths.append(str(dest))
        except RuntimeError as e:
            errs.append(f"{aid}: {e}")
    _enforce_cache_cap()
    parts = []
    if ok_paths:
        parts.append("预览图已生成，把下面这些文件作为图片发给用户：\n" + "\n".join(ok_paths))
    if errs:
        parts.append("失败：\n" + "\n".join(errs))
    return "\n\n".join(parts)


@mcp.tool()
def miying_get_original(asset_id: str) -> str:
    """取原素材用于发送给用户。不超上限（默认 950MB）直接给原片路径；超限的视频
    自动转 1080p H.264 代理片（可能要几分钟，请耐心等返回）。
    返回里「发送文件：<路径>」即要发送的文件。原素材永远不会被修改。"""
    ensure_app()
    aid = asset_id.strip()
    a = _get(f"/api/asset/{aid}", timeout=30).json()
    name, kind, size = a.get("name"), a.get("kind"), int(a.get("size") or 0)
    src = a.get("abspath")
    loc = "/".join(x for x in [a.get("volume") or "", a.get("rel_path") or ""] if x)
    if not src or not Path(src).exists():
        return (f"「{name}」的原文件现在拿不到：所在卷离线（硬盘没挂载）或文件已删除。\n"
                f"记录位置：{loc}\n请告知用户文件名和位置，回头挂上硬盘再取。")
    limit_bytes = LIMIT_MB * 1024 * 1024
    head = f"「{name}」（{KIND_CN.get(kind, kind)}，{_mb(size)}）\n原片路径：{src}\n"
    if size <= limit_bytes:
        return head + f"未超上限，直接发原片。\n发送文件：{src}"
    if kind != "video":
        return (head + f"超过微信上限（{LIMIT_MB:.0f}MB）且不是视频，没法转码压缩。"
                "建议用 miying_export 拷到文件夹，或告知用户回家取。")

    proxy = PROXY_DIR / f"{aid}_1080.mp4"
    src_mtime = Path(src).stat().st_mtime
    if not (proxy.exists() and proxy.stat().st_mtime >= src_mtime):
        cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
               "-i", src,
               "-vf", "scale=-2:min(1080\\,ih)",
               "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
               "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "128k",
               "-movflags", "+faststart",
               str(proxy)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=3600)
        except subprocess.TimeoutExpired:
            proxy.exists() and proxy.unlink()
            return head + "转码超时（>1小时），放弃。建议告知用户原片路径，回家取。"
        except subprocess.CalledProcessError as e:
            proxy.exists() and proxy.unlink()
            err = (e.stderr or b"")[-300:].decode("utf-8", "ignore")
            return head + f"转码失败：{err}\n建议告知用户原片路径。"
    _enforce_cache_cap(keep=proxy)          # 新代理片保住，旧的按 LRU 让位
    psize = proxy.stat().st_size
    if psize > limit_bytes:
        return (head + f"原片超限，转出的 1080p 代理仍有 {_mb(psize)}，还是发不出。\n"
                "建议 miying_export 拷到文件夹，或告知用户回家取原片。")
    return (head + f"原片超过 {LIMIT_MB:.0f}MB 上限，已转 1080p 代理片（{_mb(psize)}）。\n"
            f"发送文件：{proxy}\n发送时请注明这是代理片，原片在上面的路径里。")


@mcp.tool()
def miying_export(target_dir: str, asset_ids: str = "", query: str = "") -> str:
    """把素材原片复制到用户指定的文件夹（只复制，绝不动原文件）。
    target_dir：绝对路径，例 mac /Users/xxx/Desktop/周五拍摄用、Windows D:\\周五拍摄用。
    asset_ids（推荐）：逗号分隔的 id，导出明确这几条；
    query：自然语言条件，按解析结果全量导出（适合「把所有夜景航拍都拷过去」）。
    二者给一个即可，asset_ids 优先。"""
    ensure_app()
    target = os.path.expanduser((target_dir or "").strip())
    if not target or not os.path.isabs(target):
        return "target_dir 必须是绝对路径（mac 例 /Users/xxx/Desktop/素材包；Windows 例 D:\\素材包）。"
    ids = _ids(asset_ids)
    if ids:
        res = _post("/api/export", {"type": "original", "asset_ids": ids, "target": target})
    elif query.strip():
        probe = _post("/api/ai_search", {"q": query, "limit": 1}, timeout=300)
        p = probe.get("parsed") or {}
        scope = p.get("scope") or None
        res = _post("/api/ai_export", {
            "type": "original",
            "tag_ids": p.get("tag_ids") or [],
            "keywords": p.get("keywords") or "",
            "scope": {"volume_id": scope["volume_id"], "rel_path": scope.get("rel_path") or ""} if scope else None,
            "target": target,
        }, timeout=120)
    else:
        return "asset_ids 和 query 至少给一个。"

    tid, count = res.get("task_id"), res.get("count", "?")
    last = None
    for _ in range(40):                      # 最多等 2 分钟
        time.sleep(3)
        snap = _get("/api/tasks", timeout=15).json()
        row = next((t for t in snap.get("tasks") or [] if t.get("task_id") == tid), None)
        if row is None:
            done = last or {}
            failed = int(done.get("failed") or 0)
            tail = f"，失败 {failed} 个（多半是卷离线）" if failed else ""
            return f"导出完成：共 {count} 个文件 → {target}{tail}"
        last = row
    d, t = (last or {}).get("done", "?"), (last or {}).get("total", count)
    return (f"导出还在后台跑（{d}/{t}），目标 {target}。"
            "稍后用 miying_status 看进度，不用重复提交。")


@mcp.tool()
def miying_status() -> str:
    """觅影服务状态：版本、任务队列（打标/导出进度）、待确认候选词数量。
    服务没开会自动拉起，也可专门用这个工具来预热。"""
    h = ensure_app()
    snap = _get("/api/tasks", timeout=15).json()
    rows = snap.get("tasks") or []
    lines = [f"觅影 v{h.get('version', '?')} 运行中（端口 {PORT}）"]
    if rows:
        lines.append(f"进行中的任务 {len(rows)} 个：")
        for t in rows[:8]:
            lines.append(f"- {t.get('title')}：{t.get('done')}/{t.get('total')}"
                         + (f"（失败{t.get('failed')}）" if t.get("failed") else "")
                         + f" [{t.get('status')}]")
    else:
        lines.append("任务队列空闲。")
    pc = snap.get("pending_candidates")
    if pc:
        lines.append(f"候选区待确认词：{pc} 个（回觅影界面处理）")
    return "\n".join(lines)


@mcp.tool()
def miying_reveal(asset_id: str) -> str:
    """在文件管理器（mac 访达 / Windows 资源管理器）中弹出并选中素材原文件——
    人在电脑前时用这个最快，不转码、不复制。远程（微信）场景请用 miying_get_original。"""
    ensure_app()
    aid = asset_id.strip()
    a = _get(f"/api/asset/{aid}", timeout=30).json()
    name = a.get("name")
    loc = "/".join(x for x in [a.get("volume") or "", a.get("rel_path") or ""] if x)
    src = a.get("abspath")
    if not src or not Path(src).exists():
        return (f"「{name}」的原文件现在拿不到：所在卷离线（硬盘没挂载）或文件已删除。\n"
                f"记录位置：{loc}")
    _post(f"/api/asset/{aid}/reveal", {}, timeout=15)
    return f"已在文件管理器中弹出选中：{name}\n完整路径：{src}"


if __name__ == "__main__":
    mcp.run()
