"""027_docx_template_generation_indexes

Add database indexes for DOCX templates, uploaded template files, and generated
DOCX workspaces. The files themselves remain on disk under /app/uploads/docx.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "027_docx_indexes"
down_revision: Union[str, None] = "026_project_primary_workspace"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _index_exists(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return any(item.get("name") == index_name for item in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "docx_templates"):
        op.create_table(
            "docx_templates",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("template_id", sa.String(length=160), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("root_path", sa.Text(), nullable=True),
            sa.Column("files_path", sa.Text(), nullable=True),
            sa.Column("md_constraints", sa.Text(), nullable=True),
            sa.Column("docx_constraints", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("template_id", name="uq_docx_templates_template_id"),
        )
        inspector = sa.inspect(bind)

    if not _table_exists(inspector, "docx_template_files"):
        op.create_table(
            "docx_template_files",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("template_id", sa.String(length=160), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("original_filename", sa.String(length=500), nullable=False),
            sa.Column("stored_filename", sa.String(length=240), nullable=False),
            sa.Column("file_role", sa.String(length=40), nullable=False),
            sa.Column("media_type", sa.String(length=200), nullable=True),
            sa.Column("size", sa.Integer(), nullable=True),
            sa.Column("relative_path", sa.Text(), nullable=True),
            sa.Column("path", sa.Text(), nullable=True),
            sa.Column("parse_status", sa.String(length=40), nullable=False),
            sa.Column("parse_warnings", sa.JSON(), nullable=True),
            sa.Column("analysis_artifacts", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("template_id", "stored_filename", name="uq_docx_template_files_template_stored"),
        )
        inspector = sa.inspect(bind)

    if not _table_exists(inspector, "docx_generation_jobs"):
        op.create_table(
            "docx_generation_jobs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("docx_id", sa.String(length=160), nullable=False),
            sa.Column("template_id", sa.String(length=160), nullable=True),
            sa.Column("template_name", sa.String(length=200), nullable=True),
            sa.Column("artifact_id", sa.String(length=160), nullable=True),
            sa.Column("conversation_id", sa.Integer(), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("workspace_path", sa.Text(), nullable=False),
            sa.Column("source_path", sa.Text(), nullable=True),
            sa.Column("requirements_path", sa.Text(), nullable=True),
            sa.Column("output_basename", sa.String(length=200), nullable=True),
            sa.Column("docx_path", sa.Text(), nullable=True),
            sa.Column("pdf_path", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("validation_status", sa.String(length=80), nullable=True),
            sa.Column("claude_session_id", sa.String(length=200), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("files", sa.JSON(), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("docx_id", name="uq_docx_generation_jobs_docx_id"),
        )
        inspector = sa.inspect(bind)

    for table_name, indexes in {
        "docx_templates": (
            ("ix_docx_templates_template_id", ["template_id"]),
            ("ix_docx_templates_user_id", ["user_id"]),
            ("ix_docx_templates_updated_at", ["updated_at"]),
            ("idx_docx_templates_user_updated", ["user_id", "updated_at"]),
        ),
        "docx_template_files": (
            ("ix_docx_template_files_template_id", ["template_id"]),
            ("ix_docx_template_files_user_id", ["user_id"]),
            ("ix_docx_template_files_file_role", ["file_role"]),
            ("ix_docx_template_files_parse_status", ["parse_status"]),
            ("ix_docx_template_files_updated_at", ["updated_at"]),
            ("idx_docx_template_files_template_role", ["template_id", "file_role"]),
        ),
        "docx_generation_jobs": (
            ("ix_docx_generation_jobs_docx_id", ["docx_id"]),
            ("ix_docx_generation_jobs_template_id", ["template_id"]),
            ("ix_docx_generation_jobs_artifact_id", ["artifact_id"]),
            ("ix_docx_generation_jobs_conversation_id", ["conversation_id"]),
            ("ix_docx_generation_jobs_user_id", ["user_id"]),
            ("ix_docx_generation_jobs_status", ["status"]),
            ("ix_docx_generation_jobs_updated_at", ["updated_at"]),
            ("idx_docx_generation_jobs_template_updated", ["template_id", "updated_at"]),
            ("idx_docx_generation_jobs_user_updated", ["user_id", "updated_at"]),
            ("idx_docx_generation_jobs_conversation_updated", ["conversation_id", "updated_at"]),
        ),
    }.items():
        if not _table_exists(inspector, table_name):
            continue
        for index_name, columns in indexes:
            if not _index_exists(inspector, table_name, index_name):
                op.create_index(index_name, table_name, columns, unique=False)
                inspector = sa.inspect(bind)


def downgrade() -> None:
    for table_name in ("docx_generation_jobs", "docx_template_files", "docx_templates"):
        op.drop_table(table_name)
