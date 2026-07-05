"""v1.9.0 回归测试：同步=双向对账 + 重扫不重打。
跑法同冒烟：YEEHX_MOCK=1 python3 -m pytest app/tests -q
"""
from __future__ import annotations

import os
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


def _mk(d, name):
    """每个名字生成不同底色：putText 的 Hershey 字体不认中文（全画成 ???），
    底色一样会导致字节级相同 → content_id 相同 → 被搬家识别接管而不是当新素材。"""
    d.mkdir(parents=True, exist_ok=True)
    shade = (sum(ord(c) for c in name) * 37) % 200 + 20
    img = np.full((240, 320, 3), shade, np.uint8)
    cv2.putText(img, name[:6], (20, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 3)
    cv2.imwrite(str(d / f"{name}.jpg"), img)
    return d / f"{name}.jpg"


def _wait(client, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not client.get("/api/tasks").json()["active"]:
            return
        time.sleep(0.3)
    raise AssertionError("任务超时未完成")


def _counting_run_ai(monkeypatch):
    """包一层真 _run_ai（mock 模式），数打标调用并记下素材名。"""
    from app.scan import tagging
    calls = {"names": []}
    real = tagging._run_ai

    def counted(asset):
        calls["names"].append(asset["name"])
        return real(asset)

    monkeypatch.setattr(tagging, "_run_ai", counted)
    return calls


# ── 重扫同一范围：不重打已打标、不重生成已有缩略图 ─────────────────────
def test_rescan_does_not_retag(client, tmp_path_factory, monkeypatch):
    from app import db
    root = tmp_path_factory.mktemp("增量盘") / "增量夹"
    _mk(root, "素材一")
    _mk(root, "素材二")
    calls = _counting_run_ai(monkeypatch)

    r = client.post("/api/scan/quick_import", json={"root_path": str(root), "mode": "full"}).json()
    assert r["ok"]
    _wait(client)
    assert sorted(calls["names"]) == ["素材一.jpg", "素材二.jpg"]
    conn = db.connect()
    thumbs = {x[0]: x[1] for x in conn.execute(
        "SELECT name, thumb_path FROM assets WHERE rel_path LIKE '%增量夹%'").fetchall()}
    assert all(thumbs.values()), "全量扫描后两条素材都应有缩略图"

    # 同范围重扫：零打标调用、零生图任务（缩略图路径不变）
    client.post("/api/scan/quick_import", json={"root_path": str(root), "mode": "full"})
    _wait(client)
    assert sorted(calls["names"]) == ["素材一.jpg", "素材二.jpg"], "重扫不得重打已打标素材"
    thumbs2 = {x[0]: x[1] for x in conn.execute(
        "SELECT name, thumb_path FROM assets WHERE rel_path LIKE '%增量夹%'").fetchall()}
    assert thumbs2 == thumbs, "重扫不得动已有缩略图"


# ── 重扫会补上"打标失败"的素材 ─────────────────────────────────────────
def test_rescan_picks_up_failed(client, tmp_path_factory, monkeypatch):
    from app import db
    from app.core import assets as A
    root = tmp_path_factory.mktemp("补漏盘") / "补漏夹"
    _mk(root, "失败素材")
    client.post("/api/scan/quick_import", json={"root_path": str(root), "mode": "full"})
    _wait(client)
    conn = db.connect()
    aid = conn.execute("SELECT asset_id FROM assets WHERE rel_path LIKE '%补漏夹%'").fetchone()[0]
    A.set_fields(aid, status="failed")          # 人为造一个打标失败

    calls = _counting_run_ai(monkeypatch)
    # 文件夹内容没变（指纹相同跳过登记），收尾仍要把 failed 排进打标
    client.post("/api/scan/quick_import", json={"root_path": str(root), "mode": "full"})
    _wait(client)
    assert calls["names"] == ["失败素材.jpg"], "失败素材重扫必须补打"
    assert A.get(aid)["status"] == "tagged"


# ── 同步=双向对账：新增入库打标，已打标不重打，丢失清记录 ────────────────
def test_sync_op_bidirectional(client, tmp_path_factory, monkeypatch):
    from app import db
    root = tmp_path_factory.mktemp("同步盘") / "同步夹"
    old = _mk(root, "老素材")
    client.post("/api/scan/quick_import", json={"root_path": str(root), "mode": "full"})
    _wait(client)

    calls = _counting_run_ai(monkeypatch)
    _mk(root, "新素材")                         # 加法：硬盘新增
    os.remove(old)                              # 减法：硬盘删除（已打标记录）
    r = client.post("/api/apply", json={
        "targets": [{"type": "path", "path": str(root)}], "op": "sync"}).json()
    assert r["ok"] and r.get("synced") == 1 and r.get("cleanup")
    _wait(client)

    conn = db.connect()
    rows = {x[0]: x[1] for x in conn.execute(
        "SELECT name, status FROM assets WHERE rel_path LIKE '%同步夹%'").fetchall()}
    assert rows.get("新素材.jpg") == "tagged", "新增文件必须入库并打标"
    assert "老素材.jpg" not in rows, "已消失文件的库记录必须被同步清掉"
    assert calls["names"] == ["新素材.jpg"], "同步只打新增/未打标的，不重打已打标的"


# ── 同步的锁定素材保护：锁定的不打标 ───────────────────────────────────
def test_sync_skips_locked(client, tmp_path_factory, monkeypatch):
    from app import db
    from app.core import assets as A
    root = tmp_path_factory.mktemp("锁定盘") / "锁定夹"
    _mk(root, "锁定素材")
    client.post("/api/scan/quick_import", json={"root_path": str(root), "mode": "full"})
    _wait(client)
    conn = db.connect()
    aid = conn.execute("SELECT asset_id FROM assets WHERE rel_path LIKE '%锁定夹%'").fetchone()[0]
    A.set_fields(aid, status="pending", locked=1)   # 未打标但锁定

    calls = _counting_run_ai(monkeypatch)
    client.post("/api/apply", json={
        "targets": [{"type": "path", "path": str(root)}], "op": "sync"})
    _wait(client)
    assert calls["names"] == [], "锁定素材同步时不得打标"
