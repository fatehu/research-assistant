"""DOCX template and generation indexes."""
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.database import Base


class DocxTemplate(Base):
    """User-managed DOCX template metadata.

    Files and generated artifacts remain on disk; this table is only the
    queryable index and editable metadata source.
    """

    __tablename__ = "docx_templates"

    id = Column(Integer, primary_key=True, index=True)
    template_id = Column(String(160), nullable=False, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    root_path = Column(Text, nullable=True)
    files_path = Column(Text, nullable=True)
    md_constraints = Column(Text, nullable=True)
    docx_constraints = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)

    user = relationship("User")
    files = relationship(
        "DocxTemplateFile",
        back_populates="template",
        cascade="all, delete-orphan",
        primaryjoin="DocxTemplate.template_id == foreign(DocxTemplateFile.template_id)",
    )
    generation_jobs = relationship(
        "DocxGenerationJob",
        back_populates="template",
        primaryjoin="DocxTemplate.template_id == foreign(DocxGenerationJob.template_id)",
    )

    __table_args__ = (
        Index("idx_docx_templates_user_updated", "user_id", "updated_at"),
    )


class DocxTemplateFile(Base):
    """Metadata for one uploaded template attachment."""

    __tablename__ = "docx_template_files"

    id = Column(Integer, primary_key=True, index=True)
    template_id = Column(String(160), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    original_filename = Column(String(500), nullable=False)
    stored_filename = Column(String(240), nullable=False)
    file_role = Column(String(40), default="reference", nullable=False, index=True)
    media_type = Column(String(200), nullable=True)
    size = Column(Integer, default=0)
    relative_path = Column(Text, nullable=True)
    path = Column(Text, nullable=True)

    parse_status = Column(String(40), default="pending", nullable=False, index=True)
    parse_warnings = Column(JSON, default=list)
    analysis_artifacts = Column(JSON, default=dict)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)

    user = relationship("User")
    template = relationship(
        "DocxTemplate",
        back_populates="files",
        primaryjoin="foreign(DocxTemplateFile.template_id) == DocxTemplate.template_id",
    )

    __table_args__ = (
        UniqueConstraint("template_id", "stored_filename", name="uq_docx_template_files_template_stored"),
        Index("idx_docx_template_files_template_role", "template_id", "file_role"),
    )


class DocxGenerationJob(Base):
    """Queryable index for one `/app/uploads/docx/{docx_id}` generation workspace."""

    __tablename__ = "docx_generation_jobs"

    id = Column(Integer, primary_key=True, index=True)
    docx_id = Column(String(160), nullable=False, unique=True, index=True)
    template_id = Column(String(160), nullable=True, index=True)
    template_name = Column(String(200), nullable=True)
    artifact_id = Column(String(160), nullable=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    workspace_path = Column(Text, nullable=False)
    source_path = Column(Text, nullable=True)
    requirements_path = Column(Text, nullable=True)
    output_basename = Column(String(200), nullable=True)
    docx_path = Column(Text, nullable=True)
    pdf_path = Column(Text, nullable=True)

    status = Column(String(40), default="running", nullable=False, index=True)
    validation_status = Column(String(80), nullable=True)
    claude_session_id = Column(String(200), nullable=True)
    error_message = Column(Text, nullable=True)
    files = Column(JSON, default=list)
    metadata_ = Column("metadata", JSON, default=dict)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)
    completed_at = Column(DateTime, nullable=True)

    user = relationship("User")
    conversation = relationship("Conversation")
    template = relationship(
        "DocxTemplate",
        back_populates="generation_jobs",
        primaryjoin="foreign(DocxGenerationJob.template_id) == DocxTemplate.template_id",
    )

    __table_args__ = (
        Index("idx_docx_generation_jobs_template_updated", "template_id", "updated_at"),
        Index("idx_docx_generation_jobs_user_updated", "user_id", "updated_at"),
        Index("idx_docx_generation_jobs_conversation_updated", "conversation_id", "updated_at"),
    )
