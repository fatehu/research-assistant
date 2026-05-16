from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services.docx_template_service import DocxTemplateService


router = APIRouter(prefix="/docx/templates", tags=["docx-templates"])


class DocxTemplateUpsertRequest(BaseModel):
    template_id: Optional[str] = Field(default=None, max_length=160)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    md_constraints: str = ""
    docx_constraints: str = ""


class DocxTemplateFileRoleRequest(BaseModel):
    file_role: str = Field(default="reference")


class DocxTemplateAnalyzeRequest(BaseModel):
    user_notes: str = ""


class DefaultDocxStylePromptRequest(BaseModel):
    prompt: str = ""


@router.get("/overview")
async def get_docx_template_overview(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await DocxTemplateService().list_overview_for_user(db, user_id=int(current_user.id))


@router.put("/default-docx-style-prompt")
async def update_default_docx_style_prompt(
    request: DefaultDocxStylePromptRequest,
    current_user: User = Depends(get_current_user),
):
    return DocxTemplateService().update_default_docx_style_prompt(request.prompt)


@router.get("/files/download")
async def download_docx_managed_file(
    relative_path: str = Query(min_length=1, max_length=1000),
    current_user: User = Depends(get_current_user),
):
    file_info = DocxTemplateService().resolve_download_file(relative_path)
    if file_info is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(
        path=str(file_info["path"]),
        filename=str(file_info["filename"]),
        media_type=str(file_info["media_type"]),
    )


@router.post("")
async def upsert_docx_template(
    request: DocxTemplateUpsertRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = DocxTemplateService()
    template = service.upsert_template(
        template_id=request.template_id,
        name=request.name,
        description=request.description,
        md_constraints=request.md_constraints,
        docx_constraints=request.docx_constraints,
        user_id=int(current_user.id),
    )
    await service.sync_template_to_db(db, user_id=int(current_user.id), template=template)
    await db.commit()
    return template


@router.get("/{template_id}")
async def get_docx_template(
    template_id: str,
    current_user: User = Depends(get_current_user),
):
    template = DocxTemplateService().get_template(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return template


@router.post("/{template_id}/files")
async def upload_docx_template_file(
    template_id: str,
    file: UploadFile = File(...),
    file_role: str = Form(default="reference"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    content = await file.read()
    try:
        service = DocxTemplateService()
        file_payload = service.save_template_file(
            template_id=template_id,
            filename=file.filename or "template-file",
            content=content,
            file_role=file_role,
        )
        template = service.get_template(template_id)
        if template is not None:
            await service.sync_template_to_db(db, user_id=int(current_user.id), template=template)
        await service.sync_template_file_to_db(
            db,
            user_id=int(current_user.id),
            template_id=template_id,
            file_payload=file_payload,
        )
        await db.commit()
        return file_payload
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/{template_id}/files/{file_name}/role")
async def update_docx_template_file_role(
    template_id: str,
    file_name: str,
    request: DocxTemplateFileRoleRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        service = DocxTemplateService()
        file_payload = service.update_template_file_role(
            template_id=template_id,
            filename=file_name,
            file_role=request.file_role,
        )
        await service.sync_template_file_to_db(
            db,
            user_id=int(current_user.id),
            template_id=template_id,
            file_payload=file_payload,
        )
        await db.commit()
        return file_payload
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{template_id}/files/{file_name}")
async def delete_docx_template_file(
    template_id: str,
    file_name: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        service = DocxTemplateService()
        result = service.delete_template_file(
            template_id=template_id,
            filename=file_name,
        )
        await service.delete_template_file_from_db(
            db,
            template_id=template_id,
            stored_filename=str(result.get("file_name") or file_name),
        )
        await db.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{template_id}/analyze")
async def analyze_docx_template_constraints(
    template_id: str,
    request: DocxTemplateAnalyzeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        service = DocxTemplateService()
        result = await service.generate_constraints_with_llm(
            template_id=template_id,
            user_notes=request.user_notes,
        )
        await service.sync_template_analysis_to_db(
            db,
            template_id=template_id,
            analysis=dict(result.get("analysis") or {}),
        )
        await db.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
