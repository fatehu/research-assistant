"""
角色系统相关的 Pydantic 模式
"""
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, EmailStr, Field, ConfigDict

from app.models.role import UserRole, InvitationStatus, ShareType, SharePermission


# ========== 用户相关 ==========

class UserRoleUpdate(BaseModel):
    """用户角色更新"""
    role: UserRole


class UserAdminUpdate(BaseModel):
    """管理员更新用户信息"""
    full_name: Optional[str] = None
    department: Optional[str] = None
    research_direction: Optional[str] = None
    is_active: Optional[bool] = None
    role: Optional[UserRole] = None


class AdminCreateUserRequest(BaseModel):
    """管理员创建用户请求"""
    email: EmailStr
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=6, max_length=100)
    full_name: Optional[str] = None
    role: Optional[str] = "student"  # admin, mentor, student
    department: Optional[str] = None
    research_direction: Optional[str] = None


class UserWithRole(BaseModel):
    """带角色的用户信息"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    email: str
    username: str
    full_name: Optional[str] = None
    avatar: Optional[str] = None
    bio: Optional[str] = None
    role: UserRole
    department: Optional[str] = None
    research_direction: Optional[str] = None
    mentor_id: Optional[int] = None
    is_active: bool
    created_at: datetime
    last_login: Optional[datetime] = None


class UserListResponse(UserWithRole):
    """用户列表响应"""
    pass


class SystemStatistics(BaseModel):
    """系统统计"""
    total_users: int
    admin_count: int
    mentor_count: int
    student_count: int
    active_users: int
    total_conversations: int = 0
    total_knowledge_bases: int = 0
    total_papers: int = 0
    total_notebooks: int = 0


# ========== 研究组相关 ==========

class GroupCreate(BaseModel):
    """创建研究组"""
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    max_members: int = Field(default=20, ge=1, le=100)


class GroupUpdate(BaseModel):
    """更新研究组"""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    avatar: Optional[str] = None
    is_active: Optional[bool] = None
    max_members: Optional[int] = Field(None, ge=1, le=100)


class AddGroupMemberRequest(BaseModel):
    """添加组成员请求"""
    user_id: int


class GroupMemberResponse(BaseModel):
    """组成员响应"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    user_id: int
    username: str
    full_name: Optional[str] = None
    avatar: Optional[str] = None
    role: str  # member or admin
    joined_at: datetime


class GroupResponse(BaseModel):
    """研究组响应"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    name: str
    description: Optional[str] = None
    mentor_id: int
    avatar: Optional[str] = None
    is_active: bool
    max_members: int
    member_count: int = 0
    created_at: datetime
    updated_at: datetime


class GroupDetailResponse(GroupResponse):
    """研究组详情响应"""
    members: List[GroupMemberResponse] = []


# ========== 邀请相关 ==========

class InvitationUserInfo(BaseModel):
    """邀请中的用户信息"""
    id: int
    username: str
    full_name: Optional[str] = None
    email: str
    avatar: Optional[str] = None


class InviteStudentRequest(BaseModel):
    """邀请学生请求"""
    email: EmailStr
    message: Optional[str] = None
    group_id: Optional[int] = None


class ApplyToMentorRequest(BaseModel):
    """申请加入导师组"""
    mentor_id: int
    message: Optional[str] = None


class InvitationResponse(BaseModel):
    """邀请响应"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    type: str  # 'invite' or 'apply'
    from_user_id: int
    from_user_name: str
    from_user: Optional[InvitationUserInfo] = None
    to_user_id: int
    to_user_name: str
    to_user: Optional[InvitationUserInfo] = None
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    message: Optional[str] = None
    status: InvitationStatus
    created_at: datetime
    expires_at: Optional[datetime] = None
    responded_at: Optional[datetime] = None


# ========== 导师/学生相关 ==========

class MentorResponse(BaseModel):
    """导师信息响应"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    username: str
    full_name: Optional[str] = None
    email: str
    avatar: Optional[str] = None
    bio: Optional[str] = None
    department: Optional[str] = None
    research_direction: Optional[str] = None
    student_count: int = 0


class StudentResponse(BaseModel):
    """学生信息响应"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    username: str
    full_name: Optional[str] = None
    email: str
    avatar: Optional[str] = None
    department: Optional[str] = None
    research_direction: Optional[str] = None
    joined_at: Optional[datetime] = None
    last_login: Optional[datetime] = None


class StudentProgressResponse(BaseModel):
    """学生进度响应"""
    student_id: int
    username: str
    full_name: Optional[str] = None
    last_login: Optional[datetime] = None
    joined_at: Optional[datetime] = None
    conversation_count: int = 0
    knowledge_base_count: int = 0
    paper_count: int = 0
    notebook_count: int = 0


class PeerResponse(BaseModel):
    """同组同学响应"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    username: str
    full_name: Optional[str] = None
    avatar: Optional[str] = None
    research_direction: Optional[str] = None


# ========== 资源共享相关 ==========

class ShareResourceRequest(BaseModel):
    """共享资源请求"""
    resource_type: ShareType
    resource_id: int
    shared_with_type: str = Field(..., pattern="^(user|group|all_students)$")
    shared_with_id: Optional[int] = None  # user_id 或 group_id
    permission: SharePermission = SharePermission.READ
    expires_at: Optional[datetime] = None


class SharedResourceResponse(BaseModel):
    """共享资源响应"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    resource_type: ShareType
    resource_id: int
    resource_name: str = ""
    owner_id: int
    owner_name: str = ""
    shared_with_type: str
    shared_with_id: Optional[int] = None
    shared_with_name: Optional[str] = None
    permission: SharePermission
    created_at: datetime
    expires_at: Optional[datetime] = None


# ========== 公告相关 ==========

class AnnouncementCreate(BaseModel):
    """创建公告"""
    title: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., min_length=1)
    group_id: Optional[int] = None
    is_pinned: bool = False


class AnnouncementUpdate(BaseModel):
    """更新公告"""
    title: Optional[str] = Field(None, min_length=1, max_length=500)
    content: Optional[str] = Field(None, min_length=1)
    is_pinned: Optional[bool] = None
    is_active: Optional[bool] = None


class AnnouncementResponse(BaseModel):
    """公告响应"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    mentor_id: int
    mentor_name: str = ""
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    title: str
    content: str
    is_pinned: bool
    is_active: bool
    is_read: bool = False
    created_at: datetime
    updated_at: datetime
