"""
权限控制模块
"""
from functools import wraps
from typing import Callable, List, Union
from fastapi import HTTPException, status, Depends
from app.models.user import User
from app.models.role import UserRole
from app.core.security import get_current_user


class PermissionDenied(HTTPException):
    """权限不足异常"""
    def __init__(self, detail: str = "权限不足"):
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def require_role(*roles: UserRole):
    """
    角色权限依赖注入器
    
    用法:
        @router.get("/admin-only")
        async def admin_endpoint(user: User = Depends(require_role(UserRole.ADMIN))):
            pass
    """
    async def role_checker(current_user: User = Depends(get_current_user)) -> User:
        role_values = [r.value for r in roles]
        if current_user.role not in role_values:
            role_names = ", ".join(role_values)
            raise PermissionDenied(f"需要以下角色之一: {role_names}")
        return current_user
    return role_checker


def require_admin():
    """需要管理员权限"""
    return require_role(UserRole.ADMIN)


def require_mentor():
    """需要导师权限（管理员也可以）"""
    return require_role(UserRole.ADMIN, UserRole.MENTOR)


def require_mentor_only():
    """仅需要导师权限（管理员不可代替）"""
    return require_role(UserRole.MENTOR)


def require_student():
    """需要学生权限"""
    return require_role(UserRole.STUDENT)


def require_mentor_or_student():
    """需要导师或学生权限"""
    return require_role(UserRole.MENTOR, UserRole.STUDENT)


async def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    """获取管理员用户（依赖注入）"""
    if current_user.role != UserRole.ADMIN.value:
        raise PermissionDenied("需要管理员权限")
    return current_user


async def get_mentor_user(current_user: User = Depends(get_current_user)) -> User:
    """获取导师用户（依赖注入），管理员也可以"""
    if current_user.role not in [UserRole.ADMIN.value, UserRole.MENTOR.value]:
        raise PermissionDenied("需要导师权限")
    return current_user


async def get_student_user(current_user: User = Depends(get_current_user)) -> User:
    """获取学生用户（依赖注入）"""
    if current_user.role != UserRole.STUDENT.value:
        raise PermissionDenied("需要学生权限")
    return current_user


def check_mentor_student_relation(mentor: User, student: User) -> bool:
    """检查导师-学生关系是否有效"""
    return student.mentor_id == mentor.id


def check_resource_access(user: User, owner_id: int, shared_with_ids: List[int] = None) -> bool:
    """
    检查用户是否有权限访问资源
    
    Args:
        user: 当前用户
        owner_id: 资源所有者 ID
        shared_with_ids: 被共享的用户 ID 列表
    
    Returns:
        bool: 是否有权限
    """
    # 管理员有所有权限
    if user.role == UserRole.ADMIN.value:
        return True
    
    # 所有者有权限
    if user.id == owner_id:
        return True
    
    # 检查是否在共享列表中
    if shared_with_ids and user.id in shared_with_ids:
        return True
    
    return False
