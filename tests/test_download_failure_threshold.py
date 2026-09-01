"""Tests for the 80% success-rate threshold in DownloadService._download_album.

When ``result.success_rate < 0.8``, the service raises ``RuntimeError``
which propagates out of ``_download_album`` and is caught by
``_process_queue``, marking the queue item ``failed`` and persisting
``failed`` (plus a timestamp) on the album DB row.

These tests inject a fake ``qobuz.AlbumDownloader`` so we never touch
the network or run the real download pipeline — we just verify the
threshold logic and the queue/DB state changes.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.models.database import AppDatabase
from backend.services.download import DownloadService
from backend.services.event_bus import EventBus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    database = AppDatabase(path)
    yield database
    os.unlink(path)


@pytest.fixture
def event_bus():
    return EventBus()


# ---------------------------------------------------------------------------
# Fake AlbumResult / TrackResult — minimal shape the service reads
# ---------------------------------------------------------------------------


@dataclass
class FakeTrackResult:
    track_id: int
    title: str
    success: bool
    path: str | None = None
    error: str | None = None


@dataclass
class FakeAlbumResult:
    """Mimics the shape of qobuz.AlbumResult that DownloadService reads.

    The service touches: total, successful, success_rate, tracks, title,
    artist.  AlbumDownloader returns a real dataclass; we fake it.
    """

    total: int
    successful: int
    title: str = "Test Album"
    artist: str = "Test Artist"
    tracks: list[FakeTrackResult] = field(default_factory=list)
    cover_path: str | None = None
    booklet_paths: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.successful / self.total if self.total > 0 else 0.0


def _make_fake_downloader_returning(result: FakeAlbumResult):
    """Build a callable replacing the qobuz.AlbumDownloader class.

    The callable takes (client, config, **callbacks) and returns an
    object whose async ``download(album_id)`` resolves to ``result``.
    """

    def _fake_class(
        client,
        config,
        *,
        on_track_start=None,
        on_track_progress=None,
        on_track_complete=None,
    ):
        instance = MagicMock()
        instance.download = AsyncMock(return_value=result)
        return instance

    return _fake_class


def _make_qobuz_client():
    """Bare client mock — _download_album never calls anything on it
    directly, the (mocked) AlbumDownloader does."""
    client = MagicMock()
    client.catalog = MagicMock()
    return client


def _seed_album(db, source: str, source_album_id: str) -> int:
    db_id = db.upsert_album(
        source=source,
        source_album_id=source_album_id,
        title="Test Album",
        artist="Test Artist",
        track_count=10,
    )
    return db_id


def _make_queue_item(
    db, source: str, source_album_id: str, force: bool = False
) -> dict:
    db_id = _seed_album(db, source, source_album_id)
    return {
        "id": "queue-id-1",
        "album_db_id": db_id,
        "source": source,
        "source_album_id": source_album_id,
        "title": "Test Album",
        "artist": "Test Artist",
        "cover_url": None,
        "track_count": 10,
        "tracks_done": 0,
        "bytes_done": 0,
        "bytes_total": 0,
        "speed": 0.0,
        "status": "downloading",
        "force": force,
    }


# ---------------------------------------------------------------------------
# 80% threshold tests
# ---------------------------------------------------------------------------


class TestSuccessThreshold:
    async def test_below_80_percent_raises_runtime_error(self, db, event_bus):
        """0/10 should raise — the album is treated as failed."""
        result = FakeAlbumResult(
            total=10,
            successful=0,
            tracks=[
                FakeTrackResult(track_id=i, title=f"T{i}", success=False)
                for i in range(10)
            ],
        )
        client = _make_qobuz_client()
        service = DownloadService(
            db, event_bus, clients={"qobuz": client}, download_path="/tmp"
        )
        item = _make_queue_item(db, "qobuz", "fail-album")

        fake_downloader = _make_fake_downloader_returning(result)
        with patch("qobuz.AlbumDownloader", new=fake_downloader):
            with pytest.raises(RuntimeError, match=r"0/10.*0%.*below 80%"):
                await service._download_album(item)

    async def test_exactly_threshold_passes(self, db, event_bus):
        """8/10 = 80% should NOT raise — the threshold is strictly < 0.8."""
        result = FakeAlbumResult(
            total=10,
            successful=8,
            tracks=[
                FakeTrackResult(track_id=i, title=f"T{i}", success=(i < 8))
                for i in range(10)
            ],
        )
        client = _make_qobuz_client()
        service = DownloadService(
            db, event_bus, clients={"qobuz": client}, download_path="/tmp"
        )
        item = _make_queue_item(db, "qobuz", "exact-threshold")

        fake_downloader = _make_fake_downloader_returning(result)
        with patch("qobuz.AlbumDownloader", new=fake_downloader):
            # Must NOT raise
            await service._download_album(item)

    async def test_just_below_threshold_raises(self, db, event_bus):
        """7/10 = 70% must raise."""
        result = FakeAlbumResult(
            total=10,
            successful=7,
            tracks=[
                FakeTrackResult(track_id=i, title=f"T{i}", success=(i < 7))
                for i in range(10)
            ],
        )
        client = _make_qobuz_client()
        service = DownloadService(
            db, event_bus, clients={"qobuz": client}, download_path="/tmp"
        )
        item = _make_queue_item(db, "qobuz", "below-threshold")

        fake_downloader = _make_fake_downloader_returning(result)
        with patch("qobuz.AlbumDownloader", new=fake_downloader):
            with pytest.raises(RuntimeError, match="below 80%"):
                await service._download_album(item)

    async def test_one_failed_track_in_otherwise_full_album_passes(self, db, event_bus):
        """9/10 = 90% should NOT raise — a single bad track is tolerable."""
        result = FakeAlbumResult(
            total=10,
            successful=9,
            tracks=[
                FakeTrackResult(track_id=i, title=f"T{i}", success=(i != 5))
                for i in range(10)
            ],
        )
        client = _make_qobuz_client()
        service = DownloadService(
            db, event_bus, clients={"qobuz": client}, download_path="/tmp"
        )
        item = _make_queue_item(db, "qobuz", "single-fail")

        fake_downloader = _make_fake_downloader_returning(result)
        with patch("qobuz.AlbumDownloader", new=fake_downloader):
            # Must NOT raise
            await service._download_album(item)

    async def test_zero_total_does_not_raise(self, db, event_bus):
        """An empty album (total=0) should not crash on the threshold check."""
        result = FakeAlbumResult(total=0, successful=0, tracks=[])
        client = _make_qobuz_client()
        service = DownloadService(
            db, event_bus, clients={"qobuz": client}, download_path="/tmp"
        )
        item = _make_queue_item(db, "qobuz", "empty-album")

        fake_downloader = _make_fake_downloader_returning(result)
        with patch("qobuz.AlbumDownloader", new=fake_downloader):
            # The guard `if result.total > 0` skips the check entirely
            await service._download_album(item)


# ---------------------------------------------------------------------------
# Queue-level: failure marks the queue item AND reverts the DB row
# ---------------------------------------------------------------------------


def _build_queue_item_in_place(service, db, source: str, source_album_id: str):
    """Build a queue item directly without going through enqueue().

    enqueue() auto-starts a worker that races with our test patches —
    this helper just appends a pending item to the queue and seeds the
    DB row, leaving the worker for the test to drive manually.
    """
    db_id = _seed_album(db, source, source_album_id)
    db.update_album_status(db_id, "queued")
    item = {
        "id": f"queue-{source_album_id}",
        "album_db_id": db_id,
        "source": source,
        "source_album_id": source_album_id,
        "title": "Test Album",
        "artist": "Test Artist",
        "cover_url": None,
        "track_count": 10,
        "tracks_done": 0,
        "bytes_done": 0,
        "bytes_total": 0,
        "speed": 0.0,
        "status": "pending",
        "force": False,
    }
    service._queue.append(item)
    return item, db_id


class TestProcessQueueHandlesFailure:
    async def test_failed_download_marks_queue_item_failed(self, db, event_bus):
        """When _download_album raises, _process_queue must mark the item
        as failed and persist 'failed' on the album DB row."""
        result = FakeAlbumResult(
            total=10,
            successful=2,
            tracks=[
                FakeTrackResult(track_id=i, title=f"T{i}", success=(i < 2))
                for i in range(10)
            ],
        )
        client = _make_qobuz_client()
        service = DownloadService(
            db, event_bus, clients={"qobuz": client}, download_path="/tmp"
        )

        item, db_id = _build_queue_item_in_place(service, db, "qobuz", "queue-fail")
        assert db.get_album(db_id)["download_status"] == "queued"

        fake_downloader = _make_fake_downloader_returning(result)
        with patch("qobuz.AlbumDownloader", new=fake_downloader):
            await service._process_queue()

        # Queue item should be marked failed
        queue = service.get_queue()
        failed_items = [q for q in queue if q["id"] == item["id"]]
        assert failed_items[0]["status"] == "failed"

        # And the album DB row should record the failure, with a timestamp so
        # get_recent_downloads still finds it after a restart.
        album_after = db.get_album(db_id)
        assert album_after["download_status"] == "failed"
        assert album_after["downloaded_at"] is not None

    async def test_successful_download_marks_queue_item_complete(self, db, event_bus):
        """A 100% successful download must mark the queue item complete
        and the album row complete."""
        result = FakeAlbumResult(
            total=4,
            successful=4,
            tracks=[
                FakeTrackResult(
                    track_id=i, title=f"T{i}", success=True, path=f"/x/{i}.flac"
                )
                for i in range(4)
            ],
        )
        client = _make_qobuz_client()
        service = DownloadService(
            db, event_bus, clients={"qobuz": client}, download_path="/tmp"
        )

        item, db_id = _build_queue_item_in_place(service, db, "qobuz", "queue-success")

        fake_downloader = _make_fake_downloader_returning(result)
        with patch("qobuz.AlbumDownloader", new=fake_downloader):
            await service._process_queue()

        queue = service.get_queue()
        ok_items = [q for q in queue if q["id"] == item["id"]]
        assert ok_items[0]["status"] == "complete"

        album_after = db.get_album(db_id)
        assert album_after["download_status"] == "complete"


# ---------------------------------------------------------------------------
# The completed-download write-back must not wipe the rest of the album row
# ---------------------------------------------------------------------------


class TestCompletedDownloadPreservesMetadata:
    async def test_download_keeps_cover_and_metadata(self, db, event_bus):
        """A finished download resolves only title/artist/track_count.

        It used to write those back through ``upsert_album``, whose
        ``DO UPDATE`` set every omitted column to NULL — so every album lost
        its cover art and metadata the moment it finished downloading.
        """
        db_id = db.upsert_album(
            source="qobuz",
            source_album_id="rich-album",
            title="Placeholder Title",
            artist="Placeholder Artist",
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

        result = FakeAlbumResult(
            total=4,
            successful=4,
            title="In Rainbows",
            artist="Radiohead",
            tracks=[
                FakeTrackResult(
                    track_id=i, title=f"T{i}", success=True, path=f"/x/{i}.flac"
                )
                for i in range(4)
            ],
        )
        client = _make_qobuz_client()
        service = DownloadService(
            db, event_bus, clients={"qobuz": client}, download_path="/tmp"
        )
        item = {
            "id": "queue-rich",
            "album_db_id": db_id,
            "source": "qobuz",
            "source_album_id": "rich-album",
            "title": "Placeholder Title",
            "artist": "Placeholder Artist",
            "cover_url": "https://example/cover.jpg",
            "track_count": 10,
            "tracks_done": 0,
            "bytes_done": 0,
            "bytes_total": 0,
            "speed": 0.0,
            "status": "downloading",
            "force": False,
        }

        fake_downloader = _make_fake_downloader_returning(result)
        with patch("qobuz.AlbumDownloader", new=fake_downloader):
            await service._download_album(item)

        album = db.get_album(db_id)
        # The three fields the download actually resolved:
        assert album["title"] == "In Rainbows"
        assert album["artist"] == "Radiohead"
        assert album["track_count"] == 4
        # Everything else must survive untouched:
        assert album["cover_url"] == "https://example/cover.jpg"
        assert album["release_date"] == "2007-10-10"
        assert album["label"] == "XL Recordings"
        assert album["genre"] == "Alternative"
        assert album["duration_seconds"] == 2718
        assert album["quality"] == "FLAC 24/96kHz"
        assert album["bit_depth"] == 24
        assert album["added_to_library_at"] == "2026-04-01T10:00:00"


# ---------------------------------------------------------------------------
# A failed download must survive a restart and stay re-queueable
# ---------------------------------------------------------------------------


class TestFailedDownloadHistory:
    async def test_failed_download_appears_in_recent_downloads(self, db, event_bus):
        """get_recent_downloads filters on download_status IN ('complete',
        'failed') AND downloaded_at IS NOT NULL — the failure path has to
        satisfy both halves or the history is empty after a restart."""
        result = FakeAlbumResult(
            total=10,
            successful=0,
            tracks=[
                FakeTrackResult(track_id=i, title=f"T{i}", success=False)
                for i in range(10)
            ],
        )
        client = _make_qobuz_client()
        service = DownloadService(
            db, event_bus, clients={"qobuz": client}, download_path="/tmp"
        )
        _item, db_id = _build_queue_item_in_place(service, db, "qobuz", "history-fail")

        fake_downloader = _make_fake_downloader_returning(result)
        with patch("qobuz.AlbumDownloader", new=fake_downloader):
            await service._process_queue()

        history = db.get_recent_downloads()
        assert [h["id"] for h in history] == [db_id]
        assert history[0]["download_status"] == "failed"

    async def test_failed_album_can_be_re_enqueued(self, db, event_bus):
        """'failed' must not be terminal: enqueue looks albums up by source
        id, never by status, so a retry has to queue normally."""
        result = FakeAlbumResult(
            total=10,
            successful=0,
            tracks=[
                FakeTrackResult(track_id=i, title=f"T{i}", success=False)
                for i in range(10)
            ],
        )
        client = _make_qobuz_client()
        service = DownloadService(
            db, event_bus, clients={"qobuz": client}, download_path="/tmp"
        )
        _item, db_id = _build_queue_item_in_place(service, db, "qobuz", "retry-me")

        fake_downloader = _make_fake_downloader_returning(result)
        with patch("qobuz.AlbumDownloader", new=fake_downloader):
            await service._process_queue()
        assert db.get_album(db_id)["download_status"] == "failed"

        items = await service.enqueue("qobuz", ["retry-me"])
        assert len(items) == 1
        assert items[0]["album_db_id"] == db_id
        assert db.get_album(db_id)["download_status"] == "queued"
