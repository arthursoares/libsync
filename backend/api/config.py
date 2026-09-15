"""Config API routes."""

import logging

from fastapi import APIRouter, HTTPException, Request

from ..models.schemas import AppConfig, ConfigUpdate
from ..services.client_activation import (
    ClientActivationShuttingDownError,
    ClientReloadBusyError,
    activate_config_updates,
    affected_sources_for_updates,
)
from .lifecycle import require_work_admission

router = APIRouter(prefix="/api/config", tags=["config"])
logger = logging.getLogger("streamrip")


@router.get("")
async def get_config(request: Request) -> AppConfig:
    db = request.app.state.db
    raw = db.get_all_config()
    config_dict = {}
    for key, value in raw.items():
        if key in ("qobuz_quality", "tidal_quality", "max_connections"):
            config_dict[key] = int(value)
        elif key in (
            "auto_sync_enabled",
            "qobuz_download_booklets",
            "source_subdirectories",
            "disc_subdirectories",
            "embed_artwork",
            "scan_sentinel_write_enabled",
        ):
            config_dict[key] = value.lower() in ("true", "1", "yes")
        else:
            config_dict[key] = value
    return AppConfig(**config_dict)


@router.patch("")
async def update_config(request: Request, body: ConfigUpdate):
    require_work_admission(request)
    requested = body.model_dump(exclude_none=True)
    affected = affected_sources_for_updates(requested)

    async def prepare_updates():
        updates = dict(requested)
        # This decision is made under the writer reservation so two token
        # updates cannot derive an app ID from stale config.
        if "qobuz_token" in updates and not updates.get("qobuz_app_id"):
            new_token = updates["qobuz_token"] or ""
            old_token = request.app.state.db.get_config("qobuz_token") or ""
            if new_token and new_token != old_token:
                updates["qobuz_app_id"] = "798273057"
                logger.info(
                    "Detected manual qobuz_token change — pinning "
                    "qobuz_app_id=798273057"
                )
        return updates

    try:
        await activate_config_updates(
            request.app, prepare_updates, affected_sources=affected
        )
    except ClientReloadBusyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ClientActivationShuttingDownError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        logger.exception("Config activation failed")
        raise HTTPException(status_code=400, detail=str(error)) from error

    return await get_config(request)


@router.post("/reset")
async def reset_database(request: Request):
    """Reset library data and download history. Config and credentials are preserved."""
    require_work_admission(request)
    db = request.app.state.db
    with db._connect() as conn:
        conn.execute("DELETE FROM albums")
        conn.execute("DELETE FROM tracks")
        conn.execute("DELETE FROM sync_runs")
    logger.info("Database reset — library data cleared, config preserved")
    return {
        "message": "Library, tracks, and sync history cleared. Config and credentials preserved. Files on disk unchanged."
    }
