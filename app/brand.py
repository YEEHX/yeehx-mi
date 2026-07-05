"""品牌常量：署名烧进代码，web/brand.json 只允许覆盖非署名字段。

发布三件套之一（2026-06-10 拍板）：
1. 署名字段（作者/ID/官网/链接）以这里为准——外部 json 改不动；
2. 品牌资源哈希自检：logo 被替换时界面回落文字署名（tampered 标志）；
3. 「检查更新」指向官网 version.json（见 main.py /api/update/check）。

注意：这不是防篡改的技术保证（AGPL 下改代码是合法的），只是让"顺手抹署名"
多一道工序；名称与 logo 的法律保护在 NOTICE 的商标声明里。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# ── 署名常量（烧包）：/api/brand 永远以此为基底 ──
BRAND_BUILTIN = {
    "author": "玩椰 · AI导演 / AI创作者 · 武汉",
    "copy_id": "玩椰",
    "site": "https://www.yeehx.com",
    "email": "hi@yeehx.com",
    "links": [
        {"name": "抖音", "url": "https://v.douyin.com/vcq9jo-kBZ0"},
        {"name": "B站", "url": "https://space.bilibili.com/371127301"},
        {"name": "小红书", "url": "https://www.xiaohongshu.com/user/profile/5c2cfc150000000005027f1d"},
        {"name": "微博", "url": "https://weibo.com/u/7854994467"},
        {"name": "视频号", "url": "", "copy": "微信视频号搜「玩椰」"},
        {"name": "官网", "url": "https://www.yeehx.com"},
    ],
}

# brand.json 允许覆盖的非署名字段（其余一律忽略）
BRAND_OVERRIDABLE = {"slogan", "tagline", "tagline_candidates", "donation"}

# ── 品牌资源哈希（sha256）：启动自检，被替换则 tampered=True ──
ASSET_HASHES = {
    "web/觅影-logo.png": "e41cb504c08e1c0f6200ff69f458e3940087d8eac30152307cf066341b8c6e89",
}

# ── 检查更新 ──
UPDATE_URL = "https://www.yeehx.com/miying/version.json"
DOWNLOAD_PAGE = "https://www.yeehx.com/miying"


def merged_brand(external: dict | None) -> dict:
    """内置署名 + 外部 json 的非署名字段。"""
    out = dict(BRAND_BUILTIN)
    for k in BRAND_OVERRIDABLE:
        if external and k in external:
            out[k] = external[k]
    return out


def assets_tampered(base: Path) -> bool:
    """任一品牌资源缺失或哈希不符 → True（界面回落文字署名）。"""
    for rel, want in ASSET_HASHES.items():
        p = base / rel
        try:
            got = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            return True
        if got != want:
            return True
    return False
