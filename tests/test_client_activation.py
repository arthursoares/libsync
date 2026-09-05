"""Transactional credential activation and source-operation admission."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import create_app
from backend.services.client_activation import (
    ClientActivationShuttingDownError,
    ClientReloadBusyError,
    activate_config_updates,
)


class FakeClient:
    def __init__(
        self,
        *,
        enter_error: Exception | None = None,
        validation_error: Exception | None = None,
        validation_started: asyncio.Event | None = None,
        validation_release: asyncio.Event | None = None,
        close_error: Exception | None = None,
    ):
        self.enter_error = enter_error
        self.close_error = close_error
        self.enter_calls = 0
        self.close_calls = 0

        async def validate(*, limit, offset):
            assert (limit, offset) == (1, 0)
            if validation_started is not None:
                validation_started.set()
            if validation_release is not None:
                await validation_release.wait()
            if validation_error is not None:
                raise validation_error
            return type("Page", (), {"items": []})()

        self.favorites = SimpleNamespace(get_albums=validate)

    async def __aenter__(self):
        self.enter_calls += 1
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture
def app(monkeypatch):
    application = create_app(db_path=":memory:")
    monkeypatch.setattr(
        "backend.main._resolve_qobuz_credentials", AsyncMock(return_value={})
    )
    return application


def install_factory(monkeypatch, clients: dict[str, FakeClient | Exception]):
    def build(source, config, *, strict=False):
        token_key = "qobuz_token" if source == "qobuz" else "tidal_access_token"
        if not config.get(token_key):
            return None
        value = clients[source]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr("backend.main._init_client", build)


@pytest.mark.parametrize(
    "candidate",
    [
        RuntimeError("constructor failed"),
        FakeClient(enter_error=RuntimeError("open failed")),
        FakeClient(validation_error=RuntimeError("validation failed")),
    ],
)
async def test_candidate_failures_preserve_db_and_old_identity(
    app, monkeypatch, candidate
):
    old = FakeClient()
    app.state._clients_ref["qobuz"] = old
    app.state.db.set_config("qobuz_token", "old-token")
    install_factory(monkeypatch, {"qobuz": candidate})

    with pytest.raises(RuntimeError):
        await activate_config_updates(app, {"qobuz_token": "new-token"})

    assert app.state.db.get_config("qobuz_token") == "old-token"
    assert app.state._clients_ref["qobuz"] is old
    assert old.close_calls == 0
    if isinstance(candidate, FakeClient):
        assert candidate.close_calls == 1


async def test_mixed_source_partial_failure_closes_candidates_only(app, monkeypatch):
    old_qobuz = FakeClient()
    old_tidal = FakeClient()
    candidate_qobuz = FakeClient()
    shared = app.state._clients_ref
    shared.update(qobuz=old_qobuz, tidal=old_tidal)
    app.state.db.set_config_batch(
        {"qobuz_token": "old-q", "tidal_access_token": "old-t"}
    )
    install_factory(
        monkeypatch,
        {"qobuz": candidate_qobuz, "tidal": RuntimeError("tidal failed")},
    )

    with pytest.raises(RuntimeError, match="tidal failed"):
        await activate_config_updates(
            app,
            {"qobuz_token": "new-q", "tidal_access_token": "new-t"},
        )

    assert shared == {"qobuz": old_qobuz, "tidal": old_tidal}
    assert candidate_qobuz.close_calls == 1
    assert old_qobuz.close_calls == old_tidal.close_calls == 0


async def test_database_failure_does_not_publish(app, monkeypatch):
    old = FakeClient()
    candidate = FakeClient()
    app.state._clients_ref["tidal"] = old
    app.state.db.set_config("tidal_access_token", "old")
    install_factory(monkeypatch, {"tidal": candidate})
    monkeypatch.setattr(
        app.state.db,
        "set_config_batch",
        lambda updates: (_ for _ in ()).throw(RuntimeError("db failed")),
    )

    with pytest.raises(RuntimeError, match="db failed"):
        await activate_config_updates(app, {"tidal_access_token": "new"})

    assert app.state._clients_ref["tidal"] is old
    assert old.close_calls == 0
    assert candidate.close_calls == 1


async def test_active_and_pending_work_reject_updates_without_closing(app):
    old = FakeClient()
    app.state._clients_ref["qobuz"] = old
    registry = app.state.client_operations

    with registry.operation({"qobuz"}):
        with pytest.raises(ClientReloadBusyError):
            await activate_config_updates(app, {"qobuz_token": "new"})

    app.state.download_service._queue.append({"source": "qobuz", "status": "pending"})
    with pytest.raises(ClientReloadBusyError):
        await activate_config_updates(app, {"qobuz_token": "new"})
    assert old.close_calls == 0


async def test_request_scan_queue_and_soft_cancel_busy_states_return_409(app):
    registry = app.state.client_operations
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for sources in ({"qobuz"}, {"qobuz", "tidal"}):
            claim = registry.claim(sources)
            try:
                response = await client.patch(
                    "/api/config", json={"qobuz_token": "new"}
                )
                assert response.status_code == 409
            finally:
                registry.release(claim)

        app.state.download_service._queue.append(
            {"source": "qobuz", "status": "pending"}
        )
        response = await client.patch("/api/config", json={"qobuz_token": "new"})
        assert response.status_code == 409
        app.state.download_service._queue.clear()

        # Soft cancellation changes the visible queue state, but the worker's
        # operation claim remains authoritative until the SDK call returns.
        app.state.download_service._queue.append(
            {"source": "qobuz", "status": "cancelled"}
        )
        with registry.operation({"qobuz"}):
            response = await client.patch("/api/config", json={"qobuz_token": "new"})
            assert response.status_code == 409


async def test_requests_during_candidate_validation_get_409(app, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    candidate = FakeClient(validation_started=started, validation_release=release)
    install_factory(monkeypatch, {"qobuz": candidate})

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        update = asyncio.create_task(
            client.patch("/api/config", json={"qobuz_token": "new"})
        )
        await started.wait()
        response = await client.get("/api/library/search/qobuz", params={"q": "x"})
        assert response.status_code == 409
        release.set()
        assert (await update).status_code == 200


async def test_concurrent_writers_are_serialized_and_keep_both_updates(app):
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first():
        first_entered.set()
        await release_first.wait()
        return {"folder_format": "first"}

    async def second():
        second_entered.set()
        return {"track_format": "second"}

    first_task = asyncio.create_task(
        activate_config_updates(app, first, affected_sources=set())
    )
    await first_entered.wait()
    second_task = asyncio.create_task(
        activate_config_updates(app, second, affected_sources=set())
    )
    await asyncio.sleep(0)
    assert not second_entered.is_set()
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert app.state.db.get_config("folder_format") == "first"
    assert app.state.db.get_config("track_format") == "second"


async def test_config_only_update_remains_available_during_client_work(app):
    with app.state.client_operations.operation({"qobuz"}):
        await activate_config_updates(app, {"folder_format": "available"})
    assert app.state.db.get_config("folder_format") == "available"


async def test_callable_without_source_hint_clears_every_reload_flag(app):
    async def prepare():
        return {"folder_format": "updated"}

    await activate_config_updates(app, prepare)
    assert app.state.client_operations.reloading_sources == set()


async def test_cancellation_before_commit_closes_candidate_and_preserves_old(
    app, monkeypatch
):
    started = asyncio.Event()
    release = asyncio.Event()
    old = FakeClient()
    candidate = FakeClient(validation_started=started, validation_release=release)
    app.state._clients_ref["tidal"] = old
    app.state.db.set_config("tidal_access_token", "old")
    install_factory(monkeypatch, {"tidal": candidate})

    task = asyncio.create_task(
        activate_config_updates(app, {"tidal_access_token": "new"})
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert app.state.db.get_config("tidal_access_token") == "old"
    assert app.state._clients_ref["tidal"] is old
    assert candidate.close_calls == 1


async def test_cancellation_during_commit_finishes_publication(app, monkeypatch):
    old = FakeClient()
    candidate = FakeClient()
    app.state._clients_ref["tidal"] = old
    app.state.db.set_config("tidal_access_token", "old")
    install_factory(monkeypatch, {"tidal": candidate})
    original = app.state.db.set_config_batch
    entered = threading.Event()
    release = threading.Event()

    def blocked_commit(updates):
        entered.set()
        release.wait()
        original(updates)

    monkeypatch.setattr(app.state.db, "set_config_batch", blocked_commit)
    task = asyncio.create_task(
        activate_config_updates(app, {"tidal_access_token": "new"})
    )
    await asyncio.to_thread(entered.wait)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert app.state.db.get_config("tidal_access_token") == "new"
    assert app.state._clients_ref["tidal"] is candidate
    assert old.close_calls == 1


async def test_shutdown_race_cannot_publish_late_candidate(app, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    old = FakeClient()
    candidate = FakeClient(validation_started=started, validation_release=release)
    app.state._clients_ref["tidal"] = old
    app.state.db.set_config("tidal_access_token", "old")
    install_factory(monkeypatch, {"tidal": candidate})

    task = asyncio.create_task(
        activate_config_updates(app, {"tidal_access_token": "new"})
    )
    await started.wait()
    app.state.shutting_down = True
    app.state.client_operations.begin_shutdown()
    release.set()
    with pytest.raises(ClientActivationShuttingDownError):
        await task
    assert app.state._clients_ref["tidal"] is old
    assert app.state.db.get_config("tidal_access_token") == "old"
    assert candidate.close_calls == 1


async def test_retirement_close_failure_does_not_rollback(app, monkeypatch):
    old = FakeClient(close_error=RuntimeError("close failed"))
    candidate = FakeClient()
    app.state._clients_ref["tidal"] = old
    app.state.db.set_config("tidal_access_token", "old")
    install_factory(monkeypatch, {"tidal": candidate})

    await activate_config_updates(app, {"tidal_access_token": "new"})

    assert app.state.db.get_config("tidal_access_token") == "new"
    assert app.state._clients_ref["tidal"] is candidate
    assert app.state.client_operations.retirement_failures == [("tidal", old)]


async def test_clearing_credentials_disconnects_idle_source(app):
    old = FakeClient()
    app.state._clients_ref["tidal"] = old
    app.state.db.set_config("tidal_access_token", "old")

    await activate_config_updates(app, {"tidal_access_token": ""})

    assert "tidal" not in app.state._clients_ref
    assert app.state.db.get_config("tidal_access_token") == ""
    assert old.close_calls == 1


def test_unknown_operation_source_is_conservatively_scoped_to_both(app):
    registry = app.state.client_operations
    with registry.operation({"unknown"}):
        assert registry.is_busy({"qobuz"})
        assert registry.is_busy({"tidal"})
