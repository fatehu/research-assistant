"""
用户相关的 Pydantic 模式
"""
from datetime import datetime
from typing import Optional
import re
from pydantic import BaseModel, EmailStr, Field, ConfigDict, field_validator

from app.models.role import UserRole

_PASSWORD_MIN_LENGTH = 10
_PASSWORD_COMPLEXITY_REGEX = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z0-9]).+$")
_COMMON_WEAK_PASSWORDS = {
    "123456",
    "12345678",
    "123456789",
    "1234567890",
    "password",
    "password123",
    "qwerty",
    "qwerty123",
    "admin123",
    "abc123",
}


def _validate_password_strength(value: str) -> str:
    if len(value) < _PASSWORD_MIN_LENGTH:
        raise ValueError(f"密码长度至少 {_PASSWORD_MIN_LENGTH} 位")
    lowered = value.lower()
    if lowered in _COMMON_WEAK_PASSWORDS:
        raise ValueError("密码过于简单，请勿使用常见弱口令")
    if not _PASSWORD_COMPLEXITY_REGEX.match(value):
        raise ValueError("密码必须同时包含大小写字母、数字和特殊字符")
    return value


class UserBase(BaseModel):
    """用户基础模式"""
    email: EmailStr
    username: str = Field(..., min_length=3, max_length=100)


class UserCreate(UserBase):
    """用户创建模式"""
    password: str = Field(..., min_length=_PASSWORD_MIN_LENGTH, max_length=100)
    full_name: Optional[str] = None

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, value: str) -> str:
        return _validate_password_strength(value)


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


class UserPasswordChange(BaseModel):
    """用户修改密码请求"""
    old_password: str = Field(..., min_length=6, max_length=100)
    new_password: str = Field(..., min_length=_PASSWORD_MIN_LENGTH, max_length=100)

    @field_validator("new_password")
    @classmethod
    def validate_new_password_strength(cls, value: str) -> str:
        return _validate_password_strength(value)


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
