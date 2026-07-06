"""升级助手回归（v2.1.1）：发现旧安装 → 一键搬库。

真实用户场景（2026-07-06 首例）：下载新版 zip 解压出 MiYing-v2.1.0/，
旧版 MiYing-v2.0.0/ 还在旁边——库、标签、设置全在旧文件夹 app/out/ 里。
候选发现 + 迁移 + 迁移后自动进入"老用户"状态（向导不再弹），全链路都在这测。
"""
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
import app.config as cfgmod
import app.main as main_mod


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    """给本文件每个测试一套干净的库/out/扫描根，结束后恢复全局单例。"""
    out = tmp_path / "new" / "app" / "out"
    out.mkdir(parents=True)
    monkeypatch.setenv("YEEHX_DB", str(out / "miying.sqlite"))
    monkeypatch.setenv("YEEHX_OUT", str(out))
    cfgmod._CFG = None                       # Config 是单例：换 env 后强制重载
    monkeypatch.setattr(main_mod, "_UPGRADE_SCAN_PARENT", tmp_path)
    db.adopt_db()                            # 全线程换到这套空库
    yield tmp_path
    cfgmod._CFG = None                       # env 由 monkeypatch 还原，这里把单例们请回原位
    db.adopt_db()


@pytest.fixture()
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def _make_old_install(root: Path, name="MiYing-v2.0.0", version="2.0.0", n_assets=3) -> Path:
    """造一个最小可信的旧安装：真 SCHEMA 建库 + 版本文件 + 缩略图 + 模型设置。"""
    old = root / name
    out = old / "app" / "out"
    out.mkdir(parents=True)
    (old / "app" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    c = sqlite3.connect(str(out / "miying.sqlite"))
    c.executescript(db.SCHEMA)
    for i in range(n_assets):
        c.execute("INSERT INTO assets (asset_id, name, kind) VALUES (?,?,?)",
                  (f"a{i}", f"旧素材{i}.mp4", "video"))
    c.commit()
    c.close()
    (out / "thumbs").mkdir()
    (out / "thumbs" / "t1.jpg").write_bytes(b"fake-thumb")
    (out / "model_settings.json").write_text('{"provider": "api"}', encoding="utf-8")
    return old


def test_candidates_found(fresh_env, client):
    old = _make_old_install(fresh_env)
    r = client.get("/api/upgrade/candidates").json()
    assert len(r["candidates"]) == 1
    c = r["candidates"][0]
    assert c["path"] == str(old)
    assert c["assets"] == 3
    assert c["version"] == "2.0.0"


def test_migrate_moves_library_and_settings(fresh_env, client):
    old = _make_old_install(fresh_env, n_assets=5)
    r = client.post("/api/upgrade/migrate", json={"path": str(old)}).json()
    assert r["ok"] and r["assets"] == 5
    # 库真的换上了：进程内查询直接可见
    assert db.connect().execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 5
    # 附属资产到位
    cfg = cfgmod.get_cfg()
    assert (cfg.out_dir / "thumbs" / "t1.jpg").read_bytes() == b"fake-thumb"
    assert "api" in (cfg.out_dir / "model_settings.json").read_text(encoding="utf-8")
    # 旧目录的库文件已被搬走（同卷 rename），不会出现"两边都有库"的分裂状态
    assert not (old / "app" / "out" / "miying.sqlite").exists()
    # 迁移后 = 老用户：向导判定不再是 first_run（has_data 自动豁免）
    assert client.get("/api/setup/status").json()["first_run"] is False


def test_candidates_empty_when_has_data(fresh_env, client):
    old = _make_old_install(fresh_env)
    client.post("/api/upgrade/migrate", json={"path": str(old)})
    # 库已有数据：不再当"新家"，候选永远为空，再迁移也被拒
    _make_old_install(fresh_env, name="MiYing-v1.9.0", version="1.9.0")
    assert client.get("/api/upgrade/candidates").json()["candidates"] == []
    r = client.post("/api/upgrade/migrate", json={"path": str(fresh_env / 'MiYing-v1.9.0')})
    assert r.status_code == 400


def test_migrate_rejects_foreign_path(fresh_env, client):
    _make_old_install(fresh_env)
    r = client.post("/api/upgrade/migrate", json={"path": "/etc"})
    assert r.status_code == 400


def test_health_carries_version(client):
    """启动脚本的版本对账靠它：health 必须带 version 字段。"""
    from app import __version__
    assert client.get("/api/health").json()["version"] == __version__


def test_cleanup_trashes_only_migrated_source(fresh_env, client, monkeypatch):
    """清理旧版本：只认搬家记录的来源，三重校验；库没搬空/路径不符都拒绝。"""
    trashed = []
    from app.core import osplat
    monkeypatch.setattr(osplat, "move_to_trash", lambda p: trashed.append(str(p)))

    # 没搬过家 → 没有可清理的记录
    r = client.post("/api/upgrade/cleanup", json={})
    assert r.status_code == 400

    old = _make_old_install(fresh_env, n_assets=2)
    client.post("/api/upgrade/migrate", json={"path": str(old)})

    # 路径不符 → 拒绝
    r = client.post("/api/upgrade/cleanup", json={"path": "/etc"})
    assert r.status_code == 400

    # 正确路径（默认用记录值）→ 移入废纸篓 + 记录清空
    r = client.post("/api/upgrade/cleanup", json={})
    assert r.json()["ok"] and trashed == [str(old)]
    r2 = client.post("/api/upgrade/cleanup", json={})
    assert r2.status_code == 400          # 记录已清空，不能重复清理


def test_cleanup_refuses_if_library_still_there(fresh_env, client, monkeypatch):
    """旧目录里还有 miying.sqlite（没搬空/用户又用旧版写了数据）→ 绝不动它。"""
    from app.core import osplat
    monkeypatch.setattr(osplat, "move_to_trash", lambda p: (_ for _ in ()).throw(AssertionError("不该被调用")))
    old = _make_old_install(fresh_env, n_assets=2)
    client.post("/api/upgrade/migrate", json={"path": str(old)})
    # 模拟旧目录死灰复燃：又出现一个库文件
    (old / "app" / "out" / "miying.sqlite").write_bytes(b"resurrected")
    r = client.post("/api/upgrade/cleanup", json={})
    assert r.status_code == 409
