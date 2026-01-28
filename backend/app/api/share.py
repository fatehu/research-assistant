"""
资源共享路由
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_
from loguru import logger

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.role import (
    UserRole, ResearchGroup, GroupMember, SharedResource, ShareType, SharePermission
)
from app.models.knowledge import KnowledgeBase
from app.models.literature import PaperCollection
from app.models.notebook import Notebook
from app.schemas.role import ShareResourceRequest, SharedResourceResponse

router = APIRouter()


@router.get("/", response_model=list[SharedResourceResponse])
async def list_my_shared_resources(
    resource_type: ShareType = None,
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
    
    if data.resource_type == ShareType.KNOWLEDGE_BASE:
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
        
    elif data.resource_type == ShareType.PAPER_COLLECTION:
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
        
    elif data.resource_type == ShareType.NOTEBOOK:
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
        expires_at=data.expires_at
    )
    db.add(shared)
    await db.commit()
    await db.refresh(shared)
    
    logger.info(
        f"用户 {current_user.username} 共享了 {data.resource_type.value} "
        f"给 {data.shared_with_type}:{data.shared_with_id or 'all'}"
    )
    
    return SharedResourceResponse(
        id=shared.id,
        resource_type=shared.resource_type,
        resource_id=shared.resource_id,
        resource_name=resource_name,
        owner_id=shared.owner_id,
        owner_name=current_user.full_name or current_user.username,
        shared_with_type=shared.shared_with_type,
        shared_with_id=shared.shared_with_id,
        shared_with_name=shared_with_name,
        permission=shared.permission,
        created_at=shared.created_at,
        expires_at=shared.expires_at
    )


@router.put("/{share_id}")
async def update_share(
    share_id: int,
    permission: SharePermission = None,
    expires_at: datetime = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """更新共享设置"""
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
    
    if permission:
        shared.permission = permission
    if expires_at is not None:
        shared.expires_at = expires_at
    
    await db.commit()
    
    return {"message": "共享设置已更新"}


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


@router.get("/check-access")
async def check_resource_access(
    resource_type: ShareType,
    resource_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """检查对资源的访问权限"""
    # 检查是否是资源所有者
    is_owner = False
    resource_name = ""
    
    if resource_type == ShareType.KNOWLEDGE_BASE:
        result = await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == resource_id)
        )
        resource = result.scalar_one_or_none()
        if resource:
            is_owner = resource.user_id == current_user.id
            resource_name = resource.name
            
    elif resource_type == ShareType.PAPER_COLLECTION:
        result = await db.execute(
            select(PaperCollection).where(PaperCollection.id == resource_id)
        )
        resource = result.scalar_one_or_none()
        if resource:
            is_owner = resource.user_id == current_user.id
            resource_name = resource.name
            
    elif resource_type == ShareType.NOTEBOOK:
        result = await db.execute(
            select(Notebook).where(Notebook.id == resource_id)
        )
        resource = result.scalar_one_or_none()
        if resource:
            is_owner = resource.user_id == current_user.id
            resource_name = resource.title
    
    if is_owner:
        return {
            "has_access": True,
            "permission": "admin",
            "is_owner": True,
            "resource_name": resource_name
        }
    
    # 检查是否有共享权限
    # 获取用户加入的研究组
    group_ids_result = await db.execute(
        select(GroupMember.group_id).where(GroupMember.user_id == current_user.id)
    )
    group_ids = [row[0] for row in group_ids_result.fetchall()]
    
    # 查找共享记录
    share_query = select(SharedResource).where(
        and_(
            SharedResource.resource_type == resource_type,
            SharedResource.resource_id == resource_id,
            or_(
                # 直接共享给用户
                and_(
                    SharedResource.shared_with_type == 'user',
                    SharedResource.shared_with_id == current_user.id
                ),
                # 共享给用户的研究组
                and_(
                    SharedResource.shared_with_type == 'group',
                    SharedResource.shared_with_id.in_(group_ids) if group_ids else False
                ),
                # 共享给导师的所有学生
                and_(
                    SharedResource.shared_with_type == 'all_students',
                    SharedResource.owner_id == current_user.mentor_id
                ) if current_user.mentor_id else False
            ),
            # 未过期
            or_(
                SharedResource.expires_at == None,
                SharedResource.expires_at > datetime.utcnow()
            )
        )
    )
    
    share_result = await db.execute(share_query)
    shared = share_result.scalar_one_or_none()
    
    if shared:
        return {
            "has_access": True,
            "permission": shared.permission,
            "is_owner": False,
            "resource_name": resource_name,
            "shared_by": shared.owner_id
        }
    
    return {
        "has_access": False,
        "permission": None,
        "is_owner": False,
        "resource_name": resource_name
    }


async def _build_resource_responses(
    resources: list[SharedResource],
    db: AsyncSession
) -> list[SharedResourceResponse]:
    """构建资源响应列表"""
    responses = []
    
    for res in resources:
        # 获取资源名称
        resource_name = ""
        if res.resource_type == ShareType.KNOWLEDGE_BASE:
            kb_result = await db.execute(
                select(KnowledgeBase.name).where(KnowledgeBase.id == res.resource_id)
            )
            kb_row = kb_result.first()
            resource_name = kb_row[0] if kb_row else "未知知识库"
        elif res.resource_type == ShareType.PAPER_COLLECTION:
            pc_result = await db.execute(
                select(PaperCollection.name).where(PaperCollection.id == res.resource_id)
            )
            pc_row = pc_result.first()
            resource_name = pc_row[0] if pc_row else "未知文献集"
        elif res.resource_type == ShareType.NOTEBOOK:
            nb_result = await db.execute(
                select(Notebook.title).where(Notebook.id == res.resource_id)
            )
            nb_row = nb_result.first()
            resource_name = nb_row[0] if nb_row else "未知笔记本"
        
        # 获取所有者名称
        owner_result = await db.execute(
            select(User.username, User.full_name).where(User.id == res.owner_id)
        )
        owner_info = owner_result.first()
        owner_name = owner_info[1] or owner_info[0] if owner_info else ""
        
        # 获取共享对象名称
        shared_with_name = None
        if res.shared_with_type == 'user':
            user_result = await db.execute(
                select(User.username).where(User.id == res.shared_with_id)
            )
            user_row = user_result.first()
            shared_with_name = user_row[0] if user_row else None
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
            owner_id=res.owner_id,
            owner_name=owner_name,
            shared_with_type=res.shared_with_type,
            shared_with_id=res.shared_with_id,
            shared_with_name=shared_with_name,
            permission=res.permission,
            created_at=res.created_at,
            expires_at=res.expires_at
        ))
    
    return responses
