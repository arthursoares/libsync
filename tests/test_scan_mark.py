"""mark_album_downloaded / unmark_album_downloaded primitives."""

import json
import os
import sqlite3

import pytest

from backend.models.database import AppDatabase
from backend.services.scan import (
    mark_album_downloaded,
    unmark_album_downloaded,
)

TRACK_IDS = ("t1", "t2")


@pytest.fixture
def db(tmp_path):
    d = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = d.upsert_album(
        source="qobuz",
        source_album_id="42",
        title="Abbey Road",
        artist="The Beatles",
        track_count=2,
        bit_depth=24,
        sample_rate=96.0,
    )
    d.upsert_track(
        album_id=album_id,
        source_track_id="t1",
        title="Come Together",
        artist="The Beatles",
        track_number=1,
    )
    d.upsert_track(
        album_id=album_id,
        source_track_id="t2",
        title="Something",
        artist="The Beatles",
        track_number=2,
    )
    return d, album_id


def _dedup_path(tmp_path, source):
    fname = "downloads.db" if source == "qobuz" else f"downloads-{source}.db"
    return str(tmp_path / fname)


def _dedup_rows(path):
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT * FROM downloads").fetchall()
        return rows
    finally:
        conn.close()


def test_mark_updates_db_and_writes_sentinel(tmp_path, db):
    d, album_id = db
    folder = tmp_path / "music" / "Beatles - Abbey Road"
    folder.mkdir(parents=True)

    mark_album_downloaded(
        d,
        album_id,
        local_folder_path=str(folder),
        dedup_db_dir=str(tmp_path),
        track_ids=TRACK_IDS,
        sentinel_write_enabled=True,
    )

    row = d.get_album(album_id)
    assert row["download_status"] == "complete"
    assert row["downloaded_at"] is not None
    assert row["local_folder_path"] == str(folder)

    sentinel = folder / ".streamrip.json"
    assert sentinel.exists()
    payload = json.loads(sentinel.read_text())
    assert payload["source"] == "qobuz"
    assert payload["album_id"] == "42"
    assert payload["title"] == "Abbey Road"
    assert payload["tracks_count"] == 2


def test_mark_populates_dedup_db(tmp_path, db):
    d, album_id = db
    folder = tmp_path / "music" / "X"
    folder.mkdir(parents=True)

    mark_album_downloaded(
        d,
        album_id,
        local_folder_path=str(folder),
        dedup_db_dir=str(tmp_path),
        track_ids=TRACK_IDS,
        sentinel_write_enabled=False,
    )

    rows = _dedup_rows(_dedup_path(tmp_path, "qobuz"))
    # Two tracks were added in the fixture.
    assert len(rows) == 2


def test_mark_without_folder_is_db_only(tmp_path, db):
    d, album_id = db
    mark_album_downloaded(
        d,
        album_id,
        local_folder_path=None,
        dedup_db_dir=str(tmp_path),
        track_ids=TRACK_IDS,
        sentinel_write_enabled=True,
    )
    row = d.get_album(album_id)
    assert row["download_status"] == "complete"
    assert row["local_folder_path"] is None


def test_mark_idempotent(tmp_path, db):
    d, album_id = db
    folder = tmp_path / "music" / "X"
    folder.mkdir(parents=True)

    mark_album_downloaded(
        d,
        album_id,
        local_folder_path=str(folder),
        dedup_db_dir=str(tmp_path),
        track_ids=TRACK_IDS,
        sentinel_write_enabled=True,
    )
    mark_album_downloaded(
        d,
        album_id,
        local_folder_path=str(folder),
        dedup_db_dir=str(tmp_path),
        track_ids=TRACK_IDS,
        sentinel_write_enabled=True,
    )

    rows = _dedup_rows(_dedup_path(tmp_path, "qobuz"))
    # No duplicate rows despite two calls.
    assert len(rows) == 2


def test_mark_graceful_on_readonly_folder(tmp_path, db, caplog):
    d, album_id = db
    folder = tmp_path / "music" / "X"
    folder.mkdir(parents=True)
    folder.chmod(0o555)  # read + execute, no write

    try:
        mark_album_downloaded(
            d,
            album_id,
            local_folder_path=str(folder),
            dedup_db_dir=str(tmp_path),
            track_ids=TRACK_IDS,
            sentinel_write_enabled=True,
        )
    finally:
        folder.chmod(0o755)

    # DB still updated despite sentinel write failure.
    row = d.get_album(album_id)
    assert row["download_status"] == "complete"
    assert not (folder / ".streamrip.json").exists()


def test_mark_skips_sentinel_when_disabled(tmp_path, db):
    d, album_id = db
    folder = tmp_path / "music" / "X"
    folder.mkdir(parents=True)

    mark_album_downloaded(
        d,
        album_id,
        local_folder_path=str(folder),
        dedup_db_dir=str(tmp_path),
        track_ids=TRACK_IDS,
        sentinel_write_enabled=False,
    )
    assert not (folder / ".streamrip.json").exists()
    assert d.get_album(album_id)["download_status"] == "complete"


def test_unmark_reverses_everything(tmp_path, db):
    d, album_id = db
    folder = tmp_path / "music" / "X"
    folder.mkdir(parents=True)

    mark_album_downloaded(
        d,
        album_id,
        local_folder_path=str(folder),
        dedup_db_dir=str(tmp_path),
        track_ids=TRACK_IDS,
        sentinel_write_enabled=True,
    )
    unmark_album_downloaded(
        d, album_id, dedup_db_dir=str(tmp_path), track_ids=TRACK_IDS
    )

    row = d.get_album(album_id)
    assert row["download_status"] == "not_downloaded"
    assert row["downloaded_at"] is None
    assert row["local_folder_path"] is None
    assert not (folder / ".streamrip.json").exists()
    assert _dedup_rows(_dedup_path(tmp_path, "qobuz")) == []


def test_dedup_db_opened_in_wal_mode(tmp_path, db):
    """The download service writes the dedup DBs while a scan runs — WAL plus
    a busy timeout is what keeps the two off each other's locks."""
    d, album_id = db
    mark_album_downloaded(
        d,
        album_id,
        local_folder_path=None,
        dedup_db_dir=str(tmp_path),
        track_ids=TRACK_IDS,
        sentinel_write_enabled=False,
    )

    conn = sqlite3.connect(_dedup_path(tmp_path, "qobuz"))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_mark_rejects_empty_track_ids_before_any_mutation(tmp_path, db):
    d, album_id = db
    folder = tmp_path / "music" / "X"
    folder.mkdir(parents=True)

    with pytest.raises(ValueError, match="non-empty track identity"):
        mark_album_downloaded(
            d,
            album_id,
            local_folder_path=str(folder),
            dedup_db_dir=str(tmp_path),
            track_ids=(),
        )

    assert d.get_album(album_id)["download_status"] == "not_downloaded"
    assert not (folder / ".streamrip.json").exists()
    assert not os.path.exists(_dedup_path(tmp_path, "qobuz"))


def test_unmark_removes_current_and_historical_cached_ids(tmp_path, db):
    d, album_id = db
    d.upsert_track(album_id, "historical", "Old Track", "The Beatles")
    dedup_path = _dedup_path(tmp_path, "qobuz")
    conn = sqlite3.connect(dedup_path)
    try:
        conn.execute("CREATE TABLE downloads (id TEXT PRIMARY KEY)")
        conn.executemany(
            "INSERT INTO downloads (id) VALUES (?)",
            [("t1",), ("t2",), ("historical",)],
        )
        conn.commit()
    finally:
        conn.close()
    d.set_album_download_state(album_id, downloaded_at="2026-09-05T00:00:00")

    unmark_album_downloaded(
        d,
        album_id,
        dedup_db_dir=str(tmp_path),
        track_ids=TRACK_IDS,
    )

    assert _dedup_rows(dedup_path) == []
    assert d.get_album(album_id)["download_status"] == "not_downloaded"


def test_sentinel_write_failure_logs_after_mandatory_state_commits(
    tmp_path, db, monkeypatch, caplog
):
    d, album_id = db
    folder = tmp_path / "music" / "X"
    folder.mkdir(parents=True)

    def fail_open(*args, **kwargs):
        raise PermissionError("read only")

    monkeypatch.setattr("builtins.open", fail_open)
    mark_album_downloaded(
        d,
        album_id,
        local_folder_path=str(folder),
        dedup_db_dir=str(tmp_path),
        track_ids=TRACK_IDS,
    )

    assert d.get_album(album_id)["download_status"] == "complete"
    assert set(_dedup_rows(_dedup_path(tmp_path, "qobuz"))) == {("t1",), ("t2",)}
    assert "could not write sentinel" in caplog.text


def test_sentinel_delete_failure_logs_after_mandatory_state_commits(
    tmp_path, db, monkeypatch, caplog
):
    import backend.services.scan as scan_mod

    d, album_id = db
    folder = tmp_path / "music" / "X"
    folder.mkdir(parents=True)
    sentinel = folder / ".streamrip.json"
    sentinel.write_text("existing")
    d.set_album_download_state(
        album_id,
        downloaded_at="2026-09-05T00:00:00",
        local_folder_path=str(folder),
    )
    dedup_path = _dedup_path(tmp_path, "qobuz")
    conn = sqlite3.connect(dedup_path)
    try:
        conn.execute("CREATE TABLE downloads (id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO downloads (id) VALUES (?)", [("t1",), ("t2",)])
        conn.commit()
    finally:
        conn.close()

    def fail_remove(*args, **kwargs):
        raise PermissionError("read only")

    monkeypatch.setattr(scan_mod.os, "remove", fail_remove)
    unmark_album_downloaded(
        d,
        album_id,
        dedup_db_dir=str(tmp_path),
        track_ids=TRACK_IDS,
    )

    assert d.get_album(album_id)["download_status"] == "not_downloaded"
    assert _dedup_rows(dedup_path) == []
    assert sentinel.exists()
    assert "could not remove sentinel" in caplog.text
