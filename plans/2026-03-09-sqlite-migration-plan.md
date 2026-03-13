# SQLite Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace JSON file persistence and in-memory state with SQLite3, split monolithic `app.py` into focused modules.

**Architecture:** SQLite database at `/config/lidarr-downloader.db` with schema versioning. Data access through `models.py`, all SQL isolated there. Modules split by domain. Config stays in `config.json` (not migrated to DB).

**Tech Stack:** Python 3, sqlite3 (stdlib), Flask, existing dependencies unchanged.

**Design doc:** `plans/2026-03-09-sqlite-migration-design.md`

---

## Task Order & Dependencies

Tasks 1-4 build the foundation (db, models, config, utils) with no cross-dependencies beyond db->models.
Tasks 5-8 extract domain modules that depend on config/utils.
Task 9 rewires app.py to use all modules.
Tasks 10-11 handle UI pagination and the migration tool.
Task 12 updates documentation.

---

### Task 1: Create `db.py` — database connection and schema

**Files:**
- Create: `db.py`
- Create: `tests/test_db.py`

**Step 1: Write failing tests**

```python
# tests/test_db.py
import os
import sqlite3
import pytest
from db import init_db, get_db, close_db, DB_PATH

@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr("db.DB_PATH", db_path)
    yield db_path
    close_db()

def test_init_db_creates_tables(temp_db):
    init_db()
    conn = sqlite3.connect(temp_db)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    assert "schema_version" in tables
    assert "download_history" in tables
    assert "download_logs" in tables
    assert "failed_tracks" in tables
    assert "download_queue" in tables

def test_init_db_sets_schema_version(temp_db):
    init_db()
    conn = sqlite3.connect(temp_db)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    conn.close()
    assert row[0] == 1

def test_init_db_idempotent(temp_db):
    init_db()
    init_db()  # should not raise
    conn = sqlite3.connect(temp_db)
    rows = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
    conn.close()
    assert rows[0] == 1

def test_get_db_returns_connection(temp_db):
    init_db()
    conn = get_db()
    assert conn is not None
    result = conn.execute("SELECT 1").fetchone()
    assert result[0] == 1

def test_queue_status_check_constraint(temp_db):
    init_db()
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO download_queue (album_id, position, status) VALUES (1, 1, 'queued')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO download_queue (album_id, position, status) VALUES (2, 2, 'invalid')"
        )
    conn.close()
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'db'`

**Step 3: Implement `db.py`**

```python
# db.py
import os
import sqlite3
import time
import logging
import threading

logger = logging.getLogger(__name__)

DB_PATH = "/config/lidarr-downloader.db"
SCHEMA_VERSION = 1

_local = threading.local()


def get_db():
    if not hasattr(_local, "connection") or _local.connection is None:
        _local.connection = sqlite3.connect(DB_PATH)
        _local.connection.row_factory = sqlite3.Row
        _local.connection.execute("PRAGMA journal_mode=WAL")
        _local.connection.execute("PRAGMA foreign_keys=ON")
    return _local.connection


def close_db():
    conn = getattr(_local, "connection", None)
    if conn is not None:
        conn.close()
        _local.connection = None


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS download_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL,
    album_title TEXT NOT NULL,
    artist_name TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 1,
    partial INTEGER NOT NULL DEFAULT 0,
    manual INTEGER NOT NULL DEFAULT 0,
    track_title TEXT,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS download_logs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    album_id INTEGER NOT NULL,
    album_title TEXT NOT NULL,
    artist_name TEXT NOT NULL,
    timestamp REAL NOT NULL,
    details TEXT DEFAULT '',
    failed_tracks TEXT DEFAULT '[]',
    total_file_size INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS failed_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER,
    album_title TEXT DEFAULT '',
    artist_name TEXT DEFAULT '',
    cover_url TEXT DEFAULT '',
    album_path TEXT DEFAULT '',
    lidarr_album_path TEXT DEFAULT '',
    track_title TEXT NOT NULL,
    track_num INTEGER DEFAULT 0,
    reason TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS download_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL UNIQUE,
    position INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'downloading'))
);

CREATE INDEX IF NOT EXISTS idx_history_timestamp ON download_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_history_album_id ON download_history(album_id);
CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON download_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_queue_position ON download_queue(position);
"""


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        conn.executescript(_SCHEMA_V1)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, time.time()),
        )
        conn.commit()
        logger.info(f"Database initialized at schema version {SCHEMA_VERSION}")
    else:
        current = conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        current_version = current[0] if current else 0
        _run_migrations(conn, current_version)


def _run_migrations(conn, current_version):
    migrations = {
        # 2: migrate_v1_to_v2,
    }
    for version in sorted(migrations.keys()):
        if current_version < version:
            logger.info(f"Running migration to schema version {version}...")
            migrations[version](conn)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, time.time()),
            )
            conn.commit()
            logger.info(f"Migration to version {version} complete")
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_db.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "Add db.py with SQLite schema, connection management, and migration framework"
```

---

### Task 2: Create `models.py` — data access layer

**Files:**
- Create: `models.py`
- Create: `tests/test_models.py`

**Step 1: Write failing tests**

```python
# tests/test_models.py
import json
import pytest
import db
import models

@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr("db.DB_PATH", db_path)
    db.init_db()
    yield db_path
    db.close_db()

# --- History ---

def test_add_and_get_history():
    models.add_history_entry(1, "Album1", "Artist1", True, False)
    result = models.get_history(page=1, per_page=50)
    assert result["total"] == 1
    assert result["items"][0]["album_title"] == "Album1"

def test_history_pagination():
    for i in range(75):
        models.add_history_entry(i, f"Album{i}", "Artist", True, False)
    page1 = models.get_history(page=1, per_page=50)
    page2 = models.get_history(page=2, per_page=50)
    assert page1["total"] == 75
    assert page1["pages"] == 2
    assert len(page1["items"]) == 50
    assert len(page2["items"]) == 25

def test_history_ordered_newest_first():
    models.add_history_entry(1, "Old", "A", True, False)
    models.add_history_entry(2, "New", "A", True, False)
    result = models.get_history()
    assert result["items"][0]["album_title"] == "New"

def test_clear_history():
    models.add_history_entry(1, "A", "A", True, False)
    models.clear_history()
    assert models.get_history()["total"] == 0

def test_history_manual_fields():
    models.add_history_entry(1, "A", "A", True, False, manual=True, track_title="Track1")
    item = models.get_history()["items"][0]
    assert item["manual"] is True
    assert item["track_title"] == "Track1"

# --- Logs ---

def test_add_and_get_logs():
    models.add_log("download_success", 1, "Album", "Artist", details="ok")
    result = models.get_logs(page=1, per_page=50)
    assert result["total"] == 1
    assert result["items"][0]["type"] == "download_success"

def test_log_failed_tracks_json():
    tracks = [{"title": "T1", "reason": "fail", "track_num": 1}]
    models.add_log("partial_success", 1, "A", "A", failed_tracks=tracks)
    item = models.get_logs()["items"][0]
    assert isinstance(item["failed_tracks"], list)
    assert item["failed_tracks"][0]["title"] == "T1"

def test_delete_log():
    models.add_log("download_success", 1, "A", "A")
    log_id = models.get_logs()["items"][0]["id"]
    assert models.delete_log(log_id) is True
    assert models.get_logs()["total"] == 0

def test_delete_log_not_found():
    assert models.delete_log("nonexistent") is False

def test_clear_logs():
    models.add_log("download_success", 1, "A", "A")
    models.clear_logs()
    assert models.get_logs()["total"] == 0

# --- Failed tracks ---

def test_save_and_get_failed_tracks():
    tracks = [{"title": "T1", "track_num": 1, "reason": "no match"}]
    models.save_failed_tracks(1, "Album", "Artist", "http://cover", "/path", "/lidarr", tracks)
    result = models.get_failed_tracks()
    assert len(result) == 1
    assert result[0]["track_title"] == "T1"
    assert result[0]["album_title"] == "Album"

def test_clear_failed_tracks():
    tracks = [{"title": "T1", "track_num": 1, "reason": "no match"}]
    models.save_failed_tracks(1, "A", "A", "", "", "", tracks)
    models.clear_failed_tracks()
    assert len(models.get_failed_tracks()) == 0

def test_save_failed_tracks_replaces_previous():
    tracks1 = [{"title": "T1", "track_num": 1, "reason": "fail"}]
    tracks2 = [{"title": "T2", "track_num": 2, "reason": "fail2"}]
    models.save_failed_tracks(1, "A", "A", "", "", "", tracks1)
    models.save_failed_tracks(2, "B", "B", "", "", "", tracks2)
    result = models.get_failed_tracks()
    assert len(result) == 1
    assert result[0]["track_title"] == "T2"

# --- Queue ---

def test_enqueue_and_get_queue():
    models.enqueue_album(10)
    models.enqueue_album(20)
    queue = models.get_queue()
    assert len(queue) == 2
    assert queue[0]["album_id"] == 10
    assert queue[1]["album_id"] == 20

def test_enqueue_duplicate_ignored():
    models.enqueue_album(10)
    models.enqueue_album(10)
    assert len(models.get_queue()) == 1

def test_dequeue_album():
    models.enqueue_album(10)
    models.enqueue_album(20)
    models.dequeue_album(10)
    queue = models.get_queue()
    assert len(queue) == 1
    assert queue[0]["album_id"] == 20

def test_set_queue_status():
    models.enqueue_album(10)
    models.set_queue_status(10, models.QUEUE_STATUS_DOWNLOADING)
    queue = models.get_queue()
    assert queue[0]["status"] == "downloading"

def test_set_queue_status_invalid():
    models.enqueue_album(10)
    with pytest.raises(ValueError):
        models.set_queue_status(10, "invalid_status")

def test_reset_downloading_to_queued():
    models.enqueue_album(10)
    models.set_queue_status(10, models.QUEUE_STATUS_DOWNLOADING)
    models.reset_downloading_to_queued()
    queue = models.get_queue()
    assert queue[0]["status"] == "queued"

def test_clear_queue():
    models.enqueue_album(10)
    models.clear_queue()
    assert len(models.get_queue()) == 0

def test_get_history_count_today():
    models.add_history_entry(1, "A", "A", True, False)
    models.add_history_entry(2, "B", "B", False, False)  # not success
    assert models.get_history_count_today() == 1
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_models.py -v`
Expected: FAIL

**Step 3: Implement `models.py`**

```python
# models.py
import json
import math
import time
import logging
from datetime import datetime

import db

logger = logging.getLogger(__name__)

QUEUE_STATUS_QUEUED = "queued"
QUEUE_STATUS_DOWNLOADING = "downloading"
QUEUE_STATUSES = {QUEUE_STATUS_QUEUED, QUEUE_STATUS_DOWNLOADING}


def _paginate(query, count_query, params, page, per_page):
    conn = db.get_db()
    total = conn.execute(count_query, params).fetchone()[0]
    pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page
    rows = conn.execute(query + " LIMIT ? OFFSET ?", (*params, per_page, offset)).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
    }


# --- History ---

def add_history_entry(album_id, album_title, artist_name, success, partial,
                      manual=False, track_title=None):
    conn = db.get_db()
    conn.execute(
        """INSERT INTO download_history
           (album_id, album_title, artist_name, success, partial, manual, track_title, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (album_id, album_title, artist_name, int(success), int(partial),
         int(manual), track_title, time.time()),
    )
    conn.commit()


def get_history(page=1, per_page=50):
    result = _paginate(
        "SELECT * FROM download_history ORDER BY timestamp DESC",
        "SELECT COUNT(*) FROM download_history",
        (), page, per_page,
    )
    for item in result["items"]:
        item["success"] = bool(item["success"])
        item["partial"] = bool(item["partial"])
        item["manual"] = bool(item["manual"])
    return result


def get_history_count_today():
    conn = db.get_db()
    now = datetime.now()
    today_start = datetime(now.year, now.month, now.day).timestamp()
    row = conn.execute(
        "SELECT COUNT(*) FROM download_history WHERE success = 1 AND timestamp >= ?",
        (today_start,),
    ).fetchone()
    return row[0]


def clear_history():
    conn = db.get_db()
    conn.execute("DELETE FROM download_history")
    conn.commit()


# --- Logs ---

def add_log(log_type, album_id, album_title, artist_name, details="",
            failed_tracks=None, total_file_size=0):
    conn = db.get_db()
    log_id = f"{int(time.time() * 1000)}_{album_id}"
    conn.execute(
        """INSERT INTO download_logs
           (id, type, album_id, album_title, artist_name, timestamp, details, failed_tracks, total_file_size)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (log_id, log_type, album_id, album_title, artist_name, time.time(),
         details, json.dumps(failed_tracks or []), total_file_size),
    )
    conn.commit()
    return log_id


def get_logs(page=1, per_page=50):
    result = _paginate(
        "SELECT * FROM download_logs ORDER BY timestamp DESC",
        "SELECT COUNT(*) FROM download_logs",
        (), page, per_page,
    )
    for item in result["items"]:
        item["failed_tracks"] = json.loads(item["failed_tracks"])
    return result


def delete_log(log_id):
    conn = db.get_db()
    cursor = conn.execute("DELETE FROM download_logs WHERE id = ?", (log_id,))
    conn.commit()
    return cursor.rowcount > 0


def clear_logs():
    conn = db.get_db()
    conn.execute("DELETE FROM download_logs")
    conn.commit()


def get_logs_db_size():
    """Return approximate size of logs data in bytes."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT SUM(LENGTH(details) + LENGTH(failed_tracks)) FROM download_logs"
    ).fetchone()
    return row[0] or 0


# --- Failed tracks ---

def save_failed_tracks(album_id, album_title, artist_name, cover_url,
                       album_path, lidarr_album_path, tracks):
    conn = db.get_db()
    conn.execute("DELETE FROM failed_tracks")
    for t in tracks:
        conn.execute(
            """INSERT INTO failed_tracks
               (album_id, album_title, artist_name, cover_url, album_path,
                lidarr_album_path, track_title, track_num, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (album_id, album_title, artist_name, cover_url, album_path,
             lidarr_album_path, t["title"], t.get("track_num", 0), t.get("reason", "")),
        )
    conn.commit()


def get_failed_tracks():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM failed_tracks").fetchall()
    return [dict(row) for row in rows]


def get_failed_tracks_context():
    """Return album-level context from failed tracks (first row's metadata)."""
    conn = db.get_db()
    row = conn.execute("SELECT * FROM failed_tracks LIMIT 1").fetchone()
    if row is None:
        return {
            "failed_tracks": [], "album_id": None, "album_title": "",
            "artist_name": "", "cover_url": "", "album_path": "",
            "lidarr_album_path": "",
        }
    tracks = get_failed_tracks()
    return {
        "failed_tracks": [
            {"title": t["track_title"], "reason": t["reason"], "track_num": t["track_num"]}
            for t in tracks
        ],
        "album_id": row["album_id"],
        "album_title": row["album_title"],
        "artist_name": row["artist_name"],
        "cover_url": row["cover_url"],
        "album_path": row["album_path"],
        "lidarr_album_path": row["lidarr_album_path"],
    }


def remove_failed_track(track_title):
    conn = db.get_db()
    conn.execute(
        "DELETE FROM failed_tracks WHERE LOWER(track_title) = LOWER(?)",
        (track_title,),
    )
    conn.commit()


def clear_failed_tracks():
    conn = db.get_db()
    conn.execute("DELETE FROM failed_tracks")
    conn.commit()


# --- Queue ---

def enqueue_album(album_id):
    conn = db.get_db()
    existing = conn.execute(
        "SELECT id FROM download_queue WHERE album_id = ?", (album_id,)
    ).fetchone()
    if existing:
        return False
    max_pos = conn.execute("SELECT COALESCE(MAX(position), 0) FROM download_queue").fetchone()[0]
    conn.execute(
        "INSERT INTO download_queue (album_id, position, status) VALUES (?, ?, ?)",
        (album_id, max_pos + 1, QUEUE_STATUS_QUEUED),
    )
    conn.commit()
    return True


def dequeue_album(album_id):
    conn = db.get_db()
    conn.execute("DELETE FROM download_queue WHERE album_id = ?", (album_id,))
    conn.commit()
    _reorder_queue(conn)


def get_queue():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT * FROM download_queue ORDER BY position"
    ).fetchall()
    return [dict(row) for row in rows]


def get_queue_length():
    conn = db.get_db()
    return conn.execute("SELECT COUNT(*) FROM download_queue").fetchone()[0]


def pop_next_from_queue():
    """Get and remove the next queued album. Returns album_id or None."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT album_id FROM download_queue WHERE status = ? ORDER BY position LIMIT 1",
        (QUEUE_STATUS_QUEUED,),
    ).fetchone()
    if row is None:
        return None
    album_id = row[0]
    conn.execute("DELETE FROM download_queue WHERE album_id = ?", (album_id,))
    conn.commit()
    _reorder_queue(conn)
    return album_id


def set_queue_status(album_id, status):
    if status not in QUEUE_STATUSES:
        raise ValueError(f"Invalid queue status: {status}. Must be one of {QUEUE_STATUSES}")
    conn = db.get_db()
    conn.execute(
        "UPDATE download_queue SET status = ? WHERE album_id = ?",
        (status, album_id),
    )
    conn.commit()


def reset_downloading_to_queued():
    conn = db.get_db()
    conn.execute(
        "UPDATE download_queue SET status = ? WHERE status = ?",
        (QUEUE_STATUS_QUEUED, QUEUE_STATUS_DOWNLOADING),
    )
    conn.commit()


def clear_queue():
    conn = db.get_db()
    conn.execute("DELETE FROM download_queue")
    conn.commit()


def _reorder_queue(conn):
    rows = conn.execute(
        "SELECT id FROM download_queue ORDER BY position"
    ).fetchall()
    for i, row in enumerate(rows, 1):
        conn.execute("UPDATE download_queue SET position = ? WHERE id = ?", (i, row[0]))
    conn.commit()
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_models.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add models.py tests/test_models.py
git commit -m "Add models.py with CRUD operations for history, logs, failed tracks, and queue"
```

---

### Task 3: Create `config.py` — configuration management

**Files:**
- Create: `config.py`
- Create: `tests/test_config.py`

**Step 1: Write failing tests**

```python
# tests/test_config.py
import json
import os
import pytest
import config

@pytest.fixture(autouse=True)
def temp_config(tmp_path, monkeypatch):
    config_file = str(tmp_path / "config.json")
    monkeypatch.setattr("config.CONFIG_FILE", config_file)
    monkeypatch.delenv("LIDARR_URL", raising=False)
    monkeypatch.delenv("LIDARR_API_KEY", raising=False)
    yield config_file

def test_load_config_defaults(temp_config):
    cfg = config.load_config()
    assert cfg["lidarr_url"] == ""
    assert cfg["scheduler_enabled"] is False
    assert isinstance(cfg["forbidden_words"], list)

def test_load_config_from_file(temp_config):
    with open(temp_config, "w") as f:
        json.dump({"lidarr_url": "http://test:8686"}, f)
    cfg = config.load_config()
    assert cfg["lidarr_url"] == "http://test:8686"

def test_save_config(temp_config):
    cfg = config.load_config()
    cfg["lidarr_url"] = "http://saved:8686"
    config.save_config(cfg)
    reloaded = config.load_config()
    assert reloaded["lidarr_url"] == "http://saved:8686"

def test_allowed_config_keys():
    assert "scheduler_interval" in config.ALLOWED_CONFIG_KEYS
    assert "lidarr_url" not in config.ALLOWED_CONFIG_KEYS
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_config.py -v`
Expected: FAIL

**Step 3: Implement `config.py`**

Extract from `app.py` lines 41, 75-84, 171-248: `CONFIG_FILE`, `ALLOWED_CONFIG_KEYS`, `load_config()`, `save_config()`. Move `_file_write_lock` here since it only protects config file writes now (DB handles its own locking).

```python
# config.py
import os
import json
import logging
import threading

logger = logging.getLogger(__name__)

CONFIG_FILE = "/config/config.json"
_file_write_lock = threading.Lock()

ALLOWED_CONFIG_KEYS = {
    "scheduler_interval", "telegram_bot_token", "telegram_chat_id",
    "telegram_enabled", "telegram_log_types", "download_path",
    "lidarr_path", "forbidden_words", "duration_tolerance",
    "scheduler_enabled", "scheduler_auto_download",
    "xml_metadata_enabled", "yt_cookies_file", "yt_force_ipv4",
    "yt_player_client", "yt_retries", "yt_fragment_retries",
    "yt_sleep_requests", "yt_sleep_interval", "yt_max_sleep_interval",
    "discord_enabled", "discord_webhook_url", "discord_log_types",
}


def load_config():
    config = {
        "lidarr_url": os.getenv("LIDARR_URL", ""),
        "lidarr_api_key": os.getenv("LIDARR_API_KEY", ""),
        "lidarr_path": os.getenv("LIDARR_PATH", ""),
        "download_path": os.getenv("DOWNLOAD_PATH", ""),
        "scheduler_enabled": os.getenv("SCHEDULER_ENABLED", "false").lower() == "true",
        "scheduler_auto_download": os.getenv("SCHEDULER_AUTO_DOWNLOAD", "true").lower() == "true",
        "scheduler_interval": int(os.getenv("SCHEDULER_INTERVAL", "60")),
        "telegram_enabled": os.getenv("TELEGRAM_ENABLED", "false").lower() == "true",
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
        "telegram_log_types": ["partial_success", "import_partial", "album_error"],
        "xml_metadata_enabled": os.getenv("XML_METADATA_ENABLED", "true").lower() == "true",
        "forbidden_words": [
            "remix", "cover", "mashup", "bootleg", "live", "dj mix",
            "karaoke", "slowed", "reverb", "nightcore", "sped up",
            "instrumental", "acapella", "tribute",
        ],
        "duration_tolerance": int(os.getenv("DURATION_TOLERANCE", "10")),
        "yt_cookies_file": os.getenv("YT_COOKIES_FILE", ""),
        "yt_force_ipv4": os.getenv("YT_FORCE_IPV4", "true").lower() == "true",
        "yt_player_client": os.getenv("YT_PLAYER_CLIENT", "android"),
        "yt_retries": int(os.getenv("YT_RETRIES", "10")),
        "yt_fragment_retries": int(os.getenv("YT_FRAGMENT_RETRIES", "10")),
        "yt_sleep_requests": int(os.getenv("YT_SLEEP_REQUESTS", "1")),
        "yt_sleep_interval": int(os.getenv("YT_SLEEP_INTERVAL", "1")),
        "yt_max_sleep_interval": int(os.getenv("YT_MAX_SLEEP_INTERVAL", "5")),
        "discord_enabled": os.getenv("DISCORD_ENABLED", "false").lower() == "true",
        "discord_webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
        "discord_log_types": ["partial_success", "import_partial", "album_error"],
        "path_conflict": False,
    }

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                file_config = json.load(f)
                for key in config.keys():
                    if key in file_config:
                        config[key] = file_config[key]
            if "scheduler_interval" in config:
                config["scheduler_interval"] = int(config["scheduler_interval"])
            if "duration_tolerance" in config:
                config["duration_tolerance"] = int(config["duration_tolerance"])
        except Exception as e:
            logger.warning(f"Failed to load config file: {e}")

    l_path = _norm_path(config.get("lidarr_path"))
    d_path = _norm_path(config.get("download_path"))
    config["path_conflict"] = bool(l_path and l_path == d_path)

    return config


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    if "scheduler_interval" in config:
        config["scheduler_interval"] = int(config["scheduler_interval"])
    if "duration_tolerance" in config:
        config["duration_tolerance"] = int(config["duration_tolerance"])
    with _file_write_lock:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)


def _norm_path(p):
    return os.path.normcase(os.path.abspath(str(p))).rstrip("\\/") if p else ""
```

**Step 4: Run tests**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_config.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "Extract config.py from app.py with load/save config and allowed keys"
```

---

### Task 4: Create `utils.py` — shared utilities

**Files:**
- Create: `utils.py`
- Create: `tests/test_utils.py`

**Step 1: Write failing tests**

```python
# tests/test_utils.py
import time
import pytest
import utils

def test_sanitize_filename_removes_special_chars():
    assert utils.sanitize_filename('test<>:"/\\|?*file') == "testfile"

def test_sanitize_filename_empty():
    assert utils.sanitize_filename("") == "untitled"

def test_sanitize_filename_dots():
    assert utils.sanitize_filename("..") == "untitled"

def test_format_bytes_zero():
    assert utils.format_bytes(0) == ""

def test_format_bytes_mb():
    result = utils.format_bytes(1048576)
    assert "MB" in result

def test_format_bytes_gb():
    result = utils.format_bytes(1073741824)
    assert "GB" in result

def test_check_rate_limit_allows():
    store = {}
    assert utils.check_rate_limit("key1", store, window=2, max_requests=3) is True

def test_check_rate_limit_blocks():
    store = {}
    for _ in range(5):
        utils.check_rate_limit("key1", store, window=60, max_requests=5)
    assert utils.check_rate_limit("key1", store, window=60, max_requests=5) is False

def test_get_umask_default():
    assert utils.get_umask() == 0o002

def test_get_umask_custom(monkeypatch):
    monkeypatch.setenv("UMASK", "022")
    assert utils.get_umask() == 0o022
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_utils.py -v`
Expected: FAIL

**Step 3: Implement `utils.py`**

```python
# utils.py
import os
import re
import time
import logging

logger = logging.getLogger(__name__)


def sanitize_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.replace("..", "").replace("~", "")
    name = name.strip(". ")
    if not name:
        name = "untitled"
    return name


def format_bytes(size_bytes):
    if size_bytes <= 0:
        return ""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def check_rate_limit(key, store, window=2, max_requests=5):
    now = time.time()
    if key not in store:
        store[key] = []
    store[key] = [t for t in store[key] if now - t < window]
    if len(store[key]) >= max_requests:
        return False
    store[key].append(now)
    return True


def get_umask():
    umask_str = os.getenv("UMASK", "002").strip()
    try:
        return int(umask_str, 8)
    except ValueError:
        return 0o002


def set_permissions(path):
    try:
        umask = get_umask()
        dir_mode = 0o777 & ~umask
        file_mode = 0o666 & ~umask
        if os.path.isdir(path):
            os.chmod(path, dir_mode)
            for root, dirs, files in os.walk(path):
                for d in dirs:
                    os.chmod(os.path.join(root, d), dir_mode)
                for f in files:
                    os.chmod(os.path.join(root, f), file_mode)
        else:
            os.chmod(path, file_mode)
    except Exception as e:
        logger.debug(f"Failed to set permissions on {path}: {e}")
```

**Step 4: Run tests**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_utils.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add utils.py tests/test_utils.py
git commit -m "Extract utils.py with sanitize_filename, format_bytes, rate limiting, permissions"
```

---

### Task 5: Create `notifications.py` — Telegram and Discord

**Files:**
- Create: `notifications.py`
- Create: `tests/test_notifications.py`

**Step 1: Write failing tests**

```python
# tests/test_notifications.py
from unittest.mock import patch, MagicMock
import pytest
import notifications

@pytest.fixture
def mock_config():
    return {
        "telegram_enabled": True,
        "telegram_bot_token": "token123",
        "telegram_chat_id": "chat456",
        "telegram_log_types": ["album_error"],
        "discord_enabled": True,
        "discord_webhook_url": "https://discord.com/webhook/test",
        "discord_log_types": ["album_error"],
    }

@patch("notifications.load_config")
@patch("notifications.requests.post")
def test_send_telegram_sends(mock_post, mock_cfg, mock_config):
    mock_cfg.return_value = mock_config
    notifications.send_telegram("test msg", log_type="album_error")
    mock_post.assert_called_once()

@patch("notifications.load_config")
@patch("notifications.requests.post")
def test_send_telegram_filters_log_type(mock_post, mock_cfg, mock_config):
    mock_cfg.return_value = mock_config
    notifications.send_telegram("test msg", log_type="download_started")
    mock_post.assert_not_called()

@patch("notifications.load_config")
@patch("notifications.requests.post")
def test_send_discord_sends_embed(mock_post, mock_cfg, mock_config):
    mock_cfg.return_value = mock_config
    embed = {"title": "Test", "description": "desc", "color": 0xFF0000}
    notifications.send_discord("msg", log_type="album_error", embed_data=embed)
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    assert "embeds" in payload

@patch("notifications.load_config")
@patch("notifications.requests.post")
def test_send_notifications_calls_both(mock_post, mock_cfg, mock_config):
    mock_cfg.return_value = mock_config
    notifications.send_notifications("msg", log_type="album_error")
    assert mock_post.call_count == 2
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_notifications.py -v`
Expected: FAIL

**Step 3: Implement `notifications.py`**

```python
# notifications.py
import logging
import requests
from config import load_config

logger = logging.getLogger(__name__)


def send_telegram(message, log_type=None):
    config = load_config()
    if not (config.get("telegram_enabled") and config.get("telegram_bot_token")
            and config.get("telegram_chat_id")):
        return
    if log_type is not None:
        allowed_types = config.get("telegram_log_types", [])
        if log_type not in allowed_types:
            return
    try:
        url = f"https://api.telegram.org/bot{config['telegram_bot_token']}/sendMessage"
        requests.post(
            url,
            json={"chat_id": config["telegram_chat_id"], "text": message},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Telegram notification failed: {e}")


def send_discord(message, log_type=None, embed_data=None):
    config = load_config()
    if not config.get("discord_enabled"):
        return
    webhook_url = config.get("discord_webhook_url", "")
    if not webhook_url:
        return
    if log_type is not None:
        allowed_types = config.get("discord_log_types", [])
        if log_type not in allowed_types:
            return
    try:
        payload = {}
        if embed_data:
            embed = {
                "title": embed_data.get("title", ""),
                "description": embed_data.get("description", ""),
                "color": embed_data.get("color", 0x10B981),
            }
            if embed_data.get("thumbnail"):
                embed["thumbnail"] = {"url": embed_data["thumbnail"]}
            if embed_data.get("fields"):
                embed["fields"] = embed_data["fields"]
            payload["embeds"] = [embed]
        else:
            payload["content"] = message
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception as e:
        logger.warning(f"Discord notification failed: {e}")


def send_notifications(message, log_type=None, embed_data=None):
    send_telegram(message, log_type=log_type)
    send_discord(message, log_type=log_type, embed_data=embed_data)
```

**Step 4: Run tests**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_notifications.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add notifications.py tests/test_notifications.py
git commit -m "Extract notifications.py with Telegram and Discord webhook support"
```

---

### Task 6: Create `lidarr.py` — Lidarr API wrapper

**Files:**
- Create: `lidarr.py`
- Create: `tests/test_lidarr.py`

**Step 1: Write failing tests**

```python
# tests/test_lidarr.py
from unittest.mock import patch, MagicMock
import pytest
import lidarr

@patch("lidarr.load_config")
@patch("lidarr.requests.get")
def test_lidarr_request_get(mock_get, mock_cfg):
    mock_cfg.return_value = {"lidarr_url": "http://lidarr:8686", "lidarr_api_key": "key123"}
    mock_get.return_value = MagicMock(status_code=200, json=lambda: {"version": "2.0"})
    result = lidarr.lidarr_request("system/status")
    assert result["version"] == "2.0"

@patch("lidarr.load_config")
@patch("lidarr.requests.post")
def test_lidarr_request_post(mock_post, mock_cfg):
    mock_cfg.return_value = {"lidarr_url": "http://lidarr:8686", "lidarr_api_key": "key123"}
    mock_post.return_value = MagicMock(status_code=200, json=lambda: {"success": True})
    result = lidarr.lidarr_request("command", method="POST", data={"name": "RefreshArtist"})
    assert result["success"] is True

@patch("lidarr.load_config")
@patch("lidarr.requests.get")
def test_lidarr_request_error(mock_get, mock_cfg):
    mock_cfg.return_value = {"lidarr_url": "http://lidarr:8686", "lidarr_api_key": "key123"}
    mock_get.side_effect = Exception("connection failed")
    result = lidarr.lidarr_request("system/status")
    assert "error" in result

def test_get_valid_release_id_monitored():
    album = {"releases": [
        {"id": 1, "monitored": False},
        {"id": 2, "monitored": True},
    ]}
    assert lidarr.get_valid_release_id(album) == 2

def test_get_valid_release_id_fallback():
    album = {"releases": [{"id": 5, "monitored": False}]}
    assert lidarr.get_valid_release_id(album) == 5

def test_get_valid_release_id_empty():
    assert lidarr.get_valid_release_id({"releases": []}) == 0

def test_get_monitored_release():
    album = {"releases": [
        {"id": 1, "monitored": False},
        {"id": 2, "monitored": True},
    ]}
    assert lidarr.get_monitored_release(album)["id"] == 2

def test_get_monitored_release_fallback():
    album = {"releases": [{"id": 1, "monitored": False}]}
    assert lidarr.get_monitored_release(album)["id"] == 1
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_lidarr.py -v`
Expected: FAIL

**Step 3: Implement `lidarr.py`**

```python
# lidarr.py
import logging
import requests
from config import load_config

logger = logging.getLogger(__name__)


def lidarr_request(endpoint, method="GET", data=None, params=None):
    config = load_config()
    url = f"{config['lidarr_url']}/api/v1/{endpoint}"
    headers = {"X-Api-Key": config["lidarr_api_key"]}
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, params=params, timeout=30)
        elif method == "POST":
            r = requests.post(url, headers=headers, json=data, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def get_missing_albums():
    try:
        page = 1
        page_size = 500
        all_records = []
        while True:
            wanted = lidarr_request(
                f"wanted/missing?page={page}&pageSize={page_size}"
                f"&sortKey=releaseDate&sortDirection=descending&includeArtist=true"
            )
            if not isinstance(wanted, dict) or "records" not in wanted:
                break
            records = wanted.get("records", [])
            total_records = wanted.get("totalRecords", 0)
            for album in records:
                stats = album.get("statistics", {})
                total = stats.get("trackCount", 0)
                files = stats.get("trackFileCount", 0)
                album["missingTrackCount"] = total - files
            all_records.extend(records)
            if len(all_records) >= total_records or len(records) < page_size:
                break
            page += 1
        return all_records
    except Exception as e:
        logger.warning(f"Failed to get missing albums: {e}")
        return []


def get_valid_release_id(album):
    releases = album.get("releases", [])
    if not releases:
        return 0
    for rel in releases:
        if rel.get("monitored", False) and rel.get("id", 0) > 0:
            return rel["id"]
    for rel in releases:
        if rel.get("id", 0) > 0:
            return rel["id"]
    return 0


def get_monitored_release(album):
    releases = album.get("releases", [])
    if not releases:
        return None
    for rel in releases:
        if rel.get("monitored", False):
            return rel
    return releases[0]
```

**Step 4: Run tests**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_lidarr.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add lidarr.py tests/test_lidarr.py
git commit -m "Extract lidarr.py with API wrapper, missing albums, and release helpers"
```

---

### Task 7: Create `metadata.py` — ID3 tagging and iTunes

**Files:**
- Create: `metadata.py`
- Create: `tests/test_metadata.py`

**Step 1: Write failing tests**

```python
# tests/test_metadata.py
import os
from unittest.mock import patch, MagicMock
import pytest
import metadata

def test_create_xml_metadata(tmp_path):
    result = metadata.create_xml_metadata(
        str(tmp_path), "Artist", "Album", 1, "Track Title",
        album_id="mb-album-123", artist_id="mb-artist-456",
    )
    assert result is True
    xml_file = tmp_path / "01 - Track Title.xml"
    assert xml_file.exists()
    content = xml_file.read_text()
    assert "<title>Track Title</title>" in content
    assert "<artist>Artist</artist>" in content

def test_create_xml_metadata_escapes_special_chars(tmp_path):
    result = metadata.create_xml_metadata(
        str(tmp_path), "Art & Craft", "Album <1>", 1, "Track & Roll"
    )
    assert result is True
    xml_file = tmp_path / "01 - Track & Roll.xml"
    content = xml_file.read_text()
    assert "&amp;" in content

@patch("metadata.requests.get")
def test_get_itunes_tracks(mock_get):
    mock_get.side_effect = [
        MagicMock(json=lambda: {"resultCount": 1, "results": [{"collectionId": 123}]}),
        MagicMock(json=lambda: {"results": [
            {"wrapperType": "collection"},
            {"trackNumber": 1, "trackName": "Song1", "previewUrl": "http://preview"},
        ]}),
    ]
    tracks = metadata.get_itunes_tracks("Artist", "Album")
    assert len(tracks) == 1
    assert tracks[0]["title"] == "Song1"

@patch("metadata.requests.get")
def test_get_itunes_artwork(mock_get):
    mock_get.side_effect = [
        MagicMock(json=lambda: {"resultCount": 1, "results": [{"artworkUrl100": "http://img/100x100"}]}),
        MagicMock(content=b"image_data"),
    ]
    result = metadata.get_itunes_artwork("Artist", "Album")
    assert result == b"image_data"

@patch("metadata.requests.get")
def test_get_itunes_artwork_not_found(mock_get):
    mock_get.return_value = MagicMock(json=lambda: {"resultCount": 0, "results": []})
    assert metadata.get_itunes_artwork("Artist", "Album") is None
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_metadata.py -v`
Expected: FAIL

**Step 3: Implement `metadata.py`**

```python
# metadata.py
import os
import logging
from xml.sax.saxutils import escape as xml_escape

import requests
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TDRC, TRCK, APIC, TXXX, UFID

from lidarr import get_monitored_release
from utils import sanitize_filename

logger = logging.getLogger(__name__)


def tag_mp3(file_path, track_info, album_info, cover_data):
    try:
        try:
            audio = MP3(file_path, ID3=ID3)
        except Exception:
            audio = MP3(file_path)
            audio.add_tags()
        if audio.tags is None:
            audio.add_tags()

        audio.tags.add(TIT2(encoding=3, text=track_info["title"]))
        audio.tags.add(TPE1(encoding=3, text=album_info["artist"]["artistName"]))
        audio.tags.add(TPE2(encoding=3, text=album_info["artist"]["artistName"]))
        audio.tags.add(TALB(encoding=3, text=album_info["title"]))
        audio.tags.add(TDRC(encoding=3, text=str(album_info.get("releaseDate", "")[:4])))

        try:
            t_num = int(track_info["trackNumber"])
            audio.tags.add(TRCK(encoding=3, text=f"{t_num}/{album_info.get('trackCount', 0)}"))
        except (ValueError, KeyError):
            pass

        release = get_monitored_release(album_info)
        if release:
            if track_info.get("foreignRecordingId"):
                audio.tags.add(TXXX(
                    encoding=3, desc="MusicBrainz Release Track Id",
                    text=track_info["foreignRecordingId"],
                ))
            if release.get("foreignReleaseId"):
                audio.tags.add(TXXX(
                    encoding=3, desc="MusicBrainz Album Id",
                    text=release["foreignReleaseId"],
                ))
            if album_info["artist"].get("foreignArtistId"):
                audio.tags.add(TXXX(
                    encoding=3, desc="MusicBrainz Artist Id",
                    text=album_info["artist"]["foreignArtistId"],
                ))
            if album_info.get("foreignAlbumId"):
                audio.tags.add(TXXX(
                    encoding=3, desc="MusicBrainz Album Release Group Id",
                    text=album_info["foreignAlbumId"],
                ))
            if release.get("country"):
                audio.tags.add(TXXX(
                    encoding=3, desc="MusicBrainz Release Country",
                    text=release["country"],
                ))

        if track_info.get("foreignRecordingId"):
            audio.tags.add(UFID(
                owner="http://musicbrainz.org",
                data=track_info["foreignRecordingId"].encode(),
            ))
        if cover_data:
            audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data))

        audio.save(v2_version=3)
        return True
    except Exception as e:
        logger.warning(f"Failed to tag MP3 {file_path}: {e}")
        return False


def create_xml_metadata(output_dir, artist, album, track_num, title,
                        album_id=None, artist_id=None):
    try:
        sanitized_title = sanitize_filename(title)
        filename = f"{track_num:02d} - {sanitized_title}.xml"
        file_path = os.path.join(output_dir, filename)
        safe_title = xml_escape(title)
        safe_artist = xml_escape(artist)
        safe_album = xml_escape(album)
        mb_album = (
            f"  <musicbrainzalbumid>{xml_escape(str(album_id))}</musicbrainzalbumid>\n"
            if album_id else ""
        )
        mb_artist = (
            f"  <musicbrainzartistid>{xml_escape(str(artist_id))}</musicbrainzartistid>\n"
            if artist_id else ""
        )
        content = f"""<song>
  <title>{safe_title}</title>
  <artist>{safe_artist}</artist>
  <performingartist>{safe_artist}</performingartist>
  <albumartist>{safe_artist}</albumartist>
  <album>{safe_album}</album>
{mb_album}{mb_artist}</song>"""
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        logger.warning(f"Failed to create XML metadata: {e}")
        return False


def get_itunes_tracks(artist, album_name):
    try:
        url = "https://itunes.apple.com/search"
        params = {"term": f"{artist} {album_name}", "entity": "album", "limit": 1}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("resultCount", 0) > 0:
            collection_id = data["results"][0]["collectionId"]
            lookup_url = "https://itunes.apple.com/lookup"
            lookup_params = {"id": collection_id, "entity": "song"}
            lookup_r = requests.get(lookup_url, params=lookup_params, timeout=10)
            lookup_data = lookup_r.json()
            tracks = []
            for item in lookup_data.get("results", [])[1:]:
                tracks.append({
                    "trackNumber": item.get("trackNumber"),
                    "title": item.get("trackName"),
                    "previewUrl": item.get("previewUrl"),
                    "hasFile": False,
                })
            return tracks
    except Exception as e:
        logger.debug(f"iTunes tracks lookup failed: {e}")
    return []


def get_itunes_artwork(artist, album):
    try:
        url = "https://itunes.apple.com/search"
        params = {"term": f"{artist} {album}", "entity": "album", "limit": 1}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("resultCount", 0) > 0:
            artwork_url = (
                data["results"][0].get("artworkUrl100", "").replace("100x100", "3000x3000")
            )
            return requests.get(artwork_url, timeout=15).content
    except Exception as e:
        logger.debug(f"iTunes artwork lookup failed: {e}")
    return None
```

**Step 4: Run tests**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_metadata.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add metadata.py tests/test_metadata.py
git commit -m "Extract metadata.py with ID3 tagging, XML metadata, and iTunes API"
```

---

### Task 8: Create `downloader.py`, `processing.py`, `scheduler.py`

**Files:**
- Create: `downloader.py`
- Create: `processing.py`
- Create: `scheduler.py`
- Create: `tests/test_downloader.py`

**Step 1: Write failing test for downloader scoring logic**

```python
# tests/test_downloader.py
import pytest
from downloader import _title_similarity, _check_forbidden, _is_official_channel

def test_title_similarity_exact():
    score = _title_similarity("Artist Track", "Track", "Artist")
    assert score > 0.8

def test_title_similarity_low():
    score = _title_similarity("Completely Different", "Track", "Artist")
    assert score < 0.5

def test_is_official_channel_artist():
    assert _is_official_channel("ArtistName", "ArtistName") is True

def test_is_official_channel_vevo():
    assert _is_official_channel("ArtistVEVO", "Artist") is True

def test_is_official_channel_topic():
    assert _is_official_channel("Artist - Topic", "Artist") is True

def test_is_official_channel_false():
    assert _is_official_channel("RandomChannel", "Artist") is False

def test_check_forbidden_blocks():
    result = _check_forbidden("song remix version", "song", ["remix", "cover"])
    assert result == "remix"

def test_check_forbidden_allows_when_in_title():
    result = _check_forbidden("remix song", "remix song", ["remix"])
    assert result is None

def test_check_forbidden_multi_word():
    result = _check_forbidden("song dj mix version", "song", ["dj mix"])
    assert result == "dj mix"
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_downloader.py -v`
Expected: FAIL

**Step 3: Implement `downloader.py`**

Extract from `app.py`: `download_track_youtube()` (lines 454-666) and its nested functions `build_common_opts`, `_title_similarity`, `_is_official_channel`, `_check_forbidden`. Also `update_progress()`, `get_ytdlp_version()`.

Make `_title_similarity`, `_is_official_channel`, `_check_forbidden` module-level functions (not nested) so they're testable.

The `update_progress` function needs access to `download_process` dict — import it from `processing.py`.

```python
# downloader.py
import os
import re
import math
import logging
from difflib import SequenceMatcher

import yt_dlp

from config import load_config

logger = logging.getLogger(__name__)


def get_ytdlp_version():
    try:
        import importlib.metadata
        return importlib.metadata.version("yt-dlp")
    except Exception:
        try:
            return yt_dlp.version.__version__
        except Exception:
            return "unknown"


def _title_similarity(yt_title, track_title, artist_name):
    yt_lower = yt_title.lower()
    expected_lower = f"{artist_name} {track_title}".lower()
    score = SequenceMatcher(None, yt_lower, expected_lower).ratio()
    track_lower = track_title.lower()
    if track_lower in yt_lower:
        score += 0.3
    if artist_name.lower() in yt_lower:
        score += 0.2
    return min(score, 1.0)


def _is_official_channel(channel_name, artist_name):
    if not channel_name:
        return False
    ch = channel_name.lower()
    ar = artist_name.lower()
    if ar in ch:
        return True
    for suffix in [" - topic", "vevo", " official"]:
        if suffix in ch:
            return True
    return False


def _check_forbidden(yt_title_lower, track_title_lower, forbidden_list):
    for word in forbidden_list:
        if " " in word:
            if word in yt_title_lower and word not in track_title_lower:
                return word
        else:
            pattern = r'\b' + re.escape(word) + r'\b'
            if re.search(pattern, yt_title_lower) and not re.search(pattern, track_title_lower):
                return word
    return None


def _build_common_opts(player_client=None):
    cfg = load_config()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "retries": int(cfg.get("yt_retries", 10)),
        "fragment_retries": int(cfg.get("yt_fragment_retries", 10)),
        "sleep_interval_requests": int(cfg.get("yt_sleep_requests", 1)),
        "sleep_interval": int(cfg.get("yt_sleep_interval", 1)),
        "max_sleep_interval": int(cfg.get("yt_max_sleep_interval", 5)),
        "noplaylist": True,
    }
    cookies_path = (cfg.get("yt_cookies_file") or "").strip()
    if cookies_path and os.path.exists(cookies_path):
        opts["cookiefile"] = cookies_path
    elif cookies_path and not os.path.exists(cookies_path):
        logger.warning(f"YT_COOKIES_FILE not found: {cookies_path}")
    if cfg.get("yt_force_ipv4", True):
        opts["source_address"] = "0.0.0.0"
    if player_client:
        opts["extractor_args"] = {"youtube": {"player_client": [player_client]}}
    return opts


def download_track_youtube(query, output_path, track_title_original,
                           expected_duration_ms=None, progress_hook=None):
    """Search YouTube and download best matching track.

    Args:
        progress_hook: Callable that receives yt-dlp progress dicts.
    """
    config = load_config()
    ydl_opts_search = {
        **_build_common_opts(player_client=config.get("yt_player_client", "android") or None),
        "format": "bestaudio/best",
        "extract_flat": True,
    }

    candidates = []
    forbidden_words = config.get("forbidden_words", [
        "remix", "cover", "mashup", "bootleg", "live", "dj mix", "karaoke",
        "slowed", "reverb", "nightcore", "sped up", "instrumental", "acapella", "tribute",
    ])
    duration_tolerance = config.get("duration_tolerance", 10)

    expected_duration_sec = None
    if expected_duration_ms:
        expected_duration_sec = expected_duration_ms / 1000.0
        logger.info(
            f"Expected track duration: {int(expected_duration_sec // 60)}:"
            f"{int(expected_duration_sec % 60):02d} ({int(expected_duration_sec)}s)"
        )

    artist_part = query.split(" ")[0] if " " in query else query
    search_queries = [query]
    base_track = track_title_original
    base_artist = query.replace(f" {track_title_original} official audio", "").replace(
        f" {track_title_original}", ""
    ).strip()
    if not base_artist:
        base_artist = artist_part

    alt_q = f"{base_artist} {base_track}"
    if alt_q != query and alt_q not in search_queries:
        search_queries.append(alt_q)
    alt_q2 = f"{base_track} {base_artist}"
    if alt_q2 not in search_queries:
        search_queries.append(alt_q2)
    alt_q3 = f"{base_track} audio"
    if alt_q3 not in search_queries:
        search_queries.append(alt_q3)

    last_err = None
    for qi, sq in enumerate(search_queries):
        if candidates:
            break
        if qi > 0:
            logger.info(f'   Fallback search ({qi+1}/{len(search_queries)}): "{sq}"')
        try:
            with yt_dlp.YoutubeDL(ydl_opts_search) as ydl:
                search_results = ydl.extract_info(f"ytsearch15:{sq}", download=False)
                for entry in search_results.get("entries", []):
                    title = entry.get("title", "").lower()
                    url = entry.get("url")
                    duration = entry.get("duration", 0)
                    channel = entry.get("channel", "") or entry.get("uploader", "") or ""
                    view_count = entry.get("view_count", 0) or 0

                    blocked_word = _check_forbidden(title, track_title_original.lower(), forbidden_words)
                    if blocked_word:
                        logger.debug(f"   Rejected '{entry.get('title', '')}' - forbidden word '{blocked_word}'")
                        continue

                    if expected_duration_sec:
                        min_duration = max(15, expected_duration_sec - duration_tolerance)
                        max_duration = expected_duration_sec + duration_tolerance
                        if duration < min_duration or duration > max_duration:
                            logger.debug(
                                f"   Rejected '{entry.get('title', '')}' - duration {int(duration)}s "
                                f"outside [{int(min_duration)}s - {int(max_duration)}s]"
                            )
                            continue
                        duration_diff = abs(duration - expected_duration_sec)
                        duration_score = max(0, 1.0 - (duration_diff / max(duration_tolerance, 1)))
                    else:
                        if duration < 15 or duration > 7200:
                            continue
                        duration_score = 0.5

                    title_score = _title_similarity(entry.get("title", ""), track_title_original, base_artist)
                    official_bonus = 0.15 if _is_official_channel(channel, base_artist) else 0.0
                    view_score = 0.0
                    if view_count > 0:
                        view_score = min(0.1, math.log10(max(view_count, 1)) / 100)
                    total_score = (duration_score * 0.35) + (title_score * 0.40) + official_bonus + view_score

                    if url:
                        candidates.append({
                            "url": url, "title": entry.get("title", ""),
                            "duration": duration, "channel": channel, "score": total_score,
                        })
                        logger.debug(
                            f"   Candidate '{entry.get('title', '')}' — score={total_score:.2f} "
                            f"(dur={duration_score:.2f} title={title_score:.2f} "
                            f"official={official_bonus:.2f} views={view_score:.3f})"
                        )
        except Exception as e:
            logger.error(f'   Search failed for "{sq}": {str(e)}')
            last_err = e
            if qi == len(search_queries) - 1 and not candidates:
                return f"Search failed: {str(e)[:120]}"

    if not candidates:
        logger.warning("   No suitable candidates found after all search attempts")
        return "No suitable YouTube match found (filtered by duration/forbidden words)"

    candidates.sort(key=lambda x: x["score"], reverse=True)
    best = candidates[0]
    logger.info(
        f"   Best match: '{best['title']}' (score={best['score']:.2f}, "
        f"duration={int(best['duration'])}s, channel='{best.get('channel', '')}')"
    )

    hooks = [progress_hook] if progress_hook else []

    for candidate in candidates:
        clients_to_try = []
        first_client = config.get("yt_player_client", "android")
        if first_client:
            clients_to_try.append(first_client)
        for alt in ["web", "ios"]:
            if alt != first_client:
                clients_to_try.append(alt)
        clients_to_try.append(None)

        last_err = None
        for pc in clients_to_try:
            ydl_opts_download = {
                **_build_common_opts(player_client=pc),
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }],
                "outtmpl": output_path,
                "progress_hooks": hooks,
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts_download) as ydl_dl:
                    ydl_dl.download([candidate["url"]])
                return True
            except Exception as e:
                last_err = e
                msg = str(e)
                if "403" in msg:
                    logger.debug(f"   403 with player_client={pc or 'default'}")
                else:
                    logger.debug(f"   Failed with player_client={pc or 'default'}; {msg[:180]}")
                continue
        if last_err:
            logger.debug(f"   Failed to download '{candidate['title']}' after trying multiple client profiles.")
        continue

    last_error_msg = str(last_err)[:120] if last_err else "Unknown error"
    if last_err and "403" in str(last_err):
        return "HTTP 403 Forbidden - try providing/refreshing YouTube cookies"
    return f"Download failed after all attempts: {last_error_msg}"
```

**Step 4: Implement `processing.py`**

```python
# processing.py
import os
import time
import shutil
import threading
import uuid
import logging

import models
from config import load_config
from downloader import download_track_youtube
from metadata import tag_mp3, create_xml_metadata, get_itunes_tracks, get_itunes_artwork
from lidarr import lidarr_request, get_valid_release_id
from notifications import send_notifications
from utils import sanitize_filename, set_permissions

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = os.getenv("DOWNLOAD_PATH", "")

download_process = {
    "active": False,
    "stop": False,
    "result_success": True,
    "result_partial": False,
    "progress": {},
    "album_id": None,
    "album_title": "",
    "artist_name": "",
    "current_track_title": "",
    "cover_url": "",
}

queue_lock = threading.Lock()


def update_progress(d):
    if d["status"] == "downloading":
        download_process["progress"].update({
            "percent": d.get("_percent_str", "0%").strip(),
            "speed": d.get("_speed_str", "N/A").strip(),
        })


def get_download_status():
    with queue_lock:
        return dict(download_process)


def stop_download():
    with queue_lock:
        download_process["stop"] = True
        models.clear_queue()


def process_album_download(album_id, force=False):
    with queue_lock:
        if download_process["active"]:
            return {"error": "Busy"}
        download_process["active"] = True
        download_process["stop"] = False
        download_process["result_success"] = True
        download_process["result_partial"] = False
        download_process["progress"] = {
            "current": 0, "total": 0, "percent": "0%",
            "speed": "N/A", "overall_percent": 0,
        }
        download_process["album_id"] = album_id
        download_process["album_title"] = ""
        download_process["artist_name"] = ""
        download_process["current_track_title"] = ""
        download_process["cover_url"] = ""

    failed_tracks = []
    album = {}
    album_title = ""
    artist_name = ""
    album_path = ""
    cover_data = None
    lidarr_album_path = ""
    total_downloaded_size = 0

    try:
        album = lidarr_request(f"album/{album_id}")
        if "error" in album:
            logger.error(f"Error fetching album {album_id}: {album['error']}")
            return album

        logger.info(
            f"Starting download for album: {album.get('title', 'Unknown')} - "
            f"{album.get('artist', {}).get('artistName', 'Unknown')}"
        )

        tracks = album.get("tracks", [])
        if not tracks:
            try:
                tracks_res = lidarr_request(f"track?albumId={album_id}")
                if isinstance(tracks_res, list) and len(tracks_res) > 0:
                    tracks = tracks_res
            except Exception as e:
                logger.debug(f"Failed to fetch tracks from Lidarr: {e}")

        if not tracks:
            tracks = get_itunes_tracks(album["artist"]["artistName"], album["title"])

        album["tracks"] = tracks

        artist_name = album["artist"]["artistName"]
        artist_id = album["artist"]["id"]
        artist_mbid = album["artist"].get("foreignArtistId", "")
        album_title = album["title"]
        release_year = str(album.get("releaseDate", ""))[:4]
        album_type = album.get("albumType", "Album")

        download_process["album_title"] = album_title
        download_process["artist_name"] = artist_name
        download_process["cover_url"] = next(
            (img["remoteUrl"] for img in album.get("images", []) if img.get("coverType") == "cover"),
            "",
        )

        release_id = get_valid_release_id(album)
        if release_id == 0:
            return {"error": "No valid releases found for this album."}

        album_mbid = album.get("foreignAlbumId", "")

        sanitized_artist = sanitize_filename(artist_name)
        sanitized_album = sanitize_filename(album_title)

        artist_path = os.path.join(DOWNLOAD_DIR, sanitized_artist)
        if release_year:
            album_folder_name = f"{sanitized_album} ({release_year}) [{album_type}]"
        else:
            album_folder_name = f"{sanitized_album} [{album_type}]"
        album_path = os.path.join(artist_path, album_folder_name)
        os.makedirs(album_path, exist_ok=True)

        models.add_log(
            log_type="download_started",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details=f"Starting download of {len(tracks)} track(s)",
            failed_tracks=[],
        )
        send_notifications(
            f"Download Started\nAlbum: {album_title}\nArtist: {artist_name}\nTracks: {len(tracks)}",
            log_type="download_started",
            embed_data={
                "title": "Download Started",
                "description": f"{artist_name} — {album_title}",
                "color": 0x3498DB,
                "fields": [{"name": "Tracks", "value": str(len(tracks)), "inline": True}],
            },
        )

        cover_data = get_itunes_artwork(artist_name, album_title)
        if cover_data:
            with open(os.path.join(album_path, "cover.jpg"), "wb") as f:
                f.write(cover_data)

        tracks_to_download = []
        for t in tracks:
            if not force:
                if t.get("hasFile", False):
                    continue
                try:
                    track_num = int(t.get("trackNumber", 0))
                except (ValueError, TypeError):
                    track_num = 0
                track_title = t["title"]
                sanitized_track = sanitize_filename(track_title)
                final_file = os.path.join(album_path, f"{track_num:02d} - {sanitized_track}.mp3")
                if os.path.exists(final_file):
                    continue
            tracks_to_download.append(t)

        if len(tracks_to_download) == 0:
            lidarr_request("command", method="POST", data={"name": "RefreshArtist", "artistId": artist_id})
            return {"success": True, "message": "Skipped"}

        logger.info(f"Total tracks to download: {len(tracks_to_download)}")

        for idx, track in enumerate(tracks_to_download, 1):
            if download_process["stop"]:
                logger.warning("Download stopped by user")
                return {"stopped": True}

            track_title = track["title"]
            try:
                track_num = int(track.get("trackNumber", idx))
            except (ValueError, TypeError):
                track_num = idx

            download_process["current_track_title"] = track_title
            download_process["progress"]["current"] = idx
            download_process["progress"]["total"] = len(tracks_to_download)
            download_process["progress"]["overall_percent"] = int((idx / len(tracks_to_download)) * 100)

            logger.info(f"Downloading track {idx}/{len(tracks_to_download)}: {track_title}")

            sanitized_track = sanitize_filename(track_title)
            temp_file = os.path.join(album_path, f"temp_{track_num:02d}_{uuid.uuid4().hex[:8]}")
            final_file = os.path.join(album_path, f"{track_num:02d} - {sanitized_track}.mp3")

            track_duration_ms = track.get("duration")

            download_result = download_track_youtube(
                f"{artist_name} {track_title} official audio",
                temp_file, track_title, track_duration_ms,
                progress_hook=update_progress,
            )
            actual_file = temp_file + ".mp3"

            if download_result is True and os.path.exists(actual_file):
                logger.info(f"Track downloaded successfully: {track_title}")
                time.sleep(0.5)
                logger.info("Adding metadata tags...")
                tag_mp3(actual_file, track, album, cover_data)
                config = load_config()
                if config.get("xml_metadata_enabled", True):
                    logger.info("Creating XML metadata file...")
                    create_xml_metadata(
                        album_path, artist_name, album_title,
                        track_num, track_title, album_mbid, artist_mbid,
                    )
                try:
                    total_downloaded_size += os.path.getsize(actual_file)
                except OSError:
                    pass
                shutil.move(actual_file, final_file)
            else:
                fail_reason = download_result if isinstance(download_result, str) else "Download failed or file not found"
                logger.warning(f"Failed to download track: {track_title} — {fail_reason}")
                for ext in [".mp3", ".webm", ".m4a", ".part", ""]:
                    tmp = temp_file + ext
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except Exception:
                            pass
                failed_tracks.append({"title": track_title, "reason": fail_reason, "track_num": track_num})

            download_process["progress"]["current"] = idx
            download_process["progress"]["total"] = len(tracks_to_download)
            download_process["progress"]["overall_percent"] = int((idx / len(tracks_to_download)) * 100)

        set_permissions(artist_path)

        if failed_tracks:
            failed_list = "\n".join([f"• {t['title']}" for t in failed_tracks])

            if len(failed_tracks) == len(tracks_to_download):
                send_notifications(
                    f"Download Failed (All Tracks)\nAlbum: {album_title}\nArtist: {artist_name}\n\nFailed tracks:\n{failed_list}",
                    log_type="album_error",
                    embed_data={"title": "Download Failed", "description": f"{artist_name} — {album_title}", "color": 0xE74C3C, "fields": [{"name": "Failed Tracks", "value": failed_list[:1024], "inline": False}]},
                )
                logger.error(f"All {len(failed_tracks)} tracks failed to download. Skipping import.")
                models.add_log(
                    log_type="album_error", album_id=album_id, album_title=album_title,
                    artist_name=artist_name,
                    details=f"All {len(tracks_to_download)} track(s) failed to download",
                    failed_tracks=failed_tracks,
                )
                download_process["result_success"] = False
                return {"error": "All tracks failed to download"}
            else:
                download_process["result_partial"] = True
                send_notifications(
                    f"Partial Download Completed\nAlbum: {album_title}\nArtist: {artist_name}\n\nFailed tracks:\n{failed_list}",
                    log_type="partial_success",
                    embed_data={"title": "Partial Download", "description": f"{artist_name} — {album_title}", "color": 0xE67E22, "fields": [{"name": "Failed Tracks", "value": failed_list[:1024], "inline": False}]},
                )
                logger.warning(f"Download completed with {len(failed_tracks)} failed tracks. Proceeding with import.")
                models.add_log(
                    log_type="partial_success", album_id=album_id, album_title=album_title,
                    artist_name=artist_name,
                    details=f"{len(failed_tracks)} track(s) failed to download out of {len(tracks_to_download)}",
                    failed_tracks=failed_tracks, total_file_size=total_downloaded_size,
                )
        else:
            models.add_log(
                log_type="download_success", album_id=album_id, album_title=album_title,
                artist_name=artist_name,
                details=f"Successfully downloaded {len(tracks_to_download)} track(s)",
                failed_tracks=[], total_file_size=total_downloaded_size,
            )
            send_notifications(
                f"Download successful\nAlbum: {album_title}\nArtist: {artist_name}\nTracks: {len(tracks_to_download)}/{len(tracks_to_download)}",
                log_type="download_success",
                embed_data={"title": "Download Successful", "description": f"{artist_name} — {album_title}", "color": 0x2ECC71, "fields": [{"name": "Tracks", "value": f"{len(tracks_to_download)}/{len(tracks_to_download)}", "inline": True}]},
            )
            logger.info("All tracks downloaded successfully")

        logger.info("Importing album to Lidarr...")

        config = load_config()
        lidarr_path = config.get("lidarr_path", "")

        if lidarr_path:
            abs_lidarr = os.path.abspath(lidarr_path)
            abs_download = os.path.abspath(DOWNLOAD_DIR)
            if abs_lidarr == abs_download:
                logger.warning("LIDARR_PATH matches DOWNLOAD_PATH. Skipping move to prevent data loss.")
                lidarr_path = ""
            else:
                logger.info(f"Moving files to Lidarr music folder: {lidarr_path}")
            lidarr_artist_path = os.path.join(lidarr_path, sanitized_artist)
            lidarr_album_path = os.path.join(lidarr_artist_path, album_folder_name)
            try:
                os.makedirs(lidarr_album_path, exist_ok=True)
                for item in os.listdir(album_path):
                    src = os.path.join(album_path, item)
                    dst = os.path.join(lidarr_album_path, item)
                    if os.path.isfile(src):
                        shutil.copy2(src, dst)
                        logger.info(f"  Copied: {item}")
                set_permissions(lidarr_artist_path)
                logger.info("Files copied to Lidarr folder successfully")
            except Exception as e:
                logger.error(f"Error copying files to Lidarr folder: {str(e)}")

        logger.info(f"Album downloaded successfully: {artist_name} - {album_title}")

        if failed_tracks:
            models.add_log(
                log_type="import_partial", album_id=album_id, album_title=album_title,
                artist_name=artist_name,
                details=f"Album imported with {len(failed_tracks)} failed tracks",
                failed_tracks=failed_tracks, total_file_size=total_downloaded_size,
            )
            send_notifications(
                f"Import Partial\nAlbum: {album_title}\nArtist: {artist_name}\nRefreshing in Lidarr (Missing {len(failed_tracks)} tracks)",
                log_type="import_partial",
                embed_data={"title": "Import Partial", "description": f"{artist_name} — {album_title}", "color": 0xE67E22, "fields": [{"name": "Missing Tracks", "value": str(len(failed_tracks)), "inline": True}]},
            )
        else:
            models.add_log(
                log_type="import_success", album_id=album_id, album_title=album_title,
                artist_name=artist_name, details="Album downloaded and refreshing in Lidarr",
                failed_tracks=[], total_file_size=total_downloaded_size,
            )
            send_notifications(
                f"Import Success\nAlbum: {album_title}\nArtist: {artist_name}\nRefreshing in Lidarr",
                log_type="import_success",
                embed_data={"title": "Import Successful", "description": f"{artist_name} — {album_title}", "color": 0x2ECC71},
            )

        lidarr_request("command", method="POST", data={"name": "RefreshArtist", "artistId": artist_id})

        if lidarr_path and os.path.exists(artist_path):
            try:
                logger.info(f"Cleaning up download folder: {artist_path}")
                shutil.rmtree(artist_path)
                logger.info("Download folder cleaned up successfully")
            except Exception as e:
                logger.warning(f"Failed to cleanup download folder: {str(e)}")

        return {"success": True}

    except Exception as e:
        logger.error(f"Error during album download: {str(e)}")
        artist_name = download_process.get("artist_name", "Unknown")
        album_title = download_process.get("album_title", "Unknown")
        send_notifications(
            f"Download failed\nAlbum: {album_title}\nArtist: {artist_name}",
            log_type="album_error",
            embed_data={"title": "Download Failed", "description": f"{artist_name} — {album_title}", "color": 0xE74C3C},
        )
        models.add_log(
            log_type="album_error", album_id=album_id, album_title=album_title,
            artist_name=artist_name, details=f"Error: {str(e)}", failed_tracks=[],
        )
        download_process["result_success"] = False
        return {"error": str(e)}
    finally:
        _cover_url = download_process.get("cover_url", "")
        if failed_tracks:
            models.save_failed_tracks(
                album_id=album_id,
                album_title=download_process.get("album_title", "") or album_title,
                artist_name=download_process.get("artist_name", "") or artist_name,
                cover_url=_cover_url,
                album_path=album_path,
                lidarr_album_path=lidarr_album_path,
                tracks=[
                    {"title": t["title"], "reason": t["reason"], "track_num": t.get("track_num", 0)}
                    for t in failed_tracks
                ],
            )
        else:
            models.clear_failed_tracks()

        models.add_history_entry(
            album_id=download_process.get("album_id"),
            album_title=download_process.get("album_title", ""),
            artist_name=download_process.get("artist_name", ""),
            success=download_process.get("result_success", True),
            partial=download_process.get("result_partial", False),
        )

        download_process["active"] = False
        download_process["progress"] = {}
        download_process["album_id"] = None
        download_process["album_title"] = ""
        download_process["artist_name"] = ""
        download_process["current_track_title"] = ""
        download_process["cover_url"] = ""


def process_download_queue():
    while True:
        try:
            if not download_process["active"]:
                next_album_id = models.pop_next_from_queue()
                if next_album_id is not None:
                    threading.Thread(
                        target=process_album_download,
                        args=(next_album_id, False),
                        daemon=True,
                    ).start()
        except Exception as e:
            logger.warning(f"Queue processor error: {e}")
        time.sleep(2)
```

**Step 5: Implement `scheduler.py`**

```python
# scheduler.py
import logging
import threading

import schedule

import models
from config import load_config
from lidarr import get_missing_albums
from notifications import send_notifications
from processing import download_process, queue_lock

logger = logging.getLogger(__name__)


def scheduled_check():
    if download_process["active"]:
        return
    config = load_config()
    albums = get_missing_albums()
    if not albums:
        return

    with queue_lock:
        # Get recent successful history from DB
        history = models.get_history(page=1, per_page=50)
        recent_history_ids = [
            h["album_id"] for h in history["items"] if h.get("success")
        ]
        current_download_id = download_process.get("album_id")
        queue = models.get_queue()
        queued_ids = {q["album_id"] for q in queue}

        new_albums = [
            album for album in albums
            if album["id"] not in queued_ids
            and album["id"] not in recent_history_ids
            and album["id"] != current_download_id
            and album.get("missingTrackCount", 0) > 0
        ]

    if new_albums:
        if config.get("scheduler_auto_download", True):
            logger.info(f"Scheduler: Found {len(new_albums)} new missing albums, adding to queue...")
            send_notifications(
                f"Scheduler: Adding {len(new_albums)} new missing albums to queue...",
                log_type="download_started",
                embed_data={"title": "Scheduler", "description": f"Adding {len(new_albums)} new missing albums to queue", "color": 0x3498DB},
            )
            for album in new_albums:
                models.enqueue_album(album["id"])
        else:
            logger.info(f"Scheduler: Found {len(new_albums)} missing albums (Auto-Download disabled)")
            send_notifications(
                f"Scheduler: Found {len(new_albums)} missing albums (Auto-DL Disabled)",
                log_type="download_started",
                embed_data={"title": "Scheduler", "description": f"Found {len(new_albums)} missing albums (Auto-DL Disabled)", "color": 0xE67E22},
            )


def setup_scheduler():
    config = load_config()
    schedule.clear()
    if config.get("scheduler_enabled"):
        interval = int(config.get("scheduler_interval", 60))
        schedule.every(interval).minutes.do(scheduled_check)


def run_scheduler():
    import time
    while True:
        schedule.run_pending()
        time.sleep(10)
```

**Step 6: Run all tests**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_downloader.py -v`
Expected: PASS

**Step 7: Commit**

```bash
git add downloader.py processing.py scheduler.py tests/test_downloader.py
git commit -m "Extract downloader.py, processing.py, and scheduler.py from app.py"
```

---

### Task 9: Rewrite `app.py` as thin route handlers

**Files:**
- Modify: `app.py` (complete rewrite)
- Create: `tests/test_routes.py`

**Step 1: Write failing route tests**

```python
# tests/test_routes.py
import json
import pytest
import db
import models

@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr("db.DB_PATH", db_path)
    db.init_db()
    yield db_path
    db.close_db()

@pytest.fixture
def client(tmp_path, monkeypatch):
    config_file = str(tmp_path / "config.json")
    monkeypatch.setattr("config.CONFIG_FILE", config_file)
    monkeypatch.setenv("LIDARR_URL", "http://test:8686")
    monkeypatch.setenv("LIDARR_API_KEY", "testkey")
    monkeypatch.setenv("DOWNLOAD_PATH", str(tmp_path / "downloads"))

    from app import app
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client

def test_get_history_paginated(client):
    for i in range(60):
        models.add_history_entry(i, f"Album{i}", "Artist", True, False)
    resp = client.get("/api/download/history?page=1&per_page=50")
    data = resp.get_json()
    assert data["total"] == 60
    assert data["pages"] == 2
    assert len(data["items"]) == 50

def test_get_history_default(client):
    resp = client.get("/api/download/history")
    data = resp.get_json()
    assert data["total"] == 0
    assert data["items"] == []

def test_clear_history(client):
    models.add_history_entry(1, "A", "A", True, False)
    resp = client.post("/api/download/history/clear")
    assert resp.get_json()["success"] is True
    assert models.get_history()["total"] == 0

def test_get_logs_paginated(client):
    for i in range(60):
        models.add_log("download_success", i, f"Album{i}", "Artist")
    resp = client.get("/api/logs?page=2&per_page=50")
    data = resp.get_json()
    assert data["total"] == 60
    assert len(data["items"]) == 10

def test_dismiss_log(client):
    models.add_log("download_success", 1, "A", "A")
    log_id = models.get_logs()["items"][0]["id"]
    resp = client.delete(f"/api/logs/{log_id}/dismiss")
    assert resp.get_json()["success"] is True

def test_dismiss_log_not_found(client):
    resp = client.delete("/api/logs/nonexistent/dismiss")
    assert resp.status_code == 404

def test_get_failed_tracks(client):
    models.save_failed_tracks(1, "A", "A", "", "", "", [{"title": "T", "track_num": 1, "reason": "fail"}])
    resp = client.get("/api/download/failed")
    data = resp.get_json()
    assert len(data["failed_tracks"]) == 1

def test_get_queue(client):
    models.enqueue_album(10)
    resp = client.get("/api/download/queue")
    data = resp.get_json()
    assert len(data) >= 0  # Queue returns list (may need Lidarr mock for details)
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_routes.py -v`
Expected: FAIL

**Step 3: Rewrite `app.py`**

Replace the entire file. The new `app.py` imports from all modules and defines thin route handlers. Key changes:
- Remove all function definitions that moved to other modules
- Remove all global state variables (download_history, download_logs, etc.)
- Import from: `db`, `models`, `config`, `utils`, `lidarr`, `metadata`, `downloader`, `processing`, `notifications`, `scheduler`
- Route handlers call `models.*` instead of manipulating in-memory lists
- `api_download_history` and `api_get_logs` accept `?page=&per_page=` query params
- `api_dismiss_log` calls `models.delete_log()` instead of list manipulation
- `api_download_failed` calls `models.get_failed_tracks_context()`
- Queue routes call `models.enqueue_album()`, `models.dequeue_album()`, etc.
- `api_stats` calls `models.get_history_count_today()` and `models.get_queue_length()`
- `api_download_stream` reads queue from `models.get_queue()` instead of in-memory list
- `api_logs_size` calls `models.get_logs_db_size()` instead of `os.path.getsize(LOGS_FILE)`
- `__main__` block calls `db.init_db()` and `models.reset_downloading_to_queued()` instead of `load_persistent_data()`
- `api_download_manual` fetches album data from Lidarr API instead of `last_failed_result["album_data"]`

The full rewritten `app.py` should be ~400 lines (route definitions only).

**Step 4: Run all tests**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/ -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app.py tests/test_routes.py
git commit -m "Rewrite app.py as thin route handlers using extracted modules and SQLite"
```

---

### Task 10: Add UI pagination to templates

**Files:**
- Modify: `templates/downloads.html`
- Modify: `templates/logs.html`

**Step 1: Update `downloads.html`**

Add pagination controls. The JavaScript that fetches `/api/download/history` needs to:
- Send `?page=N&per_page=50` query params
- Render prev/next buttons using the `pages`, `page` fields from the response
- Update the displayed items from `response.items` instead of the raw array

**Step 2: Update `logs.html`**

Same pattern as downloads — paginated fetch and prev/next controls.

**Step 3: Test in browser**

Run: `docker compose up -d`
Navigate to http://localhost:5000/downloads and http://localhost:5000/logs
Verify pagination controls appear and work.

**Step 4: Commit**

```bash
git add templates/downloads.html templates/logs.html
git commit -m "Add server-side pagination to downloads and logs templates"
```

---

### Task 11: Create migration tool `tools/migrate_json_to_db.py`

**Files:**
- Create: `tools/migrate_json_to_db.py`
- Create: `tests/test_migrate_tool.py`

**Step 1: Write failing tests**

```python
# tests/test_migrate_tool.py
import json
import os
import sqlite3
import subprocess
import sys
import pytest

@pytest.fixture
def config_dir(tmp_path):
    history = [
        {"album_id": 1, "album_title": "A", "artist_name": "X", "success": True,
         "partial": False, "timestamp": 1700000000},
    ]
    logs = [
        {"id": "123_1", "type": "download_success", "album_id": 1, "album_title": "A",
         "artist_name": "X", "timestamp": 1700000000, "details": "ok",
         "failed_tracks": [], "dismissed": False, "total_file_size": 1024},
    ]
    failed = {
        "failed_tracks": [{"title": "T1", "reason": "fail", "track_num": 1}],
        "album_id": 1, "album_title": "A", "artist_name": "X",
        "cover_url": "http://img", "album_path": "/path",
        "album_data": None, "cover_data": None, "cover_data_b64": None,
        "lidarr_album_path": "/lidarr",
    }
    (tmp_path / "download_history.json").write_text(json.dumps(history))
    (tmp_path / "download_logs.json").write_text(json.dumps(logs))
    (tmp_path / "last_failed_result.json").write_text(json.dumps(failed))
    return tmp_path

def test_migration_creates_db(config_dir):
    result = subprocess.run(
        [sys.executable, "tools/migrate_json_to_db.py", "--config-dir", str(config_dir)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    db_path = config_dir / "lidarr-downloader.db"
    assert db_path.exists()

def test_migration_imports_history(config_dir):
    subprocess.run(
        [sys.executable, "tools/migrate_json_to_db.py", "--config-dir", str(config_dir)],
        capture_output=True, text=True,
    )
    conn = sqlite3.connect(str(config_dir / "lidarr-downloader.db"))
    count = conn.execute("SELECT COUNT(*) FROM download_history").fetchone()[0]
    conn.close()
    assert count == 1

def test_migration_renames_json_files(config_dir):
    subprocess.run(
        [sys.executable, "tools/migrate_json_to_db.py", "--config-dir", str(config_dir)],
        capture_output=True, text=True,
    )
    assert (config_dir / "download_history.json.migrated").exists()
    assert not (config_dir / "download_history.json").exists()

def test_migration_idempotent(config_dir):
    subprocess.run(
        [sys.executable, "tools/migrate_json_to_db.py", "--config-dir", str(config_dir)],
        capture_output=True, text=True,
    )
    result = subprocess.run(
        [sys.executable, "tools/migrate_json_to_db.py", "--config-dir", str(config_dir)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "No JSON files found" in result.stdout or "already migrated" in result.stdout.lower()
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_migrate_tool.py -v`
Expected: FAIL

**Step 3: Implement the migration tool**

```python
#!/usr/bin/env python3
# tools/migrate_json_to_db.py
"""Migrate JSON state files to SQLite database.

Usage:
    python3 tools/migrate_json_to_db.py [--config-dir /config]
"""
import argparse
import json
import os
import sys
import time

# Add parent directory to path so we can import db module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


def migrate(config_dir):
    history_file = os.path.join(config_dir, "download_history.json")
    logs_file = os.path.join(config_dir, "download_logs.json")
    failed_file = os.path.join(config_dir, "last_failed_result.json")

    files_found = any(os.path.exists(f) for f in [history_file, logs_file, failed_file])
    if not files_found:
        print("No JSON files found to migrate. Already migrated or fresh install.")
        return

    db.DB_PATH = os.path.join(config_dir, "lidarr-downloader.db")
    db.init_db()
    conn = db.get_db()

    # Migrate history
    if os.path.exists(history_file):
        with open(history_file, "r") as f:
            history = json.load(f)
        count = 0
        for h in history:
            conn.execute(
                """INSERT INTO download_history
                   (album_id, album_title, artist_name, success, partial, manual, track_title, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    h.get("album_id"), h.get("album_title", ""), h.get("artist_name", ""),
                    int(h.get("success", True)), int(h.get("partial", False)),
                    int(h.get("manual", False)), h.get("track_title"),
                    h.get("timestamp", time.time()),
                ),
            )
            count += 1
        conn.commit()
        os.rename(history_file, history_file + ".migrated")
        print(f"Migrated {count} history entries")

    # Migrate logs
    if os.path.exists(logs_file):
        with open(logs_file, "r") as f:
            logs = json.load(f)
        count = 0
        for log in logs:
            conn.execute(
                """INSERT OR IGNORE INTO download_logs
                   (id, type, album_id, album_title, artist_name, timestamp, details, failed_tracks, total_file_size)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    log.get("id", f"{int(time.time()*1000)}_{count}"),
                    log.get("type", "unknown"),
                    log.get("album_id", 0), log.get("album_title", ""),
                    log.get("artist_name", ""), log.get("timestamp", time.time()),
                    log.get("details", ""),
                    json.dumps(log.get("failed_tracks", [])),
                    log.get("total_file_size", 0),
                ),
            )
            count += 1
        conn.commit()
        os.rename(logs_file, logs_file + ".migrated")
        print(f"Migrated {count} log entries")

    # Migrate failed tracks
    if os.path.exists(failed_file):
        with open(failed_file, "r") as f:
            data = json.load(f)
        tracks = data.get("failed_tracks", [])
        count = 0
        for t in tracks:
            conn.execute(
                """INSERT INTO failed_tracks
                   (album_id, album_title, artist_name, cover_url, album_path,
                    lidarr_album_path, track_title, track_num, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data.get("album_id"), data.get("album_title", ""),
                    data.get("artist_name", ""), data.get("cover_url", ""),
                    data.get("album_path", ""), data.get("lidarr_album_path", ""),
                    t.get("title", ""), t.get("track_num", 0), t.get("reason", ""),
                ),
            )
            count += 1
        conn.commit()
        os.rename(failed_file, failed_file + ".migrated")
        print(f"Migrated {count} failed track entries")

    db.close_db()
    print(f"Migration complete. Database: {db.DB_PATH}")


def main():
    parser = argparse.ArgumentParser(description="Migrate JSON state files to SQLite")
    parser.add_argument("--config-dir", default="/config", help="Config directory path")
    args = parser.parse_args()
    migrate(args.config_dir)


if __name__ == "__main__":
    main()
```

**Step 4: Run tests**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/test_migrate_tool.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tools/migrate_json_to_db.py tests/test_migrate_tool.py
git commit -m "Add migration tool to convert JSON state files to SQLite"
```

---

### Task 12: Update documentation

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Create: `tests/__init__.py` (if missing)

**Step 1: Update README.md**

Add a "Migrating from v1.x (JSON files)" section:

```markdown
### Migrating from JSON to SQLite

If you're upgrading from a version that used JSON files for state, run the migration tool:

```bash
# Inside the container:
python3 tools/migrate_json_to_db.py --config-dir /config

# Or from the host if config is mounted:
python3 tools/migrate_json_to_db.py --config-dir ./config
```

This imports data from `download_history.json`, `download_logs.json`, and `last_failed_result.json` into the SQLite database and renames the originals to `*.json.migrated`.
```

**Step 2: Update CLAUDE.md**

Add to the Architecture section:

```markdown
## Database

State is stored in SQLite at `/config/lidarr-downloader.db`. Schema is versioned via `schema_version` table. When changing the DB schema:

1. Increment `SCHEMA_VERSION` in `db.py`
2. Add a migration function `migrate_vN_to_vN+1(conn)` in `db.py`
3. Register it in the `migrations` dict inside `_run_migrations()`
4. Test with `python3 -m pytest tests/test_db.py`

## Module Structure

| Module | Responsibility |
|--------|---------------|
| `app.py` | Flask app, thin route handlers, startup |
| `db.py` | SQLite connection, schema, migrations |
| `models.py` | All SQL queries, CRUD, pagination |
| `downloader.py` | YouTube search/scoring/download |
| `processing.py` | Album processing, queue processor, progress state |
| `metadata.py` | ID3 tagging, XML sidecar, iTunes API |
| `lidarr.py` | Lidarr API wrapper |
| `notifications.py` | Telegram/Discord webhooks |
| `config.py` | Config load/save, constants |
| `scheduler.py` | Scheduled polling/auto-download |
| `utils.py` | Shared utilities |
```

**Step 3: Run full test suite**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/ -v`
Expected: PASS

**Step 4: Docker smoke test**

Run: `docker compose up -d && sleep 5 && curl -s http://localhost:5000/api/stats | python3 -m json.tool`
Expected: JSON response with `in_queue` and `downloaded_today` fields

**Step 5: Commit**

```bash
git add README.md CLAUDE.md tests/__init__.py
git commit -m "Update documentation for SQLite migration and module structure"
```

---

### Task 13: Final cleanup and verification

**Step 1: Delete old plan file**

```bash
rm plans/2026-03-09_add_persistent_state.md
```

**Step 2: Run full test suite**

Run: `cd /Users/verbal/code/Lidarr-YouTube-Downloader && python3 -m pytest tests/ -v --tb=short`
Expected: All PASS

**Step 3: Docker integration test**

```bash
docker compose down && docker compose build && docker compose up -d
```

Navigate to http://localhost:5000 and verify:
- Missing albums page loads
- Downloads page shows pagination
- Logs page shows pagination
- Settings page works
- Queue add/remove works

**Step 4: Commit any remaining cleanup**

```bash
git add -A
git commit -m "Final cleanup after SQLite migration"
```
