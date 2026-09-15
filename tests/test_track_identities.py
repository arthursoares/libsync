"""Strict catalog-backed album track identity resolution."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.models.database import AppDatabase
from backend.services.tracks import (
    TrackClientUnavailableError,
    TrackIdentityError,
    resolve_album_track_ids,
)


def _qobuz_track(track_id, number=1):
    return SimpleNamespace(
        id=track_id,
        title=f"Qobuz {number}",
        performer=SimpleNamespace(name="Qobuz Artist"),
        track_number=number,
        disc_number=1,
        duration=180,
        explicit=False,
        isrc=f"Q{number}",
    )


def _tidal_track(track_id, number=1, *, primary_name="Tidal Artist"):
    return SimpleNamespace(
        id=track_id,
        title=f"Tidal {number}",
        artist=SimpleNamespace(name=primary_name),
        artists=[SimpleNamespace(name="Fallback Artist")],
        track_number=number,
        volume_number=2,
        duration=200,
        explicit=True,
        isrc=f"T{number}",
    )


async def test_partial_cache_forces_complete_qobuz_catalog_resolution(tmp_path):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = db.upsert_album("qobuz", "album-1", "Album", "Artist", track_count=2)
    db.upsert_track(album_id, "old", "Stale", "Artist")

    album = SimpleNamespace(tracks_count=2)
    tracks = [_qobuz_track("t1", 1), _qobuz_track("t2", 2)]
    client = MagicMock()
    client.catalog.get_album_with_tracks = AsyncMock(return_value=(album, tracks))

    resolved = await resolve_album_track_ids(db, {"qobuz": client}, album_id)

    assert resolved == ("t1", "t2")
    client.catalog.get_album_with_tracks.assert_awaited_once_with("album-1")
    assert {row["source_track_id"] for row in db.get_tracks(album_id)} == {
        "old",
        "t1",
        "t2",
    }


async def test_tidal_shape_updates_metadata_and_preserves_download_fields(tmp_path):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = db.upsert_album("tidal", "album-2", "Album", "Artist", track_count=9)
    track_db_id = db.upsert_track(album_id, "t1", "Old", "Old Artist")
    db.update_track_status(
        track_db_id,
        "complete",
        file_path="/music/one.flac",
        format="FLAC",
        bit_depth=24,
        sample_rate=96000,
    )
    album = SimpleNamespace(number_of_tracks=2)
    tracks = [
        _tidal_track("t1", 1),
        _tidal_track("t2", 2, primary_name=""),
    ]
    client = MagicMock()
    client.catalog.get_album_with_tracks = AsyncMock(return_value=(album, tracks))

    resolved = await resolve_album_track_ids(db, {"tidal": client}, album_id)

    assert resolved == ("t1", "t2")
    assert db.get_album(album_id)["track_count"] == 2
    cached = {row["source_track_id"]: row for row in db.get_tracks(album_id)}
    assert cached["t1"]["title"] == "Tidal 1"
    assert cached["t1"]["download_status"] == "complete"
    assert cached["t1"]["file_path"] == "/music/one.flac"
    assert cached["t1"]["format"] == "FLAC"
    assert cached["t1"]["bit_depth"] == 24
    assert cached["t1"]["sample_rate"] == 96000
    assert cached["t2"]["artist"] == "Fallback Artist"
    assert cached["t2"]["disc_number"] == 2


@pytest.mark.parametrize(
    ("count", "tracks", "message"),
    [
        (0, [], "empty authoritative"),
        (2, [_qobuz_track("t1")], "incomplete"),
        (2, [_qobuz_track("same", 1), _qobuz_track("same", 2)], "duplicate"),
        (1, [_qobuz_track("  ")], "empty identity"),
    ],
)
async def test_invalid_catalog_does_not_mutate_existing_cache(
    tmp_path, count, tracks, message
):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = db.upsert_album("qobuz", "album-1", "Album", "Artist", track_count=9)
    db.upsert_track(album_id, "old", "Old", "Artist")
    client = MagicMock()
    client.catalog.get_album_with_tracks = AsyncMock(
        return_value=(SimpleNamespace(tracks_count=count), tracks)
    )

    with pytest.raises(TrackIdentityError, match=message):
        await resolve_album_track_ids(db, {"qobuz": client}, album_id)

    assert db.get_album(album_id)["track_count"] == 9
    assert [row["source_track_id"] for row in db.get_tracks(album_id)] == ["old"]


async def test_later_normalization_failure_does_not_partially_cache(tmp_path):
    class BrokenTrack:
        @property
        def id(self):
            raise RuntimeError("broken second track")

    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = db.upsert_album("qobuz", "album-1", "Album", "Artist", track_count=9)
    client = MagicMock()
    client.catalog.get_album_with_tracks = AsyncMock(
        return_value=(
            SimpleNamespace(tracks_count=2),
            [_qobuz_track("t1"), BrokenTrack()],
        )
    )

    with pytest.raises(TrackIdentityError, match=r"normalize.*broken second track"):
        await resolve_album_track_ids(db, {"qobuz": client}, album_id)

    assert db.get_album(album_id)["track_count"] == 9
    assert db.get_tracks(album_id) == []


async def test_offline_catalog_does_not_mutate_existing_cache(tmp_path):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = db.upsert_album("qobuz", "album-1", "Album", "Artist", track_count=9)
    db.upsert_track(album_id, "old", "Old", "Artist")
    client = MagicMock()
    client.catalog.get_album_with_tracks = AsyncMock(side_effect=OSError("offline"))

    with pytest.raises(TrackIdentityError, match=r"complete Qobuz.*offline"):
        await resolve_album_track_ids(db, {"qobuz": client}, album_id)

    assert db.get_album(album_id)["track_count"] == 9
    assert [row["source_track_id"] for row in db.get_tracks(album_id)] == ["old"]


async def test_missing_client_is_actionable_and_does_not_use_partial_cache(tmp_path):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = db.upsert_album("qobuz", "album-1", "Album", "Artist", track_count=1)
    db.upsert_track(album_id, "cached", "Cached", "Artist")

    with pytest.raises(TrackClientUnavailableError, match="Connect Qobuz"):
        await resolve_album_track_ids(db, {}, album_id)

    assert [row["source_track_id"] for row in db.get_tracks(album_id)] == ["cached"]
