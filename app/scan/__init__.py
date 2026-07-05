"""扫描任务：快扫文件夹 / 生图 / 打标 / 同步。"""
from app.scan.scanner import (
    register_all, quick_import, fill_thumbs, fine_tag, cleanup_scope,
)
