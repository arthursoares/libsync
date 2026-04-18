"""Library API routes."""
import asyncio
import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("streamrip")

from ..models.schemas import MarkDownloadedRequest
from ..services import scan as scan_service
from ..services.scan import mark_album_downloaded, unmark_album_downloaded

router = APIRouter(prefix="/api/library", tags=["library"])


def _dedup_db_dir() -> str:
    db_path = os.environ.get("STREAMRIP_DB_PATH", "data/streamrip.db")
    return os.path.dirname(db_path) or "data"

@router.get("/{source}/albums")
async def get_albums(request: Request, source: str, page: int = 1, page_size: int = 50,
                     sort_by: str = "added_to_library_at", sort_dir: str = "DESC",
                     status: str | None = None, search: str | None = None):
    service = request.app.state.library_service
    return await service.get_albums(source, page=page, page_size=page_size,
                                     sort_by=sort_by, sort_dir=sort_dir, status=status, search=search)

@router.get("/{source}/albums/{album_id}")
async def get_album_detail(request: Request, source: str, album_id: int):
    service = request.app.state.library_service
    result = await service.get_album_detail(album_id)
    if result is None:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Album not found"}, status_code=404)
    return result

@router.post("/refresh/{source}")
async def refresh_library(request: Request, source: str):
    service = request.app.state.library_service
    return await service.refresh_library(source)

@router.post("/albums/{album_id}/mark-downloaded")
async def mark_downloaded(
    request: Request, album_id: int, body: MarkDownloadedRequest
):
    db = request.app.state.db
    if db.get_album(album_id) is None:
        return JSONResponse({"error": "Album not found"}, status_code=404)

    sentinel_enabled = (
        (db.get_config("scan_sentinel_write_enabled") or "True") == "True"
    )

    resolved_path = body.local_folder_path
    if body.local_folder_path is not None:
        downloads_root_cfg = db.get_config("downloads_path") or "/music"
        try:
            resolved = Path(body.local_folder_path).resolve(strict=False)
            root = Path(downloads_root_cfg).resolve(strict=False)
        except (OSError, ValueError):
            return JSONResponse(
                {"error": "Invalid local_folder_path"}, status_code=400
            )
        try:
            resolved.relative_to(root)
        except ValueError:
            return JSONResponse(
                {"error": "local_folder_path must be inside the configured downloads path"},
                status_code=400,
            )
        resolved_path = str(resolved)

    mark_album_downloaded(
        db, album_id,
        local_folder_path=resolved_path,
        dedup_db_dir=_dedup_db_dir(),
        sentinel_write_enabled=sentinel_enabled,
    )
    await request.app.state.event_bus.publish(
        "album_status_changed",
        {"album_id": album_id, "status": "complete"},
    )
    return db.get_album(album_id)


@router.post("/albums/{album_id}/unmark-downloaded")
async def unmark_downloaded(request: Request, album_id: int):
    db = request.app.state.db
    if db.get_album(album_id) is None:
        return JSONResponse({"error": "Album not found"}, status_code=404)

    unmark_album_downloaded(
        db, album_id, dedup_db_dir=_dedup_db_dir(),
    )
    await request.app.state.event_bus.publish(
        "album_status_changed",
        {"album_id": album_id, "status": "not_downloaded"},
    )
    return db.get_album(album_id)


@router.post("/scan-fuzzy")
async def start_scan(request: Request):
    app = request.app
    if app.state.active_scan_job is not None:
        return JSONResponse(
            {"error": "Another scan is already running"}, status_code=409
        )

    db = app.state.db
    download_path = db.get_config("downloads_path") or "/music"
    sentinel_enabled = (
        (db.get_config("scan_sentinel_write_enabled") or "True") == "True"
    )

    job_id = uuid.uuid4().hex
    app.state.scan_jobs[job_id] = {"status": "running", "result": None}
    app.state.active_scan_job = job_id

    async def runner():
        try:
            result = await scan_service.run_scan(
                db,
                download_path=download_path,
                dedup_db_dir=_dedup_db_dir(),
                event_bus=app.state.event_bus,
                sentinel_write_enabled=sentinel_enabled,
            )
            app.state.scan_jobs[job_id] = {"status": "complete", "result": result}
        except Exception:
            logger.exception("scan-fuzzy job %s failed", job_id)
            app.state.scan_jobs[job_id] = {
                "status": "error",
                "result": {"error": "Scan failed — see server logs"},
            }
        finally:
            app.state.active_scan_job = None

    asyncio.create_task(runner())
    return {"job_id": job_id}


@router.get("/scan-fuzzy/{job_id}")
async def scan_status(request: Request, job_id: str):
    job = request.app.state.scan_jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if job["status"] == "running":
        return {"status": "running"}
    if job["status"] == "error":
        return {"status": "error", **(job["result"] or {})}
    return {"status": "complete", **(job["result"] or {})}


@router.get("/search/{source}")
async def search(
    request: Request,
    source: str,
    q: str,
    page: int = 1,
    page_size: int = 60,
):
    """Search the streaming service's catalog (paginated).

    Mirrors the shape of ``GET /api/library/{source}/albums`` so the
    frontend can reuse its Load More / table-view machinery.  Returns
    ``{albums, total, limit, offset}``.
    """
    service = request.app.state.library_service
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    offset = (page - 1) * page_size
    return await service.search(source, q, limit=page_size, offset=offset)


@router.get("/{source}/playlists")
async def list_playlists(request: Request, source: str):
    """List the user's playlists from the streaming service.

    Currently only Qobuz is implemented.  Tidal returns an empty list
    until the SDK gains playlist read methods.
    """
    service = request.app.state.library_service
    return await service.list_playlists(source)


@router.get("/{source}/playlists/{playlist_id}")
async def get_playlist(request: Request, source: str, playlist_id: int):
    """Fetch a playlist with its track list."""
    service = request.app.state.library_service
    result = await service.get_playlist(source, playlist_id)
    if result is None:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Playlist not found"}, status_code=404)
    return result
