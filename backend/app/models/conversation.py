"""
对话和消息模型
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON, Enum
from sqlalchemy.orm import relationship
import enum

from app.core.database import Base


class MessageRole(str, enum.Enum):
    """消息角色"""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageType(str, enum.Enum):
    """消息类型"""
    TEXT = "text"
    THOUGHT = "thought"  # ReAct 思考
    ACTION = "action"    # ReAct 动作
    OBSERVATION = "observation"  # ReAct 观察结果
    ERROR = "error"


class Conversation(Base):
    """对话表"""
    __tablename__ = "conversations"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(500), default="新对话")
    
    # LLM 配置
    llm_provider = Column(String(50), default="deepseek")
    llm_model = Column(String(100), nullable=True)
    
    # 元数据
    metadata_ = Column("metadata", JSON, default=dict)
    
    # 状态
    is_archived = Column(Integer, default=0)
    
    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    user = relationship("User", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at")
    
    def __repr__(self):
        return f"<Conversation {self.id}: {self.title[:30]}>"


class Message(Base):
    """消息表"""
    __tablename__ = "messages"
    
    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    
    # 消息内容
    role = Column(Enum(MessageRole), nullable=False)
    content = Column(Text, nullable=False)
    message_type = Column(Enum(MessageType), default=MessageType.TEXT)
    
    # ReAct 相关
    thought = Column(Text, nullable=True)  # 最终思考内容（摘要）
    react_steps = Column(JSON, nullable=True)  # 完整的ReAct推理步骤
    action = Column(String(200), nullable=True)  # 动作名称
    action_input = Column(JSON, nullable=True)  # 动作输入
    observation = Column(Text, nullable=True)  # 观察结果
    
    # 元数据
    metadata_ = Column("metadata", JSON, default=dict)
    
    # Token 使用情况
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    
    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    conversation = relationship("Conversation", back_populates="messages")
    
    def __repr__(self):
        return f"<Message {self.id}: {self.role.value}>"
