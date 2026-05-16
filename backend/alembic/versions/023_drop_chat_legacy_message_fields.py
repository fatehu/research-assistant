"""
023_drop_chat_legacy_message_fields

Remove retired chat persistence structures:
- conversation_summaries
- messages.react_steps
- messages.action
- messages.action_input
- messages.observation
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "023_drop_chat_legacy_fields"
down_revision: Union[str, None] = "022_reader_plan_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_exists(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return any(item.get("name") == column_name for item in inspector.get_columns(table_name))


def _index_exists(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return any(item.get("name") == index_name for item in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "conversation_summaries"):
        for index_name in (
            "ix_conversation_summaries_up_to_message_id",
            "ix_conversation_summaries_conversation_id",
            "ix_conversation_summaries_id",
        ):
            if _index_exists(inspector, "conversation_summaries", index_name):
                op.drop_index(index_name, table_name="conversation_summaries")
                inspector = sa.inspect(bind)
        op.drop_table("conversation_summaries")
        inspector = sa.inspect(bind)

    for column_name in ("react_steps", "action", "action_input", "observation"):
        if _column_exists(inspector, "messages", column_name):
            op.drop_column("messages", column_name)
            inspector = sa.inspect(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "messages"):
        if not _column_exists(inspector, "messages", "react_steps"):
            op.add_column("messages", sa.Column("react_steps", sa.JSON(), nullable=True))
            inspector = sa.inspect(bind)
        if not _column_exists(inspector, "messages", "action"):
            op.add_column("messages", sa.Column("action", sa.String(length=200), nullable=True))
            inspector = sa.inspect(bind)
        if not _column_exists(inspector, "messages", "action_input"):
            op.add_column("messages", sa.Column("action_input", sa.JSON(), nullable=True))
            inspector = sa.inspect(bind)
        if not _column_exists(inspector, "messages", "observation"):
            op.add_column("messages", sa.Column("observation", sa.Text(), nullable=True))
            inspector = sa.inspect(bind)

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

    for index_name, columns in (
        ("ix_conversation_summaries_id", ["id"]),
        ("ix_conversation_summaries_conversation_id", ["conversation_id"]),
        ("ix_conversation_summaries_up_to_message_id", ["up_to_message_id"]),
    ):
        if not _index_exists(inspector, "conversation_summaries", index_name):
            op.create_index(index_name, "conversation_summaries", columns, unique=False)
            inspector = sa.inspect(bind)
