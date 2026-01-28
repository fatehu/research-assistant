"""
邀请相关路由
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
    UserRole, ResearchGroup, GroupMember, Invitation, InvitationStatus
)
from app.schemas.role import InvitationResponse

router = APIRouter()


@router.get("", response_model=list[InvitationResponse])
async def get_my_invitations(
    status: InvitationStatus = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取我的所有邀请（收到的和发出的）"""
    query = select(Invitation).where(
        or_(
            Invitation.to_user_id == current_user.id,
            Invitation.from_user_id == current_user.id
        )
    )
    
    if status:
        query = query.where(Invitation.status == status)
    
    query = query.order_by(Invitation.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    invitations = result.scalars().all()
    
    return await _build_invitation_responses(invitations, db)


@router.get("/received", response_model=list[InvitationResponse])
async def get_received_invitations(
    status: InvitationStatus = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取收到的邀请/申请"""
    query = select(Invitation).where(Invitation.to_user_id == current_user.id)
    
    if status:
        query = query.where(Invitation.status == status)
    
    query = query.order_by(Invitation.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    invitations = result.scalars().all()
    
    return await _build_invitation_responses(invitations, db)


@router.get("/sent", response_model=list[InvitationResponse])
async def get_sent_invitations(
    status: InvitationStatus = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取发出的邀请/申请"""
    query = select(Invitation).where(Invitation.from_user_id == current_user.id)
    
    if status:
        query = query.where(Invitation.status == status)
    
    query = query.order_by(Invitation.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    invitations = result.scalars().all()
    
    return await _build_invitation_responses(invitations, db)


@router.get("/pending-count")
async def get_pending_count(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取待处理的邀请/申请数量"""
    from sqlalchemy import func
    
    # 收到的待处理
    received_count = await db.scalar(
        select(func.count(Invitation.id)).where(
            and_(
                Invitation.to_user_id == current_user.id,
                Invitation.status == InvitationStatus.PENDING,
                or_(
                    Invitation.expires_at == None,
                    Invitation.expires_at > datetime.utcnow()
                )
            )
        )
    )
    
    # 发出的待处理
    sent_count = await db.scalar(
        select(func.count(Invitation.id)).where(
            and_(
                Invitation.from_user_id == current_user.id,
                Invitation.status == InvitationStatus.PENDING,
                or_(
                    Invitation.expires_at == None,
                    Invitation.expires_at > datetime.utcnow()
                )
            )
        )
    )
    
    return {
        "received": received_count or 0,
        "sent": sent_count or 0,
        "total": (received_count or 0) + (sent_count or 0)
    }


@router.post("/{invitation_id}/accept")
async def accept_invitation(
    invitation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """接受邀请/申请"""
    result = await db.execute(
        select(Invitation).where(
            and_(
                Invitation.id == invitation_id,
                Invitation.to_user_id == current_user.id,
                Invitation.status == InvitationStatus.PENDING
            )
        )
    )
    invitation = result.scalar_one_or_none()
    
    if not invitation:
        raise HTTPException(status_code=404, detail="邀请不存在或已处理")
    
    # 检查是否过期
    if invitation.expires_at and invitation.expires_at < datetime.utcnow():
        invitation.status = InvitationStatus.CANCELLED
        await db.commit()
        raise HTTPException(status_code=400, detail="邀请已过期")
    
    # 获取发起者
    from_user_result = await db.execute(
        select(User).where(User.id == invitation.from_user_id)
    )
    from_user = from_user_result.scalar_one_or_none()
    
    if invitation.type == 'invite':
        # 导师邀请学生
        if current_user.mentor_id:
            raise HTTPException(status_code=400, detail="您已有导师，不能接受此邀请")
        
        current_user.mentor_id = invitation.from_user_id
        current_user.joined_at = datetime.utcnow()
        
        # 如果有研究组，加入研究组
        if invitation.group_id:
            member = GroupMember(
                group_id=invitation.group_id,
                user_id=current_user.id
            )
            db.add(member)
        
        logger.info(f"学生 {current_user.username} 接受了导师 {from_user.username} 的邀请")
        
    elif invitation.type == 'apply':
        # 学生申请加入导师组
        # 验证当前用户是导师
        if current_user.role != UserRole.MENTOR.value:
            raise HTTPException(status_code=403, detail="只有导师可以接受申请")
        
        # 设置学生的导师
        from_user.mentor_id = current_user.id
        from_user.joined_at = datetime.utcnow()
        
        logger.info(f"导师 {current_user.username} 接受了学生 {from_user.username} 的申请")
    
    invitation.status = InvitationStatus.ACCEPTED
    invitation.responded_at = datetime.utcnow()
    await db.commit()
    
    return {"message": "已接受"}


@router.post("/{invitation_id}/reject")
async def reject_invitation(
    invitation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """拒绝邀请/申请"""
    result = await db.execute(
        select(Invitation).where(
            and_(
                Invitation.id == invitation_id,
                Invitation.to_user_id == current_user.id,
                Invitation.status == InvitationStatus.PENDING
            )
        )
    )
    invitation = result.scalar_one_or_none()
    
    if not invitation:
        raise HTTPException(status_code=404, detail="邀请不存在或已处理")
    
    invitation.status = InvitationStatus.REJECTED
    invitation.responded_at = datetime.utcnow()
    await db.commit()
    
    logger.info(f"用户 {current_user.username} 拒绝了邀请 #{invitation_id}")
    
    return {"message": "已拒绝"}


@router.post("/{invitation_id}/cancel")
async def cancel_invitation(
    invitation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """取消自己发出的邀请/申请"""
    result = await db.execute(
        select(Invitation).where(
            and_(
                Invitation.id == invitation_id,
                Invitation.from_user_id == current_user.id,
                Invitation.status == InvitationStatus.PENDING
            )
        )
    )
    invitation = result.scalar_one_or_none()
    
    if not invitation:
        raise HTTPException(status_code=404, detail="邀请不存在或已处理")
    
    invitation.status = InvitationStatus.CANCELLED
    invitation.responded_at = datetime.utcnow()
    await db.commit()
    
    logger.info(f"用户 {current_user.username} 取消了邀请 #{invitation_id}")
    
    return {"message": "已取消"}


async def _build_invitation_responses(
    invitations: list[Invitation],
    db: AsyncSession
) -> list[InvitationResponse]:
    """构建邀请响应列表"""
    from app.schemas.role import InvitationUserInfo
    
    responses = []
    
    for inv in invitations:
        # 获取发起者完整信息
        from_user_result = await db.execute(
            select(User).where(User.id == inv.from_user_id)
        )
        from_user = from_user_result.scalar_one_or_none()
        from_user_info = None
        from_user_name = ""
        if from_user:
            from_user_name = from_user.full_name or from_user.username
            from_user_info = InvitationUserInfo(
                id=from_user.id,
                username=from_user.username,
                full_name=from_user.full_name,
                email=from_user.email,
                avatar=from_user.avatar
            )
        
        # 获取接收者完整信息
        to_user_result = await db.execute(
            select(User).where(User.id == inv.to_user_id)
        )
        to_user = to_user_result.scalar_one_or_none()
        to_user_info = None
        to_user_name = ""
        if to_user:
            to_user_name = to_user.full_name or to_user.username
            to_user_info = InvitationUserInfo(
                id=to_user.id,
                username=to_user.username,
                full_name=to_user.full_name,
                email=to_user.email,
                avatar=to_user.avatar
            )
        
        # 获取研究组信息
        group_name = None
        if inv.group_id:
            group_result = await db.execute(
                select(ResearchGroup.name).where(ResearchGroup.id == inv.group_id)
            )
            group_row = group_result.first()
            group_name = group_row[0] if group_row else None
        
        responses.append(InvitationResponse(
            id=inv.id,
            type=inv.type,
            from_user_id=inv.from_user_id,
            from_user_name=from_user_name,
            from_user=from_user_info,
            to_user_id=inv.to_user_id,
            to_user_name=to_user_name,
            to_user=to_user_info,
            group_id=inv.group_id,
            group_name=group_name,
            message=inv.message,
            status=inv.status,
            created_at=inv.created_at,
            expires_at=inv.expires_at,
            responded_at=inv.responded_at
        ))
    
    return responses
