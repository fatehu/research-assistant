from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.projects import (
    ResearchProjectCreateRequest,
    ResearchProjectResponse,
    ResearchProjectRuntimeOverviewResponse,
    ResearchProjectWorkspaceOutputCleanupRequest,
    ResearchProjectWorkspaceOutputCleanupResponse,
    ResearchProjectWorkspaceOutputContentResponse,
    ResearchProjectWorkspaceOutputScopeCleanupRequest,
    ResearchProjectWorkspaceOutputSummary,
    ResearchProjectWorkspaceOutputUpdateRequest,
)
from app.services.project_service import ProjectService


router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ResearchProjectResponse])
async def list_projects(
    paper_id: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    payloads = await service.list_project_payloads(user_id=int(current_user.id), paper_id=paper_id)
    return [ResearchProjectResponse(**item) for item in payloads]


@router.post("", response_model=ResearchProjectResponse)
async def create_project(
    request: ResearchProjectCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    project = await service.create_project(
        user_id=int(current_user.id),
        title=request.title,
        goal=request.goal,
        status=request.status,
        paper_ids=list(request.paper_ids or []),
    )
    payload = await service.get_project_payload(project_id=int(project.id), user_id=int(current_user.id))
    if payload is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return ResearchProjectResponse(**payload)


@router.get("/{project_id}", response_model=ResearchProjectResponse)
async def get_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    payload = await service.get_project_payload(project_id=int(project_id), user_id=int(current_user.id))
    if payload is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return ResearchProjectResponse(**payload)


@router.get("/{project_id}/runtime-overview", response_model=ResearchProjectRuntimeOverviewResponse)
async def get_project_runtime_overview(
    project_id: int,
    recent_execution_limit: int = Query(default=5, ge=1, le=12),
    max_log_chars: int = Query(default=4000, ge=200, le=20000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    payload = await service.get_project_runtime_overview(
        project_id=int(project_id),
        user_id=int(current_user.id),
        recent_execution_limit=int(recent_execution_limit),
        max_log_chars=int(max_log_chars),
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return ResearchProjectRuntimeOverviewResponse(**payload)


@router.post("/{project_id}/workspaces/{workspace_id}/executions/{execution_id}/cancel", response_model=dict[str, Any])
async def cancel_project_execution(
    project_id: int,
    workspace_id: int,
    execution_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    payload = await service.cancel_project_execution(
        project_id=int(project_id),
        user_id=int(current_user.id),
        workspace_id=int(workspace_id),
        execution_id=str(execution_id or ""),
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="项目或 execution 不存在")
    return payload


@router.get("/{project_id}/workspaces/{workspace_id}/outputs", response_model=list[ResearchProjectWorkspaceOutputSummary])
async def list_project_workspace_outputs(
    project_id: int,
    workspace_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    payload = await service.list_workspace_outputs(
        project_id=int(project_id),
        user_id=int(current_user.id),
        workspace_id=int(workspace_id),
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="项目或 workspace 不存在")
    return [ResearchProjectWorkspaceOutputSummary(**item) for item in payload]


@router.get("/{project_id}/workspaces/{workspace_id}/outputs/content", response_model=ResearchProjectWorkspaceOutputContentResponse)
async def read_project_workspace_output(
    project_id: int,
    workspace_id: int,
    relative_path: str = Query(..., min_length=1, max_length=400),
    max_chars: int = Query(default=120000, ge=1000, le=400000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    try:
        payload = await service.read_workspace_output(
            project_id=int(project_id),
            user_id=int(current_user.id),
            workspace_id=int(workspace_id),
            relative_path=str(relative_path or ""),
            max_chars=int(max_chars),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload is None:
        raise HTTPException(status_code=404, detail="项目、workspace 或产物不存在")
    return ResearchProjectWorkspaceOutputContentResponse(**payload)


@router.put("/{project_id}/workspaces/{workspace_id}/outputs/content", response_model=ResearchProjectWorkspaceOutputContentResponse)
async def write_project_workspace_output(
    project_id: int,
    workspace_id: int,
    request: ResearchProjectWorkspaceOutputUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    try:
        payload = await service.write_workspace_output(
            project_id=int(project_id),
            user_id=int(current_user.id),
            workspace_id=int(workspace_id),
            relative_path=str(request.relative_path or ""),
            content=str(request.content or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload is None:
        raise HTTPException(status_code=404, detail="项目或 workspace 不存在")
    return ResearchProjectWorkspaceOutputContentResponse(**payload)


@router.delete("/{project_id}/workspaces/{workspace_id}/outputs", response_model=dict[str, Any])
async def delete_project_workspace_output(
    project_id: int,
    workspace_id: int,
    relative_path: str = Query(..., min_length=1, max_length=400),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    try:
        payload = await service.delete_workspace_output(
            project_id=int(project_id),
            user_id=int(current_user.id),
            workspace_id=int(workspace_id),
            relative_path=str(relative_path or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload is None:
        raise HTTPException(status_code=404, detail="项目、workspace 或产物不存在")
    return payload


@router.post("/{project_id}/workspaces/{workspace_id}/outputs/cleanup", response_model=ResearchProjectWorkspaceOutputCleanupResponse)
async def cleanup_project_workspace_outputs(
    project_id: int,
    workspace_id: int,
    request: ResearchProjectWorkspaceOutputCleanupRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    payload = await service.cleanup_workspace_outputs(
        project_id=int(project_id),
        user_id=int(current_user.id),
        workspace_id=int(workspace_id),
        preserve_repo=bool(request.preserve_repo),
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="项目或 workspace 不存在")
    return ResearchProjectWorkspaceOutputCleanupResponse(**payload)


@router.post("/{project_id}/workspaces/{workspace_id}/outputs/cleanup-scope", response_model=ResearchProjectWorkspaceOutputCleanupResponse)
async def cleanup_project_workspace_outputs_scope(
    project_id: int,
    workspace_id: int,
    request: ResearchProjectWorkspaceOutputScopeCleanupRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    payload = await service.cleanup_workspace_outputs_scope(
        project_id=int(project_id),
        user_id=int(current_user.id),
        workspace_id=int(workspace_id),
        scope=str(request.scope),
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="项目或 workspace 不存在")
    return ResearchProjectWorkspaceOutputCleanupResponse(**payload)


# Deprecated compatibility aliases.
@router.get("/{project_id}/workspaces/{workspace_id}/assets", response_model=list[ResearchProjectWorkspaceOutputSummary], include_in_schema=False)
async def list_project_workspace_assets(
    project_id: int,
    workspace_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await list_project_workspace_outputs(project_id, workspace_id, db, current_user)


@router.get("/{project_id}/workspaces/{workspace_id}/assets/content", response_model=ResearchProjectWorkspaceOutputContentResponse, include_in_schema=False)
async def read_project_workspace_asset(
    project_id: int,
    workspace_id: int,
    relative_path: str = Query(..., min_length=1, max_length=400),
    max_chars: int = Query(default=120000, ge=1000, le=400000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await read_project_workspace_output(project_id, workspace_id, relative_path, max_chars, db, current_user)


@router.put("/{project_id}/workspaces/{workspace_id}/assets/content", response_model=ResearchProjectWorkspaceOutputContentResponse, include_in_schema=False)
async def write_project_workspace_asset(
    project_id: int,
    workspace_id: int,
    request: ResearchProjectWorkspaceOutputUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await write_project_workspace_output(project_id, workspace_id, request, db, current_user)


@router.delete("/{project_id}/workspaces/{workspace_id}/assets", response_model=dict[str, Any], include_in_schema=False)
async def delete_project_workspace_asset(
    project_id: int,
    workspace_id: int,
    relative_path: str = Query(..., min_length=1, max_length=400),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await delete_project_workspace_output(project_id, workspace_id, relative_path, db, current_user)


@router.post("/{project_id}/workspaces/{workspace_id}/assets/cleanup", response_model=ResearchProjectWorkspaceOutputCleanupResponse, include_in_schema=False)
async def cleanup_project_workspace_assets(
    project_id: int,
    workspace_id: int,
    request: ResearchProjectWorkspaceOutputCleanupRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await cleanup_project_workspace_outputs(project_id, workspace_id, request, db, current_user)
