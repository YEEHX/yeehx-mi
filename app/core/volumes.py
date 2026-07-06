"""卷（硬盘）身份与路径解析。

库认 卷身份 ＋ 盘内相对路径，不认 /Volumes/名字/… 或 E:\\… 绝对路径。
盘改名/换 USB 口/挂载点变/盘符漂移 → 身份没变就是同一块盘，只更新显示名，不重扫、不丢库。
身份优先用系统卷标识（mac: diskutil 卷UUID；Windows: 卷GUID），读不到用 容量＋名字 指纹兜底。
asset_id 由 卷+相对路径 派生（见 core/ids），改名不变。
"""
from __future__ import annotations
import hashlib
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

from app import db
from app.core import osplat


def find_mount_point(path: Path) -> Path:
    """向上走到挂载点（跨平台）。"""
    p = Path(path).resolve()
    if p.is_file():
        p = p.parent
    cur = p
    while True:
        try:
            if os.path.ismount(cur):
                return cur
        except OSError:
            pass
        if cur.parent == cur:
            return cur
        cur = cur.parent


_UUID_CACHE: dict[str, str] = {}   # 挂载点 → 卷UUID（diskutil 每次最长 8 秒，根目录页每个卷都查一遍，必须缓存）


def _mac_volume_uuid(mount: Path) -> str | None:
    key = str(mount)
    hit = _UUID_CACHE.get(key)
    if hit is not None:
        return hit
    if platform.system() != "Darwin" or not shutil.which("diskutil"):
        return None
    try:
        out = subprocess.run(["diskutil", "info", "-plist", str(mount)],
                             capture_output=True, timeout=8)
        if out.returncode != 0:
            return None
        import plistlib
        data = plistlib.loads(out.stdout)
        uuid = data.get("VolumeUUID") or data.get("DiskUUID")
        if uuid:   # 只缓存成功结果；失败下次重试，避免把临时故障固化成错误身份
            _UUID_CACHE[key] = uuid
        return uuid
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _volume_uuid(mount: Path) -> str | None:
    """系统级卷身份：mac 卷UUID / Windows 卷GUID。两者都读不到返回 None（走指纹兜底）。"""
    if osplat.IS_WIN:
        return osplat.win_volume_guid(mount)
    return _mac_volume_uuid(mount)


def _mount_display_name(mount: Path) -> str:
    """卷显示名：mac 用挂载点名（/Volumes/拍摄素材2023 → 拍摄素材2023）；
    Windows 盘符根 name 是空串，用卷标（没有卷标 → "磁盘 E:"）。"""
    if osplat.IS_WIN:
        return osplat.win_volume_label(mount)
    return mount.name or str(mount)


def _disk_total(mount: Path) -> int:
    try:
        return shutil.disk_usage(str(mount)).total
    except OSError:
        return 0


def identify(abs_path: str | Path) -> dict:
    """识别（必要时登记）一条路径所属的卷，返回 {volume_id, mount, rel_path, name}。"""
    abs_path = Path(abs_path).resolve()
    mount = find_mount_point(abs_path)
    name = _mount_display_name(mount)
    uuid = _volume_uuid(mount)
    total = _disk_total(mount)
    if uuid:
        vid = "v" + hashlib.sha1(("uuid:" + uuid).encode()).hexdigest()[:14]
        fingerprint = "uuid:" + uuid
    else:
        fingerprint = f"fp:{name}:{total}"
        vid = "v" + hashlib.sha1(fingerprint.encode()).hexdigest()[:14]
    rel = os.path.relpath(str(abs_path), str(mount))
    if rel == ".":
        rel = ""
    register(vid, name=name, size=total, uuid=uuid, sample_fingerprint=fingerprint, mount=str(mount))
    return {"volume_id": vid, "mount": str(mount), "rel_path": rel.replace(os.sep, "/"), "name": name}


def peek(abs_path: str | Path) -> dict:
    """只读解析一条路径的卷身份（不写库）。用于免扫描浏览。"""
    abs_path = Path(abs_path).resolve()
    mount = find_mount_point(abs_path)
    name = _mount_display_name(mount)
    uuid = _volume_uuid(mount)
    if uuid:
        vid = "v" + hashlib.sha1(("uuid:" + uuid).encode()).hexdigest()[:14]
    else:
        vid = "v" + hashlib.sha1(f"fp:{name}:{_disk_total(mount)}".encode()).hexdigest()[:14]
    rel = os.path.relpath(str(abs_path), str(mount))
    if rel == ".":
        rel = ""
    return {"volume_id": vid, "mount": str(mount), "rel_path": rel.replace(os.sep, "/"), "name": name}


def register(volume_id: str, *, name: str, size: int = 0, uuid: str | None = None,
             sample_fingerprint: str = "", mount: str = ""):
    def _w(conn):
        t = db.now()
        row = conn.execute("SELECT volume_id, display_name FROM volumes WHERE volume_id=?", (volume_id,)).fetchone()
        if row:
            conn.execute("UPDATE volumes SET name=?, size=?, uuid=?, sample_fingerprint=?, last_mount=?, online=1, updated_at=? WHERE volume_id=?",
                         (name, size, uuid, sample_fingerprint, mount, t, volume_id))
        else:
            conn.execute("INSERT INTO volumes(volume_id,name,display_name,uuid,size,sample_fingerprint,last_mount,online,created_at,updated_at)"
                         " VALUES (?,?,?,?,?,?,?,1,?,?)",
                         (volume_id, name, name, uuid, size, sample_fingerprint, mount, t, t))
    db.write(_w)


def get(volume_id: str) -> dict | None:
    r = db.connect().execute("SELECT * FROM volumes WHERE volume_id=?", (volume_id,)).fetchone()
    return dict(r) if r else None


def list_volumes() -> list[dict]:
    rows = db.connect().execute("SELECT * FROM volumes ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["online"] = bool(d.get("last_mount") and Path(d["last_mount"]).exists())
        if not d["online"] and _rediscover(d["volume_id"]) is not None:
            d = dict(get(d["volume_id"]) or d)   # 找回了（如 Windows 盘符漂移）：取更新后的行
            d["online"] = True
        out.append(d)
    return out


_REDISCOVER_FAIL: dict[str, float] = {}   # vid → 上次重发现失败时间（10 秒内不重试，防导出循环里反复枚举）


def _rediscover(volume_id: str) -> Path | None:
    """记录的挂载点没了 → 按卷身份在当前在线卷里找它（Windows 盘符漂移 E:→F:、
    mac 盘改名后挂载点变，都靠这一步找回；找到即更新 last_mount，绝不重扫）。"""
    now = time.time()
    if now - _REDISCOVER_FAIL.get(volume_id, 0) < 10:
        return None
    for mount in osplat.candidate_mounts():
        try:
            info = peek(mount)
        except OSError:
            continue
        if info["volume_id"] == volume_id:
            old = get(volume_id) or {}
            register(volume_id, name=info["name"], size=_disk_total(mount),
                     uuid=_volume_uuid(mount),
                     sample_fingerprint=old.get("sample_fingerprint") or "",
                     mount=str(mount))
            return mount
    _REDISCOVER_FAIL[volume_id] = now
    return None


def resolve_mount(volume_id: str) -> Path | None:
    """卷当前挂载点（在线才返回）。盘未挂载 → None（视为离线，绝不判删除）。"""
    v = get(volume_id)
    if not v:
        return None
    if v.get("last_mount"):
        p = Path(v["last_mount"])
        if p.exists():
            return p
    return _rediscover(volume_id)


def abspath(asset: dict) -> Path | None:
    mount = resolve_mount(asset["volume_id"])
    if mount is None:
        return None
    return mount / asset["rel_path"]
