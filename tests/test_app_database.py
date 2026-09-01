"""Tests for the extended web application database."""

import os
import tempfile

import pytest

from backend.models.database import AppDatabase


@pytest.fixture
def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    database = AppDatabase(path)
    yield database
    os.unlink(path)


class TestAlbums:
    def test_upsert_and_get_album(self, db):
        album_id = db.upsert_album(
            source="qobuz",
            source_album_id="abc123",
            title="In Rainbows",
            artist="Radiohead",
            release_date="2007-10-10",
            quality="FLAC 24/44",
        )
        assert album_id > 0

        album = db.get_album(album_id)
        assert album["title"] == "In Rainbows"
        assert album["download_status"] == "not_downloaded"

    def test_upsert_updates_existing(self, db):
        id1 = db.upsert_album("qobuz", "abc123", "Old Title", "Artist")
        id2 = db.upsert_album("qobuz", "abc123", "New Title", "Artist")
        assert id1 == id2
        album = db.get_album(id1)
        assert album["title"] == "New Title"

    def test_upsert_preserves_added_to_library_at_when_omitted(self, db):
        album_id = db.upsert_album(
            "qobuz",
            "abc123",
            "Old Title",
            "Artist",
            added_to_library_at="2026-04-01T10:00:00",
        )

        db.upsert_album("qobuz", "abc123", "New Title", "Artist")

        album = db.get_album(album_id)
        assert album["added_to_library_at"] == "2026-04-01T10:00:00"

    def test_get_albums_with_filter(self, db):
        db.upsert_album("qobuz", "a1", "Album A", "Artist A")
        db.upsert_album("qobuz", "a2", "Album B", "Artist B")
        db.update_album_status(
            db.get_album_by_source_id("qobuz", "a1")["id"],
            "complete",
        )

        complete = db.get_albums("qobuz", status="complete")
        assert len(complete) == 1
        assert complete[0]["title"] == "Album A"

    def test_get_albums_with_search(self, db):
        db.upsert_album("qobuz", "a1", "In Rainbows", "Radiohead")
        db.upsert_album("qobuz", "a2", "Kid A", "Radiohead")
        db.upsert_album("qobuz", "a3", "Blue Train", "John Coltrane")

        results = db.get_albums("qobuz", search="Radiohead")
        assert len(results) == 2

    def test_count_albums(self, db):
        db.upsert_album("qobuz", "a1", "A", "B")
        db.upsert_album("qobuz", "a2", "C", "D")
        db.upsert_album("tidal", "a3", "E", "F")

        assert db.count_albums("qobuz") == 2
        assert db.count_albums("tidal") == 1


class TestUpsertAlbums:
    def test_batch_upsert_matches_sequential_upserts(self, db):
        """upsert_albums(rows) must produce the same rows (same ids, same
        values) as calling upsert_album once per row (#25)."""
        rows = [
            {
                "source": "qobuz",
                "source_album_id": "a1",
                "title": "Album A",
                "artist": "Artist A",
                "track_count": 10,
            },
            {
                "source": "qobuz",
                "source_album_id": "a2",
                "title": "Album B",
                "artist": "Artist B",
                "track_count": 12,
            },
            {
                "source": "qobuz",
                "source_album_id": "a3",
                "title": "Album C",
                "artist": "Artist C",
                "track_count": 8,
            },
        ]

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            sequential_path = f.name
        sequential_db = AppDatabase(sequential_path)
        try:
            for row in rows:
                sequential_db.upsert_album(**row)

            db.upsert_albums(rows)

            for row in rows:
                batch_album = db.get_album_by_source_id(
                    row["source"], row["source_album_id"]
                )
                sequential_album = sequential_db.get_album_by_source_id(
                    row["source"], row["source_album_id"]
                )
                assert batch_album is not None
                assert sequential_album is not None
                assert batch_album["id"] == sequential_album["id"]
                assert batch_album["title"] == sequential_album["title"] == row["title"]
                assert (
                    batch_album["artist"] == sequential_album["artist"] == row["artist"]
                )
                assert (
                    batch_album["track_count"]
                    == sequential_album["track_count"]
                    == row["track_count"]
                )
        finally:
            os.unlink(sequential_path)

    def test_batch_upsert_updates_existing_album(self, db):
        db.upsert_album("qobuz", "a1", "Old Title", "Artist")
        db.upsert_albums(
            [
                {
                    "source": "qobuz",
                    "source_album_id": "a1",
                    "title": "New Title",
                    "artist": "Artist",
                }
            ]
        )
        album = db.get_album_by_source_id("qobuz", "a1")
        assert album["title"] == "New Title"

    def test_batch_upsert_preserves_added_to_library_at_when_omitted(self, db):
        db.upsert_album(
            "qobuz",
            "a1",
            "Old Title",
            "Artist",
            added_to_library_at="2026-04-01T10:00:00",
        )
        db.upsert_albums(
            [
                {
                    "source": "qobuz",
                    "source_album_id": "a1",
                    "title": "New Title",
                    "artist": "Artist",
                }
            ]
        )
        album = db.get_album_by_source_id("qobuz", "a1")
        assert album["added_to_library_at"] == "2026-04-01T10:00:00"

    def test_batch_upsert_empty_list_is_noop(self, db):
        db.upsert_albums([])
        assert db.count_albums("qobuz") == 0


class TestTracks:
    def test_upsert_and_get_tracks(self, db):
        album_id = db.upsert_album("qobuz", "a1", "Album", "Artist")
        db.upsert_track(album_id, "t1", "Track 1", "Artist", track_number=1)
        db.upsert_track(album_id, "t2", "Track 2", "Artist", track_number=2)

        tracks = db.get_tracks(album_id)
        assert len(tracks) == 2
        assert tracks[0]["title"] == "Track 1"
        assert tracks[1]["title"] == "Track 2"

    def test_update_track_status(self, db):
        album_id = db.upsert_album("qobuz", "a1", "Album", "Artist")
        track_id = db.upsert_track(album_id, "t1", "Track", "Artist")
        db.update_track_status(
            track_id, "complete", "/music/track.flac", "FLAC", 24, 96000
        )

        tracks = db.get_tracks(album_id)
        assert tracks[0]["download_status"] == "complete"
        assert tracks[0]["file_path"] == "/music/track.flac"

    def test_update_track_status_preserves_file_metadata(self, db):
        """DownloadService updates only the status, so the file metadata a
        previous call recorded must not be NULLed out."""
        album_id = db.upsert_album("qobuz", "a1", "Album", "Artist")
        track_id = db.upsert_track(album_id, "t1", "Track", "Artist")
        db.update_track_status(
            track_id, "complete", "/music/track.flac", "FLAC", 24, 96000
        )

        db.update_track_status(track_id, "complete")

        track = db.get_tracks(album_id)[0]
        assert track["download_status"] == "complete"
        assert track["file_path"] == "/music/track.flac"
        assert track["format"] == "FLAC"
        assert track["bit_depth"] == 24
        assert track["sample_rate"] == 96000


class TestSyncRuns:
    def test_create_and_complete_sync_run(self, db):
        run_id = db.create_sync_run("qobuz")
        db.complete_sync_run(
            run_id,
            albums_found=100,
            albums_new=5,
            albums_removed=1,
            albums_downloaded=4,
        )

        history = db.get_sync_history("qobuz")
        assert len(history) == 1
        assert history[0]["albums_new"] == 5
        assert history[0]["status"] == "complete"

    def test_fail_sync_run(self, db):
        run_id = db.create_sync_run("qobuz")
        db.fail_sync_run(run_id)

        history = db.get_sync_history("qobuz")
        assert len(history) == 1
        assert history[0]["status"] == "failed"
        assert history[0]["completed_at"] is not None


class TestConfig:
    def test_set_and_get_config(self, db):
        db.set_config("qobuz.quality", "3")
        assert db.get_config("qobuz.quality") == "3"

    def test_get_all_config(self, db):
        db.set_config("qobuz.quality", "3")
        db.set_config("downloads.path", "/music")
        cfg = db.get_all_config()
        assert cfg["qobuz.quality"] == "3"
        assert cfg["downloads.path"] == "/music"

    def test_upsert_config(self, db):
        db.set_config("key", "old")
        db.set_config("key", "new")
        assert db.get_config("key") == "new"


class TestDownloadPathMetadata:
    def test_update_album_resolved_metadata_preserves_other_columns(self, db):
        """The download path resolves only title/artist/track_count.

        Routing it through ``upsert_album`` set every omitted column to the
        None of its default kwarg, wiping cover art and metadata off every
        album as soon as its download finished.
        """
        album_id = db.upsert_album(
            source="qobuz",
            source_album_id="abc123",
            title="Placeholder",
            artist="Placeholder",
            release_date="2007-10-10",
            label="XL Recordings",
            genre="Alternative",
            track_count=10,
            duration_seconds=2718,
            cover_url="https://example/cover.jpg",
            quality="FLAC 24/96kHz",
            bit_depth=24,
            sample_rate=96.0,
            added_to_library_at="2026-04-01T10:00:00",
        )

        db.update_album_resolved_metadata(
            album_id, title="In Rainbows", artist="Radiohead", track_count=15
        )

        album = db.get_album(album_id)
        assert album["title"] == "In Rainbows"
        assert album["artist"] == "Radiohead"
        assert album["track_count"] == 15
        assert album["cover_url"] == "https://example/cover.jpg"
        assert album["release_date"] == "2007-10-10"
        assert album["label"] == "XL Recordings"
        assert album["genre"] == "Alternative"
        assert album["duration_seconds"] == 2718
        assert album["quality"] == "FLAC 24/96kHz"
        assert album["bit_depth"] == 24
        assert album["sample_rate"] == 96.0
        assert album["added_to_library_at"] == "2026-04-01T10:00:00"

    def test_upsert_album_still_overwrites_metadata(self, db):
        """Guard the other half of the contract: sync relies on upsert
        replacing stale metadata, so that behaviour must not change."""
        album_id = db.upsert_album(
            "qobuz", "abc123", "T", "A", label="Old Label", genre="Old Genre"
        )
        db.upsert_album("qobuz", "abc123", "T", "A", label="New Label")

        album = db.get_album(album_id)
        assert album["label"] == "New Label"
        assert album["genre"] is None


class TestStatusFilterSentinel:
    def test_count_albums_treats_all_as_no_filter(self, db):
        """get_albums skips the filter for "all"; count_albums used to
        apply download_status = 'all', so pagination totals read 0."""
        db.upsert_album("qobuz", "a1", "A", "B")
        db.upsert_album("qobuz", "a2", "C", "D")
        db.update_album_status(
            db.get_album_by_source_id("qobuz", "a1")["id"], "complete"
        )

        assert len(db.get_albums("qobuz", status="all")) == 2
        assert db.count_albums("qobuz", status="all") == 2

    def test_helpers_agree_for_every_status(self, db):
        db.upsert_album("qobuz", "a1", "A", "B")
        db.upsert_album("qobuz", "a2", "C", "D")
        db.upsert_album("qobuz", "a3", "E", "F")
        db.update_album_status(
            db.get_album_by_source_id("qobuz", "a1")["id"], "complete"
        )
        db.update_album_status(db.get_album_by_source_id("qobuz", "a2")["id"], "failed")

        for status in (None, "all", "complete", "failed", "not_downloaded", "queued"):
            assert db.count_albums("qobuz", status=status) == len(
                db.get_albums("qobuz", status=status)
            ), status
