"""Small shared admission guards for shutdown and source-client work."""

from contextlib import contextmanager

from fastapi import HTTPException, Request

from ..services.client_activation import (
    ClientActivationShuttingDownError,
    ClientReloadBusyError,
)


def require_work_admission(request: Request) -> None:
    if getattr(request.app.state, "shutting_down", False):
        raise HTTPException(status_code=503, detail="Application is shutting down")


@contextmanager
def client_operation(request: Request, sources):
    """Register client-backed work or translate admission failures to HTTP."""
    try:
        with request.app.state.client_operations.operation(sources):
            yield
    except ClientReloadBusyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ClientActivationShuttingDownError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def claim_client_operation(request: Request, sources) -> object:
    """Claim a scope synchronously for work handed to a background task."""
    try:
        return request.app.state.client_operations.claim(sources)
    except ClientReloadBusyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ClientActivationShuttingDownError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
