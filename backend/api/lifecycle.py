"""Small shared admission guard for shutdown-sensitive API writes."""

from fastapi import HTTPException, Request


def require_work_admission(request: Request) -> None:
    if getattr(request.app.state, "shutting_down", False):
        raise HTTPException(status_code=503, detail="Application is shutting down")
