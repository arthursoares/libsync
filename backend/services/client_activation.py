"""Safe, app-local streaming-client activation and operation admission."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from typing import Any

from .tasks import await_task_completion

logger = logging.getLogger("streamrip")

SOURCES = frozenset({"qobuz", "tidal"})
QOBUZ_CREDENTIAL_KEYS = frozenset(
    {"qobuz_token", "qobuz_user_id", "qobuz_app_id", "qobuz_app_secret"}
)
TIDAL_CREDENTIAL_KEYS = frozenset(
    {
        "tidal_access_token",
        "tidal_refresh_token",
        "tidal_user_id",
        "tidal_country_code",
        "tidal_token_expiry",
        "tidal_auth_method",
    }
)


class ClientReloadBusyError(RuntimeError):
    """A client reload conflicts with active work for the same source."""


class ClientActivationShuttingDownError(RuntimeError):
    """Client activation cannot finish after application shutdown begins."""


class ClientOperationRegistry:
    """Event-loop-owned source counters and credential-writer state.

    Registration and removal deliberately contain no await points.  This keeps
    the reload flag check and operation capture atomic on the event loop without
    holding locks across SDK requests.
    """

    def __init__(self) -> None:
        self.writer_lock = asyncio.Lock()
        self.reloading_sources: set[str] = set()
        self._active = Counter()
        self._claims: dict[object, frozenset[str]] = {}
        self.activation_tasks: set[asyncio.Task] = set()
        self.retirement_failures: list[tuple[str, Any]] = []
        self.shutting_down = False

    @staticmethod
    def _normalize_sources(sources) -> frozenset[str]:
        normalized = SOURCES if sources is None else frozenset(sources)
        if not normalized.issubset(SOURCES):
            return SOURCES
        return normalized

    def claim(self, sources) -> object:
        normalized = self._normalize_sources(sources)
        if self.shutting_down:
            raise ClientActivationShuttingDownError("Application is shutting down")
        if normalized & self.reloading_sources:
            names = ", ".join(sorted(normalized & self.reloading_sources))
            raise ClientReloadBusyError(
                f"Credentials for {names} are being updated; retry shortly"
            )
        token = object()
        self._claims[token] = normalized
        self._active.update(normalized)
        return token

    def release(self, token: object) -> None:
        sources = self._claims.pop(token, ())
        for source in sources:
            self._active[source] -= 1
            if self._active[source] <= 0:
                del self._active[source]

    @contextmanager
    def operation(self, sources):
        token = self.claim(sources)
        try:
            yield
        finally:
            self.release(token)

    def is_busy(self, sources) -> bool:
        return any(self._active[source] for source in self._normalize_sources(sources))

    def begin_shutdown(self) -> None:
        self.shutting_down = True


def affected_sources_for_updates(updates: Mapping[str, Any]) -> frozenset[str]:
    affected = set()
    keys = updates.keys()
    if QOBUZ_CREDENTIAL_KEYS & keys:
        affected.add("qobuz")
    if TIDAL_CREDENTIAL_KEYS & keys:
        affected.add("tidal")
    return frozenset(affected)


def _serialize_updates(updates: Mapping[str, Any]) -> dict[str, str]:
    return {key: str(value) for key, value in updates.items()}


async def _close_clients(clients: Mapping[str, Any]) -> None:
    for source, client in clients.items():
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            logger.exception("Failed to close staged %s client", source)


async def activate_config_updates(
    app,
    updates: Mapping[str, Any] | Callable[[], Awaitable[Mapping[str, Any] | None]],
    *,
    affected_sources: set[str] | frozenset[str] | None = None,
) -> dict[str, Any] | None:
    """Serialize, validate, atomically persist, and publish config updates.

    A callable is invoked only after writer reservation and busy checks.  Auth
    routes use that form so rejected requests do not consume OAuth codes or
    PKCE handles.  ``None`` means a non-terminal auth poll and performs no
    write or client activation.
    """

    registry: ClientOperationRegistry = app.state.client_operations
    task = asyncio.current_task()
    if task is not None:
        registry.activation_tasks.add(task)
    candidates: dict[str, Any] = {}
    try:
        async with registry.writer_lock:
            if registry.shutting_down or getattr(app.state, "shutting_down", False):
                raise ClientActivationShuttingDownError("Application is shutting down")

            if callable(updates):
                sources = frozenset(
                    affected_sources if affected_sources is not None else SOURCES
                )
            else:
                sources = frozenset(
                    affected_sources
                    if affected_sources is not None
                    else affected_sources_for_updates(updates)
                )
            if not sources.issubset(SOURCES):
                sources = SOURCES
            reserved_sources = sources

            download_service = app.state.download_service
            pending_busy = download_service.has_unfinished_for_sources(sources)
            if registry.is_busy(sources) or pending_busy:
                names = ", ".join(sorted(sources))
                raise ClientReloadBusyError(
                    f"{names.title()} is busy; retry the credential update shortly"
                )

            registry.reloading_sources.update(sources)
            try:
                prepared = await updates() if callable(updates) else dict(updates)
                if prepared is None:
                    return None
                prepared = dict(prepared)
                actual_sources = affected_sources_for_updates(prepared)
                if affected_sources is None:
                    sources = actual_sources
                elif not actual_sources.issubset(sources):
                    raise ValueError(
                        "Prepared credentials changed an unreserved source"
                    )

                current_config = app.state.db.get_all_config()
                serialized = _serialize_updates(prepared)
                staged = {**current_config, **serialized}

                from ..main import _init_client, _resolve_qobuz_credentials

                derived: dict[str, Any] = {}
                for source in sorted(sources):
                    candidate = _init_client(source, staged, strict=True)
                    if candidate is None:
                        continue
                    candidates[source] = candidate
                    await candidate.__aenter__()
                    if source == "qobuz":
                        resolved = await _resolve_qobuz_credentials(
                            staged, candidate, strict=True
                        )
                        derived.update(resolved or {})
                        if derived:
                            serialized.update(_serialize_updates(derived))
                            staged.update(_serialize_updates(derived))
                    await candidate.favorites.get_albums(limit=1, offset=0)

                async def commit_publish_retire() -> None:
                    if registry.shutting_down or getattr(
                        app.state, "shutting_down", False
                    ):
                        raise ClientActivationShuttingDownError(
                            "Application is shutting down"
                        )

                    await asyncio.to_thread(app.state.db.set_config_batch, serialized)

                    shared_clients = app.state._clients_ref
                    retired: dict[str, Any] = {}
                    for source in sources:
                        old = shared_clients.get(source)
                        candidate = candidates.get(source)
                        if candidate is None:
                            shared_clients.pop(source, None)
                        else:
                            shared_clients[source] = candidate
                        if old is not None and old is not candidate:
                            retired[source] = old
                    candidates.clear()  # Published clients now belong to the app.

                    for source, old in retired.items():
                        try:
                            await old.__aexit__(None, None, None)
                        except Exception:
                            registry.retirement_failures.append((source, old))
                            logger.exception(
                                "Failed to retire previous %s client; retained for shutdown",
                                source,
                            )

                    if (
                        sources
                        or {
                            "auto_sync_enabled",
                            "auto_sync_interval",
                        }
                        & serialized.keys()
                    ):
                        sync_service = app.state.sync_service
                        await sync_service.stop_auto_sync()
                        from ..main import _start_auto_sync_if_enabled

                        await _start_auto_sync_if_enabled(
                            app.state.db, sync_service, shared_clients
                        )

                commit_task = asyncio.create_task(commit_publish_retire())
                registry.activation_tasks.add(commit_task)
                commit_task.add_done_callback(registry.activation_tasks.discard)
                await await_task_completion(
                    commit_task, operation="credential activation commit and publish"
                )
                return {**prepared, **derived}
            finally:
                registry.reloading_sources.difference_update(reserved_sources)
    finally:
        if candidates:
            cleanup = asyncio.create_task(_close_clients(candidates))
            registry.activation_tasks.add(cleanup)
            cleanup.add_done_callback(registry.activation_tasks.discard)
            await await_task_completion(cleanup, operation="staged client cleanup")
        if task is not None:
            registry.activation_tasks.discard(task)
