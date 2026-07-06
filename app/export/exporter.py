"""导出三类：图片 / 视频·原素材 / 清单。都走导出队列、后台进行、可暂停继续。

原素材全程只读：复制，不剪切、不改名原文件。
- 图片：挂 LUT → 烧录 LUT 的原图（从原素材重渲染）；没挂 → 直出原图；视频图片=代表帧静帧。
- 视频/原素材：复制原文件，可按命名规则重命名；默认不烧 LUT。
- 清单：路径(txt/csv) / FCPXML(Final Cut) / Premiere 路径清单(txt) / JSON —— 给 AI/剪辑调用。
  注：Premiere 不支持 fcpxml（它只认 FCP7 XML），所以 premiere 格式输出 txt 路径清单，
  在 Premiere 里直接把清单里的路径拖进项目即可。
"""
from __future__ import annotations
import csv
import io
import shutil
import xml.sax.saxutils as sax
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from app import db
from app.core import assets as assets_mod, inheritance, volumes
from app.core.ids import new_id
from app.core.files import filename_date
from app.media import frames, thumbnails
from app import tasks

VALID_TYPES = {"image", "original", "manifest"}


# ════════════════ 入口 ════════════════
def start_export(etype: str, asset_ids: list[str], target: str,
                 options: dict | None = None) -> dict:
    if etype not in VALID_TYPES:
        return {"ok": False, "error": f"未知导出类型: {etype}"}
    options = options or {}
    asset_ids = list(dict.fromkeys(asset_ids or []))
    if not asset_ids:
        return {"ok": False, "error": "没有要导出的素材"}
    ex = new_id("ex")
    t = db.now()
    db.write(lambda conn: conn.execute(
        "INSERT INTO exports(id,type,scope_json,target,options_json,total,done,failed,status,log_json,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,0,0,'pending','[]',?,?)",
        (ex, etype, db.jdumps({"asset_ids": asset_ids}), target, db.jdumps(options), len(asset_ids), t, t),
    ))
    params = {"export_id": ex, "type": etype, "target": target, "options": options}
    if etype == "manifest":
        params["asset_ids"] = asset_ids
        items = ["__manifest__"]
    else:
        items = asset_ids
    fmt = options.get("format", "")
    title = {"image": "导出图片", "original": "导出原素材", "manifest": f"导出清单·{fmt or 'json'}"}[etype]
    tid = tasks.create_task("export", "export_run", title, items, params=params)
    return {"ok": True, "export_id": ex, "task_id": tid, "count": len(asset_ids)}


def export_record(export_id: str) -> dict | None:
    r = db.connect().execute("SELECT * FROM exports WHERE id=?", (export_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["options"] = db.jloads(d.pop("options_json", "{}"), {})
    d["log"] = db.jloads(d.pop("log_json", "[]"), [])
    d["scope"] = db.jloads(d.pop("scope_json", "{}"), {})
    return d


# ════════════════ 处理器 ════════════════
def _h_export(task: dict, item: str):
    params = db.jloads(task["params_json"], {})
    etype = params["type"]
    target = Path(params["target"])
    options = params.get("options", {})
    ex = params["export_id"]

    if etype == "manifest":
        _write_manifest(ex, params.get("asset_ids", []), target, options)
        return

    a = assets_mod.get(item)
    if not a:
        raise RuntimeError("素材不存在")
    src = volumes.abspath(a)
    if src is None or not src.exists():
        raise RuntimeError("卷离线或原文件缺失")
    target.mkdir(parents=True, exist_ok=True)
    if etype == "image":
        dst, lut = _export_image(a, src, target, options)
    else:
        dst, lut = _export_original(a, src, target, options)
    _log(ex, {"src": str(src), "dst": str(dst), "lut": lut, "ok": True})


def _finalize_export(task: dict):
    ex = db.jloads(task["params_json"], {}).get("export_id")
    if not ex:
        return
    conn = db.connect()
    failed = conn.execute("SELECT COUNT(*) FROM task_items WHERE task_id=? AND status='failed'", (task["task_id"],)).fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM task_items WHERE task_id=? AND status='done'", (task["task_id"],)).fetchone()[0]
    db.write(lambda c: c.execute("UPDATE exports SET status='done', done=?, failed=?, updated_at=? WHERE id=?",
                                 (done, failed, db.now(), ex)))


# ════════════════ 具体导出 ════════════════
def _eff(a: dict):
    return inheritance.effective_named(a)


def _places(cats: dict) -> list[str]:
    """v2 统一标签模型：地点是一个类目（不再分城市/地标）。按类目取地点词。"""
    from app.core import tags as tags_mod
    out: list[str] = []
    for cat in tags_mod.PLACE_CATS:
        out += cats.get(cat) or []
    return out


_WIN_BAD = set('<>:"/\\|?*')                     # Windows 非法文件名字符（mac 也别写出 "/"）
_WIN_RESERVED = {"con", "prn", "aux", "nul",
                 *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _sanitize_stem(stem: str) -> str:
    """导出文件名清洗：标签/原名里可能带 : ? * 等字符，Windows（NTFS/exFAT）写不进去。"""
    s = "".join("_" if (c in _WIN_BAD or ord(c) < 32) else c for c in stem)
    s = s.rstrip(". ")                            # Windows 不允许结尾点/空格
    if s.lower() in _WIN_RESERVED:
        s = "_" + s
    return s or "未命名"


def _build_name(a: dict, ext: str, options: dict) -> str:
    if options.get("naming") != "pattern":
        stem = Path(a["name"]).stem
        return _sanitize_stem(stem) + ext
    eff = _eff(a)
    places = _places(_cats(eff))
    date = filename_date(Path(a["name"]).stem) or datetime.fromtimestamp(a.get("mtime") or 0).strftime("%Y%m%d")
    parts = places[:2] + [date]
    base = "_".join(p for p in parts if p) or Path(a["name"]).stem
    return _sanitize_stem(base) + ext


def _resolve_conflict(dst: Path, options: dict) -> Path | None:
    mode = options.get("conflict", "rename")
    if not dst.exists():
        return dst
    if mode == "overwrite":
        return dst
    if mode == "skip":
        return None
    stem, suf = dst.stem, dst.suffix
    i = 1
    while True:
        cand = dst.with_name(f"{stem}_{i}{suf}")
        if not cand.exists():
            return cand
        i += 1


def _export_original(a: dict, src: Path, target: Path, options: dict):
    """复制原文件（不烧 LUT）。可选烧录 LUT 视频为高级项。"""
    eff_lut = thumbnails.effective_lut(a)
    if options.get("burn_lut") and a["kind"] in ("video", "film") and eff_lut:
        lf = thumbnails.lut_filter_for(eff_lut)
        if lf:
            dst = _resolve_conflict(target / _build_name(a, ".mp4", options), options)
            if dst is None:
                return target / "(skipped)", None
            ok = _ffmpeg_burn(src, dst, lf)
            if ok:
                return dst, eff_lut
    name = _build_name(a, src.suffix, options)
    dst = _resolve_conflict(target / name, options)
    if dst is None:
        return target / "(skipped)", None
    shutil.copy2(src, dst)
    return dst, None


def _export_image(a: dict, src: Path, target: Path, options: dict):
    """图片：原分辨率。挂 LUT→烧录重渲染；否则直出原图；视频→代表帧静帧。"""
    eff_lut = thumbnails.effective_lut(a)
    lf = thumbnails.lut_filter_for(eff_lut)
    kind = a["kind"]
    if kind in ("video", "film", "red"):
        dst = _resolve_conflict(target / _build_name(a, ".jpg", options), options)
        if dst is None:
            return target / "(skipped)", None
        facts = a.get("facts") or {}
        # 所有视频都用 rep_ts（用户在缩略图里看到的那一帧），没有才退回中点帧
        frames.extract_fullres(src, kind, lf, dst, ts=facts.get("rep_ts"))
        return dst, eff_lut if lf else None
    # 照片/延时/RAW
    if lf:
        dst = _resolve_conflict(target / _build_name(a, ".jpg", options), options)
        if dst is None:
            return target / "(skipped)", None
        frames.extract_fullres(src, "photo", lf, dst)
        return dst, eff_lut
    # 没挂 LUT → 直出原图（复制原文件，保原分辨率/格式）
    dst = _resolve_conflict(target / _build_name(a, src.suffix, options), options)
    if dst is None:
        return target / "(skipped)", None
    shutil.copy2(src, dst)
    return dst, None


def _ffmpeg_burn(src: Path, dst: Path, lut_filter: str) -> bool:
    import subprocess
    try:
        r = subprocess.run([frames.ffmpeg_exe(), "-y", "-v", "error", "-i", str(src),
                            "-vf", lut_filter, "-c:a", "copy", str(dst)],
                           capture_output=True, timeout=3600)
        return r.returncode == 0 and dst.exists()
    except (subprocess.SubprocessError, OSError):
        return False


# ════════════════ 清单 ════════════════
def _cats(eff: dict) -> dict:
    by = {}
    for x in eff["tags"]:
        by.setdefault(x["category"], []).append(x["name"])
    return by


def _row_for(a: dict) -> dict:
    eff = _eff(a)
    src = volumes.abspath(a)
    vol = volumes.get(a["volume_id"]) or {}
    cats = _cats(eff)
    places = _places(cats)
    facts = a.get("facts") or {}
    return {
        "asset_id": a["asset_id"], "name": a["name"], "kind": a["kind"],
        "path": str(src) if src else "", "volume": vol.get("display_name") or vol.get("name") or "",
        "rel_path": a["rel_path"],
        # v2 地点类目：city=第一个地点词，landmarks=其余；places=完整列表
        "city": places[0] if places else None, "landmarks": places[1:], "places": places,
        "tags": [x["name"] for x in eff["tags"]], "by_category": cats,
        "color": eff["color"], "lut": eff["lut"],
        "duration": facts.get("duration"),
        "desc": a.get("desc_ai") or "", "note": a.get("note") or "",
    }


def _write_manifest(ex: str, asset_ids: list[str], target: Path, options: dict):
    fmt = (options.get("format") or "json").lower()
    target.mkdir(parents=True, exist_ok=True)
    rows = [_row_for(a) for a in assets_mod.get_many(asset_ids)]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if fmt in ("txt", "premiere"):
        # Premiere 不认 fcpxml（只认 FCP7 XML），给它的就是纯路径清单：整列复制/拖进项目即可
        out = target / f"清单_{ts}.txt"
        out.write_text("\n".join(r["path"] for r in rows if r["path"]), encoding="utf-8")
    elif fmt == "csv":
        out = target / f"清单_{ts}.csv"
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["asset_id", "name", "kind", "path", "volume", "rel_path", "city",
                    "landmarks", "tags", "color", "lut", "desc", "note"])
        for r in rows:
            w.writerow([r["asset_id"], r["name"], r["kind"], r["path"], r["volume"], r["rel_path"],
                        r["city"] or "", "|".join(r["landmarks"]), "|".join(r["tags"]),
                        "|".join(r["color"]), r["lut"] or "", r["desc"], r["note"]])
        out.write_text(buf.getvalue(), encoding="utf-8-sig")
    elif fmt == "fcpxml":
        out = target / f"清单_{ts}.fcpxml"
        out.write_text(_fcpxml(rows), encoding="utf-8")
    else:  # json（给 AI agent 的接口）
        from app import __version__ as _v
        out = target / f"清单_{ts}.json"
        out.write_text(db.jdumps({"generated": ts, "count": len(rows),
                                  "generator": f"觅影 v{_v} · 玩椰 YEEHX · yeehx.com",
                                  "assets": rows}), encoding="utf-8")
    _log(ex, {"manifest": str(out), "format": fmt, "count": len(rows), "ok": True})


def _xattr(value) -> str:
    """XML 属性值转义（含双引号）。"""
    return sax.escape(str(value or ""), {'"': "&quot;"})


def _fcpxml(rows: list[dict]) -> str:
    """最小可导入 FCPXML（Final Cut Pro）：媒体引用进一个 event，不造假时间线。
    路径做 URL 百分号编码（中文/空格），时长已知才写（来自缩略图阶段记录的 facts.duration）。
    Premiere 不支持 fcpxml，请用 premiere（txt 路径清单）格式。"""
    res = ['<format id="r0" name="FFVideoFormat1080p25" frameDuration="100/2500s" width="1920" height="1080"/>']
    clips = []
    for i, r in enumerate(rows):
        if not r["path"]:
            continue
        rid = f"r{i + 1}"
        try:
            url = Path(r["path"]).as_uri()        # 跨平台 file:// URL（Windows 盘符也正确）
        except ValueError:
            url = "file://" + quote(str(r["path"]), safe="/")
        name = _xattr(r["name"])
        dur = r.get("duration")
        dur_attr = f' duration="{int(round(float(dur) * 2500))}/2500s"' if dur else ""
        res.append(f'<asset id="{rid}" name="{name}" src="{url}" start="0s"{dur_attr} hasVideo="1"/>')
        clips.append(f'<asset-clip ref="{rid}" name="{name}" offset="0s"{dur_attr}/>')
    from app import __version__ as _v
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        f'<!-- Generated by 觅影 v{_v} · 玩椰 YEEHX · yeehx.com -->\n<fcpxml version="1.9">\n'
        '  <resources>\n    ' + "\n    ".join(res) + "\n  </resources>\n"
        '  <library>\n    <event name="觅影导出">\n      ' + "\n      ".join(clips) +
        "\n    </event>\n  </library>\n</fcpxml>\n"
    )


def _log(ex: str, entry: dict):
    conn = db.connect()
    r = conn.execute("SELECT log_json FROM exports WHERE id=?", (ex,)).fetchone()
    log = db.jloads(r["log_json"], []) if r else []
    log.append(entry)
    db.write(lambda c: c.execute("UPDATE exports SET log_json=?, updated_at=? WHERE id=?", (db.jdumps(log), db.now(), ex)))


def register():
    tasks.register("export_run", _h_export, finalize=_finalize_export)
