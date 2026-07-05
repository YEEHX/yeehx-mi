"""AI-assisted tag merge suggestions.

Suggestions are deliberately review-only. Accepting one delegates to
``tags.merge`` so the same migration path is used as manual merges.
"""
from __future__ import annotations

from app import db
from app.core.ids import new_id


def list_(status: str = "pending") -> list[dict]:
    q = "SELECT * FROM tag_merge_suggestions"
    args: list = []
    if status:
        q += " WHERE status=?"
        args.append(status)
        if status == "pending":
            q += (
                " AND EXISTS(SELECT 1 FROM tags s WHERE s.id=source_id)"
                " AND EXISTS(SELECT 1 FROM tags t WHERE t.id=target_id)"
            )
    q += " ORDER BY confidence DESC, created_at DESC"
    return [_row(r) for r in db.connect().execute(q, args).fetchall()]


def _insert_pending(conn, items: list[dict], model: str | None, now: float) -> int:
    inserted = 0
    seen: set[tuple[str, str]] = set()
    for item in items:
        source_id = (item.get("source_id") or "").strip()
        target_id = (item.get("target_id") or "").strip()
        if not source_id or not target_id or source_id == target_id:
            continue
        key = (source_id, target_id)
        if key in seen:
            continue
        seen.add(key)
        conn.execute(
            "INSERT OR IGNORE INTO tag_merge_suggestions("
            "id,source_id,target_id,source_name,target_name,category,confidence,reason,model,status,created_at,updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
            (
                new_id("tm"),
                source_id,
                target_id,
                item.get("source_name") or "",
                item.get("target_name") or "",
                item.get("category") or "",
                float(item.get("confidence") or 0),
                item.get("reason") or "",
                model or item.get("model") or "",
                now,
                now,
            ),
        )
        inserted += 1
    return inserted


def replace_pending(items: list[dict], *, model: str | None = None) -> dict:
    """Replace current pending suggestions with a freshly generated set."""
    now = db.now()

    def _w(conn):
        conn.execute("DELETE FROM tag_merge_suggestions WHERE status='pending'")
        return _insert_pending(conn, items, model, now)

    return {"ok": True, "count": db.write(_w)}


def append_pending(items: list[dict], *, model: str | None = None) -> dict:
    """在现有 pending 上追加（深度整理逐类目分批用）；同 source/target 自动去重。"""
    now = db.now()
    return {"ok": True, "count": db.write(lambda conn: _insert_pending(conn, items, model, now))}


def get(sid: str) -> dict | None:
    r = db.connect().execute("SELECT * FROM tag_merge_suggestions WHERE id=?", (sid,)).fetchone()
    return _row(r) if r else None


def accept(sid: str) -> dict:
    from app.core import tags

    s = get(sid)
    if not s:
        raise ValueError("建议不存在")
    if s["status"] != "pending":
        raise ValueError("建议已处理")
    result = tags.merge(s["source_id"], s["target_id"])
    now = db.now()

    def _w(conn):
        conn.execute(
            "UPDATE tag_merge_suggestions SET status='accepted', updated_at=? WHERE id=?",
            (now, sid),
        )
        conn.execute(
            "UPDATE tag_merge_suggestions SET status='rejected', updated_at=? "
            "WHERE status='pending' AND id!=? AND (source_id=? OR target_id=?)",
            (now, sid, s["source_id"], s["source_id"]),
        )

    db.write(_w)
    return {"ok": True, "suggestion": s, "merge": result}


def reject(sid: str) -> dict:
    s = get(sid)
    if not s:
        raise ValueError("建议不存在")
    now = db.now()
    db.write(lambda conn: conn.execute(
        "UPDATE tag_merge_suggestions SET status='rejected', updated_at=? WHERE id=?",
        (now, sid),
    ))
    return {"ok": True}


def _row(r) -> dict:
    d = dict(r)
    d["confidence"] = float(d.get("confidence") or 0)
    return d
