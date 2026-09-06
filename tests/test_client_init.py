"""Tests for client initialization and hot-reload."""

import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.main import _init_clients, _resolve_qobuz_credentials
from backend.models.database import AppDatabase


@pytest.fixture
def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    database = AppDatabase(path)
    yield database
    os.unlink(path)


class TestInitClients:
    def test_returns_empty_when_no_credentials(self, db):
        """Should return empty dict when no tokens are stored."""
        clients = _init_clients(db)
        assert clients == {}

    def test_returns_empty_when_only_user_id(self, db):
        """Should not create client with user_id but no token."""
        db.set_config("qobuz_user_id", "12345")
        clients = _init_clients(db)
        assert "qobuz" not in clients

    def test_creates_qobuz_client_with_credentials(self, db):
        """Should create SDK QobuzClient when token exists."""
        db.set_config("qobuz_token", "fake-token")
        db.set_config("qobuz_user_id", "12345")

        with patch("qobuz.QobuzClient") as mock_client:
            mock_client.return_value = MagicMock()
            clients = _init_clients(db)

        assert "qobuz" in clients
        mock_client.assert_called_once()
        call_kwargs = mock_client.call_args[1]
        assert call_kwargs["user_auth_token"] == "fake-token"

    def test_handles_import_error_gracefully(self, db):
        """Should not crash if qobuz SDK import fails."""
        db.set_config("qobuz_token", "fake-token")

        with patch("qobuz.QobuzClient", side_effect=ImportError("no module")):
            clients = _init_clients(db)
            assert "qobuz" not in clients


async def test_strict_qobuz_resolution_keeps_staged_token_app_id(monkeypatch):
    client = SimpleNamespace(
        _transport=SimpleNamespace(app_id="token-bound-app"),
        streaming=SimpleNamespace(_app_secret=None),
        _app_secret_cached=False,
    )
    monkeypatch.setattr(
        "qobuz.spoofer.fetch_app_credentials",
        AsyncMock(return_value=("bundle-app", ["secret"])),
    )
    monkeypatch.setattr(
        "qobuz.spoofer.find_working_secret", AsyncMock(return_value="secret")
    )

    derived = await _resolve_qobuz_credentials(
        {"qobuz_token": "token", "qobuz_app_id": "token-bound-app"},
        client,
        strict=True,
    )

    assert derived == {}
    assert client._transport.app_id == "token-bound-app"
    assert client.streaming._app_secret == "secret"


async def test_strict_qobuz_resolution_surfaces_signing_verification_error(
    monkeypatch,
):
    client = SimpleNamespace(
        _transport=SimpleNamespace(app_id="web-app"),
        streaming=SimpleNamespace(_app_secret=None),
        _app_secret_cached=False,
    )
    monkeypatch.setattr(
        "qobuz.spoofer.fetch_app_credentials",
        AsyncMock(return_value=("bundle-app", ["secret"])),
    )
    monkeypatch.setattr(
        "qobuz.spoofer.find_working_secret",
        AsyncMock(side_effect=RuntimeError("no valid signing secret")),
    )

    with pytest.raises(RuntimeError, match="no valid signing secret"):
        await _resolve_qobuz_credentials(
            {"qobuz_token": "token", "qobuz_app_id": "web-app"},
            client,
            strict=True,
        )
