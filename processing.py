"""Download processing: album downloads, queue management, progress tracking.

Manages the active download process state, orchestrates per-album
downloads (search, download, tag, import to Lidarr), and processes
the download queue.
"""

import copy
import logging
import os
import shutil
import threading
import time
import uuid

import models
from config import load_config
from downloader import (
    download_youtube_candidate,
    search_youtube_candidates,
)
from fingerprint import fingerprint_track, verify_fingerprint
from lidarr import get_valid_release_id, lidarr_request
from metadata import (
    create_xml_metadata,
    get_itunes_artwork,
    get_itunes_tracks,
    tag_mp3,
)
from notifications import send_notifications
from utils import sanitize_filename, set_permissions

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = os.getenv("DOWNLOAD_PATH", "")

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

queue_lock = threading.Lock()


class TrackSkippedException(Exception):
    """Raised from yt-dlp progress hook when track skip is requested."""


def _make_progress_hook(idx):
    """Create a yt-dlp progress hook bound to a specific track index."""

    def hook(d):
        if d["status"] == "downloading":
            if 0 <= idx < len(download_process["tracks"]):
                track = download_process["tracks"][idx]
                track["status"] = "downloading"
                track["progress_percent"] = (
                    d.get("_percent_str", "0%").strip()
                )
                track["progress_speed"] = (
                    d.get("_speed_str", "N/A").strip()
                )
                if track.get("skip"):
                    logger.debug(
                        "Skip flag detected for track %d: %s",
                        idx, track.get("track_title", ""),
                    )
                    raise TrackSkippedException()

    return hook


def get_download_status():
    """Return a snapshot of the current download process state."""
    with queue_lock:
        snapshot = dict(download_process)
        snapshot["tracks"] = copy.deepcopy(download_process["tracks"])
        return snapshot


def stop_download():
    """Signal the active download to stop and clear the queue."""
    with queue_lock:
        download_process["stop"] = True
        for track in download_process.get("tracks", []):
            if track.get("status") in (
                "pending", "searching", "downloading", "verifying",
            ):
                track["skip"] = True
        models.clear_queue()


def process_album_download(album_id, force=False):
    """Download all tracks for an album and import into Lidarr.

    Args:
        album_id: Lidarr album ID to download.
        force: If True, re-download tracks that already exist.

    Returns:
        Dict with "success", "error", or "stopped" key.
    """
    with queue_lock:
        if download_process["active"]:
            return {"error": "Busy"}
        download_process["active"] = True
        download_process["stop"] = False
        download_process["result_success"] = True
        download_process["result_partial"] = False
        download_process["tracks"] = []
        download_process["current_track_index"] = -1
        download_process["album_id"] = album_id
        download_process["album_title"] = ""
        download_process["artist_name"] = ""
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
            logger.error(
                f"Error fetching album {album_id}: {album['error']}"
            )
            return album

        logger.info(
            f"Starting download for album:"
            f" {album.get('title', 'Unknown')}"
            f" - {album.get('artist', {}).get('artistName', 'Unknown')}"
        )

        tracks = album.get("tracks", [])
        if not tracks:
            tracks_res = lidarr_request(f"track?albumId={album_id}")
            if isinstance(tracks_res, dict) and "error" in tracks_res:
                logger.warning(
                    "Lidarr track fetch for album %s failed: %s",
                    album_id, tracks_res["error"],
                )
            elif isinstance(tracks_res, list) and len(tracks_res) > 0:
                tracks = tracks_res

        if not tracks:
            logger.info(
                "No tracks from Lidarr for album %s, using iTunes fallback",
                album_id,
            )
            tracks = get_itunes_tracks(
                album["artist"]["artistName"], album["title"]
            )

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
            (
                img["remoteUrl"]
                for img in album.get("images", [])
                if img.get("coverType") == "cover"
            ),
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
            album_folder_name = (
                f"{sanitized_album} ({release_year}) [{album_type}]"
            )
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
        )
        send_notifications(
            f"Download Started\n"
            f"Album: {album_title}\n"
            f"Artist: {artist_name}\n"
            f"Tracks: {len(tracks)}",
            log_type="download_started",
            embed_data={
                "title": "Download Started",
                "description": f"{artist_name} — {album_title}",
                "color": 0x3498DB,
                "fields": [
                    {"name": "Tracks", "value": str(len(tracks)),
                     "inline": True},
                ],
            },
        )

        cover_data = get_itunes_artwork(artist_name, album_title)
        if cover_data:
            with open(os.path.join(album_path, "cover.jpg"), "wb") as f:
                f.write(cover_data)

        tracks_to_download = _filter_tracks(
            tracks, force, album_path,
        )

        if len(tracks_to_download) == 0:
            lidarr_request(
                "command",
                method="POST",
                data={"name": "RefreshArtist", "artistId": artist_id},
            )
            return {"success": True, "message": "Skipped"}

        logger.info(f"Total tracks to download: {len(tracks_to_download)}")

        album_ctx = {
            "artist_name": artist_name,
            "album_title": album_title,
            "album_id": album_id,
            "album_mbid": album_mbid,
            "artist_mbid": artist_mbid,
            "cover_data": cover_data,
            "cover_url": download_process.get("cover_url", ""),
            "lidarr_album_path": lidarr_album_path,
        }
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
        failed_tracks, total_downloaded_size = _download_tracks(
            tracks_to_download, album_path, album, album_ctx,
        )

        set_permissions(artist_path)

        result = _handle_post_download(
            failed_tracks, tracks_to_download, album_id,
            album_title, artist_name, total_downloaded_size,
        )
        if result is not None:
            return result

        config = load_config()
        lidarr_path = config.get("lidarr_path", "")
        import_path, lidarr_album_path = _copy_to_lidarr(
            lidarr_path, album_path, sanitized_artist,
            album_folder_name,
        )

        logger.info(
            f"Album downloaded successfully:"
            f" {artist_name} - {album_title}"
        )

        _log_import_result(
            failed_tracks, album_id, album_title, artist_name,
            total_downloaded_size,
        )

        lidarr_request(
            "command",
            method="POST",
            data={"name": "RefreshArtist", "artistId": artist_id},
        )

        if lidarr_path and os.path.exists(artist_path):
            try:
                logger.info(
                    f"Cleaning up download folder: {artist_path}"
                )
                shutil.rmtree(artist_path)
                logger.info("Download folder cleaned up successfully")
            except Exception as e:
                logger.warning(
                    f"Failed to cleanup download folder: {e}"
                )

        return {"success": True}

    except Exception as e:
        logger.error("Error during album download: %s", e, exc_info=True)
        _artist = download_process.get("artist_name", "Unknown")
        _album = download_process.get("album_title", "Unknown")
        send_notifications(
            f"Download failed\nAlbum: {_album}\nArtist: {_artist}",
            log_type="album_error",
            embed_data={
                "title": "Download Failed",
                "description": f"{_artist} — {_album}",
                "color": 0xE74C3C,
            },
        )
        models.add_log(
            log_type="album_error",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details=f"Error: {e}",
        )
        download_process["result_success"] = False
        return {"error": str(e)}
    finally:
        with queue_lock:
            download_process["active"] = False
            download_process["tracks"] = []
            download_process["current_track_index"] = -1
            download_process["album_id"] = None
            download_process["album_title"] = ""
            download_process["artist_name"] = ""
            download_process["cover_url"] = ""


def _filter_tracks(tracks, force, album_path):
    """Filter tracks that need downloading."""
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
            final_file = os.path.join(
                album_path, f"{track_num:02d} - {sanitized_track}.mp3"
            )
            if os.path.exists(final_file):
                continue
        tracks_to_download.append(t)
    return tracks_to_download


def _cleanup_temp_files(temp_file):
    """Remove temp download files for all common extensions."""
    for ext in [".mp3", ".webm", ".m4a", ".part", ""]:
        tmp = temp_file + ext
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError as rm_err:
                logger.debug(
                    "Failed to remove temp file %s: %s", tmp, rm_err,
                )


def _download_tracks(
    tracks_to_download, album_path, album, album_ctx,
):
    """Download each track, tag, and create XML metadata.

    Uses a ThreadPoolExecutor with ``concurrent_tracks`` workers so
    multiple tracks download in parallel.

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
    from concurrent.futures import ThreadPoolExecutor, as_completed

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
    _results_lock = threading.Lock()

    config = load_config()
    concurrent_tracks = max(
        1, min(5, config.get("concurrent_tracks", 2)),
    )

    def _process_single_track(idx, track):
        nonlocal total_downloaded_size

        track_state = download_process["tracks"][idx]
        download_process["current_track_index"] = idx
        track_title = track.get("title", f"Track {idx + 1}")
        track_num = int(track.get("trackNumber", idx + 1))
        track_duration_ms = track.get("duration")
        expected_recording_id = track.get("foreignRecordingId")
        sanitized_track = sanitize_filename(track_title)

        def _skip_check():
            return (
                track_state.get("skip")
                or download_process.get("stop")
            )

        if _skip_check():
            track_state["status"] = "skipped"
            return

        progress_hook = _make_progress_hook(idx)
        track_state["status"] = "searching"

        try:
            banned_url_set = models.get_banned_urls_for_track(
                album_id, track_title,
            )
        except Exception:
            logger.warning(
                "Failed to fetch banned URLs for track '%s',"
                " proceeding without ban filter",
                track_title, exc_info=True,
            )
            banned_url_set = set()

        candidates = search_youtube_candidates(
            f"{artist_name} {track_title} official audio",
            track_title,
            track_duration_ms,
            skip_check=_skip_check,
            banned_urls=banned_url_set,
        )

        if not candidates:
            if _skip_check():
                track_state["status"] = "skipped"
                return
            fail_reason = (
                "No suitable YouTube match found"
                " (filtered by duration/forbidden words)"
            )
            track_state["status"] = "failed"
            track_state["error_message"] = fail_reason
            with _results_lock:
                failed_tracks.append({
                    "title": track_title,
                    "reason": fail_reason,
                    "track_num": track_num,
                })
            try:
                models.add_track_download(
                    album_id=album_id, album_title=album_title,
                    artist_name=artist_name,
                    track_title=track_title,
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
                    "Failed to record track download for"
                    " '%s' (album %d)",
                    track_title, album_id, exc_info=True,
                )
            return

        all_unverified = True
        best_unverified_candidate = None
        accepted = False
        any_downloaded = False

        for ci, candidate in enumerate(candidates):
            if _skip_check():
                track_state["status"] = "skipped"
                return

            track_state["status"] = "downloading"
            attempt_temp = os.path.join(
                album_path,
                f"temp_{track_num:02d}_{uuid.uuid4().hex[:8]}",
            )

            dl_result_box = [None]
            dl_error_box = [None]

            def _run_dl(cand=candidate, tmp=attempt_temp):
                try:
                    dl_result_box[0] = download_youtube_candidate(
                        cand, tmp,
                        progress_hook=progress_hook,
                        skip_check=_skip_check,
                    )
                except TrackSkippedException:
                    dl_error_box[0] = "skipped"
                except Exception as exc:
                    dl_error_box[0] = exc

            dl_thread = threading.Thread(
                target=_run_dl, daemon=True,
            )
            dl_thread.start()

            while dl_thread.is_alive():
                dl_thread.join(timeout=0.5)
                if dl_thread.is_alive() and _skip_check():
                    _cleanup_temp_files(attempt_temp)
                    track_state["status"] = "skipped"
                    return

            if dl_error_box[0] == "skipped":
                _cleanup_temp_files(attempt_temp)
                track_state["status"] = "skipped"
                return

            if isinstance(dl_error_box[0], Exception):
                _cleanup_temp_files(attempt_temp)
                continue

            dl_result = dl_result_box[0]
            if dl_result is None or dl_result.get("skipped"):
                _cleanup_temp_files(attempt_temp)
                if dl_result and dl_result.get("skipped"):
                    track_state["status"] = "skipped"
                    return
                continue

            if not dl_result.get("success"):
                _cleanup_temp_files(attempt_temp)
                continue

            actual_file = attempt_temp + ".mp3"
            if not os.path.exists(actual_file):
                continue

            any_downloaded = True
            track_state["status"] = "tagging"
            track_state["youtube_url"] = dl_result.get(
                "youtube_url", "",
            )
            track_state["youtube_title"] = dl_result.get(
                "youtube_title", "",
            )
            tag_mp3(actual_file, track, album, cover_data)

            cfg = load_config()
            should_verify = (
                cfg.get("acoustid_enabled", True)
                and cfg.get("acoustid_api_key", "")
                and expected_recording_id
            )

            fp_data = {}

            if should_verify:
                track_state["status"] = "verifying"
                vresult = verify_fingerprint(
                    actual_file,
                    expected_recording_id,
                    cfg["acoustid_api_key"],
                )

                if vresult is None:
                    # fpcalc unavailable — skip verification
                    pass
                elif vresult["status"] == "verified":
                    fp_data = vresult["fp_data"]
                elif vresult["status"] == "mismatch":
                    all_unverified = False
                    remaining = len(candidates) - ci - 1
                    next_msg = (
                        f"Trying next candidate"
                        f" ({ci + 2}/{len(candidates)})."
                        if remaining > 0
                        else "No more candidates."
                    )
                    logger.info(
                        "AcoustID verification failed for"
                        " '%s': expected=%s, got=%s"
                        " (score=%.2f). %s",
                        track_title,
                        expected_recording_id,
                        vresult["matched_id"],
                        vresult["fp_data"].get("acoustid_score", 0),
                        next_msg,
                    )
                    _cleanup_temp_files(attempt_temp)
                    try:
                        models.add_banned_url(
                            youtube_url=candidate["url"],
                            youtube_title=candidate["title"],
                            album_id=album_id,
                            album_title=album_title,
                            artist_name=artist_name,
                            track_title=track_title,
                            track_number=track_num,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to ban URL %s",
                            candidate["url"],
                            exc_info=True,
                        )
                    track_state["youtube_url"] = ""
                    track_state["youtube_title"] = ""
                    continue
                elif vresult["status"] == "unverified":
                    if best_unverified_candidate is None:
                        best_unverified_candidate = candidate
                    _cleanup_temp_files(attempt_temp)
                    continue
            else:
                # No verification — run standard fingerprint if enabled
                if cfg.get("acoustid_enabled", True):
                    api_key = cfg.get("acoustid_api_key", "")
                    if api_key:
                        track_state["status"] = "fingerprinting"
                        fp_result = fingerprint_track(
                            actual_file, api_key,
                        )
                        if fp_result:
                            fp_data = fp_result

            # === Accept the file ===
            if cfg.get("xml_metadata_enabled", True):
                logger.info("Creating XML metadata file...")
                create_xml_metadata(
                    album_path, artist_name, album_title,
                    track_num, track_title,
                    album_mbid, artist_mbid,
                )

            final_file = os.path.join(
                album_path,
                f"{track_num:02d} - {sanitized_track}.mp3",
            )
            try:
                file_size = os.path.getsize(actual_file)
            except OSError:
                file_size = 0
            shutil.move(actual_file, final_file)
            track_state["status"] = "done"
            with _results_lock:
                total_downloaded_size += file_size
            try:
                models.add_track_download(
                    album_id=album_id,
                    album_title=album_title,
                    artist_name=artist_name,
                    track_title=track_title,
                    track_number=track_num, success=True,
                    error_message="",
                    youtube_url=dl_result.get("youtube_url", ""),
                    youtube_title=dl_result.get("youtube_title", ""),
                    match_score=dl_result.get("match_score", 0.0),
                    duration_seconds=dl_result.get(
                        "duration_seconds", 0,
                    ),
                    album_path=album_path,
                    lidarr_album_path=lidarr_album_path,
                    cover_url=cover_url,
                    acoustid_fingerprint_id=fp_data.get(
                        "acoustid_fingerprint_id", "",
                    ),
                    acoustid_score=fp_data.get("acoustid_score", 0.0),
                    acoustid_recording_id=fp_data.get(
                        "acoustid_recording_id", "",
                    ),
                    acoustid_recording_title=fp_data.get(
                        "acoustid_recording_title", "",
                    ),
                )
            except Exception:
                logger.error(
                    "Failed to record track download for"
                    " '%s' (album %d)",
                    track_title, album_id, exc_info=True,
                )
            accepted = True
            break

        if not accepted:
            if all_unverified and best_unverified_candidate:
                # Re-download best unverified, accept without verify
                track_state["status"] = "downloading"
                fallback_temp = os.path.join(
                    album_path,
                    f"temp_{track_num:02d}_{uuid.uuid4().hex[:8]}",
                )
                fb_result = download_youtube_candidate(
                    best_unverified_candidate, fallback_temp,
                    progress_hook=progress_hook,
                    skip_check=_skip_check,
                )
                if fb_result.get("skipped"):
                    _cleanup_temp_files(fallback_temp)
                    track_state["status"] = "skipped"
                    return
                fb_file = fallback_temp + ".mp3"
                if (
                    fb_result.get("success")
                    and os.path.exists(fb_file)
                ):
                    track_state["status"] = "tagging"
                    track_state["youtube_url"] = fb_result.get(
                        "youtube_url", "",
                    )
                    track_state["youtube_title"] = fb_result.get(
                        "youtube_title", "",
                    )
                    tag_mp3(fb_file, track, album, cover_data)
                    cfg = load_config()
                    if cfg.get("xml_metadata_enabled", True):
                        create_xml_metadata(
                            album_path, artist_name,
                            album_title, track_num,
                            track_title, album_mbid,
                            artist_mbid,
                        )
                    final_file = os.path.join(
                        album_path,
                        f"{track_num:02d} - {sanitized_track}.mp3",
                    )
                    try:
                        file_size = os.path.getsize(fb_file)
                    except OSError:
                        file_size = 0
                    shutil.move(fb_file, final_file)
                    track_state["status"] = "done"
                    with _results_lock:
                        total_downloaded_size += file_size
                    try:
                        models.add_track_download(
                            album_id=album_id,
                            album_title=album_title,
                            artist_name=artist_name,
                            track_title=track_title,
                            track_number=track_num,
                            success=True,
                            error_message="",
                            youtube_url=fb_result.get(
                                "youtube_url", "",
                            ),
                            youtube_title=fb_result.get(
                                "youtube_title", "",
                            ),
                            match_score=fb_result.get(
                                "match_score", 0.0,
                            ),
                            duration_seconds=fb_result.get(
                                "duration_seconds", 0,
                            ),
                            album_path=album_path,
                            lidarr_album_path=lidarr_album_path,
                            cover_url=cover_url,
                            acoustid_fingerprint_id="",
                            acoustid_score=0.0,
                            acoustid_recording_id="",
                            acoustid_recording_title="",
                        )
                    except Exception:
                        logger.error(
                            "Failed to record track download"
                            " for '%s' (album %d)",
                            track_title, album_id,
                            exc_info=True,
                        )
                    return
                else:
                    _cleanup_temp_files(fallback_temp)

            # Determine appropriate error message
            if not any_downloaded:
                fail_reason = (
                    "All candidate downloads failed"
                    f" (tried {len(candidates)} candidates)"
                )
            else:
                fail_reason = (
                    "AcoustID verification failed: no candidate"
                    f" matched expected recording"
                    f" {expected_recording_id}"
                    f" (tried {len(candidates)} candidates)"
                )
            track_state["status"] = "failed"
            track_state["error_message"] = fail_reason
            with _results_lock:
                failed_tracks.append({
                    "title": track_title,
                    "reason": fail_reason,
                    "track_num": track_num,
                })
            try:
                models.add_track_download(
                    album_id=album_id,
                    album_title=album_title,
                    artist_name=artist_name,
                    track_title=track_title,
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
                    "Failed to record track download for"
                    " '%s' (album %d)",
                    track_title, album_id, exc_info=True,
                )

    with ThreadPoolExecutor(max_workers=concurrent_tracks) as executor:
        futures = {
            executor.submit(_process_single_track, idx, track): idx
            for idx, track in enumerate(tracks_to_download)
        }
        for future in as_completed(futures):
            if download_process["stop"]:
                executor.shutdown(wait=False, cancel_futures=True)
                logger.warning("Download stopped by user")
                break
            try:
                future.result()
            except Exception as e:
                logger.warning("Track worker exception: %s", e)

    return failed_tracks, total_downloaded_size


def _handle_post_download(
    failed_tracks, tracks_to_download, album_id,
    album_title, artist_name, total_downloaded_size,
):
    """Log and notify about download results.

    Returns:
        A result dict if download should stop (all failed), else None.
    """
    skipped_count = sum(
        1 for t in download_process.get("tracks", [])
        if t.get("status") == "skipped"
    )
    attempted_count = len(tracks_to_download) - skipped_count

    if failed_tracks:
        failed_list = "\n".join(
            [f"* {t['title']}" for t in failed_tracks]
        )

        if attempted_count > 0 and len(failed_tracks) == attempted_count:
            send_notifications(
                f"Download Failed (All Tracks)\n"
                f"Album: {album_title}\nArtist: {artist_name}\n\n"
                f"Failed tracks:\n{failed_list}",
                log_type="album_error",
                embed_data={
                    "title": "Download Failed",
                    "description": f"{artist_name} — {album_title}",
                    "color": 0xE74C3C,
                    "fields": [{
                        "name": "Failed Tracks",
                        "value": failed_list[:1024],
                        "inline": False,
                    }],
                },
            )
            logger.error(
                f"All {len(failed_tracks)} tracks failed to download."
                " Skipping import."
            )
            models.add_log(
                log_type="album_error",
                album_id=album_id,
                album_title=album_title,
                artist_name=artist_name,
                details=(
                    f"All {attempted_count} track(s)"
                    " failed to download"
                ),
            )
            download_process["result_success"] = False
            return {"error": "All tracks failed to download"}

        download_process["result_partial"] = True
        send_notifications(
            f"Partial Download Completed\n"
            f"Album: {album_title}\nArtist: {artist_name}\n\n"
            f"Failed tracks:\n{failed_list}",
            log_type="partial_success",
            embed_data={
                "title": "Partial Download",
                "description": f"{artist_name} — {album_title}",
                "color": 0xE67E22,
                "fields": [{
                    "name": "Failed Tracks",
                    "value": failed_list[:1024],
                    "inline": False,
                }],
            },
        )
        logger.warning(
            f"Download completed with {len(failed_tracks)} failed"
            " tracks. Proceeding with import."
        )
        models.add_log(
            log_type="partial_success",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details=(
                f"{len(failed_tracks)} track(s) failed to download"
                f" out of {attempted_count}"
            ),
            total_file_size=total_downloaded_size,
        )
    else:
        models.add_log(
            log_type="download_success",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details=(
                f"Successfully downloaded"
                f" {attempted_count} track(s)"
            ),
            total_file_size=total_downloaded_size,
        )
        send_notifications(
            f"Download successful\n"
            f"Album: {album_title}\nArtist: {artist_name}\n"
            f"Tracks: {attempted_count}"
            f"/{attempted_count}",
            log_type="download_success",
            embed_data={
                "title": "Download Successful",
                "description": f"{artist_name} — {album_title}",
                "color": 0x2ECC71,
                "fields": [{
                    "name": "Tracks",
                    "value": (
                        f"{attempted_count}"
                        f"/{attempted_count}"
                    ),
                    "inline": True,
                }],
            },
        )
        logger.info("All tracks downloaded successfully")

    return None


def _copy_to_lidarr(
    lidarr_path, album_path, sanitized_artist, album_folder_name,
):
    """Copy downloaded files to Lidarr music folder if configured.

    Returns:
        Tuple of (import_path, lidarr_album_path).
    """
    lidarr_album_path = ""
    if lidarr_path:
        abs_lidarr = os.path.abspath(lidarr_path)
        abs_download = os.path.abspath(DOWNLOAD_DIR)

        if abs_lidarr == abs_download:
            logger.warning(
                "LIDARR_PATH matches DOWNLOAD_PATH."
                " Skipping move to prevent data loss."
            )
            lidarr_path = ""
        else:
            logger.info(
                f"Moving files to Lidarr music folder: {lidarr_path}"
            )
        lidarr_artist_path = os.path.join(
            lidarr_path, sanitized_artist
        )
        lidarr_album_path = os.path.join(
            lidarr_artist_path, album_folder_name
        )

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
            return lidarr_album_path, lidarr_album_path
        except Exception as e:
            logger.error(
                "Error copying files to Lidarr folder: %s",
                e, exc_info=True,
            )
            send_notifications(
                f"Copy to Lidarr failed: {e}",
                log_type="album_error",
            )
            return album_path, lidarr_album_path
    return album_path, lidarr_album_path


def _log_import_result(
    failed_tracks, album_id, album_title, artist_name,
    total_downloaded_size,
):
    """Log and notify about the Lidarr import result."""
    if failed_tracks:
        models.add_log(
            log_type="import_partial",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details=(
                f"Album imported with {len(failed_tracks)}"
                " failed tracks"
            ),
            total_file_size=total_downloaded_size,
        )
        send_notifications(
            f"Import Partial\nAlbum: {album_title}\n"
            f"Artist: {artist_name}\n"
            f"Refreshing in Lidarr"
            f" (Missing {len(failed_tracks)} tracks)",
            log_type="import_partial",
            embed_data={
                "title": "Import Partial",
                "description": f"{artist_name} — {album_title}",
                "color": 0xE67E22,
                "fields": [{
                    "name": "Missing Tracks",
                    "value": str(len(failed_tracks)),
                    "inline": True,
                }],
            },
        )
    else:
        models.add_log(
            log_type="import_success",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details="Album downloaded and refreshing in Lidarr",
            total_file_size=total_downloaded_size,
        )
        send_notifications(
            f"Import Success\nAlbum: {album_title}\n"
            f"Artist: {artist_name}\n"
            f"Refreshing in Lidarr",
            log_type="import_success",
            embed_data={
                "title": "Import Successful",
                "description": f"{artist_name} — {album_title}",
                "color": 0x2ECC71,
            },
        )


def process_download_queue():
    """Continuously process the download queue in a loop.

    Pops the next album from the queue and starts a download thread.
    Sleeps 2 seconds between checks.
    """
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
