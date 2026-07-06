"""Windows 移植回归测试（任何平台都能跑的部分）。

真实平台行为（PowerShell 对话框、卷 GUID、资源管理器定位、bat 启动链）要 Windows
真机验证（见 release/GitHub发布指南.md 第 6 节清单）；这里锁住可跨平台验证的部分：
- /api/health 平台字段（UI 靠它决定「Finder / 资源管理器」文案）
- 导出文件名清洗（Windows 非法字符 / 保留名 / 结尾点空格）
- 扫描系统目录跳过（$RECYCLE.BIN 等）
- 对话框接口在不支持的系统上必须 409，不能 500（老代码 os.uname 在 Windows 直接炸）
- winlaunch.py 必须纯标准库（它在依赖安装前就要能跑）
- 三个 .bat 必须存在且 CRLF + chcp 65001
"""
import ast
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app, _path_allowed
from app.core import osplat
from app.export.exporter import _sanitize_stem

client = TestClient(app)
APP_DIR = Path(__file__).resolve().parent.parent


def test_health_platform_fields():
    r = client.get("/api/health").json()
    assert r["ok"] is True
    assert r["platform"] in ("mac", "windows", "linux")
    assert r["file_manager"]          # UI 的「在 X 显示」按钮文案来源
    if r["platform"] == "windows":
        assert r["file_manager"] == "资源管理器"
    if r["platform"] == "mac":
        assert r["file_manager"] == "Finder"


def test_sanitize_stem():
    assert _sanitize_stem('黄鹤楼:日落?航拍') == '黄鹤楼_日落_航拍'
    assert _sanitize_stem('a<b>c"d/e\\f|g*h') == 'a_b_c_d_e_f_g_h'
    assert _sanitize_stem('name.') == 'name'
    assert _sanitize_stem('name  ') == 'name'
    assert _sanitize_stem('CON') == '_CON'          # Windows 保留名
    assert _sanitize_stem('com3') == '_com3'
    assert _sanitize_stem('') == '未命名'
    assert _sanitize_stem('...') == '未命名'
    # 正常名字原样保留（中文/下划线/数字）
    assert _sanitize_stem('武汉_长江_20240101') == '武汉_长江_20240101'


def test_should_skip_name():
    for bad in ('.DS_Store', '._A001.mov', '$RECYCLE.BIN', '$recycle.bin',
                'System Volume Information', 'Thumbs.db', 'desktop.ini',
                'pagefile.sys', 'found.000'):
        assert osplat.should_skip_name(bad), bad
    for good in ('素材', 'A7S3', 'DJI_0001.MP4', '2024黄鹤楼', 'RECYCLE',
                 'system', 'Desktop'):
        assert not osplat.should_skip_name(good), good


def test_fs_roots_pathsep(tmp_path):
    # conftest 用 os.pathsep 组 YEEHX_FS_ROOTS；临时目录必须进白名单
    assert _path_allowed(tmp_path)


@pytest.mark.skipif(sys.platform in ("darwin", "win32"),
                    reason="mac/Windows 上会真弹系统对话框；只在 Linux 验 409 降级")
def test_dialog_endpoints_degrade_not_crash():
    # 老代码用 os.uname()（Windows 上没有这个函数 → 500）；现在必须是明确的 409
    assert client.get("/api/dialog/folder").status_code == 409
    assert client.get("/api/dialog/export_folder").status_code == 409
    assert client.get("/api/dialog/lut_file").status_code == 409


def test_winlaunch_pure_stdlib():
    tree = ast.parse((APP_DIR / "winlaunch.py").read_text(encoding="utf-8"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    non_std = mods - set(sys.stdlib_module_names)
    assert not non_std, f"winlaunch.py 只能用标准库（装依赖前就要能跑），发现: {non_std}"
    assert "app" not in mods, "winlaunch.py 不得 import app 包（避免依赖未装时炸）"


def test_bat_files_crlf():
    for name in ("启动觅影.bat", "停止觅影.bat", "mcp_run.bat"):
        p = APP_DIR / name
        assert p.exists(), name
        raw = p.read_bytes()
        assert b"\r\n" in raw, f"{name} 必须 CRLF 行尾"
        assert b"chcp 65001" in raw, f"{name} 必须先切 UTF-8 代码页（中文提示不乱码）"
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{name} 不能带 BOM（cmd 会把首行当乱码执行）"


def test_detached_popen_kwargs_platform():
    kw = osplat.detached_popen_kwargs()
    if sys.platform.startswith("win"):
        assert "creationflags" in kw
    else:
        assert kw.get("start_new_session") is True


def test_mount_display_name_posix(tmp_path):
    from app.core.volumes import _mount_display_name
    assert _mount_display_name(tmp_path) == tmp_path.name


def test_cv2_unicode_path_io(tmp_path):
    """Windows 上 cv2.imread/imwrite 是 ANSI fopen，中文/emoji 路径直接失败；
    imread_u/imwrite_u 必须两平台都能处理（这里在任何平台验证同一条码路）。"""
    import numpy as np
    from app.media.frames import imread_u, imwrite_u
    d = tmp_path / "觅影素材🎬" / "黄鹤楼·日落"
    d.mkdir(parents=True)
    p = d / "测试帧？其实是合法名.jpg"   # 全角？合法
    img = np.zeros((32, 48, 3), dtype=np.uint8)
    img[:, :, 2] = 200
    assert imwrite_u(p, img, None)
    assert p.exists() and p.stat().st_size > 0
    back = imread_u(p)
    assert back is not None and back.shape == (32, 48, 3)
    # 失败路径的行为与 cv2 一致：返回 None / False，不抛异常
    assert imread_u(d / "不存在.jpg") is None
    assert imwrite_u(tmp_path / "不存在的目录" / "x.jpg", img) is False


def test_lut_filter_escapes_windows_path():
    """ffmpeg 滤镜串里 : 和 \\ 是元字符：C:\\luts\\x.cube 必须转义成 C\\:/luts/x.cube。"""
    from app.media.luts import ffmpeg_lut_filter
    f = ffmpeg_lut_filter(Path(r"C:\luts\索尼 S-Log3.cube"), "cube")
    assert f == r"lut3d='C\:/luts/索尼 S-Log3.cube'"
    assert "\\l" not in f.replace("\\:", "")   # 不残留裸反斜杠路径分隔
    # 单引号也要转义（文件名带 ' 的极端情况）
    f2 = ffmpeg_lut_filter(Path("/a/b'c.cube"), "cube")
    assert r"\'" in f2
