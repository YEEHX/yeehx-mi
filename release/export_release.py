#!/usr/bin/env python3
"""觅影发布导出：从开发仓生成干净的公开副本 + 安检 + zip。

原则（2026-07-03 拍板）：
- 开发仓 = 唯一真相源，永不动它；公开版是每次一键导出的**产物**，不是长期分叉
- 公开仓从零历史开始（不做 git 历史手术，规避一切历史泄漏）
- 你的数据（app/out/ 5.8G）任何情况都不进发布包

用法（在仓库根目录）：
    python3 release/export_release.py              # 导出 + 安检 + zip
    python3 release/export_release.py --init-repo  # 额外在导出目录 git init + 首次提交

产物：release/dist/MiYing-v<版本>/ 目录 + 觅影-MiYing-v<版本>.zip
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import __version__ as VERSION  # noqa: E402

DIST = ROOT / "release" / "dist"
OUT_DIR = DIST / f"MiYing-v{VERSION}"

# ── 排除表（相对仓库根；目录以 / 结尾） ──
EXCLUDES = [
    ".git/", ".pytest_cache/", ".DS_Store",
    "app/out/", "app/.venv/", "app/pip_err.log",
    "release/dist/",
    "觅影-一期优化执行方案.md",          # 内部执行文档，不随发布
    "AGENTS.md",                        # Cowork 项目内部说明，对外无意义
    "demo_export/",                     # 个人导出测试成果，无任何引用，不随发布
    "release/觅影-核心文案库.md",        # 内部运营文案，不进用户包/公开仓
    "release/官网下载页文案.md",         # 内部运营文案
    "release/GitHub发布指南.md",         # 内部操作手册
    "docs/assets/这里放三张图.md",       # 占位说明
]

# docs/ 进公开仓（README 配图、图文使用说明），但不进用户下载的 zip（省 ~12MB）。
# zip 里的 README 会把 docs/ 相对链接重写成 GitHub 绝对链接，离线包里也不裂。
ZIP_EXCLUDE_PREFIXES = ["docs/"]
REPO_RAW_BASE = "https://raw.githubusercontent.com/YEEHX/yeehx-mi/main"
REPO_BLOB_BASE = "https://github.com/YEEHX/yeehx-mi/blob/main"
EXCLUDE_PATTERNS = ["__pycache__", ".pyc", ".DS_Store", ".log"]
# 厂商 LUT 不可再分发（版权）：只带 luts/README.md 引导用户自行下载
CUBE_DIR = "app/luts"
# 杂图（uuid 命名的截图等）
UUID_PNG = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.png$")

# ── 文本清洗：个人路径 → 占位（用户名用拼接写，避免本脚本被自己的安检误伤） ──
_USER = "hi" + "yeehx"
PATH_REWRITE = ("/Users/" + _USER, "/Users/你的用户名")
REWRITE_SUFFIX = {".md", ".yaml", ".yml", ".json", ".py", ".sh", ".command"}

# ── 安检规则 ──
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI 风格 key"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"), "GitHub token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS key"),
    (re.compile(r"\"(api_key|remote_api_key|token)\"\s*:\s*\"[A-Za-z0-9_\-]{16,}\""), "json 里的实值 key"),
    (re.compile(_USER), "个人用户名"),
]


def _excluded(rel: str) -> bool:
    for e in EXCLUDES:
        if e.endswith("/") and (rel + "/").startswith(e):
            return True
        if rel == e:
            return True
    if any(p in rel for p in EXCLUDE_PATTERNS):
        return True
    if UUID_PNG.match(Path(rel).name):
        return True
    if rel.startswith(CUBE_DIR) and rel.endswith(".cube"):
        return True
    return False


def export() -> list[str]:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)
    copied = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT).as_posix()
        if _excluded(rel):
            continue
        dst = OUT_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if p.suffix in REWRITE_SUFFIX:
            try:
                txt = p.read_text(encoding="utf-8")
                dst.write_text(txt.replace(*PATH_REWRITE), encoding="utf-8")
            except UnicodeDecodeError:
                shutil.copy2(p, dst)
        else:
            shutil.copy2(p, dst)
        copied.append(rel)
    return copied


def audit() -> list[str]:
    """导出目录安检：返回问题列表（空=通过）。"""
    problems = []
    if (OUT_DIR / "app" / "out").exists():
        problems.append("app/out/ 混进了发布包！")
    if not (OUT_DIR / "LICENSE").exists() or not (OUT_DIR / "NOTICE").exists():
        problems.append("缺 LICENSE / NOTICE")
    for cube in (OUT_DIR / "app" / "luts").glob("*.cube"):
        problems.append(f"厂商 LUT 混入：{cube.name}（版权风险）")
    for p in OUT_DIR.rglob("*"):
        if not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pat, label in SECRET_PATTERNS:
            m = pat.search(txt)
            if m:
                problems.append(f"{p.relative_to(OUT_DIR)} 疑似{label}: {m.group(0)[:24]}…")
    # README 本地引用完整性（防裂图/死链：公开仓首页是门面）
    readme = OUT_DIR / "README.md"
    if readme.exists():
        txt = readme.read_text(encoding="utf-8")
        for m in re.finditer(r'(?:src="|\]\()(?!https?://)([^")]+)', txt):
            ref = m.group(1).split("#")[0].strip()
            if ref and not (OUT_DIR / ref).exists():
                problems.append(f"README 引用缺失（会裂图/死链）：{ref}")
    return problems


def make_zip() -> Path:
    zpath = DIST / f"觅影-MiYing-v{VERSION}.zip"
    if zpath.exists():
        zpath.unlink()
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(OUT_DIR.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(OUT_DIR).as_posix()
            # docs/ 只留在公开仓，不进用户 zip（README 链接下面重写成绝对链）
            if any(rel.startswith(x) for x in ZIP_EXCLUDE_PREFIXES):
                continue
            arc = str(p.relative_to(OUT_DIR.parent))
            zi = zipfile.ZipInfo.from_file(p, arc)
            zi.compress_type = zipfile.ZIP_DEFLATED
            # ⚠ zipfile 默认丢执行位：.command/.sh 不给 755 的话，用户解压后双击直接报"无法执行"
            mode = 0o755 if p.suffix in (".command", ".sh") else 0o644
            zi.external_attr = (mode | 0o100000) << 16
            if rel == "README.md":
                txt = p.read_text(encoding="utf-8")
                txt = txt.replace('src="docs/', f'src="{REPO_RAW_BASE}/docs/')
                txt = txt.replace("](docs/", f"]({REPO_BLOB_BASE}/docs/")
                z.writestr(zi, txt.encode("utf-8"))
            else:
                z.writestr(zi, p.read_bytes())
    return zpath


def init_repo():
    subprocess.run(["git", "init", "-q"], cwd=OUT_DIR, check=True)
    subprocess.run(["git", "add", "-A"], cwd=OUT_DIR, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"觅影 MiYing v{VERSION} 首次公开发布"],
                   cwd=OUT_DIR, check=True)
    print(f"✓ 公开仓已初始化（零历史）：{OUT_DIR}")
    print("  推 GitHub：git remote add origin <仓库地址> && git push -u origin main")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init-repo", action="store_true", help="导出后在产物目录 git init + 首次提交")
    ap.add_argument("--no-zip", action="store_true")
    args = ap.parse_args()

    print(f"=== 觅影发布导出 v{VERSION} ===")
    copied = export()
    print(f"✓ 导出 {len(copied)} 个文件 → {OUT_DIR}")
    problems = audit()
    if problems:
        print("✗ 安检不通过：")
        for x in problems:
            print("   -", x)
        sys.exit(1)
    print("✓ 安检通过（无个人路径 / 无密钥 / 无厂商 LUT / 无数据目录）")
    if not args.no_zip:
        z = make_zip()
        print(f"✓ 发布包：{z}（{z.stat().st_size/1048576:.1f} MB）")
    if args.init_repo:
        init_repo()
    print("完成。上传 zip 到官网 + 更新 release/version.json 即完成发版。")


if __name__ == "__main__":
    main()
