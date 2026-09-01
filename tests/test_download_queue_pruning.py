"""The in-memory download queue and cancel set must stay bounded.

Terminal items used to accumulate in ``_queue`` for the life of the
process — ``GET /api/downloads/queue`` re-serialises every one of them on
each poll and ``_process_queue`` rescans the list each iteration — while
``cancel()`` recorded IDs that only two paths ever discarded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.models.database import AppDatabase
from backend.services.download import (
    MAX_TERMINAL_QUEUE_ITEMS,
    TERMINAL_STATUSES,
    DownloadService,
)
from backend.services.event_bus import EventBus


@pytest.fixture
def db(tmp_path):
    return AppDatabase(str(tmp_path / "libsync.db"))


@pytest.fixture
def event_bus():
    return EventBus()


@dataclass
class FakeAlbumResult:
    total: int = 1
    successful: int = 1
    title: str = "Test Album"
    artist: str = "Test Artist"
    tracks: list = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.successful / self.total if self.total > 0 else 0.0


def _instant_downloader():
    """AlbumDownloader stand-in whose download() resolves immediately."""

    def _fake_class(
        client,
        config,
        *,
        on_track_start=None,
        on_track_progress=None,
        on_track_complete=None,
    ):
        instance = MagicMock()
        instance.download = AsyncMock(return_value=FakeAlbumResult())
        return instance

    return _fake_class


def _service(db, event_bus):
    client = MagicMock()
    client.catalog = MagicMock()
    return DownloadService(
        db, event_bus, clients={"qobuz": client}, download_path="/tmp"
    )


def _seed_albums(db, count: int) -> list[str]:
    ids = []
    for i in range(count):
        source_album_id = f"album-{i:04d}"
        db.upsert_album(
            source="qobuz",
            source_album_id=source_album_id,
            title=f"Album {i}",
            artist="Test Artist",
            track_count=1,
        )
        ids.append(source_album_id)
    return ids


class TestQueuePruning:
    async def test_150_completed_downloads_leave_at_most_100_items(self, db, event_bus):
        service = _service(db, event_bus)
        album_ids = _seed_albums(db, 150)

        with patch("qobuz.AlbumDownloader", new=_instant_downloader()):
            await service.enqueue("qobuz", album_ids)
            await service._worker_task

        queue = service.get_queue()
        assert len(queue) == MAX_TERMINAL_QUEUE_ITEMS
        assert all(q["status"] in TERMINAL_STATUSES for q in queue)
        # The window keeps the newest items, so the oldest 50 are gone.
        kept = [q["source_album_id"] for q in queue]
        assert kept == album_ids[50:]

    async def test_live_items_are_never_pruned(self, db, event_bus):
        """Pending items must survive even when the terminal window is full."""
        service = _service(db, event_bus)
        for i in range(MAX_TERMINAL_QUEUE_ITEMS + 20):
            service._queue.append(
                {"id": f"done-{i}", "status": "complete", "album_db_id": i}
            )
        service._queue.append({"id": "live", "status": "pending", "album_db_id": 999})

        service._prune_queue()

        statuses = [q["id"] for q in service._queue if q["status"] == "pending"]
        assert statuses == ["live"]
        assert len(service._queue) == MAX_TERMINAL_QUEUE_ITEMS + 1

    async def test_short_queue_is_left_alone(self, db, event_bus):
        service = _service(db, event_bus)
        for i in range(5):
            service._queue.append(
                {"id": f"done-{i}", "status": "complete", "album_db_id": i}
            )
        service._prune_queue()
        assert len(service._queue) == 5


class TestCancelSetPruning:
    async def test_cancelling_a_pending_item_clears_its_id(self, db, event_bus):
        service = _service(db, event_bus)
        album_ids = _seed_albums(db, 2)

        with patch("qobuz.AlbumDownloader", new=_instant_downloader()):
            items = await service.enqueue("qobuz", album_ids)
            await service.cancel([items[1]["id"]])
            await service._worker_task

        assert service._cancel_requested == set()

    async def test_cancel_during_download_clears_its_id(self, db, event_bus):
        """The post-download re-check consumes the request; nothing lingers."""
        service = _service(db, event_bus)
        album_ids = _seed_albums(db, 1)
        db.update_album_status(
            db.get_album_by_source_id("qobuz", album_ids[0])["id"], "queued"
        )
        item = {
            "id": "cancel-me",
            "album_db_id": db.get_album_by_source_id("qobuz", album_ids[0])["id"],
            "source": "qobuz",
            "source_album_id": album_ids[0],
            "title": "Album 0",
            "artist": "Test Artist",
            "cover_url": None,
            "track_count": 1,
            "tracks_done": 0,
            "bytes_done": 0,
            "bytes_total": 0,
            "speed": 0.0,
            "status": "pending",
            "force": False,
        }
        service._queue.append(item)
        # Simulate a cancel that landed while the SDK was mid-album.
        service._cancel_requested.add("cancel-me")

        with patch("qobuz.AlbumDownloader", new=_instant_downloader()):
            await service._process_queue()

        assert item["status"] == "cancelled"
        assert service._cancel_requested == set()

    async def test_cancelling_a_finished_item_records_nothing(self, db, event_bus):
        """cancel() used to add every ID unconditionally, so a cancel aimed
        at an already-finished item leaked forever."""
        service = _service(db, event_bus)
        album_ids = _seed_albums(db, 1)

        with patch("qobuz.AlbumDownloader", new=_instant_downloader()):
            items = await service.enqueue("qobuz", album_ids)
            await service._worker_task

        assert items[0]["status"] == "complete"
        await service.cancel([items[0]["id"]])
        assert service._cancel_requested == set()
        assert items[0]["status"] == "complete"

    async def test_cancel_raised_mid_download_is_recorded_then_cleared(
        self, db, event_bus
    ):
        """A cancel that lands while the SDK is downloading has to be
        remembered until the download returns, then dropped."""
        service = _service(db, event_bus)
        album_ids = _seed_albums(db, 1)
        seen_during_download: list[set] = []

        def _cancelling_downloader(
            client,
            config,
            *,
            on_track_start=None,
            on_track_progress=None,
            on_track_complete=None,
        ):
            instance = MagicMock()

            async def _download(album_id):
                # The user hits Cancel while the album is in flight.
                item_id = service._queue[0]["id"]
                await service.cancel([item_id])
                seen_during_download.append(set(service._cancel_requested))
                return FakeAlbumResult()

            instance.download = _download
            return instance

        with patch("qobuz.AlbumDownloader", new=_cancelling_downloader):
            await service.enqueue("qobuz", album_ids)
            await service._worker_task

        assert seen_during_download and len(seen_during_download[0]) == 1
        assert service._cancel_requested == set()
        assert service.get_queue()[0]["status"] == "cancelled"
        album = db.get_album_by_source_id("qobuz", album_ids[0])
        assert album["download_status"] == "not_downloaded"
