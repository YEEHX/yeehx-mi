"""缩略图 + LUT（单张当前图、文件夹优先、可重算）。

- 缩略图永远只留一张当前图：默认原片；选 LUT→从原素材重算套 LUT、覆盖；
  单张选"无 LUT"→排除文件夹 LUT、用原片重算覆盖。
- 视频存代表帧时间点（facts.rep_ts），换 LUT/重生成都用同一时间点，避免画面跳。
- 主色随之重算（套 LUT 后）。
- LUT 不参与扫描/定位/打标，只用于重生成缩略图。
"""
from __future__ import annotations
import os
import re
import tempfile
from pathlib import Path

from app.config import get_cfg
from app.media import frames, luts, color, metadata
from app.core import volumes, assets as assets_mod, inheritance
from app.core.files import RAW_EXT

NONE_LUT = "__none__"   # 单张"无 LUT" = 直出原片


def lut_filter_for(lut_name: str | None) -> str | None:
    """把 LUT 名解析成 ffmpeg 滤镜串。原片/无LUT/空 → None。"""
    if not lut_name or lut_name in ("原片", NONE_LUT, "无LUT"):
        return None
    cfg = get_cfg()
    p = cfg.lut_path(lut_name)
    if p and p.exists():
        return luts.ffmpeg_lut_filter(p, "cube")
    return None   # 缺 .cube → 不套（觅影不造假色，提示在 UI）


def generate(asset_id: str, lut_name: str | None = None) -> dict:
    """生成/重生成一条素材的当前缩略图（套指定 LUT；None=原片），并重算主色。"""
    cfg = get_cfg()
    a = assets_mod.get(asset_id)
    if not a:
        return {"ok": False, "error": "素材不存在"}
    src = volumes.abspath(a)
    if src is None or not src.exists():
        return {"ok": False, "error": "offline", "kind": a["kind"]}

    cfg.thumbs_dir.mkdir(parents=True, exist_ok=True)
    dest = cfg.thumbs_dir / f"{asset_id}.jpg"
    lf = lut_filter_for(lut_name)
    kind = a["kind"]
    facts = a.get("facts") or {}
    ok = False

    if kind in ("photo", "timelapse", "raw"):
        real_src = src
        tmp = None
        if kind == "raw" or src.suffix.lower() in RAW_EXT:
            fd, tmp_name = tempfile.mkstemp(suffix=".jpg")
            os.close(fd)   # mkstemp 的 fd 不关会泄漏，批量扫 RAW 必撞 fd 上限
            tmp = Path(tmp_name)
            # 先 exiftool 提内嵌预览；提不出再用 rawpy 解（都没有则保持原文件，走 ffmpeg 碰运气）
            if metadata.extract_embedded_preview(src, tmp) or frames.raw_to_jpeg(src, tmp):
                real_src = tmp
        if lf:
            ok = frames._grab(real_src, None, dest, cfg.thumb_max_px, lf)
            if not ok:
                # HEIC 等 ffmpeg 不认的格式：先用 PIL 转成 jpg 再套 LUT
                fd2, tmp2_name = tempfile.mkstemp(suffix=".jpg")
                os.close(fd2)
                tmp2 = Path(tmp2_name)
                if frames.pil_to_jpeg(real_src, tmp2):
                    ok = frames._grab(tmp2, None, dest, cfg.thumb_max_px, lf)
                try:
                    tmp2.unlink()
                except OSError:
                    pass
        else:
            outs, _ = frames.photo_derivative(real_src, cfg.thumbs_dir, asset_id, cfg)
            ok = bool(outs)
        if tmp and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    elif kind in ("video", "film", "red"):
        rep_ts = facts.get("rep_ts")
        if rep_ts is None:
            outs, info = frames.video_derivative(src, cfg.thumbs_dir, asset_id, cfg, lf)
            ok = bool(outs)
            dur = info.get("duration")
            fp = info.get("frame_position")
            patch = {}
            if dur:
                patch["duration"] = round(float(dur), 3)
            if dur and isinstance(fp, str) and fp.endswith("%"):
                try:
                    patch["rep_ts"] = round(dur * float(fp[:-1]) / 100, 3)
                except ValueError:
                    pass
            if patch:
                assets_mod.set_facts(asset_id, patch)
        else:
            ok = frames._grab(src, rep_ts, dest, cfg.thumb_max_px, lf)
    else:
        assets_mod.set_fields(asset_id, thumb_path="", thumb_lut="")
        return {"ok": False, "kind": kind, "error": "该类型无缩略图", "unsupported": True}

    if not ok or not dest.exists():
        if kind == "red":
            return {"ok": False, "error": "RED .R3D 抽帧失败：请确认 REDCINE-X PRO/REDline 可用",
                    "kind": kind, "unsupported": True}
        if kind == "raw" or src.suffix.lower() in RAW_EXT:
            return {"ok": False, "error": "RAW 解码失败：建议安装 exiftool（brew install exiftool）或 pip 装 rawpy", "kind": kind}
        return {"ok": False, "error": "缩略图生成失败", "kind": kind}

    # 主色（套 LUT 后重算）
    try:
        colors = color.extract_color_tags(dest)
    except Exception:
        colors = []
    assets_mod.set_facts(asset_id, {"color": colors})
    assets_mod.set_fields(asset_id, thumb_path=f"{asset_id}.jpg",
                          thumb_lut=("" if not lut_name or lut_name in ("原片", NONE_LUT) else lut_name))
    inheritance.recompute(asset_id)
    return {"ok": True, "thumb": f"{asset_id}.jpg", "kind": kind, "colors": colors}


# ── 自动 LUT 识别（只做实测可靠的两类；2026-06 用真实素材测过：
#    R3D=容器即证据；索尼=机内 capturegammaequation 字段，log/直出双向可判。
#    大疆没有任何可靠信号——元数据/文件名/colr/位深/直方图五条路全被实测证伪，不猜。）──
_SONY_GAMMA_RE = re.compile(rb'capturegammaequation"\s*value="([^"]{2,40})"')
_AUTOLUT_SCAN = 16 * 1024 * 1024


def detect_auto_lut(src: Path, kind: str) -> str:
    """容器级 log 识别。返回 LUT 名，或 ""=明确无/无法判定（都不套）。"""
    if kind == "red" or src.suffix.lower() == ".r3d":
        return "RED Log3G10"
    if src.suffix.lower() not in (".mp4", ".mov", ".mxf"):
        return ""
    try:
        size = src.stat().st_size
        with src.open("rb") as f:
            head = f.read(min(_AUTOLUT_SCAN, size))
            tail = b""
            if size > _AUTOLUT_SCAN * 2:
                f.seek(-_AUTOLUT_SCAN, 2)
                tail = f.read(_AUTOLUT_SCAN)
    except OSError:
        return ""
    m = _SONY_GAMMA_RE.search((head + tail).lower())
    if m and b"s-log3" in m.group(1):
        return "S-Log3"
    return ""   # rec709/s-cinetone 等＝明确直出；没字段＝不判，一律不套


def auto_lut(asset: dict) -> str | None:
    """自动 LUT（开关可关）。结果缓存进 facts，只算一次；卷离线不缓存下次再试。"""
    cfg = get_cfg()
    if not getattr(cfg, "auto_lut", True):
        return None
    facts = asset.get("facts") or {}
    if "auto_lut" in facts:
        return facts["auto_lut"] or None
    src = volumes.abspath(asset)
    if src is None or not src.exists():
        return None
    val = detect_auto_lut(src, asset.get("kind") or "")
    assets_mod.set_facts(asset["asset_id"], {"auto_lut": val})
    return val or None


def effective_lut(asset: dict) -> str | None:
    """这条素材当前生效的 LUT。优先级：单素材手动（含「直出」__none__）＞文件夹＞自动识别。
    手动设置永远压过自动——想要灰片缩略图，点「直出」即可。"""
    eff = inheritance.effective(asset)
    lut = eff.get("lut")
    if lut:
        return lut
    return auto_lut(asset)


def regenerate_effective(asset_id: str) -> dict:
    """按当前生效 LUT 重生成（文件夹设了 LUT、或单张例外后调用）。"""
    a = assets_mod.get(asset_id)
    if not a:
        return {"ok": False}
    return generate(asset_id, effective_lut(a))


def set_rep_frame(asset_id: str, ts: float) -> dict:
    """手动换帧：把视频代表帧时间点（秒）写回 facts.rep_ts，再按当前生效 LUT 重生成缩略图。
    缩略图、单张下载、批量 zip、导出图片都读同一个 rep_ts —— 换完这一帧，下游全部自动跟随。
    只对视频/成片/RED 生效（照片没有「帧」可换）。"""
    a = assets_mod.get(asset_id)
    if not a:
        return {"ok": False, "error": "素材不存在"}
    if a.get("kind") not in ("video", "film", "red"):
        return {"ok": False, "error": "只有视频可以换帧"}
    try:
        ts = max(0.0, round(float(ts), 3))
    except (TypeError, ValueError):
        return {"ok": False, "error": "时间点无效"}
    assets_mod.set_facts(asset_id, {"rep_ts": ts})   # generate 会重新读到这个 rep_ts，抽这一帧
    return generate(asset_id, effective_lut(a))


def remove_lut_preset(name: str) -> dict:
    """移除一个 LUT 预设：正在用它的文件夹回落"原片"、素材例外清掉（影响范围由调用方重生成缩略图）。"""
    cfg = get_cfg()
    cfg.remove_user_lut(name)
    from app import db
    affected_assets: list[str] = []

    def _w(c):
        nonlocal affected_assets
        for r in c.execute("SELECT folder_id FROM folders WHERE lut=?", (name,)).fetchall():
            c.execute("UPDATE folders SET lut=NULL, updated_at=? WHERE folder_id=?", (db.now(), r["folder_id"]))
            affected_assets += [x[0] for x in c.execute(
                "SELECT asset_id FROM assets WHERE folder_id=?", (r["folder_id"],)).fetchall()]
        for r in c.execute("SELECT asset_id FROM assets WHERE lut=?", (name,)).fetchall():
            c.execute("UPDATE assets SET lut=NULL, updated_at=? WHERE asset_id=?", (db.now(), r["asset_id"]))
            affected_assets.append(r["asset_id"])

    db.write(_w)
    return {"ok": True, "affected": sorted(set(affected_assets))}
