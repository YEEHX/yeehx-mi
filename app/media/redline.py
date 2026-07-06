"""REDline bridge for RED .R3D frame extraction.

This module keeps RED-specific decoding outside the normal ffmpeg path.
REDline renders an ungraded RWG/Log3G10 TIFF; existing LUT handling then
continues through ffmpeg/lut3d in frames.py.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

_REDLINE: str | None = None


def redline_exe() -> str | None:
    """Return the REDline executable path, if installed."""
    global _REDLINE
    if _REDLINE and Path(_REDLINE).exists():
        return _REDLINE

    env = os.environ.get("YEEHX_REDLINE")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env).expanduser())

    found = shutil.which("REDline")
    if found:
        candidates.append(Path(found))

    candidates.extend([
        Path("/Applications/REDCINE-X Professional/REDCINE-X PRO.app/Contents/MacOS/REDline"),
        Path("/Applications/REDCINE-X PRO.app/Contents/MacOS/REDline"),
        Path("/Applications/REDCINE-X Professional/RED PLAYER.app/Contents/MacOS/REDline"),
    ])
    # Windows 默认安装位置（shutil.which 只找 PATH，REDCINE-X 安装器不进 PATH）
    for pf in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if pf:
            candidates.extend([
                Path(pf) / "REDCINE-X PRO 64-bit" / "REDline.exe",
                Path(pf) / "RED" / "REDCINE-X PRO" / "REDline.exe",
                Path(pf) / "REDCINE-X PRO" / "REDline.exe",
            ])

    for p in candidates:
        if p.exists() and os.access(p, os.X_OK):
            _REDLINE = str(p)
            return _REDLINE
    return None


def available() -> bool:
    return redline_exe() is not None


@lru_cache(maxsize=512)
def probe(path_str: str) -> dict:
    """Read lightweight clip metadata with REDline --printMeta."""
    exe = redline_exe()
    if not exe:
        return {"error": "未找到 REDline，请安装 REDCINE-X PRO 或设置 YEEHX_REDLINE"}
    path = Path(path_str)
    try:
        proc = subprocess.run(
            [exe, "--i", str(path), "--printMeta", "1"],
            capture_output=True, text=True, timeout=90,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {"error": f"REDline 元数据读取失败: {exc}"}

    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    meta: dict = {"raw": text, "returncode": proc.returncode}
    fields = {
        "fps": r"(?m)^FPS:\s*([0-9.]+)",
        "record_fps": r"(?m)^Record FPS:\s*([0-9.]+)",
        "total_frames": r"(?m)^Total Frames:\s*(\d+)",
        "clip_in": r"(?m)^Clip In:\s*(\d+)",
        "clip_out": r"(?m)^Clip Out:\s*(\d+)",
        "width": r"(?m)^Frame Width:\s*(\d+)",
        "height": r"(?m)^Frame Height:\s*(\d+)",
    }
    for key, pat in fields.items():
        m = re.search(pat, text)
        if not m:
            continue
        val = m.group(1)
        meta[key] = float(val) if "." in val else int(val)

    fps = _num(meta.get("fps")) or _num(meta.get("record_fps"))
    total = _num(meta.get("total_frames"))
    if fps and total:
        meta["duration"] = float(total) / float(fps)
    if proc.returncode != 0 and not (_num(meta.get("fps")) and _num(meta.get("total_frames"))):
        meta["error"] = "REDline 元数据读取返回非零状态"
    return meta


def duration(path: Path) -> float | None:
    dur = probe(str(path)).get("duration")
    return float(dur) if isinstance(dur, (int, float)) and dur > 0 else None


def frame_index(path: Path, ts: float | None) -> int:
    meta = probe(str(path))
    fps = _num(meta.get("fps")) or _num(meta.get("record_fps"))
    total = _num(meta.get("total_frames"))
    clip_in = int(_num(meta.get("clip_in")) or 0)
    if ts is None or ts <= 0 or not fps:
        frame = clip_in
    else:
        frame = clip_in + int(round(float(ts) * float(fps)))
    if total:
        frame = min(frame, clip_in + int(total) - 1)
    return max(clip_in, frame)


def extract_log_tiff(
    path: Path,
    dest: Path,
    ts: float | None = None,
    max_px: int | None = None,
    *,
    fullres: bool = False,
    timeout: int = 300,
) -> tuple[bool, dict]:
    """Render one RWG/Log3G10 frame to a TIFF at dest."""
    exe = redline_exe()
    if not exe:
        return False, {"error": "未找到 REDline，请安装 REDCINE-X PRO 或设置 YEEHX_REDLINE"}

    frame = frame_index(path, ts)
    res = _render_res(path, max_px=max_px, fullres=fullres)
    dest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        cmd = [
            exe,
            "--i", str(path),
            "--format", "1",              # TIFF
            "--outDir", str(tmpdir),
            "--o", "yeehx_redline",
            "--start", str(frame),
            "--frameCount", "1",
            "--res", str(res),
            "--primaryDev",               # ungraded RWG/Log3G10
            "--useRMD", "2",              # color defaults only when RMD exists
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, {"error": "REDline 抽帧超时", "frame": frame, "res": res}
        except (subprocess.SubprocessError, OSError) as exc:
            return False, {"error": f"REDline 抽帧失败: {exc}", "frame": frame, "res": res}

        outs = sorted(
            [p for p in tmpdir.rglob("*") if p.suffix.lower() in (".tif", ".tiff")],
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
        if proc.returncode != 0 or not outs:
            msg = _tail((proc.stdout or "") + "\n" + (proc.stderr or ""))
            return False, {"error": "REDline 没有输出帧", "frame": frame, "res": res, "log": msg}
        shutil.copy2(outs[0], dest)
    return True, {"frame": frame, "res": res, "meta": probe(str(path))}


def _render_res(path: Path, max_px: int | None, fullres: bool) -> int:
    if fullres or not max_px:
        return 1
    meta = probe(str(path))
    longest = max(int(_num(meta.get("width")) or 0), int(_num(meta.get("height")) or 0))
    if longest <= 0:
        return 4
    if max_px <= longest / 8:
        return 8
    if max_px <= longest / 4:
        return 4
    if max_px <= longest / 2:
        return 3
    return 1


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _tail(text: str, n: int = 1600) -> str:
    text = text.strip()
    return text[-n:] if len(text) > n else text
