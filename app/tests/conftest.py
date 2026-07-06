"""冒烟测试环境：mock AI、独立数据目录，绝不碰真实 out/。
必须在任何 app 模块导入前设好环境变量（pytest 会先加载 conftest）。"""
import os
import tempfile
from pathlib import Path

os.environ["YEEHX_MOCK"] = "1"
_tmp = tempfile.mkdtemp(prefix="miying_test_")
os.environ["YEEHX_OUT"] = _tmp
os.environ["YEEHX_DB"] = str(Path(_tmp) / "test.sqlite")
# 3-1/3-3 白名单只放行 /Volumes 和用户目录；测试素材在系统临时目录里，显式加白
os.environ["YEEHX_FS_ROOTS"] = os.pathsep.join(dict.fromkeys(
    [tempfile.gettempdir(), "/tmp", "/private/var/folders", "/private/tmp"]))
