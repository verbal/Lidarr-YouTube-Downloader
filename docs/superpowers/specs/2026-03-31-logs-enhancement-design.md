# Logs Page Enhancement: Per-Track Failure Detail with Candidate Attempts

## Problem

The Logs page currently shows album-level log entries with generic failure messages like "3 track(s) failed to download out of 10". When AcoustID verification fails after trying all 10 YouTube candidates, users have no visibility into:

- Which candidates were tried for each failed track
- Why each candidate was rejected (AcoustID mismatch, no AcoustID data, download error)
- What AcoustID actually matched vs what was expected
- The verification scores

This data is currently only written to the Python application logger and lost after the download completes. The per-candidate verification details are never persisted.

Additionally, each log entry takes excessive vertical space due to the card layout with large padding, separate details sections, and multi-line structure.

## Design

### Data Layer: `candidate_attempts` Table

New table storing one row per candidate attempt per track download. Created via V5 migration.

```sql
CREATE TABLE candidate_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_download_id INTEGER NOT NULL,
    youtube_url TEXT NOT NULL DEFAULT '',
    youtube_title TEXT NOT NULL DEFAULT '',
    match_score REAL DEFAULT 0.0,
    duration_seconds INTEGER DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT '',
    acoustid_matched_id TEXT DEFAULT '',
    acoustid_matched_title TEXT DEFAULT '',
    acoustid_score REAL DEFAULT 0.0,
    expected_recording_id TEXT DEFAULT '',
    error_message TEXT DEFAULT '',
    timestamp REAL NOT NULL,
    FOREIGN KEY (track_download_id)
        REFERENCES track_downloads(id) ON DELETE CASCADE
);
CREATE INDEX idx_ca_track_dl_id ON candidate_attempts(track_download_id);
```

**`outcome` column** stores the `.value` of a Python `str` enum defined in `models.py`:

```python
class CandidateOutcome(str, Enum):
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    UNVERIFIED = "unverified"
    DOWNLOAD_FAILED = "download_failed"
    ACCEPTED_NO_VERIFY = "accepted_no_verify"
    ACCEPTED_UNVERIFIED_FALLBACK = "accepted_unverified_fallback"
```

- `VERIFIED` — AcoustID matched the expected recording (track accepted)
- `MISMATCH` — AcoustID matched a different recording (candidate rejected, URL banned)
- `UNVERIFIED` — AcoustID returned no fingerprint data
- `DOWNLOAD_FAILED` — yt-dlp could not download the candidate
- `ACCEPTED_NO_VERIFY` — AcoustID disabled or no expected recording ID; candidate accepted without verification
- `ACCEPTED_UNVERIFIED_FALLBACK` — No verified candidate found; best unverified candidate accepted as fallback

All code references use the enum members (e.g., `CandidateOutcome.MISMATCH`), never raw strings. The DB stores the string value for readability.

### Processing Layer: Capture Candidate Outcomes

In `processing.py`, the `_process_single_track` function's candidate loop currently logs mismatch details to the Python logger and moves on. Changes:

1. After each candidate attempt (download success/fail, verification result), append an attempt dict to a thread-local list. After the track completes (accepted or failed), flush the buffered attempts to the DB.

2. The data to capture per candidate:
   - `youtube_url`, `youtube_title` — from the candidate dict
   - `match_score` — from `candidate["score"]`
   - `duration_seconds` — from `candidate["duration"]`
   - `outcome` — determined by the verification result or download failure
   - `acoustid_matched_id`, `acoustid_matched_title`, `acoustid_score` — from `vresult["fp_data"]` when available
   - `expected_recording_id` — the MusicBrainz recording ID from the track
   - `error_message` — from `dl_result["error_message"]` on download failure
   - `timestamp` — `time.time()` at the moment the outcome is determined

3. **Capture points in the candidate loop** (each appends to the thread-local buffer):
   - Download failure (`dl_out` is None or `success=False`): outcome `"download_failed"`, error_message from dl_result
   - AcoustID verified (`vresult["status"] == "verified"`): outcome `"verified"`
   - AcoustID mismatch (`vresult["status"] == "mismatch"`): outcome `"mismatch"`, includes matched_id/title/score
   - AcoustID unverified (`vresult["status"] == "unverified"`): outcome `"unverified"`
   - AcoustID disabled or no expected recording ID: outcome `"accepted_no_verify"`
   - Fallback candidate re-downloaded and accepted: outcome `"accepted_unverified_fallback"`
   - Fallback candidate re-download failed: outcome `"download_failed"` (appended for the fallback attempt)

4. **Thread safety:** The candidate attempts buffer is a local variable inside `_process_single_track`, not shared state. Each thread (one per track in the ThreadPoolExecutor) has its own list. No lock needed for buffering. The flush calls `models.add_candidate_attempt()` which uses its own DB connection via `db.get_db()`.

5. **Flushing:** After `_record_track_failure` or `_accept_track_file`, both of which call `models.add_track_download()`, the returned `track_download_id` (via `cursor.lastrowid`) is used to flush all buffered attempts. Update `models.add_track_download()` to return the inserted row ID. Update `_record_track_failure` and `_accept_track_file` to accept and pass through the buffered attempts list.

6. **`models.add_candidate_attempt(track_download_id, attempt_dict)`** — inserts a single row. **`models.flush_candidate_attempts(track_download_id, attempts_list)`** — bulk inserts all buffered attempts for a track in a single transaction.

### Log Entries: Per-Track Rows for Failures

Currently, `_handle_post_download` creates a single `partial_success` or `album_error` log entry for the whole album. Change this to:

1. **Keep the album-level summary log** (`partial_success`, `album_error`, `download_success`, `import_success`, etc.) — these remain as compact single-line entries.

2. **Add per-track failure log entries.** In `_handle_post_download`, after creating the album-level log, loop through `failed_tracks` and create one log entry per failed track:
   - `log_type = "track_failure"`
   - `album_id`, `album_title`, `artist_name` — same as the album log
   - `track_title` — from `failed_track["title"]`
   - `track_number` — from `failed_track["track_num"]`
   - `track_download_id` — from `failed_track["track_download_id"]` (added to the dict in `_record_track_failure`)
   - `details` — the failure reason string from `failed_track["reason"]`

3. **Add columns to `download_logs`** (V5 migration):

```sql
ALTER TABLE download_logs ADD COLUMN track_title TEXT DEFAULT '';
ALTER TABLE download_logs ADD COLUMN track_number INTEGER DEFAULT NULL;
ALTER TABLE download_logs ADD COLUMN track_download_id INTEGER DEFAULT NULL;
```

4. **Update `models.add_log()` signature** to accept new parameters:
   ```python
   def add_log(
       log_type, album_id, album_title, artist_name,
       details="", total_file_size=0, track_number=None,
       track_title="", track_download_id=None,
   ):
   ```
   Update the INSERT statement to include all three new columns.

5. **Update `_record_track_failure`** to store and return `track_download_id`:
   - `models.add_track_download()` returns `cursor.lastrowid`
   - Add `track_download_id` to the dict appended to `failed_tracks`:
     ```python
     failed_tracks.append({
         "title": track_title,
         "reason": fail_reason,
         "track_num": track_num,
         "track_download_id": track_download_id,
     })
     ```

### API Changes

**GET /api/logs** — response items gain new fields for `track_failure` type:
- `track_title`, `track_number` — from the new columns
- `candidates` — array of candidate attempt objects, fetched by joining on `track_downloads` (matched by `album_id` + `track_number` + timestamp proximity) or by storing `track_download_id` on the log entry

The `track_download_id` column on `download_logs` (added in migration, see above) lets the API directly query `candidate_attempts WHERE track_download_id = ?`.

**Enrichment logic in `get_logs()` or `app.py`:** After fetching log rows, for any row with `type == "track_failure"` and a non-null `track_download_id`, fetch candidate attempts and attach as `candidates` array. Also cross-reference each candidate's `youtube_url` against `banned_urls` (by URL + album_id) to set `is_banned` and `ban_id` fields.

**GET /api/logs response for track_failure entries:**
```json
{
    "id": "1711900000000_123_3",
    "type": "track_failure",
    "album_id": 123,
    "album_title": "Album Name",
    "artist_name": "Artist",
    "track_title": "Track Name",
    "track_number": 3,
    "details": "AcoustID verification failed: no candidate matched expected recording abc-123 (tried 10 candidates)",
    "timestamp": 1711900000.0,
    "track_download_id": 456,
    "candidates": [
        {
            "youtube_url": "https://www.youtube.com/watch?v=abc",
            "youtube_title": "Artist - Track (Official Video)",
            "match_score": 0.87,
            "duration_seconds": 222,
            "outcome": "mismatch",
            "acoustid_matched_id": "def-456",
            "acoustid_matched_title": "Some Other Song",
            "acoustid_score": 0.92,
            "expected_recording_id": "abc-123",
            "error_message": "",
            "is_banned": true,
            "ban_id": 78
        },
        {
            "youtube_url": "https://www.youtube.com/watch?v=xyz",
            "youtube_title": "Artist Track HQ",
            "match_score": 0.81,
            "duration_seconds": 218,
            "outcome": "unverified",
            "acoustid_matched_id": "",
            "acoustid_matched_title": "",
            "acoustid_score": 0.0,
            "expected_recording_id": "abc-123",
            "error_message": "",
            "is_banned": false,
            "ban_id": null
        }
    ]
}
```

**DELETE /api/banned-urls/<ban_id>** — unchanged, still used for unbanning.

### Frontend: Compact Row Layout

Replace the card-based layout with compact rows matching the Downloads Recent History style.

**Row types:**

1. **Album summary rows** (download_success, partial_success, album_error, import_success, import_partial, download_started, manual_download):
   ```
   [status-icon]  Album Title  •  Artist  •  "details text"  •  142 MB  •  2h ago  [x]
   ```
   Single line. Left border color by type. Dismiss button on right.

2. **Track failure rows** (track_failure):
   ```
   [x-icon]  "Track Name"  •  Album — Artist  •  Track 3  •  2h ago  [x]
      [mismatch] "Artist - Track Official"  •  score: 0.87  •  3:42  •  AcoustID: "Some Other Song" (0.92)  [Unban]
      [unverified] "Artist Track HQ"  •  score: 0.81  •  3:38  •  AcoustID: no results
      [failed] "Artist - Track Live"  •  score: 0.74  •  5:12  •  Download failed: 403
   ```
   Main row is same compact style. Candidate sub-rows are always visible, indented, smaller font, dimmed. Banned candidates have orange accent + inline unban button.

3. **URL Banned rows** (from banned_urls table, for manually banned URLs not tied to a track failure):
   Kept for backwards compatibility with existing bans that predate this change. Same compact single-line format with unban button.

**Candidate sub-row styling:**
- Font size: 0.78rem
- Color: `var(--text-dim)`
- Left padding: 2.5rem (indented under parent track row)
- Outcome icon: color-coded (red for mismatch, yellow for unverified, gray for download_failed)
- Banned candidates: orange left border accent, unban button aligned right

**CSS changes:**
- Replace `.log-card` with `.log-row` — flexbox single line, `padding: 0.75rem 1rem`
- `.log-row` keeps the left border color by type
- `.candidate-row` — indented sub-row with smaller text
- Remove `.log-header`, `.log-info`, `.log-details`, `.log-details-title`, `.failed-tracks-list` styles
- Keep `.log-type` badge but make it smaller (inline pill)

### Filter Updates

Current filter options updated:
- Keep: All Types, Download Started, Download Success, Partial Success, Import Success, Import Partial, Import Failed, Album Error, Manual Download
- Add: **Track Failures** (`track_failure`) — shows only per-track failure rows with candidates
- Keep: **URL Banned** — shows manually banned URLs (pre-existing bans without candidate context)

### Migration Notes

- Existing log entries (pre-V5) will render in the new compact row style but won't have candidate data (they predate the capture).
- Existing banned URLs remain accessible via the "URL Banned" filter.
- The `banned_urls` table is unchanged. New AcoustID mismatches still create banned_urls entries (existing behavior). The candidate_attempts table records the attempt history independently.

## Files Changed

| File | Change |
|------|--------|
| `db.py` | V5 migration: `candidate_attempts` table, new columns on `download_logs` (`track_title`, `track_number`, `track_download_id`) |
| `models.py` | `add_candidate_attempt()`, `get_candidate_attempts_for_track()`, update `add_track_download()` to return ID, update `add_log()` to accept new fields, update `get_logs()` to include candidates for track_failure entries |
| `processing.py` | Buffer candidate outcomes during verification loop, flush to DB after track completes, create per-track failure log entries |
| `app.py` | Update `/api/logs` to enrich track_failure entries with candidate data and ban status |
| `templates/logs.html` | New compact row layout, candidate sub-rows, updated filters, updated CSS |
| `tests/test_db.py` | V5 migration test |
| `tests/test_models.py` | Tests for new model functions |
| `tests/test_processing.py` | Tests for candidate attempt capture |
| `tests/test_routes.py` | Tests for updated API response format |

## Out of Scope

- Changes to the Downloads page (already works well per user feedback)
- Changes to notification content (Telegram/Discord webhooks)
- Retroactive population of candidate data for past downloads
- Changes to the download/verification logic itself (only adding data capture)
