import pytest

from downloader import _check_forbidden, _is_official_channel, _title_similarity


class TestTitleSimilarity:
    def test_exact_match(self):
        score = _title_similarity("Artist Track", "Track", "Artist")
        assert score > 0.8

    def test_low_similarity(self):
        score = _title_similarity(
            "Completely Different", "Track", "Artist"
        )
        assert score < 0.5

    def test_contains_track_title_bonus(self):
        score_with = _title_similarity(
            "Something Track Name Here", "Track Name", "Other"
        )
        score_without = _title_similarity(
            "Something Else Here", "Track Name", "Other"
        )
        assert score_with > score_without

    def test_contains_artist_bonus(self):
        score_with = _title_similarity(
            "ArtistX plays a song", "Song", "ArtistX"
        )
        score_without = _title_similarity(
            "Someone plays a song", "Song", "ArtistX"
        )
        assert score_with > score_without

    def test_capped_at_one(self):
        score = _title_similarity(
            "Artist Track", "Track", "Artist"
        )
        assert score <= 1.0

    def test_empty_yt_title(self):
        score = _title_similarity("", "Track", "Artist")
        assert score >= 0.0


class TestIsOfficialChannel:
    def test_artist_name_match(self):
        assert _is_official_channel("ArtistName", "ArtistName") is True

    def test_vevo(self):
        assert _is_official_channel("ArtistVEVO", "Artist") is True

    def test_topic(self):
        assert _is_official_channel(
            "Artist - Topic", "Artist"
        ) is True

    def test_official_suffix(self):
        assert _is_official_channel(
            "Band Official", "Band"
        ) is True

    def test_false_for_random(self):
        assert _is_official_channel(
            "RandomChannel", "Artist"
        ) is False

    def test_none_channel(self):
        assert _is_official_channel(None, "Artist") is False

    def test_empty_channel(self):
        assert _is_official_channel("", "Artist") is False

    def test_case_insensitive(self):
        assert _is_official_channel("artistname", "ArtistName") is True


class TestCheckForbidden:
    def test_blocks_single_word(self):
        result = _check_forbidden(
            "song remix version", "song", ["remix", "cover"]
        )
        assert result == "remix"

    def test_allows_when_in_title(self):
        result = _check_forbidden(
            "remix song", "remix song", ["remix"]
        )
        assert result is None

    def test_multi_word_forbidden(self):
        result = _check_forbidden(
            "song dj mix version", "song", ["dj mix"]
        )
        assert result == "dj mix"

    def test_no_forbidden_match(self):
        result = _check_forbidden(
            "normal song title", "normal song", ["remix", "cover"]
        )
        assert result is None

    def test_word_boundary_respected(self):
        result = _check_forbidden(
            "covered in gold", "gold song", ["cover"]
        )
        assert result is None

    def test_multi_word_not_in_track(self):
        result = _check_forbidden(
            "track dj mix", "track", ["dj mix"]
        )
        assert result == "dj mix"

    def test_multi_word_in_track_allowed(self):
        result = _check_forbidden(
            "dj mix track", "dj mix track", ["dj mix"]
        )
        assert result is None

    def test_empty_forbidden_list(self):
        result = _check_forbidden("any title", "any title", [])
        assert result is None
