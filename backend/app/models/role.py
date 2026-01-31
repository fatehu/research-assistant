"""
角色系统相关模型
"""
from datetime import datetime
from enum import Enum as PyEnum
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship

from app.core.database import Base


class UserRole(str, PyEnum):
    """用户角色枚举"""
    ADMIN = "admin"
    MENTOR = "mentor"
    STUDENT = "student"


class InvitationStatus(str, PyEnum):
    """邀请状态枚举"""
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ShareType(str, PyEnum):
    """共享类型枚举"""
    KNOWLEDGE_BASE = "knowledge_base"
    PAPER_COLLECTION = "paper_collection"
    PAPER = "paper"
    NOTEBOOK = "notebook"


class SharePermission(str, PyEnum):
    """共享权限枚举"""
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


class ResearchGroup(Base):
    """研究组表"""
    __tablename__ = "research_groups"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    mentor_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    avatar = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True)
    max_members = Column(Integer, default=20)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    mentor = relationship("User", back_populates="owned_groups", foreign_keys=[mentor_id])
    members = relationship("GroupMember", back_populates="group", cascade="all, delete-orphan")
    invitations = relationship("Invitation", back_populates="group", cascade="all, delete-orphan")
    announcements = relationship("Announcement", back_populates="group")
    
    def __repr__(self):
        return f"<ResearchGroup {self.name}>"


class GroupMember(Base):
    """组成员表"""
    __tablename__ = "group_members"
    
    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("research_groups.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(50), default="member")  # member, admin
    joined_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    group = relationship("ResearchGroup", back_populates="members")
    user = relationship("User", back_populates="group_memberships")
    
    def __repr__(self):
        return f"<GroupMember group={self.group_id} user={self.user_id}>"


class Invitation(Base):
    """邀请/申请表"""
    __tablename__ = "invitations"
    
    id = Column(Integer, primary_key=True, index=True)
    type = Column(String(20), nullable=False)  # 'invite' (导师邀请) 或 'apply' (学生申请)
    from_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    to_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    group_id = Column(Integer, ForeignKey("research_groups.id", ondelete="CASCADE"), nullable=True)
    message = Column(Text, nullable=True)
    status = Column(String(20), default="pending")  # pending, accepted, rejected, cancelled
    responded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    
    # 关系
    from_user = relationship("User", foreign_keys=[from_user_id], back_populates="sent_invitations")
    to_user = relationship("User", foreign_keys=[to_user_id], back_populates="received_invitations")
    group = relationship("ResearchGroup", back_populates="invitations")
    
    def __repr__(self):
        return f"<Invitation {self.type} from={self.from_user_id} to={self.to_user_id}>"


class SharedResource(Base):
    """资源共享表"""
    __tablename__ = "shared_resources"
    
    id = Column(Integer, primary_key=True, index=True)
    resource_type = Column(String(30), nullable=False)  # knowledge_base, paper_collection, notebook
    resource_id = Column(Integer, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    shared_with_type = Column(String(20), nullable=False)  # 'user', 'group', 'all_students'
    shared_with_id = Column(Integer, nullable=True)  # user_id 或 group_id
    permission = Column(String(20), default="read")  # read, write, admin
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    
    # 关系
    owner = relationship("User", back_populates="shared_resources")
    
    def __repr__(self):
        return f"<SharedResource {self.resource_type}:{self.resource_id}>"


class Announcement(Base):
    """公告表"""
    __tablename__ = "announcements"
    
    id = Column(Integer, primary_key=True, index=True)
    mentor_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    group_id = Column(Integer, ForeignKey("research_groups.id", ondelete="SET NULL"), nullable=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    is_pinned = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    mentor = relationship("User", back_populates="announcements")
    group = relationship("ResearchGroup", back_populates="announcements")
    reads = relationship("AnnouncementRead", back_populates="announcement", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Announcement {self.title[:30]}>"


class AnnouncementRead(Base):
    """公告已读记录表"""
    __tablename__ = "announcement_reads"
    
    id = Column(Integer, primary_key=True, index=True)
    announcement_id = Column(Integer, ForeignKey("announcements.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    read_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    announcement = relationship("Announcement", back_populates="reads")
    user = relationship("User", back_populates="announcement_reads")
    
    def __repr__(self):
        return f"<AnnouncementRead announcement={self.announcement_id} user={self.user_id}>"
