"""LUT → ffmpeg 滤镜串。觅影里 LUT 是手动选的（决策引擎已随 phase0 移除）。"""
from __future__ import annotations
from pathlib import Path


def ffmpeg_lut_filter(lut_path: Path | None, mode: str) -> str | None:
    """生成 ffmpeg -vf 用的 LUT 滤镜串。
    cube  → lut3d='路径'
    fallback → 近似:轻微反 log 的对比/饱和/gamma 拉伸(明确不准,仅占位)
    none  → None
    """
    if mode == "cube" and lut_path:
        p = str(lut_path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        return f"lut3d='{p}'"
    if mode == "fallback":
        # log 普遍低对比低饱和发灰;近似还原:提 gamma+对比、加饱和。非真实色彩科学。
        return "curves=master='0/0 0.25/0.18 0.5/0.5 0.75/0.82 1/1',eq=contrast=1.18:saturation=1.35:gamma=0.95"
    return None
