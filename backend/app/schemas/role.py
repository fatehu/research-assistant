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


class UserPasswordUpdate(BaseModel):
    """管理员修改用户密码"""
    password: str = Field(..., min_length=6, max_length=100)


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


class DailyCountPoint(BaseModel):
    """按日计数点"""
    date: str
    count: int


class StatisticsActivity(BaseModel):
    """近 7 天活跃概览"""
    new_users_last_7_days: int = 0
    new_conversations_last_7_days: int = 0
    new_knowledge_bases_last_7_days: int = 0
    new_papers_last_7_days: int = 0
    new_notebooks_last_7_days: int = 0


class StatisticsCollaboration(BaseModel):
    """协作统计"""
    total_groups: int = 0
    active_groups: int = 0
    total_group_members: int = 0
    pending_invitations: int = 0
    total_shared_resources: int = 0
    total_announcements: int = 0
    active_announcements: int = 0


class StatisticsMentorship(BaseModel):
    """导师制统计"""
    students_with_mentor: int = 0
    students_without_mentor: int = 0


class StatisticsDocumentPipeline(BaseModel):
    """文档处理管线统计"""
    total_documents: int = 0
    completed_documents: int = 0
    running_documents: int = 0
    failed_documents: int = 0
    pending_documents: int = 0
    timeout_documents: int = 0
    cancelled_documents: int = 0


class StatisticsTrendSeries(BaseModel):
    """近 7 天趋势序列"""
    users: List[DailyCountPoint] = Field(default_factory=list)
    conversations: List[DailyCountPoint] = Field(default_factory=list)
    knowledge_bases: List[DailyCountPoint] = Field(default_factory=list)
    papers: List[DailyCountPoint] = Field(default_factory=list)
    notebooks: List[DailyCountPoint] = Field(default_factory=list)


class StatisticsBreakdownItem(BaseModel):
    """分类统计项"""
    key: str
    label: str
    count: int = 0


class StatisticsMentorRankItem(BaseModel):
    """导师排行项"""
    mentor_id: int
    username: str
    full_name: Optional[str] = None
    student_count: int = 0
    group_count: int = 0


class StatisticsRecentActivityItem(BaseModel):
    """近期活动项"""
    id: str
    type: str
    title: str
    owner_name: str
    owner_role: str
    created_at: datetime


class StatisticsDetailItem(BaseModel):
    """统计页下钻明细项"""
    id: str
    entity: str
    title: str
    subtitle: Optional[str] = None
    status: Optional[str] = None
    category: Optional[str] = None
    owner_name: Optional[str] = None
    owner_role: Optional[str] = None
    target_name: Optional[str] = None
    permission: Optional[str] = None
    member_count: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class StatisticsDetailResponse(BaseModel):
    """统计页下钻明细分页响应"""
    entity: str
    total: int
    page: int
    page_size: int
    items: List[StatisticsDetailItem] = Field(default_factory=list)


class StatisticsAIRag(BaseModel):
    """AI / RAG 统计"""
    assistant_messages_last_window: int = 0
    rag_messages_last_window: int = 0
    knowledge_search_calls_last_window: int = 0
    citation_required_answers_last_window: int = 0
    citation_valid_answers_last_window: int = 0
    citation_repair_attempts_last_window: int = 0
    citation_repair_successes_last_window: int = 0
    compression_calls_last_window: int = 0
    compression_fallback_chunks_last_window: int = 0
    assistant_total_tokens_last_window: int = 0
    agent_runs_last_window: int = 0
    successful_agent_runs_last_window: int = 0


class StatisticsCodeLab(BaseModel):
    """CodeLab 统计"""
    notebooks_active_last_window: int = 0
    executed_notebooks: int = 0
    total_execution_count: int = 0
    code_cells: int = 0
    executed_code_cells: int = 0
    agent_runs_last_window: int = 0
    agent_tokens_last_window: int = 0


class StatisticsLiterature(BaseModel):
    """文献阅读统计"""
    total_collections: int = 0
    active_read_sessions_last_window: int = 0
    annotations_last_window: int = 0
    comments_last_window: int = 0
    ratings_last_window: int = 0
    qa_sessions_last_window: int = 0
    qa_messages_last_window: int = 0
    knowledge_links_total: int = 0
    knowledge_link_breakdown: List[StatisticsBreakdownItem] = Field(default_factory=list)


class AdminAuditLogItem(BaseModel):
    """管理员审计日志项"""
    id: int
    action: str
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    admin_name: str
    summary: str
    created_at: datetime


class AdminAuditLogResponse(BaseModel):
    """管理员审计日志分页响应"""
    total: int
    page: int
    page_size: int
    items: List[AdminAuditLogItem] = Field(default_factory=list)


class SystemStatistics(BaseModel):
    """系统统计"""
    time_window_days: int = 7
    total_users: int
    admin_count: int
    mentor_count: int
    student_count: int
    active_users: int
    inactive_users: int = 0
    total_conversations: int = 0
    total_knowledge_bases: int = 0
    total_documents: int = 0
    total_papers: int = 0
    total_notebooks: int = 0
    total_groups: int = 0
    pending_invitations: int = 0
    total_shared_resources: int = 0
    total_announcements: int = 0
    students_with_mentor: int = 0
    students_without_mentor: int = 0
    activity: StatisticsActivity = Field(default_factory=StatisticsActivity)
    collaboration: StatisticsCollaboration = Field(default_factory=StatisticsCollaboration)
    mentorship: StatisticsMentorship = Field(default_factory=StatisticsMentorship)
    document_pipeline: StatisticsDocumentPipeline = Field(default_factory=StatisticsDocumentPipeline)
    trends_7d: StatisticsTrendSeries = Field(default_factory=StatisticsTrendSeries)
    share_breakdown: List[StatisticsBreakdownItem] = Field(default_factory=list)
    invitation_breakdown: List[StatisticsBreakdownItem] = Field(default_factory=list)
    top_mentors: List[StatisticsMentorRankItem] = Field(default_factory=list)
    recent_activity: List[StatisticsRecentActivityItem] = Field(default_factory=list)
    ai_rag: StatisticsAIRag = Field(default_factory=StatisticsAIRag)
    codelab: StatisticsCodeLab = Field(default_factory=StatisticsCodeLab)
    literature: StatisticsLiterature = Field(default_factory=StatisticsLiterature)


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
    conversation_count: int = 0
    knowledge_base_count: int = 0
    paper_count: int = 0
    notebook_count: int = 0


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
