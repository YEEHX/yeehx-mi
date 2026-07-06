# 来源：phase0/yeehx_phase0/frames.py  用途：抽帧 + 套 LUT + 缩到派生图（视频/照片均支持）
"""抽帧 + 套 LUT + 缩到派生图。解码只在这里发生一次,AI 之后只用派生图。

ffmpeg 解析顺序:系统 ffmpeg(brew) → 没有则用 pip 装的 imageio-ffmpeg 自带的二进制。
所以用户**不用单独装 ffmpeg**,pip install -r requirements.txt 就够。
时长用 ffmpeg 自己解析(不依赖 ffprobe)。
"""
from __future__ import annotations
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

from app.media import redline

_FFMPEG: str | None = None


# ── cv2 中文路径安全 I/O ──────────────────────────────────────────────────
# Windows 上 cv2.imread/imwrite 走 C 层 ANSI fopen：中文/生僻字/emoji 路径直接读写失败
# （用户解压到 D:\觅影\、素材盘全中文目录是常态）。统一改为 Python 读写字节 +
# imdecode/imencode——两平台同一条代码路径，mac 行为不变。业务代码禁止直接调 cv2.imread/imwrite。
def imread_u(path: Path | str, flags: int = cv2.IMREAD_COLOR):
    """cv2.imread 的跨平台安全版。失败返回 None（与 cv2.imread 一致）。"""
    try:
        data = np.frombuffer(Path(path).read_bytes(), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite_u(path: Path | str, img, params: list | None = None) -> bool:
    """cv2.imwrite 的跨平台安全版。按扩展名编码后用 Python 写字节。"""
    ext = Path(path).suffix.lower() or ".jpg"
    try:
        ok, buf = cv2.imencode(ext, img, params or [])
    except cv2.error:
        return False
    if not ok:
        return False
    try:
        Path(path).write_bytes(buf.tobytes())
        return True
    except OSError:
        return False


def ffmpeg_exe() -> str:
    """返回可用的 ffmpeg 路径:系统优先,否则 imageio-ffmpeg 自带。"""
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    sys_ff = shutil.which("ffmpeg")
    if sys_ff:
        _FFMPEG = sys_ff
        return _FFMPEG
    try:
        import imageio_ffmpeg
        _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        _FFMPEG = "ffmpeg"   # 兜底:寄望于 PATH
    return _FFMPEG


def video_duration(path: Path) -> float | None:
    """用 ffmpeg 的 stderr 解析时长(秒)。不依赖 ffprobe。"""
    if path.suffix.lower() == ".r3d":
        dur = redline.duration(path)
        if dur:
            return dur
    try:
        proc = subprocess.run([ffmpeg_exe(), "-i", str(path)],
                              capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", proc.stderr)
    if m:
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    return None


def _vf(max_px: int | None, lut_filter: str | None) -> str | None:
    chain = []
    if lut_filter:
        chain.append(lut_filter)
    if max_px:
        chain.append(f"scale='if(gt(iw,ih),min({max_px},iw),-2)':'if(gt(iw,ih),-2,min({max_px},ih))'")
    return ",".join(chain) if chain else None


def _grab_ffmpeg(path: Path, ts: float | None, dest: Path, max_px: int | None, lut_filter: str | None) -> bool:
    cmd = [ffmpeg_exe(), "-y", "-v", "error"]
    if ts is not None and ts > 0:
        cmd += ["-ss", f"{ts:.3f}"]
    cmd += ["-i", str(path), "-frames:v", "1"]
    vf = _vf(max_px, lut_filter)
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-q:v", "3", str(dest)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        return proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    except (subprocess.SubprocessError, OSError):
        return False


def _grab_redline(path: Path, ts: float | None, dest: Path, max_px: int | None, lut_filter: str | None) -> bool:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "redline_log.tif"
        ok, _ = redline.extract_log_tiff(path, tmp, ts=ts, max_px=max_px, fullres=(max_px is None))
        if not ok:
            return False
        return _grab_ffmpeg(tmp, None, dest, max_px, lut_filter)


def _grab(path: Path, ts: float | None, dest: Path, max_px: int, lut_filter: str | None) -> bool:
    if path.suffix.lower() == ".r3d":
        return _grab_redline(path, ts, dest, max_px, lut_filter)
    return _grab_ffmpeg(path, ts, dest, max_px, lut_filter)


def _sharpness(img_path: Path) -> float:
    img = imread_u(img_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return -1.0
    return float(cv2.Laplacian(img, cv2.CV_64F).var())


def video_derivative(path: Path, out_dir: Path, asset_id: str, cfg,
                     lut_filter: str | None) -> tuple[list[Path], dict]:
    """单条视频 → 1 张最清晰派生图。返回 ([派生图], info)。"""
    dur = video_duration(path)
    out_dir.mkdir(parents=True, exist_ok=True)
    cands = []
    with tempfile.TemporaryDirectory() as td:
        positions = cfg.sharp_samples if dur else [None]
        for i, p in enumerate(positions):
            ts = (dur * p) if (dur and p is not None) else None
            tmp = Path(td) / f"c{i}.jpg"
            if _grab(path, ts, tmp, cfg.max_px, lut_filter):
                cands.append((tmp, _sharpness(tmp), ts))
        if not cands:
            err = "抽帧失败(REDline 无法解码或未安装)" if path.suffix.lower() == ".r3d" else "抽帧失败(ffmpeg 无法解码?)"
            return ([], {"error": err, "duration": dur})
        best = max(cands, key=lambda c: c[1])
        final = out_dir / f"{asset_id}.jpg"
        final.write_bytes(best[0].read_bytes())
    return ([final], {"duration": dur, "sharpness": round(best[1], 1),
                      "frame_position": f"{(best[2]/dur*100):.0f}%" if (dur and best[2]) else "auto",
                      "n_candidates": len(cands)})


def pil_imread(path: Path):
    """PIL 读图 → BGR ndarray；支持 HEIC（装了 pillow-heif 时）。失败返回 None。"""
    try:
        import numpy as np
        from PIL import Image
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        with Image.open(path) as im:
            return cv2.cvtColor(np.array(im.convert("RGB")), cv2.COLOR_RGB2BGR)
    except Exception:
        return None


def pil_to_jpeg(path: Path, dest: Path, max_px: int | None = None) -> bool:
    """PIL 解码后写 JPEG（HEIC → ffmpeg 可用中转）。"""
    img = pil_imread(path)
    if img is None:
        return False
    if max_px:
        h, w = img.shape[:2]
        scale = max_px / max(h, w)
        if scale < 1:
            img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return imwrite_u(dest, img, [cv2.IMWRITE_JPEG_QUALITY, 92])


def raw_to_jpeg(src: Path, dest: Path) -> bool:
    """rawpy 解 RAW → JPEG（exiftool 提不出内嵌预览时的兜底）。没装 rawpy 返回 False。
    注意：rawpy.imread(str) 在 Windows 上也是 ANSI fopen（中文路径炸），传文件对象。"""
    try:
        import rawpy
        with open(src, "rb") as fh:
            with rawpy.imread(fh) as raw:
                rgb = raw.postprocess(use_camera_wb=True, output_bps=8)
        img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        return imwrite_u(dest, img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    except Exception:
        return False


def photo_derivative(src_img: Path, out_dir: Path, asset_id: str, cfg) -> tuple[list[Path], dict]:
    """照片/已抽预览 → 缩放成派生图。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{asset_id}.jpg"
    img = imread_u(src_img, cv2.IMREAD_COLOR)
    if img is None:
        img = pil_imread(src_img)   # HEIC/iPhone 照片 cv2 解不了 → PIL(+pillow-heif) 兜底
    if img is None:
        if _grab(src_img, None, dest, cfg.max_px, None):
            return ([dest], {})
        return ([], {"error": "图片无法解码"})
    h, w = img.shape[:2]
    scale = cfg.max_px / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    imwrite_u(dest, img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return ([dest], {"src_px": f"{w}x{h}"})


def extract_fullres(path: Path, kind: str, lut_filter: str | None, dest: Path, ts: float | None = None) -> bool:
    """抽**原分辨率**代表帧(视频中点)或原图(照片),套 LUT 还原灰片,写到 dest(JPEG高质量)。
    供"下载底图"按需生成。返回成功与否。"""
    if kind == "red" or path.suffix.lower() == ".r3d":
        if ts is None:
            dur = video_duration(path)
            ts = dur * 0.5 if dur else None
        return _grab_redline(path, ts, dest, None, lut_filter)

    cmd = [ffmpeg_exe(), "-y", "-v", "error"]
    if kind in ("video", "film"):
        if ts is not None:
            cmd += ["-ss", f"{float(ts):.3f}"]
        else:
            dur = video_duration(path)
            if dur:
                cmd += ["-ss", f"{dur * 0.5:.3f}"]
    cmd += ["-i", str(path), "-frames:v", "1"]
    if lut_filter:
        cmd += ["-vf", lut_filter]
    cmd += ["-q:v", "2", str(dest)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=180)
        return proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    except (subprocess.SubprocessError, OSError):
        return False


