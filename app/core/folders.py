"""Folders with lightweight inherited tags."""
from __future__ import annotations

from app import db
from app.core.ids import folder_id as mk_fid


def ensure(volume_id: str, rel_path: str, name: str | None = None) -> str:
    rel_path = rel_path.strip("/")
    fid = mk_fid(volume_id, rel_path)
    if db.connect().execute("SELECT 1 FROM folders WHERE folder_id=?", (fid,)).fetchone():
        return fid
    parent_rel = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    nm = name or (rel_path.rsplit("/", 1)[-1] if rel_path else "(根)")

    def _w(conn):
        t = db.now()
        conn.execute(
            "INSERT OR IGNORE INTO folders(folder_id,volume_id,rel_path,name,parent_rel,fingerprint,scan_state,"
            "asset_count,child_count,hidden,created_at,updated_at) VALUES (?,?,?,?,?,?,?,0,0,0,?,?)",
            (fid, volume_id, rel_path, nm, parent_rel, "", "none", t, t),
        )
    db.write(_w)
    return fid


def get(folder_id: str) -> dict | None:
    r = db.connect().execute("SELECT * FROM folders WHERE folder_id=?", (folder_id,)).fetchone()
    return _row(r) if r else None


def get_by_path(volume_id: str, rel_path: str) -> dict | None:
    r = db.connect().execute(
        "SELECT * FROM folders WHERE volume_id=? AND rel_path=?", (volume_id, rel_path.strip("/"))
    ).fetchone()
    return _row(r) if r else None


def is_hidden_path(volume_id: str, rel_path: str) -> bool:
    """True when this folder or any ancestor has been hidden in the library."""
    rel_path = rel_path.strip("/")
    parts = rel_path.split("/") if rel_path else []
    paths = ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]
    if not paths:
        paths = [""]
    qs = ",".join("?" * len(paths))
    row = db.connect().execute(
        f"SELECT 1 FROM folders WHERE volume_id=? AND rel_path IN ({qs}) AND hidden=1 LIMIT 1",
        [volume_id] + paths,
    ).fetchone()
    return bool(row)


def ancestors(volume_id: str, rel_path: str) -> list[dict]:
    rel_path = rel_path.strip("/")
    parts = rel_path.split("/") if rel_path else []
    paths = ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]
    out = []
    conn = db.connect()
    for p in paths:
        r = conn.execute("SELECT * FROM folders WHERE volume_id=? AND rel_path=?", (volume_id, p)).fetchone()
        if r:
            out.append(_row(r))
    return out


def add_tags(folder_id: str, ids: list[str], source: str = "manual"):
    def _w(conn):
        t = db.now()
        conn.executemany(
            "INSERT OR IGNORE INTO folder_tags(folder_id,tag_id,source,created_at) VALUES (?,?,?,?)",
            [(folder_id, i, source, t) for i in ids if i],
        )
    db.write(_w)


def remove_tags(folder_id: str, ids: list[str]):
    if not ids:
        return
    qs = ",".join("?" * len(ids))
    db.write(lambda conn: conn.execute(f"DELETE FROM folder_tags WHERE folder_id=? AND tag_id IN ({qs})", [folder_id] + ids))


def set_lut(folder_id: str, lut: str | None):
    db.write(lambda conn: conn.execute("UPDATE folders SET lut=?, updated_at=? WHERE folder_id=?", (lut, db.now(), folder_id)))


def set_meta(folder_id: str, **fields):
    allowed = ("fingerprint", "scan_state", "cover_thumb", "asset_count", "child_count", "hidden", "name")
    cols, args = [], []
    for k in allowed:
        if k in fields:
            cols.append(f"{k}=?"); args.append(fields[k])
    if not cols:
        return
    cols.append("updated_at=?"); args.append(db.now()); args.append(folder_id)
    db.write(lambda conn: conn.execute(f"UPDATE folders SET {', '.join(cols)} WHERE folder_id=?", args))


def descendants_ids(volume_id: str, rel_path: str) -> list[str]:
    rel = rel_path.strip("/")
    conn = db.connect()
    if rel:
        rows = conn.execute(
            "SELECT folder_id FROM folders WHERE volume_id=? AND (rel_path=? OR rel_path LIKE ?)",
            (volume_id, rel, rel + "/%"),
        ).fetchall()
    else:
        rows = conn.execute("SELECT folder_id FROM folders WHERE volume_id=?", (volume_id,)).fetchall()
    return [r[0] for r in rows]


def tag_ids(folder_id: str) -> list[str]:
    return [r[0] for r in db.connect().execute(
        "SELECT tag_id FROM folder_tags WHERE folder_id=? ORDER BY created_at", (folder_id,)
    ).fetchall()]


def _row(r) -> dict:
    d = dict(r)
    tids = tag_ids(d["folder_id"])
    d["tags"] = {"tags": tids, "note": d.get("note"), "description_ai": d.get("desc_ai"), "lut": d.get("lut")}
    return d
