"""换一帧（手动选代表帧）回归测试：
候选帧抽取 → 选定写回 rep_ts → 缩略图/下载跟随 → 候选零残留 → 照片拒绝。
跑法：YEEHX_MOCK=1 python3 -m pytest app/tests/test_reframe.py -q
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def _mk_video(path: Path, dur: int = 3):
    """用 ffmpeg lavfi testsrc 造一段可解码的真视频（每帧画面不同，便于换帧）。"""
    from app.media import frames
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [frames.ffmpeg_exe(), "-y", "-v", "error",
           "-f", "lavfi", "-i", f"testsrc=duration={dur}:size=320x240:rate=10",
           "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, capture_output=True, timeout=120)
    assert path.exists() and path.stat().st_size > 0, "测试视频生成失败（ffmpeg 不可用？）"


def _video_asset(client, tmp_path_factory) -> str:
    """造视频 → 浏览登记 → 直接生缩略图（拿到自动 rep_ts），返回 asset_id。"""
    from app.media import thumbnails
    root = tmp_path_factory.mktemp("换帧盘") / "片段"
    _mk_video(root / "clip.mp4")
    d = client.get("/api/fs", params={"dir": str(root)}).json()
    aid = next(x for x in d["assets"] if x["name"] == "clip.mp4")["asset_id"]
    assert thumbnails.generate(aid)["ok"], "初始缩略图应生成成功"
    return aid


def test_reframe_flow(client, tmp_path_factory):
    from app.core import assets as A
    from app.config import get_cfg

    aid = _video_asset(client, tmp_path_factory)
    a0 = A.get(aid)
    assert a0["kind"] == "video"
    ts0 = (a0.get("facts") or {}).get("rep_ts")
    assert ts0 is not None, "视频缩略图应记录 rep_ts"

    # 候选帧：均匀铺开、时间升序、落在隔离的 out/cand/<aid>/ 目录
    cj = client.get(f"/api/asset/{aid}/frame_candidates", params={"n": 8}).json()
    assert cj["ok"] and len(cj["candidates"]) >= 5
    tss = [c["ts"] for c in cj["candidates"]]
    assert tss == sorted(tss), "候选应按时间升序"
    cand_dir = get_cfg().out_dir / "cand" / aid
    assert cand_dir.exists() and any(cand_dir.iterdir()), "候选图应已落盘"
    url0 = cj["candidates"][0]["url"]
    assert client.get(url0).status_code == 200, "候选图应能通过 /cand 路由访问（前端 <img> 依赖它）"

    # 选一个明显不同于当前的帧 → 写回 rep_ts、重生成、清候选
    target = next(c for c in cj["candidates"] if abs(c["ts"] - (ts0 or 0)) > 0.2)
    assert client.post(f"/api/asset/{aid}/reframe", json={"ts": target["ts"]}).json()["ok"]

    a1 = A.get(aid)
    assert abs(a1["facts"]["rep_ts"] - target["ts"]) < 0.01, "rep_ts 应写成选定帧"
    assert a1["thumb_path"], "换帧后仍有缩略图"
    assert not cand_dir.exists(), "选定后候选应清空（零残留）"
    assert client.get(url0).status_code == 404, "选定后候选 URL 应失效（已清，零残留）"

    # 下载走 rep_ts —— 200 + 图片即说明用新帧抽了原分辨率底图
    dl = client.get(f"/api/asset/{aid}/download")
    assert dl.status_code == 200 and dl.headers["content-type"].startswith("image/")
    assert len(dl.content) > 0


def test_reframe_cancel_clears(client, tmp_path_factory):
    from app.config import get_cfg
    aid = _video_asset(client, tmp_path_factory)
    cj = client.get(f"/api/asset/{aid}/frame_candidates", params={"n": 6}).json()
    url0 = cj["candidates"][0]["url"]
    cand_dir = get_cfg().out_dir / "cand" / aid
    assert cand_dir.exists() and client.get(url0).status_code == 200
    assert client.post(f"/api/asset/{aid}/frame_candidates/clear", json={}).json()["ok"]
    assert not cand_dir.exists(), "取消应清掉候选"
    assert client.get(url0).status_code == 404, "取消后候选 URL 应失效"


def test_reframe_rejects_photo(client, tmp_path_factory):
    root = tmp_path_factory.mktemp("照片盘") / "图"
    root.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(root / "p.jpg"), np.full((240, 320, 3), 120, np.uint8))
    d = client.get("/api/fs", params={"dir": str(root)}).json()
    aid = d["assets"][0]["asset_id"]
    assert client.get(f"/api/asset/{aid}/frame_candidates").status_code == 400, "照片不该能换帧"
