"""Legacy sentinel scan reconciliation through the shared safe primitives."""

import json
import os
import sqlite3
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import create_app


def _sentinel(folder, **overrides):
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "qobuz",
        "album_id": "album-1",
        "title": "Sentinel Title",
        "artist": "Sentinel Artist",
        "tracks_count": 1,
        "downloaded_at": "2026-09-05T10:11:12",
    }
    payload.update(overrides)
    path = folder / ".streamrip.json"
    path.write_text(json.dumps(payload))
    return path, payload


def _audio(folder, name="01.flac"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).touch()


def _catalog_client(source, specs):
    async def get_album_with_tracks(source_album_id):
        spec = specs[str(source_album_id)]
        if isinstance(spec, Exception):
            raise spec
        count, returned = spec
        ids = [f"{source_album_id}-t{i}" for i in range(1, returned + 1)]
        if source == "qobuz":
            album = SimpleNamespace(tracks_count=count)
            tracks = [
                SimpleNamespace(
                    id=track_id,
                    title=f"Track {i}",
                    performer=SimpleNamespace(name="Artist"),
                    track_number=i,
                    disc_number=1,
                    duration=180,
                    explicit=False,
                    isrc=f"ISRC-{track_id}",
                )
                for i, track_id in enumerate(ids, start=1)
            ]
        else:
            album = SimpleNamespace(number_of_tracks=count)
            tracks = [
                SimpleNamespace(
                    id=track_id,
                    title=f"Track {i}",
                    artist=SimpleNamespace(name="Artist"),
                    artists=[],
                    track_number=i,
                    volume_number=1,
                    duration=180,
                    explicit=False,
                    isrc=f"ISRC-{track_id}",
                )
                for i, track_id in enumerate(ids, start=1)
            ]
        return album, tracks

    client = MagicMock()
    client.catalog.get_album_with_tracks = AsyncMock(side_effect=get_album_with_tracks)
    return client


def _install_client(app, source, specs):
    client = _catalog_client(source, specs)
    app.state._clients_ref[source] = client
    app.state.library_service.clients[source] = client
    return client


def _dedup_ids(path):
    if not path.exists():
        return set()
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT id FROM downloads").fetchall()}
    finally:
        conn.close()


@pytest.fixture
def app(tmp_path, monkeypatch):
    db_path = tmp_path / "data" / "libsync.db"
    monkeypatch.setenv("STREAMRIP_DB_PATH", str(db_path))
    return create_app(db_path=str(db_path))


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as value:
        yield value


async def test_env_only_root_reconciles_actual_folder_and_unmark_reverses(
    app, client, tmp_path, monkeypatch
):
    root = tmp_path / "env-music"
    folder = root / "Artist" / "Album"
    _audio(folder)
    sentinel, original = _sentinel(
        folder,
        source=None,
        album_id="env-album",
        _folder="/payload/must/not/be/trusted",
    )
    payload = json.loads(sentinel.read_text())
    payload.pop("source")
    sentinel.write_text(json.dumps(payload))
    original.pop("source")
    monkeypatch.setenv("STREAMRIP_DOWNLOADS_PATH", str(root))
    _install_client(app, "qobuz", {"env-album": (1, 1)})

    response = await client.post("/api/downloads/scan")

    assert response.status_code == 200
    assert response.json() == {"scanned": 1, "reconciled": 1, "failures": []}
    album = app.state.db.get_album_by_source_id("qobuz", "env-album")
    assert album["download_status"] == "complete"
    assert album["downloaded_at"] == original["downloaded_at"]
    assert album["local_folder_path"] == str(folder.resolve())
    assert json.loads(sentinel.read_text()) == payload
    assert _dedup_ids(tmp_path / "data" / "downloads.db") == {"env-album-t1"}

    unmark = await client.post(f"/api/library/albums/{album['id']}/unmark-downloaded")
    assert unmark.status_code == 200
    assert not sentinel.exists()
    assert _dedup_ids(tmp_path / "data" / "downloads.db") == set()


async def test_db_root_overrides_environment_root(app, client, tmp_path, monkeypatch):
    env_root = tmp_path / "env"
    db_root = tmp_path / "configured"
    env_folder = env_root / "Wrong"
    db_folder = db_root / "Right"
    _audio(env_folder)
    _sentinel(env_folder, album_id="wrong")
    _audio(db_folder)
    _sentinel(db_folder, album_id="right")
    monkeypatch.setenv("STREAMRIP_DOWNLOADS_PATH", str(env_root))
    app.state.db.set_config("downloads_path", str(db_root))
    _install_client(app, "qobuz", {"right": (1, 1)})

    response = await client.post("/api/downloads/scan")

    assert response.json()["scanned"] == 1
    assert response.json()["reconciled"] == 1
    assert app.state.db.get_album_by_source_id("qobuz", "right") is not None
    assert app.state.db.get_album_by_source_id("qobuz", "wrong") is None


async def test_qobuz_and_tidal_use_isolated_dedup_databases(
    app, client, tmp_path, monkeypatch
):
    root = tmp_path / "music"
    qobuz_folder = root / "Qobuz"
    tidal_folder = root / "Tidal"
    _audio(qobuz_folder)
    _sentinel(qobuz_folder, source="qobuz", album_id="q1")
    _audio(tidal_folder)
    _sentinel(tidal_folder, source="tidal", album_id="t1")
    monkeypatch.setenv("STREAMRIP_DOWNLOADS_PATH", str(root))
    _install_client(app, "qobuz", {"q1": (1, 1)})
    _install_client(app, "tidal", {"t1": (1, 1)})

    response = await client.post("/api/downloads/scan")

    assert response.json()["reconciled"] == 2
    assert _dedup_ids(tmp_path / "data" / "downloads.db") == {"q1-t1"}
    assert _dedup_ids(tmp_path / "data" / "downloads-tidal.db") == {"t1-t1"}


async def test_existing_album_metadata_is_not_replaced_by_sentinel(
    app, client, tmp_path, monkeypatch
):
    root = tmp_path / "music"
    folder = root / "Album"
    _audio(folder)
    _sentinel(folder, album_id="existing", title="Wrong", artist="Wrong")
    monkeypatch.setenv("STREAMRIP_DOWNLOADS_PATH", str(root))
    album_id = app.state.db.upsert_album(
        "qobuz",
        "existing",
        "Right Title",
        "Right Artist",
        release_date="2001-02-03",
        label="Right Label",
        cover_url="https://example.test/right.jpg",
        track_count=1,
    )
    _install_client(app, "qobuz", {"existing": (1, 1)})

    response = await client.post("/api/downloads/scan")

    assert response.json()["reconciled"] == 1
    album = app.state.db.get_album(album_id)
    assert album["title"] == "Right Title"
    assert album["artist"] == "Right Artist"
    assert album["release_date"] == "2001-02-03"
    assert album["label"] == "Right Label"
    assert album["cover_url"] == "https://example.test/right.jpg"


async def test_malformed_unsupported_and_symlink_escape_are_isolated(
    app, client, tmp_path, monkeypatch
):
    root = tmp_path / "music"
    malformed = root / "Malformed"
    malformed.mkdir(parents=True)
    (malformed / ".streamrip.json").write_text("{not-json")
    unsupported = root / "Unsupported"
    _audio(unsupported)
    _sentinel(unsupported, source="spotify", album_id="bad")
    outside = tmp_path / "outside"
    _audio(outside)
    _sentinel(outside, album_id="escaped")
    root.mkdir(exist_ok=True)
    os.symlink(outside, root / "Escape")
    monkeypatch.setenv("STREAMRIP_DOWNLOADS_PATH", str(root))

    response = await client.post("/api/downloads/scan")

    body = response.json()
    assert body["reconciled"] == 0
    assert body["scanned"] == 3
    assert len(body["failures"]) == 3
    errors = " ".join(failure["error"] for failure in body["failures"])
    assert "JSON" in errors
    assert "Unsupported sentinel source" in errors
    assert "symlink" in errors
    assert app.state.db.get_album_by_source_id("qobuz", "escaped") is None


async def test_offline_and_incomplete_catalogs_leave_unknown_rows_not_downloaded(
    app, client, tmp_path, monkeypatch
):
    root = tmp_path / "music"
    offline = root / "A Offline"
    incomplete = root / "B Incomplete"
    _audio(offline)
    _sentinel(offline, album_id="offline")
    _audio(incomplete)
    _sentinel(incomplete, album_id="incomplete", tracks_count=2)
    monkeypatch.setenv("STREAMRIP_DOWNLOADS_PATH", str(root))
    _install_client(
        app,
        "qobuz",
        {"offline": OSError("catalog offline"), "incomplete": (2, 1)},
    )

    response = await client.post("/api/downloads/scan")

    assert response.json()["reconciled"] == 0
    assert len(response.json()["failures"]) == 2
    for source_album_id in ("offline", "incomplete"):
        album = app.state.db.get_album_by_source_id("qobuz", source_album_id)
        assert album is not None
        assert album["download_status"] == "not_downloaded"
    assert _dedup_ids(tmp_path / "data" / "downloads.db") == set()


async def test_partial_folder_with_valid_sentinel_does_not_poison_dedup(
    app, client, tmp_path, monkeypatch
):
    root = tmp_path / "music"
    folder = root / "Partial"
    _audio(folder)
    _sentinel(folder, album_id="partial", tracks_count=2)
    monkeypatch.setenv("STREAMRIP_DOWNLOADS_PATH", str(root))
    _install_client(app, "qobuz", {"partial": (2, 2)})

    response = await client.post("/api/downloads/scan")

    assert response.json()["reconciled"] == 0
    assert "expected 2 audio files, found 1" in response.json()["failures"][0]["error"]
    assert _dedup_ids(tmp_path / "data" / "downloads.db") == set()


async def test_complete_multidisc_album_reconciles(app, client, tmp_path, monkeypatch):
    root = tmp_path / "music"
    folder = root / "Multidisc"
    _audio(folder, "01.flac")
    _audio(folder / "Disc 2", "02.flac")
    _sentinel(folder, album_id="multi", tracks_count=2)
    monkeypatch.setenv("STREAMRIP_DOWNLOADS_PATH", str(root))
    _install_client(app, "qobuz", {"multi": (2, 2)})

    response = await client.post("/api/downloads/scan")

    assert response.json()["reconciled"] == 1
    album = app.state.db.get_album_by_source_id("qobuz", "multi")
    assert album["local_folder_path"] == str(folder.resolve())
    assert _dedup_ids(tmp_path / "data" / "downloads.db") == {
        "multi-t1",
        "multi-t2",
    }


async def test_mandatory_db_failure_continues_and_only_publishes_success(
    app, client, tmp_path, monkeypatch
):
    root = tmp_path / "music"
    first = root / "A Fails"
    second = root / "B Works"
    _audio(first)
    _sentinel(first, album_id="first")
    _audio(second)
    _sentinel(second, album_id="second")
    monkeypatch.setenv("STREAMRIP_DOWNLOADS_PATH", str(root))
    _install_client(app, "qobuz", {"first": (1, 1), "second": (1, 1)})
    dedup_path = tmp_path / "data" / "downloads.db"
    dedup_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(dedup_path)
    try:
        conn.execute("CREATE TABLE downloads (id TEXT PRIMARY KEY)")
        conn.execute(
            """CREATE TRIGGER fail_first BEFORE INSERT ON downloads
               WHEN NEW.id = 'first-t1'
               BEGIN SELECT RAISE(FAIL, 'mandatory failure'); END"""
        )
        conn.commit()
    finally:
        conn.close()
    status_events = []
    library_events = []

    async def record(data):
        status_events.append(data)

    async def record_library(data):
        library_events.append(data)

    app.state.event_bus.subscribe("album_status_changed", record)
    app.state.event_bus.subscribe("library_updated", record_library)

    response = await client.post("/api/downloads/scan")

    assert response.json()["reconciled"] == 1
    assert len(response.json()["failures"]) == 1
    failed = app.state.db.get_album_by_source_id("qobuz", "first")
    succeeded = app.state.db.get_album_by_source_id("qobuz", "second")
    assert failed["download_status"] == "not_downloaded"
    assert succeeded["download_status"] == "complete"
    assert status_events == [{"album_id": succeeded["id"], "status": "complete"}]
    assert library_events == [{"source": "qobuz", "new_count": 1, "total": 1}]
    assert _dedup_ids(dedup_path) == {"second-t1"}


async def test_repeated_scan_is_idempotent_and_does_not_rewrite_sentinel(
    app, client, tmp_path, monkeypatch
):
    root = tmp_path / "music"
    folder = root / "Album"
    _audio(folder)
    sentinel, _ = _sentinel(folder, album_id="repeat")
    original = sentinel.read_bytes()
    monkeypatch.setenv("STREAMRIP_DOWNLOADS_PATH", str(root))
    sdk_client = _install_client(app, "qobuz", {"repeat": (1, 1)})

    first = await client.post("/api/downloads/scan")
    second = await client.post("/api/downloads/scan")

    assert first.json()["reconciled"] == 1
    assert second.json()["reconciled"] == 1
    assert sentinel.read_bytes() == original
    assert _dedup_ids(tmp_path / "data" / "downloads.db") == {"repeat-t1"}
    assert sdk_client.catalog.get_album_with_tracks.await_count == 2


@pytest.mark.parametrize("bad_album_id", [None, "", True, {"nested": "id"}])
async def test_invalid_sentinel_album_identity_is_rejected(
    app, client, tmp_path, monkeypatch, bad_album_id
):
    root = tmp_path / "music"
    folder = root / "Invalid"
    _audio(folder)
    _sentinel(folder, album_id=bad_album_id)
    monkeypatch.setenv("STREAMRIP_DOWNLOADS_PATH", str(root))

    response = await client.post("/api/downloads/scan")

    assert response.json()["reconciled"] == 0
    assert len(response.json()["failures"]) == 1
    assert "album_id is missing or invalid" in response.json()["failures"][0]["error"]
    assert app.state.db.get_albums("qobuz") == []


async def test_duplicate_sentinel_track_identities_are_rejected(
    app, client, tmp_path, monkeypatch
):
    root = tmp_path / "music"
    folder = root / "Duplicate"
    _audio(folder, "01.flac")
    _audio(folder, "02.flac")
    _sentinel(
        folder,
        album_id="duplicate",
        tracks_count=2,
        tracks_downloaded=2,
        tracks=[
            {"id": "duplicate-t1", "success": True},
            {"id": "duplicate-t1", "success": True},
        ],
    )
    monkeypatch.setenv("STREAMRIP_DOWNLOADS_PATH", str(root))
    _install_client(app, "qobuz", {"duplicate": (2, 2)})

    response = await client.post("/api/downloads/scan")

    assert response.json()["reconciled"] == 0
    assert "partial or duplicated" in response.json()["failures"][0]["error"]
    assert _dedup_ids(tmp_path / "data" / "downloads.db") == set()


async def test_duplicate_local_tag_identities_are_rejected(
    app, client, tmp_path, monkeypatch
):
    import backend.services.sentinels as sentinels

    root = tmp_path / "music"
    folder = root / "Duplicate Tags"
    _audio(folder, "01.flac")
    _audio(folder, "02.flac")
    _sentinel(folder, album_id="tag-duplicate", tracks_count=2)
    monkeypatch.setenv("STREAMRIP_DOWNLOADS_PATH", str(root))
    _install_client(app, "qobuz", {"tag-duplicate": (2, 2)})
    tagged_file = MagicMock()
    tagged_file.tags.get.return_value = ["SAME-ISRC"]
    monkeypatch.setattr(sentinels.mutagen, "File", MagicMock(return_value=tagged_file))

    response = await client.post("/api/downloads/scan")

    assert response.json()["reconciled"] == 0
    assert "identity tags are duplicated" in response.json()["failures"][0]["error"]
    assert _dedup_ids(tmp_path / "data" / "downloads.db") == set()


async def test_sentinel_walk_and_reads_run_off_event_loop(
    app, client, tmp_path, monkeypatch
):
    import backend.services.sentinels as sentinels

    root = tmp_path / "music"
    root.mkdir()
    monkeypatch.setenv("STREAMRIP_DOWNLOADS_PATH", str(root))
    event_loop_thread = threading.get_ident()
    called_on = None
    original = sentinels.discover_sentinels

    def spy(*args, **kwargs):
        nonlocal called_on
        called_on = threading.get_ident()
        return original(*args, **kwargs)

    monkeypatch.setattr(sentinels, "discover_sentinels", spy)

    response = await client.post("/api/downloads/scan")

    assert response.status_code == 200
    assert called_on is not None
    assert called_on != event_loop_thread


@pytest.mark.parametrize("invalid_timestamp", [None, "not-a-date"])
async def test_invalid_or_missing_timestamp_uses_current_utc_fallback(
    app, client, tmp_path, monkeypatch, invalid_timestamp
):
    root = tmp_path / "music"
    folder = root / "Timestamp"
    _audio(folder)
    _sentinel(folder, album_id="timestamp", downloaded_at=invalid_timestamp)
    monkeypatch.setenv("STREAMRIP_DOWNLOADS_PATH", str(root))
    _install_client(app, "qobuz", {"timestamp": (1, 1)})

    response = await client.post("/api/downloads/scan")

    assert response.json()["reconciled"] == 1
    album = app.state.db.get_album_by_source_id("qobuz", "timestamp")
    downloaded_at = datetime.fromisoformat(album["downloaded_at"])
    assert downloaded_at.tzinfo is not None
    assert downloaded_at.utcoffset() is not None
