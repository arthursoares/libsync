"""Test that auto-sync is re-evaluated after credential hot-reload.

Regression: a user can enable auto-sync BEFORE connecting Qobuz/Tidal.
At config-update time the loop is skipped because no source is connected.
Later when the user authenticates, ``_reload_clients`` runs but auto-sync
must also be re-checked, otherwise scheduled syncs stay off indefinitely.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import create_app


def _mock_qobuz_client():
    page = MagicMock()
    page.items = []
    page.total = 0
    page.limit = 500
    page.offset = 0

    client = MagicMock()
    client.favorites = MagicMock()
    client.favorites.get_albums = AsyncMock(return_value=page)
    client.catalog = MagicMock()
    client.catalog.search_albums = AsyncMock(return_value=page)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.fixture
def app():
    return create_app(db_path=":memory:")


class TestAutoSyncRevaluatedOnHotReload:
    async def test_enable_then_authenticate_starts_auto_sync(self, app, monkeypatch):
        sync_service = app.state.sync_service
        # Startup: no clients connected, no auto_sync task
        assert sync_service._auto_sync_task is None

        # User enables auto-sync BEFORE connecting any source.  The settings
        # endpoint runs _start_auto_sync_if_enabled but it short-circuits
        # because clients dict is empty ("no source connected; skipping").
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/config",
                json={"auto_sync_enabled": True, "auto_sync_interval": "60"},
            )
            assert resp.status_code == 200

        assert sync_service._auto_sync_task is None, (
            "precondition: auto-sync should NOT start while no clients are connected"
        )

        # User authenticates.  Patch _init_clients to return a mock so we
        # don't depend on real Qobuz network calls, and bypass the secret
        # resolver which would try real HTTP.
        mock_client = _mock_qobuz_client()
        monkeypatch.setattr(
            "backend.main._init_clients", lambda db: {"qobuz": mock_client}
        )
        monkeypatch.setattr(
            "backend.main._resolve_qobuz_credentials",
            AsyncMock(return_value=None),
        )

        # Trigger hot-reload via a credential write — this is what every
        # OAuth callback / manual-token path eventually does.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/config",
                json={"qobuz_token": "fake-token-from-oauth"},
            )
            assert resp.status_code == 200

        # Auto-sync MUST now be running — the user enabled it earlier and
        # a source has just become available.
        try:
            assert sync_service._auto_sync_task is not None, (
                "auto-sync did not start after hot-reload populated clients"
            )
            assert not sync_service._auto_sync_task.done()
        finally:
            sync_service.stop_auto_sync()
            await asyncio.sleep(0)

    async def test_hot_reload_does_not_double_start_when_already_running(
        self, app, monkeypatch
    ):
        """If auto-sync is already running, hot-reload must not launch a second loop."""
        sync_service = app.state.sync_service

        mock_client = _mock_qobuz_client()
        monkeypatch.setattr(
            "backend.main._init_clients", lambda db: {"qobuz": mock_client}
        )
        monkeypatch.setattr(
            "backend.main._resolve_qobuz_credentials",
            AsyncMock(return_value=None),
        )

        # First credential write: clients become available, auto-sync starts.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.patch(
                "/api/config",
                json={"auto_sync_enabled": True, "auto_sync_interval": "60"},
            )
            await client.patch("/api/config", json={"qobuz_token": "tok-1"})

        first_task = sync_service._auto_sync_task
        assert first_task is not None and not first_task.done()

        # Second credential write (e.g. token refresh) — auto-sync was
        # already running; the same task object should remain.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.patch("/api/config", json={"qobuz_token": "tok-2"})

        try:
            assert sync_service._auto_sync_task is first_task, (
                "hot-reload spawned a second auto-sync loop on top of the running one"
            )
        finally:
            sync_service.stop_auto_sync()
            await asyncio.sleep(0)
