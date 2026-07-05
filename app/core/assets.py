"""Assets for the lightweight tag model."""
from __future__ import annotations

from app import db
from app.core.ids import asset_id as mk_aid


def upsert(volume_id: str, folder_id: str, rel_path: str, name: str, kind: str,
           size: int, mtime: float, content_id: str = "", facts: dict | None = None) -> str:
    aid = mk_aid(volume_id, rel_path)

    def _w(conn):
        row = conn.execute("SELECT facts_json FROM assets WHERE asset_id=?", (aid,)).fetchone()
        t = db.now()
        if row:
            nf = db.jloads(row["facts_json"], {}) or {}
            if facts:
                nf.update(facts)
            # content_id 传空串表示"这次没算"，保留旧值；算了才覆盖
            conn.execute(
                "UPDATE assets SET folder_id=?,name=?,kind=?,size=?,mtime=?,"
                "content_id=COALESCE(NULLIF(?,''),content_id),facts_json=?,updated_at=? "
                "WHERE asset_id=?",
                (folder_id, name, kind, size, mtime, content_id or "", db.jdumps(nf), t, aid),
            )
        else:
            conn.execute(
                "INSERT INTO assets(asset_id,volume_id,folder_id,rel_path,name,kind,size,mtime,content_id,"
                "facts_json,status,score,desc_locked,locked,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,?)",
                (aid, volume_id, folder_id, rel_path, name, kind, size, mtime, content_id or "",
                 db.jdumps(facts or {}), "pending", t, t),
            )
    db.write(_w)
    return aid


def get(asset_id: str) -> dict | None:
    r = db.connect().execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    return _row(r) if r else None


def get_by_path(volume_id: str, rel_path: str) -> dict | None:
    r = db.connect().execute(
        "SELECT * FROM assets WHERE volume_id=? AND rel_path=?", (volume_id, rel_path)
    ).fetchone()
    return _row(r) if r else None


def find_by_content(content_id: str, volume_id: str | None = None) -> list[dict]:
    """按内容指纹找素材（重复检测/搬家识别）。"""
    if not content_id:
        return []
    q = "SELECT * FROM assets WHERE content_id=?"
    args: list = [content_id]
    if volume_id:
        q += " AND volume_id=?"
        args.append(volume_id)
    return [_row(r) for r in db.connect().execute(q, args).fetchall()]


def rehome(old_id: str, *, volume_id: str, folder_id: str, rel_path: str, name: str) -> str:
    """改名/移动后的素材迁到新路径：asset_id 由 卷+相对路径 派生，所以连主键一起迁，
    标签/排除/生效索引/缩略图全部保留。返回新 asset_id。"""
    new_id_ = mk_aid(volume_id, rel_path)
    if new_id_ == old_id:
        return old_id
    old = get(old_id)
    if not old or get(new_id_):
        return old_id

    def _w(conn):
        t = db.now()
        conn.execute(
            "UPDATE assets SET asset_id=?, volume_id=?, folder_id=?, rel_path=?, name=?, updated_at=? "
            "WHERE asset_id=?",
            (new_id_, volume_id, folder_id, rel_path, name, t, old_id),
        )
        for table in ("asset_tags", "asset_tag_excludes", "asset_effective"):
            conn.execute(f"UPDATE OR IGNORE {table} SET asset_id=? WHERE asset_id=?", (new_id_, old_id))
            conn.execute(f"DELETE FROM {table} WHERE asset_id=?", (old_id,))
        db.fts_delete(conn, old_id)   # FTS 由调用方 recompute 重建

    db.write(_w)

    if old.get("thumb_path"):
        from app.config import get_cfg
        cfg = get_cfg()
        src = cfg.thumbs_dir / old["thumb_path"]
        new_name = f"{new_id_}.jpg"
        try:
            if src.exists():
                src.rename(cfg.thumbs_dir / new_name)
                set_fields(new_id_, thumb_path=new_name)
            else:
                # 旧缩略图文件已丢失：清掉引用，避免悬空路径（空白卡）
                set_fields(new_id_, thumb_path="", thumb_lut="")
        except OSError:
            pass
    return new_id_


def get_many(asset_ids: list[str]) -> list[dict]:
    if not asset_ids:
        return []
    conn = db.connect()
    by = {}
    for ch in db.chunks(asset_ids):
        qs = ",".join("?" * len(ch))
        for r in conn.execute(f"SELECT * FROM assets WHERE asset_id IN ({qs})", ch).fetchall():
            by[r["asset_id"]] = _row(r)
    return [by[a] for a in asset_ids if a in by]


def in_folder(folder_id: str) -> list[dict]:
    rows = db.connect().execute("SELECT * FROM assets WHERE folder_id=? ORDER BY name", (folder_id,)).fetchall()
    return [_row(r) for r in rows]


def add_own(asset_id: str, tag_id: str, source: str = "manual"):
    def _w(conn):
        t = db.now()
        conn.execute(
            "INSERT OR IGNORE INTO asset_tags(asset_id,tag_id,source,created_at) VALUES (?,?,?,?)",
            (asset_id, tag_id, source, t),
        )
        conn.execute("DELETE FROM asset_tag_excludes WHERE asset_id=? AND tag_id=?", (asset_id, tag_id))
    db.write(_w)


def remove_own(asset_id: str, tag_id: str):
    db.write(lambda conn: conn.execute("DELETE FROM asset_tags WHERE asset_id=? AND tag_id=?", (asset_id, tag_id)))


def exclude(asset_id: str, tag_id: str):
    def _w(conn):
        t = db.now()
        conn.execute("DELETE FROM asset_tags WHERE asset_id=? AND tag_id=?", (asset_id, tag_id))
        conn.execute(
            "INSERT OR IGNORE INTO asset_tag_excludes(asset_id,tag_id,created_at) VALUES (?,?,?)",
            (asset_id, tag_id, t),
        )
    db.write(_w)


def set_lut(asset_id: str, lut: str | None):
    db.write(lambda conn: conn.execute("UPDATE assets SET lut=?, updated_at=? WHERE asset_id=?", (lut, db.now(), asset_id)))


def set_facts(asset_id: str, patch: dict):
    a = get(asset_id)
    if not a:
        return
    facts = dict(a["facts"]); facts.update(patch)
    db.write(lambda conn: conn.execute(
        "UPDATE assets SET facts_json=?, updated_at=? WHERE asset_id=?",
        (db.jdumps(facts), db.now(), asset_id),
    ))


def set_fields(asset_id: str, **fields):
    allowed = ("kind", "thumb_path", "thumb_lut", "status", "score", "desc_ai", "note",
               "desc_locked", "locked", "lut", "content_id")
    cols, args = [], []
    for k in allowed:
        if k in fields:
            cols.append(f"{k}=?"); args.append(fields[k])
    if not cols:
        return
    cols.append("updated_at=?"); args.append(db.now()); args.append(asset_id)
    db.write(lambda conn: conn.execute(f"UPDATE assets SET {', '.join(cols)} WHERE asset_id=?", args))


def set_description(asset_id: str, *, desc_ai: str | None = None, note: str | None = None, manual: bool = False):
    a = get(asset_id)
    if not a:
        return
    fields = {}
    if desc_ai is not None:
        if manual:
            fields["desc_ai"] = desc_ai; fields["desc_locked"] = 1
        elif not a.get("desc_locked"):
            fields["desc_ai"] = desc_ai
    if note is not None:
        fields["note"] = note
    if fields:
        set_fields(asset_id, **fields)


def delete(asset_id: str):
    def _w(conn):
        conn.execute("DELETE FROM asset_tags WHERE asset_id=?", (asset_id,))
        conn.execute("DELETE FROM asset_tag_excludes WHERE asset_id=?", (asset_id,))
        conn.execute("DELETE FROM asset_effective WHERE asset_id=?", (asset_id,))
        conn.execute("DELETE FROM assets WHERE asset_id=?", (asset_id,))
        db.fts_delete(conn, asset_id)
    db.write(_w)


def merge_facts(asset_id: str, updates: dict):
    """合并 facts 键值；值为 None 表示删除该键（给 fingerprint_error 这类状态位用）。"""
    def _w(conn):
        row = conn.execute("SELECT facts_json FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
        if not row:
            return
        facts = db.jloads(row["facts_json"], {}) or {}
        for k, v in updates.items():
            if v is None:
                facts.pop(k, None)
            else:
                facts[k] = v
        conn.execute("UPDATE assets SET facts_json=?, updated_at=? WHERE asset_id=?",
                     (db.jdumps(facts), db.now(), asset_id))
    db.write(_w)


def own_tag_ids(asset_id: str) -> set[str]:
    return {r[0] for r in db.connect().execute("SELECT tag_id FROM asset_tags WHERE asset_id=?", (asset_id,)).fetchall()}


def excluded_tag_ids(asset_id: str) -> set[str]:
    return {r[0] for r in db.connect().execute("SELECT tag_id FROM asset_tag_excludes WHERE asset_id=?", (asset_id,)).fetchall()}


def _row(r) -> dict:
    # 注意：不要在这里附带 own_tags/excludes（旧版每次 get() 多查两次，全局 N+1；
    # 需要时调用 own_tag_ids()/excluded_tag_ids()）。
    d = dict(r)
    d["facts"] = db.jloads(d.pop("facts_json", "{}"), {}) or {}
    return d
