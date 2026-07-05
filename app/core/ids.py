"""稳定 ID 派生。asset_id / folder_id 由 卷+相对路径 派生，改名/换盘不变。"""
from __future__ import annotations
import hashlib
import uuid


def _h(*parts: str) -> str:
    raw = "\x00".join(p or "" for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def asset_id(volume_id: str, rel_path: str) -> str:
    return "a" + _h(volume_id, rel_path)[:16]


def folder_id(volume_id: str, rel_path: str) -> str:
    return "f" + _h(volume_id, rel_path)[:16]


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"
