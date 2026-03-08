"""
数据模型模块
"""
from app.models.user import User
from app.models.conversation import Conversation, Message
from app.models.knowledge import KnowledgeBase, Document, DocumentChunk
from app.models.literature import (
    Paper,
    PaperCollection,
    PaperSearchHistory,
    PaperEntity,
    PaperReadSession,
    PaperReaderPageCache,
    PaperReaderComponentOverlay,
    PaperAnnotation,
    PaperComment,
    PaperRating,
    PaperKnowledgeLink,
    LiteratureQASession,
    LiteratureQAMessage,
)
from app.models.notebook import Notebook, NotebookCell
from app.models.agent import AgentRun, AgentStepRecord, ConversationSummary, AgentMemoryItem

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
    AdminAuditLog,
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
    "PaperEntity",
    "PaperReadSession",
    "PaperReaderPageCache",
    "PaperReaderComponentOverlay",
    "PaperAnnotation",
    "PaperComment",
    "PaperRating",
    "PaperKnowledgeLink",
    "LiteratureQASession",
    "LiteratureQAMessage",
    # 笔记本
    "Notebook",
    "NotebookCell",
    # Agent runtime persistence
    "AgentRun",
    "AgentStepRecord",
    "ConversationSummary",
    "AgentMemoryItem",
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
    "AdminAuditLog",
]
