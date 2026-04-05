"""
Agent runtime persistence models.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)

from app.core.database import Base


class AgentRun(Base):
    """One agent execution run."""

    __tablename__ = "agent_runs"

    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    channel = Column(String(32), nullable=False, index=True)

    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True)
    notebook_id = Column(String(36), ForeignKey("notebooks.id", ondelete="SET NULL"), nullable=True, index=True)

    intent = Column(String(64), nullable=True)
    selected_tools = Column(JSON, default=list)
    model_provider = Column(String(64), nullable=True)
    model_name = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default="running")

    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    iteration_count = Column(Integer, default=0)

    metadata_ = Column("metadata", JSON, default=dict)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)


class AgentStepRecord(Base):
    """One step within an agent run."""

    __tablename__ = "agent_steps"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    step_index = Column(Integer, nullable=False)
    step_type = Column(String(32), nullable=False)

    content = Column(Text, nullable=True)
    tool_name = Column(String(128), nullable=True)
    tool_input = Column(JSON, nullable=True)
    tool_output = Column(Text, nullable=True)
    tool_success = Column(Boolean, nullable=True)

    execution_time_ms = Column(Float, default=0.0)
    output_tokens_estimate = Column(Integer, default=0)
    truncated = Column(Boolean, default=False)
    retry_attempt = Column(Integer, nullable=True)

    metadata_ = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)


class AgentMemoryItem(Base):
    """Long-term memory item."""

    __tablename__ = "agent_memory_items"

    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    channel = Column(String(32), nullable=False, index=True)
    scope_type = Column(String(32), nullable=False, index=True)
    scope_id = Column(String(64), nullable=False, index=True)

    content = Column(Text, nullable=False)
    embedding = Column(JSON, nullable=True)
    importance = Column(Float, default=0.5)
    last_accessed_at = Column(DateTime, nullable=True)
    metadata_ = Column("metadata", JSON, default=dict)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
