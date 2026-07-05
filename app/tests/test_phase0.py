"""一期优化 · 阶段0 的回归测试：
0-1 AI 失败降级（不 500）· 0-2 tag_id 索引 · 0-3 本机访问守卫 · 0-4 标签库导出/导入+自动备份。
跑法同冒烟：YEEHX_MOCK=1 python3 -m pytest app/tests -q
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(scope="session")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


# ── 0-2 索引存在且生效 ─────────────────────────────────────────────────
def test_tag_id_indexes(client):
    from app import db
    conn = db.connect()
    names = {r[1] for r in conn.execute("PRAGMA index_list(asset_tags)").fetchall()}
    assert "idx_asset_tags_tag" in names
    plan = " ".join(r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM asset_tags WHERE tag_id='x'").fetchall())
    assert "idx_asset_tags_tag" in plan          # 不再全表扫
    for table, idx in (("folder_tags", "idx_folder_tags_tag"),
                       ("asset_tag_excludes", "idx_asset_tag_excludes_tag")):
        assert idx in {r[1] for r in conn.execute(f"PRAGMA index_list({table})").fetchall()}


# ── 0-3 本机访问守卫 ───────────────────────────────────────────────────
def test_guard_rejects_foreign_host(client):
    r = client.get("/api/health", headers={"host": "evil.example.com"})
    assert r.status_code == 403
    r = client.post("/api/search", json={"q": ""}, headers={"host": "evil.example.com:8788"})
    assert r.status_code == 403


def test_guard_rejects_foreign_origin_on_write(client):
    r = client.post("/api/search", json={"q": ""}, headers={"origin": "https://evil.example.com"})
    assert r.status_code == 403
    # 本机 Origin（浏览器同源请求自带）放行
    r = client.post("/api/search", json={"q": ""}, headers={"origin": "http://127.0.0.1:8788"})
    assert r.status_code == 200
    # GET 带外源 Origin 不拦（读保护靠 Host 校验挡 rebinding）
    r = client.get("/api/health", headers={"origin": "https://evil.example.com"})
    assert r.status_code == 200


def test_guard_allows_local(client):
    assert client.get("/api/health", headers={"host": "127.0.0.1:8788"}).status_code == 200
    assert client.get("/api/health", headers={"host": "localhost"}).status_code == 200


# ── 0-1 AI 解析失败 → 降级普通搜索，不再 400/500 ───────────────────────
def test_ai_search_degrades_without_model(client, monkeypatch):
    from app import main as main_mod

    def boom(*a, **kw):
        return {"ok": False, "error": "连不上模型(模拟)", "json": None}

    monkeypatch.setattr(main_mod.vision, "parse_command", boom)
    r = client.post("/api/ai_search", json={"q": "黄鹤楼 航拍"})
    assert r.status_code == 200
    d = r.json()
    assert d.get("degraded") is True
    assert "连不上" in d.get("degraded_reason", "")
    assert "assets" in d and d["parsed"]["action"] == "search"


def test_vision_catches_oserror(monkeypatch):
    """certifi 证书包失效这类 OSError 必须变成 ok=False，不能抛。"""
    import requests as req
    from app.ai import vision
    from app.config import get_cfg

    monkeypatch.delenv("YEEHX_MOCK", raising=False)

    def boom(*a, **kw):
        raise OSError("Could not find a suitable TLS CA certificate bundle")

    monkeypatch.setattr(req, "post", boom)
    out = vision.parse_command("找雪景", {}, get_cfg())
    assert out["ok"] is False and "TLS" in out["error"]
    out = vision.suggest_tag_merges([{"name": "夜景"}], get_cfg())
    assert out["ok"] is False


# ── 0-4 标签库导出 / 导入 / 自动备份 ──────────────────────────────────
def test_tags_export_import_roundtrip(client):
    # 建一个带别名+备注的词
    r = client.post("/api/tags", json={"name": "鹦鹉洲大桥", "category": "地点",
                                       "aliases": ["鹦鹉洲长江大桥"], "note": "武汉 主跨850m"})
    assert r.status_code == 200
    tid = r.json()["id"]

    # 导出 JSON
    r = client.get("/api/tags/export")
    assert r.status_code == 200 and "attachment" in r.headers.get("content-disposition", "")
    data = json.loads(r.content)
    assert data["schema_version"] == 1
    mine = [t for t in data["tags"] if t["name"] == "鹦鹉洲大桥"]
    assert mine and mine[0]["aliases"] == ["鹦鹉洲长江大桥"] and "850m" in mine[0]["note"]

    # 删掉 → merge 导入还原
    assert client.delete(f"/api/tag/{tid}").status_code == 200
    r = client.post("/api/tags/import?mode=merge", content=json.dumps(data).encode("utf-8"))
    assert r.status_code == 200
    rep = r.json()
    assert rep["created"] >= 1 and rep["ok"]
    back = client.get("/api/tags").json()
    hit = [t for t in back["tags"] if t["name"] == "鹦鹉洲大桥"]
    assert hit and hit[0]["aliases"] == ["鹦鹉洲长江大桥"]

    # 同一份再导一遍：幂等，全部走合并、不新建不重复
    r = client.post("/api/tags/import?mode=merge", content=json.dumps(data).encode("utf-8"))
    rep2 = r.json()
    assert rep2["created"] == 0 and rep2["merged"] >= 1
    again = client.get("/api/tags").json()
    assert len([t for t in again["tags"] if t["name"] == "鹦鹉洲大桥"]) == 1


def test_tags_import_replace_restores_full_library(client):
    before = json.loads(client.get("/api/tags/export").content)
    n_before = len(before["tags"])
    r = client.post("/api/tags/import?mode=replace", content=json.dumps(before).encode("utf-8"))
    assert r.status_code == 200
    after = json.loads(client.get("/api/tags/export").content)
    assert {t["name"] for t in after["tags"]} == {t["name"] for t in before["tags"]}
    assert len(after["tags"]) == n_before
    # 搜索/打标主流程不报错（词表完整重建）
    assert client.post("/api/search", json={"q": "航拍"}).status_code == 200


def test_tags_import_rejects_garbage(client):
    assert client.post("/api/tags/import", content=b"not json at all").status_code == 400
    assert client.post("/api/tags/import?mode=wat", content=b"{}").status_code == 400
    assert client.post("/api/tags/import",
                       content=json.dumps({"hello": 1}).encode()).status_code == 400


def test_destructive_ops_write_backup(client):
    from app.config import get_cfg
    bdir = get_cfg().out_dir / "backups"
    n0 = len(list(bdir.glob("tags-*.json"))) if bdir.exists() else 0
    # 删一个词 → 自动备份先落盘
    t = client.post("/api/tags", json={"name": "备份测试词", "category": "内容"}).json()
    assert client.delete(f"/api/tag/{t['id']}").status_code == 200
    files = list(bdir.glob("tags-*.json"))
    assert len(files) > n0
    # 删除前置备份（reason=delete）里必须能找到这个词——证明备份发生在删除之前
    assert any("备份测试词" in {x["name"] for x in
               json.loads(p.read_text(encoding="utf-8"))["tags"]}
               for p in bdir.glob("tags-*-delete*.json"))


def test_backup_rolls_at_limit(client):
    from app.config import get_cfg
    from app.core import tag_io
    bdir = get_cfg().out_dir / "backups"
    for i in range(12):
        tag_io.backup(f"roll{i}")
    files = list(bdir.glob("tags-*.json"))
    assert len(files) <= tag_io.KEEP_BACKUPS
