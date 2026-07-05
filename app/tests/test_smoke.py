"""冒烟测试：YEEHX_MOCK=1 下走通 扫描→生图→打标→搜索→导出 主流程。
跑法（仓库根目录）：
    YEEHX_MOCK=1 python3 -m pytest app/tests -q
不连模型、不碰真实 out/（conftest 已重定向数据目录）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.filterwarnings("ignore")


# ── 基座 ──────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def client():
    from app.main import app
    with TestClient(app) as c:   # 触发 startup：建库、种标签、起任务泵
        yield c


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("素材盘")
    sub = d / "2026武汉" / "黄鹤楼航拍"
    sub.mkdir(parents=True)
    for i in range(3):
        img = np.full((240, 320, 3), 40 + i * 50, np.uint8)
        cv2.putText(img, f"F{i}", (40, 120), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
        cv2.imwrite(str(sub / f"DSC0{i}.jpg"), img)
    return d


def wait_tasks(client, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = client.get("/api/tasks").json()
        if not s["active"]:
            return
        time.sleep(0.3)
    raise AssertionError(f"任务超时未完成: {client.get('/api/tasks').json()}")


def _assets_of(client, media_dir: Path) -> list[dict]:
    sub = media_dir / "2026武汉" / "黄鹤楼航拍"
    d = client.get("/api/fs", params={"dir": str(sub)}).json()
    return d["assets"]


# ── 1. 健康 / 版本 ────────────────────────────────────────────────────
def test_health_version(client):
    h = client.get("/api/health").json()
    assert h["ok"] and h["app"] == "觅影"
    assert h.get("version")


# ── 2. 种子标签 ───────────────────────────────────────────────────────
def test_seed_tags(client):
    d = client.get("/api/tags").json()
    cats = [c["name"] for c in d["categories"]]
    assert "地点" in cats and "拍法" in cats
    names = [t["name"] for t in d["tags"]]
    assert "航拍" in names
    assert "城市天际线" in names


# ── 3. 浏览即登记（指纹防重复登记） ───────────────────────────────────
def test_fs_browse_indexes(client, media_dir):
    sub = media_dir / "2026武汉" / "黄鹤楼航拍"
    d = client.get("/api/fs", params={"dir": str(sub)}).json()
    assert len(d["assets"]) == 3
    # 再开一次：指纹没变不应重复登记（行为相同，数量不变）
    d2 = client.get("/api/fs", params={"dir": str(sub)}).json()
    assert len(d2["assets"]) == 3


# ── 4. 全量扫描 → 生图 → mock 打标 ───────────────────────────────────
def test_quick_import_full_pipeline(client, media_dir):
    r = client.post("/api/scan/quick_import",
                    json={"root_path": str(media_dir), "mode": "full"}).json()
    assert r["ok"] and r["folders"] >= 1
    wait_tasks(client)
    assets = _assets_of(client, media_dir)
    assert len(assets) == 3
    assert all(a["thumb"] for a in assets), "缩略图应全部生成"
    detail = client.get(f"/api/asset/{assets[0]['asset_id']}").json()
    assert detail["status"] == "tagged"
    assert detail["desc_ai"].startswith("[MOCK]")


# ── 5. 搜索 / 分面 / 排序分页 ─────────────────────────────────────────
def test_search_facets_sort_pagination(client, media_dir):
    res = client.post("/api/search", json={"q": "DSC00"}).json()
    assert res["total"] >= 1
    page1 = client.post("/api/search", json={"limit": 2, "offset": 0, "sort": "name",
                                             "with_facets": False}).json()
    page2 = client.post("/api/search", json={"limit": 2, "offset": 2, "sort": "name",
                                             "with_facets": False}).json()
    assert page1["total"] == page2["total"] >= 3
    ids1 = {a["asset_id"] for a in page1["assets"]}
    ids2 = {a["asset_id"] for a in page2["assets"]}
    assert not ids1 & ids2, "分页不应重叠"
    f = client.get("/api/facets").json()
    assert f["total"] >= 3


# ── 6. 文件夹挂标签 → 继承生效 ────────────────────────────────────────
def test_apply_folder_tags_inheritance(client, media_dir):
    sub = media_dir / "2026武汉" / "黄鹤楼航拍"
    r = client.post("/api/apply", json={
        "targets": [{"type": "path", "path": str(sub)}],
        "op": "add_tags", "names": "测试夹标", "category": "内容",
    }).json()
    assert r["ok"]
    assets = _assets_of(client, media_dir)
    tags = [t["name"] for t in assets[0]["tags"]]
    assert "测试夹标" in tags
    res = client.post("/api/search", json={"q": "测试夹标"}).json()
    assert res["total"] >= 3


# ── 7. 待复核：普通开放词不刷屏，明确地标仍可复核 ──────────────────────
def test_candidates_have_confidence(client):
    d = client.get("/api/candidates").json()
    terms = [c["term"] for c in d["candidates"]]
    assert "测试候选词" not in terms
    assert "黄鹤楼" in terms
    c = next(c for c in d["candidates"] if c["term"] == "黄鹤楼")
    assert "confidence" in c and c["confidence"] >= 0.6
    assert "reason" in c


# ── 8. 清单导出：地点类目正确落 city/places 列 ────────────────────────
def test_export_manifest_places(client, media_dir, tmp_path):
    assets = _assets_of(client, media_dir)
    aid = assets[0]["asset_id"]
    client.post(f"/api/asset/{aid}/tags", json={"names": "武汉", "category": "地点"})
    target = tmp_path / "导出"
    r = client.post("/api/export", json={"type": "manifest", "asset_ids": [aid],
                                         "target": str(target), "options": {"format": "json"}}).json()
    assert r["ok"]
    wait_tasks(client)
    files = list(target.glob("清单_*.json"))
    assert files, "应产出 JSON 清单"
    data = json.loads(files[0].read_text(encoding="utf-8"))
    row = data["assets"][0]
    assert row["city"] == "武汉"
    assert "武汉" in row["places"]
    # premiere 格式 = txt 路径清单
    r2 = client.post("/api/export", json={"type": "manifest", "asset_ids": [aid],
                                          "target": str(target), "options": {"format": "premiere"}}).json()
    assert r2["ok"]
    wait_tasks(client)
    assert list(target.glob("清单_*.txt")), "premiere 应产出 txt 路径清单"


# ── 9. 重复检测：同内容文件分组 ──────────────────────────────────────
def test_duplicates(client, media_dir):
    sub = media_dir / "2026武汉" / "黄鹤楼航拍"
    dup_dir = media_dir / "备份盘副本"
    dup_dir.mkdir(exist_ok=True)
    (dup_dir / "拷贝_DSC00.jpg").write_bytes((sub / "DSC00.jpg").read_bytes())
    client.post("/api/scan/quick_import", json={"root_path": str(dup_dir), "mode": "quick"})
    wait_tasks(client)
    d = client.get("/api/duplicates").json()
    assert d["groups"], "应发现至少一组重复"
    assert any(g["count"] >= 2 for g in d["groups"])


# ── 10. 改名不丢标签（content_id 搬家识别） ──────────────────────────
def test_rename_keeps_tags(client, media_dir):
    sub = media_dir / "2026武汉" / "黄鹤楼航拍"
    assets = _assets_of(client, media_dir)
    victim = next(a for a in assets if a["name"] == "DSC01.jpg")
    client.post(f"/api/asset/{victim['asset_id']}/tags",
                json={"names": "保命标签", "category": "内容"})
    os.rename(sub / "DSC01.jpg", sub / "改名后的素材.jpg")
    client.post("/api/scan/quick_import", json={"root_path": str(sub), "mode": "quick"})
    wait_tasks(client)
    assets2 = _assets_of(client, media_dir)
    renamed = [a for a in assets2 if a["name"] == "改名后的素材.jpg"]
    assert renamed, "改名后的文件应在库里"
    tags = [t["name"] for t in renamed[0]["tags"]]
    assert "保命标签" in tags, "改名后标签必须保留（README 承诺）"
    assert all(a["name"] != "DSC01.jpg" for a in assets2), "旧记录不应残留"


# ── 11. 测试连接不落盘 ───────────────────────────────────────────────
def test_ping_does_not_save_settings(client):
    from app.config import get_cfg
    cfg = get_cfg()
    before = cfg.model_settings_path.read_text() if cfg.model_settings_path.exists() else None
    client.post("/api/ping", json={"provider": "api", "api_base_url": "http://127.0.0.1:9",
                                   "api_key": "junk", "api_model": "x"})
    after = cfg.model_settings_path.read_text() if cfg.model_settings_path.exists() else None
    assert before == after, "测试连接不应保存配置"
    assert client.get("/api/settings").json()["model"]["provider"] == "local"


# ── 12. 批量 zip 下载 ────────────────────────────────────────────────
def test_download_zip(client, media_dir):
    assets = _assets_of(client, media_dir)
    ids = [a["asset_id"] for a in assets[:2]]
    r = client.post("/api/assets/download_zip", json={"asset_ids": ids})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert len(r.content) > 200


# ── 13. AI 自然语言搜索（mock 解析）+ 追问叠加 ───────────────────────
def test_ai_search_and_follow_up(client):
    d = client.post("/api/ai_search", json={"q": "找有 测试夹标 的素材"}).json()
    assert "测试夹标" in (d["parsed"]["tags"] or [])
    assert d["total"] >= 3
    follow = client.post("/api/ai_search",
                         json={"q": "再加上 航拍 的", "base_tag_ids": d["parsed"]["tag_ids"]}).json()
    assert len(follow["parsed"]["tag_ids"]) >= 2, "追问应在已选标签上叠加"


# ── 14. 导出支持文件夹 target ────────────────────────────────────────
def test_export_folder_target(client, media_dir, tmp_path):
    sub = media_dir / "2026武汉" / "黄鹤楼航拍"
    target = tmp_path / "导出夹"
    r = client.post("/api/export", json={"type": "manifest", "targets": [{"type": "path", "path": str(sub)}],
                                         "target": str(target), "options": {"format": "json"}}).json()
    assert r["ok"] and r["count"] >= 3, "文件夹应展开为其下全部素材"
    wait_tasks(client)
    files = sorted(target.glob("清单_*.json"))
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    assert data["count"] >= 3


# ── 15. 重复检测：忽略组 / 恢复 ──────────────────────────────────────
def test_duplicates_ignore(client):
    d = client.get("/api/duplicates").json()
    assert d["groups"]
    cid = d["groups"][0]["content_id"]
    client.post("/api/duplicates/ignore", json={"content_id": cid})
    d2 = client.get("/api/duplicates").json()
    assert all(g["content_id"] != cid for g in d2["groups"])
    assert d2["ignored_count"] >= 1
    d3 = client.get("/api/duplicates", params={"include_ignored": 1}).json()
    assert any(g["content_id"] == cid for g in d3["groups"])
    client.post("/api/duplicates/ignore", json={"content_id": cid, "restore": True})
    d4 = client.get("/api/duplicates").json()
    assert any(g["content_id"] == cid for g in d4["groups"])


# ── 16. 锁定分面可筛 + 解锁 ──────────────────────────────────────────
def test_locked_facet(client, media_dir):
    assets = _assets_of(client, media_dir)
    aid = assets[-1]["asset_id"]
    client.post("/api/apply", json={"targets": [{"type": "asset", "id": aid}], "op": "lock", "locked": 1})
    f = client.get("/api/facets").json()
    assert f["facets"]["locked"] and f["facets"]["locked"][0]["count"] >= 1
    res = client.post("/api/search", json={"facets": {"locked": ["1"]}, "with_facets": False}).json()
    assert any(a["asset_id"] == aid for a in res["assets"])
    client.post("/api/apply", json={"targets": [{"type": "asset", "id": aid}], "op": "lock", "locked": 0})
    res2 = client.post("/api/search", json={"facets": {"locked": ["1"]}, "with_facets": False}).json()
    assert all(a["asset_id"] != aid for a in res2["assets"])


# ── 17. 清除打标（全清 own 标签，文件夹继承保留；锁定跳过） ──────────
def test_clear_tagging(client, media_dir):
    assets = _assets_of(client, media_dir)
    victim = assets[0]
    locked_one = assets[1]
    client.post(f"/api/asset/{victim['asset_id']}/tags", json={"names": "临时标签", "category": "内容"})
    client.post("/api/apply", json={"targets": [{"type": "asset", "id": locked_one["asset_id"]}],
                                    "op": "lock", "locked": 1})
    r = client.post("/api/apply", json={
        "targets": [{"type": "asset", "id": victim["asset_id"]},
                    {"type": "asset", "id": locked_one["asset_id"]}],
        "op": "clear_tagging", "with_thumbs": True,
    }).json()
    assert r["cleared"] == 1 and r["locked_skipped"] == 1
    d = client.get(f"/api/asset/{victim['asset_id']}").json()
    names = [t["name"] for t in d["effective"]["tags"]]
    assert "临时标签" not in names, "own 标签应清掉"
    assert "测试夹标" in names, "文件夹继承标签应保留"
    assert d["status"] == "pending" and not d["thumb"] and not d["desc_ai"]
    client.post("/api/apply", json={"targets": [{"type": "asset", "id": locked_one["asset_id"]}],
                                    "op": "lock", "locked": 0})


# ── 17b. 清除整棵树（根目录 path 目标）：连文件夹层标签一起清、封面不残留 ──
def test_clear_folder_target_full_tree(client, tmp_path_factory):
    root = tmp_path_factory.mktemp("清除树")
    sub = root / "深层" / "夹A"
    sub.mkdir(parents=True)
    for i in range(2):
        img = np.full((200, 280, 3), 90 + i * 30, np.uint8)
        cv2.imwrite(str(sub / f"CL{i}.jpg"), img)
    client.post("/api/scan/quick_import", json={"root_path": str(root), "mode": "full"})
    wait_tasks(client)
    # 给文件夹挂继承标签 + 给素材挂手动标签
    client.post("/api/apply", json={"targets": [{"type": "path", "path": str(sub)}],
                                    "op": "add_tags", "names": "树继承标", "category": "内容"})
    d = client.get("/api/fs", params={"dir": str(sub)}).json()
    aid = d["assets"][0]["asset_id"]
    client.post(f"/api/asset/{aid}/tags", json={"names": "树手动标", "category": "内容"})
    # 以"根目录 path 目标"清除（复现根硬盘卡片场景）
    r = client.post("/api/apply", json={"targets": [{"type": "path", "path": str(root)}],
                                        "op": "clear_tagging", "with_thumbs": True}).json()
    assert r["cleared"] >= 2, "path 形态的根目标必须能清（旧版静默跳过）"
    assert r["folder_tags_cleared"] >= 1, "文件夹层标签应一并清掉"
    detail = client.get(f"/api/asset/{aid}").json()
    assert not detail["effective"]["tags"], "继承+手动标签应全部清空"
    assert detail["status"] == "pending" and not detail["thumb"]
    # 文件夹卡片不应再给出指向已删文件的封面（破图横框）
    d2 = client.get("/api/fs", params={"dir": str(root / "深层")}).json()
    for card in d2["subdirs"]:
        assert not card.get("cover"), f"封面应清空，实际: {card.get('cover')}"


# ── 19. AI 指令式导出：范围+条件+动作解析 → 确认执行 → 原素材零改动 ──
def test_ai_command_export_copies_only(client, media_dir, tmp_path):
    sub = media_dir / "2026武汉" / "黄鹤楼航拍"
    before = {p.name: (p.stat().st_size, p.stat().st_mtime) for p in sub.iterdir() if p.is_file()}
    target = tmp_path / "AI导出区"
    # 解析（mock 规则：在…里 → scope；导出到… → action=export+target；标签子串匹配。
    # 范围名用 素材盘，避免和标签词（航拍/武汉）子串撞车）
    d = client.post("/api/ai_search", json={"q": f"在 素材盘 里找 测试夹标 导出到 {target}"}).json()
    p = d["parsed"]
    assert p["action"] == "export"
    assert p["scope"] and "素材盘" in p["scope"]["label"]
    assert "测试夹标" in p["tags"]
    assert str(target) in p["export"]["target"]
    assert d["total"] >= 3, "范围+标签应命中该文件夹素材"
    # 确认执行（前端确认框点确认后调的接口）
    r = client.post("/api/ai_export", json={"tag_ids": p["tag_ids"], "keywords": p["keywords"],
                                            "scope": p["scope"], "type": "original",
                                            "target": str(target)}).json()
    assert r["ok"] and r["count"] >= 3
    wait_tasks(client)
    exported = [f for f in target.iterdir() if f.suffix == ".jpg"]
    assert len(exported) >= 3, "目标文件夹应有复制出的文件"
    # 铁律验证：原始素材一个字节都不能动
    after = {p.name: (p.stat().st_size, p.stat().st_mtime) for p in sub.iterdir() if p.is_file()}
    assert before == after, "原始素材的大小/修改时间必须完全不变"
    # 相对路径目标必须拒绝
    bad = client.post("/api/ai_export", json={"tag_ids": p["tag_ids"], "keywords": "",
                                              "scope": p["scope"], "type": "original",
                                              "target": "相对路径不行"})
    assert bad.status_code == 400


# ── 20. 关于页数据：品牌配置 + 版本 + 更新日志 ───────────────────────
def test_brand_endpoint(client):
    d = client.get("/api/brand").json()
    assert d["version"]
    assert d["brand"].get("copy_id") == "玩椰"
    assert any(l.get("url", "").startswith("https://") for l in d["brand"].get("links", []))
    assert "##" in d["changelog"] or d["changelog"], "应返回最近更新内容"


# ── 21. 观察态：开放词出现满 3 次才进待复核，期间可见可清 ────────────
def test_watching_promotion(client):
    from app.core import candidates as C
    C.add("观察词X", "内容", asset_id="a_fake1", confidence=0.9, min_hits=3)
    d = client.get("/api/candidates").json()
    assert all(c["term"] != "观察词X" for c in d["candidates"]), "不满次数不该进待复核"
    assert any(i["term"] == "观察词X" for i in d["watching"]["items"]), "观察区应可见"
    C.add("观察词X", "内容", asset_id="a_fake2", confidence=0.9, min_hits=3)
    C.add("观察词X", "内容", asset_id="a_fake3", confidence=0.9, min_hits=3)
    d2 = client.get("/api/candidates").json()
    assert any(c["term"] == "观察词X" for c in d2["candidates"]), "满 3 次应晋升待复核"


# ── 22. 低频标签瘦身：固定词表受保护，孤词可批量删 ───────────────────
def test_tags_prune(client):
    client.post("/api/tags", json={"name": "孤词标签", "category": "内容"})
    low = client.get("/api/tags/lowuse", params={"max_uses": 0}).json()
    names = [t["name"] for t in low["tags"]]
    assert "孤词标签" in names
    assert "航拍" not in names and "城市天际线" not in names, "固定词表不该出现在瘦身列表"
    tid = next(t["id"] for t in low["tags"] if t["name"] == "孤词标签")
    r = client.post("/api/tags/prune", json={"tag_ids": [tid]}).json()
    assert r["deleted"] == 1
    assert "孤词标签" not in [t["name"] for t in client.get("/api/tags").json()["tags"]]


# ── 23. 自动 LUT：容器级识别（R3D/索尼），手动「直出」永远优先 ───────
def test_auto_lut_detection(tmp_path):
    from app.media import thumbnails as TH
    sony = tmp_path / "a.MP4"
    sony.write_bytes(b"\x00" * 64 + b'<item name="CaptureGammaEquation" value="s-log3-cine"/>' + b"\x00" * 64)
    assert TH.detect_auto_lut(sony, "video") == "S-Log3"
    rec = tmp_path / "b.mp4"
    rec.write_bytes(b'<item name="CaptureGammaEquation" value="rec709"/>')
    assert TH.detect_auto_lut(rec, "video") == "", "rec709 直出必须明确不套"
    red = tmp_path / "c.R3D"
    red.write_bytes(b"x")
    assert TH.detect_auto_lut(red, "red") == "RED Log3G10"
    dji = tmp_path / "DJI_xxx_D.MP4"
    dji.write_bytes(b"\x00" * 256)
    assert TH.detect_auto_lut(dji, "video") == "", "无信号不猜（文件名 _D 不可信已实测证伪）"


def test_auto_lut_manual_override(client, media_dir):
    from app.media import thumbnails as TH
    from app.core import assets as A
    assets = _assets_of(client, media_dir)
    aid = assets[0]["asset_id"]
    # 手动「直出」(__none__) 必须压过自动这一级
    client.post("/api/apply", json={"targets": [{"type": "asset", "id": aid}],
                                    "op": "set_lut", "lut": "__none__"})
    assert TH.effective_lut(A.get(aid)) == "__none__"
    # 开关在设置接口可读可写
    s = client.get("/api/settings").json()
    assert "auto_lut" in s["app"]
    client.post("/api/settings/app", json={"auto_lut": False})
    assert client.get("/api/settings").json()["app"]["auto_lut"] is False
    client.post("/api/settings/app", json={"auto_lut": True})


# ── 18. 按表单列模型不落盘 ───────────────────────────────────────────
def test_models_list_no_save(client):
    from app.config import get_cfg
    cfg = get_cfg()
    before = cfg.model_settings_path.read_text() if cfg.model_settings_path.exists() else None
    d = client.post("/api/models/list", json={"provider": "api", "api_base_url": "http://127.0.0.1:9",
                                              "api_key": "junk"}).json()
    assert d["ok"] is False
    after = cfg.model_settings_path.read_text() if cfg.model_settings_path.exists() else None
    assert before == after
    assert client.get("/api/settings").json()["model"]["provider"] == "local"
