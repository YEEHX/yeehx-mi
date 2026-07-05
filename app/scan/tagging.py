"""AI tagging for the single tag model.

Tagging uses existing thumbnails only. The model receives image frames plus
path, filename, date, source and the existing tag library as context. Existing
tag hits are applied automatically; useful unknown terms go to review.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from app.config import get_cfg
from app.core import folders, assets as assets_mod, tags as tags_mod, candidates, inheritance
from app.core.files import clean_folder_name, filename_date, filename_desc
from app.ai import vision, source_rules
from app.media import metadata
from app import tasks

AI_CAT = {"主体": "内容", "镜头": "拍法", "氛围": "氛围", "风格": "氛围", "时间": "天气"}
EXISTING_TAG_MIN_CONF = 0.55
NEW_CANDIDATE_MIN_CONF = 0.82
LANDMARK_CANDIDATE_MIN_CONF = 0.62
OPEN_CANDIDATE_MIN_HITS = 3
OPEN_PLACE_CANDIDATE_MIN_HITS = 2
_NOISY_TERM = re.compile(r"^(?:\d{4,8}|[A-Z]?\d{2,}[A-Z0-9_-]*|IMG_\d+|DJI_\d+|C\d{4,})$", re.I)


def _words(obj: dict) -> list[tuple[str, str]]:
    out = []
    for field, cat in AI_CAT.items():
        for name in (obj.get(field) or []):
            out.append((str(name).strip(), cat))
    for name in (obj.get("地标") or []):
        out.append((str(name).strip(), "地点"))
    return [(n, c) for n, c in out if n]


def _tag_library() -> dict[str, list[str]]:
    """标签库（按类目分组）。1-5 起走 tags.library_grouped() 模块级缓存——
    批量打标 100 条不再全量读 100 次表，AI 搜索同享。"""
    return tags_mod.library_grouped()


def _context(asset: dict, src: Path, folder: dict | None, meta: dict, det: dict) -> dict:
    rel_parts = [p for p in asset["rel_path"].split("/")[:-1] if p]
    cleaned = [clean_folder_name(p) for p in rel_parts]
    cleaned = [p for p in cleaned if p]
    stem = Path(asset["name"]).stem
    return {
        "kind": asset.get("kind"),
        "folder_path": cleaned[-10:],
        "folder_name": clean_folder_name((folder or {}).get("name") or "") if folder else "",
        "filename": asset["name"],
        "filename_text": filename_desc(stem),
        "filename_date": filename_date(stem),
        "source_hint": {k: det.get(k) for k in ("source_kind", "source_name", "source_category", "color_profile", "evidence")},
        "camera_hint": metadata.camera_hint(meta) if meta else None,
    }


def _source_names(det: dict, ai_gen: bool) -> list[str]:
    out = []
    mapping = {"大疆无人机": "DJI", "索尼相机": "索尼相机", "尼康相机": "尼康相机"}
    if det.get("source_kind") == "ai":
        if det.get("source_name"):
            out.append(det["source_name"])
        out.append("AI")
    else:
        source_name = det.get("source_name")
        if source_name in mapping:
            out.append(mapping[source_name])
        if det.get("source_category") in ("camera", "drone", "phone") and source_name not in (None, "其他"):
            out.append("实拍")
    if ai_gen:
        out.append("AI")
    seen, res = set(), []
    for name in out:
        if name and name not in seen:
            seen.add(name)
            res.append(name)
    return res


def _add_existing(asset_id: str, name: str, *, source: str = "ai"):
    tag = tags_mod.get_by_name(name)
    if tag:
        assets_mod.add_own(asset_id, tag["id"], source=source)
        return True
    return False


def _mapped_category(category: str | None) -> str:
    return candidates.CAT_MAP.get(category or "", category or "未分类")


def _maybe_candidate(asset_id: str, name: str, category: str, confidence: float, reason: str = "",
                     *, min_hits: int = 1, min_confidence: float = NEW_CANDIDATE_MIN_CONF):
    name = (name or "").strip()
    if not name or len(name) > 18 or _NOISY_TERM.match(name):
        return
    if tags_mod.get_by_name(name):
        _add_existing(asset_id, name, source="ai_candidate_hit")
        return
    if confidence >= min_confidence:
        candidates.add(name, category, asset_id=asset_id, kind="ai",
                       reason=reason, confidence=confidence, min_hits=min_hits)


def _ai_images(asset: dict) -> list[Path]:
    cfg = get_cfg()
    thumb = cfg.thumbs_dir / (asset.get("thumb_path") or "")
    return [thumb] if thumb.exists() else []


def _run_ai(asset: dict):
    from app.core import volumes

    cfg = get_cfg()
    src = volumes.abspath(asset)
    if src is None or not src.exists():
        raise RuntimeError("卷离线")
    images = _ai_images(asset)
    if not images:
        raise RuntimeError("无可用缩略图")
    meta = metadata.read_metadata(src)
    det = source_rules.detect(src, meta, cfg)
    folder = folders.get(asset["folder_id"]) if asset.get("folder_id") else None
    hint = clean_folder_name(folder["name"]) if folder else None
    res = vision.tag_images(
        images,
        hint,
        metadata.camera_hint(meta) if meta else None,
        cfg,
        context=_context(asset, src, folder, meta, det),
        tag_library=_tag_library(),
    )
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "AI 调用失败")
    return res.get("json") or {}, det


def _description(obj: dict) -> str:
    desc = str(obj.get("描述") or "").strip()
    keywords = [str(x).strip() for x in (obj.get("keywords") or []) if str(x).strip()]
    if keywords:
        tail = "关键词：" + "、".join(list(dict.fromkeys(keywords))[:10])
        return f"{desc}\n{tail}" if desc else tail
    return desc


def _apply_ai_result(asset: dict, obj: dict, det: dict):
    asset_id = asset["asset_id"]
    desc = _description(obj)
    if desc:
        assets_mod.set_description(asset_id, desc_ai=desc)

    # 固定视觉词表在启动时同步进标签库；缺失/禁用时不再把普通视觉词转成开放候选。
    # 只有明确识别出的地标仍允许进待复核。
    conf_default = float(obj.get("confidence") or 0.7)
    for name, cat in _words(obj):
        if _add_existing(asset_id, name):
            continue
        if cat == "地点":
            _maybe_candidate(asset_id, name, cat, conf_default, "AI 认出的地标，标签库还没有",
                             min_confidence=LANDMARK_CANDIDATE_MIN_CONF)

    for item in obj.get("existing_tags") or []:
        name = item.get("name") if isinstance(item, dict) else str(item)
        try:
            conf = float(item.get("confidence", 1.0)) if isinstance(item, dict) else 1.0
        except (TypeError, ValueError):
            conf = 1.0
        if conf >= EXISTING_TAG_MIN_CONF:
            _add_existing(asset_id, name, source="ai_hit")

    for item in obj.get("new_candidates") or []:
        if not isinstance(item, dict):
            continue
        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        category = item.get("category") or "未分类"
        mapped = _mapped_category(category)
        if mapped in tags_mod.PLACE_CATS:
            _maybe_candidate(asset_id, item.get("name") or "", mapped, conf, item.get("reason") or "",
                             min_hits=OPEN_PLACE_CANDIDATE_MIN_HITS,
                             min_confidence=LANDMARK_CANDIDATE_MIN_CONF)
        else:
            _maybe_candidate(asset_id, item.get("name") or "", mapped, conf, item.get("reason") or "",
                             min_hits=OPEN_CANDIDATE_MIN_HITS)

    for source_name in _source_names(det, bool(obj.get("ai生成"))):
        _add_existing(asset_id, source_name, source="source")

    eff = inheritance.effective(assets_mod.get(asset_id) or asset)
    has_place = any((tags_mod.get(tid) or {}).get("category") in tags_mod.PLACE_CATS for tid in eff["tags"])
    place = obj.get("place_guess")
    if not has_place and isinstance(place, dict) and place.get("name"):
        try:
            conf = float(place.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0
        if conf >= get_cfg().guess_conf_min:
            _add_existing(asset_id, place["name"], source="ai_place")


_NO_RETRY_ERRORS = ("卷离线", "无可用缩略图")
# 临时性错误特征（2-3）：超时/连接/限流/服务端 5xx——指数退避后大概率自己好
_TRANSIENT_MARKERS = ("timeout", "timed out", "超时", "连不上", "connection", "connect",
                      "refused", "reset", "429", "500", "502", "503", "504",
                      "busy", "overload", "过载", "too many")
_RETRY_DELAYS = (2, 4, 8)


def _is_transient(msg: str) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in _TRANSIENT_MARKERS)


def _h_fine_tag(task, asset_id):
    """打标重试分级（2-3）：
    - 永久性（卷离线/无缩略图）→ 立即停，不浪费模型调用；
    - 临时性（超时/连接拒绝/429/5xx）→ 指数退避 2s/4s/8s 最多重试 3 次；
    - 其他（如模型偶发吐非法 JSON）→ 与旧行为一致再试 1 次。
    最终失败置 status=failed，可在任务面板/重打入口批量重来。"""
    asset = assets_mod.get(asset_id)
    if not asset or asset.get("locked"):
        return
    last_error = None
    transient_used = generic_used = 0
    while True:
        try:
            obj, det = _run_ai(asset)
            _apply_ai_result(asset, obj, det)
            assets_mod.set_fields(asset_id, status="tagged")
            inheritance.recompute(asset_id)
            return
        except RuntimeError as exc:
            last_error = exc
            msg = str(exc)
            if msg in _NO_RETRY_ERRORS:
                break   # 卷离线/没缩略图：重试没意义，省下模型调用
            if _is_transient(msg) and transient_used < len(_RETRY_DELAYS):
                time.sleep(_RETRY_DELAYS[transient_used])
                transient_used += 1
                continue
            if not _is_transient(msg) and generic_used < 1:
                generic_used += 1
                continue
            break
    assets_mod.set_fields(asset_id, status="failed")
    raise last_error or RuntimeError("AI 调用失败")


def register():
    tasks.register("fine_tag", _h_fine_tag)
