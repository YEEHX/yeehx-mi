"""一期优化 · 阶段2/3 + 体检页 + 局域网访问 回归测试。
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
    d.mkdir(parents=True, exist_ok=True)
    img = np.full((240, 320, 3), 90, np.uint8)
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


# ── 2-1 FTS 炸掉：降级 LIKE + 标记，不再静默变少 ───────────────────────
def test_fts_failure_degrades_not_silently(client, tmp_path_factory):
    from app import db
    root = tmp_path_factory.mktemp("fts盘") / "测试夹"
    _mk(root, "雪山航拍素材")
    client.get("/api/fs", params={"dir": str(root)})
    _wait(client)
    if not db.FTS_OK:
        pytest.skip("环境无 FTS5，本测试只验证 FTS 模式")
    db.write(lambda c: c.execute("DROP TABLE assets_fts"))      # 人为弄坏
    try:
        r = client.post("/api/search", json={"q": "雪山", "with_facets": False}).json()
        assert r.get("fts_degraded") is True
        assert any("雪山" in a["name"] for a in r["assets"]), "LIKE 兜底必须仍有结果"
    finally:
        db.init_db()                                            # 重建 FTS 表


# ── 2-2 指纹失败留痕 + 体检页重试 ──────────────────────────────────────
def test_fingerprint_error_recorded_and_retryable(client, tmp_path_factory):
    from app import db
    root = tmp_path_factory.mktemp("坏文件盘") / "权限夹"
    bad = _mk(root, "读不了")
    os.chmod(bad, 0o000)
    try:
        client.get("/api/fs", params={"dir": str(root)})
        _wait(client)
        chk = client.get("/api/checkup").json()
        assert chk["fingerprints"]["errors"] >= 1, "算不出指纹必须留痕，不能静默"
    finally:
        os.chmod(bad, 0o644)
    r = client.post("/api/checkup/retry_fingerprints").json()
    assert r["count"] >= 1
    _wait(client)
    conn = db.connect()
    row = conn.execute("SELECT content_id, facts_json FROM assets WHERE name='读不了.jpg'").fetchone()
    assert row["content_id"], "修复权限后重试应算出指纹"
    assert "fingerprint_error" not in (row["facts_json"] or ""), "成功后错误标记应清除"


# ── 2-3 打标重试分级 ───────────────────────────────────────────────────
def test_fine_tag_retry_tiering(client, tmp_path_factory, monkeypatch):
    from app.scan import tagging
    from app.core import assets as A

    root = tmp_path_factory.mktemp("重试盘") / "重试夹"
    _mk(root, "重试素材")
    d = client.get("/api/fs", params={"dir": str(root)}).json()
    _wait(client)
    aid = d["assets"][0]["asset_id"]

    # 临时性错误：退避 2s/4s 后第三次成功
    calls, delays = {"n": 0}, []
    monkeypatch.setattr(tagging.time, "sleep", lambda s: delays.append(s))

    def flaky(asset):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("HTTP 503: model busy")
        return {"描述": "ok", "keywords": []}, {}

    monkeypatch.setattr(tagging, "_run_ai", flaky)
    monkeypatch.setattr(tagging, "_apply_ai_result", lambda *a, **k: None)
    tagging._h_fine_tag({}, aid)
    assert calls["n"] == 3 and delays == [2, 4]
    assert A.get(aid)["status"] == "tagged"

    # 永久性错误：一次都不重试
    calls["n"] = 0

    def offline(asset):
        calls["n"] += 1
        raise RuntimeError("卷离线")

    monkeypatch.setattr(tagging, "_run_ai", offline)
    with pytest.raises(RuntimeError):
        tagging._h_fine_tag({}, aid)
    assert calls["n"] == 1, "卷离线必须立即停，不浪费模型调用"
    A.set_fields(aid, status="tagged")


# ── 3-1/3-2/3-3 白名单 ─────────────────────────────────────────────────
def test_fs_browse_whitelist(client):
    assert client.get("/api/fs", params={"dir": "/etc"}).status_code == 403
    assert client.get("/api/fs", params={"dir": "/usr/local"}).status_code == 403


def test_ref_file_whitelist(client):
    t = client.post("/api/tags", json={"name": "白名单测试词", "category": "内容"}).json()
    r = client.post(f"/api/tag/{t['id']}/ref_file", json={"path": "/etc/passwd"})
    assert r.status_code == 400      # 先挡扩展名
    r = client.post(f"/api/tag/{t['id']}/ref_file", json={"path": "/etc/evil.jpg"})
    assert r.status_code == 403      # 再挡范围
    client.delete(f"/api/tag/{t['id']}")


def test_export_target_whitelist(client):
    r = client.post("/api/export", json={"type": "original", "targets": [], "target": "/usr/local/x"})
    assert r.status_code == 403
    r = client.post("/api/ai_export", json={"type": "original", "tag_ids": [], "target": "/usr/local/x"})
    assert r.status_code == 403


# ── 局域网访问判定（纯函数单测） ───────────────────────────────────────
def test_lan_request_rules():
    from types import SimpleNamespace
    from app.main import _lan_request_allowed as allow

    off = SimpleNamespace(lan_access=False, lan_token="abc123")
    assert allow("192.168.1.5", "abc123", None, off)[1] == 403       # 未开启一律拒

    on = SimpleNamespace(lan_access=True, lan_token="abc123")
    assert allow("evil.com", "abc123", None, on)[1] == 403           # 域名 Host 拒（防 rebinding）
    assert allow("192.168.1.5", "wrong", None, on)[1] == 401         # 口令错
    assert allow("192.168.1.5", "", None, on)[1] == 401              # 没口令
    assert allow("192.168.1.5", "abc123", None, on)[0] is True       # 私网 + 口令对 → 放行
    assert allow("10.0.0.8", "abc123", "evil.com", on)[1] == 403     # 写操作跨站 Origin 拒
    assert allow("10.0.0.8", "abc123", "192.168.1.5", on)[0] is True
    empty = SimpleNamespace(lan_access=True, lan_token="")
    assert allow("192.168.1.5", "", None, empty)[1] == 401           # 无口令配置=拒，绝不裸奔


def test_lan_settings_toggle(client):
    r = client.post("/api/settings/app", json={"lan_access": True}).json()
    assert r["app"]["lan_access"] is True
    s = client.get("/api/settings").json()
    assert s["lan"]["enabled"] and len(s["lan"]["token"]) >= 8
    tok1 = s["lan"]["token"]
    client.post("/api/settings/app", json={"regen_lan_token": 1})
    assert client.get("/api/settings").json()["lan"]["token"] != tok1
    client.post("/api/settings/app", json={"lan_access": False})
    assert client.get("/api/settings").json()["app"]["lan_access"] is False


# ── 4-1 体检页 + 清理悬空引用 ──────────────────────────────────────────
def test_checkup_shape_and_clean_dangling(client, tmp_path_factory):
    from app.core import assets as A
    root = tmp_path_factory.mktemp("体检盘") / "体检夹"
    _mk(root, "体检素材")
    d = client.get("/api/fs", params={"dir": str(root)}).json()
    _wait(client)
    aid = d["assets"][0]["asset_id"]
    A.set_fields(aid, thumb_path="不存在的文件.jpg")          # 制造悬空引用

    chk = client.get("/api/checkup").json()
    assert {"assets_total", "volumes", "thumbs", "fingerprints", "candidates", "model",
            "tasks_active", "recent_errors"} <= set(chk)
    assert chk["thumbs"]["dangling"] >= 1
    assert chk["candidates"]["pending"] <= chk["candidates"]["total"]   # 口径修正：分开统计

    r = client.post("/api/checkup/clean_dangling").json()
    assert r["cleaned"] >= 1
    assert client.get("/api/checkup").json()["thumbs"]["dangling"] == 0
    assert not A.get(aid)["thumb_path"]


# ── 打标 prompt：标签库前置（吃 ollama 前缀缓存，v1.8.1 提速修复） ─────
def test_prompt_static_library_comes_first():
    from app.ai import prompt as P
    txt = P.build_user_text("黄鹤楼", "无人机航拍",
                            context={"filename": "DJI_0001.MP4"},
                            tag_library={"地点": ["黄鹤楼"], "拍法": ["航拍"]})
    assert txt.index("已有标签库") < txt.index("路径推测地点"), "静态大块必须在变项之前"
    assert txt.index("已有标签库") < txt.index("素材上下文")
    # 同一批内词表序列化必须字节级一致，前缀缓存才有效
    txt2 = P.build_user_text("江汉关", None, context={"filename": "B.mp4"},
                             tag_library={"地点": ["黄鹤楼"], "拍法": ["航拍"]})
    lib_line = txt.split("\n")[0]
    assert txt2.startswith(lib_line)


# ── LAN 设置带监听自检字段 ─────────────────────────────────────────────
def test_lan_settings_reports_listening(client):
    client.post("/api/settings/app", json={"lan_access": True})
    s = client.get("/api/settings").json()
    assert "listening" in s["lan"] and s["lan"]["listening"] in (True, False, None)
    client.post("/api/settings/app", json={"lan_access": False})
    assert client.get("/api/settings").json()["lan"]["listening"] is None


# ── 手动改 AI 描述：保存锁定 / 清空交还（v1.8.2） ───────────────────────
def test_manual_desc_lock_and_release(client, tmp_path_factory):
    from app.core import assets as A
    root = tmp_path_factory.mktemp("描述盘") / "描述夹"
    _mk(root, "描述素材")
    d = client.get("/api/fs", params={"dir": str(root)}).json()
    _wait(client)
    aid = d["assets"][0]["asset_id"]

    r = client.post(f"/api/asset/{aid}/desc", json={"desc_ai": "导演手写的描述", "manual": True})
    assert r.status_code == 200
    A.set_description(aid, desc_ai="AI 想覆盖", manual=False)      # 模拟重新打标
    assert client.get(f"/api/asset/{aid}").json()["desc_ai"] == "导演手写的描述", "手动描述必须锁住"

    client.post(f"/api/asset/{aid}/desc", json={"desc_ai": "", "manual": True})   # 清空=交还
    A.set_description(aid, desc_ai="AI 重新写的", manual=False)
    assert client.get(f"/api/asset/{aid}").json()["desc_ai"] == "AI 重新写的", "清空后 AI 应可再写"


# ── 重置数据库后任务泵必须立即可用（用户实测卡死回归，放最后跑） ────────
def test_reset_db_then_pipeline_runs(client, tmp_path_factory):
    """v1.8.0 及之前：reset_db 只关当前线程连接，任务泵线程攥着已删除旧库
    的句柄（幽灵库），重置后新建的任务它永远看不见——快扫 0/N 卡死到重启。
    修复后：连接按代数判废，泵下一次轮询自动连上新库。"""
    from app.config import get_cfg
    cfg = get_cfg()
    cfg.thumbs_dir.mkdir(parents=True, exist_ok=True)
    (cfg.thumbs_dir / "orphan_after_reset.jpg").write_bytes(b"\xff\xd8fake")
    r = client.post("/api/db/reset", json={"confirm": "YEEHX", "wipe_thumbs": True})
    assert r.status_code == 200 and r.json()["thumbs_wiped"] >= 1
    assert not [p for p in cfg.thumbs_dir.glob("*") if p.is_file()], "勾选后缩略图孤儿文件应一并清掉"
    root = tmp_path_factory.mktemp("重置后盘") / "新库素材"
    _mk(root, "重生")
    r = client.post("/api/scan/quick_import", json={"root_path": str(root)})
    assert r.status_code == 200
    _wait(client, timeout=60)        # 修复前这里会卡满超时
    d = client.get("/api/fs", params={"dir": str(root)}).json()
    assert d["assets"], "重置后扫描必须真的登记素材"
    assert d["assets"][0]["thumb"], "生图任务必须被泵捡起并完成"


def test_reset_never_exposes_half_built_db(client):
    """重置的毫秒窗口回归：旧版先切代数再建表，其他线程会撞上 no such table，
    任务泵线程因此猝死（用户实测：重置后生图正常、打标永远不来，重启才好）。
    修复后：新库建完才切代数，并发读永远看到完整 schema。"""
    import threading
    from app import db

    errors: list[str] = []
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            try:
                db.connect().execute("SELECT COUNT(*) FROM tasks").fetchone()
            except Exception as exc:
                errors.append(repr(exc))
                return

    threads = [threading.Thread(target=hammer, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        for _ in range(12):
            assert client.post("/api/db/reset", json={"confirm": "YEEHX"}).status_code == 200
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)
    assert not errors, f"并发读在重置期间撞到了半成品库：{errors[:2]}"


def test_pump_survives_next_task_exception(client, tmp_path_factory):
    """确定性回归：泵线程轮询抛异常（如重置竞态）也绝不能死。
    旧版 _next_task 裸在 try 外，抛一次线程就没了——打标/生图从此瘫到重启。"""
    from app.tasks import manager

    orig = manager.TaskManager._next_task
    state = {"boom": 3}

    def flaky(self, queue):
        if queue == "thumb" and state["boom"] > 0:
            state["boom"] -= 1
            raise RuntimeError("模拟重置竞态：no such table: tasks")
        return orig(self, queue)

    manager.TaskManager._next_task = flaky
    try:
        root = tmp_path_factory.mktemp("泵活盘") / "泵活夹"
        _mk(root, "泵活素材")
        client.post("/api/scan/quick_import", json={"root_path": str(root)})
        _wait(client, timeout=60)    # 旧泵：第一次异常线程即死，这里会卡满超时
        d = client.get("/api/fs", params={"dir": str(root)}).json()
        assert d["assets"] and d["assets"][0]["thumb"], "泵被异常打过后必须还活着干完活"
        assert state["boom"] == 0, "异常注入应已全部触发"
    finally:
        manager.TaskManager._next_task = orig
