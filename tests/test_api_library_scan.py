"""API tests for /scan-fuzzy, /mark-downloaded, /unmark-downloaded."""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import create_app


@pytest.fixture
def app():
    return create_app(db_path=":memory:")


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture(autouse=True)
def isolated_dedup_path(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMRIP_DB_PATH", str(tmp_path / "libsync.db"))


@pytest.fixture(autouse=True)
def catalog_client(app):
    tracks = [
        SimpleNamespace(
            id=f"t{i}",
            title=f"Track {i}",
            performer=SimpleNamespace(name="The Beatles"),
            track_number=i,
            disc_number=1,
            duration=180,
            explicit=False,
            isrc=None,
        )
        for i in range(1, 18)
    ]
    client = MagicMock()
    client.catalog.get_album_with_tracks = AsyncMock(
        return_value=(SimpleNamespace(tracks_count=17), tracks)
    )
    app.state._clients_ref["qobuz"] = client
    app.state.library_service.clients["qobuz"] = client
    return client


@pytest.fixture
def album_id(app):
    db = app.state.db
    aid = db.upsert_album(
        source="qobuz",
        source_album_id="42",
        title="Abbey Road",
        artist="The Beatles",
        track_count=17,
        bit_depth=24,
        sample_rate=96.0,
    )
    db.upsert_track(
        album_id=aid,
        source_track_id="t1",
        title="Come Together",
        artist="The Beatles",
        track_number=1,
    )
    return aid


class TestMarkDownloaded:
    async def test_happy_path(self, client, album_id):
        resp = await client.post(
            f"/api/library/albums/{album_id}/mark-downloaded",
            json={"local_folder_path": None},
        )
        assert resp.status_code == 200
        assert resp.json()["download_status"] == "complete"

    async def test_with_folder(self, client, app, album_id, tmp_path):
        folder = tmp_path / "Beatles - Abbey Road"
        folder.mkdir()
        app.state.db.set_config("downloads_path", str(tmp_path))
        resp = await client.post(
            f"/api/library/albums/{album_id}/mark-downloaded",
            json={"local_folder_path": str(folder)},
        )
        assert resp.status_code == 200
        assert resp.json()["local_folder_path"] == str(folder)

    async def test_unmark_reverses(self, client, album_id):
        await client.post(f"/api/library/albums/{album_id}/mark-downloaded", json={})
        resp = await client.post(f"/api/library/albums/{album_id}/unmark-downloaded")
        assert resp.status_code == 200
        assert resp.json()["download_status"] == "not_downloaded"

    async def test_unknown_album_returns_404(self, client, app):
        events = []

        async def record(data):
            events.append(data)

        app.state.event_bus.subscribe("album_status_changed", record)
        resp = await client.post("/api/library/albums/99999/mark-downloaded", json={})
        assert resp.status_code == 404
        assert events == []

    async def test_busy_album_reconciliation_returns_409_without_success_event(
        self, client, app, album_id
    ):
        app.state.db.update_album_status(album_id, "downloading")
        events = []

        async def record(data):
            events.append(data)

        app.state.event_bus.subscribe("album_status_changed", record)

        resp = await client.post(
            f"/api/library/albums/{album_id}/mark-downloaded", json={}
        )

        assert resp.status_code == 409
        assert "downloading" in resp.json()["error"]
        assert app.state.db.get_album(album_id)["download_status"] == "downloading"
        assert events == []

    async def test_locked_dedup_database_returns_error_without_success_event(
        self, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "libsync.db"
        monkeypatch.setenv("STREAMRIP_DB_PATH", str(db_path))
        locked_app = create_app(db_path=str(db_path))
        locked_app.state.db._sqlite_timeout = 0.05
        album_id = locked_app.state.db.upsert_album(
            "qobuz", "locked", "Locked", "Artist", track_count=1
        )
        track = SimpleNamespace(
            id="locked-track",
            title="Track",
            performer=SimpleNamespace(name="Artist"),
            track_number=1,
            disc_number=1,
            duration=180,
            explicit=False,
            isrc=None,
        )
        sdk_client = MagicMock()
        sdk_client.catalog.get_album_with_tracks = AsyncMock(
            return_value=(SimpleNamespace(tracks_count=1), [track])
        )
        locked_app.state._clients_ref["qobuz"] = sdk_client
        dedup_path = tmp_path / "downloads.db"
        lock = sqlite3.connect(dedup_path)
        lock.execute("PRAGMA journal_mode=WAL")
        lock.execute("CREATE TABLE downloads (id TEXT PRIMARY KEY)")
        lock.commit()
        lock.execute("BEGIN IMMEDIATE")
        events = []

        async def record(data):
            events.append(data)

        locked_app.state.event_bus.subscribe("album_status_changed", record)
        resp = None
        try:
            async with AsyncClient(
                transport=ASGITransport(app=locked_app, raise_app_exceptions=False),
                base_url="http://test",
            ) as locked_client:
                resp = await locked_client.post(
                    f"/api/library/albums/{album_id}/mark-downloaded", json={}
                )
        finally:
            lock.rollback()
            lock.close()

        assert resp is not None
        assert resp.status_code == 500
        assert locked_app.state.db.get_album(album_id)["download_status"] == (
            "not_downloaded"
        )
        assert events == []

    async def test_rejects_path_outside_downloads_root(
        self, client, app, album_id, tmp_path
    ):
        app.state.db.set_config("downloads_path", str(tmp_path / "music"))
        (tmp_path / "music").mkdir()
        # Request a path that escapes the downloads root.
        resp = await client.post(
            f"/api/library/albums/{album_id}/mark-downloaded",
            json={"local_folder_path": str(tmp_path / "elsewhere")},
        )
        assert resp.status_code == 400

    async def test_rejects_dot_dot_traversal(self, client, app, album_id, tmp_path):
        downloads_root = tmp_path / "music"
        downloads_root.mkdir()
        app.state.db.set_config("downloads_path", str(downloads_root))
        # Traverses back up with ..
        resp = await client.post(
            f"/api/library/albums/{album_id}/mark-downloaded",
            json={"local_folder_path": f"{downloads_root}/../evil"},
        )
        assert resp.status_code == 400

    async def test_accepts_path_inside_downloads_root(
        self, client, app, album_id, tmp_path
    ):
        downloads_root = tmp_path / "music"
        (downloads_root / "Album").mkdir(parents=True)
        app.state.db.set_config("downloads_path", str(downloads_root))
        resp = await client.post(
            f"/api/library/albums/{album_id}/mark-downloaded",
            json={"local_folder_path": str(downloads_root / "Album")},
        )
        assert resp.status_code == 200

    async def test_lowercase_sentinel_config_still_writes_sentinel(
        self, client, app, album_id, tmp_path
    ):
        """Regression for #29: the config API always persists booleans as
        "True" (str(True)), but a value stored as lowercase "true" — e.g.
        by a future writer, or a hand-edited DB — must not silently disable
        sentinel writes via a case-sensitive `== "True"` comparison.
        """
        downloads_root = tmp_path / "music"
        (downloads_root / "Album").mkdir(parents=True)
        app.state.db.set_config("downloads_path", str(downloads_root))
        # Bypass the config API (which always writes "True") to simulate a
        # lowercase-stored value.
        app.state.db.set_config("scan_sentinel_write_enabled", "true")

        resp = await client.post(
            f"/api/library/albums/{album_id}/mark-downloaded",
            json={"local_folder_path": str(downloads_root / "Album")},
        )
        assert resp.status_code == 200
        sentinel = downloads_root / "Album" / ".streamrip.json"
        assert sentinel.exists()

    async def test_missing_client_fails_closed_without_event_or_state_change(
        self, client, app, album_id
    ):
        app.state._clients_ref.clear()
        events = []

        async def record(data):
            events.append(data)

        app.state.event_bus.subscribe("album_status_changed", record)

        resp = await client.post(
            f"/api/library/albums/{album_id}/mark-downloaded", json={}
        )

        assert resp.status_code == 503
        assert resp.json()["error"] == "Connect the album source and retry."
        assert app.state.db.get_album(album_id)["download_status"] == "not_downloaded"
        assert events == []

    @pytest.mark.parametrize(
        ("action", "initial_status"),
        [
            ("mark-downloaded", "not_downloaded"),
            ("unmark-downloaded", "complete"),
        ],
    )
    async def test_catalog_sdk_errors_are_sanitized_without_mutation_or_event(
        self,
        client,
        app,
        album_id,
        catalog_client,
        action,
        initial_status,
    ):
        marker = "sdk-internal-marker /private/credentials.json"
        if initial_status == "complete":
            app.state.db.set_album_download_state(
                album_id, downloaded_at="2026-09-06T00:00:00"
            )
        before = dict(app.state.db.get_album(album_id))
        catalog_client.catalog.get_album_with_tracks.side_effect = RuntimeError(marker)
        events = []

        async def record(data):
            events.append(data)

        app.state.event_bus.subscribe("album_status_changed", record)

        response = await client.post(
            f"/api/library/albums/{album_id}/{action}",
            json={} if action == "mark-downloaded" else None,
        )

        assert response.status_code == 502
        assert response.json()["error"] == (
            "Could not load a complete track catalog. Retry later."
        )
        assert marker not in response.text
        assert dict(app.state.db.get_album(album_id)) == before
        assert events == []

    @pytest.mark.parametrize(
        ("catalog_result", "catalog_error", "message"),
        [
            (
                (SimpleNamespace(tracks_count=2), [SimpleNamespace(id="only-one")]),
                None,
                "incomplete",
            ),
            ((SimpleNamespace(tracks_count=0), []), None, "empty authoritative"),
            (
                (
                    SimpleNamespace(tracks_count=2),
                    [SimpleNamespace(id="same"), SimpleNamespace(id="same")],
                ),
                None,
                "duplicate",
            ),
            (
                (SimpleNamespace(tracks_count=1), [SimpleNamespace(id=None)]),
                None,
                "without an identity",
            ),
            (None, OSError("catalog offline"), "catalog offline"),
        ],
    )
    async def test_catalog_failure_fails_closed_without_any_success_mutation(
        self,
        client,
        app,
        album_id,
        catalog_client,
        tmp_path,
        catalog_result,
        catalog_error,
        message,
    ):
        folder = tmp_path / "music" / "Album"
        folder.mkdir(parents=True)
        app.state.db.set_config("downloads_path", str(tmp_path / "music"))
        catalog_client.catalog.get_album_with_tracks.return_value = catalog_result
        catalog_client.catalog.get_album_with_tracks.side_effect = catalog_error
        events = []

        async def record(data):
            events.append(data)

        app.state.event_bus.subscribe("album_status_changed", record)

        resp = await client.post(
            f"/api/library/albums/{album_id}/mark-downloaded",
            json={"local_folder_path": str(folder)},
        )

        assert resp.status_code == 502
        assert resp.json()["error"] == (
            "Could not load a complete track catalog. Retry later."
        )
        assert message not in resp.text
        assert app.state.db.get_album(album_id)["download_status"] == "not_downloaded"
        assert not (folder / ".streamrip.json").exists()
        assert not (tmp_path / "downloads.db").exists()
        assert events == []

    async def test_downloaded_without_detail_unmark_removes_all_catalog_ids(
        self, client, app, album_id, tmp_path
    ):
        app.state.db.set_album_download_state(
            album_id, downloaded_at="2026-09-05T00:00:00"
        )
        dedup_path = tmp_path / "downloads.db"
        conn = sqlite3.connect(dedup_path)
        try:
            conn.execute("CREATE TABLE downloads (id TEXT PRIMARY KEY)")
            conn.executemany(
                "INSERT INTO downloads (id) VALUES (?)",
                [(f"t{i}",) for i in range(1, 18)],
            )
            conn.commit()
        finally:
            conn.close()
        assert len(app.state.db.get_tracks(album_id)) == 1

        resp = await client.post(f"/api/library/albums/{album_id}/unmark-downloaded")

        assert resp.status_code == 200
        assert resp.json()["download_status"] == "not_downloaded"
        conn = sqlite3.connect(dedup_path)
        try:
            assert conn.execute("SELECT id FROM downloads").fetchall() == []
        finally:
            conn.close()

    async def test_unmark_offline_leaves_state_sentinel_dedup_and_events_untouched(
        self, client, app, album_id, catalog_client, tmp_path
    ):
        folder = tmp_path / "music" / "Album"
        folder.mkdir(parents=True)
        sentinel = folder / ".streamrip.json"
        sentinel.write_text("{}")
        app.state.db.set_album_download_state(
            album_id,
            downloaded_at="2026-09-05T00:00:00",
            local_folder_path=str(folder),
        )
        dedup_path = tmp_path / "downloads.db"
        conn = sqlite3.connect(dedup_path)
        try:
            conn.execute("CREATE TABLE downloads (id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO downloads (id) VALUES ('t1')")
            conn.commit()
        finally:
            conn.close()
        catalog_client.catalog.get_album_with_tracks.side_effect = OSError("offline")
        events = []

        async def record(data):
            events.append(data)

        app.state.event_bus.subscribe("album_status_changed", record)

        resp = await client.post(f"/api/library/albums/{album_id}/unmark-downloaded")

        assert resp.status_code == 502
        album = app.state.db.get_album(album_id)
        assert album["download_status"] == "complete"
        assert album["local_folder_path"] == str(folder)
        assert sentinel.exists()
        conn = sqlite3.connect(dedup_path)
        try:
            assert conn.execute("SELECT id FROM downloads").fetchall() == [("t1",)]
        finally:
            conn.close()
        assert events == []


class TestScanFuzzy:
    async def test_starts_job_and_returns_id(self, client, app, album_id, tmp_path):
        app.state.db.set_config("downloads_path", str(tmp_path / "music"))
        (tmp_path / "music").mkdir()

        resp = await client.post("/api/library/scan-fuzzy")
        assert resp.status_code == 200
        assert "job_id" in resp.json()
        # Let the background task finish so it doesn't leak into the next test.
        job_id = resp.json()["job_id"]
        for _ in range(20):
            st = (await client.get(f"/api/library/scan-fuzzy/{job_id}")).json()
            if st["status"] == "complete":
                break
            await asyncio.sleep(0.05)

    async def test_concurrent_returns_409(
        self, client, app, album_id, tmp_path, monkeypatch
    ):
        app.state.db.set_config("downloads_path", str(tmp_path / "music"))
        (tmp_path / "music").mkdir()

        # Force the job to hang so we can fire a concurrent start. The route
        # imports the module (`from ..services import scan as scan_service`)
        # and looks up `run_scan` at call time, so monkeypatching the
        # attribute on the module is what we want.
        import backend.services.scan as scan_mod

        original = scan_mod.run_scan

        async def slow(*args, **kwargs):
            await asyncio.sleep(0.3)
            return await original(*args, **kwargs)

        monkeypatch.setattr(scan_mod, "run_scan", slow)

        r1 = await client.post("/api/library/scan-fuzzy")
        r2 = await client.post("/api/library/scan-fuzzy")
        assert r1.status_code == 200
        assert r2.status_code == 409
        # Drain the first job.
        job_id = r1.json()["job_id"]
        for _ in range(20):
            st = (await client.get(f"/api/library/scan-fuzzy/{job_id}")).json()
            if st["status"] != "running":
                break
            await asyncio.sleep(0.1)

    async def test_status_endpoint(self, client, app, album_id, tmp_path):
        app.state.db.set_config("downloads_path", str(tmp_path / "music"))
        (tmp_path / "music").mkdir()

        job_id = (await client.post("/api/library/scan-fuzzy")).json()["job_id"]
        for _ in range(40):
            resp = await client.get(f"/api/library/scan-fuzzy/{job_id}")
            if resp.json()["status"] == "complete":
                break
            await asyncio.sleep(0.05)
        body = resp.json()
        assert body["status"] == "complete"
        assert "auto_matched" in body

    async def test_running_status_includes_progress(
        self, client, app, album_id, tmp_path, monkeypatch
    ):
        """Polling a running scan must return scanned / total so the UI can
        render "Scanning… X / N" instead of sitting at 0 / ?."""
        music = tmp_path / "music"
        music.mkdir()
        # Create 3 album-shaped folders so the scan has work to report on.
        for name in ("A", "B", "C"):
            folder = music / name
            folder.mkdir()
            (folder / "01.flac").touch()
        app.state.db.set_config("downloads_path", str(music))

        # Slow the scan down so we can catch it mid-run.
        import backend.services.scan as scan_mod

        original = scan_mod.run_scan

        async def slow(*args, **kwargs):
            await asyncio.sleep(0.2)
            return await original(*args, **kwargs)

        monkeypatch.setattr(scan_mod, "run_scan", slow)

        job_id = (await client.post("/api/library/scan-fuzzy")).json()["job_id"]
        # Poll until we see a running response (should happen well before the job completes).
        running_body = None
        for _ in range(20):
            resp = await client.get(f"/api/library/scan-fuzzy/{job_id}")
            body = resp.json()
            if body["status"] == "running":
                running_body = body
                break
            await asyncio.sleep(0.01)

        assert running_body is not None, "never caught the job in a running state"
        assert "scanned" in running_body
        assert "total" in running_body
        assert isinstance(running_body["scanned"], int)
        assert isinstance(running_body["total"], int)

        # Drain the job so the fixture teardown doesn't leak an active task.
        for _ in range(40):
            resp = await client.get(f"/api/library/scan-fuzzy/{job_id}")
            if resp.json()["status"] == "complete":
                break
            await asyncio.sleep(0.05)


class TestScanJobRegistryIsBounded:
    """`app.state.scan_jobs` used to grow for the life of the process."""

    def test_prune_keeps_only_the_newest_finished_jobs(self):
        from backend.api.library import MAX_FINISHED_SCAN_JOBS, _prune_scan_jobs

        jobs = {
            f"job-{i:03d}": {"status": "complete", "result": {}}
            for i in range(MAX_FINISHED_SCAN_JOBS + 15)
        }
        _prune_scan_jobs(jobs, active_job_id=None)

        assert len(jobs) == MAX_FINISHED_SCAN_JOBS
        # Insertion order is age order, so the survivors are the newest.
        assert list(jobs) == [
            f"job-{i:03d}" for i in range(15, MAX_FINISHED_SCAN_JOBS + 15)
        ]

    def test_prune_never_evicts_the_active_or_a_running_job(self):
        from backend.api.library import MAX_FINISHED_SCAN_JOBS, _prune_scan_jobs

        jobs = {"job-active": {"status": "running", "progress": {}}}
        jobs["job-other-running"] = {"status": "running", "progress": {}}
        jobs.update(
            {
                f"job-{i:03d}": {"status": "complete", "result": {}}
                for i in range(MAX_FINISHED_SCAN_JOBS + 15)
            }
        )
        _prune_scan_jobs(jobs, active_job_id="job-active")

        assert "job-active" in jobs
        assert "job-other-running" in jobs
        finished = [k for k, v in jobs.items() if v["status"] == "complete"]
        assert len(finished) == MAX_FINISHED_SCAN_JOBS

    def test_prune_leaves_a_short_registry_alone(self):
        from backend.api.library import _prune_scan_jobs

        jobs = {f"job-{i}": {"status": "complete", "result": {}} for i in range(3)}
        _prune_scan_jobs(jobs, active_job_id=None)
        assert len(jobs) == 3

    async def test_starting_a_scan_trims_the_registry(
        self, client, app, album_id, tmp_path
    ):
        from backend.api.library import MAX_FINISHED_SCAN_JOBS

        app.state.db.set_config("downloads_path", str(tmp_path / "music"))
        (tmp_path / "music").mkdir()

        for i in range(MAX_FINISHED_SCAN_JOBS + 25):
            app.state.scan_jobs[f"old-{i:03d}"] = {"status": "complete", "result": {}}

        resp = await client.post("/api/library/scan-fuzzy")
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        # 20 retained old jobs plus the one just started.
        assert len(app.state.scan_jobs) == MAX_FINISHED_SCAN_JOBS + 1
        assert job_id in app.state.scan_jobs
        assert "old-000" not in app.state.scan_jobs
        assert f"old-{MAX_FINISHED_SCAN_JOBS + 24:03d}" in app.state.scan_jobs

        for _ in range(20):
            st = (await client.get(f"/api/library/scan-fuzzy/{job_id}")).json()
            if st["status"] == "complete":
                break
            await asyncio.sleep(0.05)
