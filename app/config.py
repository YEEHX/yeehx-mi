"""读取 config.yaml，提供全局配置对象 cfg。

模型覆盖项落 out/model_settings.json，不改带注释的 config.yaml。
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

import yaml

APP_DIR = Path(__file__).resolve().parent


class Config:
    def __init__(self, data: dict, base_dir: Path, config_path: Path | None = None):
        self.base_dir = base_dir
        self.config_path = config_path or (base_dir / "config.yaml")
        self.raw = data or {}
        data = self.raw

        # ── 模型 / 视觉 ──
        v = data.get("vision", {})
        self.provider = v.get("provider", "local")
        self.local_base_url = v.get("local_base_url", v.get("base_url", "http://localhost:11434/v1")).rstrip("/")
        self.local_api_key = v.get("local_api_key", "ollama")
        self.local_model = v.get("local_model", v.get("model", "qwen3.6:35b"))
        self.api_base_url = v.get("api_base_url", "https://api.openai.com/v1").rstrip("/")
        self.remote_api_key = v.get("remote_api_key", "" if self.provider != "api" else v.get("api_key", ""))
        self.api_model = v.get("api_model", "")
        self.base_url = self.local_base_url
        self.api_key = self.local_api_key
        self.model = self.local_model
        self.timeout = int(v.get("timeout", 240))
        self.temperature = float(v.get("temperature", 0.1))

        # ── LUT / 机型猜测（LUT 在觅影里是手动的，这些表仅供来源·设备识别兜底）──
        self.luts = data.get("luts", {})
        self.camera_profile_map = data.get("camera_profile_map", {})
        self.filename_profile_map = data.get("filename_profile_map", {})
        self.max_px = int(data.get("derivative", {}).get("max_px", 1024))

        # ── 抽帧 ──
        f = data.get("frames", {})
        self.sharp_samples = f.get("sharp_samples", [0.4, 0.5, 0.6])

        # ── 阈值 ──
        t = data.get("thresholds", {})
        self.timelapse_min_frames = int(t.get("timelapse_min_frames", 30))
        # AI place_guess 写标签的最低置信度（兼容旧 yaml 的 locate.guess_conf_min 位置）
        self.guess_conf_min = float(t.get("guess_conf_min", data.get("locate", {}).get("guess_conf_min", 0.7)))

        # ── 成片夹 / AI 标记 ──
        self.film_folder_keywords = data.get("film_folder_keywords", [])
        self.ai_markers = [str(m).lower() for m in data.get("ai_markers", [])]

        # ── 觅影 新增：扫描 ──
        s = data.get("scan", {})
        self.rep_samples = int(s.get("rep_samples", 2))           # 每个有素材的文件夹取几张代表
        self.thumb_max_px = int(s.get("thumb_max_px", self.max_px))

        # ── 觅影 新增：任务并发 ──
        q = data.get("tasks", {})
        self.workers_index = int(q.get("index", 2))
        self.workers_thumb = int(q.get("thumb", 3))
        self.workers_text = int(q.get("text", 4))
        self.workers_ai_local = int(q.get("ai_local", 1))
        self.workers_ai_api = int(q.get("ai_api", 3))
        self.workers_export = int(q.get("export", 2))

        # ── 路径 ──（数据目录默认就在 app/out；测试可用 YEEHX_OUT 覆盖。不动用户其它文件夹。）
        _out_env = os.environ.get("YEEHX_OUT")
        self.out_dir = Path(_out_env) if _out_env else (base_dir / data.get("paths", {}).get("out", "out"))
        self.db_path = self.out_dir / "miying.sqlite"
        self.thumbs_dir = self.out_dir / "thumbs"
        self.refimg_dir = self.out_dir / "refimg"
        self.model_settings_path = self.out_dir / "model_settings.json"
        self.luts_user_path = self.out_dir / "luts_user.json"   # 用户现场导入的 .cube 预设
        self.app_settings_path = self.out_dir / "app_settings.json"

        self.auto_lut = True   # 自动 LUT 预览（R3D/索尼 log 容器级识别）
        self.apply_model_settings()
        self.apply_app_settings()

    def apply_app_settings(self, settings: dict | None = None):
        if settings is None:
            settings = {}
            try:
                if self.app_settings_path.exists():
                    settings = json.loads(self.app_settings_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                settings = {}
        self.auto_lut = bool(settings.get("auto_lut", True))
        # 局域网访问（同 WiFi 手机等）：默认关；访问口令首次开启时生成，泄露可在设置页更换
        self.lan_access = bool(settings.get("lan_access", False))
        self.lan_token = str(settings.get("lan_token") or "")
        # 首跑向导完成标记（服务端记，换浏览器不重弹；老库升级由 /api/setup/status 自动补）
        self.setup_done = bool(settings.get("setup_done", False))
        # 检查更新（仅此一处出站请求：GET yeehx.com/miying/version.json；设置页可关）
        self.update_check = bool(settings.get("update_check", True))
        # 独立窗口模式（pywebview 壳）：关掉则启动脚本走纯浏览器（重启生效）
        self.window_mode = bool(settings.get("window_mode", True))
        # 升级搬家的来源目录（清理旧版本用；清理完成后置空）
        self.last_migrated_from = str(settings.get("last_migrated_from") or "")

    def save_app_settings(self, settings: dict):
        # ⚠ cur 是全量基底：新增持久字段必须加进来，否则任何一次 save 都会把它抹掉
        cur = {"auto_lut": self.auto_lut, "lan_access": self.lan_access, "lan_token": self.lan_token,
               "setup_done": self.setup_done, "update_check": self.update_check,
               "window_mode": self.window_mode,
               "last_migrated_from": self.last_migrated_from or None}
        cur.update(settings or {})
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.app_settings_path.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        self.apply_app_settings(cur)

    # ── 并发：按当前 provider 给 AI 队列线程数 ──
    def ai_workers(self) -> int:
        return self.workers_ai_api if self.provider == "api" else self.workers_ai_local

    def model_settings(self) -> dict:
        return {
            "provider": self.provider,
            "local_base_url": self.local_base_url,
            "local_api_key": self.local_api_key,
            "local_model": self.local_model,
            "api_base_url": self.api_base_url,
            "api_key": self.remote_api_key,
            "api_model": self.api_model,
            "active_base_url": self.base_url,
            "active_model": self.model,
            "timeout": self.timeout,
            "temperature": self.temperature,
        }

    def apply_model_settings(self, settings: dict | None = None):
        """应用 out/model_settings.json 里的模型覆盖项；不改带注释的 config.yaml。"""
        if settings is None:
            settings = {}
            try:
                if self.model_settings_path.exists():
                    settings = json.loads(self.model_settings_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                settings = {}
        provider = (settings.get("provider") or self.provider or "local").strip().lower()
        self.provider = "api" if provider == "api" else "local"
        self.local_base_url = (settings.get("local_base_url") or self.local_base_url).rstrip("/")
        self.local_api_key = settings.get("local_api_key", self.local_api_key) or "ollama"
        self.local_model = settings.get("local_model") or self.local_model
        self.api_base_url = (settings.get("api_base_url") or self.api_base_url).rstrip("/")
        self.remote_api_key = settings.get("api_key", self.remote_api_key) or ""
        self.api_model = settings.get("api_model") or self.api_model
        if self.provider == "api":
            self.base_url = self.api_base_url
            self.api_key = self.remote_api_key
            self.model = self.api_model or self.local_model
        else:
            self.base_url = self.local_base_url
            self.api_key = self.local_api_key
            self.model = self.local_model

    def save_model_settings(self, settings: dict):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.model_settings_path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.apply_model_settings(settings)

    # ── LUT 路径：先查内置 luts dict，再查用户导入的 luts_user.json ──
    def lut_path(self, profile: str) -> Path | None:
        rel = self.luts.get(profile)
        if not rel:
            user = self._user_luts()
            rel = user.get(profile)
        if not rel:
            return None
        p = Path(rel)
        return p if p.is_absolute() else (self.base_dir / p)

    def _user_luts(self) -> dict:
        try:
            if self.luts_user_path.exists():
                return json.loads(self.luts_user_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def all_luts(self) -> dict:
        """内置 + 用户导入，合并后的 {名称: 相对/绝对路径}。原片不在此（= 不套）。"""
        merged = dict(self.luts)
        merged.update(self._user_luts())
        return merged

    def add_user_lut(self, name: str, cube_path: str):
        user = self._user_luts()
        user[name] = cube_path
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.luts_user_path.write_text(json.dumps(user, ensure_ascii=False, indent=2), encoding="utf-8")

    def remove_user_lut(self, name: str):
        user = self._user_luts()
        user.pop(name, None)
        self.luts_user_path.write_text(json.dumps(user, ensure_ascii=False, indent=2), encoding="utf-8")

    def guess_profile(self, make: str, model: str) -> str | None:
        hay = f"{make} {model}".strip()
        if not hay:
            return None
        for needle, profile in self.camera_profile_map.items():
            if needle and needle.lower() in hay.lower():
                return profile
        return None

    def guess_profile_by_filename(self, filename: str) -> str | None:
        if not filename:
            return None
        for pat, profile in self.filename_profile_map.items():
            try:
                if re.search(pat, filename, re.IGNORECASE):
                    return profile
            except re.error:
                if str(pat).lower() in filename.lower():
                    return profile
        return None


def load_config(path: Path | None = None) -> Config:
    path = path or (APP_DIR / "config.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config(data, path.parent.resolve(), path.resolve())


# 单例：全应用共享一个 cfg
_CFG: Config | None = None


def get_cfg() -> Config:
    global _CFG
    if _CFG is None:
        _CFG = load_config()
    return _CFG
