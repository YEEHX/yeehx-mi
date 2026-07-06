"""快扫（以"有素材的文件夹"为单位）+ 缩略图 + 清理 的任务编排。

快扫流程（每个有素材的文件夹各跑一遍）：
1. 空文件夹跳过。
2. 登记该夹全部文件（卷+相对路径、大小、时间、类型；轻，不读画面）。
3. 取代表素材：<2 全取；≥2 均匀取 rep_samples 个（约 1/3、2/3，避开头尾）。
4. 收尾时只给代表素材生图并打标，形成快速搜索底稿。
5. 已登记且未变化的文件夹自动跳过（按文件夹指纹）。
盘未挂载绝不判删除；隐藏夹跳过；可暂停/停止/继续（任务系统负责）。
"""
from __future__ import annotations
import os
from pathlib import Path

from app import db
from app.config import get_cfg
from app.core import volumes, folders, assets as assets_mod, inheritance
from app.core import osplat
from app.core import files as fmod
from app.media import thumbnails
from app import tasks


# ── 枚举：从某根往下，找出所有"直接含媒体文件"的文件夹（相对卷根） ──
def enumerate_media_folders(mount: Path, root_abs: Path, volume_id: str | None = None) -> list[str]:
    out: list[str] = []
    stack = [root_abs]
    while stack:
        cur = stack.pop()
        if volume_id:
            rel_cur = os.path.relpath(str(cur), str(mount)).replace(os.sep, "/")
            rel_cur = "" if rel_cur == "." else rel_cur
            if folders.is_hidden_path(volume_id, rel_cur):
                continue
        try:
            entries = list(cur.iterdir())
        except OSError:
            continue
        has_media = False
        for e in entries:
            if osplat.should_skip_name(e.name):
                continue
            if e.is_dir():
                if e.suffix.lower() in fmod.PKG_EXT:
                    has_media = True            # 包当素材
                else:
                    stack.append(e)
            elif fmod.is_media(e):
                has_media = True
        if has_media:
            rel = os.path.relpath(str(cur), str(mount)).replace(os.sep, "/")
            out.append("" if rel == "." else rel)
    return out


def ensure_current_folder(volume_id: str, rel: str):
    """确保从根到该文件夹的每一级文件夹行都存在（浏览树+继承用）。"""
    parts = rel.split("/") if rel else []
    for i in range(len(parts) + 1):
        folders.ensure(volume_id, "/".join(parts[:i]))


def _media_files(folder_abs: Path) -> list[Path]:
    out = []
    try:
        for e in folder_abs.iterdir():
            if osplat.should_skip_name(e.name):
                continue
            if e.is_dir() and e.suffix.lower() in fmod.PKG_EXT:
                out.append(e)
            elif e.is_file() and fmod.is_media(e):
                out.append(e)
    except OSError:
        pass
    return out


def _pick_reps(asset_ids: list[str], kinds: list[str], n: int) -> list[str]:
    """可出缩略图的素材里，<n 全取；否则均匀取 n 个（避开头尾废镜）。"""
    thumbable = [aid for aid, k in zip(asset_ids, kinds) if k in fmod.THUMBABLE_KINDS]
    if len(thumbable) <= n:
        return thumbable
    idx = [round((i + 1) * len(thumbable) / (n + 1)) for i in range(n)]
    idx = sorted(set(min(len(thumbable) - 1, max(0, j)) for j in idx))
    return [thumbable[j] for j in idx]


# ── 登记单个媒体文件（含内容指纹 + 搬家/改名识别） ──
def _needs_content_id(row: dict | None, size: int, mtime: float) -> bool:
    if row is None:
        return True              # 新文件：必算（顺便用来找搬家的旧记录）
    if not row.get("content_id"):
        return True              # 旧记录还没指纹：补算
    return row.get("size") != size or int(row.get("mtime") or 0) != int(mtime)   # 内容变了：重算


def _find_moved(cid: str) -> dict | None:
    """同内容指纹 + 旧位置文件确实已不在（且其卷在线）→ 认定是搬家/改名。
    卷离线一律不判（绝不判删除）。"""
    for cand in assets_mod.find_by_content(cid):
        m = volumes.resolve_mount(cand["volume_id"])
        if m is None:
            continue
        if not (m / cand["rel_path"]).exists():
            return cand
    return None


def register_media_asset(vid: str, fid: str, mount: Path, primary: Path, kind: str,
                         extra: dict | None = None, *, compute_cid: bool = True) -> str:
    """登记一个媒体文件：算/补内容指纹；新路径先按指纹找搬家/改名的旧记录，
    找得到就把旧记录迁过来（标签/星级/缩略图全保留），找不到才新建。

    compute_cid=False（浏览即登记走这条，1-2）：不在 HTTP 请求里同步读文件算指纹
    （头尾各 8MB，机械盘 500 个新文件能卡几分钟）。调用方负责把这批素材丢进
    content_id 队列后台补算；搬家识别/去重在补算完成时做——延迟而非缺失。"""
    rel = os.path.relpath(str(primary), str(mount)).replace(os.sep, "/")
    try:
        st = primary.stat()
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, 0
    row = assets_mod.get_by_path(vid, rel)
    needs = _needs_content_id(row, size, mtime)
    cid, cid_err = "", ""
    if needs and compute_cid:
        cid, cid_err = fmod.content_id_ex(primary)
    if row is None and cid:
        moved = _find_moved(cid)
        if moved:
            assets_mod.rehome(moved["asset_id"], volume_id=vid, folder_id=fid,
                              rel_path=rel, name=primary.name)
    facts: dict = {"extra": extra or {}}
    if cid_err:
        facts["fingerprint_error"] = cid_err      # 2-2：失败留痕，体检页可见可重试
    aid = assets_mod.upsert(vid, fid, rel, primary.name, kind, size, mtime,
                            content_id=cid, facts=facts)
    if cid and row is not None and (row.get("facts") or {}).get("fingerprint_error"):
        assets_mod.merge_facts(aid, {"fingerprint_error": None})   # 这次算成了：清掉旧错误
    if needs and not compute_cid and row is not None and row.get("content_id"):
        # 内容变了但这次没算：清掉旧指纹交给后台补算，防止陈旧指纹误判重复/搬家
        assets_mod.set_fields(aid, content_id="")
    return aid


# ════════════════ 任务处理器 ════════════════
def _h_quick_index(task: dict, folder_rel: str):
    cfg = get_cfg()
    scope = db.jloads(task["scope_json"], {})
    vid = scope.get("volume_id")
    mount = volumes.resolve_mount(vid)
    if mount is None:
        raise RuntimeError("卷离线")
    folder_abs = mount / folder_rel if folder_rel else mount
    media = _media_files(folder_abs)
    if not media:
        return
    ensure_current_folder(vid, folder_rel)
    fid = folders.ensure(vid, folder_rel)
    fp = fmod.folder_fingerprint(media)
    existing = folders.get(fid)
    if existing and existing.get("fingerprint") == fp:
        if existing.get("scan_state") == "queued":
            folders.set_meta(fid, scan_state="none")
        inheritance.recompute_subtree(vid, folder_rel)
        return   # 未变化，跳过

    descs = fmod.classify(folder_abs, media, cfg)
    asset_ids, kinds, seen_rels = [], [], set()
    for d in descs:
        aid = register_media_asset(vid, fid, mount, d["primary"], d["kind"], d.get("extra", {}))
        asset_ids.append(aid)
        kinds.append(d["kind"])
        seen_rels.add(os.path.relpath(str(d["primary"]), str(mount)).replace(os.sep, "/"))

    # 顺手同步：本夹里文件已消失的记录直接清掉（重扫只增不减的问题）。
    # 有人工痕迹的（标签/星级/锁定/备注）保留——给搬家识别或手动「同步」处理，避免误删人工成果。
    tagged_ids = {r[0] for r in db.connect().execute(
        "SELECT DISTINCT asset_id FROM asset_tags WHERE asset_id IN "
        "(SELECT asset_id FROM assets WHERE folder_id=?)", (fid,)).fetchall()}
    for old in assets_mod.in_folder(fid):
        if old["rel_path"] in seen_rels or (mount / old["rel_path"]).exists():
            continue
        valuable = (old.get("score") or old.get("locked") or old.get("note")
                    or old["asset_id"] in tagged_ids)
        if valuable:
            continue
        thumb = old.get("thumb_path")
        if thumb:
            tp = cfg.thumbs_dir / thumb
            if tp.exists():
                try:
                    tp.unlink()
                except OSError:
                    pass
        assets_mod.delete(old["asset_id"])

    reps = set(_pick_reps(asset_ids, kinds, cfg.rep_samples))
    for aid in asset_ids:
        assets_mod.set_facts(aid, {"is_rep": 1 if aid in reps else 0})

    folders.set_meta(fid, fingerprint=fp, asset_count=len(asset_ids), scan_state="none")
    inheritance.recompute_many(asset_ids)


def _assets_for_scope(scope: dict, *, reps_only: bool = False) -> list[dict]:
    vid = scope.get("volume_id")
    root = (scope.get("rel_path") or "").strip("/")
    if not vid:
        return []
    like = (root + "/%") if root else "%"
    sql = (
        "SELECT asset_id, thumb_path, status, locked FROM assets "
        "WHERE volume_id=? AND (rel_path=? OR rel_path LIKE ?) "
        "AND kind IN ('photo','video','film','timelapse','raw','red')"
    )
    args = [vid, root, like]
    if reps_only:
        sql += " AND (facts_json LIKE ? OR facts_json LIKE ?)"
        args += ['%"is_rep": 1%', '%"is_rep":1%']
    rows = db.connect().execute(sql + " ORDER BY rel_path", args).fetchall()
    return [dict(r) for r in rows]


def _needs_tag(status: str | None) -> bool:
    return (status or "pending") in ("pending", "failed")


def _finalize_quick_index(task: dict):
    """收尾是增量的（v1.9.0）：缺缩略图的才生图；只给 未打标/失败 且未锁定的排打标。
    已打标素材不重打、已有缩略图不重生成——快扫/扫描/同步可以放心对同一范围反复跑。
    （强制重打走选中后的「打标」按钮；重出缩略图走「生图」/LUT。）"""
    scope = db.jloads(task["scope_json"], {})
    params = db.jloads(task["params_json"], {})
    mode = params.get("mode") or "quick"
    rows = _assets_for_scope(scope, reps_only=(mode != "full"))
    need_thumb = [r["asset_id"] for r in rows if not r["thumb_path"]]
    untagged_ready = [r["asset_id"] for r in rows
                      if r["thumb_path"] and not r["locked"] and _needs_tag(r["status"])]
    if need_thumb:
        title = "扫描文件夹·生图" if mode == "full" else "快扫·生图"
        tasks.create_task("thumb", "thumb_gen", title, need_thumb, scope=scope, params={"next": "fine"})
    if untagged_ready:
        # 有图但还没打标的（如手动生过图、或上次打标失败）：不用等生图任务，直接排打标
        tasks.create_task("ai", "fine_tag", "打标", untagged_ready, scope=scope)


def _finalize_thumb_gen(task: dict):
    params = db.jloads(task["params_json"], {})
    if params.get("next") != "fine":
        return
    rows = db.connect().execute(
        "SELECT item_key FROM task_items WHERE task_id=? AND status='done'",
        (task["task_id"],),
    ).fetchall()
    ids = []
    for r in rows:
        a = assets_mod.get(r[0])
        if a and a.get("thumb_path") and not a.get("locked") and _needs_tag(a.get("status")):
            ids.append(a["asset_id"])
    if ids:
        scope = db.jloads(task["scope_json"], {})
        tasks.create_task("ai", "fine_tag", "打标", list(dict.fromkeys(ids)), scope=scope)


def _h_thumb_gen(task: dict, asset_id: str):
    res = thumbnails.regenerate_effective(asset_id)
    if not res.get("ok"):
        if res.get("error") == "offline":
            raise RuntimeError("卷离线")
        if res.get("unsupported") or res.get("error") in ("该类型无缩略图",):
            return
        raise RuntimeError(res.get("error") or "缩略图生成失败")
    # 文件夹封面：用第一张成功的代表图；并向上回填还没有封面的祖先
    # （1-3 之后浏览页只读 cover_thumb，不再现场扫子树，所以父级封面靠这里喂）
    a = assets_mod.get(asset_id)
    if a and a.get("thumb_path"):
        f = folders.get(a["folder_id"])
        if f:
            folders.set_meta(a["folder_id"], cover_thumb=a["thumb_path"])
            for anc in folders.ancestors(a["volume_id"], f["rel_path"])[1:]:
                if not anc.get("cover_thumb"):
                    folders.set_meta(anc["folder_id"], cover_thumb=a["thumb_path"])


def _h_content_id(task: dict, asset_id: str):
    """补算内容指纹（重复检测/搬家识别的前提）。

    1-2 之后浏览登记不再同步算指纹，这里多了延迟搬家识别：本行还是"干净占位"
    （无标签/星级/备注/锁定/缩略图，即浏览刚登记的样子）且旧位置有同指纹记录、
    其文件确实已不在（卷在线）→ 把旧记录整体迁过来（标签/星级/缩略图全保留），
    删掉占位行。若用户在补算前已经对新行打了标/生了图，则不合并（交给「同步」清旧行）。"""
    a = assets_mod.get(asset_id)
    if not a or a.get("content_id"):
        return
    src = volumes.abspath(a)
    if src is None:
        raise RuntimeError("卷离线")
    if not src.exists():
        return
    cid, cid_err = fmod.content_id_ex(src)
    if not cid:
        # 2-2：失败留痕（权限/占用等），体检页显示计数并提供"重试失败项"
        assets_mod.merge_facts(asset_id, {"fingerprint_error": cid_err or "未知原因"})
        return
    if (a.get("facts") or {}).get("fingerprint_error"):
        assets_mod.merge_facts(asset_id, {"fingerprint_error": None})
    placeholder = not (a.get("score") or a.get("locked") or a.get("note")
                       or a.get("thumb_path") or assets_mod.own_tag_ids(asset_id))
    if placeholder:
        moved = _find_moved(cid)
        if moved and moved["asset_id"] != asset_id:
            assets_mod.delete(asset_id)
            new_id = assets_mod.rehome(moved["asset_id"], volume_id=a["volume_id"],
                                       folder_id=a["folder_id"], rel_path=a["rel_path"],
                                       name=a["name"])
            assets_mod.set_fields(new_id, content_id=cid)
            inheritance.recompute(new_id)
            return
    assets_mod.set_fields(asset_id, content_id=cid)


def _h_cleanup(task: dict, folder_id: str):
    f = folders.get(folder_id)
    if not f:
        return
    mount = volumes.resolve_mount(f["volume_id"])
    if mount is None:
        return   # 盘未挂载 → 绝不判删除
    for a in assets_mod.in_folder(folder_id):
        p = mount / a["rel_path"]
        if not p.exists():
            thumb = a.get("thumb_path")
            if thumb:
                tp = get_cfg().thumbs_dir / thumb
                if tp.exists():
                    try:
                        tp.unlink()
                    except OSError:
                        pass
            assets_mod.delete(a["asset_id"])


def register_all():
    """注册本模块的任务处理器。AI 打标在 tagging.py。"""
    tasks.register("quick_index", _h_quick_index, finalize=_finalize_quick_index)
    tasks.register("thumb_gen", _h_thumb_gen, finalize=_finalize_thumb_gen)
    tasks.register("cleanup", _h_cleanup)
    tasks.register("content_id", _h_content_id)
    from app.scan import tagging
    from app import export
    tagging.register()
    export.register()


def backfill_content_ids() -> dict:
    """给所有还没内容指纹、且卷在线的素材补算指纹（在 text 队列后台跑）。"""
    rows = db.connect().execute(
        "SELECT asset_id, volume_id FROM assets WHERE content_id IS NULL OR content_id=''"
    ).fetchall()
    online = {v["volume_id"] for v in volumes.list_volumes() if v.get("online")}
    ids = [r["asset_id"] for r in rows if r["volume_id"] in online]
    skipped = len(rows) - len(ids)
    if not ids:
        return {"ok": True, "count": 0, "task_id": None, "offline_skipped": skipped}
    tid = tasks.create_task("text", "content_id", "补算指纹", ids)
    return {"ok": True, "count": len(ids), "task_id": tid, "offline_skipped": skipped}


# ════════════════ 对外入口（建任务） ════════════════
def quick_import(root_path: str, mode: str = "quick", title: str | None = None) -> dict:
    """快扫/扫描文件夹：递归登记有素材的文件夹，收尾按 mode 排生图+打标（只补缺图/未打标）。"""
    info = volumes.identify(root_path)
    mount = Path(info["mount"])
    root_abs = Path(root_path).resolve()
    fols = enumerate_media_folders(mount, root_abs, info["volume_id"])
    scope = {"volume_id": info["volume_id"], "rel_path": info["rel_path"]}
    if not fols:
        return {"ok": True, "folders": 0, "task_id": None, "volume": info}
    mode = "full" if mode == "full" else "quick"
    title = title or f"{'扫描文件夹' if mode == 'full' else '快扫'} {info['name']}"
    tid = tasks.create_task("index", "quick_index", title, fols, scope=scope, params={"mode": mode})
    return {"ok": True, "folders": len(fols), "task_id": tid, "volume": info}


def fill_thumbs(volume_id: str, rel_path: str) -> dict:
    """补全本夹（及子夹）所有素材的缩略图。"""
    conn = db.connect()
    rel = rel_path.strip("/")
    like = (rel + "/%") if rel else "%"
    ids = [r[0] for r in conn.execute(
        "SELECT asset_id FROM assets WHERE volume_id=? AND (rel_path=? OR rel_path LIKE ?)",
        (volume_id, rel, like)).fetchall()]
    if not ids:
        return {"ok": True, "count": 0, "task_id": None}
    tid = tasks.create_task("thumb", "thumb_gen", "生图", ids,
                            scope={"volume_id": volume_id, "rel_path": rel})
    return {"ok": True, "count": len(ids), "task_id": tid}


def fine_tag(volume_id: str, rel_path: str | None = None, asset_ids: list[str] | None = None) -> dict:
    """打标：逐素材 AI 打标签 + 描述。给文件夹或一批素材。"""
    if asset_ids is None:
        conn = db.connect()
        rel = (rel_path or "").strip("/")
        like = (rel + "/%") if rel else "%"
        asset_ids = [r[0] for r in conn.execute(
            "SELECT asset_id FROM assets WHERE volume_id=? AND (rel_path=? OR rel_path LIKE ?) "
            "AND locked=0 AND thumb_path IS NOT NULL AND thumb_path!=''",
            (volume_id, rel, like)).fetchall()]
    if not asset_ids:
        return {"ok": True, "count": 0, "task_id": None}
    tid = tasks.create_task("ai", "fine_tag", "打标", asset_ids,
                            scope={"volume_id": volume_id, "rel_path": rel_path or ""})
    return {"ok": True, "count": len(asset_ids), "task_id": tid}


def cleanup_scope(volume_id: str, rel_path: str = "") -> dict:
    """清理丢失：盘在线时同步清掉已删除文件的库记录（不碰原文件）。"""
    if volumes.resolve_mount(volume_id) is None:
        return {"ok": False, "error": "卷离线，跳过清理（绝不判删除）"}
    conn = db.connect()
    rel = rel_path.strip("/")
    like = (rel + "/%") if rel else "%"
    fids = [r[0] for r in conn.execute(
        "SELECT folder_id FROM folders WHERE volume_id=? AND (rel_path=? OR rel_path LIKE ?)",
        (volume_id, rel, like)).fetchall()]
    if not fids:
        return {"ok": True, "count": 0, "task_id": None}
    tid = tasks.create_task("index", "cleanup", "清理丢失", fids,
                            scope={"volume_id": volume_id, "rel_path": rel})
    return {"ok": True, "folders": len(fids), "task_id": tid}
