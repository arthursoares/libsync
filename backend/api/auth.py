"""Auth API routes."""

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..services.client_activation import (
    ClientActivationShuttingDownError,
    ClientReloadBusyError,
    activate_config_updates,
)
from .lifecycle import client_operation, require_work_admission

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger("streamrip")
TIDAL_POLL_ERROR_MESSAGE = (
    "Could not check Tidal authorization. Retry or reconnect Tidal."
)
TIDAL_ACTIVATION_ERROR_MESSAGE = (
    "Tidal authorization succeeded, but credentials could not be activated. "
    "Retry or reconnect Tidal."
)


class _AuthFlowError(RuntimeError):
    def __init__(self, status_code: int, error: Exception):
        super().__init__(str(error))
        self.status_code = status_code


def _activation_http_error(error: Exception) -> HTTPException:
    if isinstance(error, ClientReloadBusyError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, ClientActivationShuttingDownError):
        return HTTPException(status_code=503, detail=str(error))
    if isinstance(error, _AuthFlowError):
        return HTTPException(status_code=error.status_code, detail=str(error))
    return HTTPException(status_code=502, detail=str(error))


def _qobuz_updates(creds: dict) -> dict:
    return {
        "qobuz_token": creds["user_auth_token"],
        "qobuz_user_id": str(creds["user_id"]),
        "qobuz_app_id": creds["app_id"],
    }


def _tidal_updates(data: dict, auth_method: str) -> dict:
    return {
        "tidal_access_token": data["access_token"],
        "tidal_refresh_token": data["refresh_token"],
        "tidal_user_id": str(data["user_id"]),
        "tidal_country_code": data["country_code"],
        "tidal_token_expiry": str(data["token_expiry"]),
        "tidal_auth_method": auth_method,
    }


@router.get("/status")
async def auth_status(request: Request):
    with client_operation(request, {"qobuz", "tidal"}):
        clients = getattr(request.app.state, "_clients_ref", {})
        db = request.app.state.db
        sources = []
        for source in ("qobuz", "tidal"):
            client = clients.get(source)
            # Both clients are now SDK clients with an open async session while
            # the FastAPI lifespan holds them — check _transport._session.
            authenticated = (
                client is not None
                and getattr(client, "_transport", None) is not None
                and client._transport._session is not None
            )
            token_key = (
                f"{source}_token" if source == "qobuz" else f"{source}_access_token"
            )
            sources.append(
                {
                    "source": source,
                    "authenticated": authenticated,
                    "user_id": db.get_config(f"{source}_user_id"),
                    "has_credentials": bool(db.get_config(token_key)),
                }
            )
        return sources


@router.get("/qobuz/oauth-url")
async def qobuz_oauth_url(origin: str = ""):
    """Get the Qobuz OAuth URL for browser login.

    The frontend passes ``window.location.origin`` so the redirect URL
    points back to wherever the user is actually browsing from — works
    in Docker, behind reverse proxies, and on non-default ports.
    """
    from qobuz.auth import APP_ID

    if not origin:
        # Fallback: assume the caller is on localhost:11111
        origin = "http://localhost:11111"

    redirect_url = f"{origin}/api/auth/qobuz/callback"
    params = urlencode({"ext_app_id": APP_ID, "redirect_url": redirect_url})
    return {"url": f"https://www.qobuz.com/signin/oauth?{params}"}


@router.get("/qobuz/callback")
async def qobuz_oauth_callback_redirect(request: Request, code_autorisation: str = ""):
    """Handle the OAuth redirect from Qobuz.

    Qobuz redirects here with ``?code_autorisation=…``.  We exchange the
    code, persist credentials, reload clients, then redirect the browser
    back to the Settings page with a success/error query param.
    """
    require_work_admission(request)

    from qobuz.auth import exchange_code

    if not code_autorisation:
        return RedirectResponse("/settings?oauth=error&reason=missing_code")

    async def prepare():
        try:
            creds = await exchange_code(code_autorisation)
        except Exception as error:
            raise _AuthFlowError(400, error) from error
        return _qobuz_updates(creds)

    try:
        await activate_config_updates(request.app, prepare, affected_sources={"qobuz"})
    except ClientReloadBusyError as error:
        raise _activation_http_error(error) from error
    except ClientActivationShuttingDownError as error:
        raise _activation_http_error(error) from error
    except _AuthFlowError:
        logger.exception("OAuth code exchange failed")
        return RedirectResponse("/settings?oauth=error&reason=exchange_failed")
    except Exception:
        logger.exception("OAuth credential activation failed")
        return RedirectResponse("/settings?oauth=error&reason=activation_failed")

    return RedirectResponse("/settings?oauth=success")


class OAuthCodeRequest(BaseModel):
    code: str


@router.post("/qobuz/oauth-callback")
async def qobuz_oauth_callback(request: Request, body: OAuthCodeRequest):
    """Exchange an OAuth code for credentials and save them."""
    require_work_admission(request)
    from qobuz.auth import exchange_code

    profile = {}

    async def prepare():
        try:
            creds = await exchange_code(body.code)
        except Exception as error:
            raise _AuthFlowError(400, error) from error
        profile.update(creds)
        return _qobuz_updates(creds)

    try:
        await activate_config_updates(request.app, prepare, affected_sources={"qobuz"})
    except Exception as error:
        if not isinstance(error, (ClientReloadBusyError, _AuthFlowError)):
            logger.exception("OAuth credential activation failed")
        raise _activation_http_error(error) from error

    return {
        "success": True,
        "user_id": profile["user_id"],
        "display_name": profile["display_name"],
    }


class OAuthRedirectRequest(BaseModel):
    redirect_url: str


@router.post("/qobuz/oauth-from-url")
async def qobuz_oauth_from_url(request: Request, body: OAuthRedirectRequest):
    """Extract code from a redirect URL, exchange for credentials, and save.

    For headless/remote machines where the browser callback can't reach localhost.
    """
    require_work_admission(request)

    from qobuz.auth import exchange_code, extract_code_from_url

    profile = {}

    async def prepare():
        try:
            code = extract_code_from_url(body.redirect_url)
            creds = await exchange_code(code)
        except Exception as error:
            raise _AuthFlowError(400, error) from error
        profile.update(creds)
        return _qobuz_updates(creds)

    try:
        await activate_config_updates(request.app, prepare, affected_sources={"qobuz"})
    except Exception as error:
        if not isinstance(error, (ClientReloadBusyError, _AuthFlowError)):
            logger.exception("OAuth URL credential activation failed")
        raise _activation_http_error(error) from error

    return {
        "success": True,
        "user_id": profile["user_id"],
        "display_name": profile["display_name"],
    }


# ── Tidal ──────────────────────────────────────────────────────────────────


@router.post("/tidal/device-code")
async def tidal_device_code():
    """Start the Tidal device-code OAuth flow.

    Returns the verification URL and user code the browser must open,
    plus the device_code the caller must pass to the poll endpoint.
    """
    from tidal.auth import request_device_code

    try:
        data = await request_device_code()
    except Exception as e:
        logger.exception("Failed to start Tidal device-code flow")
        raise HTTPException(status_code=502, detail=str(e))

    verification_url = (
        data.get("verificationUriComplete") or data.get("verificationUri") or ""
    )
    if verification_url and not verification_url.startswith("http"):
        verification_url = f"https://{verification_url}"

    return {
        "device_code": data["deviceCode"],
        "user_code": data["userCode"],
        "verification_url": verification_url,
        "expires_in": data.get("expiresIn", 300),
        "interval": data.get("interval", 5),
    }


class TidalPollRequest(BaseModel):
    device_code: str


@router.post("/tidal/poll")
async def tidal_poll(request: Request, body: TidalPollRequest):
    """Poll the Tidal token endpoint.

    Returns ``{"status": "pending"}`` while the user hasn't approved yet,
    ``{"status": "authorized", "user_id": ...}`` on success, or
    ``{"status": "error", "error": "..."}`` on failure.
    """
    require_work_admission(request)

    from tidal.auth import poll_device_code

    poll_result = {}

    async def prepare():
        try:
            status, data = await poll_device_code(body.device_code)
        except Exception as error:
            raise _AuthFlowError(200, error) from error
        poll_result.update({"status": status, "data": data})
        if status != 0:
            return None
        return _tidal_updates(data, "device_code")

    try:
        activated = await activate_config_updates(
            request.app, prepare, affected_sources={"tidal"}
        )
    except _AuthFlowError:
        logger.exception("Tidal poll request failed")
        return {"status": "error", "error": TIDAL_POLL_ERROR_MESSAGE}
    except (ClientReloadBusyError, ClientActivationShuttingDownError) as error:
        raise _activation_http_error(error) from error
    except Exception:
        logger.exception("Tidal credential activation failed")
        return {"status": "error", "error": TIDAL_ACTIVATION_ERROR_MESSAGE}

    status = poll_result["status"]
    data = poll_result["data"]
    if activated is None:
        if status == 2:
            return {"status": "pending"}
        return {"status": "error", "error": data.get("error_description") or str(data)}
    return {"status": "authorized", "user_id": data["user_id"]}


# -- Tidal PKCE (HiRes-capable) -------------------------------------------

# In-memory verifier store, keyed by an opaque handle returned to the
# client. The verifier is sensitive (it's the proof-of-possession for the
# auth code) and short-lived (login completes in minutes). A dict on
# app state is simpler than DB persistence and survives just long enough.
_pkce_pending: dict[str, dict[str, str]] = {}


@router.post("/tidal/pkce-start")
async def tidal_pkce_start():
    """Begin a PKCE OAuth flow. Returns the URL to open + a handle the
    caller must echo back when posting the redirect URL."""
    import secrets as _secrets

    from tidal.auth import build_pkce_authorize_url, generate_pkce_pair

    verifier, challenge, unique_key = generate_pkce_pair()
    handle = _secrets.token_urlsafe(16)
    _pkce_pending[handle] = {"verifier": verifier, "unique_key": unique_key}

    return {
        "handle": handle,
        "auth_url": build_pkce_authorize_url(challenge, unique_key),
        "redirect_uri_prefix": "https://tidal.com/android/login/auth",
    }


class TidalPkceCompleteRequest(BaseModel):
    handle: str
    redirect_url: str


@router.post("/tidal/pkce-complete")
async def tidal_pkce_complete(request: Request, body: TidalPkceCompleteRequest):
    """Finish PKCE: exchange the auth code from the user's pasted URL for
    access + refresh tokens, persist them, and hot-reload the client."""
    require_work_admission(request)

    from tidal.auth import exchange_pkce_code, extract_code_from_redirect

    result = {}

    async def prepare():
        pending = _pkce_pending.pop(body.handle, None)
        if pending is None:
            raise _AuthFlowError(
                400,
                ValueError("Unknown or expired PKCE handle. Start the flow again."),
            )
        try:
            code = extract_code_from_redirect(body.redirect_url)
        except ValueError as error:
            raise _AuthFlowError(400, error) from error
        try:
            data = await exchange_pkce_code(
                code, pending["verifier"], pending["unique_key"]
            )
        except Exception as error:
            raise _AuthFlowError(502, error) from error
        result.update(data)
        return _tidal_updates(data, "pkce")

    try:
        await activate_config_updates(request.app, prepare, affected_sources={"tidal"})
    except Exception as error:
        if not isinstance(error, (ClientReloadBusyError, _AuthFlowError)):
            logger.exception("Tidal PKCE credential activation failed")
        raise _activation_http_error(error) from error

    return {"status": "authorized", "user_id": result["user_id"]}
