"""Search and facet counts for the single-tag model."""
from __future__ import annotations

from app import db
from app.core import inheritance, tags as tags_mod, volumes


def _like_fallback_ids(conn, term: str) -> set[str]:
    """FTS 表本身炸掉时的兜底：直接对 assets 字段做 LIKE（名字/AI描述/备注）。
    覆盖面比 FTS 文本窄（少了文件夹名），但保证搜索永远有结果。"""
    like = f"%{term}%"
    return {r[0] for r in conn.execute(
        "SELECT asset_id FROM assets WHERE name LIKE ? OR desc_ai LIKE ? OR note LIKE ?",
        (like, like, like)).fetchall()}


def _term_asset_ids(conn, term: str, all_tags: list[dict]) -> tuple[set[str], bool]:
    """单个词的命中集合：FTS 文本命中 ∪ 标签/别名命中（词内任一字段命中即算）。"""
    ids: set[str] = set()
    degraded = False
    if db.FTS_OK:
        expr = db.fts_match_expr(term)
        if expr:
            try:
                ids |= {r[0] for r in conn.execute("SELECT asset_id FROM assets_fts WHERE assets_fts MATCH ?", (expr,)).fetchall()}
            except Exception as exc:   # FTS 表损坏/扩展异常：降级而非装死
                print(f"[警告] FTS 查询失败，已降级 LIKE 匹配：{exc}", flush=True)
                degraded = True
                ids |= _like_fallback_ids(conn, term)
    else:
        ids |= {r[0] for r in conn.execute("SELECT asset_id FROM assets_fts WHERE text LIKE ?", (f"%{term}%",)).fetchall()}

    tagids = [t["id"] for t in all_tags
              if term in t["name"] or t["name"] in term
              or any(term in a or a in term for a in (t.get("aliases") or []))]
    for ch in db.chunks(tagids):
        qs = ",".join("?" * len(ch))
        ids |= {r[0] for r in conn.execute(f"SELECT asset_id FROM asset_effective WHERE field='tag' AND value IN ({qs})", ch).fetchall()}
    return ids, degraded


def _query_asset_ids(query: str) -> tuple[set[str] | None, bool]:
    """返回 (命中 id 集合, fts_degraded)。2-1：FTS 抛异常不再静默吞掉——
    记日志、落 LIKE 兜底、把降级标记带给前端。
    多词语义 = AND：每个词各自求命中集（文本 ∪ 标签），词与词之间取交集。
    以前整句和标签名做双向包含，「黄鹤楼 航拍」会把所有带航拍的素材并进来。"""
    query = (query or "").strip()
    if not query:
        return None, False
    terms = db.fts_like_terms(query)
    if not terms:
        return set(), False
    conn = db.connect()
    all_tags = tags_mod.list_()
    inter: set[str] | None = None
    degraded = False
    for term in terms:
        tids, d = _term_asset_ids(conn, term, all_tags)
        degraded = degraded or d
        inter = tids if inter is None else (inter & tids)
        if not inter:   # 已经空了，后面的词不用再查
            break
    return inter or set(), degraded


def _scope_asset_ids(scope: dict | None) -> set[str] | None:
    if not scope:
        return None
    vid = scope.get("volume_id")
    rel = (scope.get("rel_path") or "").strip("/")
    if not vid and not rel:
        return None
    q = "SELECT asset_id FROM assets WHERE 1=1"
    args: list = []
    if vid:
        q += " AND volume_id=?"; args.append(vid)
    if rel:
        q += " AND (rel_path=? OR rel_path LIKE ?)"; args += [rel, rel + "/%"]
    return {r[0] for r in db.connect().execute(q, args).fetchall()}


def _ids_with(field: str, value: str) -> set[str]:
    return {r[0] for r in db.connect().execute(
        "SELECT asset_id FROM asset_effective WHERE field=? AND value=?", (field, value)
    ).fetchall()}


def search(query: str = "", facets: dict | None = None, scope: dict | None = None,
           limit: int = 200, offset: int = 0, sort: str = "default", with_facets: bool = True) -> dict:
    facets = facets or {}
    base = _scope_asset_ids(scope)

    for tid in facets.get("tag", []) or []:
        s = _ids_with("tag", tid)
        base = s if base is None else base & s
    for field in ("color", "lut", "status"):
        vals = facets.get(field) or []
        if vals:
            s = set()
            for v in vals:
                s |= _ids_with(field, v)
            base = s if base is None else base & s
    if facets.get("locked"):
        # 锁定直接查 assets 列（不走 asset_effective，老数据立即可筛）
        s = {r[0] for r in db.connect().execute("SELECT asset_id FROM assets WHERE locked=1").fetchall()}
        base = s if base is None else base & s

    qids, fts_degraded = _query_asset_ids(query)
    if qids is not None:
        base = qids if base is None else base & qids
    if base is None:
        base = {r[0] for r in db.connect().execute("SELECT asset_id FROM assets").fetchall()}

    ids = list(base)
    total = len(ids)
    page = _order(ids, sort)[offset:offset + limit]
    out = {"total": total, "assets": _hydrate(page), "limit": limit, "offset": offset, "sort": sort}
    if fts_degraded:
        out["fts_degraded"] = True
    if with_facets:
        out["facets"] = facet_counts(ids)
    return out


def _order(ids: list[str], sort: str = "default") -> list[str]:
    """排序在 Python 里做：分块取列，绕开 SQLite IN 变量上限（3 万条素材以上必撞）。"""
    if not ids:
        return []
    conn = db.connect()
    rows = []
    for ch in db.chunks(ids):
        qs = ",".join("?" * len(ch))
        rows += conn.execute(
            f"SELECT asset_id, score, mtime, size, name FROM assets WHERE asset_id IN ({qs})", ch
        ).fetchall()
    keys = {
        "default":   lambda r: (-(r["score"] or 0), -(r["mtime"] or 0), r["name"] or ""),
        "mtime":     lambda r: (-(r["mtime"] or 0), r["name"] or ""),
        "mtime_asc": lambda r: ((r["mtime"] or 0), r["name"] or ""),
        "score":     lambda r: (-(r["score"] or 0), -(r["mtime"] or 0), r["name"] or ""),
        "size":      lambda r: (-(r["size"] or 0), r["name"] or ""),
        "name":      lambda r: (r["name"] or "",),
    }
    rows.sort(key=keys.get(sort) or keys["default"])
    return [r["asset_id"] for r in rows]


def _hydrate(ids: list[str]) -> list[dict]:
    """整页批量水合（1-1）：own/excludes/文件夹链由 effective_many 各载入一次，
    不再每条结果现场爬祖先链（旧实现一页 200 条 ≈ 上千次查询）。"""
    if not ids:
        return []
    from app.core import assets as assets_mod
    tmap = tags_mod.label_map()
    vols = {v["volume_id"]: v for v in volumes.list_volumes()}
    assets = assets_mod.get_many(ids)
    eff_all = inheritance.effective_many(assets)
    out = []
    for a in assets:
        eff = inheritance.named_from_effective(eff_all[a["asset_id"]], tmap)
        f = eff_all[a["asset_id"]]["folder"]
        vol = vols.get(a["volume_id"], {})
        out.append({
            "asset_id": a["asset_id"], "name": a["name"], "kind": a["kind"],
            "thumb": a.get("thumb_path") or "", "status": a["status"], "score": a["score"],
            "thumb_v": _thumb_version(a),
            "locked": bool(a.get("locked")),
            "volume": vol.get("display_name") or vol.get("name") or "",
            "folder": (f or {}).get("name", ""), "rel_path": a["rel_path"],
            "tags": eff["tags"], "color": eff["color"], "lut": eff["lut"],
            "desc": a.get("desc_ai") or "", "note": a.get("note") or "",
        })
    return out


def _thumb_version(a: dict) -> int:
    try:
        return int(float(a.get("updated_at") or 0) * 1000)
    except (TypeError, ValueError):
        return 0


def facet_counts(ids: list[str]) -> dict:
    base = {"categories": {c["name"]: [] for c in tags_mod.list_categories()},
            "color": [], "lut": [], "status": [], "locked": []}
    if not ids:
        return base
    conn = db.connect()
    agg: dict[tuple[str, str], int] = {}
    locked_n = 0
    for ch in db.chunks(ids):
        qs = ",".join("?" * len(ch))
        for r in conn.execute(
            f"SELECT field,value,COUNT(*) n FROM asset_effective WHERE asset_id IN ({qs}) GROUP BY field,value", ch
        ).fetchall():
            key = (r["field"], r["value"])
            agg[key] = agg.get(key, 0) + r["n"]
        locked_n += conn.execute(
            f"SELECT COUNT(*) FROM assets WHERE locked=1 AND asset_id IN ({qs})", ch
        ).fetchone()[0]
    if locked_n:
        base["locked"].append({"value": "1", "name": "已锁定", "count": locked_n})
    tmap = tags_mod.label_map()
    for (field, value), n in agg.items():
        if field == "tag":
            t = tmap.get(value)
            if t:
                base["categories"].setdefault(t["category"], []).append({
                    "value": value, "name": t["name"], "count": n, "color": t["color"],
                })
        elif field in ("color", "lut", "status"):
            base[field].append({"value": value, "name": value, "count": n})
    for c in base["categories"]:
        base["categories"][c].sort(key=lambda x: -x["count"])
    for k in ("color", "lut", "status"):
        base[k].sort(key=lambda x: -x["count"])
    return base
