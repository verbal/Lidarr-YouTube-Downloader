# Track-Level Downloads UI Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace album-level display in Current Download and Download Queue sections with per-track detail, add per-track skip functionality with immediate cancellation, and show track lists in queued albums.

**Architecture:** Expand `download_process` dict with a `tracks` list holding per-track state. Skip mechanism uses yt-dlp progress hook to raise `TrackSkippedException` for immediate cancellation. New API endpoints for skip-track and queue track listing. Frontend rewrites `updateCurrentFromSSE` and `updateQueueFromSSE` using DOM-safe rendering.

**Tech Stack:** Python 3.13, Flask, SQLite, yt-dlp Python API, Vanilla JavaScript, Server-Sent Events, pytest

**Spec:** `docs/superpowers/specs/2026-03-15-track-level-downloads-ui-design.md`

---

## Chunk 1: Backend — State Model and Skip Mechanism

### Task 1: Expand download_process state model and TrackSkippedException

**Files:**
- Modify: `processing.py:32-52` (download_process dict, update_progress)
- Test: `tests/test_processing.py`

- [ ] **Step 1: Write failing tests for new state model**

In `tests/test_processing.py`, add a new test class after the existing `TestDownloadTracks`:

```python
class TestTrackStateModel:
    """download_process tracks list and TrackSkippedException."""

    def test_download_process_has_tracks_list(self):
        from processing import download_process
        assert "tracks" in download_process
        assert isinstance(download_process["tracks"], list)
        assert download_process["current_track_index"] == -1

    def test_download_process_no_legacy_fields(self):
        from processing import download_process
        assert "progress" not in download_process
        assert "current_track_title" not in download_process

    def test_track_skipped_exception_exists(self):
        from processing import TrackSkippedException
        assert issubclass(TrackSkippedException, Exception)

    def test_update_progress_sets_track_fields(self):
        from processing import download_process, update_progress
        download_process["tracks"] = [
            {"track_title": "T1", "track_number": 1, "status": "downloading",
             "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        download_process["current_track_index"] = 0
        update_progress({
            "status": "downloading",
            "_percent_str": " 45.2% ",
            "_speed_str": " 2.4MiB/s ",
        })
        track = download_process["tracks"][0]
        assert track["progress_percent"] == "45.2%"
        assert track["progress_speed"] == "2.4MiB/s"
        # cleanup
        download_process["tracks"] = []
        download_process["current_track_index"] = -1

    def test_update_progress_raises_on_skip_flag(self):
        from processing import (
            TrackSkippedException, download_process, update_progress,
        )
        download_process["tracks"] = [
            {"track_title": "T1", "track_number": 1, "status": "downloading",
             "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": True},
        ]
        download_process["current_track_index"] = 0
        with pytest.raises(TrackSkippedException):
            update_progress({"status": "downloading",
                             "_percent_str": "10%", "_speed_str": "1MiB/s"})
        # cleanup
        download_process["tracks"] = []
        download_process["current_track_index"] = -1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_processing.py::TestTrackStateModel -v`
Expected: FAIL — `tracks` and `current_track_index` not in `download_process`, `TrackSkippedException` not defined.

- [ ] **Step 3: Implement state model changes**

In `processing.py`, replace `download_process` dict (lines 32-41) with:

```python
download_process = {
    "active": False,
    "stop": False,
    "album_id": None,
    "album_title": "",
    "artist_name": "",
    "cover_url": "",
    "tracks": [],
    "current_track_index": -1,
}
```

Add `TrackSkippedException` class right after the `queue_lock` line (after line 43):

```python
class TrackSkippedException(Exception):
    """Raised from yt-dlp progress hook when track skip is requested."""
```

Replace `update_progress` function (lines 46-52) with:

```python
def update_progress(d):
    """yt-dlp progress hook that updates per-track progress state."""
    if d["status"] == "downloading":
        idx = download_process.get("current_track_index", -1)
        if idx >= 0 and idx < len(download_process["tracks"]):
            track = download_process["tracks"][idx]
            track["progress_percent"] = d.get("_percent_str", "0%").strip()
            track["progress_speed"] = d.get("_speed_str", "N/A").strip()
            if track.get("skip"):
                raise TrackSkippedException()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_processing.py::TestTrackStateModel -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add processing.py tests/test_processing.py
git commit -m "feat: expand download_process with per-track state and TrackSkippedException"
```

---

### Task 2: Update _download_tracks for per-track state and skip handling

**Files:**
- Modify: `processing.py:68-96` (process_album_download init/finally), `processing.py:338-494` (_download_tracks)
- Test: `tests/test_processing.py`

- [ ] **Step 1: Write failing tests for per-track state transitions**

Add to `tests/test_processing.py`:

```python
class TestTrackStateTransitions:
    """_download_tracks populates tracks list and handles skip."""

    @patch("processing.download_track_youtube")
    @patch("processing.tag_mp3")
    @patch("processing.create_xml_metadata")
    def test_tracks_populated_with_state(
        self, mock_xml, mock_tag, mock_dl, tmp_path,
    ):
        from processing import _download_tracks, download_process
        mock_dl.return_value = {
            "success": True, "youtube_url": "https://youtube.com/watch?v=abc",
            "youtube_title": "Title", "match_score": 0.9,
            "duration_seconds": 200,
        }
        album_path = str(tmp_path / "album")
        os.makedirs(album_path)
        tracks = [
            {"title": "Track 1", "trackNumber": 1, "duration": 200000},
            {"title": "Track 2", "trackNumber": 2, "duration": 180000},
        ]
        download_process["tracks"] = [
            {"track_title": t["title"], "track_number": int(t["trackNumber"]),
             "status": "pending", "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False}
            for t in tracks
        ]
        download_process["current_track_index"] = -1
        download_process["stop"] = False
        # Create fake downloaded files so shutil.move works
        for t in tracks:
            num = int(t["trackNumber"])
            temp_name = None
            # We'll need to patch uuid to predict the filename
            # Instead, just create all .mp3 files in album_path matching temp pattern
        # Simpler: mock os.path.exists for actual_file and shutil.move
        with patch("processing.shutil.move"), \
             patch("processing.os.path.getsize", return_value=1000), \
             patch("processing.os.path.exists", return_value=True):
            failed, size = _download_tracks(
                tracks, album_path, {}, _make_album_ctx(),
            )
        assert len(failed) == 0
        # Verify tracks were updated
        assert download_process["tracks"][0]["status"] == "done"
        assert download_process["tracks"][1]["status"] == "done"
        # cleanup
        download_process["tracks"] = []
        download_process["current_track_index"] = -1

    @patch("processing.download_track_youtube")
    def test_pre_skipped_track_never_downloads(self, mock_dl, tmp_path):
        from processing import _download_tracks, download_process
        album_path = str(tmp_path / "album")
        os.makedirs(album_path)
        tracks = [
            {"title": "Track 1", "trackNumber": 1, "duration": 200000},
        ]
        download_process["tracks"] = [
            {"track_title": "Track 1", "track_number": 1,
             "status": "pending", "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": True},
        ]
        download_process["current_track_index"] = -1
        download_process["stop"] = False
        failed, size = _download_tracks(
            tracks, album_path, {}, _make_album_ctx(),
        )
        mock_dl.assert_not_called()
        assert download_process["tracks"][0]["status"] == "skipped"
        # cleanup
        download_process["tracks"] = []
        download_process["current_track_index"] = -1

    @patch("processing.download_track_youtube")
    @patch("processing.tag_mp3")
    @patch("processing.create_xml_metadata")
    def test_stop_all_still_stops_everything(
        self, mock_xml, mock_tag, mock_dl, tmp_path,
    ):
        from processing import _download_tracks, download_process
        download_process["stop"] = True
        album_path = str(tmp_path / "album")
        os.makedirs(album_path)
        tracks = [
            {"title": "Track 1", "trackNumber": 1, "duration": 200000},
        ]
        download_process["tracks"] = [
            {"track_title": "Track 1", "track_number": 1,
             "status": "pending", "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        download_process["current_track_index"] = -1
        failed, size = _download_tracks(
            tracks, album_path, {}, _make_album_ctx(),
        )
        mock_dl.assert_not_called()
        # cleanup
        download_process["tracks"] = []
        download_process["current_track_index"] = -1
        download_process["stop"] = False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_processing.py::TestTrackStateTransitions -v`
Expected: FAIL — `_download_tracks` doesn't manage per-track state or check skip flags.

- [ ] **Step 3: Update process_album_download init and finally blocks**

In `processing.py`, update the init block in `process_album_download` (lines 78-96). Replace lines 84-96 with:

```python
        download_process["result_success"] = True
        download_process["result_partial"] = False
        download_process["tracks"] = []
        download_process["current_track_index"] = -1
        download_process["album_id"] = album_id
        download_process["album_title"] = ""
        download_process["artist_name"] = ""
        download_process["cover_url"] = ""
```

Update the finally block (lines 305-313). Replace with:

```python
    finally:
        with queue_lock:
            download_process["active"] = False
            download_process["tracks"] = []
            download_process["current_track_index"] = -1
            download_process["album_id"] = None
            download_process["album_title"] = ""
            download_process["artist_name"] = ""
            download_process["cover_url"] = ""
```

- [ ] **Step 4: Rewrite _download_tracks to manage per-track state**

Replace the `_download_tracks` function body (lines 338-494) with the new implementation. Key changes:
- Before the loop: no changes to tracks list init (caller sets it up)
- Check `track["skip"]` before starting each track — if pre-skipped, set status "skipped", continue
- Set status to "searching" before `download_track_youtube` call
- Pass `skip_check` callback (Task 3 adds this to downloader)
- Wrap download in try/except for `TrackSkippedException` — clean up temp files, set status "skipped"
- On success: set status "tagging", run tag_mp3, set status "done", record youtube_url/youtube_title on the track dict
- On failure: set status "failed", set error_message
- Remove all `download_process["progress"]` and `download_process["current_track_title"]` references

```python
def _download_tracks(
    tracks_to_download, album_path, album, album_ctx,
):
    """Download each track, tag, and create XML metadata.

    Args:
        tracks_to_download: List of track dicts to download.
        album_path: Local directory for downloaded files.
        album: Full album data dict from Lidarr.
        album_ctx: Dict with keys: artist_name, album_title, album_id,
            album_mbid, artist_mbid, cover_data, cover_url,
            lidarr_album_path.

    Returns:
        Tuple of (failed_tracks list, total_downloaded_size int).
    """
    artist_name = album_ctx["artist_name"]
    album_title = album_ctx["album_title"]
    album_id = album_ctx["album_id"]
    album_mbid = album_ctx["album_mbid"]
    artist_mbid = album_ctx["artist_mbid"]
    cover_data = album_ctx["cover_data"]
    cover_url = album_ctx["cover_url"]
    lidarr_album_path = album_ctx["lidarr_album_path"]

    failed_tracks = []
    total_downloaded_size = 0

    for idx, track in enumerate(tracks_to_download):
        if download_process["stop"]:
            logger.warning("Download stopped by user")
            break

        track_title = track["title"]
        try:
            track_num = int(track.get("trackNumber", idx + 1))
        except (ValueError, TypeError):
            track_num = idx + 1

        download_process["current_track_index"] = idx
        track_state = download_process["tracks"][idx]

        # Check if pre-skipped by user
        if track_state.get("skip"):
            track_state["status"] = "skipped"
            logger.info("Track '%s' skipped by user (pre-skip)", track_title)
            continue

        track_state["status"] = "searching"

        logger.info(
            "Downloading track %d/%d: %s",
            idx + 1, len(tracks_to_download), track_title,
        )

        sanitized_track = sanitize_filename(track_title)
        temp_file = os.path.join(
            album_path,
            f"temp_{track_num:02d}_{uuid.uuid4().hex[:8]}",
        )
        final_file = os.path.join(
            album_path, f"{track_num:02d} - {sanitized_track}.mp3",
        )

        def _skip_check():
            return track_state.get("skip", False)

        track_duration_ms = track.get("duration")
        try:
            download_result = download_track_youtube(
                f"{artist_name} {track_title} official audio",
                temp_file,
                track_title,
                track_duration_ms,
                progress_hook=update_progress,
                skip_check=_skip_check,
            )
        except TrackSkippedException:
            _cleanup_temp_files(temp_file)
            track_state["status"] = "skipped"
            logger.info("Track '%s' skipped by user (during download)", track_title)
            continue

        # Handle skip during search phase
        if download_result.get("skipped"):
            track_state["status"] = "skipped"
            logger.info("Track '%s' skipped by user (during search)", track_title)
            continue

        actual_file = temp_file + ".mp3"

        if download_result.get("success") and os.path.exists(actual_file):
            logger.info("Track downloaded successfully: %s", track_title)
            track_state["status"] = "tagging"
            track_state["youtube_url"] = download_result.get("youtube_url", "")
            track_state["youtube_title"] = download_result.get("youtube_title", "")
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
            track_state["status"] = "done"
            try:
                models.add_track_download(
                    album_id=album_id, album_title=album_title,
                    artist_name=artist_name, track_title=track_title,
                    track_number=track_num, success=True,
                    error_message="",
                    youtube_url=download_result.get("youtube_url", ""),
                    youtube_title=download_result.get("youtube_title", ""),
                    match_score=download_result.get("match_score", 0.0),
                    duration_seconds=download_result.get("duration_seconds", 0),
                    album_path=album_path,
                    lidarr_album_path=lidarr_album_path,
                    cover_url=cover_url,
                )
            except Exception:
                logger.error(
                    "Failed to record track download for '%s' (album %d)",
                    track_title, album_id, exc_info=True,
                )
        else:
            fail_reason = download_result.get(
                "error_message", "Download failed or file not found",
            )
            logger.warning(
                "Failed to download track: %s -- %s", track_title, fail_reason,
            )
            _cleanup_temp_files(temp_file)
            track_state["status"] = "failed"
            track_state["error_message"] = fail_reason
            failed_tracks.append({
                "title": track_title,
                "reason": fail_reason,
                "track_num": track_num,
            })
            try:
                models.add_track_download(
                    album_id=album_id, album_title=album_title,
                    artist_name=artist_name, track_title=track_title,
                    track_number=track_num, success=False,
                    error_message=fail_reason,
                    youtube_url="", youtube_title="",
                    match_score=0.0, duration_seconds=0,
                    album_path=album_path,
                    lidarr_album_path=lidarr_album_path,
                    cover_url=cover_url,
                )
            except Exception:
                logger.error(
                    "Failed to record track download for '%s' (album %d)",
                    track_title, album_id, exc_info=True,
                )

    return failed_tracks, total_downloaded_size
```

- [ ] **Step 5: Add _cleanup_temp_files helper**

Add this helper function right before `_download_tracks` in `processing.py`:

```python
def _cleanup_temp_files(temp_file):
    """Remove temp download files for all common extensions."""
    for ext in [".mp3", ".webm", ".m4a", ".part", ""]:
        tmp = temp_file + ext
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError as rm_err:
                logger.debug("Failed to remove temp file %s: %s", tmp, rm_err)
```

- [ ] **Step 6: Update process_album_download to populate tracks list**

In `process_album_download`, after `tracks_to_download` is computed from `_filter_tracks` (around line 240), add the tracks list population. Find the line that calls `_download_tracks` and add before it:

```python
        download_process["tracks"] = [
            {
                "track_title": t["title"],
                "track_number": int(t.get("trackNumber", i + 1)),
                "status": "pending",
                "youtube_url": "",
                "youtube_title": "",
                "progress_percent": "",
                "progress_speed": "",
                "error_message": "",
                "skip": False,
            }
            for i, t in enumerate(tracks_to_download)
        ]
```

- [ ] **Step 7: Run all tests to verify**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_processing.py -v`
Expected: All tests PASS. Existing tests may need minor updates if they reference `download_process["progress"]` or `download_process["current_track_title"]` — fix those references.

- [ ] **Step 8: Fix any existing test references to removed fields**

In `tests/test_processing.py`, find any references to `download_process["progress"]` or `download_process["current_track_title"]` and update them. These are in `TestDownloadTracks` (around lines 73-77 and 119-123). Replace assertions like:
- `download_process["progress"]["current"]` → removed, verify `download_process["current_track_index"]` instead
- `download_process["current_track_title"]` → verify `download_process["tracks"][idx]["track_title"]` instead

- [ ] **Step 9: Run full test suite**

Run: `source .venv/bin/activate && python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 10: Commit**

```bash
git add processing.py tests/test_processing.py
git commit -m "feat: rewrite _download_tracks for per-track state transitions and skip handling"
```

---

### Task 3: Add skip_check callback to download_track_youtube

**Files:**
- Modify: `downloader.py:132-135` (signature), `downloader.py:195-307` (search loop)
- Test: `tests/test_downloader.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_downloader.py`:

```python
class TestSkipCheck:
    """skip_check callback aborts search early."""

    @patch("downloader.yt_dlp.YoutubeDL")
    def test_skip_check_true_returns_skipped(self, mock_ydl_cls):
        from downloader import download_track_youtube
        result = download_track_youtube(
            "Artist Track official audio",
            "/tmp/test_output",
            "Track",
            expected_duration_ms=200000,
            progress_hook=None,
            skip_check=lambda: True,
        )
        assert result.get("skipped") is True
        mock_ydl_cls.assert_not_called()

    @patch("downloader.yt_dlp.YoutubeDL")
    def test_skip_check_false_continues(self, mock_ydl_cls):
        mock_ydl = mock_ydl_cls.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {"entries": []}
        from downloader import download_track_youtube
        result = download_track_youtube(
            "Artist Track official audio",
            "/tmp/test_output",
            "Track",
            expected_duration_ms=200000,
            progress_hook=None,
            skip_check=lambda: False,
        )
        assert result.get("skipped") is not True
        assert result.get("success") is False  # no candidates found
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_downloader.py::TestSkipCheck -v`
Expected: FAIL — `skip_check` parameter not accepted.

- [ ] **Step 3: Add skip_check parameter to download_track_youtube**

In `downloader.py`, update the function signature (lines 132-135):

```python
def download_track_youtube(
    query, output_path, track_title_original,
    expected_duration_ms=None, progress_hook=None, skip_check=None,
):
```

Add skip_check call at the very beginning of the function body, before the config loading:

```python
    if skip_check and skip_check():
        return {"skipped": True}
```

Add skip_check call between search query iterations. In the search loop `for qi, sq in enumerate(search_queries):` (line 195), add at the start of the loop body:

```python
        if skip_check and skip_check():
            return {"skipped": True}
```

Add skip_check call after candidates are sorted (after line 307), before starting the download:

```python
    if skip_check and skip_check():
        return {"skipped": True}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_downloader.py::TestSkipCheck -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `source .venv/bin/activate && python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add downloader.py tests/test_downloader.py
git commit -m "feat: add skip_check callback to download_track_youtube"
```

---

## Chunk 2: Backend — API Endpoints and SSE Changes

### Task 4: Add POST /api/download/skip-track endpoint

**Files:**
- Modify: `app.py` (add route)
- Test: `tests/test_routes.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_routes.py`:

```python
class TestSkipTrackRoute:
    """POST /api/download/skip-track sets skip flag."""

    def test_skip_no_active_download(self, client):
        resp = client.post("/api/download/skip-track",
                           json={"track_index": 0})
        assert resp.status_code == 409

    def test_skip_invalid_index(self, client):
        from processing import download_process
        download_process["active"] = True
        download_process["tracks"] = [
            {"track_title": "T1", "track_number": 1, "status": "pending",
             "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        try:
            resp = client.post("/api/download/skip-track",
                               json={"track_index": 5})
            assert resp.status_code == 400
        finally:
            download_process["active"] = False
            download_process["tracks"] = []

    def test_skip_valid_index(self, client):
        from processing import download_process
        download_process["active"] = True
        download_process["tracks"] = [
            {"track_title": "T1", "track_number": 1, "status": "pending",
             "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        try:
            resp = client.post("/api/download/skip-track",
                               json={"track_index": 0})
            assert resp.status_code == 200
            assert download_process["tracks"][0]["skip"] is True
        finally:
            download_process["active"] = False
            download_process["tracks"] = []

    def test_skip_missing_track_index(self, client):
        from processing import download_process
        download_process["active"] = True
        download_process["tracks"] = [
            {"track_title": "T1", "track_number": 1, "status": "pending",
             "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        try:
            resp = client.post("/api/download/skip-track", json={})
            assert resp.status_code == 400
        finally:
            download_process["active"] = False
            download_process["tracks"] = []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_routes.py::TestSkipTrackRoute -v`
Expected: FAIL — 404, route doesn't exist.

- [ ] **Step 3: Implement the endpoint**

In `app.py`, add after the existing stop-download route:

```python
@app.route("/api/download/skip-track", methods=["POST"])
def api_skip_track():
    data = request.json or {}
    track_index = data.get("track_index")
    if track_index is None:
        return jsonify({"error": "track_index required"}), 400
    with queue_lock:
        if not download_process["active"]:
            return jsonify({"error": "No active download"}), 409
        tracks = download_process.get("tracks", [])
        if track_index < 0 or track_index >= len(tracks):
            return jsonify({"error": "Invalid track_index"}), 400
        tracks[track_index]["skip"] = True
    return jsonify({"success": True})
```

Make sure to import `download_process` alongside `queue_lock` at the top of `app.py` (it should already be imported).

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_routes.py::TestSkipTrackRoute -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_routes.py
git commit -m "feat: add POST /api/download/skip-track endpoint"
```

---

### Task 5: Add GET /api/download/queue/<album_id>/tracks endpoint

**Files:**
- Modify: `app.py` (add route)
- Test: `tests/test_routes.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_routes.py`:

```python
class TestQueueTracksRoute:
    """GET /api/download/queue/<album_id>/tracks returns track list."""

    @patch("app.lidarr_request")
    def test_returns_tracks_from_lidarr(self, mock_lidarr, client):
        mock_lidarr.return_value = [
            {"title": "Track 1", "trackNumber": 1, "hasFile": False},
            {"title": "Track 2", "trackNumber": 2, "hasFile": True},
        ]
        resp = client.get("/api/download/queue/123/tracks")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 2
        assert data[0]["title"] == "Track 1"
        assert data[0]["track_number"] == 1
        assert data[0]["has_file"] is False
        assert data[1]["has_file"] is True

    @patch("app.lidarr_request")
    @patch("app.get_itunes_tracks")
    def test_falls_back_to_itunes(self, mock_itunes, mock_lidarr, client):
        mock_lidarr.return_value = []
        mock_itunes.return_value = [
            {"title": "iTunes Track", "trackNumber": 1},
        ]
        # Need to set up album cache for the fallback to know artist/album
        from app import album_cache
        import time as time_mod
        album_cache[123] = (
            {"title": "Album", "artist": {"artistName": "Artist"}},
            time_mod.time(),
        )
        try:
            resp = client.get("/api/download/queue/123/tracks")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) == 1
            assert data[0]["title"] == "iTunes Track"
        finally:
            album_cache.pop(123, None)

    @patch("app.lidarr_request")
    def test_empty_when_no_tracks(self, mock_lidarr, client):
        mock_lidarr.return_value = []
        resp = client.get("/api/download/queue/999/tracks")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_routes.py::TestQueueTracksRoute -v`
Expected: FAIL — 404, route doesn't exist.

- [ ] **Step 3: Implement the endpoint**

In `app.py`, add the route and ensure `get_itunes_tracks` is imported from `metadata`:

```python
@app.route("/api/download/queue/<int:album_id>/tracks")
def api_queue_tracks(album_id):
    tracks = lidarr_request(f"track?albumId={album_id}")
    if isinstance(tracks, dict) and "error" in tracks:
        tracks = []
    if not tracks:
        # Fallback to iTunes
        album = _get_album_cached(album_id)
        if "error" not in album:
            artist = album.get("artist", {}).get("artistName", "")
            title = album.get("title", "")
            if artist and title:
                tracks = get_itunes_tracks(artist, title)
    result = [
        {
            "title": t.get("title", ""),
            "track_number": t.get("trackNumber", 0),
            "has_file": t.get("hasFile", False),
        }
        for t in tracks
    ]
    return jsonify(result)
```

Add `get_itunes_tracks` to the imports from `metadata` if not already present.

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_routes.py::TestQueueTracksRoute -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_routes.py
git commit -m "feat: add GET /api/download/queue/<album_id>/tracks endpoint"
```

---

### Task 6: Update SSE stream to include tracks and track_count

**Files:**
- Modify: `app.py:287-327` (SSE stream), `app.py:333-353` (queue GET)
- Test: `tests/test_routes.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_routes.py`:

```python
class TestQueueTrackCount:
    """SSE and queue endpoints include track_count."""

    @patch("app.lidarr_request")
    @patch("app.models.get_queue")
    def test_queue_includes_track_count(self, mock_queue, mock_lidarr, client):
        mock_queue.return_value = [{"album_id": 123}]
        mock_lidarr.side_effect = lambda path: (
            {"title": "Album", "artist": {"artistName": "Art"},
             "images": [{"coverType": "cover", "remoteUrl": "http://img"}],
             "statistics": {"trackCount": 10}}
            if "album/" in path else
            [{"title": f"T{i}", "trackNumber": i, "hasFile": False}
             for i in range(1, 11)]
        )
        resp = client.get("/api/download/queue")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0].get("track_count") == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_routes.py::TestQueueTrackCount -v`
Expected: FAIL — `track_count` not in response.

- [ ] **Step 3: Add track_count to queue data**

In `app.py`, update both the SSE stream queue builder (lines 308-313) and the queue GET route (lines 340-351) to include `track_count`. Extract it from album statistics:

In the SSE stream `queue_data.append(...)`:
```python
queue_data.append({
    "id": row["album_id"],
    "title": album.get("title", ""),
    "artist": album.get("artist", {}).get("artistName", ""),
    "cover_url": cover_url,
    "track_count": album.get("statistics", {}).get("trackCount", 0),
})
```

In the queue GET route `queue_with_details.append(...)`:
```python
queue_with_details.append({
    "id": row["album_id"],
    "title": album.get("title", ""),
    "artist": album.get("artist", {}).get("artistName", ""),
    "cover": next(
        (
            img["remoteUrl"]
            for img in album.get("images", [])
            if img["coverType"] == "cover"
        ),
        "",
    ),
    "track_count": album.get("statistics", {}).get("trackCount", 0),
})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_routes.py::TestQueueTrackCount -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `source .venv/bin/activate && python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_routes.py
git commit -m "feat: add track_count to SSE stream and queue endpoint"
```

---

## Chunk 3: Frontend — Downloads Page UI

### Task 7: Rewrite Current Download section (updateCurrentFromSSE)

**Files:**
- Modify: `templates/downloads.html:995-1043` (updateCurrentFromSSE), CSS section

This is the largest frontend change. The function must render per-track rows with status indicators, progress bars, YouTube links, and Skip buttons — all using DOM-safe methods (createElement/textContent, no innerHTML for dynamic content).

- [ ] **Step 1: Add CSS styles for track grid**

In the `<style>` section of `templates/downloads.html`, add these styles:

```css
.current-track-grid {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-top: 12px;
    width: 100%;
}
.current-track-row {
    display: grid;
    grid-template-columns: 40px 1fr 1.2fr 80px 60px;
    gap: 12px;
    align-items: center;
    padding: 6px 8px;
    border-radius: 6px;
    font-size: 0.85rem;
}
.current-track-row.active {
    background: rgba(99, 102, 241, 0.15);
}
.current-track-row.done {
    opacity: 0.7;
}
.current-track-row.pending {
    opacity: 0.5;
}
.current-track-row.skipped {
    opacity: 0.6;
}
.current-track-row.failed {
    background: rgba(239, 68, 68, 0.1);
}
.current-track-num {
    text-align: center;
    color: var(--text-secondary, #888);
    font-weight: 500;
}
.current-track-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
}
.current-track-title {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.current-track-progress {
    height: 3px;
    background: rgba(255,255,255,0.1);
    border-radius: 2px;
    overflow: hidden;
}
.current-track-progress-fill {
    height: 100%;
    background: var(--accent, #6366f1);
    transition: width 0.3s;
}
.current-track-yt {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-size: 0.8rem;
}
.current-track-yt a {
    color: var(--accent, #6366f1);
    text-decoration: none;
}
.current-track-status {
    font-size: 0.8rem;
    text-align: center;
}
.current-track-status .done { color: #22c55e; }
.current-track-status .failed { color: #ef4444; }
.current-track-status .skipped { color: #f59e0b; }
.current-track-status .searching { color: #3b82f6; }
.current-track-status .tagging { color: #a855f7; }
.current-track-status .downloading { color: #6366f1; }
.current-track-skip-btn {
    background: transparent;
    border: 1px solid rgba(255,255,255,0.2);
    color: var(--text-primary, #fff);
    border-radius: 4px;
    padding: 2px 8px;
    cursor: pointer;
    font-size: 0.75rem;
}
.current-track-skip-btn:hover {
    border-color: #f59e0b;
    color: #f59e0b;
}
```

- [ ] **Step 2: Rewrite updateCurrentFromSSE function**

Replace `updateCurrentFromSSE` (lines 995-1043) with a DOM-safe implementation. All dynamic text uses `textContent`. The SVG placeholder is created via `createElementNS`. YouTube URLs go through `sanitizeUrl()`.

```javascript
function updateCurrentFromSSE(data) {
    const container = document.getElementById('currentDownload');

    if (!data.active) {
        if (lastWasActive) {
            updateHistory();
            lastWasActive = false;
        }
        const empty = document.createElement('div');
        empty.className = 'empty-state';
        const icon = document.createElement('i');
        icon.className = 'fa-solid fa-cloud-arrow-down';
        const p = document.createElement('p');
        p.textContent = 'No active download';
        empty.appendChild(icon);
        empty.appendChild(p);
        container.replaceChildren(empty);
        return;
    }
    lastWasActive = true;

    const tracks = data.tracks || [];
    const doneCount = tracks.filter(t =>
        t.status === 'done' || t.status === 'failed' || t.status === 'skipped'
    ).length;
    const overallPercent = tracks.length > 0
        ? Math.round((doneCount / tracks.length) * 100) : 0;

    const wrapper = document.createElement('div');
    wrapper.className = 'current-download';

    // Cover
    const safeCover = sanitizeUrl(data.cover_url);
    if (safeCover) {
        const img = document.createElement('img');
        img.className = 'current-cover';
        img.src = safeCover;
        img.alt = '';
        wrapper.appendChild(img);
    } else {
        const ph = document.createElement('div');
        ph.className = 'current-cover-placeholder';
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'white');
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', 'M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z');
        svg.appendChild(path);
        ph.appendChild(svg);
        wrapper.appendChild(ph);
    }

    // Body
    const body = document.createElement('div');
    body.className = 'current-body';

    // Album header
    const details = document.createElement('div');
    details.className = 'current-album-details';
    const h3 = document.createElement('h3');
    h3.textContent = data.album_title || 'Unknown Album';
    const pArtist = document.createElement('p');
    pArtist.textContent = data.artist_name || 'Unknown Artist';
    details.appendChild(h3);
    details.appendChild(pArtist);
    body.appendChild(details);

    // Overall progress
    const progContainer = document.createElement('div');
    progContainer.className = 'progress-bar-container';
    const progLabel = document.createElement('div');
    progLabel.className = 'progress-label';
    const spanLeft = document.createElement('span');
    spanLeft.textContent = 'Track ' + doneCount + '/' + tracks.length;
    const spanRight = document.createElement('span');
    spanRight.textContent = overallPercent + '%';
    progLabel.appendChild(spanLeft);
    progLabel.appendChild(spanRight);
    const progBar = document.createElement('div');
    progBar.className = 'progress-bar';
    const progFill = document.createElement('div');
    progFill.className = 'progress-fill';
    progFill.style.width = overallPercent + '%';
    progBar.appendChild(progFill);
    progContainer.appendChild(progLabel);
    progContainer.appendChild(progBar);
    body.appendChild(progContainer);

    // Stop All button
    const actions = document.createElement('div');
    actions.className = 'current-actions';
    const stopBtn = document.createElement('button');
    stopBtn.className = 'current-stop-btn';
    stopBtn.onclick = stopDownload;
    const stopIcon = document.createElement('i');
    stopIcon.className = 'fa-solid fa-stop';
    stopBtn.appendChild(stopIcon);
    stopBtn.appendChild(document.createTextNode(' Stop All'));
    actions.appendChild(stopBtn);
    body.appendChild(actions);

    // Track grid
    const grid = document.createElement('div');
    grid.className = 'current-track-grid';

    tracks.forEach(function(track, i) {
        const row = document.createElement('div');
        row.className = 'current-track-row';
        if (i === data.current_track_index && (track.status === 'downloading' || track.status === 'searching' || track.status === 'tagging')) {
            row.classList.add('active');
        } else {
            row.classList.add(track.status);
        }

        // Track number
        const numEl = document.createElement('div');
        numEl.className = 'current-track-num';
        numEl.textContent = track.track_number;
        row.appendChild(numEl);

        // Track info (title + progress bar if downloading)
        const info = document.createElement('div');
        info.className = 'current-track-info';
        const titleEl = document.createElement('div');
        titleEl.className = 'current-track-title';
        titleEl.textContent = track.track_title;
        info.appendChild(titleEl);
        if (track.status === 'downloading' && track.progress_percent) {
            const progTrack = document.createElement('div');
            progTrack.className = 'current-track-progress';
            const progTrackFill = document.createElement('div');
            progTrackFill.className = 'current-track-progress-fill';
            progTrackFill.style.width = track.progress_percent;
            progTrack.appendChild(progTrackFill);
            info.appendChild(progTrack);
        }
        row.appendChild(info);

        // YouTube source
        const ytEl = document.createElement('div');
        ytEl.className = 'current-track-yt';
        if (track.youtube_url && track.youtube_title) {
            const safeYt = sanitizeUrl(track.youtube_url);
            if (safeYt) {
                const a = document.createElement('a');
                a.href = safeYt;
                a.target = '_blank';
                a.rel = 'noopener';
                a.textContent = track.youtube_title;
                ytEl.appendChild(a);
            } else {
                ytEl.textContent = track.youtube_title;
            }
        } else if (track.status === 'downloading') {
            ytEl.textContent = track.progress_speed || '';
        }
        row.appendChild(ytEl);

        // Status indicator
        const statusEl = document.createElement('div');
        statusEl.className = 'current-track-status';
        const statusSpan = document.createElement('span');
        statusSpan.className = track.status;
        if (track.status === 'done') {
            statusSpan.textContent = '\u2713 Done';
        } else if (track.status === 'failed') {
            statusSpan.textContent = '\u2717 Failed';
            statusSpan.title = track.error_message || '';
        } else if (track.status === 'skipped') {
            statusSpan.textContent = '\u21b7 Skipped';
        } else if (track.status === 'searching') {
            statusSpan.textContent = 'Searching...';
        } else if (track.status === 'tagging') {
            statusSpan.textContent = 'Tagging...';
        } else if (track.status === 'downloading') {
            statusSpan.textContent = track.progress_percent || 'Downloading';
        } else {
            statusSpan.textContent = 'Pending';
        }
        statusEl.appendChild(statusSpan);
        row.appendChild(statusEl);

        // Skip button
        const skipCell = document.createElement('div');
        if (track.status === 'pending' || track.status === 'searching' || track.status === 'downloading') {
            const skipBtn = document.createElement('button');
            skipBtn.className = 'current-track-skip-btn';
            skipBtn.textContent = 'Skip';
            skipBtn.onclick = function() { skipTrack(i); };
            skipCell.appendChild(skipBtn);
        }
        row.appendChild(skipCell);

        grid.appendChild(row);
    });

    body.appendChild(grid);
    wrapper.appendChild(body);
    container.replaceChildren(wrapper);
}
```

- [ ] **Step 3: Add skipTrack JS function**

Add this function near `stopDownload`:

```javascript
function skipTrack(trackIndex) {
    fetch('/api/download/skip-track', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({track_index: trackIndex}),
    });
}
```

- [ ] **Step 4: Run app locally and verify Current Download renders**

Run: `docker compose up -d --build`
Navigate to http://localhost:5000/downloads. Verify no JS errors in console. If no active download, verify empty state renders.

- [ ] **Step 5: Commit**

```bash
git add templates/downloads.html
git commit -m "feat: rewrite Current Download section with per-track grid"
```

---

### Task 8: Rewrite Download Queue section (updateQueueFromSSE)

**Files:**
- Modify: `templates/downloads.html:1045-1083` (updateQueueFromSSE)

- [ ] **Step 1: Add CSS styles for expandable queue**

Add to the `<style>` section:

```css
.queue-item-header {
    display: flex;
    align-items: center;
    gap: 12px;
    cursor: pointer;
}
.queue-expand-icon {
    transition: transform 0.2s;
    color: var(--text-secondary, #888);
    font-size: 0.8rem;
}
.queue-expand-icon.expanded {
    transform: rotate(90deg);
}
.queue-track-count {
    background: rgba(255,255,255,0.1);
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 0.75rem;
    color: var(--text-secondary, #888);
}
.queue-track-list {
    padding: 8px 0 8px 52px;
    display: flex;
    flex-direction: column;
    gap: 2px;
}
.queue-track-list-item {
    display: flex;
    gap: 8px;
    font-size: 0.8rem;
    color: var(--text-secondary, #888);
}
.queue-track-list-item .num {
    min-width: 24px;
    text-align: right;
}
.queue-track-list-item.has-file {
    text-decoration: line-through;
    opacity: 0.5;
}
```

- [ ] **Step 2: Add expandedQueueIds Set and track cache**

Add near the `expandedAlbumIds` declaration (around line 1383):

```javascript
const expandedQueueIds = new Set();
const queueTrackCache = {};
```

- [ ] **Step 3: Add toggleQueueTracks and fetchQueueTracks functions**

```javascript
function toggleQueueTracks(albumId) {
    if (expandedQueueIds.has(albumId)) {
        expandedQueueIds.delete(albumId);
    } else {
        expandedQueueIds.add(albumId);
        if (!queueTrackCache[albumId]) {
            fetchQueueTracks(albumId);
        }
    }
}

function fetchQueueTracks(albumId) {
    fetch('/api/download/queue/' + albumId + '/tracks')
        .then(function(r) { return r.json(); })
        .then(function(tracks) {
            queueTrackCache[albumId] = tracks;
            // Re-render will pick up cached tracks on next SSE
        });
}
```

- [ ] **Step 4: Rewrite updateQueueFromSSE**

Replace `updateQueueFromSSE` (lines 1045-1083) with DOM-safe implementation:

```javascript
function updateQueueFromSSE(queue, statusData) {
    const container = document.getElementById('downloadQueue');

    const totalInQueue = queue.length + (statusData.active ? 1 : 0);
    const badge = document.getElementById('queueBadge');
    if (totalInQueue > 0) {
        badge.textContent = totalInQueue;
        badge.classList.add('active');
    } else {
        badge.classList.remove('active');
    }

    if (queue.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'empty-state';
        const icon = document.createElement('i');
        icon.className = 'fa-solid fa-list-check';
        const p = document.createElement('p');
        p.textContent = 'Queue is empty';
        empty.appendChild(icon);
        empty.appendChild(p);
        container.replaceChildren(empty);
        return;
    }

    // Auto-expand position 1
    if (queue.length > 0 && !expandedQueueIds.has(queue[0].id)) {
        expandedQueueIds.add(queue[0].id);
        if (!queueTrackCache[queue[0].id]) {
            fetchQueueTracks(queue[0].id);
        }
    }

    const frag = document.createDocumentFragment();

    queue.forEach(function(item, index) {
        const qItem = document.createElement('div');
        qItem.className = 'queue-item';

        // Header row (clickable)
        const header = document.createElement('div');
        header.className = 'queue-item-header';
        header.onclick = function() {
            toggleQueueTracks(item.id);
        };

        // Expand chevron
        const chevron = document.createElement('i');
        chevron.className = 'fa-solid fa-chevron-right queue-expand-icon';
        if (expandedQueueIds.has(item.id)) {
            chevron.classList.add('expanded');
        }
        header.appendChild(chevron);

        // Position
        const pos = document.createElement('div');
        pos.className = 'queue-position';
        pos.textContent = index + 1;
        header.appendChild(pos);

        // Cover
        const safeCover = sanitizeUrl(item.cover_url);
        if (safeCover) {
            const img = document.createElement('img');
            img.className = 'queue-album-cover';
            img.src = safeCover;
            img.alt = '';
            header.appendChild(img);
        } else {
            const ph = document.createElement('div');
            ph.className = 'queue-cover-placeholder';
            const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svg.setAttribute('viewBox', '0 0 24 24');
            svg.setAttribute('fill', 'white');
            const svgPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            svgPath.setAttribute('d', 'M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z');
            svg.appendChild(svgPath);
            ph.appendChild(svg);
            header.appendChild(ph);
        }

        // Album info
        const info = document.createElement('div');
        info.className = 'queue-album-info';
        const titleEl = document.createElement('div');
        titleEl.className = 'queue-album-title';
        titleEl.textContent = item.title;
        const artistEl = document.createElement('div');
        artistEl.className = 'queue-album-artist';
        artistEl.textContent = item.artist;
        info.appendChild(titleEl);
        info.appendChild(artistEl);
        header.appendChild(info);

        // Track count badge
        if (item.track_count) {
            const countBadge = document.createElement('span');
            countBadge.className = 'queue-track-count';
            countBadge.textContent = item.track_count + ' tracks';
            header.appendChild(countBadge);
        }

        // Remove button (stop propagation so click doesn't toggle)
        const removeBtn = document.createElement('button');
        removeBtn.textContent = 'Remove';
        removeBtn.onclick = function(e) {
            e.stopPropagation();
            removeFromQueue(item.id);
        };
        const actionsDiv = document.createElement('div');
        actionsDiv.className = 'queue-actions';
        actionsDiv.appendChild(removeBtn);
        header.appendChild(actionsDiv);

        qItem.appendChild(header);

        // Track list (if expanded)
        if (expandedQueueIds.has(item.id)) {
            const trackList = document.createElement('div');
            trackList.className = 'queue-track-list';
            var cachedTracks = queueTrackCache[item.id];
            if (cachedTracks && cachedTracks.length > 0) {
                cachedTracks.forEach(function(t) {
                    const tRow = document.createElement('div');
                    tRow.className = 'queue-track-list-item';
                    if (t.has_file) tRow.classList.add('has-file');
                    const numSpan = document.createElement('span');
                    numSpan.className = 'num';
                    numSpan.textContent = t.track_number;
                    const titleSpan = document.createElement('span');
                    titleSpan.textContent = t.title;
                    tRow.appendChild(numSpan);
                    tRow.appendChild(titleSpan);
                    trackList.appendChild(tRow);
                });
            } else {
                const loading = document.createElement('div');
                loading.style.fontSize = '0.8rem';
                loading.style.opacity = '0.5';
                loading.textContent = 'Loading tracks...';
                trackList.appendChild(loading);
            }
            qItem.appendChild(trackList);
        }

        frag.appendChild(qItem);
    });

    container.replaceChildren(frag);
}
```

- [ ] **Step 5: Run app locally and verify queue renders**

Run: `docker compose up -d --build`
Navigate to http://localhost:5000/downloads. Verify queue renders with expand/collapse.

- [ ] **Step 6: Commit**

```bash
git add templates/downloads.html
git commit -m "feat: rewrite Download Queue with expandable track lists"
```

---

### Task 9: Update TESTING.md with manual test cases

**Files:**
- Modify: `TESTING.md`

- [ ] **Step 1: Add track-level UI test cases**

Add a new section to `TESTING.md` under "UI Interaction Tests":

```markdown
## Track-Level Downloads UI Tests

Run after changes to Current Download or Download Queue sections:

- [ ] **Current Download**: Shows all tracks in album with per-track status during active download
- [ ] **Current Download**: Active track has highlighted background, progress bar, and speed
- [ ] **Current Download**: Done tracks show green checkmark and YouTube link
- [ ] **Current Download**: Pending tracks are dimmed (lower opacity)
- [ ] **Current Download**: Failed tracks show red background and error message on hover
- [ ] **Current Download**: Overall progress bar reflects (done + failed + skipped) / total
- [ ] **Skip active track**: Click Skip on downloading track - stops within a few seconds, next track starts
- [ ] **Skip pending track**: Click Skip on pending track - marked as skipped, never downloads
- [ ] **Skip searching track**: Click Skip while searching YouTube - search aborts, moves to next
- [ ] **Stop All**: Stops current track and clears entire queue (existing behavior preserved)
- [ ] **Download Queue**: Position 1 auto-expanded showing track list
- [ ] **Download Queue**: Other positions collapsed with expand chevron
- [ ] **Download Queue**: Click chevron to expand/collapse track list
- [ ] **Download Queue**: Track count badge shown on each queue item
- [ ] **Download Queue**: Tracks with existing files shown with strikethrough
- [ ] **Download Queue**: Expansion state preserved across SSE updates (1-second rebuilds)
- [ ] **Download Queue**: Remove button still works (doesn't toggle expansion)
```

- [ ] **Step 2: Commit**

```bash
git add TESTING.md
git commit -m "docs: add manual test cases for track-level downloads UI"
```

---

### Task 10: Final integration verification

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `source .venv/bin/activate && python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 2: Build and run Docker**

Run: `docker compose up -d --build`

- [ ] **Step 3: Verify Current Download with real download**

Queue an album from the missing albums page. Navigate to /downloads. Verify:
- Track grid appears with all tracks
- Active track shows progress bar and speed
- Status transitions: pending -> searching -> downloading -> tagging -> done
- YouTube URL appears after search completes

- [ ] **Step 4: Verify Skip functionality**

Queue a multi-track album. While downloading:
- Click Skip on the active track — verify it stops within a few seconds and next track starts
- Click Skip on a pending track — verify it's marked skipped when its turn comes

- [ ] **Step 5: Verify Download Queue**

Queue multiple albums. Navigate to /downloads. Verify:
- First queue item is auto-expanded showing tracks
- Other items have expand chevron
- Click to expand shows track list
- Track count badges shown
- Remove button works without toggling expansion

- [ ] **Step 6: Verify Stop All**

Start a download and click Stop All. Verify:
- Current track stops
- Queue is cleared
- No regression from existing behavior

- [ ] **Step 7: Final commit (if any fixes needed)**

```bash
git add -A
git commit -m "fix: integration fixes for track-level downloads UI"
```
