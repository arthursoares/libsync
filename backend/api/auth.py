"""Auth API routes."""

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger("streamrip")


@router.get("/status")
async def auth_status(request: Request):
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
        token_key = f"{source}_token" if source == "qobuz" else f"{source}_access_token"
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
    from qobuz.auth import exchange_code

    if not code_autorisation:
        return RedirectResponse("/settings?oauth=error&reason=missing_code")

    try:
        creds = await exchange_code(code_autorisation)
    except Exception:
        logger.exception("OAuth code exchange failed")
        return RedirectResponse("/settings?oauth=error&reason=exchange_failed")

    db = request.app.state.db
    db.set_config("qobuz_token", creds["user_auth_token"])
    db.set_config("qobuz_user_id", str(creds["user_id"]))
    db.set_config("qobuz_app_id", creds["app_id"])

    from .config import _reload_clients

    await _reload_clients(request)

    return RedirectResponse("/settings?oauth=success")


class OAuthCodeRequest(BaseModel):
    code: str


@router.post("/qobuz/oauth-callback")
async def qobuz_oauth_callback(request: Request, body: OAuthCodeRequest):
    """Exchange an OAuth code for credentials and save them."""
    from qobuz.auth import exchange_code

    try:
        creds = await exchange_code(body.code)
    except Exception as e:
        logger.exception("OAuth code exchange failed")
        raise HTTPException(status_code=400, detail=str(e))

    db = request.app.state.db
    db.set_config("qobuz_token", creds["user_auth_token"])
    db.set_config("qobuz_user_id", str(creds["user_id"]))
    # The OAuth flow issues tokens bound to the OAuth app ("304027809").
    # All subsequent requests MUST send X-App-Id = that app, otherwise
    # Qobuz rejects the token with 401 on every endpoint that validates
    # token-app binding (catalog, downloads, sync).  Persist it explicitly.
    db.set_config("qobuz_app_id", creds["app_id"])

    from .config import _reload_clients

    await _reload_clients(request)

    return {
        "success": True,
        "user_id": creds["user_id"],
        "display_name": creds["display_name"],
    }


class OAuthRedirectRequest(BaseModel):
    redirect_url: str


@router.post("/qobuz/oauth-from-url")
async def qobuz_oauth_from_url(request: Request, body: OAuthRedirectRequest):
    """Extract code from a redirect URL, exchange for credentials, and save.

    For headless/remote machines where the browser callback can't reach localhost.
    """
    from qobuz.auth import exchange_code, extract_code_from_url

    try:
        code = extract_code_from_url(body.redirect_url)
        creds = await exchange_code(code)
    except Exception as e:
        logger.exception("OAuth URL exchange failed")
        raise HTTPException(status_code=400, detail=str(e))

    db = request.app.state.db
    db.set_config("qobuz_token", creds["user_auth_token"])
    db.set_config("qobuz_user_id", str(creds["user_id"]))
    # See oauth-callback above — the OAuth token is bound to creds["app_id"]
    db.set_config("qobuz_app_id", creds["app_id"])

    from .config import _reload_clients

    await _reload_clients(request)

    return {
        "success": True,
        "user_id": creds["user_id"],
        "display_name": creds["display_name"],
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
    from tidal.auth import poll_device_code

    try:
        status, data = await poll_device_code(body.device_code)
    except Exception as e:
        logger.exception("Tidal poll request failed")
        return {"status": "error", "error": str(e)}

    if status == 2:
        return {"status": "pending"}
    if status != 0:
        return {"status": "error", "error": data.get("error_description") or str(data)}

    # Authorized — persist credentials and hot-reload the client
    db = request.app.state.db
    db.set_config("tidal_access_token", data["access_token"])
    db.set_config("tidal_refresh_token", data["refresh_token"])
    db.set_config("tidal_user_id", str(data["user_id"]))
    db.set_config("tidal_country_code", data["country_code"])
    db.set_config("tidal_token_expiry", str(data["token_expiry"]))

    from .config import _reload_clients

    await _reload_clients(request)

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
    from tidal.auth import exchange_pkce_code, extract_code_from_redirect

    pending = _pkce_pending.pop(body.handle, None)
    if pending is None:
        raise HTTPException(
            status_code=400,
            detail="Unknown or expired PKCE handle. Start the flow again.",
        )

    try:
        code = extract_code_from_redirect(body.redirect_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        data = await exchange_pkce_code(
            code, pending["verifier"], pending["unique_key"]
        )
    except Exception as e:
        logger.exception("Tidal PKCE token exchange failed")
        raise HTTPException(status_code=502, detail=str(e))

    db = request.app.state.db
    db.set_config("tidal_access_token", data["access_token"])
    db.set_config("tidal_refresh_token", data["refresh_token"])
    db.set_config("tidal_user_id", str(data["user_id"]))
    db.set_config("tidal_country_code", data["country_code"])
    db.set_config("tidal_token_expiry", str(data["token_expiry"]))
    db.set_config("tidal_auth_method", "pkce")

    from .config import _reload_clients

    await _reload_clients(request)

    return {"status": "authorized", "user_id": data["user_id"]}
