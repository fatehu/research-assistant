"""
聊天相关的 Pydantic 模式
"""
from datetime import datetime
from typing import Optional, List, Literal, Any, Dict
from pydantic import BaseModel, Field, ConfigDict, model_validator


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
    metadata: Optional[dict] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    created_at: datetime


class MessageSpanRewriteRequest(BaseModel):
    instruction: str = Field(..., min_length=1, max_length=500)
    selected_text: str = Field(..., min_length=1, max_length=4000)
    before_context: str = Field(default="", max_length=2000)
    after_context: str = Field(default="", max_length=2000)
    occurrence_index: Optional[int] = Field(default=None, ge=0)


class MessageSpanRewriteResponse(BaseModel):
    message: MessageResponse
    old_content: str
    new_content: str
    selected_text: str
    replacement_text: str
    start_offset: int
    end_offset: int


class ConversationEvidenceLedgerEntryResponse(BaseModel):
    entry_id: str
    origin_kind: Literal["tool_result", "assistant_summary", "llm_inferred"] = "llm_inferred"
    summary: str
    status: Literal["confirmed", "provisional"] = "confirmed"
    source_kind: Optional[str] = None
    source_labels: List[str] = Field(default_factory=list)
    tool_names: List[str] = Field(default_factory=list)
    turn_ids: List[str] = Field(default_factory=list)
    tool_call_ids: List[str] = Field(default_factory=list)
    result_count: Optional[int] = None
    provenance_hints: List[str] = Field(default_factory=list)
    retrieval_scope: Optional[dict] = None


class ConversationDecisionStateResponse(BaseModel):
    status: Optional[Literal["active", "ready", "blocked", "waiting"]] = None
    evidence_status: Optional[Literal["insufficient", "sufficient"]] = None
    next_action: Optional[str] = None
    blocked_reason: Optional[str] = None
    allowed_actions: List[str] = Field(default_factory=list)
    repo_edit_allowed: Optional[bool] = None


class ConversationContextStateResponse(BaseModel):
    version: str
    active_topic: Optional[str] = None
    user_goal: Optional[str] = None
    constraints: List[str] = Field(default_factory=list)
    open_questions: List[str] = Field(default_factory=list)
    resolved_facts: List[str] = Field(default_factory=list)
    evidence_ledger: List[ConversationEvidenceLedgerEntryResponse] = Field(default_factory=list)
    decision_state: Optional[ConversationDecisionStateResponse] = None
    last_reasoning_summary: Optional[str] = None
    last_user_message: Optional[str] = None
    turn_count: int = 0
    updated_at: Optional[str] = None


class ConversationReplacementHistoryEntryResponse(BaseModel):
    role: Literal["system", "user", "assistant"] = "system"
    content: str


class ConversationCompactedHistoryResponse(BaseModel):
    version: str
    history_anchors: Optional[str] = None
    history_summary: Optional[str] = None
    compact_boundary_message_id: Optional[int] = None
    replacement_history: List[ConversationReplacementHistoryEntryResponse] = Field(default_factory=list)
    compacted_message_count: int = 0
    up_to_message_id: Optional[int] = None
    updated_at: Optional[str] = None


class ConversationHistoryEventResponse(BaseModel):
    title: str
    detail: str
    created_at: Optional[str] = None


class ConversationHistoryLogResponse(BaseModel):
    version: str
    updated_at: Optional[str] = None
    events: List[ConversationHistoryEventResponse] = Field(default_factory=list)


class ConversationTurnEntryResponse(BaseModel):
    turn_id: str
    status: str
    user_message_id: Optional[int] = None
    assistant_message_id: Optional[int] = None
    run_id: Optional[str] = None
    user_content: Optional[str] = None
    assistant_summary: Optional[str] = None
    iteration_count: int = 0
    tool_call_count: int = 0
    tool_result_count: int = 0
    error_message: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class ConversationTurnStoreResponse(BaseModel):
    version: str
    updated_at: Optional[str] = None
    entries: List[ConversationTurnEntryResponse] = Field(default_factory=list)


class ConversationToolLedgerEntryResponse(BaseModel):
    entry_id: str
    kind: str
    tool_name: str
    turn_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    run_id: Optional[str] = None
    iteration: int = 0
    status: Optional[str] = None
    arguments: Optional[dict] = None
    summary: Optional[str] = None
    success: Optional[bool] = None
    error: Optional[str] = None
    permission_required: bool = False
    execution_time_ms: Optional[float] = None
    output_tokens_estimate: Optional[int] = None
    truncated: Optional[bool] = None
    parallel_group: Optional[str] = None
    metadata: Optional[dict] = None
    created_at: Optional[str] = None


class ConversationToolLedgerResponse(BaseModel):
    version: str
    updated_at: Optional[str] = None
    entries: List[ConversationToolLedgerEntryResponse] = Field(default_factory=list)


class ConversationItemEntryResponse(BaseModel):
    item_id: str
    kind: str
    turn_id: Optional[str] = None
    role: Optional[str] = None
    content: Optional[str] = None
    message_id: Optional[int] = None
    run_id: Optional[str] = None
    iteration: int = 0
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    status: Optional[str] = None
    arguments: Optional[dict] = None
    thought: Optional[str] = None
    summary: Optional[str] = None
    success: Optional[bool] = None
    error: Optional[str] = None
    permission_required: bool = False
    execution_time_ms: Optional[float] = None
    output_tokens_estimate: Optional[int] = None
    truncated: Optional[bool] = None
    parallel_group: Optional[str] = None
    metadata: Optional[dict] = None
    created_at: Optional[str] = None


class ConversationItemStreamResponse(BaseModel):
    version: str
    updated_at: Optional[str] = None
    entries: List[ConversationItemEntryResponse] = Field(default_factory=list)


class ConversationContextSnapshotResponse(BaseModel):
    version: str
    mode: Optional[str] = None
    created_at: Optional[str] = None
    summary_text: Optional[str] = None
    compacted_message_count: int = 0
    up_to_message_id: Optional[int] = None
    context_state: Optional[ConversationContextStateResponse] = None
    compacted_history: Optional[ConversationCompactedHistoryResponse] = None


class ConversationBase(BaseModel):
    """对话基础模式"""
    title: str = "新对话"


class ConversationCreate(ConversationBase):
    """对话创建模式"""
    llm_provider: Optional[str] = None


class ConversationBranchRequest(BaseModel):
    """创建对话分支。"""
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)


class DocumentArtifactBlockResponse(BaseModel):
    block_id: str
    index: int = 0
    title: str
    heading_path: List[str] = Field(default_factory=list)
    required: bool = True
    target_words: int = 0
    block_constraints: str = ""
    markdown: str = ""
    status: str = "empty"
    updated_at: Optional[str] = None


class DocumentArtifactResponse(BaseModel):
    schema_version: str = "document_artifact.v1"
    artifact_id: str
    template_id: str
    title: str
    global_constraints: str = ""
    blocks: List[DocumentArtifactBlockResponse] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DocumentArtifactSchemaGenerateRequest(BaseModel):
    template_id: str = Field(..., min_length=1, max_length=120)
    title: Optional[str] = Field(default="", max_length=300)
    user_notes: Optional[str] = Field(default="", max_length=4000)


class DocumentArtifactCreateRequest(BaseModel):
    template_id: str = Field(..., min_length=1, max_length=120)
    schema_: Dict[str, Any] = Field(..., alias="schema")


class DocumentArtifactBlockUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=300)
    block_constraints: Optional[str] = Field(default=None, max_length=20000)
    markdown: Optional[str] = Field(default=None, max_length=300000)
    status: Optional[str] = Field(default=None, max_length=40)


class DocumentArtifactSpanRewriteRequest(BaseModel):
    instruction: str = Field(..., min_length=1, max_length=4000)
    selected_text: str = Field(..., min_length=1, max_length=20000)
    before_context: Optional[str] = Field(default="", max_length=12000)
    after_context: Optional[str] = Field(default="", max_length=12000)
    occurrence_index: Optional[int] = Field(default=None, ge=0)
    start_offset: Optional[int] = Field(default=None, ge=0)
    end_offset: Optional[int] = Field(default=None, ge=0)


class DocumentArtifactSpanRewriteResponse(BaseModel):
    artifact: DocumentArtifactResponse
    block_id: str
    old_markdown: str
    new_markdown: str
    selected_text: str
    replacement_text: str
    start_offset: int
    end_offset: int


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
    messages: List[MessageResponse] = Field(default_factory=list)
    message_count: Optional[int] = None
    context_state: Optional[ConversationContextStateResponse] = None
    compacted_history: Optional[ConversationCompactedHistoryResponse] = None
    history_log: Optional[ConversationHistoryLogResponse] = None
    turn_store: Optional[ConversationTurnStoreResponse] = None
    tool_ledger: Optional[ConversationToolLedgerResponse] = None
    item_stream: Optional[ConversationItemStreamResponse] = None
    context_snapshots: List[ConversationContextSnapshotResponse] = Field(default_factory=list)
    document_artifact: Optional[DocumentArtifactResponse] = None


class ConversationCompactResponse(BaseModel):
    conversation_id: int
    context_state: Optional[ConversationContextStateResponse] = None
    compacted_history: Optional[ConversationCompactedHistoryResponse] = None
    history_log: Optional[ConversationHistoryLogResponse] = None
    turn_store: Optional[ConversationTurnStoreResponse] = None
    tool_ledger: Optional[ConversationToolLedgerResponse] = None
    item_stream: Optional[ConversationItemStreamResponse] = None
    context_snapshots: List[ConversationContextSnapshotResponse] = []
    summary_text: Optional[str] = None
    compacted_message_count: int = 0


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


class ChatSkillLaunch(BaseModel):
    model_config = ConfigDict(extra="allow")

    skill_name: str = Field(..., min_length=1)
    stage: Optional[str] = None
    paper_id: Optional[int] = Field(default=None, ge=1)
    project_id: Optional[int] = Field(default=None, ge=1)
    goal: Optional[str] = Field(default=None, max_length=4000)
    preferred_draft_id: Optional[str] = Field(default=None, max_length=256)


class ChatWorkflowActionResponse(BaseModel):
    label: str
    message: str
    skill_launch: Optional[ChatSkillLaunch] = None


class ChatWorkflowControlResponse(BaseModel):
    skill_name: str
    display_name: Optional[str] = None
    stage: str
    stage_label: Optional[str] = None
    stage_status: Literal["completed", "blocked", "running"] = "completed"
    continue_policy: Optional[str] = None
    next_stage: Optional[str] = None
    next_stage_label: Optional[str] = None
    suggested_action: Optional[str] = None
    action: Optional[ChatWorkflowActionResponse] = None


class ChatRequest(BaseModel):
    """聊天请求模式"""
    message: Optional[str] = Field(default=None, min_length=1)
    conversation_id: Optional[int] = None
    llm_provider: Optional[str] = None  # 临时指定 LLM
    stream: bool = True  # 是否流式返回
    use_tools: Optional[bool] = None  # 是否使用工具（None=自动检测）
    send_plan_id: Optional[str] = None
    chat_preference_overrides: Optional[dict] = None
    rag_overrides: Optional[dict] = None
    document_artifact_block_ids: List[str] = Field(default_factory=list, max_length=50)
    skill_launch: Optional[ChatSkillLaunch] = None

    @model_validator(mode="after")
    def _validate_message_or_skill_launch(self) -> "ChatRequest":
        message = str(self.message or "").strip()
        if not message and self.skill_launch is None:
            raise ValueError("message or skill_launch is required")
        self.message = message or None
        return self


class ChatContextPreviewRequest(BaseModel):
    message: str = Field(..., min_length=1)
    conversation_id: Optional[int] = None
    llm_provider: Optional[str] = None
    use_tools: Optional[bool] = None
    chat_preference_overrides: Optional[dict] = None
    rag_overrides: Optional[dict] = None


class ChatStreamResponse(BaseModel):
    """聊天流式响应模式"""
    event: Literal["start", "thought", "action", "observation", "content", "done", "error", "context_debug"]
    data: Any
    conversation_id: Optional[int] = None
    message_id: Optional[int] = None


class ChatContextPreviewResponse(BaseModel):
    conversation_id: Optional[int] = None
    preview_mode: Literal["agent", "direct"] = "agent"
    context_debug: dict
    context_state: Optional[ConversationContextStateResponse] = None
    compacted_history: Optional[ConversationCompactedHistoryResponse] = None
    history_log: Optional[ConversationHistoryLogResponse] = None
    turn_store: Optional[ConversationTurnStoreResponse] = None
    tool_ledger: Optional[ConversationToolLedgerResponse] = None
    item_stream: Optional[ConversationItemStreamResponse] = None
    context_snapshots: List[ConversationContextSnapshotResponse] = Field(default_factory=list)
    chat_preferences: Optional[dict] = None
    effective_chat_preferences: Optional[dict] = None
    effective_rag_overrides: Optional[dict] = None
    chat_preference_candidates: List[dict] = Field(default_factory=list)
    send_plan: Optional[dict] = None


class SaveStoppedMessageRequest(BaseModel):
    """保存停止消息的请求"""
    conversation_id: int
    content: str
    thought: Optional[str] = None
    metadata: Optional[dict] = None
