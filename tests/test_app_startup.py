"""Test that the FastAPI app starts and serves basic routes."""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient

from backend.main import create_app
from backend.models.database import AppDatabase


class TestAppStartup:
    async def test_health_check(self):
        app = create_app(db_path=":memory:")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_auth_status(self):
        app = create_app(db_path=":memory:")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/auth/status")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestLifespanShutdown:
    async def test_shutdown_closes_current_clients_ref_not_stale_closure(self):
        """Regression for #26: lifespan shutdown must close whatever is on
        app.state._clients_ref at shutdown time, not the `clients` dict that
        was captured in the closure when create_app() ran. _reload_clients()
        (backend/api/config.py) swaps in a brand-new dict on hot-reload —
        closing the stale closure copy re-closes already-closed sessions and
        leaks the live ones.
        """
        app = create_app(db_path=":memory:")

        # Simulate a credential hot-reload having replaced the clients dict,
        # the way _reload_clients() does, with a fake client we can inspect.
        fake_client = MagicMock()
        fake_client.__aexit__ = AsyncMock(return_value=None)
        app.state._clients_ref = {"qobuz": fake_client}

        async with app.router.lifespan_context(app):
            pass

        fake_client.__aexit__.assert_awaited_once_with(None, None, None)


class TestBootRecoversStuckDownloadStatuses:
    """Regression for #32 (and the restart-recovery test #49 asked for):
    the download queue is in-memory only, so a `queued`/`downloading` row
    left behind by a crashed or restarted backend must be normalised back
    to `not_downloaded` when the app is (re)constructed.
    """

    def test_restart_resets_queued_and_downloading_albums(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            seed_db = AppDatabase(db_path)
            queued_id = seed_db.upsert_album("qobuz", "a1", "Queued Album", "Artist")
            seed_db.update_album_status(queued_id, "queued")
            downloading_id = seed_db.upsert_album(
                "qobuz", "a2", "Downloading Album", "Artist"
            )
            seed_db.update_album_status(downloading_id, "downloading")
            complete_id = seed_db.upsert_album(
                "qobuz", "a3", "Complete Album", "Artist"
            )
            seed_db.update_album_status(complete_id, "complete")
            failed_id = seed_db.upsert_album("qobuz", "a4", "Failed Album", "Artist")
            seed_db.update_album_status(failed_id, "failed")

            app = create_app(db_path=db_path)

            assert (
                app.state.db.get_album(queued_id)["download_status"] == "not_downloaded"
            )
            assert (
                app.state.db.get_album(downloading_id)["download_status"]
                == "not_downloaded"
            )
            assert app.state.db.get_album(complete_id)["download_status"] == "complete"
            assert app.state.db.get_album(failed_id)["download_status"] == "failed"
        finally:
            os.unlink(db_path)

    def test_logs_at_info_when_albums_were_reset(self, caplog):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            seed_db = AppDatabase(db_path)
            album_id = seed_db.upsert_album("qobuz", "a1", "Album", "Artist")
            seed_db.update_album_status(album_id, "downloading")

            with caplog.at_level("INFO", logger="streamrip"):
                create_app(db_path=db_path)

            assert "Reset 1 albums stuck in queued/downloading" in caplog.text
        finally:
            os.unlink(db_path)

    def test_no_reset_log_when_nothing_stuck(self, caplog):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            with caplog.at_level("INFO", logger="streamrip"):
                create_app(db_path=db_path)

            assert "stuck in queued/downloading" not in caplog.text
        finally:
            os.unlink(db_path)
