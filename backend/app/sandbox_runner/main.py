"""
Internal CodeLab sandbox runner service.

This service should only be reachable in the internal container network.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.config import settings
from app.sandbox_runner.local_executor import CodeLabExecutor as LocalCodeLabExecutor


app = FastAPI(
    title="CodeLab Sandbox Runner",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_executors: Dict[str, LocalCodeLabExecutor] = {}


def _require_internal_token(authorization: Optional[str] = Header(default=None)):
    configured = str(getattr(settings, "codelab_runner_token", "") or "").strip()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "runner_token_missing", "message": "runner token is not configured"},
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "missing bearer token"},
        )
    token = authorization[7:].strip()
    if token != configured:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "invalid bearer token"},
        )


class ExecutePayload(BaseModel):
    notebook_id: str = Field(min_length=1, max_length=200)
    code: str
    timeout_seconds: int = Field(default=20, ge=0, le=86400)
    hard_timeout_seconds: int = Field(default=20, ge=0, le=86400)
    workspace: Optional[Dict[str, Any]] = None


class NotebookPayload(BaseModel):
    notebook_id: str = Field(min_length=1, max_length=200)
    hard_timeout_seconds: int = Field(default=20, ge=0, le=86400)
    workspace: Optional[Dict[str, Any]] = None


def _get_or_create_executor(notebook_id: str, hard_timeout_seconds: int) -> LocalCodeLabExecutor:
    executor = _executors.get(notebook_id)
    if executor is None:
        executor = LocalCodeLabExecutor(notebook_id=notebook_id, hard_timeout_seconds=hard_timeout_seconds)
        _executors[notebook_id] = executor
    return executor


@app.post("/internal/codelab/execute", dependencies=[Depends(_require_internal_token)])
def execute(payload: ExecutePayload):
    executor = _get_or_create_executor(payload.notebook_id, payload.hard_timeout_seconds)
    return executor.execute(
        code=payload.code,
        timeout_seconds=payload.timeout_seconds,
        workspace_context=payload.workspace,
    )


@app.post("/internal/codelab/reset", dependencies=[Depends(_require_internal_token)])
def reset(payload: NotebookPayload):
    executor = _get_or_create_executor(payload.notebook_id, payload.hard_timeout_seconds)
    executor.reset(workspace_context=payload.workspace)
    return {
        "success": True,
        "variables": executor.get_variables(workspace_context=payload.workspace),
    }


@app.get("/internal/codelab/variables", dependencies=[Depends(_require_internal_token)])
def variables(
    notebook_id: str = Query(..., min_length=1, max_length=200),
    hard_timeout_seconds: int = Query(default=20, ge=0, le=86400),
):
    executor = _get_or_create_executor(notebook_id, hard_timeout_seconds)
    return {
        "variables": executor.get_variables(),
    }


@app.post("/internal/codelab/close", dependencies=[Depends(_require_internal_token)])
def close(payload: NotebookPayload):
    executor = _executors.pop(payload.notebook_id, None)
    if executor is not None:
        executor.close()
    return {"success": True}


@app.post("/internal/codelab/interrupt", dependencies=[Depends(_require_internal_token)])
def interrupt(payload: NotebookPayload):
    executor = _get_or_create_executor(payload.notebook_id, payload.hard_timeout_seconds)
    executor.interrupt()
    return {"success": True}


@app.get("/internal/healthz")
def healthz():
    return {"ok": True}
