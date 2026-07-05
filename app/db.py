"""Lightweight SQLite layer for 觅影 v2.

The app now has one vocabulary: tags. Categories are only display labels and
colors. SQLite writes are serialized here so UI actions do not race background
tasks into ``database is locked``.
"""
from __future__ import annotations

import contextlib
import json
import os
import re as _re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, TypeVar

from app.config import get_cfg

_LOCAL = threading.local()
_WRITE_LOCK = threading.RLock()
_DB_GEN = 0      # 数据库"代数"：reset_db() 自增，所有线程的旧连接按代数判废
FTS_OK = True
T = TypeVar("T")


def _db_path() -> Path:
    env = os.environ.get("YEEHX_DB")
    if env:
        p = Path(env)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    cfg = get_cfg()
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    return cfg.db_path


def connect() -> sqlite3.Connection:
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None and getattr(_LOCAL, "gen", -1) != _DB_GEN:
        # 数据库被重置过：本线程攥着的是已删除旧文件的句柄（"幽灵库"——
        # 任务泵线程曾因此看不见重置后新建的任务，整个流水线卡死到重启）。换新。
        with contextlib.suppress(sqlite3.Error):
            conn.close()
        conn = None
        _LOCAL.conn = None
    if conn is None:
        conn = sqlite3.connect(str(_db_path()), timeout=60, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=60000")
        if not _try_journal(conn, "WAL"):
            _try_journal(conn, "DELETE")
        conn.execute("PRAGMA synchronous=NORMAL")
        _LOCAL.conn = conn
        _LOCAL.gen = _DB_GEN
    return conn


def _try_journal(conn: sqlite3.Connection, mode: str) -> bool:
    try:
        conn.execute(f"PRAGMA journal_mode={mode}")
        conn.execute("CREATE TABLE IF NOT EXISTS _probe(x)")
        conn.execute("INSERT INTO _probe(x) VALUES (1)")
        conn.execute("DROP TABLE _probe")
        conn.commit()
        return True
    except sqlite3.OperationalError:
        with contextlib.suppress(sqlite3.Error):
            conn.rollback()
        return False


def now() -> float:
    return time.time()


def jdumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def jloads(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return default


def chunks(seq, n: int = 900):
    """把序列切块，绕开 SQLite 的 IN (?,?,...) 变量上限（32766）。"""
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def write(fn: Callable[[sqlite3.Connection], T], *, retries: int = 8) -> T:
    """Run a write transaction with process-wide serialization and retry."""
    delay = 0.05
    last: Exception | None = None
    for _ in range(retries):
        with _WRITE_LOCK:
            conn = connect()
            try:
                res = fn(conn)
                conn.commit()
                return res
            except sqlite3.OperationalError as e:
                with contextlib.suppress(sqlite3.Error):
                    conn.rollback()
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                last = e
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    conn.rollback()
                raise
        time.sleep(delay)
        delay = min(delay * 1.8, 1.0)
    raise last or sqlite3.OperationalError("database is locked")


SCHEMA = """
CREATE TABLE IF NOT EXISTS volumes (
  volume_id           TEXT PRIMARY KEY,
  name                TEXT,
  display_name        TEXT,
  uuid                TEXT,
  size                INTEGER,
  sample_fingerprint  TEXT,
  last_mount          TEXT,
  online              INTEGER DEFAULT 1,
  created_at          REAL,
  updated_at          REAL
);

CREATE TABLE IF NOT EXISTS folders (
  folder_id      TEXT PRIMARY KEY,
  volume_id      TEXT,
  rel_path       TEXT,
  name           TEXT,
  parent_rel     TEXT,
  fingerprint    TEXT,
  scan_state     TEXT DEFAULT 'none',
  cover_thumb    TEXT,
  asset_count    INTEGER DEFAULT 0,
  child_count    INTEGER DEFAULT 0,
  hidden         INTEGER DEFAULT 0,
  note           TEXT,
  desc_ai        TEXT,
  lut            TEXT,
  created_at     REAL,
  updated_at     REAL,
  UNIQUE(volume_id, rel_path)
);

CREATE TABLE IF NOT EXISTS assets (
  asset_id      TEXT PRIMARY KEY,
  volume_id     TEXT,
  folder_id     TEXT,
  rel_path      TEXT,
  name          TEXT,
  kind          TEXT,
  size          INTEGER,
  mtime         REAL,
  content_id    TEXT,
  facts_json    TEXT,
  thumb_path    TEXT,
  thumb_lut     TEXT,
  status        TEXT DEFAULT 'pending',
  score         INTEGER DEFAULT 0,
  desc_ai       TEXT,
  note          TEXT,
  desc_locked   INTEGER DEFAULT 0,
  locked        INTEGER DEFAULT 0,
  lut           TEXT,
  created_at    REAL,
  updated_at    REAL,
  UNIQUE(volume_id, rel_path)
);

CREATE TABLE IF NOT EXISTS categories (
  id          TEXT PRIMARY KEY,
  name        TEXT UNIQUE,
  color       TEXT,
  ord         INTEGER DEFAULT 0,
  is_default  INTEGER DEFAULT 0,
  created_at  REAL,
  updated_at  REAL
);

CREATE TABLE IF NOT EXISTS tags (
  id              TEXT PRIMARY KEY,
  name            TEXT UNIQUE,
  category_id     TEXT,
  aliases_json    TEXT,
  note            TEXT,
  ref_images_json TEXT,
  pinned          INTEGER DEFAULT 0,
  enabled         INTEGER DEFAULT 1,
  created_at      REAL,
  updated_at      REAL
);

CREATE TABLE IF NOT EXISTS folder_tags (
  folder_id TEXT,
  tag_id    TEXT,
  source    TEXT DEFAULT 'manual',
  created_at REAL,
  PRIMARY KEY(folder_id, tag_id)
);

CREATE TABLE IF NOT EXISTS asset_tags (
  asset_id TEXT,
  tag_id   TEXT,
  source   TEXT DEFAULT 'manual',
  created_at REAL,
  PRIMARY KEY(asset_id, tag_id)
);

CREATE TABLE IF NOT EXISTS asset_tag_excludes (
  asset_id TEXT,
  tag_id   TEXT,
  created_at REAL,
  PRIMARY KEY(asset_id, tag_id)
);

CREATE TABLE IF NOT EXISTS suggestions (
  id              TEXT PRIMARY KEY,
  name            TEXT,
  category_id     TEXT,
  aliases_json    TEXT,
  note            TEXT,
  ref_images_json TEXT,
  hits_json       TEXT,
  hit_count       INTEGER DEFAULT 0,
  status          TEXT DEFAULT 'pending',
  reason          TEXT,
  created_at      REAL,
  updated_at      REAL,
  UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS tag_merge_suggestions (
  id            TEXT PRIMARY KEY,
  source_id     TEXT,
  target_id     TEXT,
  source_name   TEXT,
  target_name   TEXT,
  category      TEXT,
  confidence    REAL DEFAULT 0,
  reason        TEXT,
  model         TEXT,
  status        TEXT DEFAULT 'pending',
  created_at    REAL,
  updated_at    REAL,
  UNIQUE(source_id, target_id, status)
);

CREATE TABLE IF NOT EXISTS asset_effective (
  asset_id TEXT,
  field    TEXT,
  value    TEXT,
  PRIMARY KEY(asset_id, field, value)
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id     TEXT PRIMARY KEY,
  queue       TEXT,
  kind        TEXT,
  title       TEXT,
  scope_json  TEXT,
  params_json TEXT,
  total       INTEGER DEFAULT 0,
  done        INTEGER DEFAULT 0,
  failed      INTEGER DEFAULT 0,
  status      TEXT DEFAULT 'pending',
  error       TEXT,
  created_at  REAL,
  updated_at  REAL
);

CREATE TABLE IF NOT EXISTS task_items (
  task_id  TEXT,
  item_key TEXT,
  status   TEXT DEFAULT 'pending',
  error    TEXT,
  PRIMARY KEY (task_id, item_key)
);

CREATE TABLE IF NOT EXISTS exports (
  id           TEXT PRIMARY KEY,
  type         TEXT,
  scope_json   TEXT,
  target       TEXT,
  options_json TEXT,
  total        INTEGER DEFAULT 0,
  done         INTEGER DEFAULT 0,
  failed       INTEGER DEFAULT 0,
  status       TEXT DEFAULT 'pending',
  log_json     TEXT,
  created_at   REAL,
  updated_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_folders_vol      ON folders(volume_id, parent_rel);
CREATE INDEX IF NOT EXISTS idx_assets_folder    ON assets(folder_id);
CREATE INDEX IF NOT EXISTS idx_assets_vol       ON assets(volume_id);
CREATE INDEX IF NOT EXISTS idx_assets_status    ON assets(status);
CREATE INDEX IF NOT EXISTS idx_eff_field_value  ON asset_effective(field, value);
CREATE INDEX IF NOT EXISTS idx_eff_asset        ON asset_effective(asset_id);
CREATE INDEX IF NOT EXISTS idx_tags_name        ON tags(name);
CREATE INDEX IF NOT EXISTS idx_asset_tags_tag   ON asset_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_folder_tags_tag  ON folder_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_asset_tag_excludes_tag ON asset_tag_excludes(tag_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status, hit_count);
CREATE INDEX IF NOT EXISTS idx_tag_merge_suggestions_status ON tag_merge_suggestions(status, created_at);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS assets_fts USING fts5(
  asset_id UNINDEXED,
  text,
  tokenize = 'unicode61'
);
"""


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    global FTS_OK
    try:
        conn.executescript(FTS_SCHEMA)
        FTS_OK = True
    except sqlite3.OperationalError:
        FTS_OK = False
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS assets_fts (asset_id TEXT PRIMARY KEY, text TEXT);"
        )
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection):
    """旧库就地升级（幂等）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(suggestions)").fetchall()}
    if "confidence" not in cols:
        conn.execute("ALTER TABLE suggestions ADD COLUMN confidence REAL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_content ON assets(content_id)")
    # 重复检测的"忽略此组"（有意多盘备份的指纹不再提示）
    conn.execute("CREATE TABLE IF NOT EXISTS dup_ignores (content_id TEXT PRIMARY KEY, created_at REAL)")


_CJK_RUN = _re.compile(r"[一-鿿]+")
_LATIN = _re.compile(r"[A-Za-z0-9]+")


def _cjk_split(text: str) -> str:
    return _re.sub(r"([一-鿿])", r" \1 ", text or "").strip()


def fts_match_expr(query: str) -> str:
    if not query:
        return ""
    parts: list[str] = []
    for run in _CJK_RUN.findall(query):
        parts.append(f'"{" ".join(list(run))}"')
    for tok in _LATIN.findall(query):
        parts.append(f"{tok}*")
    return " AND ".join(parts)


def fts_like_terms(query: str) -> list[str]:
    return [t for t in (_CJK_RUN.findall(query or "") + _LATIN.findall(query or "")) if t]


def fts_set(conn: sqlite3.Connection, asset_id: str, text: str):
    stored = _cjk_split(text or "")
    if FTS_OK:
        conn.execute("DELETE FROM assets_fts WHERE asset_id=?", (asset_id,))
        conn.execute("INSERT INTO assets_fts(asset_id, text) VALUES (?,?)", (asset_id, stored))
    else:
        conn.execute(
            "INSERT INTO assets_fts(asset_id, text) VALUES (?,?) "
            "ON CONFLICT(asset_id) DO UPDATE SET text=excluded.text",
            (asset_id, text or ""),
        )


def fts_delete(conn: sqlite3.Connection, asset_id: str):
    conn.execute("DELETE FROM assets_fts WHERE asset_id=?", (asset_id,))


def close_local():
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        conn.close()
        _LOCAL.conn = None


def reset_db():
    global _DB_GEN
    cfg = get_cfg()
    close_local()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(cfg.db_path) + suffix)
        if p.exists():
            p.unlink()
    # 顺序关键：先在本线程把新库整个建好（建文件+全部表），再切代数。
    # 反过来切，其他线程会在"新文件已存在但表还没建完"的毫秒窗口里撞上
    # no such table——任务泵线程曾因此猝死（重置后生图正常、打标永远不来）。
    conn = init_db()
    _DB_GEN += 1            # 其他线程下次 connect() 自动换上这个已就绪的新库
    _LOCAL.gen = _DB_GEN    # 本线程这条新连接登记为新代，不用自废重连
    return conn
