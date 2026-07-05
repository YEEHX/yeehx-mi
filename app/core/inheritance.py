"""Tag inheritance and search index rebuild."""
from __future__ import annotations

from app import db
from app.core import folders, assets as assets_mod, tags as tags_mod


def effective(asset: dict) -> dict:
    """单素材生效值。批量场景请用 effective_many（搜索水合 1-1 的教训：
    这里逐条爬祖先链，一页 200 条就是上千次查询）。"""
    return effective_many([asset])[asset["asset_id"]]


def effective_many(assets: list[dict]) -> dict[str, dict]:
    """批量算生效值：own/excludes/文件夹/folder_tags 各载入一次，不再逐条爬链。
    返回 {asset_id: {tags, lut, color, status, own, folder}}；
    own 是该素材自有标签集合，folder 是其直属文件夹 dict（给水合/FTS 文本用）。"""
    out: dict[str, dict] = {}
    if not assets:
        return out
    conn = db.connect()
    ids = [a["asset_id"] for a in assets]

    # own / excludes 批量载入（own 按 created_at 保序）
    own_map: dict[str, list[str]] = {}
    exc_map: dict[str, set[str]] = {}
    for ch in db.chunks(ids):
        qs = ",".join("?" * len(ch))
        for r in conn.execute(
                f"SELECT asset_id, tag_id FROM asset_tags WHERE asset_id IN ({qs}) ORDER BY created_at", ch).fetchall():
            own_map.setdefault(r["asset_id"], []).append(r["tag_id"])
        for r in conn.execute(
                f"SELECT asset_id, tag_id FROM asset_tag_excludes WHERE asset_id IN ({qs})", ch).fetchall():
            exc_map.setdefault(r["asset_id"], set()).add(r["tag_id"])

    # 涉及卷的文件夹 + 全部 folder_tags 一次载入
    fol_by_id: dict[str, dict] = {}
    fol_by_path: dict[tuple[str, str], dict] = {}
    for vid in {a["volume_id"] for a in assets}:
        for r in conn.execute("SELECT * FROM folders WHERE volume_id=?", (vid,)).fetchall():
            d = dict(r)
            fol_by_id[d["folder_id"]] = d
            fol_by_path[(vid, d["rel_path"])] = d
    ftags: dict[str, list[str]] = {}
    for r in conn.execute("SELECT folder_id, tag_id FROM folder_tags ORDER BY created_at").fetchall():
        ftags.setdefault(r["folder_id"], []).append(r["tag_id"])

    chain_cache: dict[str, list[dict]] = {}

    def chain_for(fol: dict) -> list[dict]:
        # 与 folders.ancestors 同序：自己 → 父级 → … → 根
        hit = chain_cache.get(fol["folder_id"])
        if hit is not None:
            return hit
        vid = fol["volume_id"]
        rel = (fol.get("rel_path") or "").strip("/")
        parts = rel.split("/") if rel else []
        paths = ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]
        res = [fol_by_path[(vid, p)] for p in paths if (vid, p) in fol_by_path]
        chain_cache[fol["folder_id"]] = res
        return res

    for a in assets:
        fol = fol_by_id.get(a.get("folder_id"))
        chain = chain_for(fol) if fol else []
        tag_ids: list[str] = []
        for f in chain:
            for tid in ftags.get(f["folder_id"], []):
                if tid not in tag_ids:
                    tag_ids.append(tid)
        excluded = exc_map.get(a["asset_id"], set())
        tag_ids = [t for t in tag_ids if t not in excluded]
        own = own_map.get(a["asset_id"], [])
        for tid in own:
            if tid not in tag_ids:
                tag_ids.append(tid)
        lut = a.get("lut")
        if lut is None:
            for f in chain:
                if f.get("lut"):
                    lut = f["lut"]
                    break
        out[a["asset_id"]] = {
            "tags": tag_ids,
            "lut": lut,
            "color": list((a.get("facts") or {}).get("color") or []),
            "status": a.get("status"),
            "own": set(own),
            "folder": fol,
        }
    return out


def recompute(asset_id: str):
    a = assets_mod.get(asset_id)
    if not a:
        return
    _recompute_batch([a])


def recompute_many(asset_ids) -> int:
    """批量重算：标签表/文件夹链/own/excludes 各载入一次，按 400 条一个写事务。
    旧实现每条素材独立开事务+全量加载标签表，5000 条要跑几十秒；这里快一个数量级。"""
    ids = list(dict.fromkeys(asset_ids or []))
    if not ids:
        return 0
    rows = assets_mod.get_many(ids)
    for group in db.chunks(rows, 400):
        _recompute_batch(group)
    return len(rows)


def _recompute_batch(assets: list[dict]):
    if not assets:
        return
    tmap = tags_mod.label_map()
    eff_all = effective_many(assets)

    payload: list[tuple[str, list, str]] = []
    for a in assets:
        eff = eff_all[a["asset_id"]]
        tag_ids, fol = eff["tags"], eff["folder"]
        rows = [(a["asset_id"], "tag", t) for t in tag_ids]
        if eff["lut"]:
            rows.append((a["asset_id"], "lut", eff["lut"]))
        for c in eff["color"]:
            rows.append((a["asset_id"], "color", c))
        if a.get("status"):
            rows.append((a["asset_id"], "status", a["status"]))
        names, aliases = [], []
        for tid in tag_ids:
            t = tmap.get(tid)
            if t:
                names.append(t["name"])
                aliases += t.get("aliases") or []
        parts_txt = [a.get("desc_ai") or "", a.get("note") or "", a.get("name") or ""] + names + aliases
        if fol:
            parts_txt += [fol.get("note") or "", fol.get("desc_ai") or "", fol.get("name") or ""]
        payload.append((a["asset_id"], rows, " ".join(p for p in parts_txt if p)))

    def _w(c):
        for aid, rows, text in payload:
            c.execute("DELETE FROM asset_effective WHERE asset_id=?", (aid,))
            if rows:
                c.executemany("INSERT OR IGNORE INTO asset_effective(asset_id,field,value) VALUES (?,?,?)", rows)
            db.fts_set(c, aid, text)
    db.write(_w)


def recompute_subtree(volume_id: str, rel_path: str) -> int:
    fids = folders.descendants_ids(volume_id, rel_path)
    if not fids:
        return 0
    conn = db.connect()
    aids = []
    for ch in db.chunks(fids):
        qs = ",".join("?" * len(ch))
        aids += [r[0] for r in conn.execute(
            f"SELECT asset_id FROM assets WHERE folder_id IN ({qs})", ch
        ).fetchall()]
    return recompute_many(aids)


def rebuild_all() -> int:
    db.write(lambda conn: conn.execute("DELETE FROM asset_effective"))
    aids = [r[0] for r in db.connect().execute("SELECT asset_id FROM assets").fetchall()]
    return recompute_many(aids)


def named_from_effective(eff: dict, tmap: dict) -> dict:
    """把 effective_many 的一条结果翻成带名字/颜色/own 的 UI 结构。"""
    items = []
    for tid in eff["tags"]:
        t = tmap.get(tid)
        items.append({
            "id": tid,
            "name": t["name"] if t else tid,
            "category": t["category"] if t else "未分类",
            "color": t["color"] if t else "#8b8b96",
            "group": "tag",
            "own": tid in eff["own"],
        })
    return {"tags": items, "lut": eff["lut"], "color": eff["color"], "status": eff["status"]}


def effective_named(asset: dict, tmap: dict | None = None) -> dict:
    """单素材版（详情页用）。搜索一页 200 条请走 effective_many + named_from_effective，
    否则就回到 1-1 修掉的那个每条 4-8 次查询的老问题。"""
    if tmap is None:
        tmap = tags_mod.label_map()
    return named_from_effective(effective_many([asset])[asset["asset_id"]], tmap)
