"""
资源共享路由 - 支持论文、知识库、文献集、笔记本共享
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func
from pydantic import BaseModel
from typing import Optional, List
from loguru import logger

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.role import (
    UserRole, ResearchGroup, GroupMember, SharedResource, SharePermission
)
from app.models.knowledge import KnowledgeBase
from app.models.literature import PaperCollection, Paper
from app.models.notebook import Notebook

router = APIRouter()


# ========== Schemas ==========

class ShareResourceRequest(BaseModel):
    resource_type: str  # knowledge_base, paper_collection, paper, notebook
    resource_id: int
    shared_with_type: str  # user, group, all_students
    shared_with_id: Optional[int] = None
    permission: str = "read"  # read, write
    message: Optional[str] = None


class SharedResourceResponse(BaseModel):
    id: int
    resource_type: str
    resource_id: int
    resource_name: str
    resource_detail: Optional[dict] = None
    owner_id: int
    owner_name: str
    owner_avatar: Optional[str] = None
    shared_with_type: str
    shared_with_id: Optional[int]
    shared_with_name: Optional[str]
    permission: str
    message: Optional[str] = None
    created_at: datetime
    expires_at: Optional[datetime] = None


class SharedWithMeResponse(BaseModel):
    id: int
    resource_type: str
    resource_id: int
    resource_name: str
    resource_detail: Optional[dict] = None
    owner_id: int
    owner_name: str
    owner_avatar: Optional[str] = None
    permission: str
    shared_at: datetime
    group_name: Optional[str] = None


# ========== 我共享的资源 ==========

@router.get("/my-shares", response_model=List[SharedResourceResponse])
async def list_my_shared_resources(
    resource_type: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取我共享出去的资源"""
    query = select(SharedResource).where(SharedResource.owner_id == current_user.id)
    
    if resource_type:
        query = query.where(SharedResource.resource_type == resource_type)
    
    query = query.order_by(SharedResource.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    resources = result.scalars().all()
    
    return await _build_resource_responses(resources, db)


@router.post("/", response_model=SharedResourceResponse)
async def share_resource(
    data: ShareResourceRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """共享资源"""
    # 验证资源存在且属于当前用户
    resource_name = ""
    resource_detail = None
    
    if data.resource_type == "knowledge_base":
        result = await db.execute(
            select(KnowledgeBase).where(
                and_(
                    KnowledgeBase.id == data.resource_id,
                    KnowledgeBase.user_id == current_user.id
                )
            )
        )
        resource = result.scalar_one_or_none()
        if not resource:
            raise HTTPException(status_code=404, detail="知识库不存在或不属于您")
        resource_name = resource.name
        
    elif data.resource_type == "paper_collection":
        result = await db.execute(
            select(PaperCollection).where(
                and_(
                    PaperCollection.id == data.resource_id,
                    PaperCollection.user_id == current_user.id
                )
            )
        )
        resource = result.scalar_one_or_none()
        if not resource:
            raise HTTPException(status_code=404, detail="文献集不存在或不属于您")
        resource_name = resource.name
        
    elif data.resource_type == "paper":
        result = await db.execute(
            select(Paper).where(
                and_(
                    Paper.id == data.resource_id,
                    Paper.user_id == current_user.id
                )
            )
        )
        resource = result.scalar_one_or_none()
        if not resource:
            raise HTTPException(status_code=404, detail="论文不存在或不属于您")
        resource_name = resource.title
        resource_detail = {
            "title": resource.title,
            "authors": resource.author_names[:3] if resource.authors else [],
            "year": resource.year,
            "venue": resource.venue,
            "abstract": resource.abstract[:200] + "..." if resource.abstract and len(resource.abstract) > 200 else resource.abstract,
            "pdf_url": resource.pdf_url,
            "url": resource.url
        }
        
    elif data.resource_type == "notebook":
        result = await db.execute(
            select(Notebook).where(
                and_(
                    Notebook.id == data.resource_id,
                    Notebook.user_id == current_user.id
                )
            )
        )
        resource = result.scalar_one_or_none()
        if not resource:
            raise HTTPException(status_code=404, detail="笔记本不存在或不属于您")
        resource_name = resource.title
    else:
        raise HTTPException(status_code=400, detail="不支持的资源类型")
    
    # 验证共享对象
    shared_with_name = None
    
    if data.shared_with_type == 'user':
        if not data.shared_with_id:
            raise HTTPException(status_code=400, detail="请指定共享给的用户")
        
        user_result = await db.execute(
            select(User).where(User.id == data.shared_with_id)
        )
        target_user = user_result.scalar_one_or_none()
        if not target_user:
            raise HTTPException(status_code=404, detail="目标用户不存在")
        shared_with_name = target_user.full_name or target_user.username
        
    elif data.shared_with_type == 'group':
        if not data.shared_with_id:
            raise HTTPException(status_code=400, detail="请指定共享给的研究组")
        
        # 验证研究组存在（且用户是导师或成员）
        group_result = await db.execute(
            select(ResearchGroup).where(ResearchGroup.id == data.shared_with_id)
        )
        group = group_result.scalar_one_or_none()
        if not group:
            raise HTTPException(status_code=404, detail="研究组不存在")
        
        # 检查权限：必须是组的导师或成员
        is_mentor = group.mentor_id == current_user.id
        member_result = await db.execute(
            select(GroupMember).where(
                and_(
                    GroupMember.group_id == data.shared_with_id,
                    GroupMember.user_id == current_user.id
                )
            )
        )
        is_member = member_result.scalar_one_or_none() is not None
        
        if not is_mentor and not is_member:
            raise HTTPException(status_code=403, detail="您不是该研究组的成员")
        
        shared_with_name = group.name
        
    elif data.shared_with_type == 'all_students':
        # 只有导师可以共享给所有学生
        if current_user.role != UserRole.MENTOR.value:
            raise HTTPException(status_code=403, detail="只有导师可以共享给所有学生")
        shared_with_name = "所有学生"
    else:
        raise HTTPException(status_code=400, detail="不支持的共享类型")
    
    # 检查是否已存在相同的共享
    existing = await db.execute(
        select(SharedResource).where(
            and_(
                SharedResource.resource_type == data.resource_type,
                SharedResource.resource_id == data.resource_id,
                SharedResource.owner_id == current_user.id,
                SharedResource.shared_with_type == data.shared_with_type,
                SharedResource.shared_with_id == data.shared_with_id
            )
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="已存在相同的共享")
    
    # 创建共享
    shared = SharedResource(
        resource_type=data.resource_type,
        resource_id=data.resource_id,
        owner_id=current_user.id,
        shared_with_type=data.shared_with_type,
        shared_with_id=data.shared_with_id,
        permission=data.permission,
    )
    db.add(shared)
    await db.commit()
    await db.refresh(shared)
    
    logger.info(
        f"用户 {current_user.username} 共享了 {data.resource_type} "
        f"给 {data.shared_with_type}:{data.shared_with_id or 'all'}"
    )
    
    return SharedResourceResponse(
        id=shared.id,
        resource_type=shared.resource_type,
        resource_id=shared.resource_id,
        resource_name=resource_name,
        resource_detail=resource_detail,
        owner_id=shared.owner_id,
        owner_name=current_user.full_name or current_user.username,
        owner_avatar=current_user.avatar,
        shared_with_type=shared.shared_with_type,
        shared_with_id=shared.shared_with_id,
        shared_with_name=shared_with_name,
        permission=shared.permission,
        created_at=shared.created_at,
        expires_at=shared.expires_at
    )


@router.delete("/{share_id}")
async def remove_share(
    share_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """取消共享"""
    result = await db.execute(
        select(SharedResource).where(
            and_(
                SharedResource.id == share_id,
                SharedResource.owner_id == current_user.id
            )
        )
    )
    shared = result.scalar_one_or_none()
    
    if not shared:
        raise HTTPException(status_code=404, detail="共享记录不存在")
    
    await db.delete(shared)
    await db.commit()
    
    logger.info(f"用户 {current_user.username} 取消了共享 #{share_id}")
    
    return {"message": "共享已取消"}


# ========== 共享给我的资源 ==========

@router.get("/shared-with-me", response_model=List[SharedWithMeResponse])
async def get_shared_with_me(
    resource_type: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取共享给我的资源"""
    # 获取用户加入的研究组
    group_ids_result = await db.execute(
        select(GroupMember.group_id).where(GroupMember.user_id == current_user.id)
    )
    group_ids = [row[0] for row in group_ids_result.fetchall()]
    
    # 构建查询条件
    conditions = [
        # 直接共享给用户
        and_(
            SharedResource.shared_with_type == 'user',
            SharedResource.shared_with_id == current_user.id
        ),
    ]
    
    # 共享给用户的研究组
    if group_ids:
        conditions.append(
            and_(
                SharedResource.shared_with_type == 'group',
                SharedResource.shared_with_id.in_(group_ids)
            )
        )
    
    # 导师共享给所有学生
    if current_user.mentor_id:
        conditions.append(
            and_(
                SharedResource.shared_with_type == 'all_students',
                SharedResource.owner_id == current_user.mentor_id
            )
        )
    
    # 如果是学生，还可以看到所在研究组导师共享给all_students的
    if current_user.role == UserRole.STUDENT.value and group_ids:
        # 获取组的导师ID
        mentor_ids_result = await db.execute(
            select(ResearchGroup.mentor_id).where(ResearchGroup.id.in_(group_ids))
        )
        mentor_ids = [row[0] for row in mentor_ids_result.fetchall()]
        if mentor_ids:
            conditions.append(
                and_(
                    SharedResource.shared_with_type == 'all_students',
                    SharedResource.owner_id.in_(mentor_ids)
                )
            )
    
    query = select(SharedResource).where(
        and_(
            or_(*conditions),
            SharedResource.owner_id != current_user.id,  # 排除自己共享的
            # 未过期
            or_(
                SharedResource.expires_at == None,
                SharedResource.expires_at > datetime.utcnow()
            )
        )
    )
    
    if resource_type:
        query = query.where(SharedResource.resource_type == resource_type)
    
    query = query.order_by(SharedResource.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    resources = result.scalars().all()
    
    # 构建响应
    responses = []
    for res in resources:
        resource_name = ""
        resource_detail = None
        
        if res.resource_type == "knowledge_base":
            kb_result = await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.id == res.resource_id)
            )
            kb = kb_result.scalar_one_or_none()
            if kb:
                resource_name = kb.name
                resource_detail = {"description": kb.description}
                
        elif res.resource_type == "paper_collection":
            pc_result = await db.execute(
                select(PaperCollection).where(PaperCollection.id == res.resource_id)
            )
            pc = pc_result.scalar_one_or_none()
            if pc:
                resource_name = pc.name
                resource_detail = {
                    "description": pc.description,
                    "paper_count": pc.paper_count
                }
                
        elif res.resource_type == "paper":
            paper_result = await db.execute(
                select(Paper).where(Paper.id == res.resource_id)
            )
            paper = paper_result.scalar_one_or_none()
            if paper:
                resource_name = paper.title
                resource_detail = {
                    "title": paper.title,
                    "authors": paper.author_names[:3] if paper.authors else [],
                    "year": paper.year,
                    "venue": paper.venue,
                    "abstract": paper.abstract[:300] + "..." if paper.abstract and len(paper.abstract) > 300 else paper.abstract,
                    "pdf_url": paper.pdf_url,
                    "url": paper.url,
                    "citation_count": paper.citation_count
                }
                
        elif res.resource_type == "notebook":
            nb_result = await db.execute(
                select(Notebook).where(Notebook.id == res.resource_id)
            )
            nb = nb_result.scalar_one_or_none()
            if nb:
                resource_name = nb.title
        
        if not resource_name:
            continue  # 资源已删除
        
        # 获取所有者信息
        owner_result = await db.execute(
            select(User).where(User.id == res.owner_id)
        )
        owner = owner_result.scalar_one_or_none()
        
        # 获取研究组名称
        group_name = None
        if res.shared_with_type == 'group':
            group_result = await db.execute(
                select(ResearchGroup.name).where(ResearchGroup.id == res.shared_with_id)
            )
            group_row = group_result.first()
            group_name = group_row[0] if group_row else None
        
        responses.append(SharedWithMeResponse(
            id=res.id,
            resource_type=res.resource_type,
            resource_id=res.resource_id,
            resource_name=resource_name,
            resource_detail=resource_detail,
            owner_id=res.owner_id,
            owner_name=owner.full_name or owner.username if owner else "未知",
            owner_avatar=owner.avatar if owner else None,
            permission=res.permission,
            shared_at=res.created_at,
            group_name=group_name
        ))
    
    return responses


@router.get("/shared-with-me/count")
async def get_shared_count(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取共享给我的资源数量统计"""
    # 获取用户加入的研究组
    group_ids_result = await db.execute(
        select(GroupMember.group_id).where(GroupMember.user_id == current_user.id)
    )
    group_ids = [row[0] for row in group_ids_result.fetchall()]
    
    conditions = [
        and_(
            SharedResource.shared_with_type == 'user',
            SharedResource.shared_with_id == current_user.id
        ),
    ]
    
    if group_ids:
        conditions.append(
            and_(
                SharedResource.shared_with_type == 'group',
                SharedResource.shared_with_id.in_(group_ids)
            )
        )
    
    if current_user.mentor_id:
        conditions.append(
            and_(
                SharedResource.shared_with_type == 'all_students',
                SharedResource.owner_id == current_user.mentor_id
            )
        )
    
    if current_user.role == UserRole.STUDENT.value and group_ids:
        mentor_ids_result = await db.execute(
            select(ResearchGroup.mentor_id).where(ResearchGroup.id.in_(group_ids))
        )
        mentor_ids = [row[0] for row in mentor_ids_result.fetchall()]
        if mentor_ids:
            conditions.append(
                and_(
                    SharedResource.shared_with_type == 'all_students',
                    SharedResource.owner_id.in_(mentor_ids)
                )
            )
    
    # 统计各类型数量
    counts = {}
    for rtype in ["paper", "paper_collection", "knowledge_base", "notebook"]:
        count_result = await db.execute(
            select(func.count(SharedResource.id)).where(
                and_(
                    or_(*conditions),
                    SharedResource.owner_id != current_user.id,
                    SharedResource.resource_type == rtype,
                    or_(
                        SharedResource.expires_at == None,
                        SharedResource.expires_at > datetime.utcnow()
                    )
                )
            )
        )
        counts[rtype] = count_result.scalar() or 0
    
    counts["total"] = sum(counts.values())
    return counts


# ========== 获取可共享的研究组 ==========

@router.get("/my-groups")
async def get_my_groups_for_sharing(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取可以共享资源的研究组列表"""
    groups = []
    
    # 作为导师拥有的组
    if current_user.role == UserRole.MENTOR.value:
        owned_result = await db.execute(
            select(ResearchGroup).where(
                and_(
                    ResearchGroup.mentor_id == current_user.id,
                    ResearchGroup.is_active == True
                )
            )
        )
        for g in owned_result.scalars().all():
            groups.append({
                "id": g.id,
                "name": g.name,
                "role": "mentor"
            })
    
    # 作为成员加入的组
    member_result = await db.execute(
        select(ResearchGroup).join(GroupMember).where(
            and_(
                GroupMember.user_id == current_user.id,
                ResearchGroup.is_active == True
            )
        )
    )
    for g in member_result.scalars().all():
        if not any(x["id"] == g.id for x in groups):
            groups.append({
                "id": g.id,
                "name": g.name,
                "role": "member"
            })
    
    return groups


# ========== 获取我的论文列表（用于共享选择）==========

@router.get("/my-papers")
async def get_my_papers_for_sharing(
    search: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取我的论文列表（用于选择共享）"""
    query = select(Paper).where(Paper.user_id == current_user.id)
    
    if search:
        query = query.where(
            or_(
                Paper.title.ilike(f"%{search}%"),
                Paper.abstract.ilike(f"%{search}%")
            )
        )
    
    query = query.order_by(Paper.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    papers = result.scalars().all()
    
    return [
        {
            "id": p.id,
            "title": p.title,
            "authors": p.author_names[:3] if p.authors else [],
            "year": p.year,
            "venue": p.venue
        }
        for p in papers
    ]


# ========== 辅助函数 ==========

async def _build_resource_responses(
    resources: list[SharedResource],
    db: AsyncSession
) -> list[SharedResourceResponse]:
    """构建资源响应列表"""
    responses = []
    
    for res in resources:
        resource_name = ""
        resource_detail = None
        
        if res.resource_type == "knowledge_base":
            kb_result = await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.id == res.resource_id)
            )
            kb = kb_result.scalar_one_or_none()
            if kb:
                resource_name = kb.name
                
        elif res.resource_type == "paper_collection":
            pc_result = await db.execute(
                select(PaperCollection).where(PaperCollection.id == res.resource_id)
            )
            pc = pc_result.scalar_one_or_none()
            if pc:
                resource_name = pc.name
                
        elif res.resource_type == "paper":
            paper_result = await db.execute(
                select(Paper).where(Paper.id == res.resource_id)
            )
            paper = paper_result.scalar_one_or_none()
            if paper:
                resource_name = paper.title
                resource_detail = {
                    "title": paper.title,
                    "authors": paper.author_names[:3] if paper.authors else [],
                    "year": paper.year,
                    "venue": paper.venue
                }
                
        elif res.resource_type == "notebook":
            nb_result = await db.execute(
                select(Notebook).where(Notebook.id == res.resource_id)
            )
            nb = nb_result.scalar_one_or_none()
            if nb:
                resource_name = nb.title
        
        if not resource_name:
            continue
        
        # 获取所有者信息
        owner_result = await db.execute(
            select(User).where(User.id == res.owner_id)
        )
        owner = owner_result.scalar_one_or_none()
        
        # 获取共享对象名称
        shared_with_name = None
        if res.shared_with_type == 'user':
            user_result = await db.execute(
                select(User.username, User.full_name).where(User.id == res.shared_with_id)
            )
            user_row = user_result.first()
            shared_with_name = (user_row[1] or user_row[0]) if user_row else None
        elif res.shared_with_type == 'group':
            group_result = await db.execute(
                select(ResearchGroup.name).where(ResearchGroup.id == res.shared_with_id)
            )
            group_row = group_result.first()
            shared_with_name = group_row[0] if group_row else None
        elif res.shared_with_type == 'all_students':
            shared_with_name = "所有学生"
        
        responses.append(SharedResourceResponse(
            id=res.id,
            resource_type=res.resource_type,
            resource_id=res.resource_id,
            resource_name=resource_name,
            resource_detail=resource_detail,
            owner_id=res.owner_id,
            owner_name=owner.full_name or owner.username if owner else "",
            owner_avatar=owner.avatar if owner else None,
            shared_with_type=res.shared_with_type,
            shared_with_id=res.shared_with_id,
            shared_with_name=shared_with_name,
            permission=res.permission,
            created_at=res.created_at,
            expires_at=res.expires_at
        ))
    
    return responses
