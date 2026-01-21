"""
数据模型
"""
from app.models.user import User
from app.models.conversation import Conversation, Message
from app.models.knowledge import KnowledgeBase, Document, DocumentChunk
from app.models.literature import Paper, PaperCollection, PaperSearchHistory

__all__ = [
    "User", 
    "Conversation", 
    "Message",
    "KnowledgeBase",
    "Document",
    "DocumentChunk",
    "Paper",
    "PaperCollection",
    "PaperSearchHistory",
]
