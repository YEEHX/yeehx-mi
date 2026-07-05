"""Pending tag suggestions.

AI only applies existing tags automatically. Unknown words go here for review;
confirming creates/applies the tag, rejecting keeps the vocabulary stable.
"""
from __future__ import annotations

from app import db
from app.core.ids import new_id
from app.core import folders, assets as assets_mod, tags as tags_mod, inheritance

CAT_MAP = {
    "time": "天气", "时间": "天气",
    "shooting": "拍法", "镜头": "拍法",
    "mood": "氛围", "风格": "氛围",
    "content": "内容", "主体": "内容",
    "source": "来源",
    "city": "地点", "地标": "地点", "区域": "地点", "地点": "地点",
}


def _cat_id(category: str | None) -> str | None:
    mapped = CAT_MAP.get(category or "", category or "")
    hit = tags_mod.category_by_name(mapped) if mapped else None
    if hit:
        return hit["id"]
    fallback = tags_mod.category_by_name("未分类")
    return fallback["id"] if fallback else tags_mod.ensure_category("未分类")


def _apply_existing_hit(term: str, *, folder_id: str | None = None, asset_id: str | None = None,
                        hits: dict | None = None, source: str = "candidate_hit") -> bool:
    tag = tags_mod.get_by_name(term)
    if not tag:
        return False
    tid = tag["id"]
    subtrees, assets = set(), set()
    folders_to_apply = list((hits or {}).get("folders") or [])
    assets_to_apply = list((hits or {}).get("assets") or [])
    if folder_id:
        folders_to_apply.append(folder_id)
    if asset_id:
        assets_to_apply.append(asset_id)
    for fid in dict.fromkeys(folders_to_apply):
        f = folders.get(fid)
        if not f:
            continue
        folders.add_tags(fid, [tid], source=source)
        subtrees.add((f["volume_id"], f["rel_path"]))
    for aid in dict.fromkeys(assets_to_apply):
        if not assets_mod.get(aid):
            continue
        assets_mod.add_own(aid, tid, source=source)
        assets.add(aid)
    for vol, rel in subtrees:
        inheritance.recompute_subtree(vol, rel)
    inheritance.recompute_many(assets)
    return bool(subtrees or assets)


def add(term: str, suggested_category: str | None = None, *,
        folder_id: str | None = None, asset_id: str | None = None, kind: str = "tag",
        reason: str = "", confidence: float = 0.0, min_hits: int = 1):
    term = (term or "").strip()
    if not term:
        return None
    if tags_mod.get_by_name(term):
        _apply_existing_hit(term, folder_id=folder_id, asset_id=asset_id, source=f"{kind}_candidate_hit")
        return None
    cid = _cat_id(suggested_category)
    reason = (reason or "").strip()[:160]
    confidence = max(0.0, min(1.0, float(confidence or 0)))
    min_hits = max(1, int(min_hits or 1))

    def _w(conn):
        row = conn.execute("SELECT * FROM suggestions WHERE name=?", (term,)).fetchone()
        t = db.now()
        if row:
            hits = db.jloads(row["hits_json"], {"folders": [], "assets": []})
            if folder_id and folder_id not in hits["folders"]:
                hits["folders"].append(folder_id)
            if asset_id and asset_id not in hits["assets"]:
                hits["assets"].append(asset_id)
            cnt = len(hits["folders"]) + len(hits["assets"])
            status = row["status"] or "pending"
            if status == "watching" and cnt >= min_hits:
                status = "pending"
            conn.execute(
                "UPDATE suggestions SET hits_json=?, hit_count=?, status=?, category_id=COALESCE(category_id,?), "
                "reason=CASE WHEN ?!='' THEN ? ELSE reason END, "
                "confidence=MAX(COALESCE(confidence,0), ?), updated_at=? WHERE id=?",
                (db.jdumps(hits), cnt, status, cid, reason, reason, confidence, t, row["id"]),
            )
            return row["id"]
        hits = {"folders": [folder_id] if folder_id else [], "assets": [asset_id] if asset_id else []}
        status = "pending" if len(hits["folders"]) + len(hits["assets"]) >= min_hits else "watching"
        sid = new_id("sug")
        conn.execute(
            "INSERT INTO suggestions(id,name,category_id,aliases_json,ref_images_json,hits_json,hit_count,"
            "status,reason,confidence,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, term, cid, "[]", "[]", db.jdumps(hits), len(hits["folders"]) + len(hits["assets"]),
             status, reason, confidence, t, t),
        )
        return sid

    return db.write(_w)


def list_(status: str = "pending") -> list[dict]:
    rows = db.connect().execute(
        "SELECT s.*, c.name category, c.color color FROM suggestions s LEFT JOIN categories c ON c.id=s.category_id "
        "WHERE s.status=? ORDER BY s.hit_count DESC, s.name",
        (status,),
    ).fetchall()
    out = []
    for r in rows:
        d = _row(r)
        # Drop stale suggestions when the tag already exists.
        if status == "pending" and tags_mod.get_by_name(d["name"]):
            _apply_existing_hit(d["name"], hits=d["hits"], source="stale_candidate_hit")
            reject(d["id"])
            continue
        out.append(d)
    return out


def get(sid: str) -> dict | None:
    r = db.connect().execute(
        "SELECT s.*, c.name category, c.color color FROM suggestions s LEFT JOIN categories c ON c.id=s.category_id WHERE s.id=?",
        (sid,),
    ).fetchone()
    return _row(r) if r else None


def confirm(sid: str, category: str | None = None) -> dict:
    s = get(sid)
    if not s:
        return {"ok": False, "error": "待复核词不存在"}
    tag = tags_mod.get_by_name(s["name"]) or tags_mod.add(
        s["name"], category or s.get("category") or "未分类",
        aliases=s.get("aliases") or [], note=s.get("note"), ref_images=s.get("ref_images") or [],
    )
    tid = tag["id"]
    subtrees, aset = set(), set()
    for fid in s["hits"].get("folders", []):
        f = folders.get(fid)
        if not f:
            continue
        folders.add_tags(fid, [tid], source="ai_reviewed")
        subtrees.add((f["volume_id"], f["rel_path"]))
    for aid in s["hits"].get("assets", []):
        assets_mod.add_own(aid, tid, source="ai_reviewed")
        aset.add(aid)
    for vol, rel in subtrees:
        inheritance.recompute_subtree(vol, rel)
    inheritance.recompute_many(aset)
    db.write(lambda conn: conn.execute("UPDATE suggestions SET status='accepted', updated_at=? WHERE id=?", (db.now(), sid)))
    return {"ok": True, "applied": tag["name"], "category": tag["category"],
            "folders": len(s["hits"].get("folders", [])), "assets": len(s["hits"].get("assets", []))}


def merge_into_tag(sid: str, target_id: str) -> dict:
    s = get(sid)
    tag = tags_mod.get(target_id)
    if not s:
        return {"ok": False, "error": "待复核词不存在"}
    if not tag:
        return {"ok": False, "error": "目标标签不存在"}
    tags_mod.add_alias(target_id, s["name"])
    tid = target_id
    subtrees, aset = set(), set()
    for fid in s["hits"].get("folders", []):
        f = folders.get(fid)
        if not f:
            continue
        folders.add_tags(fid, [tid], source="ai_reviewed_alias")
        subtrees.add((f["volume_id"], f["rel_path"]))
    for aid in s["hits"].get("assets", []):
        assets_mod.add_own(aid, tid, source="ai_reviewed_alias")
        aset.add(aid)
    for vol, rel in subtrees:
        inheritance.recompute_subtree(vol, rel)
    inheritance.recompute_many(aset)
    db.write(lambda conn: conn.execute("UPDATE suggestions SET status='accepted', updated_at=? WHERE id=?", (db.now(), sid)))
    return {"ok": True, "applied": tag["name"], "alias": s["name"],
            "folders": len(s["hits"].get("folders", [])), "assets": len(s["hits"].get("assets", []))}


def list_watching(limit: int = 30) -> list[dict]:
    """观察态：出现次数还没到门槛的开放词（不进待复核，但可见可清）。"""
    rows = db.connect().execute(
        "SELECT s.*, c.name category, c.color color FROM suggestions s LEFT JOIN categories c ON c.id=s.category_id "
        "WHERE s.status='watching' ORDER BY s.hit_count DESC, s.updated_at DESC LIMIT ?", (limit,),
    ).fetchall()
    return [_row(r) for r in rows]


def watching_count() -> int:
    return db.connect().execute("SELECT COUNT(*) FROM suggestions WHERE status='watching'").fetchone()[0]


def clear_watching():
    db.write(lambda conn: conn.execute("DELETE FROM suggestions WHERE status='watching'"))


def expire_watching(days: int = 90) -> int:
    """观察词只进不出会暗膨胀：长期不再出现的自动清理（启动时调用）。"""
    cutoff = db.now() - days * 86400

    def _w(conn):
        return conn.execute(
            "DELETE FROM suggestions WHERE status='watching' AND updated_at<?", (cutoff,)
        ).rowcount
    return db.write(_w) or 0


def reject(sid: str):
    db.write(lambda conn: conn.execute("UPDATE suggestions SET status='rejected', updated_at=? WHERE id=?", (db.now(), sid)))


def reject_all():
    db.write(lambda conn: conn.execute("UPDATE suggestions SET status='rejected', updated_at=? WHERE status='pending'", (db.now(),)))


def confirm_all() -> dict:
    items = list_("pending")
    ok = 0
    for item in items:
        res = confirm(item["id"], category=item.get("suggested_category"))
        if res.get("ok"):
            ok += 1
    return {"ok": True, "accepted": ok}


def counts() -> dict:
    n = db.connect().execute("SELECT COUNT(*) FROM suggestions WHERE status='pending'").fetchone()[0]
    return {"total": n}


def _row(r) -> dict:
    d = dict(r)
    d["term"] = d["name"]
    d["aliases"] = db.jloads(d.pop("aliases_json", "[]"), [])
    d["ref_images"] = db.jloads(d.pop("ref_images_json", "[]"), [])
    d["hits"] = db.jloads(d.pop("hits_json", "{}"), {"folders": [], "assets": []})
    d["suggested_category"] = d.get("category") or "未分类"
    d["color"] = d.get("color") or "#8b8b96"
    return d
