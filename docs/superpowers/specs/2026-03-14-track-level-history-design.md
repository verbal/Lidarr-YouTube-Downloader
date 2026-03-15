# Track-Level Download History

Tracks individual song downloads instead of just album-level records. Captures YouTube source metadata per track and maintains a full audit trail across re-downloads.

## Problem

Everything is tracked at the album level. YouTube URLs used for downloads are discarded after use. If a song is deleted and re-downloaded, there's no record of which YouTube video was previously used. No per-track metadata is visible in the UI.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Data model | Replace `download_history` + `failed_tracks` with `track_downloads` | Single source of truth, album views derived via GROUP BY |
| Per-track metadata | YouTube URL, video title, match score, duration, timestamp | Core metadata without bloat (no channel, file size, bitrate) |
| Logs | Keep album-level events, add track-level entries, drop `failed_tracks` JSON column | Full granularity in Logs page |
| UI layout | Album rows with expandable track detail | Preserves current UX, one click to drill in |
| `failed_tracks` table | Drop entirely | Redundant once `track_downloads` exists |
| Multiple attempts | Keep all rows per track | Full audit trail of YouTube URLs used |
| Migration | Drop all old data (history, logs, failed tracks), clean slate | Old data has no track-level info to preserve; logs reference dropped columns |
| `manual` flag | Drop | Not used or needed |

## Database Schema

### New table: `track_downloads`

```sql
CREATE TABLE track_downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL,
    album_title TEXT NOT NULL,
    artist_name TEXT NOT NULL,
    track_title TEXT NOT NULL,
    track_number INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 0,
    error_message TEXT DEFAULT '',
    youtube_url TEXT DEFAULT '',
    youtube_title TEXT DEFAULT '',
    match_score REAL DEFAULT 0.0,
    duration_seconds INTEGER DEFAULT 0,
    album_path TEXT DEFAULT '',
    lidarr_album_path TEXT DEFAULT '',
    cover_url TEXT DEFAULT '',
    timestamp REAL NOT NULL
);

CREATE INDEX idx_track_dl_album_id ON track_downloads(album_id);
CREATE INDEX idx_track_dl_album_id_success ON track_downloads(album_id, success);
CREATE INDEX idx_track_dl_timestamp ON track_downloads(timestamp);
CREATE INDEX idx_track_dl_youtube_url ON track_downloads(youtube_url);
```

Notes:
- `album_path` stores the download directory path.
- `lidarr_album_path` stores the Lidarr import directory path, needed by the manual retry flow to place re-downloaded files where Lidarr watches for imports.
- `cover_url` stores the album cover URL for UI display and retry context.
- All text columns default to `''` (empty string), never NULL. Queries use `= ''` checks, never `IS NULL`.
- The composite index on `(album_id, success)` optimizes the `get_album_history()` grouped query.

### Modified table: `download_logs`

Drop the `failed_tracks` column. New log types added: `track_success`, `track_error`.

Since `download_logs` data references the dropped `failed_tracks` column and old table structure, all existing log data is dropped during migration and the table is recreated fresh. This avoids SQLite version compatibility issues with `ALTER TABLE DROP COLUMN` (requires SQLite 3.35.0+).

Updated functions:
- `add_log()` — remove `failed_tracks` parameter
- `get_logs()` — remove `json.loads(item["failed_tracks"])` post-processing
- `get_logs_db_size()` — remove `LENGTH(failed_tracks)` from the sum, use only `LENGTH(details)`

### Dropped tables

- `download_history` — replaced by `track_downloads`
- `failed_tracks` — replaced by `track_downloads`

### Migration: V1 to V2

The entire migration runs inside an explicit transaction (`BEGIN`/`COMMIT`). If any step fails, the transaction rolls back and V1 schema stays intact.

1. `BEGIN` transaction
2. Drop `download_history` table
3. Drop `failed_tracks` table
4. Drop `download_logs` table (and its data — avoids SQLite column-drop compatibility issues)
5. Create `track_downloads` table with all indexes
6. Recreate `download_logs` table (without `failed_tracks` column)
7. Recreate `download_logs` timestamp index
8. Insert new `schema_version` row
9. `COMMIT`

### Log ID collision prevention

The current log ID format `{int(time.time() * 1000)}_{album_id}` can collide when multiple track-level logs are created for the same album within the same millisecond. Updated format: `{int(time.time() * 1000)}_{album_id}_{track_number}` for track-level entries, unchanged for album-level entries.

## Data Flow Changes

### `downloader.py`

`download_track_youtube()` returns a metadata dict instead of `True`/string:

```python
# Success
{
    "success": True,
    "youtube_url": "https://www.youtube.com/watch?v=abc123",
    "youtube_title": "Artist - Track (Official Audio)",
    "match_score": 0.87,
    "duration_seconds": 234,
}

# Failure
{
    "success": False,
    "error_message": "No suitable YouTube match found",
}
```

### `processing.py`

- `_download_tracks()`: after each track download (success or failure), call `models.add_track_download()` with the metadata. Pass `album_path` and `cover_url` from the parent `process_album_download()` context.
- `process_album_download()` `finally` block: remove `save_failed_tracks()` and `add_history_entry()` calls — per-track recording happens inline in `_download_tracks()`
- All `add_log()` calls in `_handle_post_download()`, `_log_import_result()`, and `app.py`'s manual download route: remove `failed_tracks=` parameter. Failed track info is now in `track_downloads` and can be looked up by `album_id`.

### `models.py`

New functions:
- `add_track_download(album_id, album_title, artist_name, track_title, track_number, success, error_message, youtube_url, youtube_title, match_score, duration_seconds, album_path, lidarr_album_path, cover_url)`
- `get_track_downloads_for_album(album_id)` — all track records for an album, newest first
- `get_album_history(page, per_page)` — grouped query returning album-level summaries (see query below)
- `get_failed_tracks_for_retry(album_id)` — replaces `get_failed_tracks_context()`, returns failed tracks + album context from `track_downloads`
- Updated: `get_history_album_ids_since(timestamp)` — query `track_downloads`, returns distinct album IDs with at least one successful track since timestamp
- Updated: `get_history_count_today()` — `SELECT COUNT(DISTINCT album_id) FROM track_downloads WHERE success = 1 AND timestamp >= ?` (semantic change: now counts distinct albums, not total rows)
- `clear_history()` — delete all `track_downloads` rows

Removed functions:
- `add_history_entry`
- `get_history`
- `save_failed_tracks`
- `get_failed_tracks`
- `get_failed_tracks_context`
- `remove_failed_track`
- `clear_failed_tracks`

### Album history grouped query

```sql
SELECT
    album_id,
    album_title,
    artist_name,
    cover_url,
    MAX(timestamp) as latest_timestamp,
    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count,
    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as fail_count,
    COUNT(*) as total_count
FROM track_downloads
GROUP BY album_id, album_title, artist_name
ORDER BY latest_timestamp DESC
```

Album status derivation:
- `fail_count == 0` → success (green)
- `success_count == 0` → error (red)
- `success_count > 0 AND fail_count > 0` → partial (amber)

### Manual download flow (`/api/download/manual`)

The manual download route currently uses `get_failed_tracks_context()` to get album context (path, cover URL) and `remove_failed_track()` after success. Updated flow:

1. `get_failed_tracks_for_retry(album_id)` returns failed tracks + context from `track_downloads` (album_path, lidarr_album_path, cover_url are stored per-row)
2. After successful manual re-download, insert a new `track_downloads` row with `success=1` and the new YouTube metadata. The old failed row stays (audit trail).
3. The retry UI queries for the latest attempt per track — if the latest is successful, it's no longer shown as "failed"

No `remove_failed_track()` equivalent needed — success is determined by the most recent attempt for each track.

## API Changes

### Modified endpoints

| Endpoint | Change |
|----------|--------|
| `GET /api/download/history` | Returns album-grouped summaries (total/success/fail counts, latest timestamp, cover_url) |
| `GET /api/download/failed` | No parameters — infers album from the most recent download batch (highest timestamp group). Queries latest `track_downloads` per track for that album, returns those where latest attempt has `success = 0`. Response includes `album_id`, `album_title`, `artist_name`, `cover_url`, `album_path`, `lidarr_album_path`, and `failed_tracks` list |
| `GET /api/stats` | Same shape, backed by `track_downloads`. `downloaded_today` counts distinct albums with successful tracks |
| `GET /api/logs` | Responses no longer include `failed_tracks` JSON field; track-level log entries appear as rows |
| `POST /api/download/manual` | Uses `get_failed_tracks_for_retry()` instead of `get_failed_tracks_context()`. Inserts new `track_downloads` row on success instead of calling `remove_failed_track()` + `add_history_entry()` |

### New endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /api/download/history/<album_id>/tracks` | All track download records for an album (for expandable UI). Returns list of track dicts with all `track_downloads` fields. |

## UI Changes

### Downloads page (`downloads.html`)

History section changes from flat album rows to expandable album rows:

**Collapsed (default):** Album row with title, artist, color-coded track count badge (green = all success, amber = partial, red = all failed), timestamp. Click arrow to expand.

**Expanded:** Grid of individual tracks showing:
- Track number
- Track title (with "(N attempts)" indicator if downloaded more than once)
- YouTube source link (clickable, opens video, title on hover)
- Match score
- Duration (formatted as M:SS)
- Download timestamp

Failed tracks show red-tinted background with error message in place of YouTube link.

All styling uses existing CSS variables (`--bg`, `--surface`, `--primary`, `--danger`, `--text`, `--text-dim`, `--border`, etc.) and follows the existing glassmorphism patterns. Status colors: success = `--primary` (#10b981), partial = `#f59e0b`, error = `--danger` (#ef4444). History items use `border-left: 4px solid` color coding matching existing `.history-item` pattern.

The `manual` flag UI treatment (purple color, hand icon) is removed since the flag is dropped.

### Logs page

Track-level log entries (`track_success`, `track_error`) appear as regular log rows. No structural changes to the template. The `failed_tracks` JSON is no longer rendered since the column is dropped.

## Error Handling

- If `download_track_youtube` succeeds but metadata extraction fails (missing URL from yt-dlp response): record with empty YouTube fields, `success=True`
- Failed downloads: `success=0`, `error_message` populated, YouTube fields empty (empty string, not NULL)
- Migration failure: entire transaction rolls back, V1 schema stays intact, startup error logged

## Affected Utility Tools

`tools/migrate_json_to_db.py` references the old `failed_tracks` table. This is a one-time migration tool from JSON state files and is no longer needed. It can be left as-is (it will error if run against V2 schema, which is acceptable since JSON migration is a historical one-time operation).

## Testing

| Test file | Coverage |
|-----------|----------|
| `test_db.py` | V1→V2 migration: tables dropped/created, transaction rollback on failure |
| `test_models.py` | `add_track_download`, `get_track_downloads_for_album`, `get_album_history`, `get_failed_tracks_for_retry`, `get_history_album_ids_since`, `get_history_count_today`, `clear_history`, `add_log` (without failed_tracks param), `get_logs` (without failed_tracks field), `get_logs_db_size` (updated sum) |
| `test_downloader.py` | `download_track_youtube` returns metadata dict |
| `test_processing.py` | `_download_tracks` calls `add_track_download` per track with correct metadata |
| `test_routes.py` | New tracks endpoint, updated history/failed/logs response shapes, manual download flow |
