"""
Global FastAPI error handlers.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from app.core.errors import AppServiceError


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppServiceError)
    async def _handle_app_service_error(request: Request, exc: AppServiceError):
        logger.warning(
            f"[AppServiceError] path={request.url.path} code={exc.code} status={exc.status_code} message={exc.message}"
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_detail())

