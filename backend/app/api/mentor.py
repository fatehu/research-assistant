"""
导师路由
"""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from loguru import logger

from app.core.database import get_db
from app.core.security import get_current_user
from app.core.permissions import get_mentor_user
from app.models.user import User
from app.models.role import (
    UserRole, ResearchGroup, GroupMember, Invitation, InvitationStatus
)
from app.models.conversation import Conversation
from app.models.knowledge import KnowledgeBase
from app.models.literature import Paper
from app.models.notebook import Notebook
from app.schemas.role import (
    StudentResponse, InviteStudentRequest, StudentProgressResponse,
    GroupCreate, GroupUpdate, GroupResponse, GroupDetailResponse, GroupMemberResponse,
    AddGroupMemberRequest
)

router = APIRouter()


# ========== 学生管理 ==========

@router.get("/students", response_model=list[StudentResponse])
async def get_my_students(
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """获取我的学生列表"""
    result = await db.execute(
        select(User)
        .where(User.mentor_id == current_user.id)
        .order_by(User.joined_at.desc())
    )
    students = result.scalars().all()
    return [StudentResponse.model_validate(s) for s in students]


@router.get("/students/count")
async def get_student_count(
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """获取学生数量"""
    count = await db.scalar(
        select(func.count(User.id)).where(User.mentor_id == current_user.id)
    )
    return {"count": count or 0}


@router.post("/students/invite")
async def invite_student(
    data: InviteStudentRequest,
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """邀请学生加入"""
    # 确保当前用户是导师
    if current_user.role != UserRole.MENTOR.value:
        raise HTTPException(status_code=403, detail="只有导师可以邀请学生")
    
    # 查找目标用户
    result = await db.execute(
        select(User).where(User.email == data.email)
    )
    target_user = result.scalar_one_or_none()
    
    if not target_user:
        raise HTTPException(status_code=404, detail="用户不存在，请确认邮箱是否正确")
    
    if target_user.role != UserRole.STUDENT.value:
        raise HTTPException(status_code=400, detail="只能邀请学生角色的用户")
    
    if target_user.mentor_id:
        raise HTTPException(status_code=400, detail="该学生已有导师")
    
    if target_user.id == current_user.id:
        raise HTTPException(status_code=400, detail="不能邀请自己")
    
    # 检查是否已有待处理的邀请
    existing = await db.execute(
        select(Invitation).where(
            and_(
                Invitation.from_user_id == current_user.id,
                Invitation.to_user_id == target_user.id,
                Invitation.status == InvitationStatus.PENDING
            )
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="已存在待处理的邀请")
    
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
    
    # 创建邀请
    invitation = Invitation(
        type='invite',
        from_user_id=current_user.id,
        to_user_id=target_user.id,
        group_id=data.group_id,
        message=data.message,
        expires_at=datetime.utcnow() + timedelta(days=7)
    )
    db.add(invitation)
    await db.commit()
    await db.refresh(invitation)
    
    logger.info(f"导师 {current_user.username} 邀请学生 {target_user.username}")
    
    return {"message": "邀请已发送", "invitation_id": invitation.id}


@router.delete("/students/{student_id}")
async def remove_student(
    student_id: int,
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """移除学生"""
    result = await db.execute(
        select(User).where(
            and_(
                User.id == student_id,
                User.mentor_id == current_user.id
            )
        )
    )
    student = result.scalar_one_or_none()
    
    if not student:
        raise HTTPException(status_code=404, detail="学生不存在或不是您的学生")
    
    student_name = student.username
    student.mentor_id = None
    student.joined_at = None
    
    # 同时从研究组中移除
    await db.execute(
        GroupMember.__table__.delete().where(
            and_(
                GroupMember.user_id == student_id,
                GroupMember.group_id.in_(
                    select(ResearchGroup.id).where(ResearchGroup.mentor_id == current_user.id)
                )
            )
        )
    )
    
    await db.commit()
    
    logger.info(f"导师 {current_user.username} 移除了学生 {student_name}")
    
    return {"message": "学生已移除"}


@router.get("/students/{student_id}/progress", response_model=StudentProgressResponse)
async def get_student_progress(
    student_id: int,
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """查看学生进度"""
    result = await db.execute(
        select(User).where(
            and_(
                User.id == student_id,
                User.mentor_id == current_user.id
            )
        )
    )
    student = result.scalar_one_or_none()
    
    if not student:
        raise HTTPException(status_code=404, detail="学生不存在或不是您的学生")
    
    # 获取学生的各项统计
    conversation_count = await db.scalar(
        select(func.count(Conversation.id)).where(Conversation.user_id == student_id)
    )
    knowledge_base_count = await db.scalar(
        select(func.count(KnowledgeBase.id)).where(KnowledgeBase.user_id == student_id)
    )
    paper_count = await db.scalar(
        select(func.count(Paper.id)).where(Paper.user_id == student_id)
    )
    notebook_count = await db.scalar(
        select(func.count(Notebook.id)).where(Notebook.user_id == student_id)
    )
    
    return StudentProgressResponse(
        student_id=student_id,
        username=student.username,
        full_name=student.full_name,
        last_login=student.last_login,
        joined_at=student.joined_at,
        conversation_count=conversation_count or 0,
        knowledge_base_count=knowledge_base_count or 0,
        paper_count=paper_count or 0,
        notebook_count=notebook_count or 0
    )


# ========== 研究组管理 ==========

@router.get("/groups", response_model=list[GroupResponse])
async def get_my_groups(
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """获取我的研究组列表"""
    result = await db.execute(
        select(ResearchGroup)
        .where(ResearchGroup.mentor_id == current_user.id)
        .order_by(ResearchGroup.created_at.desc())
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


@router.post("/groups", response_model=GroupResponse)
async def create_group(
    data: GroupCreate,
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """创建研究组"""
    if current_user.role != UserRole.MENTOR.value:
        raise HTTPException(status_code=403, detail="只有导师可以创建研究组")
    
    group = ResearchGroup(
        name=data.name,
        description=data.description,
        mentor_id=current_user.id,
        max_members=data.max_members
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)
    
    logger.info(f"导师 {current_user.username} 创建了研究组 {group.name}")
    
    response = GroupResponse.model_validate(group)
    response.member_count = 0
    return response


@router.get("/groups/{group_id}", response_model=GroupDetailResponse)
async def get_group(
    group_id: int,
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """获取研究组详情"""
    result = await db.execute(
        select(ResearchGroup).where(
            and_(
                ResearchGroup.id == group_id,
                ResearchGroup.mentor_id == current_user.id
            )
        )
    )
    group = result.scalar_one_or_none()
    
    if not group:
        raise HTTPException(status_code=404, detail="研究组不存在")
    
    # 获取成员列表
    members_result = await db.execute(
        select(GroupMember, User)
        .join(User, GroupMember.user_id == User.id)
        .where(GroupMember.group_id == group_id)
    )
    members = []
    for gm, user in members_result:
        members.append(GroupMemberResponse(
            id=gm.id,
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            avatar=user.avatar,
            role=gm.role,
            joined_at=gm.joined_at
        ))
    
    response = GroupDetailResponse.model_validate(group)
    response.member_count = len(members)
    response.members = members
    return response


@router.put("/groups/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: int,
    data: GroupUpdate,
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """更新研究组"""
    result = await db.execute(
        select(ResearchGroup).where(
            and_(
                ResearchGroup.id == group_id,
                ResearchGroup.mentor_id == current_user.id
            )
        )
    )
    group = result.scalar_one_or_none()
    
    if not group:
        raise HTTPException(status_code=404, detail="研究组不存在")
    
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(group, field, value)
    
    await db.commit()
    await db.refresh(group)
    
    member_count = await db.scalar(
        select(func.count(GroupMember.id)).where(GroupMember.group_id == group.id)
    )
    
    response = GroupResponse.model_validate(group)
    response.member_count = member_count or 0
    return response


@router.delete("/groups/{group_id}")
async def delete_group(
    group_id: int,
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """删除研究组"""
    result = await db.execute(
        select(ResearchGroup).where(
            and_(
                ResearchGroup.id == group_id,
                ResearchGroup.mentor_id == current_user.id
            )
        )
    )
    group = result.scalar_one_or_none()
    
    if not group:
        raise HTTPException(status_code=404, detail="研究组不存在")
    
    group_name = group.name
    await db.delete(group)
    await db.commit()
    
    logger.info(f"导师 {current_user.username} 删除了研究组 {group_name}")
    
    return {"message": "研究组已删除"}


@router.post("/groups/{group_id}/members")
async def add_group_member(
    group_id: int,
    data: AddGroupMemberRequest,
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """添加组成员"""
    student_id = data.user_id
    
    # 验证研究组
    group_result = await db.execute(
        select(ResearchGroup).where(
            and_(
                ResearchGroup.id == group_id,
                ResearchGroup.mentor_id == current_user.id
            )
        )
    )
    group = group_result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="研究组不存在")
    
    # 验证学生
    student_result = await db.execute(
        select(User).where(
            and_(
                User.id == student_id,
                User.mentor_id == current_user.id
            )
        )
    )
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="学生不存在或不是您的学生")
    
    # 检查是否已在组中
    existing = await db.execute(
        select(GroupMember).where(
            and_(
                GroupMember.group_id == group_id,
                GroupMember.user_id == student_id
            )
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="学生已在该研究组中")
    
    # 检查组员数量限制
    member_count = await db.scalar(
        select(func.count(GroupMember.id)).where(GroupMember.group_id == group_id)
    )
    if member_count >= group.max_members:
        raise HTTPException(status_code=400, detail="研究组已达到人数上限")
    
    # 添加成员
    member = GroupMember(
        group_id=group_id,
        user_id=student_id
    )
    db.add(member)
    await db.commit()
    
    return {"message": "成员已添加"}


@router.delete("/groups/{group_id}/members/{user_id}")
async def remove_group_member(
    group_id: int,
    user_id: int,
    current_user: User = Depends(get_mentor_user),
    db: AsyncSession = Depends(get_db)
):
    """移除组成员"""
    # 验证研究组
    group_result = await db.execute(
        select(ResearchGroup).where(
            and_(
                ResearchGroup.id == group_id,
                ResearchGroup.mentor_id == current_user.id
            )
        )
    )
    if not group_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="研究组不存在")
    
    # 删除成员
    result = await db.execute(
        select(GroupMember).where(
            and_(
                GroupMember.group_id == group_id,
                GroupMember.user_id == user_id
            )
        )
    )
    member = result.scalar_one_or_none()
    
    if not member:
        raise HTTPException(status_code=404, detail="成员不存在")
    
    await db.delete(member)
    await db.commit()
    
    return {"message": "成员已移除"}
