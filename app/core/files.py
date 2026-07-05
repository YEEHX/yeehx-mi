"""文件分类与指纹（纯逻辑，从 phase0 assets.py 移植精简）。

把一个文件夹里的文件归并成"素材(asset)"：
- 包(.fcpbundle/.aplibrary…)当整体登记为工程，不递归。
- RAW+JPG 同名 → 合并 1 张照片（分析 JPG）。
- 同前缀连号图 ≥ timelapse_min_frames → 合并 1 条延时，取中间帧。
- 视频每条 1 素材；"成片/交付"类文件夹标 film。
- .r3d(RED) 登记为素材；生图阶段先尝试 ffmpeg，失败时跳过不阻塞。
"""
from __future__ import annotations
import hashlib
import re
from pathlib import Path

RAW_EXT = {".arw", ".nef", ".dng", ".cr2", ".cr3", ".raf", ".orf", ".rw2", ".raw", ".pef", ".srw"}
JPG_EXT = {".jpg", ".jpeg"}
IMG_OTHER = {".png", ".tif", ".tiff", ".heic", ".heif", ".webp", ".gif", ".bmp"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mxf", ".mkv"}
RED_EXT = {".r3d"}
PKG_EXT = {".fcpbundle", ".aplibrary", ".photoslibrary", ".lrlibrary", ".lrdata"}
SKIP_EXT = {".docx", ".pdf", ".txt", ".xml", ".fcpxml", ".aae", ".lrcat", ".plist", ".ds_store",
            ".json", ".md", ".csv", ".cube", ".srt", ".log", ".ini", ".lnk"}
MEDIA_EXT = RAW_EXT | JPG_EXT | IMG_OTHER | VIDEO_EXT | RED_EXT
THUMBABLE_KINDS = {"photo", "video", "film", "timelapse", "raw", "red"}
MISSING_THUMB_BLOCKING_KINDS = {"photo", "video", "film", "timelapse", "raw"}

_DATE_RE = re.compile(r"^[\s\-_]*(?:\d{8}|\d{6}|\d{4}[-._]\d{1,2}[-._]\d{1,2})[\s\-_]*")
_PREFIX_RE = re.compile(r"^(.*?)(\d+)$")
_CJK_RE = re.compile(r"[一-鿿]+")
_FN_DATE_RE = re.compile(r"(?:^|[^0-9])(\d{8}|\d{6})(?:[^0-9]|$)")


def is_media(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_EXT or path.suffix.lower() in PKG_EXT


def filename_desc(stem: str) -> str | None:
    runs = _CJK_RE.findall(stem or "")
    if not runs:
        return None
    desc = "".join(runs)
    return desc if len(desc) >= 2 else None


def filename_date(stem: str) -> str | None:
    m = _FN_DATE_RE.search(stem or "")
    return m.group(1) if m else None


def clean_folder_name(name: str) -> str:
    s = _DATE_RE.sub("", name).strip(" -_")
    s = re.sub(r"^20\d{2}[\-_]?", "", s).strip(" -_")
    return s


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in PKG_EXT:
        return "package"
    if ext in RED_EXT:
        return "red"
    if ext in VIDEO_EXT:
        return "video"
    if ext in RAW_EXT:
        return "raw"
    if ext in JPG_EXT or ext in IMG_OTHER:
        return "photo"
    return "other"


def content_id_ex(path: Path, chunk: int = 8 * 1024 * 1024) -> tuple[str, str]:
    """blake3(头chunk+尾chunk+size)；缺 blake3 用 sha256。改名/移动/换盘据此认作同一素材。
    返回 (指纹, 错误原因)——2-2：权限/占用算不出时不再静默，错误原因交给调用方留痕。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return "", f"无法读取文件信息: {exc.strerror or exc}"
    try:
        import blake3
        h = blake3.blake3()
    except ImportError:
        h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            h.update(f.read(chunk))
            if size > chunk * 2:
                f.seek(-chunk, 2)
                h.update(f.read(chunk))
        h.update(str(size).encode())
    except OSError as exc:
        return "", f"读取失败: {exc.strerror or exc}"
    return h.hexdigest()[:24], ""


def content_id(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    return content_id_ex(path, chunk)[0]


def _alpha_prefix(stem: str) -> str | None:
    m = _PREFIX_RE.match(stem)
    return m.group(1) if m else None


def classify(folder: Path, files: list[Path], cfg) -> list[dict]:
    """把一个文件夹的文件列表归并成素材描述符列表。
    每个描述符：{primary: Path, kind, files: [Path], raw_source: Path|None, extra: {}}。
    不读画面、不算内容指纹（轻），那些留给生图/打标阶段。
    """
    images_raw: dict[str, Path] = {}
    images_jpg: dict[str, Path] = {}
    images_other: list[Path] = []
    videos: list[Path] = []
    out: list[dict] = []

    is_film_folder = any(
        k.lower() in str(folder).lower() for k in (cfg.film_folder_keywords or [])
    )

    for f in files:
        ext = f.suffix.lower()
        if ext in PKG_EXT:
            out.append({"primary": f, "kind": "package", "files": [f], "raw_source": None,
                        "extra": {"note": "工程包，登记占位不递归"}})
        elif ext in RED_EXT:
            out.append({"primary": f, "kind": "red", "files": [f], "raw_source": None,
                        "extra": {"note": "RED .r3d 需 RED SDK 解码"}})
        elif ext in VIDEO_EXT:
            videos.append(f)
        elif ext in RAW_EXT:
            images_raw[f.stem] = f
        elif ext in JPG_EXT:
            images_jpg[f.stem] = f
        elif ext in IMG_OTHER:
            images_other.append(f)
        # SKIP_EXT 及未知：忽略

    for v in videos:
        out.append({"primary": v, "kind": "film" if is_film_folder else "video",
                    "files": [v], "raw_source": None, "extra": {}})

    paired_jpg: set[str] = set()
    photo_files: list[tuple[Path, Path | None]] = []
    for stem, raw in images_raw.items():
        if stem in images_jpg:
            photo_files.append((images_jpg[stem], raw))
            paired_jpg.add(stem)
        else:
            photo_files.append((raw, raw))
    for stem, jpg in images_jpg.items():
        if stem not in paired_jpg:
            photo_files.append((jpg, None))
    for other in images_other:
        photo_files.append((other, None))

    groups: dict[str, list[tuple[Path, Path | None]]] = {}
    singles: list[tuple[Path, Path | None]] = []
    for primary, raw in photo_files:
        pref = _alpha_prefix(primary.stem)
        if pref is not None:
            groups.setdefault(pref, []).append((primary, raw))
        else:
            singles.append((primary, raw))

    for pref, items in groups.items():
        if len(items) >= cfg.timelapse_min_frames:
            items_sorted = sorted(items, key=lambda x: x[0].name)
            mid = items_sorted[len(items_sorted) // 2]
            out.append({"primary": mid[0], "kind": "timelapse",
                        "files": [i[0] for i in items_sorted], "raw_source": mid[1],
                        "extra": {"frame_count": len(items_sorted),
                                  "first": items_sorted[0][0].name,
                                  "last": items_sorted[-1][0].name}})
        else:
            singles.extend(items)

    for primary, raw in singles:
        out.append({"primary": primary, "kind": "photo", "files": [primary],
                    "raw_source": raw, "extra": {}})
    return out


def folder_fingerprint(files: list[Path]) -> str:
    """文件夹指纹：文件数 + 大小/时间聚合。变了才重处理。"""
    h = hashlib.sha1()
    h.update(str(len(files)).encode())
    total = 0
    latest = 0.0
    for f in sorted(files, key=lambda p: p.name):
        try:
            st = f.stat()
            total += st.st_size
            latest = max(latest, st.st_mtime)
            h.update(f"{f.name}|{st.st_size}".encode())
        except OSError:
            continue
    h.update(f"{total}|{int(latest)}".encode())
    return h.hexdigest()[:16]
