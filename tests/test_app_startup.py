"""Test that the FastAPI app starts and serves basic routes."""

from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient

from backend.main import create_app


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
