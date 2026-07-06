"""觅影 v2 backend: clean single-tag workflow.

路由分两组（删改前先看这里，别误删 Agent 接口）：
- UI 用：web/index.html 实际调用的接口。
- Agent 用（UI 不调，给外部 AI/脚本，保留勿删）：
  /api/health · GET /api/ping · /api/asset/{aid}/tags(POST) · /api/asset/{aid}/lock ·
  /api/asset/{aid}/fine_tag · /api/folder/{fid}(GET/tags/tag/fine_tag/fill_thumbs/hide) ·
  /api/path/hide · /api/scan/cleanup · /api/tag/{tid}/alias · /api/tag/{tid}/ref_file ·
  /api/tasks/cancel_all · DELETE /api/lut/{name} · /api/db/reset
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
from pathlib import Path

from fastapi import FastAPI, Body, Query, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from app import db, tasks, export, __version__ as APP_VERSION
from app import brand as brand_mod
from app.config import get_cfg
from app.core import volumes, folders, assets as A, tags as TG, candidates as C, tag_merges as TM, inheritance, search as S
from app.core import osplat
from app.core import tag_io
from app.core import files as fmod
from app.scan import scanner
from app.media import thumbnails, frames
from app.ai import vision

app = FastAPI(title="玩椰 YEEHX · 觅影")
WEB = Path(__file__).resolve().parent / "web"

# ── 访问守卫：本机 + 可选局域网（手机） ──────────────────────────────────
# 本机客户端：Host 必须是本机名（挡 DNS rebinding）；非 GET 校验 Origin（挡 CSRF）。
# 局域网客户端（设置页开启后才放行）：Host 必须是私网 IP（同样挡 rebinding 域名）
# + 必须携带访问口令（首次用 ?token= 打开，之后走 cookie）。
# "testserver"/"testclient" 是 FastAPI TestClient 的默认值，仅测试出现。
_LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1", "testserver"}
_LOCAL_CLIENT_IPS = {"127.0.0.1", "::1", "testclient"}
_PRIVATE_HOST_RE = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.)")


def _hostname_of(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("["):                      # [::1]:8788
        return host.split("]")[0].lstrip("[")
    return host.rsplit(":", 1)[0] if ":" in host else host


def _origin_hostname(origin: str) -> str:
    if not origin:
        return ""
    from urllib.parse import urlparse
    return (urlparse(origin).hostname or "").lower()


def _lan_request_allowed(host_name: str, token: str, origin_host: str | None, cfg) -> tuple[bool, int, str]:
    """局域网客户端的放行判定（纯函数，便于单测）。origin_host=None 表示无需校验 Origin。"""
    if not cfg.lan_access:
        return False, 403, "局域网访问未开启（电脑端设置页可开启）"
    if not (_PRIVATE_HOST_RE.match(host_name) or host_name in _LOCAL_HOSTNAMES):
        return False, 403, "Host 校验失败"
    if not cfg.lan_token or token != cfg.lan_token:
        return False, 401, "需要访问口令：请用电脑端设置页给出的带口令地址打开"
    if origin_host and not (_PRIVATE_HOST_RE.match(origin_host) or origin_host in _LOCAL_HOSTNAMES):
        return False, 403, "跨站请求被拒绝（Origin 校验失败）"
    return True, 200, ""


@app.middleware("http")
async def _access_guard(request, call_next):
    host_name = _hostname_of(request.headers.get("host", ""))
    client_ip = request.client.host if request.client else ""
    if client_ip in _LOCAL_CLIENT_IPS:
        if host_name not in _LOCAL_HOSTNAMES:
            return JSONResponse({"error": "觅影只接受本机访问（Host 校验失败）"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = (request.headers.get("origin") or "").strip()
            if origin and _origin_hostname(origin) not in _LOCAL_HOSTNAMES:
                return JSONResponse({"error": "跨站请求被拒绝（Origin 校验失败）"}, status_code=403)
        return await call_next(request)

    # 局域网客户端（绑定 0.0.0.0 且设置页开启后才会走到这）
    cfg = get_cfg()
    token = request.query_params.get("token") or request.cookies.get("miying_token") or ""
    origin_host = None
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin_host = _origin_hostname((request.headers.get("origin") or "").strip())
    ok, code, msg = _lan_request_allowed(host_name, token, origin_host, cfg)
    if not ok:
        return JSONResponse({"error": msg}, status_code=code)
    resp = await call_next(request)
    if request.query_params.get("token") == cfg.lan_token and request.cookies.get("miying_token") != cfg.lan_token:
        resp.set_cookie("miying_token", cfg.lan_token, max_age=30 * 86400,
                        httponly=True, samesite="lax")   # 首次带口令打开后落 cookie，之后不用再带
    return resp


@app.exception_handler(sqlite3.OperationalError)
def _sqlite_error(_, exc: sqlite3.OperationalError):
    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
        return JSONResponse({"error": "数据库正在写入，请稍后重试"}, status_code=503)
    return JSONResponse({"error": str(exc)}, status_code=500)


def _check_tls_bundle():
    """TLS 证书包自检：certifi 的 cacert.pem 丢了（典型：项目目录改名后旧 venv 残留），
    所有 HTTPS 模型请求会变 OSError。启动时给一句明确提示，省得到处翻日志。"""
    try:
        import certifi
        p = Path(certifi.where())
        if not p.exists():
            print(f"[觅影] 警告：TLS 证书包缺失（{p}）。HTTPS 模型接口会连不上；"
                  f"修复：app/.venv/bin/pip install --force-reinstall certifi（或重建 .venv）", flush=True)
    except ImportError:
        pass


def _backfill_covers():
    """存量封面回填（1-3 一次性迁移，幂等且廉价）：浏览页改为只读 cover_thumb 后，
    老库里"自己没直属素材、靠现场扫子树拿封面"的父文件夹会变空白——这里
    先用直属素材补，再让子级封面沿父链向上填空。"""
    conn = db.connect()
    empty = {r["folder_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM folders WHERE cover_thumb IS NULL OR cover_thumb=''").fetchall()}
    if not empty:
        return
    # 1) 直属素材里挑最新的缩略图
    direct: dict[str, str] = {}
    for r in conn.execute(
            "SELECT folder_id, thumb_path FROM assets "
            "WHERE thumb_path IS NOT NULL AND thumb_path!='' ORDER BY updated_at").fetchall():
        if r["folder_id"] in empty:
            direct[r["folder_id"]] = r["thumb_path"]
    # 2) 子级已有封面的，沿父链向上填空（深路径优先，越近的子级越先占坑）
    rows = conn.execute("SELECT volume_id, rel_path, cover_thumb FROM folders").fetchall()
    by_path = {(r["volume_id"], r["rel_path"]): (r["cover_thumb"] or "") for r in rows}
    fills: dict[str, str] = dict(direct)
    for f in sorted(empty.values(), key=lambda x: len(x.get("rel_path") or ""), reverse=True):
        if f["folder_id"] in fills:
            continue
        rel = (f.get("rel_path") or "").strip("/")
        prefix = rel + "/" if rel else ""
        best = ""
        for (vid2, rp), cv in by_path.items():
            if vid2 == f["volume_id"] and cv and rp.startswith(prefix) and rp != rel:
                best = cv
                break
        if best:
            fills[f["folder_id"]] = best
    if fills:
        def _w(c):
            t = db.now()
            for fid, cv in fills.items():
                c.execute("UPDATE folders SET cover_thumb=?, updated_at=? WHERE folder_id=? "
                          "AND (cover_thumb IS NULL OR cover_thumb='')", (cv, t, fid))
        db.write(_w)


@app.on_event("startup")
def _startup():
    _check_tls_bundle()
    db.init_db()
    _backfill_covers()
    TG.seed_if_empty()
    TG.ensure_core_vocabulary()
    C.expire_watching(90)   # 观察区只进不出会暗膨胀，90 天不再出现的自动清理
    scanner.register_all()
    tasks.MANAGER.start()
    cfg = get_cfg()
    cfg.thumbs_dir.mkdir(parents=True, exist_ok=True)
    cfg.refimg_dir.mkdir(parents=True, exist_ok=True)


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "app": "觅影", "version": APP_VERSION, "fts": db.FTS_OK,
            "platform": osplat.PLATFORM, "file_manager": osplat.FILE_MANAGER}


_BRAND_TAMPERED: bool | None = None   # 启动后首次访问时算一次，进程内缓存


@app.get("/api/brand")
def api_brand():
    """关于页数据：内置署名（app/brand.py 烧包）+ brand.json 非署名覆盖 + 版本 + 最近更新。"""
    global _BRAND_TAMPERED
    base = Path(__file__).resolve().parent
    external = db.jloads((base / "web" / "brand.json").read_text(encoding="utf-8"), {}) \
        if (base / "web" / "brand.json").exists() else {}
    if _BRAND_TAMPERED is None:
        _BRAND_TAMPERED = brand_mod.assets_tampered(base)
    changelog = ""
    cl = base / "CHANGELOG.md"
    if cl.exists():
        # 只取最近两个版本段
        parts = cl.read_text(encoding="utf-8").split("\n## ")
        changelog = "\n## ".join(parts[:3]).strip()
    return {"brand": brand_mod.merged_brand(external), "version": APP_VERSION,
            "changelog": changelog, "tampered": _BRAND_TAMPERED,
            "download_page": brand_mod.DOWNLOAD_PAGE}


def _fetch_version_info() -> dict:
    """拉官网 version.json（拆出来方便测试注入）。"""
    import requests as _rq
    r = _rq.get(brand_mod.UPDATE_URL, timeout=4)
    r.raise_for_status()
    return r.json()


def _ver_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v or "").strip().lstrip("v").split("."))
    except ValueError:
        return (0,)


@app.get("/api/update/check")
def api_update_check():
    """检查更新：比对官网 version.json。默认开启，可在设置关（隐私：仅此一处出站请求）。"""
    if not get_cfg().update_check:
        return {"enabled": False, "has_update": False}
    try:
        info = _fetch_version_info()
    except Exception as exc:
        return {"enabled": True, "ok": False, "has_update": False, "error": str(exc)[:120]}
    latest = str(info.get("version") or "")
    return {"enabled": True, "ok": True,
            "has_update": _ver_tuple(latest) > _ver_tuple(APP_VERSION),
            "current": APP_VERSION, "latest": latest,
            "url": info.get("url") or brand_mod.DOWNLOAD_PAGE,
            "notes": str(info.get("notes") or "")[:500]}


# ── 路径白名单（3-1/3-2/3-3）──────────────────────────────────────────────
# 浏览/读参考图/导出 只允许落在：mac=/Volumes（外接盘）+用户主目录；Windows=各盘符+主目录
# （但 Windows/Program Files/ProgramData/AppData 等系统区仍然拒绝）。
# 额外白名单走 YEEHX_FS_ROOTS（os.pathsep 分隔，给测试/特殊场景用）。系统目录一律 403。
def _allowed_roots() -> list[Path]:
    return osplat.allowed_browse_roots()


def _path_allowed(p: Path) -> bool:
    try:
        rp = p.expanduser().resolve()
    except OSError:
        return False
    for deny in osplat.denied_browse_roots():
        try:
            d = deny.resolve()
        except OSError:
            continue
        if rp == d or d in rp.parents:
            return False
    for root in _allowed_roots():
        try:
            r = root.resolve()
        except OSError:
            continue
        if rp == r or r in rp.parents:
            return True
    return False


# ── Browse / import ─────────────────────────────────────────────────────
def _root_cards() -> list[dict]:
    """根目录页的卡片：mac=/Volumes 各盘；Windows=各盘符（卷标显示名）。都追加桌面。"""
    roots = []
    if osplat.IS_WIN:
        for p in osplat.win_drives():
            try:
                roots.append(_fs_card(str(p), scan_counts=False))
            except OSError:
                continue
    else:
        vroot = Path("/Volumes")
        if vroot.exists():
            for p in sorted(vroot.iterdir()):
                if not p.is_dir() or p.name.startswith("."):
                    continue
                try:
                    if p.resolve() == Path("/"):
                        continue
                except OSError:
                    pass
                if p.name == "Macintosh HD":
                    continue
                roots.append(_fs_card(str(p), scan_counts=False))
    desktop = Path.home() / "Desktop"
    if not desktop.exists() and osplat.IS_WIN:
        onedrive = Path.home() / "OneDrive" / "Desktop"   # OneDrive 重定向桌面（Win11 常见）
        if onedrive.exists():
            desktop = onedrive
    if desktop.exists():
        roots.append(_fs_card(str(desktop), scan_counts=False))
    return roots


@app.get("/api/fs")
def api_fs(dir: str = Query("")):
    if not dir or (not osplat.IS_WIN and Path(dir).resolve() == Path("/")):
        return {"dir": "", "crumbs": [], "roots": _root_cards(), "subdirs": [], "assets": [], "direct_media": 0}

    base = Path(dir)
    if not _path_allowed(base):
        raise HTTPException(403, "该路径不在允许浏览的范围（仅各硬盘/盘符和用户目录）")
    if not base.exists():
        raise HTTPException(404, "路径不存在")
    info = volumes.peek(dir)
    vid, rel = info["volume_id"], info["rel_path"]
    entries, direct_media = [], 0
    try:
        for e in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            if osplat.should_skip_name(e.name):
                continue
            if e.is_dir():
                if e.suffix.lower() in fmod.PKG_EXT:
                    direct_media += 1
                else:
                    entries.append((e.name, str(e), f"{rel}/{e.name}".strip("/")))
            elif fmod.is_media(e):
                direct_media += 1
    except OSError:
        pass
    subdirs = _fs_cards_batch(vid, entries)   # 1-3：50 个子文件夹 ≤3 条查询（旧实现≈每卡 4-5 条）

    if direct_media:
        _index_current_layer(base, vid, rel)
    f = folders.get_by_path(vid, rel)
    cards = _cards(A.in_folder(f["folder_id"])) if f else []
    return {"dir": dir, "crumbs": _fs_crumbs(dir), "subdirs": subdirs, "assets": cards,
            "direct_media": direct_media, "imported": bool(f),
            "folder": f and {**f, "tags_resolved": _resolve_folder(f)},
            "volume_id": vid, "rel_path": rel}


@app.post("/api/scan/quick_import")
def api_quick_import(payload: dict = Body(...)):
    root = payload.get("root_path")
    if payload.get("volume_id"):
        mnt = volumes.resolve_mount(payload["volume_id"])
        if mnt is None:
            raise HTTPException(409, "硬盘离线")
        root = str(mnt / (payload.get("rel_path") or "").strip("/"))
    if not root or not Path(root).exists():
        raise HTTPException(400, "路径不存在")
    return scanner.quick_import(root, mode=payload.get("mode") or "quick")


@app.get("/api/dialog/folder")
def api_dialog_folder():
    return _choose_folder("选择文件夹")


@app.get("/api/dialog/export_folder")
def api_dialog_export_folder():
    return _choose_folder("选择导出目标文件夹")


@app.get("/api/dialog/lut_file")
def api_dialog_lut_file():
    try:
        path = osplat.choose_file("选择 .cube LUT 文件", ext=".cube", filter_name="LUT")
    except osplat.DialogCancelled as e:
        raise HTTPException(409, str(e))
    except osplat.DialogUnsupported as e:
        raise HTTPException(409, f"当前系统不支持系统选择：{e}")
    p = Path(path)
    if p.suffix.lower() != ".cube":
        raise HTTPException(400, "请选择 .cube 文件")
    return {"path": str(p), "name": p.stem}


def _choose_folder(prompt: str):
    try:
        return {"path": osplat.choose_folder(prompt)}
    except osplat.DialogCancelled as e:
        raise HTTPException(409, str(e))
    except osplat.DialogUnsupported as e:
        raise HTTPException(409, f"当前系统不支持系统选择：{e}")


# ── Search / facets ─────────────────────────────────────────────────────
@app.post("/api/search")
def api_search(payload: dict = Body(default={})):
    return S.search(
        query=payload.get("q", ""),
        facets=payload.get("facets", {}),
        scope=payload.get("scope"),
        limit=int(payload.get("limit", 200)),
        offset=int(payload.get("offset", 0)),
        sort=payload.get("sort") or "default",
        with_facets=payload.get("with_facets", True),
    )


def _resolve_scope_hint(hint: str):
    """把模型给的范围提示（文件夹名/路径片段/绝对路径）解析成 {volume_id, rel_path, label}。
    返回 (best, 其余候选数)。"""
    hint = (hint or "").strip()
    if not hint:
        return None, 0
    vols = {v["volume_id"]: v for v in volumes.list_volumes()}

    def label_of(vid: str, rel: str) -> str:
        v = vols.get(vid, {})
        vol_name = v.get("display_name") or v.get("name") or "?"
        return f"{vol_name}/{rel}" if rel else vol_name

    if hint.startswith("/"):
        info = volumes.peek(hint)
        f = folders.get_by_path(info["volume_id"], info["rel_path"])
        if f:
            return {"volume_id": f["volume_id"], "rel_path": f["rel_path"],
                    "label": label_of(f["volume_id"], f["rel_path"])}, 0
        return None, 0
    norm = hint.strip("/").casefold()
    matches = []
    for r in db.connect().execute("SELECT volume_id, rel_path, name FROM folders WHERE hidden=0").fetchall():
        nm = (r["name"] or "").casefold()
        rel = (r["rel_path"] or "").casefold()
        vol_nm = ((vols.get(r["volume_id"]) or {}).get("display_name")
                  or (vols.get(r["volume_id"]) or {}).get("name") or "").casefold()
        if norm in nm or norm in rel or (not r["rel_path"] and norm in vol_nm):
            matches.append(dict(r))
    if not matches:
        return None, 0
    matches.sort(key=lambda m: (
        0 if (vols.get(m["volume_id"]) or {}).get("online") else 1,   # 在线卷优先（离线卷导不出）
        0 if (m["name"] or "").casefold() == norm else 1,
        len(m["rel_path"] or ""),
    ))
    best = matches[0]
    return {"volume_id": best["volume_id"], "rel_path": best["rel_path"],
            "label": label_of(best["volume_id"], best["rel_path"]),
            "online": bool((vols.get(best["volume_id"]) or {}).get("online"))}, len(matches) - 1


def _ai_match_ids(tag_ids: list[str], keywords: str, scope: dict | None) -> list[str]:
    """按 标签∩关键词∩范围 取全部命中 id（不分页、不水合，给导出用）。"""
    base = S._scope_asset_ids(scope)
    for tid in tag_ids:
        s = S._ids_with("tag", tid)
        base = s if base is None else base & s
    qids, _deg = S._query_asset_ids(keywords)
    if qids is not None:
        base = qids if base is None else base & qids
    if base is None:
        base = {r[0] for r in db.connect().execute("SELECT asset_id FROM assets").fetchall()}
    return S._order(list(base))


@app.post("/api/ai_search")
def api_ai_search(payload: dict = Body(...)):
    """AI 自然语言指令：解析成 动作+范围+标签组合+关键词 → 搜索结果 + 动作提议。
    安全：这里只解析与搜索（只读）；导出动作只是"提议"，由用户在前端确认后走 /api/ai_export。"""
    q = (payload.get("q") or "").strip()
    if not q:
        raise HTTPException(400, "说一句你要找什么")
    from app.scan import tagging as tagging_mod
    base_ids = [x for x in (payload.get("base_tag_ids") or []) if TG.get(x)]
    selected = [TG.get(x)["name"] for x in base_ids]
    res = vision.parse_command(q, tagging_mod._tag_library(), get_cfg(), selected=selected)
    if not res.get("ok"):
        # 降级：模型不可用不再 500/400，自动回退普通关键词搜索，前端按 degraded 提示
        out = S.search(query=q, facets={"tag": base_ids} if base_ids else {},
                       limit=int(payload.get("limit", 120)), sort=payload.get("sort") or "default",
                       with_facets=True)
        out["degraded"] = True
        out["degraded_reason"] = res.get("error") or "AI 解析失败"
        out["parsed"] = {"action": "search", "tag_ids": base_ids, "tags": selected,
                         "keywords": q, "note": "", "scope": None, "scope_alternates": 0,
                         "export": {"type": "original", "target": ""}, "model": None}
        return out
    obj = res.get("json") or {}
    ids = list(base_ids)
    for nm in obj.get("tags") or []:
        t = TG.get_by_name(str(nm))
        if t and t["id"] not in ids:
            ids.append(t["id"])
    kw = str(obj.get("keywords") or "").strip()
    # 范围：模型解析的优先；没有就沿用追问带来的上一轮范围
    scope, scope_alts = _resolve_scope_hint(obj.get("scope") or "")
    if scope is None and isinstance(payload.get("scope"), dict) and payload["scope"].get("volume_id"):
        prev = payload["scope"]
        scope = {"volume_id": prev.get("volume_id"), "rel_path": prev.get("rel_path") or "",
                 "label": prev.get("label") or ""}
        scope_alts = 0
    scope_q = {"volume_id": scope["volume_id"], "rel_path": scope["rel_path"]} if scope else None
    out = S.search(query=kw, facets={"tag": ids} if ids else {}, scope=scope_q,
                   limit=int(payload.get("limit", 120)), sort=payload.get("sort") or "default",
                   with_facets=True)
    exp = obj.get("export") or {}
    out["parsed"] = {"action": obj.get("action") or "search",
                     "tag_ids": ids, "tags": [(TG.get(i) or {}).get("name") for i in ids],
                     "keywords": kw, "note": obj.get("note") or "",
                     "scope": scope, "scope_alternates": scope_alts,
                     "export": {"type": exp.get("type") or "original", "target": exp.get("target") or ""},
                     "model": res.get("model")}
    return out


@app.post("/api/ai_export")
def api_ai_export(payload: dict = Body(...)):
    """AI 指令的执行步：只会"导出=复制"。动作白名单到此为止——这个接口底层是
    export.start_export（shutil.copy2 / 重渲染到目标目录），对原始素材零写入；
    AI 层不存在任何能移动/删除/改写原素材的通道。"""
    etype = (payload.get("type") or "original").strip()
    if etype not in ("original", "image", "manifest"):
        raise HTTPException(400, "不支持的导出方式")
    target = os.path.expanduser((payload.get("target") or "").strip())
    if not target or not os.path.isabs(target):
        raise HTTPException(400, "请给出导出目标文件夹（绝对路径）")
    if not _path_allowed(Path(target)):
        raise HTTPException(403, "导出目标只能在硬盘/盘符或用户目录里（拒绝系统目录）")
    tag_ids = [x for x in (payload.get("tag_ids") or []) if TG.get(x)]
    kw = (payload.get("keywords") or "").strip()
    scope_in = payload.get("scope") or None
    scope_q = None
    if isinstance(scope_in, dict) and scope_in.get("volume_id"):
        scope_q = {"volume_id": scope_in["volume_id"], "rel_path": scope_in.get("rel_path") or ""}
    ids = _ai_match_ids(tag_ids, kw, scope_q)
    if not ids:
        raise HTTPException(400, "没有命中素材")
    if len(ids) > 5000:
        raise HTTPException(400, f"命中 {len(ids)} 条太多了，请先收窄条件再导出")
    options = {"format": payload.get("format") or "json"} if etype == "manifest" else {}
    return export.start_export(etype, ids, target, options)


@app.get("/api/duplicates")
def api_duplicates(limit: int = Query(80), include_ignored: int = Query(0)):
    """完全重复素材（内容指纹相同）。多盘备份场景：同一条素材在几块盘上都有。"""
    conn = db.connect()
    limit = max(1, min(limit, 200))
    ignored = {r[0] for r in conn.execute("SELECT content_id FROM dup_ignores").fetchall()}
    groups = conn.execute(
        "SELECT content_id, COUNT(*) n FROM assets WHERE content_id IS NOT NULL AND content_id!='' "
        "GROUP BY content_id HAVING n>1 ORDER BY n DESC LIMIT ?", (limit + len(ignored),),
    ).fetchall()
    if include_ignored:
        groups = [g for g in groups if g["content_id"] in ignored][:limit]
    else:
        groups = [g for g in groups if g["content_id"] not in ignored][:limit]
    vols = {v["volume_id"]: v for v in volumes.list_volumes()}
    out = []
    for g in groups:
        items = []
        for a in A.find_by_content(g["content_id"]):
            vol = vols.get(a["volume_id"], {})
            items.append({"asset_id": a["asset_id"], "name": a["name"], "kind": a["kind"],
                          "thumb": a.get("thumb_path") or "", "size": a.get("size") or 0,
                          "score": a.get("score") or 0, "rel_path": a["rel_path"],
                          "volume": vol.get("display_name") or vol.get("name") or "",
                          "online": bool(vol.get("online"))})
        out.append({"content_id": g["content_id"], "count": g["n"], "items": items})
    no_fp = conn.execute("SELECT COUNT(*) FROM assets WHERE content_id IS NULL OR content_id=''").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    return {"groups": out, "no_fingerprint": no_fp, "total_assets": total,
            "ignored_count": len(ignored)}


@app.post("/api/duplicates/backfill")
def api_duplicates_backfill():
    return scanner.backfill_content_ids()


@app.post("/api/duplicates/ignore")
def api_duplicates_ignore(payload: dict = Body(...)):
    cid = (payload.get("content_id") or "").strip()
    if not cid:
        raise HTTPException(400, "content_id 为空")
    if payload.get("restore"):
        db.write(lambda conn: conn.execute("DELETE FROM dup_ignores WHERE content_id=?", (cid,)))
    else:
        db.write(lambda conn: conn.execute(
            "INSERT OR IGNORE INTO dup_ignores(content_id, created_at) VALUES (?,?)", (cid, db.now())))
    return {"ok": True}


@app.get("/api/facets")
def api_facets(volume_id: str = Query(None), rel_path: str = Query("")):
    scope = {"volume_id": volume_id, "rel_path": rel_path} if (volume_id or rel_path) else None
    ids = list(S._scope_asset_ids(scope) or set()) if scope else \
        [r[0] for r in db.connect().execute("SELECT asset_id FROM assets").fetchall()]
    return {"facets": S.facet_counts(ids), "total": len(ids)}


# ── Tags / categories ───────────────────────────────────────────────────
def _tags_backup(reason: str):
    """破坏性词表操作的统一前置：备份失败就不执行（宁可拒绝操作，不冒丢词风险）。"""
    try:
        tag_io.backup(reason)
    except OSError as exc:
        raise HTTPException(500, f"自动备份失败，操作已取消：{exc}")


@app.get("/api/tags")
def api_tags():
    tags = [_tag_with_refs(t) for t in TG.list_()]
    return {"categories": TG.list_categories(), "tags": tags, "grouped": _group_tags(tags)}


@app.get("/api/tags/export")
def api_tags_export(with_refs: int = Query(0)):
    """标签库导出（词表定义；不含素材打标关系——完整备份请复制 out/miying.sqlite）。
    with_refs=1 时打包参考图为 zip。"""
    import time as _t
    from urllib.parse import quote
    stamp = _t.strftime("%Y%m%d")
    if with_refs:
        cn = quote(f"觅影标签库-{stamp}.zip")
        return Response(tag_io.export_zip_bytes(), media_type="application/zip",
                        headers={"Content-Disposition":
                                 f"attachment; filename=miying-tags-{stamp}.zip; filename*=UTF-8''{cn}"})
    cn = quote(f"觅影标签库-{stamp}.json")
    return Response(tag_io.export_json_text(), media_type="application/json; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename=miying-tags-{stamp}.json; filename*=UTF-8''{cn}"})


@app.post("/api/tags/import")
async def api_tags_import(request: Request, mode: str = Query("merge")):
    """标签库导入。merge（默认）：并入现有库，绝不删除；replace：备份后清空重建。
    请求体直接是 JSON 或 zip 的字节流（前端 file input 直传）。"""
    if mode not in ("merge", "replace"):
        raise HTTPException(400, "mode 只能是 merge 或 replace")
    if mode == "replace" and tasks.snapshot()["active"]:
        raise HTTPException(409, "有任务正在运行，请先取消全部任务再替换标签库")
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "空文件")
    try:
        return tag_io.import_blob(raw, mode=mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:
        raise HTTPException(500, f"自动备份失败，导入已取消：{exc}")


@app.post("/api/tags")
def api_tag_create(payload: dict = Body(...)):
    try:
        tag = TG.add(payload["name"], payload.get("category"), category_id=payload.get("category_id"),
                     aliases=payload.get("aliases"), note=payload.get("note"), ref_images=payload.get("ref_images"),
                     reuse_existing=False)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _tag_with_refs(tag)


@app.patch("/api/tag/{tid}")
def api_tag_update(tid: str, payload: dict = Body(...)):
    try:
        TG.update(tid, **payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    tag = TG.get(tid)
    if not tag:
        raise HTTPException(404, "标签不存在")
    return _tag_with_refs(tag)


@app.delete("/api/tag/{tid}")
def api_tag_delete(tid: str):
    _tags_backup("delete")
    TG.delete(tid)
    inheritance.rebuild_all()
    return {"ok": True}


@app.post("/api/tag/{tid}/alias")
def api_tag_alias(tid: str, payload: dict = Body(...)):
    try:
        TG.add_alias(tid, payload["alias"])
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _tag_with_refs(TG.get(tid))


@app.post("/api/tag/{tid}/merge")
def api_tag_merge(tid: str, payload: dict = Body(...)):
    target_id = payload.get("target_id")
    if not target_id:
        raise HTTPException(400, "目标标签为空")
    _tags_backup("merge")
    try:
        return TG.merge(tid, target_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/tag_merge_suggestions")
def api_tag_merge_suggestions(status: str = Query("pending")):
    return {"suggestions": TM.list_(status=status)}


@app.post("/api/tag_merge_suggestions/generate")
def api_tag_merge_suggestions_generate(payload: dict = Body(default={})):
    tags = TG.list_(include_disabled=False)
    mode = (payload.get("mode") or "normal").strip().lower()
    cfg = get_cfg()
    failed_batches = 0
    category = (payload.get("category") or "").strip()
    append = bool(payload.get("append"))
    if mode == "deep" and category:
        # 单类目模式：前端逐类目调用，实时显示进度；append=1 在已有 pending 上追加
        by_cat: dict[str, list[dict]] = {}
        for tag in tags:
            by_cat.setdefault(tag.get("category") or "未分类", []).append(tag)
        cat_tags = by_cat.get(category) or []
        if len(cat_tags) < 2:
            saved = {"ok": True, "count": 0} if append else TM.replace_pending([], model=None)
            return {**saved, "model": None, "mode": "deep", "category": category,
                    "calls": 0, "failed_batches": 0, "suggestions": TM.list_()}
        res = vision.suggest_tag_merges(cat_tags, cfg, model=payload.get("model"), max_suggestions=60)
        if not res.get("ok"):
            raise HTTPException(400, res.get("error") or "AI 整理失败")
        items = _resolve_tag_merge_suggestions(tags, (res.get("json") or {}).get("suggestions") or [],
                                               min_confidence=0.62, limit=60)
        saved = TM.append_pending(items, model=res.get("model")) if append \
            else TM.replace_pending(items, model=res.get("model"))
        return {**saved, "model": res.get("model"), "mode": "deep", "category": category,
                "calls": 1, "failed_batches": 0, "suggestions": TM.list_()}
    if mode == "deep":
        raw, model, calls = [], None, 0
        last_err = None
        by_cat: dict[str, list[dict]] = {}
        for tag in tags:
            by_cat.setdefault(tag.get("category") or "未分类", []).append(tag)
        for cat_tags in by_cat.values():
            if len(cat_tags) < 2:
                continue
            res = vision.suggest_tag_merges(cat_tags, cfg, model=payload.get("model"), max_suggestions=60)
            calls += 1
            model = res.get("model") or model
            if not res.get("ok"):
                # 单批失败跳过继续，不作废前面批次的结果
                failed_batches += 1
                last_err = res.get("error")
                continue
            raw.extend((res.get("json") or {}).get("suggestions") or [])
        if failed_batches and not raw:
            raise HTTPException(400, f"AI 整理失败（{failed_batches} 批全部出错）：{last_err or ''}")
        items = _resolve_tag_merge_suggestions(tags, raw, min_confidence=0.62, limit=160)
    else:
        res = vision.suggest_tag_merges(tags, cfg, model=payload.get("model"), max_suggestions=100)
        calls = 1
        model = res.get("model")
        if not res.get("ok"):
            raise HTTPException(400, res.get("error") or "AI 整理失败")
        items = _resolve_tag_merge_suggestions(tags, (res.get("json") or {}).get("suggestions") or [],
                                               min_confidence=0.7, limit=100)
    saved = TM.replace_pending(items, model=model)
    return {**saved, "model": model, "mode": "deep" if mode == "deep" else "normal",
            "calls": calls, "failed_batches": failed_batches, "suggestions": TM.list_()}


@app.post("/api/tag_merge_suggestion/{sid}/accept")
def api_tag_merge_suggestion_accept(sid: str):
    _tags_backup("merge")
    try:
        return TM.accept(sid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/tag_merge_suggestion/{sid}/reject")
def api_tag_merge_suggestion_reject(sid: str):
    try:
        return TM.reject(sid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/tag/{tid}/ref_asset")
def api_tag_ref_asset(tid: str, payload: dict = Body(...)):
    TG.set_ref_asset(tid, payload["asset_id"], payload.get("on", True))
    return _tag_with_refs(TG.get(tid))


@app.post("/api/tag/{tid}/ref_file")
def api_tag_ref_file(tid: str, payload: dict = Body(...)):
    src = Path(payload["path"]).expanduser()
    if src.suffix.lower() not in tag_io._REF_IMG_EXTS:
        raise HTTPException(400, "参考图只接受图片文件（jpg/png/webp/gif/bmp/heic）")
    if not _path_allowed(src):
        raise HTTPException(403, "该路径不在允许读取的范围（仅各硬盘/盘符和用户目录）")
    if not src.exists() or not src.is_file():
        raise HTTPException(400, "图片不存在")
    cfg = get_cfg()
    cfg.refimg_dir.mkdir(parents=True, exist_ok=True)
    dest = cfg.refimg_dir / f"{tid}_{src.name}"
    shutil.copy2(src, dest)
    TG.add_ref_file(tid, dest.name)
    return _tag_with_refs(TG.get(tid))


@app.post("/api/tag/{tid}/ref_remove")
def api_tag_ref_remove(tid: str, payload: dict = Body(...)):
    TG.remove_ref(tid, payload.get("ref") or payload)
    return _tag_with_refs(TG.get(tid))


@app.post("/api/categories")
def api_category_create(payload: dict = Body(...)):
    cid = TG.ensure_category(payload["name"], payload.get("color"))
    return {"category": next(c for c in TG.list_categories() if c["id"] == cid)}


@app.post("/api/categories/reorder")
def api_category_reorder(payload: dict = Body(...)):
    ids = payload.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "类目顺序为空")
    current = TG.list_categories()
    known = {c["id"] for c in current}
    ids = [str(x) for x in ids if str(x) in known]
    ids += [c["id"] for c in current if c["id"] not in ids]
    for i, cid in enumerate(ids):
        TG.update_category(cid, ord=i)
    return {"ok": True, "categories": TG.list_categories()}


@app.patch("/api/category/{cid}")
def api_category_update(cid: str, payload: dict = Body(...)):
    TG.update_category(cid, **payload)
    return {"category": next((c for c in TG.list_categories() if c["id"] == cid), None)}


@app.delete("/api/category/{cid}")
def api_category_delete(cid: str):
    TG.delete_category(cid)
    return {"ok": True, "categories": TG.list_categories()}


# ── Assets / folders ────────────────────────────────────────────────────
@app.get("/api/asset/{aid}")
def api_asset(aid: str):
    a = A.get(aid)
    if not a:
        raise HTTPException(404, "素材不存在")
    return _asset_detail(a)


@app.post("/api/asset/{aid}/tags")
def api_asset_add_tags(aid: str, payload: dict = Body(...)):
    a = A.get(aid)
    if not a:
        raise HTTPException(404, "素材不存在")
    tag_ids = _ensure_tag_ids(payload)
    for tid in tag_ids:
        A.add_own(aid, tid)
    inheritance.recompute(aid)
    return _asset_detail(A.get(aid))


@app.delete("/api/asset/{aid}/tag/{tid}")
def api_asset_remove_tag(aid: str, tid: str):
    if not A.get(aid):
        raise HTTPException(404, "素材不存在")
    A.remove_own(aid, tid)
    A.exclude(aid, tid)
    inheritance.recompute(aid)
    return {"ok": True}


@app.post("/api/asset/{aid}/desc")
def api_asset_desc(aid: str, payload: dict = Body(...)):
    desc = payload.get("desc_ai")
    if payload.get("manual", True) and desc is not None and not str(desc).strip():
        # 手动清空 = 解除锁定交还 AI（重新打标可再写）；非空手动保存则锁定防 AI 覆盖
        A.set_fields(aid, desc_ai="", desc_locked=0)
        if payload.get("note") is not None:
            A.set_description(aid, note=payload.get("note"))
    else:
        A.set_description(aid, desc_ai=desc, note=payload.get("note"), manual=payload.get("manual", True))
    inheritance.recompute(aid)
    return {"ok": True}


@app.post("/api/asset/{aid}/score")
def api_asset_score(aid: str, payload: dict = Body(...)):
    A.set_fields(aid, score=int(payload.get("score", 0)))
    inheritance.recompute(aid)
    return {"ok": True}


@app.post("/api/asset/{aid}/lock")
def api_asset_lock(aid: str, payload: dict = Body(...)):
    A.set_fields(aid, locked=1 if payload.get("locked") else 0)
    return {"ok": True}


@app.post("/api/asset/{aid}/fine_tag")
def api_asset_fine(aid: str):
    a = A.get(aid)
    if not a:
        raise HTTPException(404, "素材不存在")
    if not a.get("thumb_path"):
        raise HTTPException(400, "这个素材没有缩略图，请先生图")
    return scanner.fine_tag(a["volume_id"], asset_ids=[aid])


def _render_frame(a: dict, src: Path, dest: Path) -> bool:
    """按缩略图同款逻辑抽原分辨率代表帧（含 LUT/rep_ts），单张下载和批量 zip 共用。"""
    eff_lut = thumbnails.effective_lut(a)
    lut_filter = thumbnails.lut_filter_for(eff_lut)
    facts = a.get("facts") or {}
    rep_ts = facts.get("rep_ts") if a["kind"] in ("video", "film", "red") else None
    return frames.extract_fullres(src, a["kind"], lut_filter, dest, ts=rep_ts)


@app.get("/api/asset/{aid}/download")
def api_asset_download(aid: str):
    a = A.get(aid)
    if not a:
        raise HTTPException(404, "素材不存在")
    src = volumes.abspath(a)
    if src is None or not src.exists():
        raise HTTPException(409, "原文件离线或已删除")
    cfg = get_cfg()
    out_dir = cfg.out_dir / "downloads"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{aid}_frame.jpg"
    if not _render_frame(a, src, dest):
        raise HTTPException(500, "抽帧下载生成失败")
    filename = f"{Path(a['name']).stem}.jpg"
    return FileResponse(
        str(dest),
        filename=filename,
        media_type="image/jpeg",
        background=BackgroundTask(lambda p=dest: p.exists() and p.unlink()),
    )


@app.post("/api/assets/download_zip")
def api_assets_download_zip(payload: dict = Body(...)):
    """批量下载代表帧打 zip（≤40 个，同步生成；更多请走导出队列）。"""
    import time as _time
    import zipfile
    ids = _expand_asset_targets(payload)
    if not ids:
        raise HTTPException(400, "没有选中素材（未导入的文件夹请先快扫）")
    if len(ids) > 40:
        raise HTTPException(400, f"共 {len(ids)} 个素材，zip 一次最多 40 个；更多请用「导出 → 图片/帧」")
    cfg = get_cfg()
    out_dir = cfg.out_dir / "downloads"
    out_dir.mkdir(parents=True, exist_ok=True)
    zpath = out_dir / f"觅影帧_{_time.strftime('%Y%m%d_%H%M%S')}.zip"
    used, done, failed = set(), 0, 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for aid in ids:
            a = A.get(aid)
            src = volumes.abspath(a) if a else None
            if not a or src is None or not src.exists():
                failed += 1
                continue
            tmp = out_dir / f"{aid}_frame.jpg"
            if not _render_frame(a, src, tmp):
                failed += 1
                continue
            stem = Path(a["name"]).stem
            arc, i = f"{stem}.jpg", 1
            while arc in used:
                arc = f"{stem}_{i}.jpg"
                i += 1
            used.add(arc)
            zf.write(tmp, arc)
            done += 1
            try:
                tmp.unlink()
            except OSError:
                pass
    if not done:
        try:
            zpath.unlink()
        except OSError:
            pass
        raise HTTPException(500, "全部素材抽帧失败（离线或不可解码）")
    return FileResponse(
        str(zpath),
        filename=zpath.name,
        media_type="application/zip",
        headers={"X-Frames-Done": str(done), "X-Frames-Failed": str(failed)},
        background=BackgroundTask(lambda p=zpath: p.exists() and p.unlink()),
    )


def _clear_candidates(aid: str):
    """删掉一条素材的换帧候选图（选定/取消/重开前都清，零残留）。"""
    d = _CAND_DIR / aid
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


@app.get("/api/asset/{aid}/frame_candidates")
def api_frame_candidates(aid: str, n: int = 12):
    """换一帧：沿整段时长均匀抽 n 张候选帧（套同款生效 LUT、缩略图尺寸），按时间排序返回。
    候选图写在隔离的 out/cand/<aid>/ 下，由 /cand 静态路由临时托管，不与正式缩略图/孤儿清理相干。"""
    a = A.get(aid)
    if not a:
        raise HTTPException(404, "素材不存在")
    if a["kind"] not in ("video", "film", "red"):
        raise HTTPException(400, "只有视频可以换帧")
    src = volumes.abspath(a)
    if src is None or not src.exists():
        raise HTTPException(409, "原文件离线或已删除")
    facts = a.get("facts") or {}
    dur = facts.get("duration") or frames.video_duration(src)
    if not dur or dur <= 0:
        raise HTTPException(409, "读不到视频时长，换帧不可用")
    n = max(3, min(int(n), 24))
    lf = thumbnails.lut_filter_for(thumbnails.effective_lut(a))
    _clear_candidates(aid)
    cdir = _CAND_DIR / aid
    cdir.mkdir(parents=True, exist_ok=True)
    px = get_cfg().thumb_max_px
    cands = []
    for i in range(n):
        ts = round(float(dur) * (i + 1) / (n + 1), 3)   # 均匀铺开，跳过首尾
        dest = cdir / f"{i}.jpg"
        if frames._grab(src, ts, dest, px, lf):
            cands.append({"ts": ts, "url": f"/cand/{aid}/{i}.jpg"})
    if not cands:
        _clear_candidates(aid)
        raise HTTPException(500, "候选帧抽取失败（可能不可解码）")
    return {"ok": True, "candidates": cands, "current_ts": facts.get("rep_ts"),
            "duration": round(float(dur), 3)}


@app.post("/api/asset/{aid}/reframe")
def api_asset_reframe(aid: str, payload: dict = Body(...)):
    """选定某个候选帧：写回 rep_ts、按生效 LUT 重生成缩略图，并清掉候选。下游下载/导出自动跟随。"""
    try:
        res = thumbnails.set_rep_frame(aid, payload.get("ts"))
    finally:
        _clear_candidates(aid)   # 无论成败都清候选，零残留
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "换帧失败")
    a = A.get(aid)
    return {"ok": True, "thumb": res.get("thumb"), "thumb_v": _thumb_version(a) if a else 0}


@app.post("/api/asset/{aid}/frame_candidates/clear")
def api_frame_candidates_clear(aid: str):
    """取消换帧：丢弃候选图。"""
    _clear_candidates(aid)
    return {"ok": True}


@app.post("/api/asset/{aid}/reveal")
def api_asset_reveal(aid: str):
    """在文件管理器中显示原文件（mac: Finder / Windows: 资源管理器）。"""
    a = A.get(aid)
    if not a:
        raise HTTPException(404, "素材不存在")
    src = volumes.abspath(a)
    if src is None or not src.exists():
        raise HTTPException(409, "原文件离线或已删除")
    try:
        osplat.reveal(src)
    except osplat.DialogUnsupported as e:
        raise HTTPException(409, str(e))
    except (subprocess.SubprocessError, OSError) as e:
        raise HTTPException(500, str(e))
    return {"ok": True}


@app.get("/api/folder/{fid}")
def api_folder(fid: str):
    f = folders.get(fid)
    if not f:
        raise HTTPException(404, "文件夹不存在")
    return {**f, "tags_resolved": _resolve_folder(f)}


@app.post("/api/folder/{fid}/tags")
def api_folder_tags(fid: str, payload: dict = Body(...)):
    f = folders.get(fid)
    if not f:
        raise HTTPException(404, "文件夹不存在")
    tag_ids = _ensure_tag_ids(payload)
    folders.add_tags(fid, tag_ids)
    inheritance.recompute_subtree(f["volume_id"], f["rel_path"])
    return api_folder(fid)


@app.delete("/api/folder/{fid}/tag/{tid}")
def api_folder_remove_tag(fid: str, tid: str):
    f = folders.get(fid)
    if not f:
        raise HTTPException(404, "文件夹不存在")
    folders.remove_tags(fid, [tid])
    inheritance.recompute_subtree(f["volume_id"], f["rel_path"])
    return {"ok": True}


@app.delete("/api/folder/{fid}/common_tag/{tid}")
def api_folder_remove_common_tag(fid: str, tid: str):
    f = folders.get(fid)
    if not f:
        raise HTTPException(404, "文件夹不存在")
    folders.remove_tags(fid, [tid])
    for aid in _asset_ids_under_folder(f):
        A.remove_own(aid, tid)
        A.exclude(aid, tid)
    inheritance.recompute_subtree(f["volume_id"], f["rel_path"])
    return {"ok": True}


@app.post("/api/folder/{fid}/fine_tag")
def api_folder_fine(fid: str):
    f = folders.get(fid)
    if not f:
        raise HTTPException(404, "文件夹不存在")
    return scanner.fine_tag(f["volume_id"], f["rel_path"])


@app.post("/api/folder/{fid}/fill_thumbs")
def api_folder_fill(fid: str):
    f = folders.get(fid)
    if not f:
        raise HTTPException(404, "文件夹不存在")
    return scanner.fill_thumbs(f["volume_id"], f["rel_path"])


@app.post("/api/folder/{fid}/hide")
def api_folder_hide(fid: str, payload: dict = Body(default={})):
    f = folders.get(fid)
    if not f:
        raise HTTPException(404, "文件夹不存在")
    folders.set_meta(fid, hidden=1 if payload.get("hidden", True) else 0)
    return {"ok": True}


@app.post("/api/path/hide")
def api_path_hide(payload: dict = Body(...)):
    path = payload.get("path")
    if not path:
        raise HTTPException(400, "路径为空")
    info = volumes.identify(path)
    fid = folders.ensure(info["volume_id"], info["rel_path"])
    folders.set_meta(fid, hidden=1 if payload.get("hidden", True) else 0)
    return {"ok": True, "folder_id": fid}


@app.get("/api/hidden")
def api_hidden():
    rows = db.connect().execute(
        "SELECT * FROM folders WHERE hidden=1 ORDER BY updated_at DESC, name"
    ).fetchall()
    items = []
    for r in rows:
        f = folders._row(r)
        m = volumes.resolve_mount(f["volume_id"])
        items.append({**f, "path": str(m / f["rel_path"]) if m else None, "online": bool(m)})
    return {"folders": items}


@app.post("/api/hidden/{fid}/restore")
def api_hidden_restore(fid: str):
    if not folders.get(fid):
        raise HTTPException(404, "文件夹不存在")
    folders.set_meta(fid, hidden=0)
    return {"ok": True}


@app.post("/api/apply")
def api_apply(payload: dict = Body(...)):
    targets = payload.get("targets", [])
    op = payload.get("op")
    if not targets:
        raise HTTPException(400, "没有选中对象")
    tag_ids = _ensure_tag_ids(payload) if op in ("add_tags", "remove_tags") else []
    changed_assets, changed_subtrees, scans, full_scans, cleanup_scopes, fine_assets, thumb_assets = set(), set(), [], [], [], [], []
    sync_scans: list[str] = []
    deleted = 0
    fine_skipped = 0
    clear_assets: list[str] = []
    clear_folder_scopes: list[tuple[str, str]] = []
    for tgt in targets:
        typ = tgt.get("type")
        if typ == "path":
            if op == "scan":
                scans.append(tgt["path"])
            elif op == "full_scan":
                full_scans.append(tgt["path"])
            elif op == "hide":
                info = volumes.identify(tgt["path"])
                fid = folders.ensure(info["volume_id"], info["rel_path"])
                folders.set_meta(fid, hidden=1)
            elif op == "add_tags":
                info = volumes.identify(tgt["path"])
                scanner.ensure_current_folder(info["volume_id"], info["rel_path"])
                fid = folders.ensure(info["volume_id"], info["rel_path"])
                folders.add_tags(fid, tag_ids)
                changed_subtrees.add((info["volume_id"], info["rel_path"]))
            elif op == "remove_tags":
                info = volumes.identify(tgt["path"])
                fid = folders.ensure(info["volume_id"], info["rel_path"])
                folders.remove_tags(fid, tag_ids)
                changed_subtrees.add((info["volume_id"], info["rel_path"]))
            elif op == "cleanup":
                info = volumes.identify(tgt["path"])
                cleanup_scopes.append((info["volume_id"], info["rel_path"]))
            elif op == "sync":
                # 同步=双向对账：先扫描（新增入库+生图+补打未打标），再清理丢失。
                # 任务同队列串行，扫描在前保证 搬家/改名识别 先于清理执行，标签不丢。
                info = volumes.identify(tgt["path"])
                sync_scans.append(tgt["path"])
                cleanup_scopes.append((info["volume_id"], info["rel_path"]))
            elif op == "clear_tagging":
                # 路径形态的目标（如根视图的盘卡片）也要能清，否则静默跳过
                info = volumes.peek(tgt["path"])
                pf = folders.get_by_path(info["volume_id"], info["rel_path"])
                if pf:
                    clear_assets += _asset_ids_under_folder(pf)
                    clear_folder_scopes.append((pf["volume_id"], pf["rel_path"]))
            continue
        if typ == "folder":
            f = folders.get(tgt.get("id"))
            if not f:
                continue
            scoped_assets = _asset_ids_under_folder(f)
            if op == "scan":
                mnt = volumes.resolve_mount(f["volume_id"])
                if mnt:
                    scans.append(str(mnt / f["rel_path"]))
            elif op == "full_scan":
                mnt = volumes.resolve_mount(f["volume_id"])
                if mnt:
                    full_scans.append(str(mnt / f["rel_path"]))
            elif op == "fine":
                # 与 scanner.fine_tag 同一口径：文件夹打标只取有缩略图的（无图的统计为跳过）
                _all = _asset_ids_under_folder(f, unlocked_only=True)
                _ready = _asset_ids_under_folder(f, unlocked_only=True, require_thumb=True)
                fine_assets += _ready
                fine_skipped += len(_all) - len(_ready)
            elif op == "thumbs":
                thumb_assets += _asset_ids_under_folder(f, kinds=fmod.THUMBABLE_KINDS)
            elif op == "add_tags":
                folders.add_tags(f["folder_id"], tag_ids)
                changed_subtrees.add((f["volume_id"], f["rel_path"]))
            elif op == "remove_tags":
                folders.remove_tags(f["folder_id"], tag_ids)
                changed_subtrees.add((f["volume_id"], f["rel_path"]))
            elif op == "lock":
                for aid in scoped_assets:
                    A.set_fields(aid, locked=1 if payload.get("locked", True) else 0)
            elif op == "score":
                score = max(0, min(5, int(payload.get("score", 0))))
                for aid in scoped_assets:
                    A.set_fields(aid, score=score)
            elif op == "hide":
                folders.set_meta(f["folder_id"], hidden=1)
            elif op == "delete_records":
                for aid in scoped_assets:
                    _delete_asset_record(aid)
                    deleted += 1
            elif op == "clear_tagging":
                clear_assets += scoped_assets
                clear_folder_scopes.append((f["volume_id"], f["rel_path"]))
            elif op == "set_lut":
                lut = payload.get("lut")
                folders.set_lut(f["folder_id"], None if lut in ("", "原片", thumbnails.NONE_LUT, None) else lut)
                changed_subtrees.add((f["volume_id"], f["rel_path"]))
                thumb_assets += _asset_ids_under_folder(f, kinds=fmod.THUMBABLE_KINDS)
            elif op == "cleanup":
                cleanup_scopes.append((f["volume_id"], f["rel_path"]))
            elif op == "sync":
                mnt = volumes.resolve_mount(f["volume_id"])
                if mnt:   # 盘离线：不扫描；cleanup_scope 自己也会拒绝（绝不判删除）
                    sync_scans.append(str(mnt / f["rel_path"]))
                cleanup_scopes.append((f["volume_id"], f["rel_path"]))
            continue
        if typ == "asset":
            aid = tgt.get("id")
            a = A.get(aid)
            if not a:
                continue
            if op == "add_tags":
                for tid in tag_ids:
                    A.add_own(aid, tid)
                changed_assets.add(aid)
            elif op == "remove_tags":
                for tid in tag_ids:
                    A.remove_own(aid, tid); A.exclude(aid, tid)
                changed_assets.add(aid)
            elif op == "fine":
                fine_assets.append(aid)
            elif op == "thumbs":
                if a.get("kind") in fmod.THUMBABLE_KINDS:
                    thumb_assets.append(aid)
            elif op == "lock":
                A.set_fields(aid, locked=1 if payload.get("locked", True) else 0)
            elif op == "score":
                A.set_fields(aid, score=max(0, min(5, int(payload.get("score", 0)))))
            elif op == "ignore":
                A.set_fields(aid, status="ignored")
                changed_assets.add(aid)
            elif op == "clear_tagging":
                clear_assets.append(aid)
            elif op == "delete_records":
                _delete_asset_record(aid)
                deleted += 1
            elif op == "set_lut":
                lut = payload.get("lut")
                A.set_lut(aid, None if lut in ("", "继承", None) else lut)
                changed_assets.add(aid)
                if a.get("kind") in fmod.THUMBABLE_KINDS:
                    thumb_assets.append(aid)
    cleared = locked_skipped = folder_tags_cleared = 0
    if clear_assets or clear_folder_scopes:
        with_thumbs = bool(payload.get("with_thumbs"))
        done_ids = []
        for aid in dict.fromkeys(clear_assets):
            res = _clear_tagging(aid, with_thumbs)
            if res is True:
                done_ids.append(aid)
                cleared += 1
            elif res is False:
                locked_skipped += 1
        # 清文件夹时，连文件夹层挂的标签（继承来源）一起清——否则下级素材的
        # effective 里仍带着继承标签，看起来"没清干净"
        for vol, rel in clear_folder_scopes:
            fids = folders.descendants_ids(vol, rel)
            for ch in db.chunks(fids):
                qs = ",".join("?" * len(ch))

                def _w(conn, _ch=ch, _qs=qs):
                    n = conn.execute(f"DELETE FROM folder_tags WHERE folder_id IN ({_qs})", _ch).rowcount
                    if with_thumbs:
                        # 范围内文件夹的封面缩略图刚被删，存档的 cover_thumb 一并清掉
                        conn.execute(f"UPDATE folders SET cover_thumb='' WHERE folder_id IN ({_qs})", _ch)
                    return n
                folder_tags_cleared += db.write(_w) or 0
        # 子树重算覆盖文件夹目标（含锁定素材的继承变化）；单选素材目标单独补算
        scoped = set()
        for vol, rel in dict.fromkeys(clear_folder_scopes):
            inheritance.recompute_subtree(vol, rel)
            scoped.update(_asset_ids_under_folder({"volume_id": vol, "rel_path": rel}))
        inheritance.recompute_many([a for a in done_ids if a not in scoped])
    for vol, rel in changed_subtrees:
        inheritance.recompute_subtree(vol, rel)
    inheritance.recompute_many(changed_assets)
    for path in scans:
        scanner.quick_import(path, mode="quick")
    for path in full_scans:
        scanner.quick_import(path, mode="full")
    for path in sync_scans:
        # 扫描任务必须先于清理任务创建（index 队列按创建顺序串行执行）
        scanner.quick_import(path, mode="full", title=f"同步 {Path(path).name or path}")
    cleanup_tasks = []
    for vol, rel in cleanup_scopes:
        cleanup_tasks.append(scanner.cleanup_scope(vol, rel))
    thumb_task = None
    if thumb_assets:
        thumb_task = tasks.create_task("thumb", "thumb_gen", "生图", list(dict.fromkeys(thumb_assets)))
    out = {"ok": True, "scanned": len(scans), "deleted": deleted, "thumb_task": thumb_task,
           "cleared": cleared, "locked_skipped": locked_skipped,
           "folder_tags_cleared": folder_tags_cleared}
    if fine_assets or fine_skipped:
        # 统一行为：只打有缩略图的素材，没图的跳过并汇报数量（不再整单 400 报错）
        fine_assets = list(dict.fromkeys(fine_assets))
        rows = [a for a in (A.get(aid) for aid in fine_assets) if a]
        ready = [a["asset_id"] for a in rows if a.get("thumb_path") and not a.get("locked")]
        locked_n = sum(1 for a in rows if a.get("thumb_path") and a.get("locked"))
        skipped = fine_skipped + (len(rows) - len(ready) - locked_n)
        if not ready:
            raise HTTPException(400, "选中对象里没有可打标的素材：请先生图（RED .R3D 未抽帧成功会被跳过）")
        first = A.get(ready[0])
        out["fine"] = scanner.fine_tag(first["volume_id"], asset_ids=ready) if first else None
        if skipped:
            out["skipped_no_thumb"] = skipped
        if locked_n:
            out["skipped_locked"] = locked_n
    if full_scans:
        out["full_scan"] = len(full_scans)
    if sync_scans:
        out["synced"] = len(sync_scans)
    if cleanup_tasks:
        out["cleanup"] = cleanup_tasks
    return out


# ── Review suggestions ─────────────────────────────────────────────────
@app.get("/api/candidates")
def api_candidates(status: str = Query("pending")):
    items = C.list_(status=status)
    for c in items:
        c["samples"] = _candidate_samples(c)
        c["folders_n"] = len(c["hits"].get("folders", []))
        c["assets_n"] = len(c["hits"].get("assets", []))
    watching = C.list_watching(30)
    need = lambda c: 2 if (c.get("category") in TG.PLACE_CATS) else 3   # 地点 2 次，其他 3 次
    return {"candidates": items, "counts": C.counts(), "categories": TG.list_categories(),
            "watching": {"count": C.watching_count(),
                         "items": [{"term": c["term"], "hits": c["hit_count"], "need": need(c)} for c in watching]}}


@app.post("/api/candidates/clear_watching")
def api_candidates_clear_watching():
    C.clear_watching()
    return {"ok": True}


@app.get("/api/tags/lowuse")
def api_tags_lowuse(max_uses: int = Query(2)):
    """低频标签瘦身候选：使用次数 ≤ max_uses，排除 固定词表/置顶/有卡片（别名/备注/参考图）的标签。"""
    conn = db.connect()
    max_uses = max(0, min(int(max_uses), 50))
    eff = {r["value"]: r["n"] for r in conn.execute(
        "SELECT value, COUNT(DISTINCT asset_id) n FROM asset_effective WHERE field='tag' GROUP BY value").fetchall()}
    fol = {r["tag_id"]: r["n"] for r in conn.execute(
        "SELECT tag_id, COUNT(*) n FROM folder_tags GROUP BY tag_id").fetchall()}
    core = TG.core_vocabulary_terms()
    items = []
    for t in TG.list_():
        if t.get("pinned") or t.get("has_card"):
            continue
        if TG.normalize_term(t["name"]) in core:
            continue
        uses = eff.get(t["id"], 0) + fol.get(t["id"], 0)
        if uses <= max_uses:
            items.append({"id": t["id"], "name": t["name"], "category": t["category"],
                          "color": t["color"], "uses": uses})
    items.sort(key=lambda x: (x["uses"], x["category"], x["name"]))
    return {"tags": items, "max_uses": max_uses}


@app.post("/api/tags/prune")
def api_tags_prune(payload: dict = Body(...)):
    """批量删除低频标签（存量治理）。固定词表双保险不可删；只动觅影库记录。"""
    ids = [x for x in (payload.get("tag_ids") or []) if x]
    if not ids:
        raise HTTPException(400, "没有选中标签")
    _tags_backup("prune")
    core = TG.core_vocabulary_terms()
    deleted = 0
    for tid in ids:
        t = TG.get(tid)
        if not t or TG.normalize_term(t["name"]) in core:
            continue
        TG.delete(tid)
        deleted += 1
    return {"ok": True, "deleted": deleted}


@app.post("/api/candidate/{cid}/confirm")
def api_candidate_confirm(cid: str, payload: dict = Body(default={})):
    return C.confirm(cid, category=payload.get("category"))


@app.post("/api/candidate/{cid}/merge")
def api_candidate_merge(cid: str, payload: dict = Body(...)):
    target_id = payload.get("target_id")
    if not target_id:
        raise HTTPException(400, "目标标签为空")
    res = C.merge_into_tag(cid, target_id)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "合并失败")
    return res


@app.post("/api/candidate/{cid}/ignore")
def api_candidate_ignore(cid: str):
    C.reject(cid)
    return {"ok": True}


@app.post("/api/candidates/reject_all")
def api_candidate_reject_all():
    C.reject_all()
    return {"ok": True}


@app.post("/api/candidates/confirm_all")
def api_candidate_confirm_all():
    return C.confirm_all()


@app.get("/api/featured")
def api_featured(limit: int = Query(260)):
    limit = max(1, min(limit, 500))
    ids = [r[0] for r in db.connect().execute(
        "SELECT asset_id FROM assets WHERE score>0 ORDER BY score DESC, updated_at DESC, mtime DESC LIMIT ?",
        (limit,),
    ).fetchall()]
    return {"assets": S._hydrate(ids), "total": len(ids)}


# ── Tasks / settings / export ───────────────────────────────────────────
@app.get("/api/tasks")
def api_tasks():
    snap = tasks.snapshot()
    snap["pending_candidates"] = C.counts()["total"]
    for t in snap.get("tasks", []):
        t["current_label"] = _task_current_label(t.get("current") or "")
    return snap


@app.post("/api/tasks/pause_ai")
def api_pause_ai():
    tasks.pause_ai(); return {"ok": True}


@app.post("/api/tasks/resume_ai")
def api_resume_ai():
    tasks.resume_ai(); return {"ok": True}


@app.post("/api/tasks/cancel_all")
def api_cancel_all():
    for t in tasks.snapshot()["tasks"]:
        tasks.cancel_task(t["task_id"])
    return {"ok": True}


@app.post("/api/task/{tid}/{action}")
def api_task_action(tid: str, action: str):
    {"pause": tasks.pause_task, "resume": tasks.resume_task, "cancel": tasks.cancel_task}[action](tid)
    return {"ok": True}


def _lan_addresses() -> list[str]:
    """本机的私网 IPv4 地址（给手机访问地址用）。"""
    ips: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if _PRIVATE_HOST_RE.match(ip))


def _lan_listening(ips: list[str]) -> bool:
    """实测服务是否真的在局域网口监听（区分"开关开了但还没重启/被防火墙拦"）。"""
    for ip in ips[:2]:
        try:
            with socket.create_connection((ip, 8788), timeout=0.4):
                return True
        except OSError:
            continue
    return False


@app.get("/api/settings")
def api_settings():
    cfg = get_cfg()
    lan = {"enabled": cfg.lan_access, "token": cfg.lan_token, "urls": [], "listening": None}
    if cfg.lan_access and cfg.lan_token:
        ips = _lan_addresses()
        lan["urls"] = [f"http://{ip}:8788/?token={cfg.lan_token}" for ip in ips]
        lan["listening"] = _lan_listening(ips)
    return {"model": cfg.model_settings(), "provider": cfg.provider,
            "app": {"auto_lut": cfg.auto_lut, "lan_access": cfg.lan_access,
                    "update_check": cfg.update_check, "window_mode": cfg.window_mode}, "lan": lan}


@app.post("/api/settings/app")
def api_settings_app(payload: dict = Body(...)):
    cfg = get_cfg()
    updates: dict = {}
    if "auto_lut" in payload:
        updates["auto_lut"] = bool(payload.get("auto_lut", True))
    if "update_check" in payload:
        updates["update_check"] = bool(payload["update_check"])
    if "window_mode" in payload:
        updates["window_mode"] = bool(payload["window_mode"])
    if "lan_access" in payload:
        updates["lan_access"] = bool(payload["lan_access"])
        if updates["lan_access"] and not cfg.lan_token:
            updates["lan_token"] = secrets.token_hex(6)   # 首次开启生成 12 位口令
    if payload.get("regen_lan_token"):
        updates["lan_token"] = secrets.token_hex(6)
    if updates:
        cfg.save_app_settings(updates)
    cfg = get_cfg()
    return {"ok": True, "app": {"auto_lut": cfg.auto_lut, "lan_access": cfg.lan_access}}


@app.get("/api/setup/status")
def api_setup_status():
    """首跑向导判定 + 环境探测。老库（已有素材）自动视为已完成，升级用户不被打扰。"""
    cfg = get_cfg()
    has_data = bool(db.connect().execute("SELECT 1 FROM assets LIMIT 1").fetchone())
    if has_data and not cfg.setup_done:
        cfg.save_app_settings({"setup_done": True})
    first_run = not (cfg.setup_done or has_data)
    ollama_alive = False
    try:
        import requests as _rq
        origin = (cfg.local_base_url or "http://localhost:11434/v1").split("/v1")[0].rstrip("/")
        ollama_alive = _rq.get(origin + "/api/tags", timeout=1.2).status_code == 200
    except Exception:
        pass
    import platform as _pf
    return {"first_run": first_run, "ollama_alive": ollama_alive,
            "has_api_key": bool(cfg.remote_api_key), "provider": cfg.provider,
            "arch": _pf.machine()}


@app.post("/api/setup/done")
def api_setup_done(payload: dict = Body(default={})):
    get_cfg().save_app_settings({"setup_done": True})
    return {"ok": True}


@app.post("/api/settings/model")
def api_settings_model(payload: dict = Body(...)):
    get_cfg().save_model_settings(payload)
    return {"ok": True, "model": get_cfg().model_settings()}


@app.get("/api/settings/models")
def api_settings_models():
    ok, models, err = vision.list_models(get_cfg())
    return {"ok": ok, "models": models, "error": err}


@app.post("/api/models/list")
def api_models_list(payload: dict = Body(...)):
    """按表单值列模型（临时配置，不落盘）——设置页双卡片的「获取列表」。"""
    import copy
    tmp = copy.copy(get_cfg())
    tmp.apply_model_settings(payload)
    ok, models, err = vision.list_models(tmp)
    return {"ok": ok, "models": models, "error": err}


@app.post("/api/settings/models")
def api_settings_models_post(payload: dict = Body(...)):
    get_cfg().save_model_settings(payload)
    return api_settings_models()


@app.get("/api/ping")
def api_ping():
    ok, msg = vision.ping(get_cfg())
    return {"ok": ok, "message": msg}


@app.post("/api/ping")
def api_ping_post(payload: dict = Body(...)):
    """测试连接：用临时配置探活，不落盘（想保存请走 /api/settings/model）。"""
    import copy
    tmp = copy.copy(get_cfg())
    tmp.apply_model_settings(payload)
    ok, msg = vision.ping(tmp)
    return {"ok": ok, "message": msg}


@app.get("/api/luts")
def api_luts():
    cfg = get_cfg()
    builtin = list(cfg.luts.keys())
    all_luts = cfg.all_luts()
    return {"luts": [{"name": n, "builtin": n in builtin, "exists": (cfg.lut_path(n) or Path("/x")).exists()} for n in all_luts],
            "modes": ["原片"] + list(all_luts.keys())}


@app.post("/api/luts/add")
def api_lut_add(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    path = Path(payload.get("path") or "").expanduser()
    if not name:
        raise HTTPException(400, "LUT 名称为空")
    if not path.exists() or path.suffix.lower() != ".cube":
        raise HTTPException(400, "请选择 .cube 文件")
    get_cfg().add_user_lut(name, str(path))
    return api_luts()


@app.delete("/api/lut/{name}")
def api_lut_delete(name: str):
    res = thumbnails.remove_lut_preset(name)
    if res.get("affected"):
        tasks.create_task("thumb", "thumb_gen", "生图", res["affected"])
    return res


@app.post("/api/luts/delete")
def api_lut_delete_post(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "LUT 名称为空")
    return api_lut_delete(name)


@app.post("/api/scan/cleanup")
def api_scan_cleanup(payload: dict = Body(...)):
    return scanner.cleanup_scope(payload["volume_id"], payload.get("rel_path") or "")


@app.get("/api/ref_candidates")
def api_ref_candidates(q: str = Query(""), limit: int = Query(30)):
    limit = max(1, min(limit, 160))
    items = []
    if q:
        res = S.search(query=q, limit=limit, with_facets=False)
        for a in res.get("assets", []):
            if a.get("thumb"):
                items.append({"asset_id": a["asset_id"], "name": a["name"], "thumb": a["thumb"], "score": a.get("score", 0)})
    if len(items) < limit:
        rows = db.connect().execute(
            "SELECT asset_id,name,thumb_path,score FROM assets WHERE thumb_path IS NOT NULL AND thumb_path!='' "
            "ORDER BY score DESC, updated_at DESC LIMIT ?",
            (limit - len(items),),
        ).fetchall()
        seen = {x["asset_id"] for x in items}
        for r in rows:
            if r["asset_id"] not in seen:
                items.append({"asset_id": r["asset_id"], "name": r["name"], "thumb": r["thumb_path"], "score": r["score"]})
    items.sort(key=lambda x: (-(x.get("score") or 0), x.get("name") or ""))
    return {"assets": items[:limit]}


def _expand_asset_targets(payload: dict) -> list[str]:
    """asset_ids + targets（asset/folder/path 混选）展开为素材 id 列表（文件夹含子夹）。"""
    ids = list(payload.get("asset_ids") or [])
    for tgt in payload.get("targets") or []:
        typ = tgt.get("type")
        if typ == "asset":
            ids.append(tgt.get("id"))
        elif typ == "folder":
            f = folders.get(tgt.get("id"))
            if f:
                ids += _asset_ids_under_folder(f)
        elif typ == "path":
            info = volumes.peek(tgt.get("path") or "")
            f = folders.get_by_path(info["volume_id"], info["rel_path"])
            if f:
                ids += _asset_ids_under_folder(f)
    return list(dict.fromkeys([x for x in ids if x]))


@app.post("/api/export")
def api_export(payload: dict = Body(...)):
    target = os.path.expanduser((payload.get("target") or "").strip())
    if not target:
        raise HTTPException(400, "请选择导出目标文件夹")
    if not os.path.isabs(target):
        raise HTTPException(400, "导出目标需要绝对路径")
    if not _path_allowed(Path(target)):
        raise HTTPException(403, "导出目标只能在硬盘/盘符或用户目录里（拒绝系统目录）")
    ids = _expand_asset_targets(payload)
    if not ids:
        raise HTTPException(400, "选中对象里没有已登记素材（未导入的文件夹请先快扫）")
    return export.start_export(payload["type"], ids, target, payload.get("options"))


@app.post("/api/db/reset")
def api_db_reset(payload: dict = Body(default={})):
    if payload.get("confirm") not in ("YEEHX", "RESET"):   # RESET 兼容老脚本
        raise HTTPException(400, "需要 confirm=YEEHX")
    if tasks.snapshot()["active"]:
        raise HTTPException(409, "有任务正在运行，请先取消全部任务再重置数据库")
    wiped = 0
    if payload.get("wipe_thumbs"):
        # 账本删了之后缩略图就是孤儿文件（重扫同名覆盖、不扫白占空间），默认一起清
        cfg = get_cfg()
        if cfg.thumbs_dir.exists():
            for p in cfg.thumbs_dir.glob("*"):
                if p.is_file():
                    p.unlink(missing_ok=True)
                    wiped += 1
    db.reset_db()
    TG.invalidate_term_cache()
    TG.seed_if_empty()
    TG.ensure_core_vocabulary()   # 不用等下次重启就有完整固定词表（与 4-3 恢复出厂一致）
    _audit("db_reset", thumbs_wiped=wiped)
    return {"ok": True, "thumbs_wiped": wiped}


# ── 危险区（设置页）：统一规范 = confirm短语 + 任务运行409 + 影响预览 + 备份前置 + 审计行 ──
def _audit(op: str, **info):
    """破坏性操作审计行：结构化一行进日志（stdout → out/app.log）。"""
    import time as _t
    print("[审计] " + db.jdumps({"time": _t.strftime("%Y-%m-%d %H:%M:%S"), "op": op, **info}),
          flush=True)


def _thumbs_stats() -> dict:
    cfg = get_cfg()
    files = [p for p in cfg.thumbs_dir.glob("*") if p.is_file()] if cfg.thumbs_dir.exists() else []
    return {"count": len(files), "bytes": sum(p.stat().st_size for p in files)}


# ── 素材库体检（4-1）────────────────────────────────────────────────────
_PING_CACHE = {"t": 0.0, "ok": None, "msg": ""}


def _model_health() -> dict:
    import time as _t
    if _PING_CACHE["ok"] is None or _t.time() - _PING_CACHE["t"] > 300:   # 探活缓存 5 分钟
        ok, msg = vision.ping(get_cfg())
        _PING_CACHE.update(t=_t.time(), ok=ok, msg=msg)
    return {"ok": bool(_PING_CACHE["ok"]), "message": _PING_CACHE["msg"]}


def _dangling_thumb_assets(conn) -> list[str]:
    cfg = get_cfg()
    existing = {p.name for p in cfg.thumbs_dir.glob("*")} if cfg.thumbs_dir.exists() else set()
    return [r[0] for r in conn.execute(
        "SELECT asset_id, thumb_path FROM assets WHERE thumb_path IS NOT NULL AND thumb_path!=''").fetchall()
        if r[1] not in existing]


@app.get("/api/checkup")
def api_checkup():
    """素材库体检：一屏看清 离线卷/缺图/悬空引用/指纹失败/候选词/模型连通/任务。"""
    conn = db.connect()
    n = lambda q: conn.execute(q).fetchone()[0]
    vols = []
    online_ids = set()
    for v in volumes.list_volumes():
        on = volumes.resolve_mount(v["volume_id"]) is not None
        if on:
            online_ids.add(v["volume_id"])
        cnt = conn.execute("SELECT COUNT(*) FROM assets WHERE volume_id=?", (v["volume_id"],)).fetchone()[0]
        vols.append({"name": v.get("display_name") or v.get("name") or "?", "online": on, "assets": cnt})
    missing_rows = conn.execute(
        "SELECT volume_id, COUNT(*) cnt FROM assets WHERE thumb_path IS NULL OR thumb_path='' "
        "GROUP BY volume_id").fetchall()
    miss_off = sum(r["cnt"] for r in missing_rows if r["volume_id"] not in online_ids)
    miss_on = sum(r["cnt"] for r in missing_rows if r["volume_id"] in online_ids)
    dangling = _dangling_thumb_assets(conn)
    recent = [f'{r["item_key"][:20]}: {(r["error"] or "")[:60]}' for r in conn.execute(
        "SELECT ti.item_key, ti.error FROM task_items ti JOIN tasks t ON t.task_id=ti.task_id "
        "WHERE ti.status='failed' AND ti.error IS NOT NULL ORDER BY t.updated_at DESC LIMIT 5").fetchall()]
    return {
        "assets_total": n("SELECT COUNT(*) FROM assets"),
        "volumes": vols,
        "thumbs": {"missing_total": miss_off + miss_on, "offline": miss_off, "pending": miss_on,
                   "dangling": len(dangling)},
        "fingerprints": {"missing": n("SELECT COUNT(*) FROM assets WHERE content_id IS NULL OR content_id=''"),
                         "errors": n("SELECT COUNT(*) FROM assets WHERE facts_json LIKE '%\"fingerprint_error\"%'")},
        "candidates": {"pending": n("SELECT COUNT(*) FROM suggestions WHERE status IN ('pending','watching')"),
                       "total": n("SELECT COUNT(*) FROM suggestions")},
        "model": _model_health(),
        "tasks_active": tasks.snapshot()["active"],
        "recent_errors": recent,
    }


@app.post("/api/checkup/clean_dangling")
def api_checkup_clean_dangling():
    """清理悬空缩略图引用：thumb_path 指向已不存在的文件 → 置空（含文件夹封面）。"""
    conn = db.connect()
    ids = _dangling_thumb_assets(conn)
    cfg = get_cfg()
    existing = {p.name for p in cfg.thumbs_dir.glob("*")} if cfg.thumbs_dir.exists() else set()

    def _w(c):
        t = db.now()
        for ch in db.chunks(ids):
            qs = ",".join("?" * len(ch))
            c.execute(f"UPDATE assets SET thumb_path='', thumb_lut='', updated_at=? "
                      f"WHERE asset_id IN ({qs})", [t] + ch)
        for r in c.execute("SELECT folder_id, cover_thumb FROM folders "
                           "WHERE cover_thumb IS NOT NULL AND cover_thumb!=''").fetchall():
            if r["cover_thumb"] not in existing:
                c.execute("UPDATE folders SET cover_thumb='', updated_at=? WHERE folder_id=?",
                          (t, r["folder_id"]))
    db.write(_w)
    _audit("clean_dangling_thumbs", assets=len(ids))
    return {"ok": True, "cleaned": len(ids)}


@app.post("/api/checkup/retry_fingerprints")
def api_checkup_retry_fingerprints():
    """重试指纹失败/缺失项（仅在线卷），复用 index 队列。"""
    conn = db.connect()
    online = [v["volume_id"] for v in volumes.list_volumes()
              if volumes.resolve_mount(v["volume_id"]) is not None]
    ids: list[str] = []
    for ch in db.chunks(online):
        qs = ",".join("?" * len(ch))
        ids += [r[0] for r in conn.execute(
            "SELECT asset_id FROM assets WHERE (content_id IS NULL OR content_id='' "
            f"OR facts_json LIKE '%\"fingerprint_error\"%') AND volume_id IN ({qs})", ch).fetchall()]
    if not ids:
        return {"ok": True, "count": 0, "task_id": None}
    tid = tasks.create_task("index", "content_id", "指纹重试", ids)
    return {"ok": True, "count": len(ids), "task_id": tid}


@app.get("/api/danger/preview")
def api_danger_preview():
    """危险区操作的影响范围预览（数字给确认弹窗用）。"""
    conn = db.connect()
    return {"thumbs": _thumbs_stats(),
            "tags": tag_io.tags_stats(),
            "assets_total": conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
            "original_files_affected": 0}   # 永远为 0：原始素材只读，写明给前端展示


@app.post("/api/thumbs/clear")
def api_thumbs_clear(payload: dict = Body(default={})):
    """一键清空缩略图（4-2）。只删 out/thumbs/ 的派生图和库内引用，原始素材 0 接触。
    缩略图同时是 AI 打标的输入图：清空后未重新生图前，打标会走「无可用缩略图」失败路径（预期）。"""
    if payload.get("confirm") not in ("YEEHX", "CLEAR-THUMBS"):
        raise HTTPException(400, "需要 confirm=YEEHX")
    if tasks.snapshot()["active"]:
        raise HTTPException(409, "有任务正在运行，请先取消全部任务再清空缩略图")
    cfg = get_cfg()
    stats = _thumbs_stats()
    if cfg.thumbs_dir.exists():
        for p in cfg.thumbs_dir.glob("*"):
            if p.is_file():
                p.unlink(missing_ok=True)

    def _w(conn):
        conn.execute("UPDATE assets SET thumb_path='', thumb_lut='', updated_at=? "
                     "WHERE thumb_path IS NOT NULL AND thumb_path!=''", (db.now(),))
        conn.execute("UPDATE folders SET cover_thumb='', updated_at=? "
                     "WHERE cover_thumb IS NOT NULL AND cover_thumb!=''", (db.now(),))
    db.write(_w)
    _audit("thumbs_clear", files=stats["count"], bytes=stats["bytes"])
    return {"ok": True, "deleted": stats["count"], "bytes": stats["bytes"]}


@app.post("/api/thumbs/rebuild")
def api_thumbs_rebuild():
    """清空后的「立即为在线素材重新生图」：缺缩略图且所在盘在线的素材批量入 thumb 队列；
    离线盘素材等挂载后用文件夹「补缩略图」再补。"""
    conn = db.connect()
    online = [v["volume_id"] for v in volumes.list_volumes()
              if volumes.resolve_mount(v["volume_id"]) is not None]
    ids: list[str] = []
    for ch in db.chunks(online):
        qs = ",".join("?" * len(ch))
        ids += [r[0] for r in conn.execute(
            "SELECT asset_id FROM assets WHERE (thumb_path IS NULL OR thumb_path='') "
            f"AND volume_id IN ({qs})", ch).fetchall()]
    if not ids:
        return {"ok": True, "count": 0, "task_id": None}
    tid = tasks.create_task("thumb", "thumb_gen", "生图", ids)
    return {"ok": True, "count": len(ids), "task_id": tid}


@app.post("/api/tags/clear")
def api_tags_clear(payload: dict = Body(default={})):
    """一键清空标签库（4-3）。scope=all 恢复出厂 / scope=taggings 只清打标关系。
    两种范围都先强制 zip 备份（含参考图）到 out/backups，备份失败不执行。"""
    if payload.get("confirm") not in ("YEEHX", "CLEAR-TAGS"):
        raise HTTPException(400, "需要 confirm=YEEHX")
    if tasks.snapshot()["active"]:
        raise HTTPException(409, "有任务正在运行，请先取消全部任务再清空标签库")
    scope = (payload.get("scope") or "all").strip()
    keep_locked = bool(payload.get("keep_locked", True))
    try:
        res = tag_io.clear_tags(scope, keep_locked=keep_locked)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:
        raise HTTPException(500, f"自动备份失败，清空已取消：{exc}")
    _audit("tags_clear", scope=scope, keep_locked=keep_locked,
           **{k: v for k, v in res["cleared"].items()})
    return res


# ── Helpers ─────────────────────────────────────────────────────────────
def _resolve_tag_merge_suggestions(tags: list[dict], raw: list[dict], *,
                                   min_confidence: float = 0.7, limit: int = 80) -> list[dict]:
    lookup: dict[str, dict] = {}
    for tag in tags:
        lookup[TG.normalize_term(tag.get("name"))] = tag
        for alias in tag.get("aliases") or []:
            lookup.setdefault(TG.normalize_term(alias), tag)

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            confidence = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0
        if confidence < min_confidence:
            continue
        source = lookup.get(TG.normalize_term(item.get("source")))
        target = lookup.get(TG.normalize_term(item.get("target")))
        if not source or not target or source["id"] == target["id"]:
            continue
        if source.get("category") != target.get("category"):
            continue
        source_compact = _compact_tag_term(source["name"])
        target_compact = _compact_tag_term(target["name"])
        if source_compact != target_compact and (source_compact in target_compact or target_compact in source_compact):
            continue
        key = (source["id"], target["id"])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "source_id": source["id"],
            "target_id": target["id"],
            "source_name": source["name"],
            "target_name": target["name"],
            "category": source.get("category") or "未分类",
            "confidence": confidence,
            "reason": (item.get("reason") or "")[:160],
        })
    return out[:limit]


def _compact_tag_term(value: str | None) -> str:
    return "".join(ch for ch in TG.normalize_term(value) if ch.isalnum())


def _ensure_tag_ids(payload: dict) -> list[str]:
    ids = list(payload.get("tag_ids") or [])
    names = payload.get("names") or []
    if isinstance(names, str):
        names = [x.strip() for x in names.replace("，", ",").split(",")]
    for name in names:
        if name:
            ids.append(TG.resolve(name, payload.get("category") or "未分类", create=True))
    return list(dict.fromkeys([x for x in ids if x]))


def _task_current_label(item_key: str) -> str:
    if not item_key:
        return ""
    a = A.get(item_key)
    if a:
        return a["name"]
    f = folders.get(item_key)
    if f:
        return f["name"]
    return Path(item_key).name or item_key


def _fs_card(path: str, *, scan_counts: bool = True, volume_info: dict | None = None) -> dict:
    p = Path(path)
    info = volume_info or volumes.peek(path)
    child_dirs = direct_media = 0
    if scan_counts:
        try:
            for e in os.scandir(path):
                if osplat.should_skip_name(e.name):
                    continue
                try:
                    isdir = e.is_dir()
                except OSError:
                    continue
                if isdir:
                    if Path(e.name).suffix.lower() in fmod.PKG_EXT:
                        direct_media += 1
                    else:
                        child_dirs += 1
                elif fmod.is_media(Path(e.name)):
                    direct_media += 1
        except OSError:
            pass
    f = folders.get_by_path(info["volume_id"], info["rel_path"])
    hidden = folders.is_hidden_path(info["volume_id"], info["rel_path"])
    indexed, cover, status = 0, "", ""
    if f:
        indexed = db.connect().execute("SELECT COUNT(*) FROM assets WHERE folder_id=?", (f["folder_id"],)).fetchone()[0]
        # 封面只读持久化字段（生图任务负责写入并向祖先回填，1-3），不再现场扫子树
        cover = f.get("cover_thumb") or ""
        if cover and not (get_cfg().thumbs_dir / cover).exists():
            cover = ""   # 缩略图被清除后 cover_thumb 可能指向已删文件 → 显示占位
    # 盘根（rel_path 为空）用卷显示名：Windows 盘符根 p.name 是空串，卷标才是人话
    card_name = (info["name"] if not info["rel_path"] else p.name) or str(p)
    return {"name": card_name, "path": str(p), "child_dirs": child_dirs, "direct_media": direct_media,
            "imported": bool(f), "folder_id": f["folder_id"] if f else None, "indexed": indexed, "cover": cover,
            "status": status, "hidden": hidden}


def _fs_cards_batch(vid: str, entries: list[tuple[str, str, str]]) -> list[dict]:
    """浏览页子文件夹卡片批量版（1-3）：folders/隐藏链/计数 各一条查询，
    封面只读 cover_thumb。entries = [(名字, 绝对路径, child_rel)]，隐藏的直接剔除。"""
    if not entries:
        return []
    conn = db.connect()
    fol_map: dict[str, dict] = {}
    rels = [c for _, _, c in entries]
    for ch in db.chunks(rels):
        qs = ",".join("?" * len(ch))
        for r in conn.execute(f"SELECT * FROM folders WHERE volume_id=? AND rel_path IN ({qs})",
                              [vid] + ch).fetchall():
            fol_map[r["rel_path"]] = dict(r)
    hidden_rels = {r[0] for r in conn.execute(
        "SELECT rel_path FROM folders WHERE volume_id=? AND hidden=1", (vid,)).fetchall()}
    counts: dict[str, int] = {}
    fids = [f["folder_id"] for f in fol_map.values()]
    for ch in db.chunks(fids):
        qs = ",".join("?" * len(ch))
        for r in conn.execute(
                f"SELECT folder_id, COUNT(*) n FROM assets WHERE folder_id IN ({qs}) GROUP BY folder_id",
                ch).fetchall():
            counts[r["folder_id"]] = r["n"]

    def _chain(relp: str) -> set:
        parts = relp.split("/") if relp else []
        return {"/".join(parts[:i]) for i in range(len(parts) + 1)}

    thumbs_dir = get_cfg().thumbs_dir
    cards = []
    for name, path, child_rel in entries:
        if _chain(child_rel) & hidden_rels:
            continue   # 自己或祖先被隐藏 → 与旧行为一致，直接不出卡
        f = fol_map.get(child_rel)
        cover = (f or {}).get("cover_thumb") or ""
        if cover and not (thumbs_dir / cover).exists():
            cover = ""
        cards.append({"name": name, "path": path, "child_dirs": 0, "direct_media": 0,
                      "imported": bool(f), "folder_id": f["folder_id"] if f else None,
                      "indexed": counts.get((f or {}).get("folder_id"), 0), "cover": cover,
                      "status": "", "hidden": False})
    return cards


def _fs_crumbs(dir: str) -> list[dict]:
    parts = Path(dir).parts
    crumbs, acc = [{"name": "觅影素材库", "path": ""}], ""
    for i, part in enumerate(parts):
        acc = part if i == 0 else str(Path(acc) / part)
        if part == "/":
            continue
        crumbs.append({"name": part, "path": acc})
    return crumbs


def _index_current_layer(base: Path, vid: str, rel: str):
    """Register only media in the opened folder; no recursion, no thumbnail, no AI."""
    if folders.is_hidden_path(vid, rel):
        return
    media = []
    try:
        for e in base.iterdir():
            if osplat.should_skip_name(e.name):
                continue
            if e.is_dir() and e.suffix.lower() in fmod.PKG_EXT:
                media.append(e)
            elif e.is_file() and fmod.is_media(e):
                media.append(e)
    except OSError:
        return
    if not media:
        return
    # 指纹没变就不重复登记：否则每次进文件夹都做 N 次 upsert+recompute，是浏览卡顿的最大来源
    fp = fmod.folder_fingerprint(media)
    f = folders.get_by_path(vid, rel)
    if f and f.get("fingerprint") == fp:
        return
    mount = volumes.resolve_mount(vid)
    if mount is None:
        # 第一次见这块盘：登记卷身份（peek 不写库，所以这里补一次 identify）
        mount = Path(volumes.identify(str(base))["mount"])
    scanner.ensure_current_folder(vid, rel)
    fid = folders.ensure(vid, rel)
    descs = fmod.classify(base, media, get_cfg())
    # 1-2：浏览即登记不再同步算指纹（机械盘上 500 个新文件 = 在这个 GET 里读 8GB），
    # 登记完丢进 content_id 队列后台补算，搬家识别/去重延迟到补算完成时
    aids = [scanner.register_media_asset(vid, fid, mount, d["primary"], d["kind"], d.get("extra", {}),
                                         compute_cid=False)
            for d in descs]
    inheritance.recompute_many(aids)
    folders.set_meta(fid, fingerprint=fp, asset_count=len(descs))
    need_cid = [a["asset_id"] for a in A.get_many(aids) if not a.get("content_id")]
    if need_cid:
        tasks.create_task("index", "content_id", "指纹", need_cid,
                          scope={"volume_id": vid, "rel_path": rel})


def _candidate_samples(c: dict, limit: int = 4) -> list[str]:
    thumbs = []
    for aid in c["hits"].get("assets", []):
        if len(thumbs) >= limit:
            break
        a = A.get(aid)
        if a and a.get("thumb_path"):
            thumbs.append(a["thumb_path"])
    for fid in c["hits"].get("folders", []):
        if len(thumbs) >= limit:
            break
        rows = db.connect().execute("SELECT thumb_path FROM assets WHERE folder_id=? AND thumb_path!='' LIMIT ?",
                                    (fid, limit)).fetchall()
        for x in rows:
            if x[0]:
                thumbs.append(x[0])
                if len(thumbs) >= limit:
                    break
    return thumbs[:limit]


# _folder_cover（现场扫子树取封面）已删除：封面统一走 folders.cover_thumb 持久化字段，
# 由生图任务写入并向祖先回填（scanner._h_thumb_gen），启动时做一次存量回填（_backfill_covers）。


def _asset_ids_under_folder(f: dict, *, unlocked_only: bool = False, require_thumb: bool = False,
                            kinds: set[str] | None = None) -> list[str]:
    rel = (f.get("rel_path") or "").strip("/")
    if rel:
        sql = ("SELECT a.asset_id FROM assets a JOIN folders fo ON fo.folder_id=a.folder_id "
               "WHERE fo.volume_id=? AND (fo.rel_path=? OR fo.rel_path LIKE ?)")
        args: list = [f["volume_id"], rel, rel + "/%"]
    else:
        sql = ("SELECT a.asset_id FROM assets a JOIN folders fo ON fo.folder_id=a.folder_id "
               "WHERE fo.volume_id=?")
        args = [f["volume_id"]]
    if unlocked_only:
        sql += " AND a.locked=0"
    if require_thumb:
        sql += " AND a.thumb_path IS NOT NULL AND a.thumb_path!=''"
    if kinds:
        ks = sorted(kinds)
        sql += f" AND a.kind IN ({','.join('?' * len(ks))})"
        args += ks
    rows = db.connect().execute(sql + " ORDER BY a.name", args).fetchall()
    return [r[0] for r in rows]


def _cards(assets_list: list[dict]) -> list[dict]:
    """整层批量水合（同搜索页 1-1 的教训）：旧版逐条 effective_named，
    每个素材都全量加载标签表 + 卷内全部文件夹 + 整张 folder_tags，
    打标越多进文件夹越卡；现在整层只载入一次。"""
    if not assets_list:
        return []
    tmap = TG.label_map()
    eff_all = inheritance.effective_many(assets_list)
    out = []
    for a in assets_list:
        eff = inheritance.named_from_effective(eff_all[a["asset_id"]], tmap)
        out.append({"asset_id": a["asset_id"], "name": a["name"], "kind": a["kind"],
                    "thumb": a.get("thumb_path") or "", "thumb_v": _thumb_version(a),
                    "status": a["status"], "score": a["score"], "locked": bool(a.get("locked")),
                    "tags": eff["tags"], "color": eff["color"], "lut": eff["lut"]})
    return out


def _thumb_version(a: dict) -> int:
    try:
        return int(float(a.get("updated_at") or 0) * 1000)
    except (TypeError, ValueError):
        return 0


def _resolve_folder(f: dict) -> dict:
    tmap = TG.label_map()
    own = [{"id": tid, "name": (tmap.get(tid) or {}).get("name", tid),
            "category": (tmap.get(tid) or {}).get("category", "未分类"),
            "color": (tmap.get(tid) or {}).get("color", "#8b8b96")}
           for tid in folders.tag_ids(f["folder_id"])]
    common = _folder_common_tags(f, tmap)
    return {"tags": common, "common_tags": common, "own_tags": own,
            "lut": f.get("lut"), "note": f.get("note"), "description_ai": f.get("desc_ai")}


def _folder_common_tags(f: dict, tmap: dict | None = None) -> list[dict]:
    rel = (f.get("rel_path") or "").strip("/")
    if rel:
        args = [f["volume_id"], rel, rel + "/%"]
        scope_sql = "fo.volume_id=? AND (fo.rel_path=? OR fo.rel_path LIKE ?)"
    else:
        args = [f["volume_id"]]
        scope_sql = "fo.volume_id=?"
    # CROSS JOIN 固定连接顺序：范围文件夹 → 素材 → asset_effective（都走索引）。
    # 旧写法 SQLite 从整张 asset_effective(field='tag') 入手，库越大越慢，
    # 每次进文件夹固定白付 200ms+，与目标文件夹大小无关。
    total = db.connect().execute(
        f"SELECT COUNT(*) FROM folders fo CROSS JOIN assets a ON a.folder_id=fo.folder_id WHERE {scope_sql}",
        args,
    ).fetchone()[0]
    if not total:
        return []
    rows = db.connect().execute(
        f"SELECT ae.value tag_id, COUNT(DISTINCT ae.asset_id) n "
        f"FROM folders fo "
        f"CROSS JOIN assets a ON a.folder_id=fo.folder_id "
        f"CROSS JOIN asset_effective ae ON ae.asset_id=a.asset_id AND ae.field='tag' "
        f"WHERE {scope_sql} "
        f"GROUP BY ae.value HAVING n=?",
        args + [total],
    ).fetchall()
    tmap = tmap or TG.label_map()
    out = []
    for r in rows:
        t = tmap.get(r["tag_id"])
        if t:
            out.append({"id": t["id"], "name": t["name"], "category": t["category"], "color": t["color"]})
    out.sort(key=lambda x: (x["category"], x["name"]))
    return out


def _asset_detail(a: dict) -> dict:
    eff = inheritance.effective_named(a)
    f = folders.get(a["folder_id"]) if a.get("folder_id") else None
    vol = volumes.get(a["volume_id"]) or {}
    src = volumes.abspath(a)
    return {**{k: a[k] for k in ("asset_id", "name", "kind", "size", "status", "score", "rel_path")},
            "thumb": a.get("thumb_path") or "", "thumb_v": _thumb_version(a),
            "auto_lut": (a.get("facts") or {}).get("auto_lut") or "",
            "locked": bool(a.get("locked")),
            "desc_ai": a.get("desc_ai") or "", "note": a.get("note") or "", "facts": a.get("facts") or {},
            "volume": vol.get("display_name") or vol.get("name") or "",
            "folder": f and {"folder_id": f["folder_id"], "name": f["name"]},
            "abspath": str(src) if src else None, "effective": eff}


def _clear_tagging(asset_id: str, with_thumbs: bool):
    """清掉一个素材的打标数据：全部标签（含手动）+排除记录+AI描述，状态回 pending；
    with_thumbs 再连缩略图一起删。锁定返回 False（跳过），不存在返回 None，成功 True。"""
    a = A.get(asset_id)
    if not a:
        return None
    if a.get("locked"):
        return False
    facts = dict(a.get("facts") or {})
    if with_thumbs:
        # 只有连缩略图一起清时才丢 代表帧时间点/主色/时长——否则缩略图还在，
        # 之后换 LUT 重生成会跳帧、主色筛选失效
        for k in ("color", "rep_ts", "duration"):
            facts.pop(k, None)

    def _w(conn):
        t = db.now()
        conn.execute("DELETE FROM asset_tags WHERE asset_id=?", (asset_id,))
        conn.execute("DELETE FROM asset_tag_excludes WHERE asset_id=?", (asset_id,))
        cols = "desc_ai=NULL, desc_locked=0, status='pending', facts_json=?, updated_at=?"
        args: list = [db.jdumps(facts), t]
        if with_thumbs:
            cols += ", thumb_path='', thumb_lut=''"
        conn.execute(f"UPDATE assets SET {cols} WHERE asset_id=?", args + [asset_id])
    db.write(_w)

    if with_thumbs and a.get("thumb_path"):
        p = get_cfg().thumbs_dir / a["thumb_path"]
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
        if a.get("folder_id"):
            f = folders.get(a["folder_id"])
            if f and f.get("cover_thumb") == a["thumb_path"]:
                folders.set_meta(a["folder_id"], cover_thumb="")
    return True


def _delete_asset_record(asset_id: str):
    a = A.get(asset_id)
    if not a:
        return
    thumb = a.get("thumb_path")
    if thumb:
        p = get_cfg().thumbs_dir / thumb
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
        if a.get("folder_id"):
            f = folders.get(a["folder_id"])
            if f and f.get("cover_thumb") == thumb:
                folders.set_meta(a["folder_id"], cover_thumb="")
    A.delete(asset_id)


def _tag_with_refs(tag: dict | None) -> dict | None:
    if not tag:
        return None
    out = dict(tag)
    refs = []
    for ref in out.get("ref_images") or []:
        if ref.get("type") == "asset":
            a = A.get(ref.get("id"))
            if a and a.get("thumb_path"):
                refs.append({**ref, "url": f"/thumbs/{a['thumb_path']}", "name": a["name"]})
        elif ref.get("type") == "file":
            refs.append({**ref, "url": f"/refimg/{ref.get('path')}"})
    out["refs"] = refs
    out["has_card"] = bool(out.get("aliases") or out.get("note") or refs or out.get("pinned"))
    return out


def _group_tags(tags: list[dict]) -> dict:
    out = {c["name"]: [] for c in TG.list_categories()}
    for t in tags:
        out.setdefault(t["category"], []).append(t)
    return out


_cfg = get_cfg()
_cfg.thumbs_dir.mkdir(parents=True, exist_ok=True)
_cfg.refimg_dir.mkdir(parents=True, exist_ok=True)
_CAND_DIR = _cfg.out_dir / "cand"   # 换帧候选图（临时；选定/取消即清，与正式缩略图隔离）
shutil.rmtree(_CAND_DIR, ignore_errors=True)   # 启动先清，扫掉上次崩溃可能残留的候选
_CAND_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/thumbs", StaticFiles(directory=str(_cfg.thumbs_dir)), name="thumbs")
app.mount("/refimg", StaticFiles(directory=str(_cfg.refimg_dir)), name="refimg")
app.mount("/cand", StaticFiles(directory=str(_CAND_DIR)), name="cand")
app.mount("/web", StaticFiles(directory=str(WEB)), name="web")
