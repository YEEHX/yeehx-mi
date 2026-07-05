"""卷（硬盘）身份与路径解析。

库认 卷身份 ＋ 盘内相对路径，不认 /Volumes/名字/… 绝对路径。
盘改名/换 USB 口/挂载点变 → 身份没变就是同一块盘，只更新显示名，不重扫、不丢库。
身份优先用系统卷 UUID（mac），读不到用 容量＋名字 指纹兜底。
asset_id 由 卷+相对路径 派生（见 core/ids），改名不变。
"""
from __future__ import annotations
import hashlib
import os
import platform
import shutil
import subprocess
from pathlib import Path

from app import db


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


def _disk_total(mount: Path) -> int:
    try:
        return shutil.disk_usage(str(mount)).total
    except OSError:
        return 0


def identify(abs_path: str | Path) -> dict:
    """识别（必要时登记）一条路径所属的卷，返回 {volume_id, mount, rel_path, name}。"""
    abs_path = Path(abs_path).resolve()
    mount = find_mount_point(abs_path)
    name = mount.name or str(mount)
    uuid = _mac_volume_uuid(mount)
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
    name = mount.name or str(mount)
    uuid = _mac_volume_uuid(mount)
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
        out.append(d)
    return out


def resolve_mount(volume_id: str) -> Path | None:
    """卷当前挂载点（在线才返回）。盘未挂载 → None（视为离线，绝不判删除）。"""
    v = get(volume_id)
    if not v or not v.get("last_mount"):
        return None
    p = Path(v["last_mount"])
    return p if p.exists() else None


def abspath(asset: dict) -> Path | None:
    mount = resolve_mount(asset["volume_id"])
    if mount is None:
        return None
    return mount / asset["rel_path"]
