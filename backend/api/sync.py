"""Sync API routes."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..services.sync import SyncServiceStoppingError
from .lifecycle import require_work_admission

router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.get("/status/{source}")
async def sync_status(request: Request, source: str):
    service = request.app.state.sync_service
    return await service.get_diff(source)


@router.post("/run/{source}")
async def run_sync(request: Request, source: str, download_new: bool = False):
    require_work_admission(request)
    service = request.app.state.sync_service
    try:
        return await service.run_sync(source, download_new=download_new)
    except SyncServiceStoppingError as error:
        return JSONResponse({"error": str(error)}, status_code=503)


@router.get("/history")
async def sync_history(request: Request, source: str = "qobuz", limit: int = 10):
    service = request.app.state.sync_service
    return await service.get_history(source, limit=limit)
