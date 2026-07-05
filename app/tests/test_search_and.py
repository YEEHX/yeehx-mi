"""搜索多词 AND 语义回归（修复：整句和标签名双向包含导致「黄鹤楼 航拍」把所有航拍并进来）。
语义：每个词各自求命中集（FTS 文本 ∪ 标签/别名），词与词之间取交集。"""
import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def two_assets(client, tmp_path_factory):
    d = tmp_path_factory.mktemp("搜索AND盘")
    for name in ("鹤A.jpg", "武B.jpg"):
        img = np.full((240, 320, 3), 90, np.uint8)
        cv2.putText(img, name[:1], (40, 120), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
        cv2.imwrite(str(d / name), img)
    assets = client.get("/api/fs", params={"dir": str(d)}).json()["assets"]
    assert len(assets) == 2
    a = next(x["asset_id"] for x in assets if x["name"].startswith("鹤"))
    b = next(x["asset_id"] for x in assets if x["name"].startswith("武"))
    r = client.post(f"/api/asset/{a}/tags", json={"names": "青鹤阁,航拍"})
    assert r.status_code == 200
    r = client.post(f"/api/asset/{b}/tags", json={"names": "紫霄峰,航拍"})
    assert r.status_code == 200
    return a, b


def _hit_ids(client, q):
    res = client.post("/api/search", json={"q": q, "limit": 500, "with_facets": False}).json()
    return {x["asset_id"] for x in res["assets"]}


def test_and_across_terms(client, two_assets):
    a, b = two_assets
    ids = _hit_ids(client, "青鹤阁 航拍")
    assert a in ids and b not in ids, "多词必须同时命中：另一词的素材不该混进「青鹤阁 航拍」"
    ids = _hit_ids(client, "紫霄峰 航拍")
    assert b in ids and a not in ids
    ids = _hit_ids(client, "航拍")
    assert {a, b} <= ids, "单词仍是宽命中"


def test_term_union_within_fields(client, two_assets):
    """词内跨字段并集：B 的文本里有青鹤阁（备注/文件名层），标签里有航拍 → 「黄鹤楼 航拍」应命中 B。"""
    a, b = two_assets
    conn = db.connect()
    db.fts_set(conn, b, "武B 画面很像青鹤阁的角度")
    conn.commit()
    ids = _hit_ids(client, "青鹤阁 航拍")
    assert {a, b} <= ids


def test_no_token_query_returns_empty(client, two_assets):
    ids = _hit_ids(client, "！！？")
    assert ids == set()
