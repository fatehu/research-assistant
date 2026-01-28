"""
数据模型模块
"""
from app.models.user import User
from app.models.conversation import Conversation, Message
from app.models.knowledge import KnowledgeBase, Document, DocumentChunk
from app.models.literature import Paper, PaperCollection, PaperSearchHistory
from app.models.notebook import Notebook, NotebookCell

# 角色系统模型
from app.models.role import (
    UserRole,
    InvitationStatus,
    ShareType,
    SharePermission,
    ResearchGroup,
    GroupMember,
    Invitation,
    SharedResource,
    Announcement,
    AnnouncementRead,
)

__all__ = [
    # 用户
    "User",
    # 对话
    "Conversation",
    "Message",
    # 知识库
    "KnowledgeBase",
    "Document",
    "DocumentChunk",
    # 文献
    "Paper",
    "PaperCollection",
    "PaperSearchHistory",
    # 笔记本
    "Notebook",
    "NotebookCell",
    # 角色系统
    "UserRole",
    "InvitationStatus",
    "ShareType",
    "SharePermission",
    "ResearchGroup",
    "GroupMember",
    "Invitation",
    "SharedResource",
    "Announcement",
    "AnnouncementRead",
]
