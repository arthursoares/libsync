"""Album-level byte progress must aggregate concurrently downloading tracks.

The SDKs download up to ``max_connections`` tracks at once and each
``on_track_progress`` callback carries that one track's absolute counters.
Writing them straight onto the shared queue item made the album totals
reflect whichever track called last, so the UI progress bar jumped
backwards and the speed oscillated.

These tests drive the callbacks directly through a scripted fake
``AlbumDownloader`` and inspect the queue item after every callback.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.models.database import AppDatabase
from backend.services.download import DownloadService
from backend.services.event_bus import EventBus

MB = 1024 * 1024


@pytest.fixture
def db(tmp_path):
    return AppDatabase(str(tmp_path / "libsync.db"))


@pytest.fixture
def event_bus():
    return EventBus()


@dataclass
class FakeAlbumResult:
    total: int
    successful: int
    title: str = "Test Album"
    artist: str = "Test Artist"
    tracks: list = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.successful / self.total if self.total > 0 else 0.0


def _scripted_downloader(script, result, item, snapshots, clock):
    """Fake AlbumDownloader that replays ``script`` through the callbacks.

    Steps are ``("start", num, title)``, ``("progress", num, done, total)``,
    ``("complete", num, title, ok)`` and ``("tick", seconds)``.  The whole
    replay runs synchronously under a frozen ``time.monotonic`` so the
    derived speed is deterministic; nothing awaits inside the patch window,
    so the event loop never observes the fake clock.
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

        async def _download(album_id):
            with patch("time.monotonic", side_effect=lambda: clock["t"]):
                for step in script:
                    kind = step[0]
                    if kind == "tick":
                        clock["t"] += step[1]
                        continue
                    if kind == "start":
                        on_track_start(step[1], step[2])
                    elif kind == "progress":
                        on_track_progress(step[1], step[2], step[3])
                    else:
                        on_track_complete(step[1], step[2], step[3])
                    snapshots.append(
                        {
                            "bytes_done": item["bytes_done"],
                            "bytes_total": item["bytes_total"],
                            "speed": item["speed"],
                        }
                    )
            return result

        instance.download = _download
        return instance

    return _fake_class


def _item(db, source_album_id: str) -> dict:
    db_id = db.upsert_album(
        source="qobuz",
        source_album_id=source_album_id,
        title="Test Album",
        artist="Test Artist",
        track_count=2,
    )
    return {
        "id": f"queue-{source_album_id}",
        "album_db_id": db_id,
        "source": "qobuz",
        "source_album_id": source_album_id,
        "title": "Test Album",
        "artist": "Test Artist",
        "cover_url": None,
        "track_count": 2,
        "tracks_done": 0,
        "bytes_done": 0,
        "bytes_total": 0,
        "speed": 0.0,
        "status": "downloading",
        "force": False,
    }


def _service(db, event_bus):
    client = MagicMock()
    client.catalog = MagicMock()
    tracks = [
        SimpleNamespace(
            id=i,
            title=f"Track {i}",
            performer=SimpleNamespace(name="Test Artist"),
            track_number=i + 1,
            disc_number=1,
            duration=180,
            explicit=False,
            isrc=None,
        )
        for i in range(2)
    ]
    client.catalog.get_album_with_tracks = AsyncMock(
        return_value=(SimpleNamespace(tracks_count=2), tracks)
    )
    return DownloadService(
        db, event_bus, clients={"qobuz": client}, download_path="/tmp"
    )


class TestInterleavedTrackProgress:
    async def test_album_totals_are_sums_and_never_drop(self, db, event_bus):
        """Two tracks reporting alternately must produce monotonic totals."""
        script = [
            ("start", 1, "One"),
            ("tick", 1.0),
            ("progress", 1, 3 * MB, 4 * MB),
            ("start", 2, "Two"),
            ("tick", 1.0),
            # Track 2's first report is smaller than track 1's running total.
            # Assigning it to the album (the old behaviour) went 3 MB -> 1 MB.
            ("progress", 2, 1 * MB, 2 * MB),
            ("progress", 1, 4 * MB, 4 * MB),
            ("complete", 1, "One", True),
            ("tick", 2.0),
            ("progress", 2, 2 * MB, 2 * MB),
            ("complete", 2, "Two", True),
        ]
        result = FakeAlbumResult(total=2, successful=2)
        item = _item(db, "interleaved")
        snapshots: list[dict] = []
        clock = {"t": 1000.0}
        service = _service(db, event_bus)

        with patch(
            "qobuz.AlbumDownloader",
            new=_scripted_downloader(script, result, item, snapshots, clock),
        ):
            await service._download_album(item)

        done = [s["bytes_done"] for s in snapshots]
        total = [s["bytes_total"] for s in snapshots]
        assert done == sorted(done), f"bytes_done went backwards: {done}"
        assert total == sorted(total), f"bytes_total went backwards: {total}"

        # Progress callbacks only (indices of the "progress" steps).
        progress_done = [
            s["bytes_done"]
            for s, step in zip(snapshots, [x for x in script if x[0] != "tick"])
            if step[0] == "progress"
        ]
        assert progress_done == [3 * MB, 4 * MB, 5 * MB, 6 * MB]

        assert item["bytes_done"] == 6 * MB
        assert item["bytes_total"] == 6 * MB

    async def test_speed_is_one_album_wide_rate(self, db, event_bus):
        """Speed is total bytes over the time since the first track started,
        not a per-track rate reset by whichever track reported last."""
        script = [
            ("start", 1, "One"),
            ("tick", 1.0),
            ("progress", 1, 3 * MB, 4 * MB),  # 3 MB in 1 s
            ("start", 2, "Two"),
            ("tick", 1.0),
            ("progress", 2, 1 * MB, 2 * MB),  # 4 MB in 2 s
            ("tick", 2.0),
            ("progress", 2, 2 * MB, 2 * MB),  # 5 MB in 4 s
        ]
        result = FakeAlbumResult(total=2, successful=2)
        item = _item(db, "speed")
        snapshots: list[dict] = []
        clock = {"t": 500.0}
        service = _service(db, event_bus)

        with patch(
            "qobuz.AlbumDownloader",
            new=_scripted_downloader(script, result, item, snapshots, clock),
        ):
            await service._download_album(item)

        speeds = [s["speed"] for s in snapshots]
        # Snapshot order is: track 1 start, its progress, track 2 start,
        # then track 2's two progress reports.
        assert speeds[1] == 3.0
        assert speeds[3] == 2.0
        assert speeds[4] == 1.25
        assert item["speed"] == 1.25

    async def test_progress_emits_stay_throttled_to_two_per_second(self, db, event_bus):
        """The 0.5 s throttle on progress emits must survive the rework."""
        seen: list[dict] = []

        async def _handler(data):
            seen.append(data)

        event_bus.subscribe("download_progress", _handler)

        script = [
            ("start", 1, "One"),
            ("progress", 1, 1 * MB, 4 * MB),
            ("tick", 0.1),
            ("progress", 1, 2 * MB, 4 * MB),
            ("tick", 0.1),
            ("progress", 1, 3 * MB, 4 * MB),
        ]
        result = FakeAlbumResult(total=1, successful=1)
        item = _item(db, "throttle")
        snapshots: list[dict] = []
        clock = {"t": 2000.0}
        service = _service(db, event_bus)

        with patch(
            "qobuz.AlbumDownloader",
            new=_scripted_downloader(script, result, item, snapshots, clock),
        ):
            await service._download_album(item)
        # Progress emits are fire-and-forget tasks; let them run.
        await asyncio.sleep(0)

        # One from on_track_start, one from the first progress call. The two
        # follow-ups land inside the 0.5 s window and are dropped.
        assert len(seen) == 2
        # The published totals are the aggregated ones.
        assert seen[-1]["bytes_done"] == 1 * MB
