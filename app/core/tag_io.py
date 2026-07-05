"""标签库导出 / 导入 / 自动备份（一期 0-4）。

边界：导出的是「词表定义」——类目、词、别名、备注、置顶/停用、参考图。
不含 素材↔标签 的打标关系：那属于数据库本体，完整备份 = 复制 out/miying.sqlite。

自动备份：合并、瘦身、删词、替换导入这类会减少词表信息的操作执行前，
先把当前词表落一份 JSON 到 out/backups/（滚动保留最近 KEEP_BACKUPS 份），
备份写不出来就不执行操作。out/ 不进 git、不进发布包，无泄漏问题。

导入两种模式：
- merge（默认）：按 normalize_term 匹配词名和别名。已存在 → 并别名/参考图/补备注，
  不动现有 id、不动 enabled；新词 → 新建（类目缺失自动建）。绝不删除现有标签。
  同一份文件导两遍结果幂等。
- replace：自动备份 → 清空词表（连带打标关系，词没了关系必然失效）→ 按文件重建。
"""
from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

from app import db, __version__ as APP_VERSION
from app.config import get_cfg
from app.core import tags as TG

SCHEMA_VERSION = 1
KEEP_BACKUPS = 10
_REF_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}


# ── 导出 ────────────────────────────────────────────────────────────────
def export_payload() -> dict:
    cats = TG.list_categories()
    tags = TG.list_()
    return {
        "schema_version": SCHEMA_VERSION,
        "app": "觅影",
        "app_version": APP_VERSION,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "categories": [{"name": c["name"], "color": c["color"], "order": c["ord"]} for c in cats],
        "tags": [{
            "name": t["name"],
            "category": t["category"],
            "aliases": t.get("aliases") or [],
            "note": t.get("note") or "",
            "pinned": int(t.get("pinned") or 0),
            "enabled": int(1 if t.get("enabled", 1) else 0),
            "ref_images": t.get("ref_images") or [],
        } for t in tags],
    }


def export_json_text() -> str:
    return json.dumps(export_payload(), ensure_ascii=False, indent=1)


def export_zip_bytes() -> bytes:
    """tags.json + 被引用的 out/refimg/ 参考图文件，打成一个 zip。"""
    payload = export_payload()
    cfg = get_cfg()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("tags.json", json.dumps(payload, ensure_ascii=False, indent=1))
        seen: set[str] = set()
        for t in payload["tags"]:
            for ref in t["ref_images"]:
                name = ref.get("path") if isinstance(ref, dict) and ref.get("type") == "file" else None
                if not name or name in seen:
                    continue
                seen.add(name)
                p = cfg.refimg_dir / name
                if p.exists() and p.is_file():
                    z.write(p, f"refimg/{name}")
    return buf.getvalue()


# ── 自动备份 ─────────────────────────────────────────────────────────────
def backup(reason: str = "manual", keep: int = KEEP_BACKUPS, with_refs: bool = False) -> Path:
    """词表落盘 out/backups/tags-时间戳-原因.json（with_refs=True 时打 zip 连参考图），
    滚动保留最近 keep 份（json 和 zip 一起计数）。"""
    cfg = get_cfg()
    bdir = cfg.out_dir / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in str(reason) if ch.isalnum() or ch in "-_")[:24] or "manual"
    ext = "zip" if with_refs else "json"
    stem = f"tags-{time.strftime('%Y%m%d-%H%M%S')}-{safe}"
    path = bdir / f"{stem}.{ext}"
    n = 1
    while path.exists():                       # 同一秒多次操作不互相覆盖
        n += 1
        path = bdir / f"{stem}-{n}.{ext}"
    if with_refs:
        path.write_bytes(export_zip_bytes())
    else:
        path.write_text(export_json_text(), encoding="utf-8")
    olds = sorted([*bdir.glob("tags-*.json"), *bdir.glob("tags-*.zip")],
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for p in olds[keep:]:
        p.unlink(missing_ok=True)
    return path


# ── 危险区：统计 + 清空 ──────────────────────────────────────────────────
def tags_stats() -> dict:
    """清空前的影响范围预览数字（也给体检页用）。"""
    conn = db.connect()
    tags = TG.list_()
    n = lambda q: conn.execute(q).fetchone()[0]
    return {
        "tags": len(tags),
        "aliases": sum(len(t.get("aliases") or []) for t in tags),
        "ref_files": sum(1 for t in tags for r in (t.get("ref_images") or [])
                         if isinstance(r, dict) and r.get("type") == "file"),
        "categories": n("SELECT COUNT(*) FROM categories"),
        "candidates": n("SELECT COUNT(*) FROM suggestions"),
        "candidates_pending": n("SELECT COUNT(*) FROM suggestions WHERE status IN ('pending','watching')"),
        "merge_suggestions": n("SELECT COUNT(*) FROM tag_merge_suggestions"),
        "asset_links": n("SELECT COUNT(*) FROM asset_tags"),
        "folder_links": n("SELECT COUNT(*) FROM folder_tags"),
        "excludes": n("SELECT COUNT(*) FROM asset_tag_excludes"),
        "locked_with_tags": n("SELECT COUNT(DISTINCT at.asset_id) FROM asset_tags at "
                              "JOIN assets a ON a.asset_id=at.asset_id WHERE a.locked=1"),
        "ai_descs": n("SELECT COUNT(*) FROM assets WHERE locked=0 AND desc_ai IS NOT NULL AND desc_ai!=''"),
        "status_tagged": n("SELECT COUNT(*) FROM assets WHERE locked=0 AND status='tagged'"),
        "locked_total": n("SELECT COUNT(*) FROM assets WHERE locked=1"),
    }


def clear_tags(scope: str = "all", keep_locked: bool = True) -> dict:
    """一键清空（4-3）。先强制 zip 备份（含参考图），备份失败抛 OSError、不动库。

    scope="all"：恢复出厂——词表/类目/候选/合并建议/全部打标关系/参考图全清，
        锁定素材的标签同样被清（锁只防 AI 改写，防不了词表本身被删），
        收尾 seed_if_empty() 重建出厂种子，与 db/reset 行为一致。
    scope="taggings"：只清打标关系+候选数据，词表/类目/参考图原样保留；
        keep_locked=True 时锁定素材自己的标签保留（此模式词表还在，锁才保得住）。

    素材本体字段（与单素材「清除打标」_clear_tagging 同一套锁语义）：
        AI 描述 desc_ai / desc_locked / 打标状态 status → 一并重置回 pending；
        锁定素材跳过（scope=taggings 且 keep_locked=False 时锁定也清）；
        人工备注 note、星级 score、锁定标记本身、facts 永远不动。
    """
    if scope not in ("all", "taggings"):
        raise ValueError(f"未知 scope：{scope}")
    stats = tags_stats()
    backup_path = backup(f"pre-clear-{scope}", with_refs=True)

    if scope == "all":
        def _w(conn):
            conn.execute("DELETE FROM folder_tags")
            conn.execute("DELETE FROM asset_tags")
            conn.execute("DELETE FROM asset_tag_excludes")
            conn.execute("DELETE FROM asset_effective WHERE field='tag'")
            conn.execute("DELETE FROM suggestions")
            conn.execute("DELETE FROM tag_merge_suggestions")
            conn.execute("DELETE FROM tags")
            conn.execute("DELETE FROM categories")
            # 素材本体的打标产物一并归零（AI 描述/状态），锁定素材跳过；note/score 不动
            conn.execute("UPDATE assets SET desc_ai=NULL, desc_locked=0, status='pending', updated_at=? "
                         "WHERE locked=0", (db.now(),))
            # 文件夹 AI 描述：现行版本已无写入点，属旧版残留，顺手清（人工 note 不动）
            conn.execute("UPDATE folders SET desc_ai=NULL, updated_at=? "
                         "WHERE desc_ai IS NOT NULL AND desc_ai!=''", (db.now(),))
        db.write(_w)
        cfg = get_cfg()
        removed_refs = 0
        if cfg.refimg_dir.exists():
            for p in cfg.refimg_dir.glob("*"):
                if p.is_file():
                    p.unlink(missing_ok=True)
                    removed_refs += 1
        TG.invalidate_term_cache()
        TG.seed_if_empty()             # 出厂种子类目+种子词
        TG.ensure_core_vocabulary()    # 固定视觉词表（启动时也会做；这里立即补齐，不用等重启）
        stats["ref_files_removed"] = removed_refs
    else:
        def _w(conn):
            if keep_locked:
                locked = "SELECT asset_id FROM assets WHERE locked=1"
                conn.execute(f"DELETE FROM asset_tags WHERE asset_id NOT IN ({locked})")
                conn.execute(f"DELETE FROM asset_tag_excludes WHERE asset_id NOT IN ({locked})")
                conn.execute("UPDATE assets SET desc_ai=NULL, desc_locked=0, status='pending', updated_at=? "
                             "WHERE locked=0", (db.now(),))
            else:
                conn.execute("DELETE FROM asset_tags")
                conn.execute("DELETE FROM asset_tag_excludes")
                conn.execute("UPDATE assets SET desc_ai=NULL, desc_locked=0, status='pending', updated_at=?",
                             (db.now(),))
            conn.execute("DELETE FROM folder_tags")
            conn.execute("DELETE FROM asset_effective WHERE field='tag'")
            conn.execute("DELETE FROM suggestions")
            conn.execute("DELETE FROM tag_merge_suggestions")
            conn.execute("UPDATE folders SET desc_ai=NULL, updated_at=? "
                         "WHERE desc_ai IS NOT NULL AND desc_ai!=''", (db.now(),))
        db.write(_w)
        TG.invalidate_term_cache()

    from app.core import inheritance
    inheritance.rebuild_all()      # 重算 asset_effective + 刷新 FTS（保留的锁定标签会写回）
    return {"ok": True, "scope": scope, "keep_locked": keep_locked,
            "cleared": stats, "backup": backup_path.name}


# ── 导入 ────────────────────────────────────────────────────────────────
def import_blob(raw: bytes, mode: str = "merge") -> dict:
    """zip（tags.json + refimg/）或裸 JSON 都收。返回导入报告。"""
    ref_files: dict[str, bytes] = {}
    if raw[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                names = z.namelist()
                if "tags.json" not in names:
                    raise ValueError("zip 里没有 tags.json，不是觅影标签库导出包")
                data = json.loads(z.read("tags.json").decode("utf-8"))
                for nm in names:
                    if nm.startswith("refimg/") and not nm.endswith("/"):
                        base = Path(nm).name           # 防 zip 路径穿越：只取文件名
                        if Path(base).suffix.lower() in _REF_IMG_EXTS:
                            ref_files[base] = z.read(nm)
        except zipfile.BadZipFile:
            raise ValueError("zip 文件损坏")
    else:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("不是合法的 JSON / zip 文件")
    return import_payload(data, mode=mode, ref_files=ref_files)


def import_payload(data: dict, mode: str = "merge", ref_files: dict[str, bytes] | None = None) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("tags"), list):
        raise ValueError("不是觅影标签库导出文件（缺 tags 列表）")
    ver = int(data.get("schema_version") or 0)
    if ver > SCHEMA_VERSION:
        raise ValueError(f"文件 schema_version={ver} 比当前支持的 {SCHEMA_VERSION} 新，请先升级觅影")
    if mode not in ("merge", "replace"):
        raise ValueError(f"未知导入模式：{mode}")

    if mode == "replace":
        backup("pre-replace")                  # 失败会抛 OSError，由路由层兜
        _clear_vocabulary()

    cats_before = {c["name"] for c in TG.list_categories()}
    for c in data.get("categories") or []:
        name = (c.get("name") or "").strip() if isinstance(c, dict) else ""
        if name:
            TG.ensure_category(name, c.get("color"))

    cfg = get_cfg()
    cfg.refimg_dir.mkdir(parents=True, exist_ok=True)
    valid_assets = {r[0] for r in db.connect().execute("SELECT asset_id FROM assets").fetchall()}

    created = merged = skipped = ref_written = 0
    for item in data.get("tags") or []:
        if not isinstance(item, dict):
            skipped += 1
            continue
        name = TG.clean_term(item.get("name"))
        if not name:
            skipped += 1
            continue
        try:
            existing = TG.get_by_name(name)
            refs = _usable_refs(item.get("ref_images"), ref_files, valid_assets, cfg)
            ref_written += refs.pop("_written")
            if existing:
                _merge_into(existing, item, refs["refs"])
                merged += 1
            else:
                _create_from(item, name, refs["refs"])
                created += 1
        except ValueError:
            skipped += 1

    cats_created = len({c["name"] for c in TG.list_categories()} - cats_before)
    TG.invalidate_term_cache()
    if mode == "replace":
        from app.core import inheritance
        inheritance.rebuild_all()              # 关系已清空：刷新 asset_effective 和 FTS
    return {"ok": True, "mode": mode, "created": created, "merged": merged,
            "skipped": skipped, "categories_created": cats_created, "ref_files": ref_written}


def _usable_refs(raw_refs, ref_files: dict[str, bytes] | None, valid_assets: set, cfg) -> dict:
    """参考图引用过滤：file 引用要么 zip 里带了文件（落盘），要么本地已存在；
    asset 引用只有素材还在库里才保留。返回 {"refs": [...], "_written": n}。"""
    out, written = [], 0
    for ref in raw_refs or []:
        if not isinstance(ref, dict):
            continue
        if ref.get("type") == "file" and ref.get("path"):
            base = Path(str(ref["path"])).name
            dest = cfg.refimg_dir / base
            if ref_files and base in ref_files and not dest.exists():
                dest.write_bytes(ref_files[base])
                written += 1
            if dest.exists():
                out.append({"type": "file", "path": base})
        elif ref.get("type") == "asset" and ref.get("id") in valid_assets:
            out.append({"type": "asset", "id": ref["id"]})
    return {"refs": out, "_written": written}


def _safe_aliases(name: str, aliases, *, exclude_id: str | None = None) -> list[str]:
    """逐个别名做冲突检查：撞了别的标签就丢弃该别名（绝不动别人），其余保留。"""
    out: list[str] = []
    name_norm = TG.normalize_term(name)
    seen = {name_norm}
    for raw in aliases or []:
        alias = TG.clean_term(raw)
        norm = TG.normalize_term(alias)
        if not alias or norm in seen:
            continue
        conflict = TG.find_term_conflict(alias, exclude_id=exclude_id)
        if conflict:
            continue
        seen.add(norm)
        out.append(alias)
    return out


def _merge_into(existing: dict, item: dict, refs: list[dict]):
    """合并进已有标签：并别名（冲突丢弃）、并参考图、备注只补空、置顶只升不降。
    不动 enabled、不动类目——现有库的人工状态优先。"""
    tid = existing["id"]
    cur_aliases = list(existing.get("aliases") or [])
    cur_norms = {TG.normalize_term(existing["name"])} | {TG.normalize_term(a) for a in cur_aliases}
    incoming = [TG.clean_term(a) for a in (item.get("aliases") or []) if TG.clean_term(a)]
    incoming = [a for a in incoming if TG.normalize_term(a) not in cur_norms]
    add_aliases = _safe_aliases(existing["name"], incoming, exclude_id=tid)

    cur_refs = list(existing.get("ref_images") or [])
    add_refs = [r for r in refs if r not in cur_refs]

    fields: dict = {}
    if add_aliases:
        fields["aliases"] = cur_aliases + add_aliases
    if add_refs:
        fields["ref_images"] = cur_refs + add_refs
    if not (existing.get("note") or "").strip() and (item.get("note") or "").strip():
        fields["note"] = str(item["note"]).strip()
    if int(item.get("pinned") or 0) and not existing.get("pinned"):
        fields["pinned"] = 1
    if fields:
        TG.update(tid, **fields)


def _create_from(item: dict, name: str, refs: list[dict]):
    category = (item.get("category") or "未分类").strip() or "未分类"
    aliases = _safe_aliases(name, item.get("aliases"))
    note = str(item.get("note") or "").strip() or None
    tag = TG.add(name, category, aliases=aliases, note=note)
    fields: dict = {}
    if refs:
        fields["ref_images"] = refs
    if int(item.get("pinned") or 0):
        fields["pinned"] = 1
    if not int(item.get("enabled", 1)):
        fields["enabled"] = 0
    if fields:
        TG.update(tag["id"], **fields)


def _clear_vocabulary():
    """replace 模式专用：清词表+类目，连带打标关系（词没了关系必然失效）。
    suggestions（候选词历史）与词表独立，保留。只动觅影自己的库，原始素材零接触。"""
    def _w(conn):
        conn.execute("DELETE FROM folder_tags")
        conn.execute("DELETE FROM asset_tags")
        conn.execute("DELETE FROM asset_tag_excludes")
        conn.execute("DELETE FROM asset_effective WHERE field='tag'")
        conn.execute("DELETE FROM tags")
        conn.execute("DELETE FROM categories")
    db.write(_w)
    TG.invalidate_term_cache()
