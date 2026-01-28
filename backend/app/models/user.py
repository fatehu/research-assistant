"""
用户模型 - 多角色系统扩展版
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, JSON, ForeignKey
from sqlalchemy.orm import relationship

from app.core.database import Base
from app.models.role import UserRole


class User(Base):
    """用户表"""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    username = Column(String(100), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(200), nullable=True)
    avatar = Column(String(500), nullable=True)
    bio = Column(Text, nullable=True)
    
    # 状态
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    
    # 偏好设置
    preferences = Column(JSON, default=dict)
    
    # LLM 偏好
    preferred_llm_provider = Column(String(50), default="deepseek")
    
    # === 角色系统扩展字段 ===
    role = Column(String(20), default="student", nullable=False)  # admin, mentor, student
    mentor_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    department = Column(String(200), nullable=True)  # 院系/部门
    research_direction = Column(String(500), nullable=True)  # 研究方向
    joined_at = Column(DateTime, nullable=True)  # 加入导师时间
    
    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)
    
    # 原有关系
    conversations = relationship("Conversation", back_populates="user", cascade="all, delete-orphan")
    knowledge_bases = relationship("KnowledgeBase", back_populates="user", cascade="all, delete-orphan")
    papers = relationship("Paper", back_populates="user", cascade="all, delete-orphan")
    paper_collections = relationship("PaperCollection", back_populates="user", cascade="all, delete-orphan")
    notebooks = relationship("Notebook", back_populates="user", cascade="all, delete-orphan")
    
    # === 角色系统扩展关系 ===
    # 导师-学生关系（自引用）
    mentor = relationship("User", remote_side=[id], backref="students", foreign_keys=[mentor_id])
    
    # 研究组关系
    owned_groups = relationship("ResearchGroup", back_populates="mentor", foreign_keys="ResearchGroup.mentor_id")
    group_memberships = relationship("GroupMember", back_populates="user", cascade="all, delete-orphan")
    
    # 邀请关系
    sent_invitations = relationship("Invitation", back_populates="from_user", foreign_keys="Invitation.from_user_id", cascade="all, delete-orphan")
    received_invitations = relationship("Invitation", back_populates="to_user", foreign_keys="Invitation.to_user_id", cascade="all, delete-orphan")
    
    # 共享资源
    shared_resources = relationship("SharedResource", back_populates="owner", cascade="all, delete-orphan")
    
    # 公告关系
    announcements = relationship("Announcement", back_populates="mentor", cascade="all, delete-orphan")
    announcement_reads = relationship("AnnouncementRead", back_populates="user", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<User {self.username} role={self.role}>"
    
    @property
    def is_admin(self) -> bool:
        """是否是管理员"""
        return self.role == "admin"
    
    @property
    def is_mentor(self) -> bool:
        """是否是导师"""
        return self.role == "mentor"
    
    @property
    def is_student(self) -> bool:
        """是否是学生"""
        return self.role == "student"
    
    @property
    def has_mentor(self) -> bool:
        """是否有导师"""
        return self.mentor_id is not None
