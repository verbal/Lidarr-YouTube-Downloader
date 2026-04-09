"""Fire one example of each Telegram/Discord notification type.

Run with CONFIG_DIR pointed at a config.json that has telegram configured.
Usage: CONFIG_JSON=tmp/config/config.json python3 tools/send_example_notifications.py
"""
import json
import os
import sys
import time

cfg_path = os.environ.get("CONFIG_JSON", "tmp/config/config.json")
with open(cfg_path) as f:
    cfg = json.load(f)

# Force all log types on for the demo
demo_types = [
    "download_started", "download_success", "partial_success",
    "album_error", "import_success", "import_partial", "manual_download",
]
cfg["telegram_log_types"] = demo_types
cfg["discord_log_types"] = demo_types

import config as config_mod
config_mod.load_config = lambda: cfg  # type: ignore

import notifications  # noqa: E402
import processing  # noqa: E402

ARTIST = "Daft Punk"
ALBUM = "Discovery"
MBID = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"
COVER = "https://is1-ssl.mzstatic.com/image/thumb/Music221/v4/fd/4a/77/fd4a77db-0ebc-d043-41a2-f32fa1bb0fb4/dj.qrikkdwj.jpg/600x600bb.jpg"

failed = [
    {"title": "One More Time", "error": "No matching candidate (best score 0.42)",
     "reason": "No matching candidate (best score 0.42)"},
    {"title": "Aerodynamic", "error": "AcoustID mismatch",
     "reason": "AcoustID mismatch: got 'Cover Version'"},
]
verify_stats = {
    "verified_count": 8,
    "accepted_acoustid_scores": [0.97, 0.95, 0.99, 0.93, 0.98, 0.96, 0.94, 0.99],
    "mismatch_count": 2,
    "best_rejected_score": 0.71,
}

def fire(label, fn):
    print(f"→ {label}")
    fn()
    time.sleep(1.2)

# 1. download_started (with cover art photo)
fire("download_started", lambda: processing._send_album_notification(
    log_type="download_started",
    title="⬇️ Download Started",
    color=0x3498DB,
    artist_name=ARTIST, album_title=ALBUM, album_mbid=MBID, cover_url=COVER,
    fields=[{"name": "Tracks", "value": "14", "inline": True}],
))

# 2. download_success
fire("download_success", lambda: processing._send_album_notification(
    log_type="download_success",
    title="✅ Download Complete",
    color=0x2ECC71,
    artist_name=ARTIST, album_title=ALBUM, album_mbid=MBID, cover_url=COVER,
    fields=[
        {"name": "Tracks", "value": "14/14", "inline": True},
        {"name": "AcoustID verified", "value": "14/14 (avg 0.96)", "inline": True},
    ],
))

# 3. partial_success (failed tracks + verify summary)
verify_field, verify_md2 = processing._verify_summary_lines(verify_stats, 10)
failed_field = processing._format_failed_tracks_field(failed)
failed_md2 = processing._format_failed_tracks_md2(failed)
fire("partial_success", lambda: processing._send_album_notification(
    log_type="partial_success",
    title="⚠️ Partial Success",
    color=0xF39C12,
    artist_name=ARTIST, album_title=ALBUM, album_mbid=MBID, cover_url=COVER,
    fields=[
        {"name": "Result", "value": "8/10 tracks", "inline": True},
        {"name": "AcoustID", "value": verify_field, "inline": False},
        {"name": "Failed", "value": failed_field, "inline": False},
    ],
    extra_md2_lines=[*verify_md2, "*Failed tracks:*", *failed_md2],
))

# 4. album_error
fire("album_error", lambda: processing._send_album_notification(
    log_type="album_error",
    title="❌ Album Error",
    color=0xE74C3C,
    artist_name=ARTIST, album_title=ALBUM, album_mbid=MBID, cover_url=COVER,
    fields=[{"name": "Error", "value": "Lidarr refresh command failed", "inline": False}],
))

# 5. import_success
fire("import_success", lambda: processing._send_album_notification(
    log_type="import_success",
    title="📥 Imported to Lidarr",
    color=0x1ABC9C,
    artist_name=ARTIST, album_title=ALBUM, album_mbid=MBID, cover_url=COVER,
    fields=[{"name": "Imported", "value": "14/14", "inline": True}],
))

# 6. import_partial
fire("import_partial", lambda: processing._send_album_notification(
    log_type="import_partial",
    title="⚠️ Partial Import",
    color=0xF39C12,
    artist_name=ARTIST, album_title=ALBUM, album_mbid=MBID, cover_url=COVER,
    fields=[{"name": "Imported", "value": "12/14", "inline": True}],
))

# 7. manual_download (uses plain send_notifications — matches PR2 code path)
fire("manual_download", lambda: notifications.send_notifications(
    "Manual Download\nTrack: Harder Better Faster Stronger\n"
    f"Album: {ALBUM}\nArtist: {ARTIST}\nAcoustID: 0.98",
    log_type="manual_download",
    embed_data={
        "title": "Manual Download",
        "description": f"{ARTIST} — {ALBUM} — Harder Better Faster Stronger",
        "color": 0x2ECC71,
        "fields": [{"name": "AcoustID", "value": "0.98", "inline": True}],
    },
))

print("done — check Telegram")
