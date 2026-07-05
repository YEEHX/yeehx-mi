"""危险区（4-2 清空缩略图 / 4-3 清空标签库）回归测试。
铁律验收：任何清空操作后，原始素材文件 mtime/字节 零变化。
跑法同冒烟：YEEHX_MOCK=1 python3 -m pytest app/tests -q
"""
from __future__ import annotations

import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(scope="session")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def media(client, tmp_path_factory):
    """独立素材夹：快扫 + 等任务，返回 (目录, assets)。"""
    d = tmp_path_factory.mktemp("危险区素材盘") / "测试场景"
    d.mkdir(parents=True)
    for i in range(3):
        img = np.full((240, 320, 3), 60 + i * 40, np.uint8)
        cv2.putText(img, f"D{i}", (40, 120), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
        cv2.imwrite(str(d / f"CLIP0{i}.jpg"), img)
    r = client.post("/api/scan/quick_import", json={"root_path": str(d)})
    assert r.status_code == 200
    _wait_tasks(client)
    assets = client.get("/api/fs", params={"dir": str(d)}).json()["assets"]
    assert len(assets) == 3
    return d, assets


def _wait_tasks(client, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not client.get("/api/tasks").json()["active"]:
            return
        time.sleep(0.3)
    raise AssertionError("任务超时未完成")


# ── 预览接口 ───────────────────────────────────────────────────────────
def test_danger_preview(client):
    d = client.get("/api/danger/preview").json()
    assert d["original_files_affected"] == 0
    assert {"count", "bytes"} <= set(d["thumbs"])
    assert {"tags", "aliases", "candidates", "asset_links", "locked_with_tags"} <= set(d["tags"])


# ── 4-2 清空缩略图 + 重新生图 ──────────────────────────────────────────
def test_thumbs_clear_then_rebuild(client, media):
    from app.config import get_cfg
    from app import db
    d, assets = media
    cfg = get_cfg()
    assert any(a["thumb"] for a in assets), "前置：生图任务应已产出缩略图"
    originals = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in d.glob("*.jpg")}

    # 错误短语 → 400；正确 → 执行
    assert client.post("/api/thumbs/clear", json={"confirm": "CLEAR"}).status_code == 400
    r = client.post("/api/thumbs/clear", json={"confirm": "CLEAR-THUMBS"})
    assert r.status_code == 200 and r.json()["deleted"] > 0

    # 派生图全删、库内引用全空、封面清空
    assert not [p for p in cfg.thumbs_dir.glob("*") if p.is_file()]
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE thumb_path!=''").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM folders WHERE cover_thumb!='' AND cover_thumb IS NOT NULL").fetchone()[0] == 0
    # 界面拿到的是占位（thumb 为空），不报错
    after = client.get("/api/fs", params={"dir": str(d)}).json()["assets"]
    assert all(not a["thumb"] for a in after)

    # 铁律：原始素材文件零变化
    for p, (mt, size) in originals.items():
        assert p.stat().st_mtime_ns == mt and p.stat().st_size == size

    # 一键重新生图（在线素材）→ 缩略图恢复
    r = client.post("/api/thumbs/rebuild")
    assert r.status_code == 200 and r.json()["count"] >= 3
    _wait_tasks(client)
    back = client.get("/api/fs", params={"dir": str(d)}).json()["assets"]
    assert all(a["thumb"] for a in back)
    for p, (mt, size) in originals.items():
        assert p.stat().st_mtime_ns == mt and p.stat().st_size == size


# ── 4-3 只清打标关系（保留词表 + 锁定素材标签） ────────────────────────
def test_tags_clear_taggings_keeps_vocab_and_locked(client, media):
    _, assets = media
    locked_aid, plain_aid = assets[0]["asset_id"], assets[1]["asset_id"]
    for aid in (locked_aid, plain_aid):
        r = client.post(f"/api/asset/{aid}/tags", json={"names": ["危险区锁定词"]})
        assert r.status_code == 200
    client.post(f"/api/asset/{locked_aid}/lock", json={"locked": True})

    r = client.post("/api/tags/clear", json={"confirm": "CLEAR-TAGS", "scope": "taggings",
                                             "keep_locked": True})
    assert r.status_code == 200 and r.json()["scope"] == "taggings"

    # 词表还在；锁定素材的标签保住，未锁定的清掉
    names = {t["name"] for t in client.get("/api/tags").json()["tags"]}
    assert "危险区锁定词" in names
    locked_a = client.get(f"/api/asset/{locked_aid}").json()
    assert "危险区锁定词" in [t["name"] for t in locked_a["effective"]["tags"]]
    plain_a = client.get(f"/api/asset/{plain_aid}").json()
    assert "危险区锁定词" not in [t["name"] for t in plain_a["effective"]["tags"]]
    # AI 描述/状态：锁定保留，未锁定清空回 pending（人工备注 note 不动）
    assert locked_a["desc_ai"] and locked_a["status"] == "tagged"
    assert not plain_a["desc_ai"] and plain_a["status"] == "pending"
    client.post(f"/api/asset/{locked_aid}/lock", json={"locked": False})


# ── 4-3 全部清空（恢复出厂） ───────────────────────────────────────────
def test_tags_clear_all_factory_reset(client, media):
    from app.config import get_cfg
    _, assets = media
    probe_aid = assets[0]["asset_id"]   # 上个测试解锁后仍带着 AI 描述
    client.post(f"/api/asset/{probe_aid}/desc", json={"note": "人工备注不能丢", "manual": True})
    t = client.post("/api/tags", json={"name": "污染词XYZ", "category": "自定类目X",
                                       "aliases": ["污染别名"]})
    assert t.status_code == 200

    assert client.post("/api/tags/clear", json={"confirm": "错的"}).status_code == 400
    r = client.post("/api/tags/clear", json={"confirm": "CLEAR-TAGS", "scope": "all"})
    assert r.status_code == 200
    out = r.json()
    assert out["cleared"]["tags"] > 0 and out["backup"].endswith(".zip")

    # 强制备份真实落盘（zip 含参考图）
    bdir = get_cfg().out_dir / "backups"
    assert list(bdir.glob("tags-*pre-clear-all*.zip"))

    # 回到出厂：污染词没了、自定类目没了、种子词和固定词表立即可用
    d = client.get("/api/tags").json()
    names = {x["name"] for x in d["tags"]}
    cats = {c["name"] for c in d["categories"]}
    assert "污染词XYZ" not in names and "自定类目X" not in cats
    assert "航拍" in names and "城市天际线" in names and "地点" in cats

    # 搜索 / 打标关系空但不报错
    assert client.post("/api/search", json={"q": "航拍"}).status_code == 200
    from app import db
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM asset_tags").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0] == 0

    # 素材回到未打标：AI 描述清空、状态 pending；人工备注保留（用户实测反馈的修复点）
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE locked=0 AND "
                        "(status='tagged' OR (desc_ai IS NOT NULL AND desc_ai!=''))").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM folders WHERE desc_ai IS NOT NULL AND desc_ai!=''").fetchone()[0] == 0
    probe = client.get(f"/api/asset/{probe_aid}").json()
    assert probe["desc_ai"] == "" and probe["status"] == "pending"
    assert probe["note"] == "人工备注不能丢"


# ── 任务运行时 409 ─────────────────────────────────────────────────────
def test_danger_blocked_while_task_active(client):
    from app import db

    def _ins(conn):
        conn.execute("INSERT INTO tasks(task_id,queue,kind,title,total,done,failed,status,created_at,updated_at)"
                     " VALUES ('t_fake_busy','thumb','thumb_gen','假任务',1,0,0,'paused',?,?)",
                     (db.now(), db.now()))
    db.write(_ins)
    try:
        # 统一确认短语 YEEHX（短语校验在任务校验之前，409 即证明短语被接受）
        assert client.post("/api/thumbs/clear", json={"confirm": "YEEHX"}).status_code == 409
        assert client.post("/api/tags/clear", json={"confirm": "YEEHX"}).status_code == 409
        assert client.post("/api/tags/import?mode=replace", content=b"{}").status_code == 409
    finally:
        db.write(lambda c: c.execute("DELETE FROM tasks WHERE task_id='t_fake_busy'"))
