"""Shutdown task ownership and admission-control regressions for #74."""

import asyncio
import threading
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import create_app
from backend.models.database import AppDatabase
from backend.services.download import DownloadService
from backend.services.event_bus import EventBus
from backend.services.scan import FolderMeta, run_scan
from backend.services.sync import SyncService


def _catalog_client(*, close=None):
    track = SimpleNamespace(
        id="track-1",
        title="Track",
        performer=SimpleNamespace(name="Artist"),
        track_number=1,
        disc_number=1,
        duration=180,
        explicit=False,
        isrc=None,
    )
    client = MagicMock()
    client.catalog.get_album_with_tracks = AsyncMock(
        return_value=(SimpleNamespace(tracks_count=1), [track])
    )
    client.__aexit__ = AsyncMock(side_effect=close)
    return client


def _sync_service(db, refresh):
    library = MagicMock()
    library.refresh_library = AsyncMock(side_effect=refresh)
    return SyncService(db, EventBus(), clients={}, library_service=library)


async def _next_loop_turn():
    reached = asyncio.Event()
    asyncio.get_running_loop().call_soon(reached.set)
    await reached.wait()


async def test_lifespan_drains_current_album_without_advancing_queue(tmp_path):
    app = create_app(db_path=str(tmp_path / "libsync.db"))
    service = app.state.download_service
    first_id = app.state.db.upsert_album("qobuz", "first", "First", "Artist")
    second_id = app.state.db.upsert_album("qobuz", "second", "Second", "Artist")
    active_started = asyncio.Event()
    active_finished = asyncio.Event()
    release_album = asyncio.Event()
    second_started = asyncio.Event()

    async def close_client(*_args):
        assert active_finished.is_set()

    client = _catalog_client(close=close_client)
    service.clients = {"qobuz": client}
    app.state.library_service.clients = service.clients
    app.state.sync_service.clients = service.clients
    app.state._clients_ref = service.clients

    class FakeDownloader:
        def __init__(self, *_args, **_kwargs):
            pass

        async def download(self, source_album_id):
            if source_album_id == "first":
                active_started.set()
                await release_album.wait()
                active_finished.set()
            else:
                second_started.set()
            return SimpleNamespace(
                total=1,
                successful=1,
                success_rate=1.0,
                title=source_album_id.title(),
                artist="Artist",
                tracks=[],
            )

    context = app.router.lifespan_context(app)
    await context.__aenter__()
    shutdown_task = None
    try:
        with patch("qobuz.AlbumDownloader", new=FakeDownloader):
            await service.enqueue("qobuz", ["first", "second"])
            await active_started.wait()

            shutdown_entered = asyncio.Event()
            original_shutdown = service.shutdown

            async def observed_shutdown():
                shutdown_entered.set()
                await original_shutdown()

            service.shutdown = observed_shutdown
            shutdown_task = asyncio.create_task(context.__aexit__(None, None, None))
            await shutdown_entered.wait()

            assert service.stopping is True
            assert not second_started.is_set()
            client.__aexit__.assert_not_awaited()

            release_album.set()
            await shutdown_task
    finally:
        release_album.set()
        if shutdown_task is not None and not shutdown_task.done():
            await shutdown_task
        elif shutdown_task is None:
            with suppress(Exception):
                await context.__aexit__(None, None, None)
        worker = service._worker_task
        if worker is not None and not worker.done():
            await worker

    queue = {item["source_album_id"]: item for item in service.get_queue()}
    assert queue["first"]["status"] == "complete"
    assert queue["second"]["status"] == "cancelled"
    assert app.state.db.get_album(first_id)["download_status"] == "complete"
    assert app.state.db.get_album(second_id)["download_status"] == "not_downloaded"
    assert service._worker_task is None
    assert service._progress_tasks == set()
    client.__aexit__.assert_awaited_once_with(None, None, None)


async def test_download_shutdown_awaits_callback_progress_publish(tmp_path):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    bus = EventBus()
    publish_started = asyncio.Event()
    publish_finished = asyncio.Event()
    release_publish = asyncio.Event()

    async def block_progress(_data):
        publish_started.set()
        await release_publish.wait()
        publish_finished.set()

    bus.subscribe("download_progress", block_progress)
    client = _catalog_client()
    service = DownloadService(db, bus, {"qobuz": client}, str(tmp_path))
    album_id = db.upsert_album("qobuz", "progress", "Progress", "Artist")
    item = {
        "id": "queue-progress",
        "album_db_id": album_id,
        "source": "qobuz",
        "source_album_id": "progress",
        "title": "Progress",
        "artist": "Artist",
        "track_count": 1,
        "tracks_done": 0,
        "bytes_done": 0,
        "bytes_total": 0,
        "speed": 0.0,
        "force": False,
    }

    class ProgressDownloader:
        def __init__(self, *_args, on_track_start, **_kwargs):
            self.on_track_start = on_track_start

        async def download(self, _source_album_id):
            self.on_track_start(1, "Track")
            return SimpleNamespace(
                total=1,
                successful=1,
                success_rate=1.0,
                title="Progress",
                artist="Artist",
                tracks=[],
            )

    try:
        with patch("qobuz.AlbumDownloader", new=ProgressDownloader):
            await service._download_album(item)
        await publish_started.wait()
        shutdown = asyncio.create_task(service.shutdown())
        await _next_loop_turn()
        assert not shutdown.done()
        assert not publish_finished.is_set()
        release_publish.set()
        await shutdown
        assert publish_finished.is_set()
        assert service._progress_tasks == set()
    finally:
        release_publish.set()
        for task in getattr(service, "_progress_tasks", set()):
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.parametrize("owner", ["manual", "auto"])
async def test_sync_shutdown_interrupts_and_awaits_active_runs(tmp_path, owner):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    refresh_started = asyncio.Event()
    refresh_cancelled = asyncio.Event()

    async def refresh(_source):
        refresh_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            refresh_cancelled.set()

    service = _sync_service(db, refresh)
    owned_task = None
    try:
        if owner == "manual":
            owned_task = asyncio.create_task(service.run_sync("qobuz"))
        else:
            await service.start_auto_sync("qobuz", 0)
            owned_task = service._auto_sync_task

        await refresh_started.wait()
        await service.shutdown()

        assert refresh_cancelled.is_set()
        assert owned_task is not None and owned_task.cancelled()
        assert service._auto_sync_task is None
        assert service._sync_tasks == set()
        history = db.get_sync_history("qobuz")
        assert history[0]["status"] == "interrupted"
        assert history[0]["completed_at"] is not None
        with pytest.raises(asyncio.CancelledError):
            await owned_task
    finally:
        if owned_task is not None and not owned_task.done():
            owned_task.cancel()
            await asyncio.gather(owned_task, return_exceptions=True)
        auto_task = getattr(service, "_auto_sync_task", None)
        if auto_task is not None and not auto_task.done():
            auto_task.cancel()
            await asyncio.gather(auto_task, return_exceptions=True)


@pytest.mark.parametrize("phase", ["sync_started", "sync_complete"])
async def test_sync_cancellation_history_is_conditional(tmp_path, phase):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    bus = EventBus()
    event_entered = asyncio.Event()
    release_event = asyncio.Event()

    async def block_event(_data):
        event_entered.set()
        await release_event.wait()

    bus.subscribe(phase, block_event)
    library = MagicMock()
    library.refresh_library = AsyncMock(
        return_value={"total": 1, "new": 0, "new_album_ids": []}
    )
    service = SyncService(db, bus, clients={}, library_service=library)
    task = asyncio.create_task(service.run_sync("qobuz"))
    try:
        await event_entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_event.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    history = db.get_sync_history("qobuz")
    expected = "interrupted" if phase == "sync_started" else "complete"
    assert history[0]["status"] == expected
    assert history[0]["completed_at"] is not None
    assert service._sync_tasks == set()


def test_startup_interrupts_only_stale_running_syncs(tmp_path):
    db_path = tmp_path / "libsync.db"
    db = AppDatabase(str(db_path))
    stale_id = db.create_sync_run("qobuz")
    complete_id = db.create_sync_run("qobuz")
    db.complete_sync_run(complete_id, 1, 0, 0, 0)
    failed_id = db.create_sync_run("qobuz")
    db.fail_sync_run(failed_id)

    app = create_app(db_path=str(db_path))
    history = {row["id"]: row for row in app.state.db.get_sync_history("qobuz")}

    assert history[stale_id]["status"] == "interrupted"
    assert history[stale_id]["completed_at"] is not None
    assert history[complete_id]["status"] == "complete"
    assert history[failed_id]["status"] == "failed"


async def test_cancelling_scan_waits_for_running_thread_mutation(tmp_path, monkeypatch):
    import backend.services.scan as scan_module

    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = db.upsert_album("qobuz", "scan", "Album", "Artist", track_count=1)
    folder = tmp_path / "Artist - Album"
    folder.mkdir()
    meta = FolderMeta(folder, "Artist", "Album", None, None, 1, "tags")
    mutation_started = threading.Event()
    mutation_finished = threading.Event()
    release_mutation = threading.Event()

    monkeypatch.setattr(
        scan_module, "_find_album_folders", lambda _root: ([folder], [])
    )
    monkeypatch.setattr(scan_module, "_inspect_folder", lambda _folder: (False, meta))
    monkeypatch.setattr(
        scan_module, "resolve_album_track_ids", AsyncMock(return_value=("track-1",))
    )

    def blocking_mark(*_args, **_kwargs):
        mutation_started.set()
        release_mutation.wait()
        db.update_album_status(album_id, "complete")
        mutation_finished.set()

    monkeypatch.setattr(scan_module, "mark_album_downloaded", blocking_mark)
    task = asyncio.create_task(
        run_scan(
            db,
            clients={},
            download_path=str(tmp_path),
            dedup_db_dir=str(tmp_path),
            event_bus=EventBus(),
        )
    )
    try:
        await asyncio.to_thread(mutation_started.wait)
        task.cancel()
        await _next_loop_turn()
        assert not task.done()
        assert not mutation_finished.is_set()
        release_mutation.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert mutation_finished.is_set()
        assert db.get_album(album_id)["download_status"] == "complete"
    finally:
        release_mutation.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)


async def test_scan_registry_is_separate_and_lifespan_stops_owned_scan(
    tmp_path, monkeypatch
):
    app = create_app(db_path=str(tmp_path / "libsync.db"))
    app.state.db.set_config("downloads_path", str(tmp_path))
    scan_started = asyncio.Event()

    async def cooperative_scan(*_args, stop_event=None, **_kwargs):
        scan_started.set()
        await stop_event.wait()
        return {"status": "interrupted", "scanned": 0}

    monkeypatch.setattr("backend.services.scan.run_scan", cooperative_scan)
    context = app.router.lifespan_context(app)
    await context.__aenter__()
    shutdown = None
    task = None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/library/scan-fuzzy")
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        await scan_started.wait()
        assert "_task" not in app.state.scan_jobs[job_id]
        assert len(app.state.scan_tasks) == 1
        task = next(iter(app.state.scan_tasks))

        shutdown = asyncio.create_task(context.__aexit__(None, None, None))
        await shutdown
        assert task.done()
        assert app.state.scan_tasks == set()
        assert app.state.scan_jobs[job_id]["status"] == "complete"
    finally:
        if shutdown is None:
            stop_event = getattr(app.state, "scan_stop_event", None)
            if stop_event is not None:
                stop_event.set()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            with suppress(Exception):
                await context.__aexit__(None, None, None)


async def test_lifespan_body_and_cleanup_errors_do_not_skip_remaining_cleanup(tmp_path):
    app = create_app(db_path=str(tmp_path / "libsync.db"))
    first = _catalog_client(close=RuntimeError("close failed"))
    second = _catalog_client()
    app.state._clients_ref = {"qobuz": first, "tidal": second}
    app.state.sync_service.shutdown = AsyncMock(
        side_effect=RuntimeError("sync shutdown failed")
    )
    app.state.download_service.shutdown = AsyncMock()

    with pytest.raises(RuntimeError, match="lifespan body failed"):
        async with app.router.lifespan_context(app):
            raise RuntimeError("lifespan body failed")

    app.state.sync_service.shutdown.assert_awaited_once_with()
    app.state.download_service.shutdown.assert_awaited_once_with()
    first.__aexit__.assert_awaited_once_with(None, None, None)
    second.__aexit__.assert_awaited_once_with(None, None, None)
    assert app.state.shutting_down is True


async def test_shutdown_admission_rejects_new_background_and_credential_work(
    tmp_path, monkeypatch
):
    app = create_app(db_path=str(tmp_path / "libsync.db"))
    app.state.shutting_down = True
    app.state.download_service.enqueue = AsyncMock()
    app.state.sync_service.run_sync = AsyncMock()
    scan = AsyncMock(return_value={"status": "complete"})
    monkeypatch.setattr("backend.services.scan.run_scan", scan)
    reload_clients = AsyncMock()
    monkeypatch.setattr("backend.api.config._reload_clients", reload_clients)
    exchange = AsyncMock(
        return_value={
            "user_auth_token": "token",
            "user_id": "user",
            "app_id": "app",
            "display_name": "User",
        }
    )
    monkeypatch.setattr("qobuz.auth.exchange_code", exchange)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = [
            await client.post(
                "/api/downloads/queue",
                json={"source": "qobuz", "album_ids": ["album"]},
            ),
            await client.post("/api/sync/run/qobuz"),
            await client.post("/api/library/scan-fuzzy"),
            await client.patch("/api/config", json={"qobuz_token": "token"}),
            await client.post("/api/auth/qobuz/oauth-callback", json={"code": "code"}),
        ]

    try:
        assert [response.status_code for response in responses] == [503] * 5
        app.state.download_service.enqueue.assert_not_awaited()
        app.state.sync_service.run_sync.assert_not_awaited()
        scan.assert_not_awaited()
        reload_clients.assert_not_awaited()
        exchange.assert_not_awaited()
    finally:
        for job in app.state.scan_jobs.values():
            task = job.get("_task")
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
