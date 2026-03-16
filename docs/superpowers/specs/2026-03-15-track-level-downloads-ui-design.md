# Track-Level Downloads UI Design

## Goal

Replace album-level display in Current Download and Download Queue sections with per-track detail, add per-track skip functionality with immediate cancellation, and show track lists in queued albums.

## Architecture

The existing SSE stream (`/api/download/stream`) already pushes `download_process` state every second. We expand this dict to hold a `tracks` list with per-track status, progress, and YouTube metadata. The frontend renders this list directly — no additional polling needed for Current Download. Queue track lists are fetched on-demand via a new API endpoint.

The skip mechanism uses yt-dlp's progress hook callback to detect a cancel flag and raise an exception, aborting the download within ~1 second. For the search phase (before download starts), the skip flag is checked between YouTube search candidates.

## Tech Stack

- Python 3.13, Flask, SQLite, yt-dlp Python API
- Vanilla JavaScript (no frameworks), Server-Sent Events
- Existing test stack: pytest

---

## 1. Backend State Model

### Expanded `download_process` dict (processing.py)

```python
download_process = {
    "active": False,
    "stop": False,                # album-level stop (Stop All)
    "album_id": None,
    "album_title": "",
    "artist_name": "",
    "cover_url": "",
    "tracks": [],                 # per-track state dicts
    "current_track_index": -1,    # index into tracks list
}
```

Each entry in `tracks`:

```python
{
    "track_title": "Thriller",
    "track_number": 3,
    "status": "pending",          # pending | searching | downloading | tagging | done | failed | skipped
    "youtube_url": "",
    "youtube_title": "",
    "progress_percent": "",
    "progress_speed": "",
    "error_message": "",
    "skip": False,                # set True via skip-track API
}
```

### Removed fields

The following fields from `download_process` are replaced by the per-track `tracks` list:

- `progress` dict (current, total, percent, speed, overall_percent) — replaced by per-track progress + computed overall. The initialization block (lines 85–91) and teardown block (lines 306–313) in `processing.py` should reset `tracks` to `[]` and `current_track_index` to `-1` instead of setting `progress`.
- `current_track_title` — replaced by `tracks[current_track_index]["track_title"]`

The `result_success` and `result_partial` fields remain unchanged.

### Thread safety

The `tracks` list is written by the download thread and read by the SSE thread. Individual dict value assignments are atomic under Python's GIL. This is the same safety model as the existing `download_process` dict.

The skip-track API endpoint must acquire `queue_lock` when setting `tracks[N]["skip"] = True`, consistent with the existing pattern where `get_download_status()` and `stop_download()` both acquire the lock.

---

## 2. Skip Mechanism

### Custom exception

```python
class TrackSkippedException(Exception):
    """Raised from yt-dlp progress hook when track skip is requested."""
```

### Progress hook changes (processing.py)

The `update_progress` function checks the current track's skip flag on every callback (~1/second during download):

```python
def update_progress(d):
    if d["status"] == "downloading":
        idx = download_process.get("current_track_index", -1)
        if idx >= 0:
            track = download_process["tracks"][idx]
            track["progress_percent"] = d.get("_percent_str", "0%").strip()
            track["progress_speed"] = d.get("_speed_str", "N/A").strip()
            if track.get("skip"):
                raise TrackSkippedException()
```

### Search phase cancellation (downloader.py)

`download_track_youtube` accepts an optional `skip_check` callable. Between scoring YouTube search candidates, it calls `skip_check()`. If it returns True, the function returns early with `{"skipped": True}`.

### Download loop changes (_download_tracks in processing.py)

For each track:

1. Check if pre-skipped (user clicked Skip while pending) — if so, mark "skipped", continue
2. Set status to "searching", call `download_track_youtube` with `skip_check` callback
3. If skipped during search, mark "skipped", continue
4. yt-dlp downloads — progress hook may raise `TrackSkippedException`
5. Catch `TrackSkippedException`: clean up temp files, mark "skipped", continue to next track
6. On success: set status "tagging", run `tag_mp3`, set status "done"
7. On failure: set status "failed" with error message

### Stop All behavior

Unchanged: sets `download_process["stop"] = True` and clears queue. The download loop checks this flag between tracks (same as today). Stop All does NOT use the per-track skip mechanism.

---

## 3. New API Endpoints

### POST /api/download/skip-track

Request: `{"track_index": N}`

Behavior:
- If no active download: return 409
- If track_index out of range: return 400
- Sets `download_process["tracks"][N]["skip"] = True`
- Returns `{"success": true}`

Rate-limited same as other download actions.

### GET /api/download/queue/\<album_id\>/tracks

Returns track list for a queued album.

Behavior:
- Fetches tracks from Lidarr API (`/api/v1/track?albumId={album_id}`)
- Falls back to iTunes track lookup if Lidarr returns empty
- Returns `[{"title": "...", "track_number": N, "has_file": bool}, ...]`
- Lidarr responses cached via `_get_album_cached`. If Lidarr returns no tracks and iTunes fallback is used, the result is not cached (same as `process_album_download` behavior — iTunes is a fallback, not a primary source)

---

## 4. SSE Stream Changes

The SSE payload structure changes:

**Before:**
```json
{
    "status": {
        "active": true,
        "album_title": "Thriller",
        "artist_name": "Michael Jackson",
        "current_track_title": "Beat It",
        "progress": {"current": 3, "total": 9, "percent": "67%", "speed": "2.4MiB/s", "overall_percent": 33},
        "cover_url": "...",
        "stop": false
    },
    "queue": [{"id": 123, "title": "...", "artist": "...", "cover_url": "..."}]
}
```

**After:**
```json
{
    "status": {
        "active": true,
        "album_title": "Thriller",
        "artist_name": "Michael Jackson",
        "cover_url": "...",
        "stop": false,
        "current_track_index": 2,
        "tracks": [
            {"track_title": "Wanna Be Startin'", "track_number": 1, "status": "done", "youtube_url": "...", "youtube_title": "...", "progress_percent": "", "progress_speed": "", "error_message": "", "skip": false},
            {"track_title": "Baby Be Mine", "track_number": 2, "status": "done", "...": "..."},
            {"track_title": "Thriller", "track_number": 3, "status": "downloading", "progress_percent": "67%", "progress_speed": "2.4MiB/s", "youtube_url": "...", "...": "..."},
            {"track_title": "Beat It", "track_number": 4, "status": "pending", "...": "..."}
        ]
    },
    "queue": [{"id": 456, "title": "Back in Black", "artist": "AC/DC", "cover_url": "...", "track_count": 10}]
}
```

Queue items gain a `track_count` field (computed from cached album data, no extra API call).

---

## 5. Frontend Changes (downloads.html)

### Current Download section

`updateCurrentFromSSE(data)` renders:

1. **Album header:** Cover art, title, artist, overall progress ("Track N of M"), Stop All button
2. **Track grid:** All tracks from `data.tracks` array
   - Grid: `40px 1fr 1.2fr 80px 60px` with `gap: 12px`
   - Columns: #, Title, YouTube Source, Status, Action
   - Active track: highlighted background, progress bar under title, speed, YouTube URL, Skip button
   - Done tracks: green checkmark, YouTube link
   - Pending tracks: dimmed (opacity 0.5), Skip button
   - Failed: red X, error message
   - Skipped: amber arrow, "Skipped by user"
   - Searching: blue text, "Searching YouTube..."
   - Tagging: purple text, "Tagging metadata..."

Overall progress is computed client-side: `(done + failed + skipped count) / total tracks * 100`. Skipped tracks count toward progress intentionally — from the user's perspective, a skipped track is "handled" and shouldn't hold back the progress bar.

### Download Queue section

`updateQueueFromSSE(queue, statusData)` renders:

1. Each queue item: position, cover, title, artist, track count badge, expand chevron, Remove button
2. Position 1: auto-expanded, tracks fetched via `GET /api/download/queue/<id>/tracks`
3. Others: collapsed, expandable on click
4. `expandedQueueIds` Set preserves expansion across SSE rebuilds (same pattern as `expandedAlbumIds` in history)

### DOM safety

All dynamic content rendered via `document.createElement` / `textContent` (no innerHTML). YouTube URLs validated through existing `sanitizeUrl()`. Same XSS-safe patterns as the existing track detail grid in Recent History.

---

## 6. Files Changed

| File | Change |
|------|--------|
| `processing.py` | Expand `download_process`, add `TrackSkippedException`, modify `update_progress`, `_download_tracks`, `stop_download` |
| `downloader.py` | Add `skip_check` callback parameter to `download_track_youtube` |
| `app.py` | Add `POST /api/download/skip-track`, add `GET /api/download/queue/<id>/tracks`, update SSE stream to include `track_count` |
| `templates/downloads.html` | Rewrite `updateCurrentFromSSE`, rewrite `updateQueueFromSSE`, add `expandedQueueIds`, add `skipTrack()` JS function |
| `tests/test_processing.py` | Tests for skip mechanism, track state transitions, Stop All regression |
| `tests/test_routes.py` | Tests for new endpoints, SSE payload shape |
| `tests/test_downloader.py` | Test for `skip_check` callback |
| `TESTING.md` | Add manual test cases for track-level UI |

---

## 7. Testing

### Automated (pytest)

- Track skip flag sets status to "skipped" and continues to next track
- `TrackSkippedException` in progress hook aborts download, cleans temp files
- Pre-skipped pending tracks are skipped without starting download
- Stop All still stops everything + clears queue (regression)
- `download_process["tracks"]` list populated correctly with all state transitions
- `POST /api/download/skip-track` with valid index sets skip flag, returns 200
- `POST /api/download/skip-track` with invalid index returns 400
- `POST /api/download/skip-track` when no active download returns 409
- `GET /api/download/queue/<id>/tracks` returns track list
- SSE stream includes `tracks` list and `track_count` in payload
- `skip_check` callback returning True aborts search early in `download_track_youtube`

### Manual (TESTING.md)

- Current Download shows per-track progress during active download
- Skip button on active track stops it within a few seconds, next track starts
- Skip button on pending track marks it skipped (never downloads)
- Stop All stops current track + clears queue
- Download Queue position 1 auto-expanded with tracks
- Queue items expandable/collapsible on click
- Expansion state preserved across SSE updates
- Track states cycle correctly: pending -> searching -> downloading -> tagging -> done
