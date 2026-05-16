from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.core.security import get_current_user
from app.models.user import User
from app.services.literature_review_workspace_service import LiteratureReviewWorkspaceService


router = APIRouter(prefix="/literature-reviews", tags=["literature-reviews"])


@router.get("/overview")
async def get_literature_review_overview(
    current_user: User = Depends(get_current_user),
):
    return LiteratureReviewWorkspaceService().list_workspaces(user_id=int(current_user.id))


@router.get("/{review_id}")
async def get_literature_review_workspace(
    review_id: str,
    current_user: User = Depends(get_current_user),
):
    workspace = LiteratureReviewWorkspaceService().get_workspace(review_id, user_id=int(current_user.id))
    if workspace is None:
        raise HTTPException(status_code=404, detail="文献综述工作区不存在")
    return workspace


@router.get("/{review_id}/files/content")
async def read_literature_review_file_content(
    review_id: str,
    relative_path: str = Query(min_length=1, max_length=1000),
    current_user: User = Depends(get_current_user),
):
    payload = LiteratureReviewWorkspaceService().read_preview_file(
        review_id,
        relative_path,
        user_id=int(current_user.id),
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="文件不存在或不支持预览")
    return payload


@router.get("/{review_id}/files/download")
async def download_literature_review_file(
    review_id: str,
    relative_path: str = Query(min_length=1, max_length=1000),
    current_user: User = Depends(get_current_user),
):
    file_info = LiteratureReviewWorkspaceService().resolve_download_file(
        review_id,
        relative_path,
        user_id=int(current_user.id),
    )
    if file_info is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(
        path=str(file_info["path"]),
        filename=str(file_info["filename"]),
        media_type=str(file_info["media_type"]),
    )
