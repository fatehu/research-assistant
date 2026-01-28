"""
管理员路由
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from loguru import logger

from app.core.database import get_db
from app.core.security import get_current_user, get_password_hash
from app.core.permissions import get_admin_user
from app.models.user import User
from app.models.role import UserRole
from app.models.conversation import Conversation
from app.models.knowledge import KnowledgeBase
from app.models.literature import Paper
from app.models.notebook import Notebook
from app.schemas.role import (
    UserListResponse, UserAdminUpdate, UserRoleUpdate, SystemStatistics,
    AdminCreateUserRequest
)

router = APIRouter()


@router.get("/users", response_model=list[UserListResponse])
async def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    role: UserRole = None,
    search: str = None,
    is_active: bool = None,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """获取用户列表（管理员）"""
    query = select(User)
    
    # 筛选条件
    if role:
        query = query.where(User.role == role)
    if is_active is not None:
        query = query.where(User.is_active == is_active)
    if search:
        search_pattern = f"%{search}%"
        query = query.where(
            or_(
                User.username.ilike(search_pattern),
                User.email.ilike(search_pattern),
                User.full_name.ilike(search_pattern)
            )
        )
    
    query = query.order_by(User.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    users = result.scalars().all()
    
    return [UserListResponse.model_validate(u) for u in users]


@router.post("/users", response_model=UserListResponse)
async def create_user(
    data: AdminCreateUserRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """管理员创建用户"""
    # 检查邮箱是否已存在
    result = await db.execute(
        select(User).where(User.email == data.email)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="该邮箱已被注册")
    
    # 检查用户名是否已存在
    result = await db.execute(
        select(User).where(User.username == data.username)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="该用户名已被使用")
    
    # 创建用户
    new_user = User(
        email=data.email,
        username=data.username,
        hashed_password=get_password_hash(data.password),
        full_name=data.full_name,
        role=data.role or "student",
        department=data.department,
        research_direction=data.research_direction,
        is_active=True
    )
    
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    
    logger.info(f"管理员 {current_user.username} 创建了用户 {new_user.username} (角色: {new_user.role})")
    
    return UserListResponse.model_validate(new_user)


@router.get("/users/count")
async def get_user_count(
    role: UserRole = None,
    is_active: bool = None,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """获取用户数量"""
    query = select(func.count(User.id))
    
    if role:
        query = query.where(User.role == role)
    if is_active is not None:
        query = query.where(User.is_active == is_active)
    
    count = await db.scalar(query)
    return {"count": count}


@router.get("/users/{user_id}", response_model=UserListResponse)
async def get_user(
    user_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """获取用户详情（管理员）"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    return UserListResponse.model_validate(user)


@router.put("/users/{user_id}", response_model=UserListResponse)
async def update_user(
    user_id: int,
    data: UserAdminUpdate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """更新用户信息（管理员）"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    # 更新字段
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(user, field, value)
    
    await db.commit()
    await db.refresh(user)
    
    logger.info(f"管理员 {current_user.username} 更新了用户 {user.username} 的信息")
    
    return UserListResponse.model_validate(user)


@router.put("/users/{user_id}/role")
async def update_user_role(
    user_id: int,
    data: UserRoleUpdate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """修改用户角色（管理员）"""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="不能修改自己的角色")
    
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    old_role = user.role
    new_role = data.role
    
    # 如果从学生变为导师，需要解除与原导师的关系
    if old_role == UserRole.STUDENT.value and new_role == UserRole.MENTOR.value:
        user.mentor_id = None
        user.joined_at = None
    
    # 如果从导师变为学生，需要处理其名下学生
    if old_role == UserRole.MENTOR.value and new_role == UserRole.STUDENT.value:
        # 将其名下学生的 mentor_id 设为 NULL
        await db.execute(
            select(User).where(User.mentor_id == user_id)
        )
        students = (await db.execute(
            select(User).where(User.mentor_id == user_id)
        )).scalars().all()
        
        for student in students:
            student.mentor_id = None
            student.joined_at = None
        
        logger.info(f"导师 {user.username} 角色变更，{len(students)} 名学生已解除关联")
    
    user.role = new_role
    await db.commit()
    
    logger.info(f"管理员 {current_user.username} 将用户 {user.username} 的角色从 {old_role} 修改为 {new_role}")
    
    return {"message": f"用户角色已更新为 {new_role}"}


@router.put("/users/{user_id}")
async def update_user_info(
    user_id: int,
    data: UserAdminUpdate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """更新用户信息（管理员）"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    # 更新字段
    if data.full_name is not None:
        user.full_name = data.full_name
    if data.department is not None:
        user.department = data.department
    if data.research_direction is not None:
        user.research_direction = data.research_direction
    if data.is_active is not None:
        if user_id == current_user.id and not data.is_active:
            raise HTTPException(status_code=400, detail="不能禁用自己")
        user.is_active = data.is_active
    if data.role is not None:
        # 处理角色变更逻辑
        old_role = user.role
        new_role = data.role.value if hasattr(data.role, 'value') else data.role
        
        if old_role == UserRole.MENTOR.value and new_role == UserRole.STUDENT.value:
            # 从导师变为学生，解除与学生的关联
            students = (await db.execute(
                select(User).where(User.mentor_id == user_id)
            )).scalars().all()
            for student in students:
                student.mentor_id = None
                student.joined_at = None
        
        user.role = new_role
    
    await db.commit()
    await db.refresh(user)
    
    logger.info(f"管理员 {current_user.username} 更新了用户 {user.username} 的信息")
    
    return UserListResponse.model_validate(user)


@router.put("/users/{user_id}/toggle-active")
async def toggle_user_active(
    user_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """切换用户状态（管理员）"""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="不能禁用自己")
    
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    user.is_active = not user.is_active
    await db.commit()
    
    action = "启用" if user.is_active else "禁用"
    logger.info(f"管理员 {current_user.username} {action}了用户 {user.username}")
    
    return {"message": f"用户已{action}", "is_active": user.is_active}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """删除用户（管理员）"""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    username = user.username
    await db.delete(user)
    await db.commit()
    
    logger.info(f"管理员 {current_user.username} 删除了用户 {username}")
    
    return {"message": "用户已删除"}


@router.get("/statistics", response_model=SystemStatistics)
async def get_statistics(
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """获取系统统计（管理员）"""
    # 用户统计
    total_users = await db.scalar(select(func.count(User.id)))
    admin_count = await db.scalar(
        select(func.count(User.id)).where(User.role == UserRole.ADMIN.value)
    )
    mentor_count = await db.scalar(
        select(func.count(User.id)).where(User.role == UserRole.MENTOR.value)
    )
    student_count = await db.scalar(
        select(func.count(User.id)).where(User.role == UserRole.STUDENT.value)
    )
    active_users = await db.scalar(
        select(func.count(User.id)).where(User.is_active == True)
    )
    
    # 资源统计
    total_conversations = await db.scalar(select(func.count(Conversation.id)))
    total_knowledge_bases = await db.scalar(select(func.count(KnowledgeBase.id)))
    total_papers = await db.scalar(select(func.count(Paper.id)))
    total_notebooks = await db.scalar(select(func.count(Notebook.id)))
    
    return SystemStatistics(
        total_users=total_users or 0,
        admin_count=admin_count or 0,
        mentor_count=mentor_count or 0,
        student_count=student_count or 0,
        active_users=active_users or 0,
        total_conversations=total_conversations or 0,
        total_knowledge_bases=total_knowledge_bases or 0,
        total_papers=total_papers or 0,
        total_notebooks=total_notebooks or 0,
    )


@router.get("/mentors")
async def list_mentors(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """获取所有导师列表"""
    result = await db.execute(
        select(User)
        .where(User.role == UserRole.MENTOR.value)
        .order_by(User.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    mentors = result.scalars().all()
    
    # 统计每个导师的学生数量
    mentor_list = []
    for mentor in mentors:
        student_count = await db.scalar(
            select(func.count(User.id)).where(User.mentor_id == mentor.id)
        )
        mentor_data = UserListResponse.model_validate(mentor).model_dump()
        mentor_data["student_count"] = student_count or 0
        mentor_list.append(mentor_data)
    
    return mentor_list
