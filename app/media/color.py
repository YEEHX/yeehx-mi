# 来源：phase0/yeehx_phase0/color.py  用途：色系算法提取（k-means 聚类主色，映射到固定色桶标签）
"""色系算法提取(方案 §7:色系由算法定,不靠模型)。

对派生图做颜色聚类取主色 → 按色相/亮度/对比映射到固定桶:
  冷暖:   暖色调 / 冷色调 / 中性
  影调:   高调 / 低调 / 高对比 / 柔和
  主色相: 青蓝 蓝绿 暖橙 金黄 墨绿 红 紫 灰 黑白
输出一个字符串列表,如 ["冷色调","青蓝"]。
"""
from __future__ import annotations
from pathlib import Path

import cv2
import numpy as np


def extract_color_tags(img_path: Path, k: int = 4) -> list[str]:
    img = cv2.imread(str(img_path), cv2.IMREAD_COLOR)
    if img is None:
        return []
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    small = cv2.resize(img, (160, 160), interpolation=cv2.INTER_AREA)
    flat = small.reshape(-1, 3).astype(np.float32)

    # k-means 取主色
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    try:
        _, labels, centers = cv2.kmeans(flat, k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    except cv2.error:
        return []
    counts = np.bincount(labels.flatten(), minlength=k)
    order = np.argsort(counts)[::-1]
    centers = centers[order]
    weights = counts[order] / counts.sum()

    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV).reshape(-1, 3).astype(np.float32)
    mean_v = float(hsv[:, 2].mean()) / 255.0      # 亮度均值 0~1
    mean_s = float(hsv[:, 1].mean()) / 255.0      # 饱和均值 0~1
    contrast = float(small.std()) / 128.0         # 粗略对比 0~~2

    tags: list[str] = []

    # ── 黑白 / 灰 ──
    if mean_s < 0.08:
        tags.append("黑白" if contrast > 0.35 else "灰")

    # ── 冷暖(用主色相加权) ──
    warm = cool = 0.0
    for c, w in zip(centers, weights):
        h = _hue_deg(c)
        if h is None:
            continue
        if h < 60 or h > 300:        # 红橙黄紫红
            warm += w
        elif 90 < h < 270:           # 绿青蓝
            cool += w
    if "黑白" not in tags and "灰" not in tags:
        if warm - cool > 0.15:
            tags.append("暖色调")
        elif cool - warm > 0.15:
            tags.append("冷色调")
        else:
            tags.append("中性")

    # ── 影调 ──
    if mean_v > 0.62 and contrast < 0.5:
        tags.append("高调")
    elif mean_v < 0.32:
        tags.append("低调")
    if contrast > 0.62:
        tags.append("高对比")
    elif contrast < 0.30:
        tags.append("柔和")

    # ── 主色相桶(取最大权重的有彩色主色) ──
    if mean_s >= 0.08:
        bucket = _dominant_hue_bucket(centers, weights)
        if bucket:
            tags.append(bucket)

    # 去重保序
    seen, uniq = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _hue_deg(rgb) -> float | None:
    r, g, b = [float(x) for x in rgb]
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 8:        # 近灰,无明确色相
        return None
    px = np.uint8([[[r, g, b]]])
    h = cv2.cvtColor(px, cv2.COLOR_RGB2HSV)[0, 0, 0]
    return float(h) * 2.0  # OpenCV H 是 0~179,转 0~360


def _dominant_hue_bucket(centers, weights) -> str | None:
    best, best_w = None, 0.0
    for c, w in zip(centers, weights):
        h = _hue_deg(c)
        if h is None:
            continue
        if w > best_w:
            best_w, best = w, h
    if best is None:
        return None
    h = best
    if h < 15 or h >= 345:
        return "红"
    if h < 45:
        return "暖橙"
    if h < 70:
        return "金黄"
    if h < 100:
        return "墨绿"
    if h < 160:
        return "蓝绿"
    if h < 200:
        return "青蓝"
    if h < 260:
        return "青蓝"
    if h < 290:
        return "紫"
    return "红"
