import pytest

import db
import models


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr("db.DB_PATH", db_path)
    db.init_db()
    yield db_path
    db.close_db()


# --- History ---


def test_add_and_get_history():
    models.add_history_entry(1, "Album1", "Artist1", True, False)
    result = models.get_history(page=1, per_page=50)
    assert result["total"] == 1
    assert result["items"][0]["album_title"] == "Album1"


def test_history_pagination():
    for i in range(75):
        models.add_history_entry(i, f"Album{i}", "Artist", True, False)
    page1 = models.get_history(page=1, per_page=50)
    page2 = models.get_history(page=2, per_page=50)
    assert page1["total"] == 75
    assert page1["pages"] == 2
    assert len(page1["items"]) == 50
    assert len(page2["items"]) == 25


def test_history_ordered_newest_first():
    models.add_history_entry(1, "Old", "A", True, False)
    models.add_history_entry(2, "New", "A", True, False)
    result = models.get_history()
    assert result["items"][0]["album_title"] == "New"


def test_clear_history():
    models.add_history_entry(1, "A", "A", True, False)
    models.clear_history()
    assert models.get_history()["total"] == 0


def test_history_manual_fields():
    models.add_history_entry(
        1, "A", "A", True, False, manual=True, track_title="Track1"
    )
    item = models.get_history()["items"][0]
    assert item["manual"] is True
    assert item["track_title"] == "Track1"


# --- Logs ---


def test_add_and_get_logs():
    models.add_log("download_success", 1, "Album", "Artist", details="ok")
    result = models.get_logs(page=1, per_page=50)
    assert result["total"] == 1
    assert result["items"][0]["type"] == "download_success"


def test_log_failed_tracks_json():
    tracks = [{"title": "T1", "reason": "fail", "track_num": 1}]
    models.add_log("partial_success", 1, "A", "A", failed_tracks=tracks)
    item = models.get_logs()["items"][0]
    assert isinstance(item["failed_tracks"], list)
    assert item["failed_tracks"][0]["title"] == "T1"


def test_delete_log():
    models.add_log("download_success", 1, "A", "A")
    log_id = models.get_logs()["items"][0]["id"]
    assert models.delete_log(log_id) is True
    assert models.get_logs()["total"] == 0


def test_delete_log_not_found():
    assert models.delete_log("nonexistent") is False


def test_clear_logs():
    models.add_log("download_success", 1, "A", "A")
    models.clear_logs()
    assert models.get_logs()["total"] == 0


# --- Failed tracks ---


def test_save_and_get_failed_tracks():
    tracks = [{"title": "T1", "track_num": 1, "reason": "no match"}]
    models.save_failed_tracks(
        1, "Album", "Artist", "http://cover", "/path", "/lidarr", tracks
    )
    result = models.get_failed_tracks()
    assert len(result) == 1
    assert result[0]["track_title"] == "T1"
    assert result[0]["album_title"] == "Album"


def test_clear_failed_tracks():
    tracks = [{"title": "T1", "track_num": 1, "reason": "no match"}]
    models.save_failed_tracks(1, "A", "A", "", "", "", tracks)
    models.clear_failed_tracks()
    assert len(models.get_failed_tracks()) == 0


def test_save_failed_tracks_replaces_previous():
    tracks1 = [{"title": "T1", "track_num": 1, "reason": "fail"}]
    tracks2 = [{"title": "T2", "track_num": 2, "reason": "fail2"}]
    models.save_failed_tracks(1, "A", "A", "", "", "", tracks1)
    models.save_failed_tracks(2, "B", "B", "", "", "", tracks2)
    result = models.get_failed_tracks()
    assert len(result) == 1
    assert result[0]["track_title"] == "T2"


# --- Queue ---


def test_enqueue_and_get_queue():
    models.enqueue_album(10)
    models.enqueue_album(20)
    queue = models.get_queue()
    assert len(queue) == 2
    assert queue[0]["album_id"] == 10
    assert queue[1]["album_id"] == 20


def test_enqueue_duplicate_ignored():
    models.enqueue_album(10)
    models.enqueue_album(10)
    assert len(models.get_queue()) == 1


def test_dequeue_album():
    models.enqueue_album(10)
    models.enqueue_album(20)
    models.dequeue_album(10)
    queue = models.get_queue()
    assert len(queue) == 1
    assert queue[0]["album_id"] == 20


def test_set_queue_status():
    models.enqueue_album(10)
    models.set_queue_status(10, models.QUEUE_STATUS_DOWNLOADING)
    queue = models.get_queue()
    assert queue[0]["status"] == "downloading"


def test_set_queue_status_invalid():
    models.enqueue_album(10)
    with pytest.raises(ValueError):
        models.set_queue_status(10, "invalid_status")


def test_reset_downloading_to_queued():
    models.enqueue_album(10)
    models.set_queue_status(10, models.QUEUE_STATUS_DOWNLOADING)
    models.reset_downloading_to_queued()
    queue = models.get_queue()
    assert queue[0]["status"] == "queued"


def test_clear_queue():
    models.enqueue_album(10)
    models.clear_queue()
    assert len(models.get_queue()) == 0


def test_get_history_count_today():
    models.add_history_entry(1, "A", "A", True, False)
    models.add_history_entry(2, "B", "B", False, False)
    assert models.get_history_count_today() == 1
