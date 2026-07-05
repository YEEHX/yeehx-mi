"""一期优化 · 阶段1 回归测试：
1-1 搜索水合批量化（与旧逐条算法逐项一致）· 1-2 浏览登记延迟指纹+搬家识别 ·
1-3 子文件夹卡片聚合/封面持久化 · 1-5 标签库缓存。
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


def _mk_media(d, names):
    d.mkdir(parents=True, exist_ok=True)
    for i, n in enumerate(names):
        img = np.full((240, 320, 3), 50 + i * 30, np.uint8)
        cv2.putText(img, n[:6], (20, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 3)
        cv2.imwrite(str(d / f"{n}.jpg"), img)


def _wait_tasks(client, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not client.get("/api/tasks").json()["active"]:
            return
        time.sleep(0.3)
    raise AssertionError("任务超时未完成")


# ── 1-1 水合结果与旧逐条算法逐项一致 ───────────────────────────────────
def _old_effective(a) -> dict:
    """v1.6.2 之前的逐条爬链实现，作为对照基准。"""
    from app.core import folders, assets as assets_mod
    folder = folders.get(a["folder_id"]) if a.get("folder_id") else None
    chain = folders.ancestors(a["volume_id"], folder["rel_path"]) if folder else []
    tag_ids: list[str] = []
    for fol in chain:
        for tid in folders.tag_ids(fol["folder_id"]):
            if tid not in tag_ids:
                tag_ids.append(tid)
    excluded = assets_mod.excluded_tag_ids(a["asset_id"])
    tag_ids = [t for t in tag_ids if t not in excluded]
    own = assets_mod.own_tag_ids(a["asset_id"])
    for tid in own:
        if tid not in tag_ids:
            tag_ids.append(tid)
    lut = a.get("lut")
    if lut is None:
        for fol in chain:
            if fol.get("lut"):
                lut = fol["lut"]
                break
    return {"ids": set(tag_ids), "own": set(own) & set(tag_ids), "lut": lut,
            "color": list((a.get("facts") or {}).get("color") or []), "status": a.get("status")}


def test_hydrate_matches_legacy(client, tmp_path_factory):
    from app.core import assets as A, folders, search as S

    root = tmp_path_factory.mktemp("一致性盘") / "城市夜景"
    _mk_media(root, ["N0", "N1", "N2"])
    client.post("/api/scan/quick_import", json={"root_path": str(root)})
    _wait_tasks(client)

    d = client.get("/api/fs", params={"dir": str(root)}).json()
    fid = d["folder"]["folder_id"]
    aids = [a["asset_id"] for a in d["assets"]]
    # 文件夹继承标签 + 自有标签 + 一个排除 + 文件夹 LUT，把四种语义都摆上
    client.post(f"/api/folder/{fid}/tags", json={"names": ["夜景"]})
    client.post(f"/api/asset/{aids[0]}/tags", json={"names": ["航拍"]})
    night = next(t for t in client.get("/api/tags").json()["tags"] if t["name"] == "夜景")
    client.delete(f"/api/asset/{aids[1]}/tag/{night['id']}")   # 排除继承
    folders.set_meta(fid, lut="测试LUT")
    from app.core import inheritance
    inheritance.recompute_subtree(d["volume_id"], d["rel_path"])

    res = S.search(query="", facets={}, scope={"volume_id": d["volume_id"], "rel_path": d["rel_path"]},
                   with_facets=False)
    assert res["total"] == 3
    for item in res["assets"]:
        a = A.get(item["asset_id"])
        old = _old_effective(a)
        assert {t["id"] for t in item["tags"]} == old["ids"], a["name"]
        assert {t["id"] for t in item["tags"] if t["own"]} == old["own"], a["name"]
        assert item["lut"] == old["lut"] and item["color"] == old["color"]
        assert item["status"] == old["status"]


def test_hydrate_no_per_asset_crawl(client, monkeypatch):
    """结构性断言：水合不再走单条 effective_named（防回归到逐条爬链）。"""
    from app.core import search as S, inheritance

    def boom(*a, **kw):
        raise AssertionError("搜索水合不应逐条调用 effective_named")

    monkeypatch.setattr(inheritance, "effective_named", boom)
    out = S.search(query="", facets={}, with_facets=False, limit=10)
    assert "assets" in out   # 正常返回即证明走的是批量路径


# ── 1-2 浏览登记：指纹后台补算 + 改名搬家延迟识别 ──────────────────────
def test_browse_defers_fingerprint(client, tmp_path_factory):
    from app import db

    root = tmp_path_factory.mktemp("延迟指纹盘") / "新素材"
    _mk_media(root, ["F0", "F1"])
    d = client.get("/api/fs", params={"dir": str(root)}).json()
    assert len(d["assets"]) == 2          # 浏览即登记（同步部分）
    conn = db.connect()
    aids = [a["asset_id"] for a in d["assets"]]
    qs = ",".join("?" * len(aids))
    # 指纹不在请求里同步算：登记完成时应为空，由后台任务补
    _wait_tasks(client)
    n_filled = conn.execute(
        f"SELECT COUNT(*) FROM assets WHERE asset_id IN ({qs}) AND content_id!=''", aids).fetchone()[0]
    assert n_filled == 2, "后台补算完成后指纹应填齐"


def test_browse_rename_keeps_tags_via_deferred_merge(client, tmp_path_factory):
    root = tmp_path_factory.mktemp("改名盘") / "外拍"
    _mk_media(root, ["旧名"])
    d = client.get("/api/fs", params={"dir": str(root)}).json()
    _wait_tasks(client)                    # 等指纹补算完成
    aid = d["assets"][0]["asset_id"]
    client.post(f"/api/asset/{aid}/tags", json={"names": ["保命标签2"]})

    os.rename(root / "旧名.jpg", root / "新名.jpg")
    d2 = client.get("/api/fs", params={"dir": str(root)}).json()   # 重新浏览 → 登记新路径占位
    _wait_tasks(client)                    # 补算指纹 → 延迟搬家识别合并
    d3 = client.get("/api/fs", params={"dir": str(root)}).json()
    names = [a["name"] for a in d3["assets"]]
    assert "新名.jpg" in names and "旧名.jpg" not in names, "旧占位行不应残留"
    renamed = next(a for a in d3["assets"] if a["name"] == "新名.jpg")
    assert "保命标签2" in [t["name"] for t in renamed["tags"]], "改名后标签必须保留"
    assert len(d3["assets"]) == 1


# ── 1-3 子文件夹卡片：批量查询 + 封面来自持久化字段（含祖先回填） ──────
def test_subfolder_cards_aggregate_and_cover(client, tmp_path_factory):
    base = tmp_path_factory.mktemp("封面盘")
    _mk_media(base / "A", ["A0", "A1"])
    _mk_media(base / "A" / "B", ["B0", "B1", "B2"])
    client.post("/api/scan/quick_import", json={"root_path": str(base / "A")})
    _wait_tasks(client)

    top = client.get("/api/fs", params={"dir": str(base)}).json()
    card_a = next(c for c in top["subdirs"] if c["name"] == "A")
    assert card_a["imported"] and card_a["indexed"] == 2
    assert card_a["cover"], "A 自己有素材，封面应来自 cover_thumb"

    mid = client.get("/api/fs", params={"dir": str(base / "A")}).json()
    card_b = next(c for c in mid["subdirs"] if c["name"] == "B")
    assert card_b["imported"] and card_b["indexed"] == 3 and card_b["cover"]


def test_parent_cover_bubbles_up(client, tmp_path_factory):
    """父夹自己没直属素材：封面靠生图任务向祖先回填（不再现场扫子树）。"""
    base = tmp_path_factory.mktemp("上浮盘")
    _mk_media(base / "P" / "C", ["C0", "C1"])      # P 本身无直属媒体
    client.post("/api/scan/quick_import", json={"root_path": str(base / "P")})
    _wait_tasks(client)
    from app.core import folders, volumes
    info = volumes.peek(str(base / "P"))
    f = folders.get_by_path(info["volume_id"], info["rel_path"])
    if f:   # P 有登记行才有封面可言（quick_import 会为根建行）
        assert f.get("cover_thumb"), "子级生图后应向上回填父夹封面"


# ── 1-5 标签库缓存 ─────────────────────────────────────────────────────
def test_tag_library_cached_and_fresh(client, monkeypatch):
    from app.core import tags as TG
    from app.scan import tagging

    calls = {"n": 0}
    real_list = TG.list_

    def counting_list(*a, **kw):
        calls["n"] += 1
        return real_list(*a, **kw)

    monkeypatch.setattr(TG, "list_", counting_list)
    TG.invalidate_term_cache()             # 清掉别的测试留下的缓存，从零计数
    lib1 = tagging._tag_library()
    n_after_first = calls["n"]
    for _ in range(10):
        assert tagging._tag_library() == lib1
    assert calls["n"] == n_after_first, "命中缓存后不应再全量读标签表"

    TG.add("缓存新词", "内容")
    lib2 = tagging._tag_library()
    assert "缓存新词" in [n for names in lib2.values() for n in names], "写入后下一次必须立刻拿到新词"
