"""
015_agent_core_runtime

Add agent runtime persistence tables:
- agent_runs
- agent_steps
- conversation_summaries
- agent_memory_items
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "015_agent_core_runtime"
down_revision: Union[str, None] = "014_drop_legacy_hnsw"
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

    if not _table_exists(inspector, "agent_runs"):
        op.create_table(
            "agent_runs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("channel", sa.String(length=32), nullable=False),
            sa.Column("conversation_id", sa.Integer(), nullable=True),
            sa.Column("notebook_id", sa.String(length=36), nullable=True),
            sa.Column("intent", sa.String(length=64), nullable=True),
            sa.Column("selected_tools", sa.JSON(), nullable=True),
            sa.Column("model_provider", sa.String(length=64), nullable=True),
            sa.Column("model_name", sa.String(length=128), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
            sa.Column("prompt_tokens", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("total_tokens", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("iteration_count", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["notebook_id"], ["notebooks.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(bind)
    if not _index_exists(inspector, "agent_runs", "ix_agent_runs_user_id"):
        op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"], unique=False)
    if not _index_exists(inspector, "agent_runs", "ix_agent_runs_channel"):
        op.create_index("ix_agent_runs_channel", "agent_runs", ["channel"], unique=False)
    if not _index_exists(inspector, "agent_runs", "ix_agent_runs_conversation_id"):
        op.create_index("ix_agent_runs_conversation_id", "agent_runs", ["conversation_id"], unique=False)
    if not _index_exists(inspector, "agent_runs", "ix_agent_runs_notebook_id"):
        op.create_index("ix_agent_runs_notebook_id", "agent_runs", ["notebook_id"], unique=False)

    if not _table_exists(inspector, "agent_steps"):
        op.create_table(
            "agent_steps",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("step_index", sa.Integer(), nullable=False),
            sa.Column("step_type", sa.String(length=32), nullable=False),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("tool_name", sa.String(length=128), nullable=True),
            sa.Column("tool_input", sa.JSON(), nullable=True),
            sa.Column("tool_output", sa.Text(), nullable=True),
            sa.Column("tool_success", sa.Boolean(), nullable=True),
            sa.Column("execution_time_ms", sa.Float(), nullable=True, server_default="0"),
            sa.Column("output_tokens_estimate", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("truncated", sa.Boolean(), nullable=True, server_default=sa.text("false")),
            sa.Column("retry_attempt", sa.Integer(), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(bind)
    if not _index_exists(inspector, "agent_steps", "ix_agent_steps_id"):
        op.create_index("ix_agent_steps_id", "agent_steps", ["id"], unique=False)
    if not _index_exists(inspector, "agent_steps", "ix_agent_steps_run_id"):
        op.create_index("ix_agent_steps_run_id", "agent_steps", ["run_id"], unique=False)

    if not _table_exists(inspector, "conversation_summaries"):
        op.create_table(
            "conversation_summaries",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("conversation_id", sa.Integer(), nullable=False),
            sa.Column("up_to_message_id", sa.Integer(), nullable=True),
            sa.Column("summary_text", sa.Text(), nullable=False),
            sa.Column("token_count", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(bind)
    if not _index_exists(inspector, "conversation_summaries", "ix_conversation_summaries_id"):
        op.create_index("ix_conversation_summaries_id", "conversation_summaries", ["id"], unique=False)
    if not _index_exists(inspector, "conversation_summaries", "ix_conversation_summaries_conversation_id"):
        op.create_index("ix_conversation_summaries_conversation_id", "conversation_summaries", ["conversation_id"], unique=False)
    if not _index_exists(inspector, "conversation_summaries", "ix_conversation_summaries_up_to_message_id"):
        op.create_index("ix_conversation_summaries_up_to_message_id", "conversation_summaries", ["up_to_message_id"], unique=False)

    if not _table_exists(inspector, "agent_memory_items"):
        op.create_table(
            "agent_memory_items",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("channel", sa.String(length=32), nullable=False),
            sa.Column("scope_type", sa.String(length=32), nullable=False),
            sa.Column("scope_id", sa.String(length=64), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("embedding", sa.JSON(), nullable=True),
            sa.Column("importance", sa.Float(), nullable=True, server_default="0.5"),
            sa.Column("last_accessed_at", sa.DateTime(), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(bind)
    if not _index_exists(inspector, "agent_memory_items", "ix_agent_memory_items_user_id"):
        op.create_index("ix_agent_memory_items_user_id", "agent_memory_items", ["user_id"], unique=False)
    if not _index_exists(inspector, "agent_memory_items", "ix_agent_memory_items_channel"):
        op.create_index("ix_agent_memory_items_channel", "agent_memory_items", ["channel"], unique=False)
    if not _index_exists(inspector, "agent_memory_items", "ix_agent_memory_items_scope_type"):
        op.create_index("ix_agent_memory_items_scope_type", "agent_memory_items", ["scope_type"], unique=False)
    if not _index_exists(inspector, "agent_memory_items", "ix_agent_memory_items_scope_id"):
        op.create_index("ix_agent_memory_items_scope_id", "agent_memory_items", ["scope_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "agent_memory_items"):
        for idx in (
            "ix_agent_memory_items_scope_id",
            "ix_agent_memory_items_scope_type",
            "ix_agent_memory_items_channel",
            "ix_agent_memory_items_user_id",
        ):
            if _index_exists(inspector, "agent_memory_items", idx):
                op.drop_index(idx, table_name="agent_memory_items")
                inspector = sa.inspect(bind)
        op.drop_table("agent_memory_items")
        inspector = sa.inspect(bind)

    if _table_exists(inspector, "conversation_summaries"):
        for idx in (
            "ix_conversation_summaries_up_to_message_id",
            "ix_conversation_summaries_conversation_id",
            "ix_conversation_summaries_id",
        ):
            if _index_exists(inspector, "conversation_summaries", idx):
                op.drop_index(idx, table_name="conversation_summaries")
                inspector = sa.inspect(bind)
        op.drop_table("conversation_summaries")
        inspector = sa.inspect(bind)

    if _table_exists(inspector, "agent_steps"):
        for idx in ("ix_agent_steps_run_id", "ix_agent_steps_id"):
            if _index_exists(inspector, "agent_steps", idx):
                op.drop_index(idx, table_name="agent_steps")
                inspector = sa.inspect(bind)
        op.drop_table("agent_steps")
        inspector = sa.inspect(bind)

    if _table_exists(inspector, "agent_runs"):
        for idx in (
            "ix_agent_runs_notebook_id",
            "ix_agent_runs_conversation_id",
            "ix_agent_runs_channel",
            "ix_agent_runs_user_id",
        ):
            if _index_exists(inspector, "agent_runs", idx):
                op.drop_index(idx, table_name="agent_runs")
                inspector = sa.inspect(bind)
        op.drop_table("agent_runs")
