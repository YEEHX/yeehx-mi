"""Single tag vocabulary.

Every searchable word is a tag. Categories are only display text, color, and
ordering. Tags with aliases, notes, or reference images render as cards in the
same tag library; plain tags render as compact word blocks.
"""
from __future__ import annotations

from app import db
from app.core.ids import new_id

DEFAULT_CATEGORIES = [
    ("地点", "#e98a78"),
    ("天气", "#e9c07a"),
    ("内容", "#aeb2bc"),
    ("拍法", "#8ab0d6"),
    ("氛围", "#d6a8e9"),
    ("来源", "#7fd3a0"),
    ("未分类", "#8b8b96"),
]

DEFAULT_TAGS = {
    "天气": ["雨天", "雪天", "晴天", "阴天", "雾", "黄昏", "夜景", "白天", "晚霞", "蓝调时刻"],
    "内容": ["建筑", "古建筑", "人物", "街道", "山", "天空", "云", "植物", "车辆", "室内", "产品", "风景"],
    "拍法": ["航拍", "手持", "固定机位", "广角", "长焦", "延时", "特写", "移动镜头", "慢动作"],
    "氛围": ["冷峻", "温暖", "电影感", "科技感", "烟火气", "高级感", "清新", "复古"],
    "来源": ["实拍", "AI", "DJI", "索尼相机", "尼康相机", "iPhone", "Midjourney", "即梦", "可灵", "Runway"],
}
VOCAB_CATEGORY_MAP = {
    "主体": "内容",
    "镜头": "拍法",
    "氛围": "氛围",
    "风格": "氛围",
    "时间": "天气",
}

PLACE_CATS = ["地点"]
ALL_CATS = [x[0] for x in DEFAULT_CATEGORIES]


def normalize_term(value: str | None) -> str:
    return " ".join((value or "").strip().split()).casefold()


def clean_term(value: str | None) -> str:
    return " ".join((value or "").strip().split())


# ── 规范化词 → tag_id 内存缓存（打标时每个词都要查一次，旧版全表扫描+逐个 normalize） ──
_TERM_CACHE: dict[str, str] | None = None
_GROUPED_CACHE: tuple | None = None     # (签名, {类目: [词+别名]})，见 library_grouped


def invalidate_term_cache():
    global _TERM_CACHE, _GROUPED_CACHE
    _TERM_CACHE = None
    _GROUPED_CACHE = None


def _lib_signature(conn) -> tuple:
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM tags), (SELECT COALESCE(MAX(updated_at),0) FROM tags), "
        "(SELECT COUNT(*) FROM categories), (SELECT COALESCE(MAX(updated_at),0) FROM categories)"
    ).fetchone()
    return tuple(row)


def library_grouped() -> dict[str, list[str]]:
    """{类目: [词+别名...]}——喂给打标 prompt 和 AI 指令解析。模块级缓存（1-5）：
    每次调用只跑一条签名查询校验新鲜度，词表/类目任何写入（数量或 updated_at 变化）
    自动重建；所有写出口走 invalidate_term_cache() 也会立即清掉。
    返回值当只读用，调用方不得修改。"""
    global _GROUPED_CACHE
    conn = db.connect()
    sig = _lib_signature(conn)
    if _GROUPED_CACHE and _GROUPED_CACHE[0] == sig:
        return _GROUPED_CACHE[1]
    grouped: dict[str, list[str]] = {}
    for tag in list_(include_disabled=False):
        names = [tag["name"]] + list(tag.get("aliases") or [])
        grouped.setdefault(tag["category"], []).extend(names)
    for cat, names in list(grouped.items()):
        grouped[cat] = list(dict.fromkeys([n for n in names if n]))
    _GROUPED_CACHE = (sig, grouped)
    return grouped


def _term_cache() -> dict[str, str]:
    global _TERM_CACHE
    if _TERM_CACHE is None:
        m: dict[str, str] = {}
        for t in list_(include_disabled=False):
            m.setdefault(normalize_term(t["name"]), t["id"])
            for a in t.get("aliases") or []:
                m.setdefault(normalize_term(a), t["id"])
        _TERM_CACHE = m
    return _TERM_CACHE


def seed_if_empty():
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]:
        return

    def _w(c):
        t = db.now()
        for i, (name, color) in enumerate(DEFAULT_CATEGORIES):
            c.execute(
                "INSERT OR IGNORE INTO categories(id,name,color,ord,is_default,created_at,updated_at) "
                "VALUES (?,?,?,?,1,?,?)",
                (new_id("cat"), name, color, i, t, t),
            )
        for cat, names in DEFAULT_TAGS.items():
            cid = _category_id(c, cat)
            for name in names:
                c.execute(
                    "INSERT OR IGNORE INTO tags(id,name,category_id,aliases_json,ref_images_json,enabled,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,1,?,?)",
                    (new_id("t"), name, cid, "[]", "[]", t, t),
                )

    db.write(_w)
    invalidate_term_cache()


def ensure_core_vocabulary() -> dict:
    """Keep the bounded AI visual vocabulary in the tag table.

    The model can only choose fixed terms for visual content, shot, mood,
    weather/time and style. Seeding those terms up front keeps scans from
    turning bounded vocabulary misses into an endless stream of "new" tags.
    """
    from app.ai import prompt as ai_prompt

    grouped: dict[str, list[str]] = {cat: list(names) for cat, names in DEFAULT_TAGS.items()}
    for field, category in VOCAB_CATEGORY_MAP.items():
        grouped.setdefault(category, []).extend(ai_prompt.VOCAB.get(field, []))

    default_cats = {name: (color, i) for i, (name, color) in enumerate(DEFAULT_CATEGORIES)}

    def _w(conn):
        t = db.now()
        inserted = 0
        for name, (color, ord_) in default_cats.items():
            conn.execute(
                "INSERT OR IGNORE INTO categories(id,name,color,ord,is_default,created_at,updated_at) "
                "VALUES (?,?,?,?,1,?,?)",
                (new_id("cat"), name, color, ord_, t, t),
            )

        cat_ids = {
            r["name"]: r["id"]
            for r in conn.execute("SELECT id,name FROM categories").fetchall()
        }
        existing_terms: set[str] = set()
        rows = conn.execute("SELECT name, aliases_json FROM tags").fetchall()
        for row in rows:
            existing_terms.add(normalize_term(row["name"]))
            for alias in db.jloads(row["aliases_json"], []):
                existing_terms.add(normalize_term(alias))

        for category, names in grouped.items():
            cid = cat_ids.get(category)
            if not cid:
                continue
            for raw in dict.fromkeys(names):
                name = clean_term(raw)
                norm = normalize_term(name)
                if not norm or norm in existing_terms:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO tags(id,name,category_id,aliases_json,ref_images_json,enabled,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,1,?,?)",
                    (new_id("t"), name, cid, "[]", "[]", t, t),
                )
                existing_terms.add(norm)
                inserted += 1
        return inserted

    inserted = db.write(_w)
    if inserted:
        invalidate_term_cache()
    return {"ok": True, "inserted": inserted}


def core_vocabulary_terms() -> set[str]:
    """种子标签 + 固定视觉词表 的规范化词集——低频瘦身工具的保护名单，永不可删。"""
    from app.ai import prompt as ai_prompt
    out: set[str] = set()
    for names in DEFAULT_TAGS.values():
        out |= {normalize_term(n) for n in names}
    for field in VOCAB_CATEGORY_MAP:
        out |= {normalize_term(n) for n in ai_prompt.VOCAB.get(field, [])}
    return out


def list_categories() -> list[dict]:
    rows = db.connect().execute("SELECT * FROM categories ORDER BY ord, name").fetchall()
    return [dict(r) for r in rows]


def category_map() -> dict[str, dict]:
    return {c["id"]: c for c in list_categories()}


def category_by_name(name: str | None) -> dict | None:
    if not name:
        return None
    row = db.connect().execute("SELECT * FROM categories WHERE name=?", (name.strip(),)).fetchone()
    return dict(row) if row else None


def ensure_category(name: str | None, color: str | None = None) -> str | None:
    name = (name or "未分类").strip() or "未分类"
    hit = category_by_name(name)
    if hit:
        return hit["id"]

    def _w(conn):
        t = db.now()
        ord_ = conn.execute("SELECT COALESCE(MAX(ord),0)+1 FROM categories").fetchone()[0]
        cid = new_id("cat")
        conn.execute(
            "INSERT INTO categories(id,name,color,ord,is_default,created_at,updated_at) VALUES (?,?,?,?,0,?,?)",
            (cid, name, color or "#8b8b96", ord_, t, t),
        )
        return cid

    return db.write(_w)


def update_category(cid: str, **fields):
    def _w(conn):
        cols, args = [], []
        for k in ("name", "color", "ord"):
            if k in fields:
                cols.append(f"{k}=?"); args.append(fields[k])
        if not cols:
            return
        cols.append("updated_at=?"); args.append(db.now()); args.append(cid)
        conn.execute(f"UPDATE categories SET {', '.join(cols)} WHERE id=?", args)
    db.write(_w)


def delete_category(cid: str):
    fallback = ensure_category("未分类")

    def _w(conn):
        conn.execute("UPDATE tags SET category_id=?, updated_at=? WHERE category_id=?", (fallback, db.now(), cid))
        conn.execute("DELETE FROM categories WHERE id=? AND name!='未分类'", (cid,))
    db.write(_w)


def list_(category: str | None = None, include_disabled: bool = True, **_) -> list[dict]:
    q = "SELECT t.*, c.name category, c.color color FROM tags t LEFT JOIN categories c ON c.id=t.category_id WHERE 1=1"
    args: list = []
    if category:
        q += " AND c.name=?"; args.append(category)
    if not include_disabled:
        q += " AND t.enabled=1"
    q += " ORDER BY t.pinned DESC, c.ord, t.name"
    return [_row(r) for r in db.connect().execute(q, args).fetchall()]


def get(tag_id: str) -> dict | None:
    r = db.connect().execute(
        "SELECT t.*, c.name category, c.color color FROM tags t LEFT JOIN categories c ON c.id=t.category_id WHERE t.id=?",
        (tag_id,),
    ).fetchone()
    return _row(r) if r else None


def get_by_name(name: str) -> dict | None:
    norm = normalize_term(clean_term(name))
    if not norm:
        return None
    tid = _term_cache().get(norm)
    return get(tid) if tid else None


def find_term_conflict(term: str, *, exclude_id: str | None = None) -> dict | None:
    norm = normalize_term(term)
    if not norm:
        return None
    rows = db.connect().execute(
        "SELECT t.*, c.name category, c.color color FROM tags t LEFT JOIN categories c ON c.id=t.category_id "
        "ORDER BY t.name"
    ).fetchall()
    for row in rows:
        tag = _row(row)
        if exclude_id and tag["id"] == exclude_id:
            continue
        if normalize_term(tag["name"]) == norm:
            return {"tag": tag, "field": "name", "term": tag["name"]}
        for alias in tag.get("aliases") or []:
            if normalize_term(alias) == norm:
                return {"tag": tag, "field": "alias", "term": alias}
    return None


def validate_terms(name: str, aliases=None, *, exclude_id: str | None = None) -> tuple[str, list[str]]:
    name = clean_term(name)
    if not name:
        raise ValueError("标签名为空")
    aliases_clean: list[str] = []
    seen_aliases: set[str] = set()
    name_norm = normalize_term(name)
    for raw in aliases or []:
        alias = clean_term(raw)
        if not alias:
            continue
        norm = normalize_term(alias)
        if norm == name_norm:
            raise ValueError(f"别名不能和标签名重复：{alias}")
        if norm in seen_aliases:
            raise ValueError(f"别名重复：{alias}")
        seen_aliases.add(norm)
        aliases_clean.append(alias)
    for term in [name] + aliases_clean:
        conflict = find_term_conflict(term, exclude_id=exclude_id)
        if not conflict:
            continue
        tag = conflict["tag"]
        if conflict["field"] == "name":
            raise ValueError(f"“{term}”已经是标签「{tag['name']}」")
        raise ValueError(f"“{term}”已经是标签「{tag['name']}」的别名")
    return name, aliases_clean


def add(name: str, category: str | None = None, *, category_id: str | None = None,
        aliases=None, note=None, ref_images=None, reuse_existing: bool = True, **_) -> dict:
    name = clean_term(name)
    existing = get_by_name(name)
    if existing:
        if reuse_existing:
            return existing
        raise ValueError(f"“{name}”已经是标签「{existing['name']}」或它的别名")
    name, aliases = validate_terms(name, aliases)
    cid = category_id or ensure_category(category)

    def _w(conn):
        t = db.now()
        tid = new_id("t")
        conn.execute(
            "INSERT INTO tags(id,name,category_id,aliases_json,note,ref_images_json,enabled,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,1,?,?)",
            (tid, name, cid, db.jdumps(aliases or []), note, db.jdumps(ref_images or []), t, t),
        )
        return tid

    tid = db.write(_w)
    invalidate_term_cache()
    return get(tid)


def resolve(name: str, category: str | None = None, create: bool = True, default_cat: str = "未分类") -> str | None:
    hit = get_by_name(name)
    if hit:
        return hit["id"]
    return add(name, category or default_cat)["id"] if create else None


def update(tag_id: str, **fields):
    if "name" in fields or "aliases" in fields:
        current = get(tag_id)
        if not current:
            raise ValueError("标签不存在")
        final_name = fields.get("name", current["name"])
        final_aliases = fields.get("aliases", current.get("aliases") or [])
        fields["name"], fields["aliases"] = validate_terms(final_name, final_aliases, exclude_id=tag_id)

    def _w(conn):
        cols, args = [], []
        if "category" in fields and "category_id" not in fields:
            fields["category_id"] = ensure_category(fields["category"])
        for k in ("name", "category_id", "note", "pinned", "enabled"):
            if k in fields:
                cols.append(f"{k}=?")
                args.append(int(fields[k]) if k in ("pinned", "enabled") else fields[k])
        if "aliases" in fields:
            cols.append("aliases_json=?"); args.append(db.jdumps(fields["aliases"] or []))
        if "ref_images" in fields:
            cols.append("ref_images_json=?"); args.append(db.jdumps(fields["ref_images"] or []))
        if not cols:
            return
        cols.append("updated_at=?"); args.append(db.now()); args.append(tag_id)
        conn.execute(f"UPDATE tags SET {', '.join(cols)} WHERE id=?", args)
    db.write(_w)
    invalidate_term_cache()
    _recompute_usage(tag_id)


def add_alias(tag_id: str, alias: str):
    alias = (alias or "").strip()
    if not alias:
        return
    t = get(tag_id)
    if not t:
        return
    aliases = list(dict.fromkeys((t.get("aliases") or []) + [alias]))
    update(tag_id, aliases=aliases)


def _recompute_usage(tag_id: str):
    from app.core import inheritance
    aids = [r[0] for r in db.connect().execute(
        "SELECT asset_id FROM asset_effective WHERE field='tag' AND value=?",
        (tag_id,),
    ).fetchall()]
    inheritance.recompute_many(aids)


def merge(source_id: str, target_id: str) -> dict:
    """Merge source tag into target tag, keeping source terms as aliases."""
    if source_id == target_id:
        raise ValueError("不能合并到自己")
    source = get(source_id)
    target = get(target_id)
    if not source or not target:
        raise ValueError("标签不存在")

    aliases = list(target.get("aliases") or [])
    for term in [source["name"]] + list(source.get("aliases") or []):
        term = clean_term(term)
        if not term or normalize_term(term) == normalize_term(target["name"]):
            continue
        if any(normalize_term(x) == normalize_term(term) for x in aliases):
            continue
        conflict = find_term_conflict(term, exclude_id=target_id)
        if conflict and conflict["tag"]["id"] != source_id:
            raise ValueError(f"“{term}”已经被「{conflict['tag']['name']}」使用")
        aliases.append(term)

    def _w(conn):
        t = db.now()
        folder_rows = conn.execute("SELECT folder_id, source FROM folder_tags WHERE tag_id=?", (source_id,)).fetchall()
        asset_rows = conn.execute("SELECT asset_id, source FROM asset_tags WHERE tag_id=?", (source_id,)).fetchall()
        exclude_rows = conn.execute("SELECT asset_id FROM asset_tag_excludes WHERE tag_id=?", (source_id,)).fetchall()
        target_assets = conn.execute(
            "SELECT asset_id FROM asset_effective WHERE field='tag' AND value=?",
            (target_id,),
        ).fetchall()
        conn.executemany(
            "INSERT OR IGNORE INTO folder_tags(folder_id,tag_id,source,created_at) VALUES (?,?,?,?)",
            [(r["folder_id"], target_id, r["source"] or "merged", t) for r in folder_rows],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO asset_tags(asset_id,tag_id,source,created_at) VALUES (?,?,?,?)",
            [(r["asset_id"], target_id, r["source"] or "merged", t) for r in asset_rows],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO asset_tag_excludes(asset_id,tag_id,created_at) VALUES (?,?,?)",
            [(r["asset_id"], target_id, t) for r in exclude_rows
             if not conn.execute("SELECT 1 FROM asset_tags WHERE asset_id=? AND tag_id=?",
                                 (r["asset_id"], target_id)).fetchone()],
        )
        conn.execute("DELETE FROM folder_tags WHERE tag_id=?", (source_id,))
        conn.execute("DELETE FROM asset_tags WHERE tag_id=?", (source_id,))
        conn.execute("DELETE FROM asset_tag_excludes WHERE tag_id=?", (source_id,))
        conn.execute("DELETE FROM asset_effective WHERE field='tag' AND value=?", (source_id,))
        conn.execute("UPDATE tags SET aliases_json=?, updated_at=? WHERE id=?", (db.jdumps(aliases), t, target_id))
        conn.execute("DELETE FROM tags WHERE id=?", (source_id,))
        conn.execute("UPDATE suggestions SET status='rejected', updated_at=? WHERE name=?", (t, source["name"]))
        return {
            "folders": [r["folder_id"] for r in folder_rows],
            "assets": [r["asset_id"] for r in target_assets] +
                      [r["asset_id"] for r in asset_rows] +
                      [r["asset_id"] for r in exclude_rows],
        }

    affected = db.write(_w)
    invalidate_term_cache()
    from app.core import folders as folders_mod, inheritance
    for fid in dict.fromkeys(affected["folders"]):
        f = folders_mod.get(fid)
        if f:
            inheritance.recompute_subtree(f["volume_id"], f["rel_path"])
    inheritance.recompute_many(affected["assets"])
    return {"ok": True, "merged": source["name"], "target": target["name"],
            "aliases": aliases, "folders": len(set(affected["folders"])),
            "assets": len(set(affected["assets"]))}


def set_ref_asset(tag_id: str, asset_id: str, on: bool = True):
    t = get(tag_id)
    if not t:
        return
    item = {"type": "asset", "id": asset_id}
    refs = list(t.get("ref_images") or [])
    if on and item not in refs:
        refs.append(item)
    elif not on:
        refs = [x for x in refs if x != item]
    update(tag_id, ref_images=refs)


def add_ref_file(tag_id: str, rel_name: str):
    t = get(tag_id)
    if not t:
        return
    refs = list(t.get("ref_images") or [])
    item = {"type": "file", "path": rel_name}
    if item not in refs:
        refs.append(item)
    update(tag_id, ref_images=refs)


def remove_ref(tag_id: str, ref: dict):
    t = get(tag_id)
    if not t or not isinstance(ref, dict):
        return
    refs = []
    for item in (t.get("ref_images") or []):
        if item == ref:
            continue
        if ref.get("type") == item.get("type"):
            if ref.get("type") == "asset" and ref.get("id") == item.get("id"):
                continue
            if ref.get("type") == "file" and ref.get("path") == item.get("path"):
                continue
        refs.append(item)
    update(tag_id, ref_images=refs)


def delete(tag_id: str):
    def _w(conn):
        conn.execute("DELETE FROM folder_tags WHERE tag_id=?", (tag_id,))
        conn.execute("DELETE FROM asset_tags WHERE tag_id=?", (tag_id,))
        conn.execute("DELETE FROM asset_tag_excludes WHERE tag_id=?", (tag_id,))
        conn.execute("DELETE FROM asset_effective WHERE field='tag' AND value=?", (tag_id,))
        conn.execute("DELETE FROM tags WHERE id=?", (tag_id,))
    db.write(_w)
    invalidate_term_cache()


def label_map() -> dict:
    return {t["id"]: t for t in list_()}


def _category_id(conn, name: str) -> str | None:
    row = conn.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
    return row[0] if row else None


def _row(r) -> dict:
    d = dict(r)
    d["aliases"] = db.jloads(d.pop("aliases_json", "[]"), [])
    d["ref_images"] = db.jloads(d.pop("ref_images_json", "[]"), [])
    d["category"] = d.get("category") or "未分类"
    d["color"] = d.get("color") or "#8b8b96"
    d["group"] = "tag"
    d["has_card"] = bool(d["aliases"] or d.get("note") or d["ref_images"] or d.get("pinned"))
    return d
