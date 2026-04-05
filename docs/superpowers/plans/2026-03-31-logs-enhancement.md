# Logs Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the Logs page to show per-track failure detail with per-candidate AcoustID verification data in a compact row layout.

**Architecture:** New `candidate_attempts` table stores per-candidate outcomes during download verification. `download_logs` gains track-level columns. Processing layer buffers candidate outcomes per-thread and flushes after track completes. API enriches `track_failure` log entries with candidate data. Frontend switches from card layout to compact rows with always-visible candidate sub-rows.

**Tech Stack:** Python/Flask, SQLite, vanilla JS

**Spec:** `docs/superpowers/specs/2026-03-31-logs-enhancement-design.md`

---

### Task 1: CandidateOutcome Enum and Model Functions

**Files:**
- Modify: `models.py:1-18` (imports and constants section)
- Modify: `models.py:56-86` (`add_track_download`)
- Modify: `models.py:300-322` (`add_log`)
- Test: `tests/test_models.py`

- [ ] **Step 1: Write failing tests for CandidateOutcome enum**

```python
# In tests/test_models.py, add at the top after existing imports:

from models import CandidateOutcome


class TestCandidateOutcome:
    def test_enum_values(self):
        assert CandidateOutcome.VERIFIED.value == "verified"
        assert CandidateOutcome.MISMATCH.value == "mismatch"
        assert CandidateOutcome.UNVERIFIED.value == "unverified"
        assert CandidateOutcome.DOWNLOAD_FAILED.value == "download_failed"
        assert CandidateOutcome.ACCEPTED_NO_VERIFY.value == "accepted_no_verify"
        assert CandidateOutcome.ACCEPTED_UNVERIFIED_FALLBACK.value == "accepted_unverified_fallback"

    def test_enum_is_str(self):
        assert isinstance(CandidateOutcome.VERIFIED, str)
        assert CandidateOutcome.MISMATCH == "mismatch"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_models.py::TestCandidateOutcome -v`
Expected: ImportError — `CandidateOutcome` not found

- [ ] **Step 3: Implement CandidateOutcome enum**

In `models.py`, add after the existing imports (line 12):

```python
from enum import Enum


class CandidateOutcome(str, Enum):
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    UNVERIFIED = "unverified"
    DOWNLOAD_FAILED = "download_failed"
    ACCEPTED_NO_VERIFY = "accepted_no_verify"
    ACCEPTED_UNVERIFIED_FALLBACK = "accepted_unverified_fallback"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_models.py::TestCandidateOutcome -v`
Expected: PASS

- [ ] **Step 5: Write failing test for add_track_download returning ID**

```python
# In tests/test_models.py

def test_add_track_download_returns_id():
    row_id = models.add_track_download(
        album_id=1, album_title="A", artist_name="A",
        track_title="T1", track_number=1, success=True,
        error_message="", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0, album_path="",
        lidarr_album_path="", cover_url="",
    )
    assert isinstance(row_id, int)
    assert row_id > 0
```

- [ ] **Step 6: Run test to verify it fails**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_models.py::test_add_track_download_returns_id -v`
Expected: FAIL — `add_track_download` returns None

- [ ] **Step 7: Update add_track_download to return lastrowid**

In `models.py`, change `add_track_download` (line 65):

Replace:
```python
    conn.execute(
```
with:
```python
    cursor = conn.execute(
```

And at the end of the function (line 86), replace:
```python
    conn.commit()
```
with:
```python
    conn.commit()
    return cursor.lastrowid
```

- [ ] **Step 8: Run test to verify it passes**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_models.py::test_add_track_download_returns_id -v`
Expected: PASS

- [ ] **Step 9: Write failing tests for updated add_log with new fields**

```python
# In tests/test_models.py

def test_add_log_with_track_fields():
    log_id = models.add_log(
        log_type="track_failure",
        album_id=1,
        album_title="Album1",
        artist_name="Artist1",
        details="AcoustID verification failed",
        track_title="Track Name",
        track_number=3,
        track_download_id=42,
    )
    logs = models.get_logs()
    item = logs["items"][0]
    assert item["type"] == "track_failure"
    assert item["track_title"] == "Track Name"
    assert item["track_number"] == 3
    assert item["track_download_id"] == 42
```

- [ ] **Step 10: Run test to verify it fails**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_models.py::test_add_log_with_track_fields -v`
Expected: FAIL — `add_log` doesn't accept `track_title` or `track_download_id`

- [ ] **Step 11: Implement — will be done after Task 2 (migration adds columns first)**

This test will remain failing until Task 2 completes the migration. Move to Task 2.

- [ ] **Step 12: Commit**

```bash
git add models.py tests/test_models.py
git commit -m "feat(models): add CandidateOutcome enum and return ID from add_track_download"
```

---

### Task 2: V5 Database Migration

**Files:**
- Modify: `db.py:1` (SCHEMA_VERSION constant, line 12)
- Modify: `db.py:278-306` (migrations dict)
- Test: `tests/test_db.py`

- [ ] **Step 1: Write failing tests for V5 migration**

```python
# In tests/test_db.py, add:

def test_v5_migration_creates_candidate_attempts(temp_db):
    init_db()
    conn = sqlite3.connect(temp_db)
    tables = [
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    assert "candidate_attempts" in tables

    # Check columns exist
    cols = [
        row[1] for row in conn.execute(
            "PRAGMA table_info(candidate_attempts)"
        ).fetchall()
    ]
    assert "track_download_id" in cols
    assert "youtube_url" in cols
    assert "outcome" in cols
    assert "acoustid_matched_id" in cols
    assert "acoustid_score" in cols
    assert "expected_recording_id" in cols
    conn.close()


def test_v5_migration_adds_download_logs_columns(temp_db):
    init_db()
    conn = sqlite3.connect(temp_db)
    cols = [
        row[1] for row in conn.execute(
            "PRAGMA table_info(download_logs)"
        ).fetchall()
    ]
    assert "track_title" in cols
    assert "track_number" in cols
    assert "track_download_id" in cols
    conn.close()


def test_v5_migration_preserves_existing_logs(temp_db):
    init_db()
    conn = sqlite3.connect(temp_db)
    # Insert a log entry (pre-V5 style, no track fields)
    conn.execute(
        "INSERT INTO download_logs"
        " (id, type, album_id, album_title, artist_name,"
        "  timestamp, details, total_file_size)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("test1", "download_success", 1, "A", "A", 1.0, "ok", 0),
    )
    conn.commit()
    row = conn.execute(
        "SELECT track_title, track_number, track_download_id"
        " FROM download_logs WHERE id = 'test1'"
    ).fetchone()
    assert row[0] == ""  # track_title default
    assert row[1] is None  # track_number default
    assert row[2] is None  # track_download_id default
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_db.py::test_v5_migration_creates_candidate_attempts tests/test_db.py::test_v5_migration_adds_download_logs_columns tests/test_db.py::test_v5_migration_preserves_existing_logs -v`
Expected: FAIL — `candidate_attempts` table doesn't exist, columns missing

- [ ] **Step 3: Implement V5 migration**

In `db.py`, change `SCHEMA_VERSION` (line 12):
```python
SCHEMA_VERSION = 5
```

Add migration function before `_run_migrations`:
```python
def _migrate_v4_to_v5(conn):
    """Add candidate_attempts table and track fields to download_logs."""
    conn.execute("""
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
        )
    """)
    conn.execute(
        "CREATE INDEX idx_ca_track_dl_id"
        " ON candidate_attempts(track_download_id)"
    )
    conn.execute(
        "ALTER TABLE download_logs"
        " ADD COLUMN track_title TEXT DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE download_logs"
        " ADD COLUMN track_number INTEGER DEFAULT NULL"
    )
    conn.execute(
        "ALTER TABLE download_logs"
        " ADD COLUMN track_download_id INTEGER DEFAULT NULL"
    )
```

Register in `_run_migrations` (line 280-284), add to migrations dict:
```python
    migrations = {
        2: _migrate_v1_to_v2,
        3: _migrate_v2_to_v3,
        4: _migrate_v3_to_v4,
        5: _migrate_v4_to_v5,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_db.py -v`
Expected: ALL PASS

- [ ] **Step 5: Update test_init_db_sets_schema_version assertion**

In `tests/test_db.py`, update line 38:
```python
    assert row[0] == 5
```

And update line 48 (idempotent test):
```python
    # V1 insert + V2 + V3 + V4 + V5 migrations = 5 rows
    assert rows[0] == 5
```

- [ ] **Step 6: Run full db test suite**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_db.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat(db): add V5 migration with candidate_attempts table and log track fields"
```

---

### Task 3: Model Functions for Candidate Attempts and Updated add_log

**Files:**
- Modify: `models.py:300-322` (`add_log`)
- Modify: `models.py:325-344` (`get_logs`)
- Test: `tests/test_models.py`

- [ ] **Step 1: Complete add_log with new fields (test from Task 1 Step 9)**

In `models.py`, update `add_log` signature and body:

```python
def add_log(
    log_type, album_id, album_title, artist_name,
    details="", total_file_size=0, track_number=None,
    track_title="", track_download_id=None,
):
    """Create a download log entry. Returns the generated log ID."""
    conn = db.get_db()
    ts = int(time.time() * 1000)
    if track_number is not None:
        log_id = f"{ts}_{album_id}_{track_number}"
    else:
        log_id = f"{ts}_{album_id}"
    conn.execute(
        """INSERT INTO download_logs
           (id, type, album_id, album_title, artist_name, timestamp,
            details, total_file_size, track_title, track_number,
            track_download_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            log_id, log_type, album_id, album_title, artist_name,
            time.time(), details, total_file_size,
            track_title, track_number, track_download_id,
        ),
    )
    conn.commit()
    return log_id
```

- [ ] **Step 2: Run the add_log test from Task 1**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_models.py::test_add_log_with_track_fields -v`
Expected: PASS

- [ ] **Step 3: Write failing tests for flush_candidate_attempts**

```python
# In tests/test_models.py

from models import CandidateOutcome


class TestCandidateAttempts:
    def _add_track(self):
        return models.add_track_download(
            album_id=1, album_title="A", artist_name="A",
            track_title="T1", track_number=1, success=False,
            error_message="failed", youtube_url="", youtube_title="",
            match_score=0.0, duration_seconds=0, album_path="",
            lidarr_album_path="", cover_url="",
        )

    def test_flush_candidate_attempts_inserts_rows(self):
        track_id = self._add_track()
        attempts = [
            {
                "youtube_url": "https://youtube.com/watch?v=abc",
                "youtube_title": "Test Video",
                "match_score": 0.87,
                "duration_seconds": 222,
                "outcome": CandidateOutcome.MISMATCH,
                "acoustid_matched_id": "rec-wrong",
                "acoustid_matched_title": "Wrong Song",
                "acoustid_score": 0.92,
                "expected_recording_id": "rec-expected",
                "error_message": "",
                "timestamp": 1000.0,
            },
            {
                "youtube_url": "https://youtube.com/watch?v=xyz",
                "youtube_title": "Another Video",
                "match_score": 0.81,
                "duration_seconds": 218,
                "outcome": CandidateOutcome.UNVERIFIED,
                "acoustid_matched_id": "",
                "acoustid_matched_title": "",
                "acoustid_score": 0.0,
                "expected_recording_id": "rec-expected",
                "error_message": "",
                "timestamp": 1001.0,
            },
        ]
        models.flush_candidate_attempts(track_id, attempts)
        rows = models.get_candidate_attempts(track_id)
        assert len(rows) == 2
        assert rows[0]["outcome"] == "mismatch"
        assert rows[0]["acoustid_matched_title"] == "Wrong Song"
        assert rows[1]["outcome"] == "unverified"

    def test_flush_empty_list_is_noop(self):
        track_id = self._add_track()
        models.flush_candidate_attempts(track_id, [])
        rows = models.get_candidate_attempts(track_id)
        assert len(rows) == 0

    def test_get_candidate_attempts_ordered_by_timestamp(self):
        track_id = self._add_track()
        attempts = [
            {
                "youtube_url": "url1", "youtube_title": "V1",
                "match_score": 0.5, "duration_seconds": 100,
                "outcome": CandidateOutcome.DOWNLOAD_FAILED,
                "acoustid_matched_id": "", "acoustid_matched_title": "",
                "acoustid_score": 0.0, "expected_recording_id": "",
                "error_message": "403 Forbidden", "timestamp": 1002.0,
            },
            {
                "youtube_url": "url2", "youtube_title": "V2",
                "match_score": 0.9, "duration_seconds": 200,
                "outcome": CandidateOutcome.MISMATCH,
                "acoustid_matched_id": "x", "acoustid_matched_title": "X",
                "acoustid_score": 0.8, "expected_recording_id": "y",
                "error_message": "", "timestamp": 1001.0,
            },
        ]
        models.flush_candidate_attempts(track_id, attempts)
        rows = models.get_candidate_attempts(track_id)
        assert rows[0]["timestamp"] == 1001.0
        assert rows[1]["timestamp"] == 1002.0
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_models.py::TestCandidateAttempts -v`
Expected: FAIL — `flush_candidate_attempts` and `get_candidate_attempts` don't exist

- [ ] **Step 5: Implement flush_candidate_attempts and get_candidate_attempts**

In `models.py`, add after the `get_logs_db_size` function (after line 370):

```python
# --- Candidate Attempts ---


def flush_candidate_attempts(track_download_id, attempts):
    """Bulk insert candidate attempts for a track download."""
    if not attempts:
        return
    conn = db.get_db()
    conn.executemany(
        """INSERT INTO candidate_attempts
           (track_download_id, youtube_url, youtube_title,
            match_score, duration_seconds, outcome,
            acoustid_matched_id, acoustid_matched_title,
            acoustid_score, expected_recording_id,
            error_message, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                track_download_id,
                a["youtube_url"], a["youtube_title"],
                a["match_score"], a["duration_seconds"],
                str(a["outcome"]),
                a.get("acoustid_matched_id", ""),
                a.get("acoustid_matched_title", ""),
                a.get("acoustid_score", 0.0),
                a.get("expected_recording_id", ""),
                a.get("error_message", ""),
                a["timestamp"],
            )
            for a in attempts
        ],
    )
    conn.commit()


def get_candidate_attempts(track_download_id):
    """Return candidate attempts for a track, ordered by timestamp."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT * FROM candidate_attempts"
        " WHERE track_download_id = ?"
        " ORDER BY timestamp ASC",
        (track_download_id,),
    ).fetchall()
    return [dict(row) for row in rows]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_models.py::TestCandidateAttempts tests/test_models.py::test_add_log_with_track_fields -v`
Expected: ALL PASS

- [ ] **Step 7: Run full model test suite for regressions**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_models.py -v`
Expected: ALL PASS

- [ ] **Step 8: Commit**

```bash
git add models.py tests/test_models.py
git commit -m "feat(models): add candidate attempt functions and track fields on add_log"
```

---

### Task 4: Capture Candidate Outcomes in Processing

**Files:**
- Modify: `processing.py:531-562` (`_record_track_failure`)
- Modify: `processing.py:460-528` (`_accept_track_file`)
- Modify: `processing.py:600-862` (`_process_single_track` inside `_download_tracks`)
- Modify: `processing.py:882-1002` (`_handle_post_download`)
- Test: `tests/test_processing.py`

- [ ] **Step 1: Write failing test for candidate attempt capture on mismatch**

Read the existing test patterns in `tests/test_processing.py` first. Then add:

```python
# In tests/test_processing.py

import models
from models import CandidateOutcome


class TestCandidateAttemptCapture:
    """Verify that _record_track_failure flushes candidate attempts."""

    def test_record_track_failure_flushes_attempts(self, temp_db):
        import threading
        from processing import _record_track_failure

        track_state = {"status": "", "error_message": ""}
        failed_tracks = []
        lock = threading.Lock()
        album_ctx = {
            "album_id": 1, "album_title": "A",
            "artist_name": "Ar", "lidarr_album_path": "/m",
            "cover_url": "",
        }
        attempts = [
            {
                "youtube_url": "https://youtube.com/watch?v=x",
                "youtube_title": "V1",
                "match_score": 0.8, "duration_seconds": 200,
                "outcome": CandidateOutcome.MISMATCH,
                "acoustid_matched_id": "wrong",
                "acoustid_matched_title": "Wrong",
                "acoustid_score": 0.9,
                "expected_recording_id": "expected",
                "error_message": "",
                "timestamp": 1000.0,
            },
        ]
        _record_track_failure(
            "AcoustID failed", track_state, "Track1", 1,
            album_path="/dl", album_ctx=album_ctx,
            failed_tracks=failed_tracks,
            _results_lock=lock,
            candidate_attempts=attempts,
        )
        assert failed_tracks[0]["track_download_id"] is not None
        rows = models.get_candidate_attempts(
            failed_tracks[0]["track_download_id"],
        )
        assert len(rows) == 1
        assert rows[0]["outcome"] == "mismatch"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_processing.py::TestCandidateAttemptCapture -v`
Expected: FAIL — `_record_track_failure` doesn't accept `candidate_attempts`

- [ ] **Step 3: Update _record_track_failure to accept and flush candidate attempts**

In `processing.py`, update `_record_track_failure` (line 531):

```python
def _record_track_failure(
    fail_reason, track_state, track_title, track_num,
    *, album_path, album_ctx, failed_tracks, _results_lock,
    candidate_attempts=None,
):
    """Record a track failure in state, failed_tracks list, and DB."""
    track_state["status"] = "failed"
    track_state["error_message"] = fail_reason
    track_download_id = None
    try:
        track_download_id = models.add_track_download(
            album_id=album_ctx["album_id"],
            album_title=album_ctx["album_title"],
            artist_name=album_ctx["artist_name"],
            track_title=track_title,
            track_number=track_num, success=False,
            error_message=fail_reason,
            youtube_url="", youtube_title="",
            match_score=0.0, duration_seconds=0,
            album_path=album_path,
            lidarr_album_path=album_ctx["lidarr_album_path"],
            cover_url=album_ctx["cover_url"],
        )
        if candidate_attempts and track_download_id:
            models.flush_candidate_attempts(
                track_download_id, candidate_attempts,
            )
    except Exception:
        logger.error(
            "Failed to record track download for '%s' (album %d)",
            track_title, album_ctx["album_id"], exc_info=True,
        )
    with _results_lock:
        failed_tracks.append({
            "title": track_title,
            "reason": fail_reason,
            "track_num": track_num,
            "track_download_id": track_download_id,
        })
```

- [ ] **Step 4: Run test to verify it passes**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_processing.py::TestCandidateAttemptCapture -v`
Expected: PASS

- [ ] **Step 5: Update _accept_track_file to flush candidate attempts and return track_download_id**

In `processing.py`, update `_accept_track_file` function signature (around line 456) to accept `candidate_attempts=None`. After the `models.add_track_download()` call (line 495), capture the return value and flush:

```python
    try:
        track_download_id = models.add_track_download(
            # ... existing params ...
        )
        if candidate_attempts and track_download_id:
            models.flush_candidate_attempts(
                track_download_id, candidate_attempts,
            )
    except Exception:
        logger.error(
            "Failed to record track download for '%s' (album %d)",
            track_title, album_ctx["album_id"], exc_info=True,
        )
```

- [ ] **Step 6: Buffer candidate attempts in _process_single_track**

In `_process_single_track` (starts at line 600), add a thread-local buffer. Place it right after `best_unverified_candidate = None` (line 660), before the candidate `for` loop starts at line 664. This buffer is local to each thread — no shared state:

```python
        all_unverified = True
        best_unverified_candidate = None
        candidate_attempts_buf = []  # <-- add here
        accepted = False
        any_downloaded = False
```

At each capture point in the candidate loop, append to buffer:

**Download failure** (after line 679, when `dl_out is None`):
```python
            if dl_out is None:
                candidate_attempts_buf.append({
                    "youtube_url": candidate.get("url", ""),
                    "youtube_title": candidate.get("title", ""),
                    "match_score": candidate.get("score", 0.0),
                    "duration_seconds": candidate.get("duration", 0),
                    "outcome": CandidateOutcome.DOWNLOAD_FAILED,
                    "acoustid_matched_id": "",
                    "acoustid_matched_title": "",
                    "acoustid_score": 0.0,
                    "expected_recording_id": expected_recording_id or "",
                    "error_message": track_state.get("error_message", ""),
                    "timestamp": time.time(),
                })
                if track_state["status"] == "skipped":
                    return
                continue
```

**AcoustID verified** (after line 714):
```python
                elif vresult["status"] == "verified":
                    fp_data = vresult["fp_data"]
                    candidate_attempts_buf.append({
                        "youtube_url": candidate.get("url", ""),
                        "youtube_title": candidate.get("title", ""),
                        "match_score": candidate.get("score", 0.0),
                        "duration_seconds": candidate.get("duration", 0),
                        "outcome": CandidateOutcome.VERIFIED,
                        "acoustid_matched_id": fp_data.get("acoustid_recording_id", ""),
                        "acoustid_matched_title": fp_data.get("acoustid_recording_title", ""),
                        "acoustid_score": fp_data.get("acoustid_score", 0.0),
                        "expected_recording_id": expected_recording_id or "",
                        "error_message": "",
                        "timestamp": time.time(),
                    })
```

**AcoustID mismatch** (after line 716):
```python
                elif vresult["status"] == "mismatch":
                    all_unverified = False
                    candidate_attempts_buf.append({
                        "youtube_url": candidate.get("url", ""),
                        "youtube_title": candidate.get("title", ""),
                        "match_score": candidate.get("score", 0.0),
                        "duration_seconds": candidate.get("duration", 0),
                        "outcome": CandidateOutcome.MISMATCH,
                        "acoustid_matched_id": vresult.get("matched_id", ""),
                        "acoustid_matched_title": vresult["fp_data"].get("acoustid_recording_title", ""),
                        "acoustid_score": vresult["fp_data"].get("acoustid_score", 0.0),
                        "expected_recording_id": expected_recording_id or "",
                        "error_message": "",
                        "timestamp": time.time(),
                    })
                    # ... existing mismatch handling continues ...
```

**AcoustID unverified** (after line 757):
```python
                elif vresult["status"] == "unverified":
                    candidate_attempts_buf.append({
                        "youtube_url": candidate.get("url", ""),
                        "youtube_title": candidate.get("title", ""),
                        "match_score": candidate.get("score", 0.0),
                        "duration_seconds": candidate.get("duration", 0),
                        "outcome": CandidateOutcome.UNVERIFIED,
                        "acoustid_matched_id": "",
                        "acoustid_matched_title": "",
                        "acoustid_score": 0.0,
                        "expected_recording_id": expected_recording_id or "",
                        "error_message": "",
                        "timestamp": time.time(),
                    })
                    # ... existing unverified handling continues ...
```

**No verification** (in the else branch, after line 767):
```python
            else:
                candidate_attempts_buf.append({
                    "youtube_url": candidate.get("url", ""),
                    "youtube_title": candidate.get("title", ""),
                    "match_score": candidate.get("score", 0.0),
                    "duration_seconds": candidate.get("duration", 0),
                    "outcome": CandidateOutcome.ACCEPTED_NO_VERIFY,
                    "acoustid_matched_id": fp_data.get("acoustid_recording_id", ""),
                    "acoustid_matched_title": fp_data.get("acoustid_recording_title", ""),
                    "acoustid_score": fp_data.get("acoustid_score", 0.0),
                    "expected_recording_id": expected_recording_id or "",
                    "error_message": "",
                    "timestamp": time.time(),
                })
                # ... existing fingerprinting continues ...
```

**Fallback candidate** (around line 792-831): When fallback succeeds, append with `ACCEPTED_UNVERIFIED_FALLBACK`. When fallback download fails, append with `DOWNLOAD_FAILED`.

Pass `candidate_attempts=candidate_attempts_buf` to both `_accept_track_file` and `_record_track_failure` calls.

- [ ] **Step 7: Run full processing test suite**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_processing.py -v`
Expected: ALL PASS

- [ ] **Step 8: Commit**

```bash
git add processing.py tests/test_processing.py
git commit -m "feat(processing): capture per-candidate outcomes during verification loop"
```

---

### Task 5: Per-Track Failure Log Entries in _handle_post_download

**Files:**
- Modify: `processing.py:882-1002` (`_handle_post_download`)
- Test: `tests/test_processing.py`

- [ ] **Step 1: Write failing test for per-track log entries**

```python
# In tests/test_processing.py

def test_handle_post_download_creates_track_failure_logs(temp_db, monkeypatch):
    """After a partial download, each failed track gets its own log entry."""
    import processing
    import models

    monkeypatch.setattr(processing, "download_process", {
        "tracks": [
            {"status": "done"},
            {"status": "failed"},
            {"status": "failed"},
        ],
        "stop": False,
    })
    monkeypatch.setattr(processing, "send_notifications", lambda *a, **kw: None)

    failed_tracks = [
        {"title": "Track2", "reason": "AcoustID failed", "track_num": 2, "track_download_id": 10},
        {"title": "Track3", "reason": "Download failed", "track_num": 3, "track_download_id": 11},
    ]
    processing._handle_post_download(
        failed_tracks, [None, None, None], 1, "Album", "Artist", 5000000,
    )
    logs = models.get_logs(log_type="track_failure")
    assert logs["total"] == 2
    titles = {item["track_title"] for item in logs["items"]}
    assert titles == {"Track2", "Track3"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_processing.py::test_handle_post_download_creates_track_failure_logs -v`
Expected: FAIL — no `track_failure` log entries created

- [ ] **Step 3: Add per-track log creation in _handle_post_download**

In `processing.py`, in `_handle_post_download`, after the album-level `models.add_log()` call for `album_error` (around line 931) and `partial_success` (around line 967), add a loop:

```python
        for ft in failed_tracks:
            models.add_log(
                log_type="track_failure",
                album_id=album_id,
                album_title=album_title,
                artist_name=artist_name,
                details=ft["reason"],
                track_title=ft["title"],
                track_number=ft["track_num"],
                track_download_id=ft.get("track_download_id"),
            )
```

Add this loop in both the `album_error` branch (all tracks failed) and the `partial_success` branch (some tracks failed). Place it right after the respective `models.add_log()` call.

- [ ] **Step 4: Run test to verify it passes**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_processing.py::test_handle_post_download_creates_track_failure_logs -v`
Expected: PASS

- [ ] **Step 5: Run full processing + models test suites**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_processing.py tests/test_models.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add processing.py tests/test_processing.py
git commit -m "feat(processing): create per-track failure log entries in _handle_post_download"
```

---

### Task 6: API Enrichment — Candidates on track_failure Logs

**Files:**
- Modify: `app.py:637-642` (`api_get_logs`)
- Modify: `models.py` (update `get_logs` or add enrichment helper)
- Test: `tests/test_routes.py`

- [ ] **Step 1: Write failing test for enriched API response**

```python
# In tests/test_routes.py

import models
from models import CandidateOutcome


class TestLogsEnrichment:
    def test_track_failure_log_includes_candidates(self, client):
        track_id = models.add_track_download(
            album_id=1, album_title="A", artist_name="A",
            track_title="T1", track_number=1, success=False,
            error_message="AcoustID failed", youtube_url="",
            youtube_title="", match_score=0.0, duration_seconds=0,
            album_path="", lidarr_album_path="", cover_url="",
        )
        models.flush_candidate_attempts(track_id, [
            {
                "youtube_url": "https://youtube.com/watch?v=a",
                "youtube_title": "Video A",
                "match_score": 0.87, "duration_seconds": 222,
                "outcome": CandidateOutcome.MISMATCH,
                "acoustid_matched_id": "wrong-id",
                "acoustid_matched_title": "Wrong Song",
                "acoustid_score": 0.92,
                "expected_recording_id": "expected-id",
                "error_message": "", "timestamp": 1000.0,
            },
        ])
        models.add_log(
            log_type="track_failure", album_id=1,
            album_title="A", artist_name="A",
            details="AcoustID failed", track_title="T1",
            track_number=1, track_download_id=track_id,
        )
        resp = client.get("/api/logs?type=track_failure")
        data = resp.get_json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["track_title"] == "T1"
        assert "candidates" in item
        assert len(item["candidates"]) == 1
        assert item["candidates"][0]["outcome"] == "mismatch"
        assert item["candidates"][0]["acoustid_matched_title"] == "Wrong Song"

    def test_track_failure_candidate_includes_ban_status(self, client):
        track_id = models.add_track_download(
            album_id=1, album_title="A", artist_name="A",
            track_title="T1", track_number=1, success=False,
            error_message="failed", youtube_url="",
            youtube_title="", match_score=0.0, duration_seconds=0,
            album_path="", lidarr_album_path="", cover_url="",
        )
        models.flush_candidate_attempts(track_id, [
            {
                "youtube_url": "https://youtube.com/watch?v=banned",
                "youtube_title": "Banned Vid",
                "match_score": 0.8, "duration_seconds": 200,
                "outcome": CandidateOutcome.MISMATCH,
                "acoustid_matched_id": "x",
                "acoustid_matched_title": "X",
                "acoustid_score": 0.9,
                "expected_recording_id": "y",
                "error_message": "", "timestamp": 1000.0,
            },
        ])
        models.add_banned_url(
            youtube_url="https://youtube.com/watch?v=banned",
            youtube_title="Banned Vid", album_id=1,
            album_title="A", artist_name="A",
            track_title="T1", track_number=1,
        )
        models.add_log(
            log_type="track_failure", album_id=1,
            album_title="A", artist_name="A",
            details="failed", track_title="T1",
            track_number=1, track_download_id=track_id,
        )
        resp = client.get("/api/logs?type=track_failure")
        data = resp.get_json()
        cand = data["items"][0]["candidates"][0]
        assert cand["is_banned"] is True
        assert isinstance(cand["ban_id"], int)

    def test_non_track_failure_logs_have_no_candidates(self, client):
        models.add_log(
            log_type="download_success", album_id=1,
            album_title="A", artist_name="A",
            details="ok",
        )
        resp = client.get("/api/logs")
        data = resp.get_json()
        for item in data["items"]:
            assert "candidates" not in item
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_routes.py::TestLogsEnrichment -v`
Expected: FAIL — `candidates` not in response

- [ ] **Step 3: Implement API enrichment**

In `app.py`, update `api_get_logs` (around line 637):

```python
@app.route("/api/logs", methods=["GET"])
def api_get_logs():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    log_type = request.args.get("type", None, type=str)
    result = models.get_logs(page, per_page, log_type=log_type)
    _enrich_track_failure_logs(result["items"])
    return jsonify(result)


def _enrich_track_failure_logs(items):
    """Attach candidate attempts and ban status to track_failure logs."""
    for item in items:
        if item.get("type") != "track_failure":
            continue
        td_id = item.get("track_download_id")
        if not td_id:
            item["candidates"] = []
            continue
        candidates = models.get_candidate_attempts(td_id)
        album_id = item.get("album_id")
        banned_lookup = {}
        try:
            banned = models.get_banned_urls_for_album(album_id)
            for b in banned:
                banned_lookup[b["youtube_url"]] = b["id"]
        except Exception:
            pass
        for c in candidates:
            url = c.get("youtube_url", "")
            c["is_banned"] = url in banned_lookup
            c["ban_id"] = banned_lookup.get(url)
        item["candidates"] = candidates
```

In `models.py`, add a helper to fetch banned URLs for an album:

```python
def get_banned_urls_for_album(album_id):
    """Return banned URL records for an album."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, youtube_url FROM banned_urls"
        " WHERE album_id = ?",
        (album_id,),
    ).fetchall()
    return [dict(row) for row in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_routes.py::TestLogsEnrichment -v`
Expected: ALL PASS

- [ ] **Step 5: Run full route test suite**

Run: `source .venv/bin/activate && python3 -m pytest tests/test_routes.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add app.py models.py tests/test_routes.py
git commit -m "feat(api): enrich track_failure logs with candidate attempts and ban status"
```

---

### Task 7: Frontend — Compact Row Layout and Candidate Sub-Rows

**Files:**
- Modify: `templates/logs.html` (CSS, JS rendering, filter options)

- [ ] **Step 1: Replace CSS card styles with compact row styles**

Replace the `.log-card` styles (lines 341-348) and related card styles with compact row styles:

```css
.log-row {
    display: flex;
    gap: 0.75rem;
    padding: 0.75rem 1rem;
    background: var(--surface);
    border-radius: 12px;
    align-items: center;
    border-left: 4px solid transparent;
    transition: all 0.3s ease;
    animation: slideIn 0.3s ease;
}
```

Keep the type-specific border colors but apply to `.log-row` instead of `.log-card`:

```css
.log-row.download_started { border-left-color: #3b82f6; }
.log-row.download_success { border-left-color: var(--primary); }
.log-row.partial_success { border-left-color: var(--warning); }
.log-row.import_partial { border-left-color: var(--warning); }
.log-row.import_success { border-left-color: var(--primary); }
.log-row.import_failed { border-left-color: var(--danger); }
.log-row.album_error { border-left-color: var(--danger); }
.log-row.manual_download { border-left-color: #8b5cf6; }
.log-row.track_failure { border-left-color: var(--danger); }
.log-row.url_banned { border-left-color: #f97316; }
```

Add candidate sub-row styles:

```css
.candidate-row {
    display: flex;
    gap: 0.5rem;
    padding: 0.4rem 1rem 0.4rem 2.5rem;
    font-size: 0.78rem;
    color: var(--text-dim);
    align-items: center;
    flex-wrap: wrap;
}

.candidate-outcome {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 600;
    flex-shrink: 0;
}

.candidate-outcome.mismatch {
    background: rgba(239, 68, 68, 0.1);
    color: var(--danger);
}

.candidate-outcome.unverified {
    background: rgba(245, 158, 11, 0.1);
    color: var(--warning);
}

.candidate-outcome.download_failed {
    background: rgba(161, 161, 170, 0.1);
    color: var(--text-dim);
}

.candidate-outcome.verified {
    background: rgba(16, 185, 129, 0.1);
    color: var(--primary);
}

.candidate-sep {
    color: var(--text-dim);
    opacity: 0.4;
}

.log-row-info {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
}

.log-row-title {
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.log-row-meta {
    color: var(--text-dim);
    font-size: 0.85rem;
    white-space: nowrap;
}

.log-row-actions {
    display: flex;
    gap: 0.5rem;
    flex-shrink: 0;
}
```

Remove old styles: `.log-card`, `.log-header`, `.log-info`, `.log-details`, `.log-details-title`, `.failed-tracks-list`, `.failed-track-reason`.

- [ ] **Step 2: Update renderLogCard to produce compact rows**

Replace the `renderLogCard` function with two renderers:

```javascript
function renderLogEntry(log) {
    if (log.type === 'track_failure') {
        return renderTrackFailureRow(log);
    }
    if (log.type === 'url_banned') {
        return renderBannedUrlRow(log);
    }
    return renderAlbumLogRow(log);
}

function renderAlbumLogRow(log) {
    const typeInfo = getLogTypeInfo(log.type);
    const sizeHtml = log.total_file_size
        ? ' <span class="log-file-size"><i class="fas fa-weight-hanging"></i> '
          + formatBytes(log.total_file_size) + '</span>'
        : '';
    return '<div class="log-row ' + escapeHtml(log.type)
        + '" id="log-' + escapeHtml(String(log.id)) + '">'
        + '<div class="status-icon"><i class="fas ' + typeInfo.icon + '"></i></div>'
        + '<div class="log-row-info">'
        + '<span class="log-row-title">' + escapeHtml(log.album_title) + '</span>'
        + '<span class="log-row-meta">by ' + escapeHtml(log.artist_name)
        + ' &bull; ' + escapeHtml(log.details || '')
        + sizeHtml
        + ' &bull; ' + formatTimestamp(log.timestamp) + '</span>'
        + '</div>'
        + '<div class="log-row-actions">'
        + '<button class="btn-icon" onclick="dismissLog(\'' + escapeHtml(String(log.id))
        + '\')" title="Dismiss"><i class="fas fa-times"></i></button>'
        + '</div></div>';
}

function renderTrackFailureRow(log) {
    var html = '<div class="log-entry-group">'
        + '<div class="log-row track_failure" id="log-' + escapeHtml(String(log.id)) + '">'
        + '<div class="status-icon error"><i class="fas fa-xmark"></i></div>'
        + '<div class="log-row-info">'
        + '<span class="log-row-title">&ldquo;' + escapeHtml(log.track_title || '') + '&rdquo;</span>'
        + '<span class="log-row-meta">'
        + escapeHtml(log.album_title) + ' &mdash; ' + escapeHtml(log.artist_name)
        + ' &bull; Track ' + (log.track_number || '?')
        + ' &bull; ' + formatTimestamp(log.timestamp) + '</span>'
        + '</div>'
        + '<div class="log-row-actions">'
        + '<button class="btn-icon" onclick="dismissLog(\'' + escapeHtml(String(log.id))
        + '\')" title="Dismiss"><i class="fas fa-times"></i></button>'
        + '</div></div>';

    if (log.candidates && log.candidates.length > 0) {
        log.candidates.forEach(function(c) {
            html += renderCandidateRow(c);
        });
    }
    html += '</div>';
    return html;
}

function renderBannedUrlRow(log) {
    var safeUrl = sanitizeUrl(log.youtube_url);
    var urlHtml = safeUrl
        ? '<a class="ban-url-link" href="' + escapeHtml(safeUrl) + '" target="_blank" rel="noopener">' + escapeHtml(log.youtube_url) + '</a>'
        : escapeHtml(log.youtube_url || '');
    var trackNum = log.track_number
        ? String(log.track_number).padStart(2, '0') + ' - '
        : '';
    return '<div class="log-row url_banned" id="log-' + escapeHtml(String(log.id)) + '">'
        + '<div class="status-icon"><i class="fas fa-ban"></i></div>'
        + '<div class="log-row-info">'
        + '<span class="log-row-title">' + escapeHtml(log.track_title || log.album_title || '') + '</span>'
        + '<span class="log-row-meta">by ' + escapeHtml(log.artist_name || '')
        + ' &bull; ' + urlHtml
        + ' &bull; ' + formatTimestamp(log.timestamp) + '</span>'
        + '</div>'
        + '<div class="log-row-actions">'
        + '<button class="btn-unban" onclick="unbanUrl(' + log._ban_id + ',\'' + escapeHtml(String(log.id)) + '\')">'
        + '<i class="fas fa-unlock"></i> Unban</button>'
        + '</div></div>';
}

function renderCandidateRow(c) {
    var outcomeClass = escapeHtml(c.outcome || '');
    var outcomeLabel = {
        mismatch: 'Mismatch',
        unverified: 'No AcoustID',
        download_failed: 'DL Failed',
        verified: 'Verified',
        accepted_no_verify: 'No Verify',
        accepted_unverified_fallback: 'Fallback',
    }[c.outcome] || c.outcome;

    var safeUrl = sanitizeUrl(c.youtube_url);
    var urlHtml = safeUrl
        ? '<a href="' + escapeHtml(safeUrl)
          + '" target="_blank" rel="noopener" style="color:var(--text-dim);text-decoration:none;">'
          + escapeHtml(c.youtube_title || c.youtube_url) + '</a>'
        : escapeHtml(c.youtube_title || '');

    var detail = '';
    if (c.outcome === 'mismatch') {
        detail = 'AcoustID: &ldquo;' + escapeHtml(c.acoustid_matched_title || '?')
            + '&rdquo; (' + (c.acoustid_score || 0).toFixed(2) + ')';
    } else if (c.outcome === 'unverified') {
        detail = 'AcoustID: no results';
    } else if (c.outcome === 'download_failed') {
        detail = escapeHtml(c.error_message || 'Download failed');
    } else if (c.outcome === 'verified') {
        detail = 'AcoustID: verified (' + (c.acoustid_score || 0).toFixed(2) + ')';
    }

    var duration = c.duration_seconds
        ? Math.floor(c.duration_seconds / 60) + ':'
          + String(c.duration_seconds % 60).padStart(2, '0')
        : '';

    var unbanHtml = '';
    if (c.is_banned && c.ban_id) {
        unbanHtml = ' <button class="btn-unban" onclick="event.stopPropagation();unbanUrl('
            + c.ban_id + ',null)"><i class="fas fa-unlock"></i> Unban</button>';
    }

    return '<div class="candidate-row">'
        + '<span class="candidate-outcome ' + outcomeClass + '">' + outcomeLabel + '</span>'
        + urlHtml
        + (c.match_score ? ' <span class="candidate-sep">&bull;</span> score: '
            + c.match_score.toFixed(2) : '')
        + (duration ? ' <span class="candidate-sep">&bull;</span> ' + duration : '')
        + (detail ? ' <span class="candidate-sep">&bull;</span> ' + detail : '')
        + unbanHtml
        + '</div>';
}
```

- [ ] **Step 3: Update renderLogs to use renderLogEntry**

In the `renderLogs` function, change the `.map(renderLogCard)` call to `.map(renderLogEntry)`.

- [ ] **Step 4: Add "Track Failures" option to filter dropdown**

In the filter `<select>` (line 793-804), add after the "Manual Download" option:

```html
<option value="track_failure">Track Failures</option>
```

- [ ] **Step 5: Update unbanUrl to work from candidate rows**

The `unbanUrl` function already handles unbanning. Update it to handle `null` logElementId gracefully (just refresh logs without targeting a specific card):

```javascript
async function unbanUrl(banId, logElementId) {
    try {
        var resp = await fetch('/api/banned-urls/' + banId, {
            method: 'DELETE',
        });
        if (resp.ok) {
            if (logElementId) {
                var card = document.getElementById('log-' + logElementId);
                if (card) {
                    card.style.animation = 'slideOut 0.3s ease forwards';
                }
            }
            setTimeout(function() { fetchLogs(); }, 300);
        } else {
            alert('Failed to unban URL');
        }
    } catch (e) {
        console.error('Unban error:', e);
        alert('Failed to unban URL');
    }
}
```

- [ ] **Step 6: Verify in browser**

Run: `docker compose up -d --build`

Visit `http://localhost:5000/logs` and verify:
- Album-level logs render as compact single-line rows
- Track failure entries show candidate sub-rows
- Unban buttons work on mismatch candidates
- Filter dropdown includes "Track Failures" option
- Pagination still works
- Light/dark theme toggle works

Run: `docker compose down`

- [ ] **Step 7: Commit**

```bash
git add templates/logs.html
git commit -m "feat(ui): compact row layout for logs with candidate sub-rows"
```

---

### Task 8: Full Integration Test and Cleanup

**Files:**
- Test: `tests/test_routes.py`
- Test: `tests/test_models.py`
- Test: `tests/test_db.py`
- Modify: `CLAUDE.md` (update schema version reference)

- [ ] **Step 1: Run full test suite**

Run: `source .venv/bin/activate && python3 -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 2: Run linter**

Run: `source .venv/bin/activate && ruff check .`
Expected: No errors

- [ ] **Step 3: Update CLAUDE.md schema version**

In `CLAUDE.md`, update:
- "Current schema version: **4**" to "Current schema version: **5**"
- Add V4->V5 migration note: "V4->V5: Added `candidate_attempts` table for per-candidate verification data. Added `track_title`, `track_number`, `track_download_id` columns to `download_logs`."

- [ ] **Step 4: Update TESTING.md with new manual test cases**

Add to the "Logs page" section:
- Track Failures filter shows per-track failure rows
- Each track failure shows candidate sub-rows with outcome badges
- Mismatch candidates show AcoustID matched title and score
- Unverified candidates show "AcoustID: no results"
- Download failed candidates show error message
- Unban button on mismatch candidates works
- Success/import logs render as compact single-line rows
- All log types render correctly in compact format

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md TESTING.md
git commit -m "docs: update schema version and testing checklist for logs enhancement"
```

- [ ] **Step 6: Run final full test suite**

Run: `source .venv/bin/activate && python3 -m pytest tests/ -v`
Expected: ALL PASS
