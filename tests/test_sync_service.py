"""Tests for SyncService."""
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.models.database import AppDatabase
from backend.services.event_bus import EventBus
from backend.services.library import LibraryService
from backend.services.sync import SyncService


@pytest.fixture
def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    database = AppDatabase(path)
    yield database
    os.unlink(path)


@pytest.fixture
def event_bus():
    return EventBus()


class TestSyncService:
    async def test_get_diff_no_client(self, db, event_bus):
        library_service = LibraryService(db, event_bus, clients={})
        service = SyncService(db, event_bus, clients={}, library_service=library_service)
        diff = await service.get_diff("qobuz")
        assert diff["new_albums"] == []
        assert diff["removed_albums"] == []
        assert diff["source"] == "qobuz"

    async def test_run_sync_records_history(self, db, event_bus):
        # Mock the Qobuz SDK client's favorites namespace. Empty result
        # page → no albums to sync.
        empty_page = MagicMock()
        empty_page.items = []
        empty_page.total = 0
        empty_page.limit = 500

        mock_client = MagicMock()
        mock_client.favorites = MagicMock()
        mock_client.favorites.get_albums = AsyncMock(return_value=empty_page)

        clients = {"qobuz": mock_client}
        library_service = LibraryService(db, event_bus, clients=clients)
        service = SyncService(db, event_bus, clients=clients, library_service=library_service)

        result = await service.run_sync("qobuz")
        assert result["status"] == "complete"

        history = await service.get_history("qobuz")
        assert len(history) == 1
        assert history[0]["status"] == "complete"

    async def test_run_sync_marks_history_failed_when_refresh_errors(self, db, event_bus):
        class FailingLibraryService(LibraryService):
            async def refresh_library(self, source):
                raise RuntimeError("boom")

        service = SyncService(
            db,
            event_bus,
            clients={},
            library_service=FailingLibraryService(db, event_bus, {}),
        )

        result = await service.run_sync("qobuz")
        assert result["status"] == "failed"

        history = await service.get_history("qobuz")
        assert len(history) == 1
        assert history[0]["status"] == "failed"
        assert history[0]["completed_at"] is not None
