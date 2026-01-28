"""
用户相关的 Pydantic 模式
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, Field, ConfigDict

from app.models.role import UserRole


class UserBase(BaseModel):
    """用户基础模式"""
    email: EmailStr
    username: str = Field(..., min_length=3, max_length=100)


class UserCreate(UserBase):
    """用户创建模式"""
    password: str = Field(..., min_length=6, max_length=100)
    full_name: Optional[str] = None


class UserLogin(BaseModel):
    """用户登录模式"""
    email: EmailStr
    password: str


class UserUpdate(BaseModel):
    """用户更新模式"""
    full_name: Optional[str] = None
    bio: Optional[str] = None
    avatar: Optional[str] = None
    preferred_llm_provider: Optional[str] = None
    preferences: Optional[dict] = None
    department: Optional[str] = None
    research_direction: Optional[str] = None


class UserResponse(BaseModel):
    """用户响应模式"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    email: str
    username: str
    full_name: Optional[str] = None
    avatar: Optional[str] = None
    bio: Optional[str] = None
    is_active: bool
    preferred_llm_provider: str
    preferences: dict
    created_at: datetime
    last_login: Optional[datetime] = None
    # 角色相关
    role: UserRole = UserRole.STUDENT
    mentor_id: Optional[int] = None
    department: Optional[str] = None
    research_direction: Optional[str] = None
    joined_at: Optional[datetime] = None


class Token(BaseModel):
    """Token 响应模式"""
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class TokenData(BaseModel):
    """Token 数据模式"""
    user_id: Optional[int] = None
