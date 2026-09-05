"""The download path must record the album folder it downloaded into.

Without ``albums.local_folder_path`` the unmark primitive has nowhere to
look for the ``.streamrip.json`` sentinel the SDK writes, so the sentinel
survives an unmark and hides the folder from every later fuzzy scan.

The two SDKs spell the per-track path differently — Qobuz's ``TrackResult``
uses ``path``, Tidal's uses ``file_path`` — so both shapes are exercised.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.models.database import AppDatabase
from backend.services.download import DownloadService, _album_folder_from_result
from backend.services.event_bus import EventBus
from backend.services.scan import unmark_album_downloaded


@pytest.fixture
def db(tmp_path):
    return AppDatabase(str(tmp_path / "libsync.db"))


@pytest.fixture
def event_bus():
    return EventBus()


# ---------------------------------------------------------------------------
# Fake SDK result shapes
# ---------------------------------------------------------------------------


@dataclass
class QobuzTrackResult:
    """Mirrors qobuz.downloader.TrackResult — the path attribute is ``path``."""

    track_id: int
    title: str
    success: bool
    path: str | None = None
    error: str | None = None


@dataclass
class TidalTrackResult:
    """Mirrors tidal.downloader.TrackResult — the path attribute is ``file_path``."""

    track_id: int
    title: str
    success: bool
    error: str | None = None
    file_path: str | None = None


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


def _fake_downloader_returning(result):
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


def _queue_item_in_place(service, db, source: str, source_album_id: str):
    db_id = db.upsert_album(
        source=source,
        source_album_id=source_album_id,
        title="Test Album",
        artist="Test Artist",
        track_count=4,
    )
    db.update_album_status(db_id, "queued")
    item = {
        "id": f"queue-{source_album_id}",
        "album_db_id": db_id,
        "source": source,
        "source_album_id": source_album_id,
        "title": "Test Album",
        "artist": "Test Artist",
        "cover_url": None,
        "track_count": 4,
        "tracks_done": 0,
        "bytes_done": 0,
        "bytes_total": 0,
        "speed": 0.0,
        "status": "pending",
        "force": False,
    }
    service._queue.append(item)
    return item, db_id


def _service(db, event_bus):
    client = MagicMock()
    client.catalog = MagicMock()
    tracks = [
        SimpleNamespace(
            id=i,
            title=f"T{i}",
            performer=SimpleNamespace(name="Test Artist"),
            track_number=i + 1,
            disc_number=1,
            duration=180,
            explicit=False,
            isrc=None,
        )
        for i in range(4)
    ]
    client.catalog.get_album_with_tracks = AsyncMock(
        return_value=(SimpleNamespace(tracks_count=4), tracks)
    )
    return DownloadService(
        db, event_bus, clients={"qobuz": client, "tidal": client}, download_path="/tmp"
    )


# ---------------------------------------------------------------------------
# The folder-derivation helper
# ---------------------------------------------------------------------------


class TestAlbumFolderFromResult:
    def test_reads_the_qobuz_path_attribute(self):
        result = FakeAlbumResult(
            total=2,
            successful=2,
            tracks=[
                QobuzTrackResult(1, "A", True, path="/music/Artist - Album/01.flac"),
                QobuzTrackResult(2, "B", True, path="/music/Artist - Album/02.flac"),
            ],
        )
        assert _album_folder_from_result(result) == "/music/Artist - Album"

    def test_reads_the_tidal_file_path_attribute(self):
        result = FakeAlbumResult(
            total=1,
            successful=1,
            tracks=[
                TidalTrackResult(
                    1, "A", True, file_path="/music/Artist - Album/01.flac"
                )
            ],
        )
        assert _album_folder_from_result(result) == "/music/Artist - Album"

    def test_ignores_failed_tracks(self):
        result = FakeAlbumResult(
            total=2,
            successful=1,
            tracks=[
                QobuzTrackResult(1, "A", False, path="/elsewhere/01.flac"),
                QobuzTrackResult(2, "B", True, path="/music/Artist - Album/02.flac"),
            ],
        )
        assert _album_folder_from_result(result) == "/music/Artist - Album"

    def test_multi_disc_resolves_to_the_album_folder_not_the_disc_folder(self):
        """Discs after the first land in ``Disc N/`` but the sentinel does not."""
        result = FakeAlbumResult(
            total=2,
            successful=2,
            tracks=[
                QobuzTrackResult(1, "A", True, path="/music/Artist - Album/01.flac"),
                QobuzTrackResult(
                    2, "B", True, path="/music/Artist - Album/Disc 2/01.flac"
                ),
            ],
        )
        assert _album_folder_from_result(result) == "/music/Artist - Album"

    def test_returns_none_without_usable_paths(self):
        assert (
            _album_folder_from_result(
                FakeAlbumResult(
                    total=2,
                    successful=2,
                    tracks=[
                        QobuzTrackResult(1, "A", True),
                        QobuzTrackResult(2, "B", True),
                    ],
                )
            )
            is None
        )
        assert _album_folder_from_result(FakeAlbumResult(total=0, successful=0)) is None


# ---------------------------------------------------------------------------
# The completed download persists the folder
# ---------------------------------------------------------------------------


class TestCompletedDownloadRecordsFolder:
    async def test_local_folder_path_is_persisted(self, db, event_bus, tmp_path):
        folder = str(tmp_path / "Test Artist - Test Album")
        result = FakeAlbumResult(
            total=4,
            successful=4,
            tracks=[
                QobuzTrackResult(
                    i, f"T{i}", True, path=os.path.join(folder, f"{i}.flac")
                )
                for i in range(4)
            ],
        )
        service = _service(db, event_bus)
        _item, db_id = _queue_item_in_place(service, db, "qobuz", "folder-album")

        with patch("qobuz.AlbumDownloader", new=_fake_downloader_returning(result)):
            await service._process_queue()

        album = db.get_album(db_id)
        assert album["download_status"] == "complete"
        assert album["downloaded_at"] is not None
        assert album["local_folder_path"] == folder

    async def test_result_without_paths_leaves_the_column_null(self, db, event_bus):
        result = FakeAlbumResult(
            total=4,
            successful=4,
            tracks=[QobuzTrackResult(i, f"T{i}", True) for i in range(4)],
        )
        service = _service(db, event_bus)
        _item, db_id = _queue_item_in_place(service, db, "qobuz", "pathless-album")

        with patch("qobuz.AlbumDownloader", new=_fake_downloader_returning(result)):
            await service._process_queue()

        album = db.get_album(db_id)
        assert album["download_status"] == "complete"
        assert album["local_folder_path"] is None

    async def test_unmark_removes_the_sentinel_the_download_left(
        self, db, event_bus, tmp_path
    ):
        """The whole point of #23: unmark has to find the SDK's sentinel."""
        folder = tmp_path / "Test Artist - Test Album"
        folder.mkdir()
        # Stand in for the sentinel the SDK writes at the end of a download.
        sentinel = folder / ".streamrip.json"
        sentinel.write_text(
            json.dumps({"source": "qobuz", "album_id": "sentinel-album"})
        )

        result = FakeAlbumResult(
            total=2,
            successful=2,
            tracks=[
                QobuzTrackResult(i, f"T{i}", True, path=str(folder / f"{i}.flac"))
                for i in range(2)
            ],
        )
        service = _service(db, event_bus)
        _item, db_id = _queue_item_in_place(service, db, "qobuz", "sentinel-album")

        with patch("qobuz.AlbumDownloader", new=_fake_downloader_returning(result)):
            await service._process_queue()

        assert db.get_album(db_id)["local_folder_path"] == str(folder)
        assert sentinel.exists()

        unmark_album_downloaded(
            db,
            db_id,
            dedup_db_dir=str(tmp_path),
            track_ids=tuple(track["source_track_id"] for track in db.get_tracks(db_id)),
        )

        assert not sentinel.exists()
        assert db.get_album(db_id)["download_status"] == "not_downloaded"
        assert db.get_album(db_id)["local_folder_path"] is None
