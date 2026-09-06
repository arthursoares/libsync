"""Tests for the /api/auth routes: Qobuz OAuth (redirect + JSON), Tidal
device-code, and Tidal PKCE.

Every route that persists credentials uses the common safe activation path.
These tests replace only SDK construction and signing resolution, so auth
exchange, atomic persistence, validation, and publication still execute.

The Qobuz/Tidal SDK functions each route calls are imported *inside* the
route bodies (``from qobuz.auth import exchange_code`` etc.), so they must
be patched on the module that owns them (``qobuz.auth``, ``tidal.auth``)
rather than on backend.api.auth -- the local import resolves the attribute
off that module at call time.
"""

from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient
from qobuz.auth import APP_ID as QOBUZ_OAUTH_APP_ID

from backend.api.auth import _pkce_pending
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
def no_real_client_activation(monkeypatch):
    """Use validating fake clients while exercising real activation logic."""

    def build_client(source, config, *, strict=False):
        token_key = "qobuz_token" if source == "qobuz" else "tidal_access_token"
        token = config.get(token_key) if isinstance(config, dict) else None
        if not token:
            return None
        candidate = Mock()
        candidate.__aenter__ = AsyncMock(return_value=candidate)
        candidate.__aexit__ = AsyncMock(return_value=None)
        candidate.favorites = Mock()
        candidate.favorites.get_albums = AsyncMock(return_value=Mock(items=[]))
        return candidate

    monkeypatch.setattr("backend.main._init_client", build_client)
    monkeypatch.setattr(
        "backend.main._resolve_qobuz_credentials", AsyncMock(return_value={})
    )


@pytest.fixture(autouse=True)
def clean_pkce_pending():
    """`_pkce_pending` (backend/api/auth.py) is a module-level dict shared
    across every app instance in this process, same hazard as the
    WebSocket ConnectionManager singleton. Clear it around each test so a
    handle left over from a failed/incomplete test can't linger."""
    _pkce_pending.clear()
    yield
    _pkce_pending.clear()


class TestAuthStatus:
    async def test_status_shape_with_no_credentials(self, client):
        resp = await client.get("/api/auth/status")
        assert resp.status_code == 200
        assert resp.json() == [
            {
                "source": "qobuz",
                "authenticated": False,
                "user_id": None,
                "has_credentials": False,
            },
            {
                "source": "tidal",
                "authenticated": False,
                "user_id": None,
                "has_credentials": False,
            },
        ]

    async def test_status_reflects_stored_credentials(self, client, app):
        # has_credentials/user_id come straight from the DB; authenticated
        # requires a live client session (app.state._clients_ref), which a
        # bare db.set_config() does not create.
        app.state.db.set_config("qobuz_token", "tok-1")
        app.state.db.set_config("qobuz_user_id", "42")
        app.state.db.set_config("tidal_access_token", "tok-2")
        app.state.db.set_config("tidal_user_id", "7")

        resp = await client.get("/api/auth/status")
        qobuz, tidal = resp.json()

        assert qobuz["has_credentials"] is True
        assert qobuz["user_id"] == "42"
        assert qobuz["authenticated"] is False
        assert tidal["has_credentials"] is True
        assert tidal["user_id"] == "7"
        assert tidal["authenticated"] is False


class TestQobuzOAuthUrl:
    async def test_oauth_url_defaults_origin_to_localhost(self, client):
        resp = await client.get("/api/auth/qobuz/oauth-url")
        assert resp.status_code == 200
        qs = parse_qs(urlparse(resp.json()["url"]).query)
        assert qs["redirect_url"][0] == "http://localhost:11111/api/auth/qobuz/callback"
        assert qs["ext_app_id"][0] == QOBUZ_OAUTH_APP_ID

    async def test_oauth_url_uses_given_origin(self, client):
        resp = await client.get(
            "/api/auth/qobuz/oauth-url",
            params={"origin": "https://myhost.example:9999"},
        )
        assert resp.status_code == 200
        qs = parse_qs(urlparse(resp.json()["url"]).query)
        assert (
            qs["redirect_url"][0]
            == "https://myhost.example:9999/api/auth/qobuz/callback"
        )


class TestQobuzCallbackRedirect:
    async def test_missing_code_redirects_with_error(self, client, app):
        resp = await client.get("/api/auth/qobuz/callback", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/settings?oauth=error&reason=missing_code"
        assert app.state.db.get_config("qobuz_token") is None

    async def test_exchange_failure_redirects_with_error(
        self, client, app, monkeypatch
    ):
        monkeypatch.setattr(
            "qobuz.auth.exchange_code",
            AsyncMock(side_effect=RuntimeError("bad code")),
        )
        resp = await client.get(
            "/api/auth/qobuz/callback",
            params={"code_autorisation": "abc123"},
            follow_redirects=False,
        )
        assert resp.status_code == 307
        assert (
            resp.headers["location"] == "/settings?oauth=error&reason=exchange_failed"
        )
        assert app.state.db.get_config("qobuz_token") is None

    async def test_success_redirects_and_persists_credentials(
        self, client, app, monkeypatch
    ):
        monkeypatch.setattr(
            "qobuz.auth.exchange_code",
            AsyncMock(
                return_value={
                    "user_auth_token": "tok-abc",
                    "user_id": 555,
                    "app_id": "304027809",
                    "display_name": "Someone",
                }
            ),
        )
        resp = await client.get(
            "/api/auth/qobuz/callback",
            params={"code_autorisation": "abc123"},
            follow_redirects=False,
        )
        assert resp.status_code == 307
        assert resp.headers["location"] == "/settings?oauth=success"
        assert app.state.db.get_config("qobuz_token") == "tok-abc"
        assert app.state.db.get_config("qobuz_user_id") == "555"
        assert app.state.db.get_config("qobuz_app_id") == "304027809"


class TestQobuzOAuthCallbackJSON:
    async def test_success_persists_credentials_and_returns_profile(
        self, client, app, monkeypatch
    ):
        monkeypatch.setattr(
            "qobuz.auth.exchange_code",
            AsyncMock(
                return_value={
                    "user_auth_token": "tok-xyz",
                    "user_id": 111,
                    "app_id": "304027809",
                    "display_name": "Test User",
                }
            ),
        )
        resp = await client.post("/api/auth/qobuz/oauth-callback", json={"code": "abc"})
        assert resp.status_code == 200
        assert resp.json() == {
            "success": True,
            "user_id": 111,
            "display_name": "Test User",
        }
        assert app.state.db.get_config("qobuz_token") == "tok-xyz"
        assert app.state.db.get_config("qobuz_user_id") == "111"
        assert app.state.db.get_config("qobuz_app_id") == "304027809"

    async def test_exchange_failure_returns_400_and_does_not_persist(
        self, client, app, monkeypatch
    ):
        monkeypatch.setattr(
            "qobuz.auth.exchange_code",
            AsyncMock(side_effect=RuntimeError("nope")),
        )
        resp = await client.post("/api/auth/qobuz/oauth-callback", json={"code": "abc"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "nope"
        assert app.state.db.get_config("qobuz_token") is None


class TestQobuzOAuthFromUrl:
    async def test_success_persists_credentials(self, client, app, monkeypatch):
        monkeypatch.setattr(
            "qobuz.auth.extract_code_from_url", lambda url: "extracted-code"
        )
        monkeypatch.setattr(
            "qobuz.auth.exchange_code",
            AsyncMock(
                return_value={
                    "user_auth_token": "tok-url",
                    "user_id": 222,
                    "app_id": "304027809",
                    "display_name": "URL User",
                }
            ),
        )
        resp = await client.post(
            "/api/auth/qobuz/oauth-from-url",
            json={"redirect_url": "https://example.com/callback?code=abc"},
        )
        assert resp.status_code == 200
        assert resp.json()["user_id"] == 222
        assert app.state.db.get_config("qobuz_token") == "tok-url"
        assert app.state.db.get_config("qobuz_app_id") == "304027809"

    async def test_extract_failure_returns_400_and_does_not_persist(
        self, client, app, monkeypatch
    ):
        def _raise(url):
            raise ValueError("no code in url")

        monkeypatch.setattr("qobuz.auth.extract_code_from_url", _raise)
        resp = await client.post(
            "/api/auth/qobuz/oauth-from-url",
            json={"redirect_url": "https://example.com/callback"},
        )
        assert resp.status_code == 400
        assert app.state.db.get_config("qobuz_token") is None

    async def test_exchange_failure_returns_400_and_does_not_persist(
        self, client, app, monkeypatch
    ):
        monkeypatch.setattr("qobuz.auth.extract_code_from_url", lambda url: "code")
        monkeypatch.setattr(
            "qobuz.auth.exchange_code",
            AsyncMock(side_effect=RuntimeError("bad code")),
        )
        resp = await client.post(
            "/api/auth/qobuz/oauth-from-url",
            json={"redirect_url": "https://example.com/callback?code=bad"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "bad code"
        assert app.state.db.get_config("qobuz_token") is None


class TestTidalDeviceCode:
    async def test_success_prefixes_bare_verification_url_with_https(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(
            "tidal.auth.request_device_code",
            AsyncMock(
                return_value={
                    "deviceCode": "dev-1",
                    "userCode": "USER-1",
                    "verificationUri": "tidal.com/link",
                    "expiresIn": 300,
                    "interval": 5,
                }
            ),
        )
        resp = await client.post("/api/auth/tidal/device-code")
        assert resp.status_code == 200
        data = resp.json()
        assert data["device_code"] == "dev-1"
        assert data["user_code"] == "USER-1"
        assert data["verification_url"] == "https://tidal.com/link"
        assert data["expires_in"] == 300
        assert data["interval"] == 5

    async def test_already_prefixed_verification_url_is_untouched(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(
            "tidal.auth.request_device_code",
            AsyncMock(
                return_value={
                    "deviceCode": "dev-2",
                    "userCode": "USER-2",
                    "verificationUriComplete": "https://tidal.com/link?u=USER-2",
                }
            ),
        )
        resp = await client.post("/api/auth/tidal/device-code")
        assert resp.status_code == 200
        assert resp.json()["verification_url"] == "https://tidal.com/link?u=USER-2"

    async def test_failure_returns_502(self, client, monkeypatch):
        monkeypatch.setattr(
            "tidal.auth.request_device_code",
            AsyncMock(side_effect=RuntimeError("tidal is down")),
        )
        resp = await client.post("/api/auth/tidal/device-code")
        assert resp.status_code == 502
        assert resp.json()["detail"] == "tidal is down"


class TestTidalPoll:
    async def test_pending(self, client, monkeypatch):
        monkeypatch.setattr(
            "tidal.auth.poll_device_code", AsyncMock(return_value=(2, {}))
        )
        resp = await client.post("/api/auth/tidal/poll", json={"device_code": "dev-1"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "pending"}

    async def test_error_status_returns_error_body(self, client, monkeypatch):
        monkeypatch.setattr(
            "tidal.auth.poll_device_code",
            AsyncMock(return_value=(1, {"error_description": "access_denied"})),
        )
        resp = await client.post("/api/auth/tidal/poll", json={"device_code": "dev-1"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "error", "error": "access_denied"}

    async def test_poll_raising_returns_error_body(self, client, monkeypatch):
        marker = "poll-internal-marker /private/tidal-response.json"
        monkeypatch.setattr(
            "tidal.auth.poll_device_code",
            AsyncMock(side_effect=RuntimeError(marker)),
        )
        resp = await client.post("/api/auth/tidal/poll", json={"device_code": "dev-1"})
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "error",
            "error": "Could not check Tidal authorization. Retry or reconnect Tidal.",
        }
        assert marker not in resp.text

    async def test_activation_failure_is_sanitized_and_preserves_old_client_and_db(
        self, client, app, monkeypatch
    ):
        marker = "activation-internal-marker /private/tidal-token.json"
        old_client = Mock()
        app.state._clients_ref["tidal"] = old_client
        app.state.db.set_config("tidal_access_token", "old-access-token")
        old_config = app.state.db.get_all_config()

        candidate = Mock()
        candidate.__aenter__ = AsyncMock(return_value=candidate)
        candidate.__aexit__ = AsyncMock(return_value=None)
        candidate.favorites = Mock()
        candidate.favorites.get_albums = AsyncMock(
            side_effect=RuntimeError(marker)
        )

        def build_client(source, config, *, strict=False):
            assert source == "tidal"
            assert strict is True
            return candidate

        monkeypatch.setattr("backend.main._init_client", build_client)
        monkeypatch.setattr(
            "tidal.auth.poll_device_code",
            AsyncMock(
                return_value=(
                    0,
                    {
                        "access_token": "new-access-token",
                        "refresh_token": "new-refresh-token",
                        "user_id": 999,
                        "country_code": "US",
                        "token_expiry": 123456.0,
                    },
                )
            ),
        )

        response = await client.post(
            "/api/auth/tidal/poll", json={"device_code": "dev-1"}
        )

        assert response.status_code == 200
        assert response.json() == {
            "status": "error",
            "error": (
                "Tidal authorization succeeded, but credentials could not be "
                "activated. Retry or reconnect Tidal."
            ),
        }
        assert marker not in response.text
        assert app.state.db.get_all_config() == old_config
        assert app.state._clients_ref["tidal"] is old_client
        candidate.__aexit__.assert_awaited_once_with(None, None, None)

    async def test_authorized_replaces_pkce_credentials_and_auth_method(
        self, client, app, monkeypatch
    ):
        app.state.db.set_config("tidal_auth_method", "pkce")
        monkeypatch.setattr(
            "tidal.auth.poll_device_code",
            AsyncMock(
                return_value=(
                    0,
                    {
                        "access_token": "at-1",
                        "refresh_token": "rt-1",
                        "user_id": 999,
                        "country_code": "US",
                        "token_expiry": 123456.0,
                    },
                )
            ),
        )
        resp = await client.post("/api/auth/tidal/poll", json={"device_code": "dev-1"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "authorized", "user_id": 999}
        db = app.state.db
        assert db.get_config("tidal_access_token") == "at-1"
        assert db.get_config("tidal_refresh_token") == "rt-1"
        assert db.get_config("tidal_user_id") == "999"
        assert db.get_config("tidal_country_code") == "US"
        assert db.get_config("tidal_token_expiry") == "123456.0"
        assert db.get_config("tidal_auth_method") == "device_code"

        assert "tidal" in app.state._clients_ref


class TestTidalPkce:
    async def _start(
        self, client, monkeypatch, verifier="verifier-1", unique_key="unique-1"
    ):
        # generate_pkce_pair() and build_pkce_authorize_url() are called
        # WITHOUT await in the route (backend/api/auth.py:tidal_pkce_start) --
        # plain sync stand-ins, not AsyncMock.
        monkeypatch.setattr(
            "tidal.auth.generate_pkce_pair",
            lambda: (verifier, "challenge-1", unique_key),
        )
        monkeypatch.setattr(
            "tidal.auth.build_pkce_authorize_url",
            lambda challenge, unique_key: f"https://tidal.com/authorize?c={challenge}",
        )
        resp = await client.post("/api/auth/tidal/pkce-start")
        assert resp.status_code == 200
        return resp.json()

    async def test_pkce_start_returns_handle_and_auth_url(self, client, monkeypatch):
        data = await self._start(client, monkeypatch)
        assert data["handle"]
        assert data["auth_url"] == "https://tidal.com/authorize?c=challenge-1"
        assert data["redirect_uri_prefix"] == "https://tidal.com/android/login/auth"

    async def test_pkce_complete_unknown_handle_returns_400(self, client):
        resp = await client.post(
            "/api/auth/tidal/pkce-complete",
            json={"handle": "does-not-exist", "redirect_url": "https://x/y?code=1"},
        )
        assert resp.status_code == 400
        assert "Unknown or expired PKCE handle" in resp.json()["detail"]

    async def test_busy_pkce_completion_does_not_consume_handle(
        self, client, app, monkeypatch
    ):
        data = await self._start(client, monkeypatch)
        handle = data["handle"]
        exchange = AsyncMock()
        monkeypatch.setattr("tidal.auth.exchange_pkce_code", exchange)

        with app.state.client_operations.operation({"tidal"}):
            resp = await client.post(
                "/api/auth/tidal/pkce-complete",
                json={
                    "handle": handle,
                    "redirect_url": "https://tidal.com/android/login/auth?code=1",
                },
            )

        assert resp.status_code == 409
        assert handle in _pkce_pending
        exchange.assert_not_awaited()

    async def test_pkce_complete_success_persists_tokens_and_auth_method(
        self, client, app, monkeypatch
    ):
        data = await self._start(client, monkeypatch)
        handle = data["handle"]

        # extract_code_from_redirect() is also called WITHOUT await in the
        # route -- only exchange_pkce_code() is async.
        monkeypatch.setattr(
            "tidal.auth.extract_code_from_redirect", lambda url: "authcode-1"
        )
        monkeypatch.setattr(
            "tidal.auth.exchange_pkce_code",
            AsyncMock(
                return_value={
                    "access_token": "at-pkce",
                    "refresh_token": "rt-pkce",
                    "user_id": 321,
                    "country_code": "DE",
                    "token_expiry": 999.5,
                }
            ),
        )
        resp = await client.post(
            "/api/auth/tidal/pkce-complete",
            json={
                "handle": handle,
                "redirect_url": "https://tidal.com/android/login/auth?code=1",
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "authorized", "user_id": 321}
        db = app.state.db
        assert db.get_config("tidal_access_token") == "at-pkce"
        assert db.get_config("tidal_refresh_token") == "rt-pkce"
        assert db.get_config("tidal_user_id") == "321"
        assert db.get_config("tidal_country_code") == "DE"
        assert db.get_config("tidal_token_expiry") == "999.5"
        assert db.get_config("tidal_auth_method") == "pkce"

    async def test_pkce_complete_handle_can_only_be_used_once(
        self, client, monkeypatch
    ):
        data = await self._start(client, monkeypatch)
        handle = data["handle"]

        monkeypatch.setattr(
            "tidal.auth.extract_code_from_redirect", lambda url: "authcode-1"
        )
        monkeypatch.setattr(
            "tidal.auth.exchange_pkce_code",
            AsyncMock(
                return_value={
                    "access_token": "at",
                    "refresh_token": "rt",
                    "user_id": 1,
                    "country_code": "US",
                    "token_expiry": 1.0,
                }
            ),
        )
        body = {
            "handle": handle,
            "redirect_url": "https://tidal.com/android/login/auth?code=1",
        }
        first = await client.post("/api/auth/tidal/pkce-complete", json=body)
        assert first.status_code == 200

        second = await client.post("/api/auth/tidal/pkce-complete", json=body)
        assert second.status_code == 400
        assert "Unknown or expired PKCE handle" in second.json()["detail"]

    async def test_pkce_complete_extract_failure_returns_400(self, client, monkeypatch):
        data = await self._start(client, monkeypatch)
        handle = data["handle"]

        def _raise(url):
            raise ValueError("no code in redirect")

        monkeypatch.setattr("tidal.auth.extract_code_from_redirect", _raise)
        resp = await client.post(
            "/api/auth/tidal/pkce-complete",
            json={
                "handle": handle,
                "redirect_url": "https://tidal.com/android/login/auth?bad=1",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "no code in redirect"

    async def test_pkce_complete_exchange_failure_returns_502(
        self, client, monkeypatch
    ):
        data = await self._start(client, monkeypatch)
        handle = data["handle"]

        monkeypatch.setattr(
            "tidal.auth.extract_code_from_redirect", lambda url: "authcode-1"
        )
        monkeypatch.setattr(
            "tidal.auth.exchange_pkce_code",
            AsyncMock(side_effect=RuntimeError("tidal token endpoint down")),
        )
        resp = await client.post(
            "/api/auth/tidal/pkce-complete",
            json={
                "handle": handle,
                "redirect_url": "https://tidal.com/android/login/auth?code=1",
            },
        )
        assert resp.status_code == 502
        assert resp.json()["detail"] == "tidal token endpoint down"
