"""
公告路由（导师管理公告）
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from loguru import logger

from app.core.database import get_db
from app.core.security import get_current_user
from app.core.permissions import get_mentor_user
from app.models.user import User
from app.models.role import (
    UserRole, ResearchGroup, Announcement, AnnouncementRead
)
from app.schemas.role import (
    AnnouncementCreate, AnnouncementUpdate, AnnouncementResponse
)

router = APIRouter()


@router.get("/", response_model=list[AnnouncementResponse])
async def list_announcements(
    group_id: int = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """获取我发布的公告列表"""
    query = select(Announcement).where(Announcement.mentor_id == current_user.id)
    
    if group_id is not None:
        query = query.where(Announcement.group_id == group_id)
    
    query = query.order_by(
        Announcement.is_pinned.desc(),
        Announcement.created_at.desc()
    ).offset(skip).limit(limit)
    
    result = await db.execute(query)
    announcements = result.scalars().all()
    
    return await _build_announcement_responses(announcements, current_user, db)


@router.post("/", response_model=AnnouncementResponse)
async def create_announcement(
    data: AnnouncementCreate,
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """创建公告"""
    # 验证研究组
    if data.group_id:
        group_result = await db.execute(
            select(ResearchGroup).where(
                and_(
                    ResearchGroup.id == data.group_id,
                    ResearchGroup.mentor_id == current_user.id
                )
            )
        )
        if not group_result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="研究组不存在或不属于您")
    
    announcement = Announcement(
        mentor_id=current_user.id,
        group_id=data.group_id,
        title=data.title,
        content=data.content,
        is_pinned=data.is_pinned
    )
    db.add(announcement)
    await db.commit()
    await db.refresh(announcement)
    
    logger.info(f"导师 {current_user.username} 创建了公告: {announcement.title}")
    
    # 获取研究组名称
    group_name = None
    if announcement.group_id:
        group_result = await db.execute(
            select(ResearchGroup.name).where(ResearchGroup.id == announcement.group_id)
        )
        group_row = group_result.first()
        group_name = group_row[0] if group_row else None
    
    return AnnouncementResponse(
        id=announcement.id,
        mentor_id=announcement.mentor_id,
        mentor_name=current_user.full_name or current_user.username,
        group_id=announcement.group_id,
        group_name=group_name,
        title=announcement.title,
        content=announcement.content,
        is_pinned=announcement.is_pinned,
        is_active=announcement.is_active,
        is_read=True,  # 自己发的公告默认已读
        created_at=announcement.created_at,
        updated_at=announcement.updated_at
    )


@router.get("/{announcement_id}", response_model=AnnouncementResponse)
async def get_announcement(
    announcement_id: int,
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """获取公告详情"""
    result = await db.execute(
        select(Announcement).where(
            and_(
                Announcement.id == announcement_id,
                Announcement.mentor_id == current_user.id
            )
        )
    )
    announcement = result.scalar_one_or_none()
    
    if not announcement:
        raise HTTPException(status_code=404, detail="公告不存在")
    
    responses = await _build_announcement_responses([announcement], current_user, db)
    return responses[0]


@router.put("/{announcement_id}", response_model=AnnouncementResponse)
async def update_announcement(
    announcement_id: int,
    data: AnnouncementUpdate,
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """更新公告"""
    result = await db.execute(
        select(Announcement).where(
            and_(
                Announcement.id == announcement_id,
                Announcement.mentor_id == current_user.id
            )
        )
    )
    announcement = result.scalar_one_or_none()
    
    if not announcement:
        raise HTTPException(status_code=404, detail="公告不存在")
    
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(announcement, field, value)
    
    await db.commit()
    await db.refresh(announcement)
    
    logger.info(f"导师 {current_user.username} 更新了公告: {announcement.title}")
    
    responses = await _build_announcement_responses([announcement], current_user, db)
    return responses[0]


@router.delete("/{announcement_id}")
async def delete_announcement(
    announcement_id: int,
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """删除公告"""
    result = await db.execute(
        select(Announcement).where(
            and_(
                Announcement.id == announcement_id,
                Announcement.mentor_id == current_user.id
            )
        )
    )
    announcement = result.scalar_one_or_none()
    
    if not announcement:
        raise HTTPException(status_code=404, detail="公告不存在")
    
    title = announcement.title
    await db.delete(announcement)
    await db.commit()
    
    logger.info(f"导师 {current_user.username} 删除了公告: {title}")
    
    return {"message": "公告已删除"}


@router.get("/{announcement_id}/read-status")
async def get_announcement_read_status(
    announcement_id: int,
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """获取公告的阅读状态统计"""
    # 验证公告存在且属于当前用户
    result = await db.execute(
        select(Announcement).where(
            and_(
                Announcement.id == announcement_id,
                Announcement.mentor_id == current_user.id
            )
        )
    )
    announcement = result.scalar_one_or_none()
    
    if not announcement:
        raise HTTPException(status_code=404, detail="公告不存在")
    
    # 获取应该看到这个公告的学生数量
    if announcement.group_id:
        # 只统计研究组成员
        from app.models.role import GroupMember
        total_count = await db.scalar(
            select(func.count(GroupMember.id)).where(
                GroupMember.group_id == announcement.group_id
            )
        )
    else:
        # 统计所有学生
        total_count = await db.scalar(
            select(func.count(User.id)).where(
                User.mentor_id == current_user.id
            )
        )
    
    # 获取已读数量
    read_count = await db.scalar(
        select(func.count(AnnouncementRead.id)).where(
            AnnouncementRead.announcement_id == announcement_id
        )
    )
    
    # 获取已读学生列表
    read_result = await db.execute(
        select(User.id, User.username, User.full_name, AnnouncementRead.read_at)
        .join(AnnouncementRead, AnnouncementRead.user_id == User.id)
        .where(AnnouncementRead.announcement_id == announcement_id)
        .order_by(AnnouncementRead.read_at.desc())
    )
    read_list = [
        {
            "user_id": row[0],
            "username": row[1],
            "full_name": row[2],
            "read_at": row[3].isoformat() if row[3] else None
        }
        for row in read_result.fetchall()
    ]
    
    return {
        "total_students": total_count or 0,
        "read_count": read_count or 0,
        "unread_count": (total_count or 0) - (read_count or 0),
        "read_rate": round((read_count or 0) / (total_count or 1) * 100, 1),
        "read_list": read_list
    }


async def _build_announcement_responses(
    announcements: list[Announcement],
    current_user: User,
    db: AsyncSession
) -> list[AnnouncementResponse]:
    """构建公告响应列表"""
    responses = []
    
    for ann in announcements:
        # 获取研究组名称
        group_name = None
        if ann.group_id:
            group_result = await db.execute(
                select(ResearchGroup.name).where(ResearchGroup.id == ann.group_id)
            )
            group_row = group_result.first()
            group_name = group_row[0] if group_row else None
        
        responses.append(AnnouncementResponse(
            id=ann.id,
            mentor_id=ann.mentor_id,
            mentor_name=current_user.full_name or current_user.username,
            group_id=ann.group_id,
            group_name=group_name,
            title=ann.title,
            content=ann.content,
            is_pinned=ann.is_pinned,
            is_active=ann.is_active,
            is_read=True,  # 自己发的公告默认已读
            created_at=ann.created_at,
            updated_at=ann.updated_at
        ))
    
    return responses
