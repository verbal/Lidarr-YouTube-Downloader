"""Data access layer for SQLite-backed persistence.

This is the ONLY module that writes SQL. All other modules call these
functions to read/write download history, logs, failed tracks, and the
download queue.
"""

import json
import logging
import math
import time
from datetime import datetime

import db

logger = logging.getLogger(__name__)

QUEUE_STATUS_QUEUED = "queued"
QUEUE_STATUS_DOWNLOADING = "downloading"
QUEUE_STATUSES = {QUEUE_STATUS_QUEUED, QUEUE_STATUS_DOWNLOADING}


def _paginate(query, count_query, params, page, per_page):
    """Run a paginated query and return a standard response dict."""
    conn = db.get_db()
    total = conn.execute(count_query, params).fetchone()[0]
    pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page
    rows = conn.execute(
        query + " LIMIT ? OFFSET ?", (*params, per_page, offset)
    ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
    }


# --- History ---


def add_history_entry(
    album_id, album_title, artist_name, success, partial,
    manual=False, track_title=None,
):
    """Record a completed download attempt."""
    conn = db.get_db()
    conn.execute(
        """INSERT INTO download_history
           (album_id, album_title, artist_name, success, partial,
            manual, track_title, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            album_id, album_title, artist_name,
            int(success), int(partial), int(manual),
            track_title, time.time(),
        ),
    )
    conn.commit()


def get_history(page=1, per_page=50):
    """Return paginated download history, newest first."""
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
    """Count successful downloads since midnight today."""
    now = datetime.now()
    today_start = datetime(now.year, now.month, now.day).timestamp()
    conn = db.get_db()
    row = conn.execute(
        "SELECT COUNT(*) FROM download_history"
        " WHERE success = 1 AND timestamp >= ?",
        (today_start,),
    ).fetchone()
    return row[0]


def clear_history():
    """Delete all download history entries."""
    conn = db.get_db()
    conn.execute("DELETE FROM download_history")
    conn.commit()


# --- Logs ---


def add_log(
    log_type, album_id, album_title, artist_name,
    details="", failed_tracks=None, total_file_size=0,
):
    """Create a download log entry. Returns the generated log ID."""
    conn = db.get_db()
    log_id = f"{int(time.time() * 1000)}_{album_id}"
    conn.execute(
        """INSERT INTO download_logs
           (id, type, album_id, album_title, artist_name, timestamp,
            details, failed_tracks, total_file_size)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            log_id, log_type, album_id, album_title, artist_name,
            time.time(), details,
            json.dumps(failed_tracks or []), total_file_size,
        ),
    )
    conn.commit()
    return log_id


def get_logs(page=1, per_page=50):
    """Return paginated download logs, newest first."""
    result = _paginate(
        "SELECT * FROM download_logs ORDER BY timestamp DESC",
        "SELECT COUNT(*) FROM download_logs",
        (), page, per_page,
    )
    for item in result["items"]:
        item["failed_tracks"] = json.loads(item["failed_tracks"])
    return result


def delete_log(log_id):
    """Delete a single log entry by ID. Returns True if deleted."""
    conn = db.get_db()
    cursor = conn.execute(
        "DELETE FROM download_logs WHERE id = ?", (log_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


def clear_logs():
    """Delete all download log entries."""
    conn = db.get_db()
    conn.execute("DELETE FROM download_logs")
    conn.commit()


def get_logs_db_size():
    """Estimate the storage used by log text fields."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT SUM(LENGTH(details) + LENGTH(failed_tracks))"
        " FROM download_logs"
    ).fetchone()
    return row[0] or 0


# --- Failed tracks ---


def save_failed_tracks(
    album_id, album_title, artist_name, cover_url,
    album_path, lidarr_album_path, tracks,
):
    """Replace all failed tracks with a new set."""
    conn = db.get_db()
    conn.execute("DELETE FROM failed_tracks")
    for t in tracks:
        conn.execute(
            """INSERT INTO failed_tracks
               (album_id, album_title, artist_name, cover_url,
                album_path, lidarr_album_path, track_title,
                track_num, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                album_id, album_title, artist_name, cover_url,
                album_path, lidarr_album_path,
                t["title"], t.get("track_num", 0), t.get("reason", ""),
            ),
        )
    conn.commit()


def get_failed_tracks():
    """Return all failed track rows as dicts."""
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM failed_tracks").fetchall()
    return [dict(row) for row in rows]


def get_failed_tracks_context():
    """Return failed tracks with album context for retry UI."""
    conn = db.get_db()
    row = conn.execute("SELECT * FROM failed_tracks LIMIT 1").fetchone()
    if row is None:
        return {
            "failed_tracks": [],
            "album_id": None,
            "album_title": "",
            "artist_name": "",
            "cover_url": "",
            "album_path": "",
            "lidarr_album_path": "",
        }
    tracks = get_failed_tracks()
    return {
        "failed_tracks": [
            {
                "title": t["track_title"],
                "reason": t["reason"],
                "track_num": t["track_num"],
            }
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
    """Remove a single failed track by title (case-insensitive)."""
    conn = db.get_db()
    conn.execute(
        "DELETE FROM failed_tracks WHERE LOWER(track_title) = LOWER(?)",
        (track_title,),
    )
    conn.commit()


def clear_failed_tracks():
    """Delete all failed track entries."""
    conn = db.get_db()
    conn.execute("DELETE FROM failed_tracks")
    conn.commit()


# --- Queue ---


def enqueue_album(album_id):
    """Add an album to the download queue. Returns False if duplicate."""
    conn = db.get_db()
    existing = conn.execute(
        "SELECT id FROM download_queue WHERE album_id = ?", (album_id,)
    ).fetchone()
    if existing:
        return False
    max_pos = conn.execute(
        "SELECT COALESCE(MAX(position), 0) FROM download_queue"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO download_queue (album_id, position, status)"
        " VALUES (?, ?, ?)",
        (album_id, max_pos + 1, QUEUE_STATUS_QUEUED),
    )
    conn.commit()
    return True


def dequeue_album(album_id):
    """Remove an album from the queue and reorder positions."""
    conn = db.get_db()
    conn.execute(
        "DELETE FROM download_queue WHERE album_id = ?", (album_id,)
    )
    conn.commit()
    _reorder_queue(conn)


def get_queue():
    """Return all queued albums ordered by position."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT * FROM download_queue ORDER BY position"
    ).fetchall()
    return [dict(row) for row in rows]


def get_queue_length():
    """Return the number of albums in the queue."""
    conn = db.get_db()
    return conn.execute(
        "SELECT COUNT(*) FROM download_queue"
    ).fetchone()[0]


def pop_next_from_queue():
    """Remove and return the next queued album_id, or None."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT album_id FROM download_queue"
        " WHERE status = ? ORDER BY position LIMIT 1",
        (QUEUE_STATUS_QUEUED,),
    ).fetchone()
    if row is None:
        return None
    album_id = row[0]
    conn.execute(
        "DELETE FROM download_queue WHERE album_id = ?", (album_id,)
    )
    conn.commit()
    _reorder_queue(conn)
    return album_id


def set_queue_status(album_id, status):
    """Update the status of a queued album."""
    if status not in QUEUE_STATUSES:
        raise ValueError(
            f"Invalid queue status: {status}."
            f" Must be one of {QUEUE_STATUSES}"
        )
    conn = db.get_db()
    conn.execute(
        "UPDATE download_queue SET status = ? WHERE album_id = ?",
        (status, album_id),
    )
    conn.commit()


def reset_downloading_to_queued():
    """Reset any 'downloading' entries back to 'queued' on startup."""
    conn = db.get_db()
    conn.execute(
        "UPDATE download_queue SET status = ? WHERE status = ?",
        (QUEUE_STATUS_QUEUED, QUEUE_STATUS_DOWNLOADING),
    )
    conn.commit()


def clear_queue():
    """Delete all entries from the download queue."""
    conn = db.get_db()
    conn.execute("DELETE FROM download_queue")
    conn.commit()


def _reorder_queue(conn):
    """Renumber queue positions sequentially starting at 1."""
    rows = conn.execute(
        "SELECT id FROM download_queue ORDER BY position"
    ).fetchall()
    for i, row in enumerate(rows, 1):
        conn.execute(
            "UPDATE download_queue SET position = ? WHERE id = ?",
            (i, row[0]),
        )
    conn.commit()
