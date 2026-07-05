"""品牌三件套 + 检查更新：署名烧包、资源哈希自检、version.json 比对。"""
import json
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_mod
from app.main import app, _ver_tuple
from app import brand as brand_mod
from app.config import get_cfg

client = TestClient(app)


def test_brand_signature_burned_in(tmp_path, monkeypatch):
    """brand.json 里改署名字段无效：author/links/copy_id 永远来自 brand.py。"""
    external = {"author": "冒名者", "copy_id": "假ID", "links": [{"name": "x", "url": "http://x"}],
                "tagline": "外部改口号是允许的"}
    merged = brand_mod.merged_brand(external)
    assert merged["author"] == brand_mod.BRAND_BUILTIN["author"]
    assert merged["copy_id"] == "玩椰"
    assert merged["links"] == brand_mod.BRAND_BUILTIN["links"]
    assert merged["tagline"] == "外部改口号是允许的"   # 非署名字段可覆盖


def test_api_brand_returns_builtin_author():
    d = client.get("/api/brand").json()
    assert d["brand"]["author"] == brand_mod.BRAND_BUILTIN["author"]
    assert "tampered" in d


def test_assets_tampered(tmp_path):
    """logo 缺失或被替换 → tampered。"""
    assert brand_mod.assets_tampered(tmp_path) is True   # 缺失
    p = tmp_path / "web" / "觅影-logo.png"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"fake logo")
    assert brand_mod.assets_tampered(tmp_path) is True   # 哈希不符
    real = Path(main_mod.__file__).resolve().parent
    if (real / "web" / "觅影-logo.png").exists():
        assert brand_mod.assets_tampered(real) is False  # 真资源通过


def test_ver_tuple_compare():
    assert _ver_tuple("2.0.0") > _ver_tuple("1.9.1")
    assert _ver_tuple("v2.0.0") == _ver_tuple("2.0.0")
    assert _ver_tuple("1.10.0") > _ver_tuple("1.9.9")
    assert _ver_tuple("") == (0,)


def test_update_check_flow(monkeypatch):
    cfg = get_cfg()
    cfg.save_app_settings({"update_check": True})
    monkeypatch.setattr(main_mod, "_fetch_version_info",
                        lambda: {"version": "99.0.0", "url": "https://www.yeehx.com/miying", "notes": "新"})
    d = client.get("/api/update/check").json()
    assert d["ok"] and d["has_update"] and d["latest"] == "99.0.0"
    # 同版本 → 无更新
    monkeypatch.setattr(main_mod, "_fetch_version_info", lambda: {"version": d["current"]})
    assert client.get("/api/update/check").json()["has_update"] is False
    # 网络故障 → 静默降级不抛错
    def _boom():
        raise RuntimeError("dns fail")
    monkeypatch.setattr(main_mod, "_fetch_version_info", _boom)
    d2 = client.get("/api/update/check").json()
    assert d2["ok"] is False and d2["has_update"] is False
    # 设置关闭 → 不请求
    cfg.save_app_settings({"update_check": False})
    assert client.get("/api/update/check").json()["enabled"] is False
    cfg.save_app_settings({"update_check": True})
