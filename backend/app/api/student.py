"""
学生路由
"""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from loguru import logger

from app.core.database import get_db
from app.core.security import get_current_user
from app.core.permissions import get_student_user
from app.models.user import User
from app.models.role import (
    UserRole, ResearchGroup, GroupMember, Invitation, InvitationStatus,
    Announcement, AnnouncementRead, SharedResource, ShareType
)
from app.models.knowledge import KnowledgeBase
from app.models.literature import PaperCollection
from app.models.notebook import Notebook
from app.schemas.role import (
    MentorResponse, PeerResponse, ApplyToMentorRequest,
    InvitationResponse, AnnouncementResponse, SharedResourceResponse,
    GroupResponse
)

router = APIRouter()


# ========== 导师相关 ==========

@router.get("/mentor", response_model=MentorResponse | None)
async def get_my_mentor(
    current_user: User = Depends(get_student_user),
    db: AsyncSession = Depends(get_db)
):
    """获取我的导师信息"""
    if not current_user.mentor_id:
        return None
    
    result = await db.execute(
        select(User).where(User.id == current_user.mentor_id)
    )
    mentor = result.scalar_one_or_none()
    
    if not mentor:
        return None
    
    # 获取导师的学生数量
    student_count = await db.scalar(
        select(func.count(User.id)).where(User.mentor_id == mentor.id)
    )
    
    return MentorResponse(
        id=mentor.id,
        username=mentor.username,
        full_name=mentor.full_name,
        email=mentor.email,
        avatar=mentor.avatar,
        bio=mentor.bio,
        department=mentor.department,
        research_direction=mentor.research_direction,
        student_count=student_count or 0
    )


@router.get("/mentors/available", response_model=list[MentorResponse])
async def list_available_mentors(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    search: str = None,
    current_user: User = Depends(get_student_user),
    db: AsyncSession = Depends(get_db)
):
    """获取可申请的导师列表"""
    query = select(User).where(
        and_(
            User.role == UserRole.MENTOR.value.value,
            User.is_active == True
        )
    )
    
    if search:
        search_pattern = f"%{search}%"
        query = query.where(
            or_(
                User.username.ilike(search_pattern),
                User.full_name.ilike(search_pattern),
                User.department.ilike(search_pattern),
                User.research_direction.ilike(search_pattern)
            )
        )
    
    query = query.order_by(User.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    mentors = result.scalars().all()
    
    mentor_list = []
    for mentor in mentors:
        student_count = await db.scalar(
            select(func.count(User.id)).where(User.mentor_id == mentor.id)
        )
        mentor_list.append(MentorResponse(
            id=mentor.id,
            username=mentor.username,
            full_name=mentor.full_name,
            email=mentor.email,
            avatar=mentor.avatar,
            bio=mentor.bio,
            department=mentor.department,
            research_direction=mentor.research_direction,
            student_count=student_count or 0
        ))
    
    return mentor_list


@router.get("/mentors/search", response_model=list[MentorResponse])
async def search_mentors(
    query: str = Query(..., min_length=1),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_student_user),
    db: AsyncSession = Depends(get_db)
):
    """搜索导师"""
    search_pattern = f"%{query}%"
    
    db_query = select(User).where(
        and_(
            User.role == UserRole.MENTOR.value,
            User.is_active == True,
            or_(
                User.username.ilike(search_pattern),
                User.full_name.ilike(search_pattern),
                User.email.ilike(search_pattern),
                User.department.ilike(search_pattern),
                User.research_direction.ilike(search_pattern)
            )
        )
    ).order_by(User.created_at.desc()).offset(skip).limit(limit)
    
    result = await db.execute(db_query)
    mentors = result.scalars().all()
    
    mentor_list = []
    for mentor in mentors:
        student_count = await db.scalar(
            select(func.count(User.id)).where(User.mentor_id == mentor.id)
        )
        mentor_list.append(MentorResponse(
            id=mentor.id,
            username=mentor.username,
            full_name=mentor.full_name,
            email=mentor.email,
            avatar=mentor.avatar,
            bio=mentor.bio,
            department=mentor.department,
            research_direction=mentor.research_direction,
            student_count=student_count or 0
        ))
    
    return mentor_list


@router.post("/mentor/apply")
async def apply_to_mentor(
    data: ApplyToMentorRequest,
    current_user: User = Depends(get_student_user),
    db: AsyncSession = Depends(get_db)
):
    """申请加入导师组"""
    if current_user.mentor_id:
        raise HTTPException(status_code=400, detail="您已有导师，不能重复申请")
    
    # 验证导师
    result = await db.execute(
        select(User).where(
            and_(
                User.id == data.mentor_id,
                User.role == UserRole.MENTOR.value,
                User.is_active == True
            )
        )
    )
    mentor = result.scalar_one_or_none()
    
    if not mentor:
        raise HTTPException(status_code=404, detail="导师不存在或不可用")
    
    # 检查是否已有待处理的申请
    existing = await db.execute(
        select(Invitation).where(
            and_(
                Invitation.from_user_id == current_user.id,
                Invitation.to_user_id == data.mentor_id,
                Invitation.type == 'apply',
                Invitation.status == InvitationStatus.PENDING
            )
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="您已有待处理的申请")
    
    # 创建申请
    invitation = Invitation(
        type='apply',
        from_user_id=current_user.id,
        to_user_id=data.mentor_id,
        message=data.message,
        expires_at=datetime.utcnow() + timedelta(days=30)
    )
    db.add(invitation)
    await db.commit()
    await db.refresh(invitation)
    
    logger.info(f"学生 {current_user.username} 申请加入导师 {mentor.username}")
    
    return {"message": "申请已提交", "invitation_id": invitation.id}


@router.post("/mentor/leave")
async def leave_mentor(
    current_user: User = Depends(get_student_user),
    db: AsyncSession = Depends(get_db)
):
    """离开导师组"""
    if not current_user.mentor_id:
        raise HTTPException(status_code=400, detail="您还没有加入任何导师组")
    
    mentor_id = current_user.mentor_id
    current_user.mentor_id = None
    current_user.joined_at = None
    
    # 同时从研究组中移除
    await db.execute(
        GroupMember.__table__.delete().where(
            and_(
                GroupMember.user_id == current_user.id,
                GroupMember.group_id.in_(
                    select(ResearchGroup.id).where(ResearchGroup.mentor_id == mentor_id)
                )
            )
        )
    )
    
    await db.commit()
    
    logger.info(f"学生 {current_user.username} 离开了导师组")
    
    return {"message": "已离开导师组"}


# ========== 同组同学 ==========

@router.get("/peers", response_model=list[PeerResponse])
async def get_peers(
    current_user: User = Depends(get_student_user),
    db: AsyncSession = Depends(get_db)
):
    """获取同组同学列表"""
    if not current_user.mentor_id:
        return []
    
    result = await db.execute(
        select(User).where(
            and_(
                User.mentor_id == current_user.mentor_id,
                User.id != current_user.id,
                User.is_active == True
            )
        )
    )
    peers = result.scalars().all()
    
    return [PeerResponse.model_validate(p) for p in peers]


# ========== 研究组 ==========

@router.get("/groups", response_model=list[GroupResponse])
async def get_my_groups(
    current_user: User = Depends(get_student_user),
    db: AsyncSession = Depends(get_db)
):
    """获取我加入的研究组"""
    result = await db.execute(
        select(ResearchGroup)
        .join(GroupMember, GroupMember.group_id == ResearchGroup.id)
        .where(GroupMember.user_id == current_user.id)
    )
    groups = result.scalars().all()
    
    group_list = []
    for group in groups:
        member_count = await db.scalar(
            select(func.count(GroupMember.id)).where(GroupMember.group_id == group.id)
        )
        group_data = GroupResponse.model_validate(group)
        group_data.member_count = member_count or 0
        group_list.append(group_data)
    
    return group_list


# ========== 公告 ==========

@router.get("/announcements", response_model=list[AnnouncementResponse])
async def get_announcements(
    unread_only: bool = False,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_student_user),
    db: AsyncSession = Depends(get_db)
):
    """获取我的公告（来自导师的公告）"""
    if not current_user.mentor_id:
        return []
    
    # 获取我加入的研究组
    group_ids_result = await db.execute(
        select(GroupMember.group_id).where(GroupMember.user_id == current_user.id)
    )
    group_ids = [row[0] for row in group_ids_result.fetchall()]
    
    # 查询公告：导师发给所有学生的 或 发给我所在研究组的
    query = select(Announcement).where(
        and_(
            Announcement.mentor_id == current_user.mentor_id,
            Announcement.is_active == True,
            or_(
                Announcement.group_id == None,
                Announcement.group_id.in_(group_ids) if group_ids else False
            )
        )
    )
    
    query = query.order_by(
        Announcement.is_pinned.desc(),
        Announcement.created_at.desc()
    ).offset(skip).limit(limit)
    
    result = await db.execute(query)
    announcements = result.scalars().all()
    
    # 获取导师名称
    mentor_result = await db.execute(
        select(User.username, User.full_name).where(User.id == current_user.mentor_id)
    )
    mentor_info = mentor_result.first()
    mentor_name = mentor_info[1] or mentor_info[0] if mentor_info else ""
    
    # 获取已读状态
    read_ids_result = await db.execute(
        select(AnnouncementRead.announcement_id).where(
            AnnouncementRead.user_id == current_user.id
        )
    )
    read_ids = set(row[0] for row in read_ids_result.fetchall())
    
    # 构建响应
    announcement_list = []
    for ann in announcements:
        is_read = ann.id in read_ids
        if unread_only and is_read:
            continue
        
        # 获取研究组名称
        group_name = None
        if ann.group_id:
            group_result = await db.execute(
                select(ResearchGroup.name).where(ResearchGroup.id == ann.group_id)
            )
            group_row = group_result.first()
            group_name = group_row[0] if group_row else None
        
        announcement_list.append(AnnouncementResponse(
            id=ann.id,
            mentor_id=ann.mentor_id,
            mentor_name=mentor_name,
            group_id=ann.group_id,
            group_name=group_name,
            title=ann.title,
            content=ann.content,
            is_pinned=ann.is_pinned,
            is_active=ann.is_active,
            is_read=is_read,
            created_at=ann.created_at,
            updated_at=ann.updated_at
        ))
    
    return announcement_list


@router.post("/announcements/{announcement_id}/read")
async def mark_announcement_read(
    announcement_id: int,
    current_user: User = Depends(get_student_user),
    db: AsyncSession = Depends(get_db)
):
    """标记公告为已读"""
    # 验证公告存在且属于自己的导师
    result = await db.execute(
        select(Announcement).where(
            and_(
                Announcement.id == announcement_id,
                Announcement.mentor_id == current_user.mentor_id
            )
        )
    )
    announcement = result.scalar_one_or_none()
    
    if not announcement:
        raise HTTPException(status_code=404, detail="公告不存在")
    
    # 检查是否已读
    existing = await db.execute(
        select(AnnouncementRead).where(
            and_(
                AnnouncementRead.announcement_id == announcement_id,
                AnnouncementRead.user_id == current_user.id
            )
        )
    )
    if existing.scalar_one_or_none():
        return {"message": "已标记为已读"}
    
    # 创建已读记录
    read_record = AnnouncementRead(
        announcement_id=announcement_id,
        user_id=current_user.id
    )
    db.add(read_record)
    await db.commit()
    
    return {"message": "已标记为已读"}


@router.get("/announcements/unread-count")
async def get_unread_announcement_count(
    current_user: User = Depends(get_student_user),
    db: AsyncSession = Depends(get_db)
):
    """获取未读公告数量"""
    if not current_user.mentor_id:
        return {"count": 0}
    
    # 获取我加入的研究组
    group_ids_result = await db.execute(
        select(GroupMember.group_id).where(GroupMember.user_id == current_user.id)
    )
    group_ids = [row[0] for row in group_ids_result.fetchall()]
    
    # 查询公告总数
    total_query = select(func.count(Announcement.id)).where(
        and_(
            Announcement.mentor_id == current_user.mentor_id,
            Announcement.is_active == True,
            or_(
                Announcement.group_id == None,
                Announcement.group_id.in_(group_ids) if group_ids else False
            )
        )
    )
    total_count = await db.scalar(total_query)
    
    # 查询已读数
    read_count = await db.scalar(
        select(func.count(AnnouncementRead.id)).where(
            and_(
                AnnouncementRead.user_id == current_user.id,
                AnnouncementRead.announcement_id.in_(
                    select(Announcement.id).where(
                        Announcement.mentor_id == current_user.mentor_id
                    )
                )
            )
        )
    )
    
    return {"count": (total_count or 0) - (read_count or 0)}


# ========== 共享资源 ==========

@router.get("/shared-resources", response_model=list[SharedResourceResponse])
async def get_shared_resources(
    resource_type: ShareType = None,
    current_user: User = Depends(get_student_user),
    db: AsyncSession = Depends(get_db)
):
    """获取共享给我的资源"""
    if not current_user.mentor_id:
        return []
    
    # 获取我加入的研究组
    group_ids_result = await db.execute(
        select(GroupMember.group_id).where(GroupMember.user_id == current_user.id)
    )
    group_ids = [row[0] for row in group_ids_result.fetchall()]
    
    # 查询共享资源
    query = select(SharedResource).where(
        or_(
            # 直接共享给我
            and_(
                SharedResource.shared_with_type == 'user',
                SharedResource.shared_with_id == current_user.id
            ),
            # 共享给我的研究组
            and_(
                SharedResource.shared_with_type == 'group',
                SharedResource.shared_with_id.in_(group_ids) if group_ids else False
            ),
            # 共享给导师的所有学生
            and_(
                SharedResource.shared_with_type == 'all_students',
                SharedResource.owner_id == current_user.mentor_id
            )
        )
    )
    
    if resource_type:
        query = query.where(SharedResource.resource_type == resource_type)
    
    # 排除过期的
    query = query.where(
        or_(
            SharedResource.expires_at == None,
            SharedResource.expires_at > datetime.utcnow()
        )
    )
    
    result = await db.execute(query)
    resources = result.scalars().all()
    
    # 构建响应
    resource_list = []
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
        
        resource_list.append(SharedResourceResponse(
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
    
    return resource_list
