"""平台差异集中营：mac / Windows 的系统级差异只允许写在这一个文件里。

觅影核心承诺（原素材只读、卷身份不认盘符/挂载点、数据只落 out/）在两个平台
必须一模一样；本文件只解决"同一件事在两个系统上怎么做"：
- 卷身份：mac 用 diskutil 卷UUID；Windows 用卷 GUID（GetVolumeNameForVolumeMountPoint，
  盘符 E:→F: 变了 GUID 不变，库不丢）
- 根目录页：mac 列 /Volumes + 桌面；Windows 列所有盘符（带卷标）+ 桌面
- 系统对话框：mac osascript；Windows PowerShell（FolderBrowserDialog / OpenFileDialog）
- 文件管理器定位：mac `open -R`；Windows `explorer /select,`
- 后台进程拉起 / 验明正身：mac ps + setsid；Windows CIM 查询 + CREATE_NO_WINDOW

除测试外，业务代码禁止直接写 sys.platform / os.uname 分支——都走这里。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
PLATFORM = "windows" if IS_WIN else ("mac" if IS_MAC else "linux")

# UI 用：文件管理器叫什么
FILE_MANAGER = "资源管理器" if IS_WIN else "Finder"

# 扫描/浏览要跳过的系统垃圾（小写比较）。mac 的 .DS_Store/._* 由「. 开头即跳过」
# 规则覆盖；这里补 Windows（以及 mac 盘插到 Windows 上会看到的）系统目录/文件。
_SKIP_LOWER = {
    "$recycle.bin", "system volume information", "found.000",
    "thumbs.db", "desktop.ini", "pagefile.sys", "hiberfil.sys", "swapfile.sys",
    "$windows.~bt", "recovery", "msocache",
}


def should_skip_name(name: str) -> bool:
    """扫描/浏览时该不该跳过这个目录项（两平台统一入口）。"""
    return name.startswith(".") or name.lower() in _SKIP_LOWER


# ═══════════════ Windows：盘符 / 卷标 / 卷 GUID ═══════════════

def win_drives() -> list[Path]:
    """当前所有盘符根（C:\\、D:\\ …），排除光驱。仅 Windows 有效。"""
    if not IS_WIN:
        return []
    import ctypes
    drives: list[Path] = []
    buf = ctypes.create_unicode_buffer(256)
    n = ctypes.windll.kernel32.GetLogicalDriveStringsW(255, buf)  # type: ignore[attr-defined]
    if not n:
        return []
    for root in buf[:n].split("\x00"):
        if not root:
            continue
        dtype = ctypes.windll.kernel32.GetDriveTypeW(root)  # type: ignore[attr-defined]
        # 2=可移动 3=本地盘 4=网络盘；跳过 5=光驱 / 1=无效 / 0=未知
        if dtype in (2, 3, 4, 6):
            drives.append(Path(root))
    return drives


_WIN_GUID_RE = re.compile(r"\{([0-9a-fA-F-]{36})\}")
_WIN_GUID_CACHE: dict[str, str] = {}


def win_volume_guid(mount: Path) -> str | None:
    """卷 GUID（盘符变了它不变）——Windows 版的"卷身份"。失败返回 None。"""
    if not IS_WIN:
        return None
    key = str(mount)
    hit = _WIN_GUID_CACHE.get(key)
    if hit is not None:
        return hit
    import ctypes
    root = str(mount)
    if not root.endswith("\\"):
        root += "\\"
    buf = ctypes.create_unicode_buffer(64)
    ok = ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root, buf, 64)  # type: ignore[attr-defined]
    if not ok:
        return None
    m = _WIN_GUID_RE.search(buf.value or "")
    if not m:
        return None
    guid = m.group(1).lower()
    _WIN_GUID_CACHE[key] = guid   # 只缓存成功结果
    return guid


def win_volume_label(mount: Path) -> str:
    """卷标（如"拍摄素材2023"）；没有卷标 → "磁盘 E:"。仅 Windows 有效。"""
    if not IS_WIN:
        return mount.name or str(mount)
    import ctypes
    root = str(mount)
    if not root.endswith("\\"):
        root += "\\"
    name_buf = ctypes.create_unicode_buffer(261)
    fs_buf = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
        root, name_buf, 261, None, None, None, fs_buf, 261)
    label = (name_buf.value or "").strip() if ok else ""
    letter = str(mount).rstrip("\\/")
    return label or f"磁盘 {letter}"


def candidate_mounts() -> list[Path]:
    """当前在线的候选卷根（用于离线卷的按身份重发现）。"""
    if IS_WIN:
        return win_drives()
    vroot = Path("/Volumes")
    if vroot.exists():
        out = []
        for p in sorted(vroot.iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                out.append(p)
        return out
    return []


# ═══════════════ 系统对话框 / 文件管理器定位 ═══════════════

class DialogUnsupported(RuntimeError):
    """当前系统没有可用的系统选择对话框。"""


class DialogCancelled(RuntimeError):
    """用户取消了选择。"""


_PS_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "


def _ps_run(script: str, timeout: int = 300) -> str:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", _PS_PREFIX + script],
        capture_output=True, timeout=timeout)
    return r.stdout.decode("utf-8", errors="replace").strip()


def choose_folder(prompt: str) -> str:
    """系统"选文件夹"对话框，返回绝对路径。取消→DialogCancelled，不支持→DialogUnsupported。"""
    if IS_MAC:
        try:
            r = subprocess.run(
                ["osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")'],
                capture_output=True, text=True, timeout=300)
        except subprocess.SubprocessError as e:
            raise DialogUnsupported(str(e))
        path = r.stdout.strip()
        if r.returncode != 0 or not path:
            raise DialogCancelled("未选择文件夹")
        return path.rstrip("/")
    if IS_WIN:
        desc = prompt.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            f"$d.Description = '{desc}'; $d.ShowNewFolderButton = $true; "
            "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }")
        try:
            path = _ps_run(script)
        except (subprocess.SubprocessError, OSError) as e:
            raise DialogUnsupported(str(e))
        if not path:
            raise DialogCancelled("未选择文件夹")
        return path.rstrip("\\/") or path
    raise DialogUnsupported("当前系统不支持系统选择对话框")


def choose_file(prompt: str, ext: str = ".cube", filter_name: str = "LUT") -> str:
    """系统"选文件"对话框，返回绝对路径。"""
    if IS_MAC:
        try:
            r = subprocess.run(
                ["osascript", "-e", f'POSIX path of (choose file with prompt "{prompt}")'],
                capture_output=True, text=True, timeout=300)
        except subprocess.SubprocessError as e:
            raise DialogUnsupported(str(e))
        path = r.stdout.strip()
        if r.returncode != 0 or not path:
            raise DialogCancelled("未选择文件")
        return path.rstrip("/")
    if IS_WIN:
        star = "*" + ext
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.OpenFileDialog; "
            f"$d.Filter = '{filter_name} ({star})|{star}|所有文件 (*.*)|*.*'; "
            f"$d.Title = '{prompt.replace(chr(39), chr(39) * 2)}'; "
            "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.FileName }")
        try:
            path = _ps_run(script)
        except (subprocess.SubprocessError, OSError) as e:
            raise DialogUnsupported(str(e))
        if not path:
            raise DialogCancelled("未选择文件")
        return path
    raise DialogUnsupported("当前系统不支持系统选择对话框")


def reveal(path: Path) -> None:
    """在文件管理器中定位该文件（mac: Finder / win: 资源管理器）。"""
    if IS_MAC:
        subprocess.run(["open", "-R", str(path)], timeout=10)
        return
    if IS_WIN:
        # explorer 成功也常返回退出码 1，不看返回码。
        # /select, 和路径必须拼成一个参数串传（list 形式在部分环境不生效）；
        # Windows 文件名不允许双引号，直接包引号安全。
        subprocess.run(f'explorer /select,"{path}"', timeout=10)
        return
    raise DialogUnsupported("当前系统不支持文件管理器定位")


def open_url(url: str) -> None:
    """默认浏览器打开（跨平台）。"""
    import webbrowser
    webbrowser.open(url)


# ═══════════════ 后台进程 ═══════════════

def detached_popen_kwargs() -> dict:
    """让子进程不随父进程/终端退出、不闪黑窗的平台参数。"""
    if IS_WIN:
        # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        return {"creationflags": 0x08000000 | 0x00000200}
    return {"start_new_session": True}


def pid_command(pid: int) -> str:
    """查进程命令行（验明正身用）；查不到返回空串。"""
    try:
        if IS_WIN:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", _PS_PREFIX +
                 f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\").CommandLine"],
                capture_output=True, timeout=10).stdout
            return out.decode("utf-8", errors="replace").strip()
        out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout
        return out.strip()
    except (subprocess.SubprocessError, OSError, ValueError):
        return ""


# ═══════════════ 浏览白名单 ═══════════════

def allowed_browse_roots() -> list[Path]:
    """允许浏览/导出的根：mac=/Volumes+主目录；Windows=所有盘符+主目录。
    额外白名单走 YEEHX_FS_ROOTS（os.pathsep 分隔：mac 冒号 / Windows 分号）。"""
    if IS_WIN:
        roots = [*win_drives(), Path.home()]
    else:
        roots = [Path("/Volumes"), Path.home()]
    for part in (os.environ.get("YEEHX_FS_ROOTS") or "").split(os.pathsep):
        if part.strip():
            roots.append(Path(part.strip()))
    return roots


def denied_browse_roots() -> list[Path]:
    """白名单内仍要拒绝的系统目录（Windows 盘符全放行后，系统区必须挡回去）。"""
    if not IS_WIN:
        return []
    out: list[Path] = []
    for env in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        v = os.environ.get(env)
        if v:
            out.append(Path(v))
    out.append(Path.home() / "AppData")
    return out
