"""Library API routes."""

import asyncio
import logging
import sqlite3
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..models.database import (
    AlbumDownloadStateConflictError,
    AlbumDownloadStateError,
    AlbumNotFoundError,
)
from ..models.schemas import MarkDownloadedRequest
from ..services import scan as scan_service
from ..services.download import _parse_bool
from ..services.paths import resolve_database_dir, resolve_downloads_root
from ..services.scan import mark_album_downloaded, unmark_album_downloaded
from ..services.tasks import run_thread_write
from ..services.tracks import (
    TrackClientUnavailableError,
    TrackIdentityError,
    resolve_album_track_ids,
)
from .lifecycle import (
    claim_client_operation,
    client_operation,
    require_work_admission,
)

logger = logging.getLogger("streamrip")

router = APIRouter(prefix="/api/library", tags=["library"])


# How many finished scan jobs stay in the in-memory registry. The polling
# endpoint reads it, so a small rolling window is enough — without a cap it
# grows for the life of the process.
MAX_FINISHED_SCAN_JOBS = 20
TRACK_CLIENT_UNAVAILABLE_MESSAGE = "Connect the album source and retry."
TRACK_IDENTITY_ERROR_MESSAGE = "Could not load a complete track catalog. Retry later."
ALBUM_NOT_FOUND_MESSAGE = "Album not found"
ALBUM_DOWNLOAD_STATE_CONFLICT_MESSAGE = (
    "Album is queued or downloading. Wait for it to finish."
)
ALBUM_DOWNLOAD_STATE_ERROR_MESSAGE = "Could not update album download state"


def _prune_scan_jobs(jobs: dict, *, active_job_id: str | None) -> None:
    """Drop all but the most recently started finished scan jobs.

    Never evicts the active job, nor any job still reporting "running".
    ``dict`` preserves insertion order, so the oldest keys come first.
    """
    finished = [
        job_id
        for job_id, job in jobs.items()
        if job_id != active_job_id and job.get("status") != "running"
    ]
    excess = len(finished) - MAX_FINISHED_SCAN_JOBS
    for job_id in finished[:excess] if excess > 0 else []:
        jobs.pop(job_id, None)


def _track_identity_error(error: TrackIdentityError) -> JSONResponse:
    message = (
        TRACK_CLIENT_UNAVAILABLE_MESSAGE
        if isinstance(error, TrackClientUnavailableError)
        else TRACK_IDENTITY_ERROR_MESSAGE
    )
    return JSONResponse({"error": message}, status_code=error.status_code)


def _download_state_error(error: Exception) -> JSONResponse:
    if isinstance(error, AlbumNotFoundError):
        return JSONResponse(
            {"error": ALBUM_NOT_FOUND_MESSAGE}, status_code=error.status_code
        )
    if isinstance(error, AlbumDownloadStateConflictError):
        return JSONResponse(
            {"error": ALBUM_DOWNLOAD_STATE_CONFLICT_MESSAGE},
            status_code=error.status_code,
        )
    logger.exception("Could not reconcile album download state")
    return JSONResponse({"error": ALBUM_DOWNLOAD_STATE_ERROR_MESSAGE}, status_code=500)


def _validate_local_folder_path(
    db, raw: str | None
) -> tuple[str | None, JSONResponse | None]:
    """Resolve `raw` and verify it's inside the configured downloads root.

    Returns (resolved_path, None) on success; (None, error_response) on failure.
    Sync — intentionally extracted so the async route doesn't call Path.resolve()
    on the event loop.
    """
    if raw is None:
        return None, None
    downloads_root_cfg = resolve_downloads_root(db)
    try:
        resolved = Path(raw).resolve(strict=False)
        root = Path(downloads_root_cfg).resolve(strict=False)
    except (OSError, ValueError):
        return None, JSONResponse(
            {"error": "Invalid local_folder_path"}, status_code=400
        )
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, JSONResponse(
            {"error": "local_folder_path must be inside the configured downloads path"},
            status_code=400,
        )
    return str(resolved), None


@router.get("/{source}/albums")
async def get_albums(
    request: Request,
    source: str,
    page: int = 1,
    page_size: int = 50,
    sort_by: str = "added_to_library_at",
    sort_dir: str = "DESC",
    status: str | None = None,
    search: str | None = None,
):
    service = request.app.state.library_service
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    return await service.get_albums(
        source,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_dir=sort_dir,
        status=status,
        search=search,
    )


@router.get("/{source}/albums/{album_id}")
async def get_album_detail(request: Request, source: str, album_id: int):
    service = request.app.state.library_service
    album = request.app.state.db.get_album(album_id)
    operation_source = album["source"] if album is not None else source
    with client_operation(request, {operation_source}):
        result = await service.get_album_detail(album_id)
    if result is None:
        return JSONResponse({"error": "Album not found"}, status_code=404)
    return result


@router.post("/refresh/{source}")
async def refresh_library(request: Request, source: str):
    require_work_admission(request)
    service = request.app.state.library_service
    with client_operation(request, {source}):
        return await service.refresh_library(source)


@router.post("/albums/{album_id}/mark-downloaded")
async def mark_downloaded(request: Request, album_id: int, body: MarkDownloadedRequest):
    require_work_admission(request)
    db = request.app.state.db
    album = db.get_album(album_id)
    if album is None:
        return JSONResponse({"error": "Album not found"}, status_code=404)

    sentinel_enabled = _parse_bool(
        db.get_config("scan_sentinel_write_enabled"), default=True
    )

    resolved_path, err = _validate_local_folder_path(db, body.local_folder_path)
    if err is not None:
        return err

    with client_operation(request, {album["source"]}):
        try:
            track_ids = await resolve_album_track_ids(
                db, request.app.state._clients_ref, album_id
            )
        except TrackIdentityError as error:
            return _track_identity_error(error)

    try:
        await run_thread_write(
            mark_album_downloaded,
            db,
            album_id,
            local_folder_path=resolved_path,
            dedup_db_dir=resolve_database_dir(db),
            track_ids=track_ids,
            sentinel_write_enabled=sentinel_enabled,
            operation="manual mark-downloaded write",
        )
    except (AlbumDownloadStateError, sqlite3.Error) as error:
        return _download_state_error(error)
    await request.app.state.event_bus.publish(
        "album_status_changed",
        {"album_id": album_id, "status": "complete"},
    )
    return db.get_album(album_id)


@router.post("/albums/{album_id}/unmark-downloaded")
async def unmark_downloaded(request: Request, album_id: int):
    require_work_admission(request)
    db = request.app.state.db
    album = db.get_album(album_id)
    if album is None:
        return JSONResponse({"error": "Album not found"}, status_code=404)

    with client_operation(request, {album["source"]}):
        try:
            track_ids = await resolve_album_track_ids(
                db, request.app.state._clients_ref, album_id
            )
        except TrackIdentityError as error:
            return _track_identity_error(error)

    try:
        await run_thread_write(
            unmark_album_downloaded,
            db,
            album_id,
            dedup_db_dir=resolve_database_dir(db),
            track_ids=track_ids,
            operation="manual unmark-downloaded write",
        )
    except (AlbumDownloadStateError, sqlite3.Error) as error:
        return _download_state_error(error)
    await request.app.state.event_bus.publish(
        "album_status_changed",
        {"album_id": album_id, "status": "not_downloaded"},
    )
    return db.get_album(album_id)


@router.post("/scan-fuzzy")
async def start_scan(request: Request):
    require_work_admission(request)
    app = request.app
    if app.state.active_scan_job is not None:
        return JSONResponse(
            {"error": "Another scan is already running"}, status_code=409
        )

    db = app.state.db
    download_path = resolve_downloads_root(db)
    sentinel_enabled = _parse_bool(
        db.get_config("scan_sentinel_write_enabled"), default=True
    )

    _prune_scan_jobs(app.state.scan_jobs, active_job_id=app.state.active_scan_job)

    job_id = uuid.uuid4().hex

    # Wrap the event bus so scan_progress events also update the in-memory
    # job registry — the polling GET endpoint reads that so the UI can show
    # "Scanning… X / N" without subscribing to the WebSocket directly.
    class _ProgressTrackingBus:
        def __init__(self, inner, jobs, job_id):
            self._inner = inner
            self._jobs = jobs
            self._job_id = job_id

        async def publish(self, event_type, data):
            if event_type == "scan_progress":
                job = self._jobs.get(self._job_id)
                if job is not None and job["status"] == "running":
                    job["progress"] = {
                        "scanned": data.get("scanned", 0),
                        "total": data.get("total", 0),
                    }
            await self._inner.publish(event_type, data)

    tracked_bus = _ProgressTrackingBus(app.state.event_bus, app.state.scan_jobs, job_id)

    async def runner():
        try:
            result = await scan_service.run_scan(
                db,
                clients=app.state._clients_ref,
                download_path=download_path,
                dedup_db_dir=resolve_database_dir(db),
                event_bus=tracked_bus,
                sentinel_write_enabled=sentinel_enabled,
                stop_event=app.state.scan_stop_event,
            )
            app.state.scan_jobs[job_id] = {"status": "complete", "result": result}
        except asyncio.CancelledError:
            app.state.scan_jobs[job_id] = {
                "status": "complete",
                "result": {"status": "interrupted"},
            }
            raise
        except Exception:
            logger.exception("scan-fuzzy job %s failed", job_id)
            app.state.scan_jobs[job_id] = {
                "status": "error",
                "result": {"error": "Scan failed — see server logs"},
            }
        finally:
            if app.state.active_scan_job == job_id:
                app.state.active_scan_job = None

    operation_claim = claim_client_operation(request, {"qobuz", "tidal"})
    task = None
    try:
        app.state.scan_jobs[job_id] = {
            "status": "running",
            "progress": {"scanned": 0, "total": 0},
            "result": None,
        }
        app.state.active_scan_job = job_id
        task = asyncio.create_task(runner())
        app.state.scan_tasks.add(task)
        task.add_done_callback(app.state.scan_tasks.discard)
        task.add_done_callback(
            lambda _task: app.state.client_operations.release(operation_claim)
        )
    except BaseException:
        if task is not None:
            task.cancel()
            app.state.scan_tasks.discard(task)
        app.state.scan_jobs.pop(job_id, None)
        if app.state.active_scan_job == job_id:
            app.state.active_scan_job = None
        app.state.client_operations.release(operation_claim)
        raise
    return {"job_id": job_id}


@router.get("/scan-fuzzy/{job_id}")
async def scan_status(request: Request, job_id: str):
    job = request.app.state.scan_jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if job["status"] == "running":
        progress = job.get("progress") or {}
        return {
            "status": "running",
            "scanned": progress.get("scanned", 0),
            "total": progress.get("total", 0),
        }
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
    with client_operation(request, {source}):
        return await service.search(source, q, limit=page_size, offset=offset)


@router.get("/{source}/playlists")
async def list_playlists(request: Request, source: str):
    """List the user's playlists from the streaming service.

    Currently only Qobuz is implemented.  Tidal returns an empty list
    until the SDK gains playlist read methods.
    """
    service = request.app.state.library_service
    with client_operation(request, {source}):
        return await service.list_playlists(source)


@router.get("/{source}/playlists/{playlist_id}")
async def get_playlist(request: Request, source: str, playlist_id: int):
    """Fetch a playlist with its track list."""
    service = request.app.state.library_service
    with client_operation(request, {source}):
        result = await service.get_playlist(source, playlist_id)
    if result is None:
        return JSONResponse({"error": "Playlist not found"}, status_code=404)
    return result
