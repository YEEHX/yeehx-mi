# 来源：phase0/yeehx_phase0/metadata.py  用途：exiftool 读元数据（机型/镜头/GPS/时间/log探测）
"""exiftool 读元数据(机型/镜头/GPS/时间),并尽量探测 log 类型。

需要系统装了 exiftool:  brew install exiftool
若环境没有 exiftool,read_metadata 返回空 dict,不致命(管线退化为纯画面)。
"""
from __future__ import annotations
import json
import shutil
import subprocess
from pathlib import Path

HAS_EXIFTOOL = shutil.which("exiftool") is not None

# 可能标示 log/flat 画质的字段值关键词(不同厂家放的位置不一样,统一扫一遍值)
_LOG_HINTS = ["log", "s-log", "slog", "n-log", "nlog", "d-log", "dlog", "flat", "hlg", "cine"]


def read_metadata(path: Path) -> dict:
    """返回归一化后的元数据 dict。失败返回 {}。"""
    if not HAS_EXIFTOOL:
        return {}
    try:
        # -j JSON, -n 数值化(GPS 给小数), -G0 分组前缀, -api largefilesupport 给大视频
        proc = subprocess.run(
            ["exiftool", "-j", "-n", "-api", "largefilesupport=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0 and not proc.stdout:
            return {}
        arr = json.loads(proc.stdout or "[]")
        raw = arr[0] if arr else {}
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return {}

    def g(*keys):
        for k in keys:
            if k in raw and raw[k] not in (None, ""):
                return raw[k]
        return None

    make = g("Make") or ""
    model = g("Model") or g("CameraModelName") or ""
    lens = g("LensModel", "LensID", "Lens") or ""
    lat = g("GPSLatitude")
    lng = g("GPSLongitude")
    # exiftool -n 下 GPS 已是带符号小数;个别老文件给 Ref,这里只取已签名值
    dt = g("DateTimeOriginal", "CreateDate", "MediaCreateDate", "TrackCreateDate")

    # log 探测:扫所有字符串值找关键词(PictureProfile / ColorMode / PictureControl 等都覆盖)
    log_signal = None
    for k, v in raw.items():
        if isinstance(v, str):
            low = v.lower()
            for h in _LOG_HINTS:
                if h in low:
                    log_signal = f"{k}={v}"
                    break
        if log_signal:
            break

    return {
        "make": str(make).strip(),
        "model": str(model).strip(),
        "lens": str(lens).strip(),
        "lat": _to_float(lat),
        "lng": _to_float(lng),
        "datetime": str(dt).strip() if dt else None,
        "log_signal": log_signal,            # 元数据里直接出现的 log 线索(可能为 None)
        "duration": _to_float(g("Duration", "MediaDuration", "TrackDuration")),
        "is_drone": _is_drone(make, model),
        "_raw_keys": len(raw),
    }


def extract_embedded_preview(path: Path, dest: Path) -> bool:
    """用 exiftool 从 RAW 里抽内嵌大预览 JPG(免 rawpy)。成功写到 dest 返回 True。"""
    if not HAS_EXIFTOOL:
        return False
    for tag in ("-JpgFromRaw", "-PreviewImage", "-OtherImage", "-ThumbnailImage"):
        try:
            proc = subprocess.run(["exiftool", "-b", tag, str(path)],
                                  capture_output=True, timeout=60)
            if proc.returncode == 0 and proc.stdout and len(proc.stdout) > 2000:
                dest.write_bytes(proc.stdout)
                return True
        except (subprocess.SubprocessError, OSError):
            continue
    return False


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_drone(make: str, model: str) -> bool:
    hay = f"{make} {model}".lower()
    return any(k in hay for k in ["dji", "mavic", "phantom", "inspire", "air ", "mini", "fc"])


def camera_hint(meta: dict) -> str | None:
    """给模型的机位线索文本(只做提示,不强制)。"""
    if not meta:
        return None
    bits = []
    if meta.get("is_drone"):
        bits.append("可能是无人机航拍")
    if meta.get("model"):
        bits.append(f"机型 {meta['model']}")
    if meta.get("lens"):
        bits.append(f"镜头 {meta['lens']}")
    return "；".join(bits) if bits else None
