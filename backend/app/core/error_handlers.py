"""
Global FastAPI error handlers.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from app.core.errors import AppServiceError


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppServiceError)
    async def _handle_app_service_error(request: Request, exc: AppServiceError):
        request_id = (
            exc.request_id
            or request.headers.get("X-Request-ID")
            or request.headers.get("x-request-id")
            or str(uuid4())
        )
        logger.warning(
            f"[AppServiceError] path={request.url.path} code={exc.code} status={exc.status_code} "
            f"request_id={request_id} message={exc.message}"
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_detail(default_request_id=request_id))

