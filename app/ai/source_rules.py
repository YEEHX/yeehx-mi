# 来源：phase0/yeehx_phase0/source_rules.py  用途：统一判断素材来源、设备/AI平台、色彩配置和 LUT
"""统一判断素材来源、设备/AI平台、色彩配置和 LUT。

所有"这是什么设备/平台、该不该套哪个 LUT"的逻辑都集中在这里。
其它模块只读取本模块输出,不要各自散落猜测。
"""
from __future__ import annotations

import json
import re
from pathlib import Path


AI_PLATFORM_PATTERNS = [
    ("seedance2.0", [r"seedance\s*2(?:\.0)?", "seedance2.0", "seedance_2"]),
    ("seedance", ["seedance"]),
    ("banana", ["banana"]),
    ("即梦", ["即梦", "jimeng"]),
    ("可灵", ["可灵", "kling"]),
    ("Runway", ["runway"]),
    ("Sora", ["sora"]),
    ("Midjourney", ["midjourney", "mj_"]),
    ("ComfyUI", ["comfyui"]),
    ("Stable Diffusion", ["stable diffusion", "sdxl"]),
    ("海螺", ["海螺", "hailuo"]),
    ("Pika", ["pika"]),
    ("Luma", ["luma"]),
]

CAMERA_RULES = [
    {
        "name": "大疆无人机",
        "category": "drone",
        "needles": ["dji", "mavic", "phantom", "inspire", "avata", "hasselblad", "l3d"],
        "filename": [r"^DJI_", r"_D_HAR", r"_D_"],
        "profile": None,
    },
    {
        "name": "大疆Pocket",
        "category": "camera",
        "needles": ["osmo", "pocket"],
        "filename": [],
        "profile": None,
    },
    {
        "name": "索尼相机",
        "category": "camera",
        "needles": ["sony", "ilce", "fx3", "fx30", "fx6", "zv-e", "a7", "a1", "a9"],
        "filename": [r"(^|_)C\d{4,}"],
        "profile": None,
    },
    {
        "name": "尼康相机",
        "category": "camera",
        "needles": ["nikon", "nikon z", "z 6", "z 8", "z 9"],
        "filename": [],
        "profile": "N-Log",
    },
    {"name": "iPhone", "category": "phone", "needles": ["apple", "iphone"], "filename": [], "profile": None},
    {"name": "GoPro", "category": "camera", "needles": ["gopro", "hero"], "filename": [], "profile": None},
    {"name": "佳能相机", "category": "camera", "needles": ["canon", "eos"], "filename": [], "profile": None},
    {"name": "富士相机", "category": "camera", "needles": ["fujifilm", "x-"], "filename": [], "profile": None},
    {"name": "松下相机", "category": "camera", "needles": ["panasonic", "lumix", "dc-", "dmc-"], "filename": [], "profile": None},
]

PROFILE_FILENAME_PATTERNS = [
    ("D-Log", [r"_D_HAR", r"_D_LOG", r"D-?LOG"]),
    ("S-Log3", [r"S-?LOG3", r"SLOG3"]),
    ("HLG", [r"(^|_)HLG(_|$)"]),
]


def detect(path: Path, meta: dict | None, cfg) -> dict:
    """返回统一来源判断。人工规则预留在 out/source_rules.json,当前支持 path_contains。"""
    meta = meta or {}
    path = Path(path)
    hay_path = str(path).lower()
    hay_meta = f"{meta.get('make') or ''} {meta.get('model') or ''} {meta.get('lens') or ''}".lower()
    out = {
        "source_kind": "real",
        "source_name": None,
        "source_category": None,
        "source_confidence": "low",
        "color_profile": None,
        "lut_id": None,
        "evidence": [],
        "rule_id": None,
        "_path_name": path.name,
    }

    manual = _match_manual_rule(path, cfg)
    if manual:
        out.update({k: v for k, v in manual.items() if k in out and v is not None})
        out["rule_id"] = manual.get("id")
        out["source_confidence"] = manual.get("confidence") or "high"
        out["evidence"].append(f"人工规则:{manual.get('name') or manual.get('id')}")

    platform = _match_ai_platform(hay_path, cfg)
    if platform:
        out.update({
            "source_kind": "ai",
            "source_name": platform,
            "source_category": "ai_platform",
            "source_confidence": "high",
        })
        out["evidence"].append(f"路径/文件名命中AI平台:{platform}")
        return _finish(out, cfg)

    if manual and out.get("source_kind") == "ai":
        return _finish(out, cfg)

    for rule in CAMERA_RULES:
        meta_hit = any(n and n in hay_meta for n in rule["needles"])
        file_hit = any(_rx_search(p, path.name) for p in rule["filename"])
        if meta_hit or file_hit:
            out.update({
                "source_kind": "real",
                "source_name": rule["name"],
                "source_category": rule["category"],
                "source_confidence": "high" if meta_hit else "medium",
                "color_profile": rule["profile"],
            })
            out["evidence"].append(("EXIF" if meta_hit else "文件名") + f"命中:{rule['name']}")
            return _finish(out, cfg)

    # 兼容旧 config 里的显式文件名/机型映射。
    fp = cfg.guess_profile_by_filename(path.name)
    if fp:
        out.update({
            "source_kind": "real",
            "source_name": _profile_device(fp) or "相机",
            "source_category": "camera",
            "source_confidence": "medium",
            "color_profile": fp,
        })
        out["evidence"].append(f"文件名色彩规则:{fp}")
        return _finish(out, cfg)

    cp = cfg.guess_profile(meta.get("make", ""), meta.get("model", ""))
    if cp:
        out.update({
            "source_kind": "real",
            "source_name": _profile_device(cp) or "相机",
            "source_category": "camera",
            "source_confidence": "medium",
            "color_profile": cp,
        })
        out["evidence"].append(f"机型色彩规则:{cp}")
        return _finish(out, cfg)

    if meta.get("make") or meta.get("model"):
        out["source_name"] = "其他"
        out["source_category"] = "camera"
        out["source_confidence"] = "low"
        out["evidence"].append("EXIF存在但未命中设备规则")
    return _finish(out, cfg)


def load_rules(cfg) -> list[dict]:
    path = cfg.out_dir / "source_rules.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def save_rules(cfg, rules: list[dict]):
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    (cfg.out_dir / "source_rules.json").write_text(
        json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def matches_rule_path(rule: dict, path: Path | str) -> bool:
    """单条人工规则是否命中某路径。给管理界面统计影响范围用。"""
    p = Path(path)
    ps = str(p)
    typ = rule.get("match_type") or "path_prefix"
    pat = rule.get("pattern") or ""
    if typ == "path_prefix":
        return bool(pat and ps.startswith(pat))
    if typ == "path_contains":
        return bool(pat and pat.lower() in ps.lower())
    if typ == "filename_regex":
        return _rx_search(pat, p.name)
    return False


def _match_manual_rule(path: Path, cfg) -> dict | None:
    p = str(path)
    matches = []
    for r in load_rules(cfg):
        if r.get("disabled"):
            continue
        if matches_rule_path(r, p):
            matches.append(r)
    if not matches:
        return None
    # 更具体的路径规则优先。
    return sorted(matches, key=lambda r: len(r.get("pattern") or ""), reverse=True)[0]


def _match_ai_platform(hay_path: str, cfg) -> str | None:
    for name, pats in AI_PLATFORM_PATTERNS:
        if any(_contains_or_regex(p, hay_path) for p in pats):
            return name
    for marker in getattr(cfg, "ai_markers", []):
        if marker and marker.lower() in hay_path:
            return marker
    return None


def _finish(out: dict, cfg) -> dict:
    if not out.get("color_profile") and out.get("_path_name"):
        out["color_profile"] = _filename_profile(out["_path_name"])
    profile = out.get("color_profile")
    if profile:
        out["lut_id"] = _lut_id(profile, cfg)
    out.pop("_path_name", None)
    return out


def _filename_profile(name: str) -> str | None:
    for profile, pats in PROFILE_FILENAME_PATTERNS:
        if any(_rx_search(p, name) for p in pats):
            return profile
    return None


def _lut_id(profile: str, cfg) -> str | None:
    lut = cfg.lut_path(profile)
    return lut.stem if lut else None


def _profile_device(profile: str) -> str | None:
    if profile in ("D-Log", "D-Log M"):
        return "大疆无人机"
    if profile == "S-Log3":
        return "索尼相机"
    if profile == "N-Log":
        return "尼康相机"
    return None


def _rx_search(pattern: str, text: str) -> bool:
    try:
        return bool(re.search(pattern, text, re.IGNORECASE))
    except re.error:
        return pattern.lower() in text.lower()


def _contains_or_regex(pattern: str, text: str) -> bool:
    if any(c in pattern for c in r"\^$.*+?[](){}|"):
        return _rx_search(pattern, text)
    return pattern.lower() in text
