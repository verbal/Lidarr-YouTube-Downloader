"""Metadata functions for audio tagging, XML sidecar files, and iTunes API.

Handles audio tagging (MP3/M4A/Opus) with MusicBrainz IDs, XML metadata
generation for Lidarr import, and iTunes API lookups for track lists and
album artwork.
"""

import base64
import logging
import os
from xml.sax.saxutils import escape as xml_escape

import requests
from mutagen.flac import Picture
from mutagen.id3 import (
    APIC,
    ID3,
    TALB,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TRCK,
    TXXX,
    UFID,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm, AtomDataType
from mutagen.oggopus import OggOpus

from lidarr import get_monitored_release
from utils import sanitize_filename

logger = logging.getLogger(__name__)


def tag_mp3(file_path, track_info, album_info, cover_data):
    """Apply ID3 tags to an MP3 file including MusicBrainz metadata.

    Args:
        file_path: Path to the MP3 file.
        track_info: Dict with title, trackNumber, foreignRecordingId.
        album_info: Dict with title, artist, releaseDate, trackCount,
            foreignAlbumId, and releases list.
        cover_data: Raw bytes of cover art image, or None.

    Returns:
        True on success, False on failure.
    """
    try:
        try:
            audio = MP3(file_path, ID3=ID3)
        except Exception as e:
            logger.debug("MP3 load with ID3 failed for %s: %s, retrying", file_path, e)
            audio = MP3(file_path)
            audio.add_tags()
        if audio.tags is None:
            audio.add_tags()

        audio.tags.add(TIT2(encoding=3, text=track_info["title"]))
        audio.tags.add(
            TPE1(encoding=3, text=album_info["artist"]["artistName"])
        )
        audio.tags.add(
            TPE2(encoding=3, text=album_info["artist"]["artistName"])
        )
        audio.tags.add(TALB(encoding=3, text=album_info["title"]))
        audio.tags.add(
            TDRC(
                encoding=3,
                text=str(album_info.get("releaseDate", "")[:4]),
            )
        )

        try:
            t_num = int(track_info["trackNumber"])
            audio.tags.add(
                TRCK(
                    encoding=3,
                    text=f"{t_num}/{album_info.get('trackCount', 0)}",
                )
            )
        except (ValueError, KeyError):
            pass

        release = get_monitored_release(album_info)
        if release:
            _add_musicbrainz_tags(audio, track_info, album_info, release)

        if track_info.get("foreignRecordingId"):
            audio.tags.add(
                UFID(
                    owner="http://musicbrainz.org",
                    data=track_info["foreignRecordingId"].encode(),
                )
            )
        if cover_data:
            audio.tags.add(
                APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,
                    desc="Cover",
                    data=cover_data,
                )
            )

        audio.save(v2_version=3)
        return True
    except Exception as e:
        logger.warning(f"Failed to tag MP3 {file_path}: {e}")
        return False


def _add_musicbrainz_tags(audio, track_info, album_info, release):
    """Add MusicBrainz-specific TXXX frames to the audio tags."""
    mb_fields = [
        (
            track_info.get("foreignRecordingId"),
            "MusicBrainz Release Track Id",
        ),
        (
            release.get("foreignReleaseId"),
            "MusicBrainz Album Id",
        ),
        (
            album_info["artist"].get("foreignArtistId"),
            "MusicBrainz Artist Id",
        ),
        (
            album_info.get("foreignAlbumId"),
            "MusicBrainz Album Release Group Id",
        ),
        (
            release.get("country"),
            "MusicBrainz Release Country",
        ),
    ]
    for value, desc in mb_fields:
        if value:
            audio.tags.add(TXXX(encoding=3, desc=desc, text=value))


def create_xml_metadata(
    output_dir, artist, album, track_num, title,
    album_id=None, artist_id=None,
):
    """Create an XML sidecar file with track metadata for Lidarr import.

    Args:
        output_dir: Directory to write the XML file.
        artist: Artist name.
        album: Album name.
        track_num: Track number (int).
        title: Track title.
        album_id: Optional MusicBrainz album ID.
        artist_id: Optional MusicBrainz artist ID.

    Returns:
        True on success, False on failure.
    """
    try:
        sanitized_title = sanitize_filename(title)
        filename = f"{track_num:02d} - {sanitized_title}.xml"
        file_path = os.path.join(output_dir, filename)
        safe_title = xml_escape(title)
        safe_artist = xml_escape(artist)
        safe_album = xml_escape(album)
        mb_album = (
            f"  <musicbrainzalbumid>"
            f"{xml_escape(str(album_id))}"
            f"</musicbrainzalbumid>\n"
            if album_id
            else ""
        )
        mb_artist = (
            f"  <musicbrainzartistid>"
            f"{xml_escape(str(artist_id))}"
            f"</musicbrainzartistid>\n"
            if artist_id
            else ""
        )
        content = (
            f"<song>\n"
            f"  <title>{safe_title}</title>\n"
            f"  <artist>{safe_artist}</artist>\n"
            f"  <performingartist>{safe_artist}</performingartist>\n"
            f"  <albumartist>{safe_artist}</albumartist>\n"
            f"  <album>{safe_album}</album>\n"
            f"{mb_album}{mb_artist}</song>"
        )
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        logger.warning(f"Failed to create XML metadata: {e}")
        return False


def get_itunes_tracks(artist, album_name):
    """Look up album tracks from the iTunes Search API.

    Args:
        artist: Artist name to search for.
        album_name: Album name to search for.

    Returns:
        List of track dicts with trackNumber, title, previewUrl, hasFile.
        Returns an empty list on error or no results.
    """
    try:
        url = "https://itunes.apple.com/search"
        params = {
            "term": f"{artist} {album_name}",
            "entity": "album",
            "limit": 1,
        }
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("resultCount", 0) > 0:
            collection_id = data["results"][0]["collectionId"]
            lookup_url = "https://itunes.apple.com/lookup"
            lookup_params = {"id": collection_id, "entity": "song"}
            lookup_r = requests.get(
                lookup_url, params=lookup_params, timeout=10
            )
            lookup_data = lookup_r.json()
            tracks = []
            for item in lookup_data.get("results", [])[1:]:
                tracks.append(
                    {
                        "trackNumber": item.get("trackNumber"),
                        "title": item.get("trackName"),
                        "previewUrl": item.get("previewUrl"),
                        "hasFile": False,
                    }
                )
            return tracks
    except Exception as e:
        logger.debug(f"iTunes tracks lookup failed: {e}")
    return []


def get_itunes_artwork(artist, album):
    """Fetch high-resolution album artwork from the iTunes Search API.

    Iterates the top 10 results and returns the artwork from the first
    album whose artist name matches (case-insensitive substring) the
    requested artist. Replaces both ``100x100bb`` and ``100x100`` URL
    segments with ``3000x3000bb`` / ``3000x3000`` to fetch hi-res art.

    Args:
        artist: Artist name to search for.
        album: Album name to search for.

    Returns:
        Raw bytes of the artwork image, or None if not found.
    """
    try:
        url = "https://itunes.apple.com/search"
        params = {
            "term": f"{artist} {album}",
            "entity": "album",
            "limit": 10,
        }
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        artist_lower = artist.lower()
        for result in data.get("results", []):
            result_artist = result.get("artistName", "").lower()
            if (
                artist_lower in result_artist
                or result_artist in artist_lower
            ):
                artwork_url = (
                    result.get("artworkUrl100", "")
                    .replace("100x100bb", "3000x3000bb")
                    .replace("100x100", "3000x3000")
                )
                if artwork_url:
                    return requests.get(artwork_url, timeout=15).content
    except Exception as e:
        logger.debug(f"iTunes artwork lookup failed: {e}")
    return None


def get_album_artwork(artist, album, lidarr_cover_url=""):
    """Fetch album artwork: iTunes first, then Lidarr URL fallback.

    Args:
        artist: Artist name.
        album: Album name.
        lidarr_cover_url: Optional Lidarr cover URL to use as fallback.

    Returns:
        Raw bytes of artwork image, or None if not found.
    """
    cover = get_itunes_artwork(artist, album)
    if cover:
        return cover
    if lidarr_cover_url:
        try:
            r = requests.get(lidarr_cover_url, timeout=15)
            if r.status_code == 200 and r.content:
                logger.debug("Using Lidarr cover URL as artwork fallback")
                return r.content
        except Exception as e:
            logger.debug(f"Lidarr cover URL download failed: {e}")
    return None


def tag_m4a(file_path, track_info, album_info, cover_data):
    """Apply iTunes-style atom tags to an M4A file.

    Args:
        file_path: Path to the M4A file.
        track_info: Dict with title, trackNumber, foreignRecordingId.
        album_info: Dict with title, artist, releaseDate.
        cover_data: Raw bytes of cover art image, or None.

    Returns:
        True on success, False on failure.
    """
    try:
        audio = MP4(file_path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        tags["\u00a9nam"] = [track_info.get("title", "")]
        artist = album_info.get("artist", {}).get("artistName", "")
        tags["\u00a9ART"] = [artist]
        tags["aART"] = [artist]
        tags["\u00a9alb"] = [album_info.get("title", "")]
        release_date = album_info.get("releaseDate", "")
        if release_date:
            tags["\u00a9day"] = [release_date[:4]]
        try:
            track_num = int(track_info.get("trackNumber", 0))
            if track_num:
                tags["trkn"] = [(track_num, 0)]
        except (ValueError, KeyError):
            pass
        mb_id = track_info.get("foreignRecordingId")
        if mb_id:
            tags["----:com.apple.iTunes:MusicBrainz Track Id"] = [
                MP4FreeForm(mb_id.encode("utf-8"), AtomDataType.UTF8)
            ]
        if cover_data:
            tags["covr"] = [
                MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)
            ]
        audio.save()
        return True
    except Exception as e:
        logger.warning(f"Failed to tag M4A {file_path}: {e}")
        return False


def tag_opus(file_path, track_info, album_info, cover_data):
    """Apply VorbisComment tags to an Opus file.

    Args:
        file_path: Path to the Opus file.
        track_info: Dict with title, trackNumber, foreignRecordingId.
        album_info: Dict with title, artist, releaseDate.
        cover_data: Raw bytes of cover art image, or None.

    Returns:
        True on success, False on failure.
    """
    try:
        audio = OggOpus(file_path)
        audio["title"] = [track_info.get("title", "")]
        artist = album_info.get("artist", {}).get("artistName", "")
        audio["artist"] = [artist]
        audio["albumartist"] = [artist]
        audio["album"] = [album_info.get("title", "")]
        release_date = album_info.get("releaseDate", "")
        if release_date:
            audio["date"] = [release_date[:4]]
        try:
            track_num = int(track_info.get("trackNumber", 0))
            if track_num:
                audio["tracknumber"] = [str(track_num)]
        except (ValueError, KeyError):
            pass
        mb_id = track_info.get("foreignRecordingId")
        if mb_id:
            audio["musicbrainz_trackid"] = [mb_id]
        if cover_data:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = cover_data
            encoded = base64.b64encode(pic.write()).decode("ascii")
            audio["metadata_block_picture"] = [encoded]
        audio.save()
        return True
    except Exception as e:
        logger.warning(f"Failed to tag Opus {file_path}: {e}")
        return False


def tag_audio(file_path, track_info, album_info, cover_data):
    """Dispatch audio tagging based on file extension.

    Routes to tag_mp3, tag_m4a, or tag_opus depending on the file's
    extension. Returns False with a warning for unsupported formats.

    Args:
        file_path: Path to the audio file.
        track_info: Dict with track metadata.
        album_info: Dict with album metadata.
        cover_data: Raw bytes of cover art image, or None.

    Returns:
        True on success, False on failure or unsupported format.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".mp3":
        return tag_mp3(file_path, track_info, album_info, cover_data)
    if ext == ".m4a":
        return tag_m4a(file_path, track_info, album_info, cover_data)
    if ext == ".opus":
        return tag_opus(file_path, track_info, album_info, cover_data)
    logger.warning(
        "Tagging skipped — unsupported format: %s (%s)",
        ext or "unknown", file_path,
    )
    return False
