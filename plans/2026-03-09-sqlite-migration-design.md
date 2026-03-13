# Design: Migrate State to SQLite3

**Date:** 2026-03-09
**Status:** Approved

## Goal

Replace JSON file-based persistence (`download_history.json`, `download_logs.json`, `last_failed_result.json`) and in-memory state (`download_queue`, `download_process`) with a SQLite3 database. Split monolithic `app.py` into focused modules.

## Database

**Location:** `/config/lidarr-downloader.db`

### Schema (v1)

```sql
CREATE TABLE schema_version (
    version INTEGER NOT NULL,
    applied_at REAL NOT NULL
);

CREATE TABLE download_history (
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

CREATE TABLE download_logs (
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

CREATE TABLE failed_tracks (
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

CREATE TABLE download_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL UNIQUE,
    position INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'downloading'))
);

CREATE INDEX idx_history_timestamp ON download_history(timestamp);
CREATE INDEX idx_history_album_id ON download_history(album_id);
CREATE INDEX idx_logs_timestamp ON download_logs(timestamp);
CREATE INDEX idx_queue_position ON download_queue(position);
```

### Key Decisions

- **`failed_tracks`**: Only stores IDs/metadata. Album data and cover art re-fetched from Lidarr/iTunes on retry (no BLOBs).
- **`download_logs.failed_tracks`**: JSON column — display-only data, always read as a whole list.
- **`download_queue.status`**: Enforced via CHECK constraint. Python constants in `models.py`.
- **`download_history`**: Unbounded (SQLite handles thousands of rows trivially).
- **`download_process`**: In-memory only — transient progress state (percent, speed, etc.) not persisted.
- **Schema versioning**: From day one. Sequential migration functions in `db.py`.

## Module Structure

Split `app.py` (~2000+ lines) into focused modules:

| Module | Responsibility |
|--------|---------------|
| `app.py` | Flask app factory, thin route handlers, startup |
| `db.py` | SQLite connection, schema creation, migrations |
| `models.py` | All SQL queries, CRUD functions, pagination |
| `downloader.py` | `download_track_youtube()`, YouTube search/scoring |
| `processing.py` | `process_album_download()`, manual download, queue processor |
| `metadata.py` | `tag_mp3()`, XML sidecar, iTunes API calls |
| `lidarr.py` | `lidarr_request()`, `get_missing_albums()`, `get_valid_release_id()` |
| `notifications.py` | Telegram/Discord webhooks |
| `config.py` | `load_config()`, `save_config()`, constants |
| `scheduler.py` | Scheduled polling/auto-download |
| `utils.py` | `sanitize_filename()`, `format_bytes()`, `set_permissions()`, etc. |

### Principles

- `app.py` routes are thin: parse request -> call model/processing function -> return response
- `db.py` owns the connection, exposes `get_db()` — all other modules import from it
- `models.py` is the only module that writes SQL
- `download_process` (transient progress) lives as a module-level dict in `processing.py`
- Threading locks move to the modules that own the state they protect

## Data Access Layer (`models.py`)

```python
# History
def add_history_entry(album_id, album_title, artist_name, success, partial, manual=False, track_title=None): ...
def get_history(page=1, per_page=50): ...
def clear_history(): ...

# Logs
def add_log(log_type, album_id, album_title, artist_name, details="", failed_tracks=None, total_file_size=0): ...
def get_logs(page=1, per_page=50): ...
def delete_log(log_id): ...
def clear_logs(): ...

# Failed tracks
def save_failed_tracks(album_id, album_title, artist_name, cover_url, album_path, lidarr_album_path, tracks): ...
def get_failed_tracks(): ...
def clear_failed_tracks(): ...

# Queue
def enqueue_album(album_id): ...
def dequeue_album(album_id): ...
def get_queue(): ...
def set_queue_status(album_id, status): ...
def reset_downloading_to_queued(): ...
def clear_queue(): ...
```

**Pagination response shape** (consistent across all paginated endpoints):
```json
{"items": [...], "total": 150, "page": 2, "pages": 3, "per_page": 50}
```

## Migration

**`tools/migrate_json_to_db.py`** — standalone script, run manually:
- No Flask dependency
- Takes optional `--config-dir` arg (defaults to `/config`)
- Looks for `download_history.json`, `download_logs.json`, `last_failed_result.json`
- Creates/opens `lidarr-downloader.db`, runs schema creation if needed
- Imports data, renames files to `*.json.migrated`
- Reports row counts migrated

**Not migrated:** `download_queue` (transient mid-flight state, not worth migrating)

## UI Pagination

- Server-side pagination via query params: `?page=1&per_page=50`
- Default 50 items per page
- Prev/next navigation bar in `downloads.html` (history) and `logs.html`
- API endpoints support same pagination params
- No pagination needed for queue or failed tracks (always small)

## Startup Sequence

1. `db.init_db()` — create DB if missing, or validate/upgrade schema
2. `models.reset_downloading_to_queued()` — recover interrupted queue entries
3. Load config, start scheduler
