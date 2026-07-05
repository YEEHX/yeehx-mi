"""首跑向导：/api/setup/status 判定 + /api/setup/done 落盘 + 老库自动豁免。"""
import json

from fastapi.testclient import TestClient

from app.main import app
from app.config import get_cfg

client = TestClient(app)


def _fresh_cfg(tmp_path=None):
    cfg = get_cfg()
    if cfg.app_settings_path.exists():
        # 清空 setup_done，模拟全新数据目录
        data = json.loads(cfg.app_settings_path.read_text(encoding="utf-8"))
        data.pop("setup_done", None)
        cfg.app_settings_path.write_text(json.dumps(data), encoding="utf-8")
    cfg.setup_done = False
    return cfg


def test_setup_status_first_run_when_empty():
    """空库 + 无标记 = 首跑；返回环境探测字段。"""
    from app import db
    has_assets = bool(db.connect().execute("SELECT 1 FROM assets LIMIT 1").fetchone())
    _fresh_cfg()
    d = client.get("/api/setup/status").json()
    assert d["first_run"] == (not has_assets)
    assert "ollama_alive" in d and "arch" in d and "has_api_key" in d


def test_setup_done_persists_and_survives_other_saves():
    """done 落盘后不再首跑；且保存其它 app 设置不会抹掉标记（save 基底坑）。"""
    cfg = _fresh_cfg()
    r = client.post("/api/setup/done", json={})
    assert r.json()["ok"]
    assert client.get("/api/setup/status").json()["first_run"] is False
    # 关键回归：另存一个无关设置，setup_done 必须存活
    cfg.save_app_settings({"auto_lut": True})
    data = json.loads(cfg.app_settings_path.read_text(encoding="utf-8"))
    assert data.get("setup_done") is True
    assert client.get("/api/setup/status").json()["first_run"] is False


def test_setup_status_auto_done_for_existing_library(tmp_path):
    """老库升级：已有素材时自动补 setup_done，不弹向导。"""
    from app.core import volumes as V, folders as F, assets as A
    cfg = _fresh_cfg()
    # 造一条素材（等价老库）
    vid = "vol_wiz_test"
    from app import db
    db.write(lambda c: c.execute(
        "INSERT OR IGNORE INTO volumes(volume_id,uuid,name,display_name,online,created_at,updated_at)"
        " VALUES (?,?,?,?,1,?,?)", (vid, "u", "wiz", "wiz", db.now(), db.now())))
    fid = F.ensure(vid, "wizdir")
    A.upsert(vid, fid, "wizdir/a.jpg", "a.jpg", "photo", size=1, mtime=1.0)
    d = client.get("/api/setup/status").json()
    assert d["first_run"] is False
    assert json.loads(cfg.app_settings_path.read_text(encoding="utf-8")).get("setup_done") is True
