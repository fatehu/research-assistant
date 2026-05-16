from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.projects import (
    ResearchProjectCreateRequest,
    ResearchProjectFolderTreeResponse,
    ResearchProjectResponse,
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


@router.get("/{project_id}/folder-tree", response_model=ResearchProjectFolderTreeResponse)
async def get_project_folder_tree(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    payload = await service.get_project_folder_tree(project_id=int(project_id), user_id=int(current_user.id))
    if payload is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return ResearchProjectFolderTreeResponse(**payload)


@router.delete("/{project_id}", response_model=dict[str, Any])
async def delete_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    payload = await service.delete_project(project_id=int(project_id), user_id=int(current_user.id))
    if payload is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return payload
