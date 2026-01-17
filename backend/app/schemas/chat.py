"""
聊天相关的 Pydantic 模式
"""
from datetime import datetime
from typing import Optional, List, Literal, Any
from pydantic import BaseModel, Field, ConfigDict


class ReActStep(BaseModel):
    """ReAct 步骤"""
    step_type: Literal["thought", "action", "observation", "answer"]
    content: str
    action_name: Optional[str] = None
    action_input: Optional[dict] = None


class MessageBase(BaseModel):
    """消息基础模式"""
    content: str
    role: Literal["user", "assistant", "system"] = "user"


class MessageCreate(MessageBase):
    """消息创建模式"""
    pass


class MessageResponse(BaseModel):
    """消息响应模式"""
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    
    id: int
    conversation_id: int
    role: str
    content: str
    message_type: str
    thought: Optional[str] = None
    react_steps: Optional[List[dict]] = None  # ReAct 推理步骤
    action: Optional[str] = None
    action_input: Optional[dict] = None
    observation: Optional[str] = None
    metadata: Optional[dict] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    created_at: datetime


class ConversationBase(BaseModel):
    """对话基础模式"""
    title: str = "新对话"


class ConversationCreate(ConversationBase):
    """对话创建模式"""
    llm_provider: Optional[str] = None


class ConversationResponse(BaseModel):
    """对话响应模式"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    user_id: int
    title: str
    llm_provider: str
    llm_model: Optional[str] = None
    is_archived: int
    created_at: datetime
    updated_at: datetime
    messages: List[MessageResponse] = []
    message_count: Optional[int] = None


class ConversationListResponse(BaseModel):
    """对话列表响应模式"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    title: str
    llm_provider: str
    is_archived: int
    created_at: datetime
    updated_at: datetime
    last_message: Optional[str] = None
    message_count: int = 0


class ChatRequest(BaseModel):
    """聊天请求模式"""
    message: str = Field(..., min_length=1)
    conversation_id: Optional[int] = None
    llm_provider: Optional[str] = None  # 临时指定 LLM
    stream: bool = True  # 是否流式返回
    use_tools: Optional[bool] = None  # 是否使用工具（None=自动检测）


class ChatStreamResponse(BaseModel):
    """聊天流式响应模式"""
    event: Literal["start", "thought", "action", "observation", "content", "done", "error"]
    data: Any
    conversation_id: Optional[int] = None
    message_id: Optional[int] = None
