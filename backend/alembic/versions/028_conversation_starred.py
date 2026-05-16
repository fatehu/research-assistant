"""028_conversation_starred

Add star metadata for pinning important chat conversations.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "028_conversation_starred"
down_revision: Union[str, None] = "027_docx_indexes"
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

    if not _column_exists(inspector, "conversations", "is_starred"):
        op.add_column(
            "conversations",
            sa.Column("is_starred", sa.Integer(), nullable=False, server_default="0"),
        )
        inspector = sa.inspect(bind)
    if not _column_exists(inspector, "conversations", "starred_at"):
        op.add_column("conversations", sa.Column("starred_at", sa.DateTime(), nullable=True))
        inspector = sa.inspect(bind)

    if not _index_exists(inspector, "conversations", "idx_conversations_user_starred_updated"):
        op.create_index(
            "idx_conversations_user_starred_updated",
            "conversations",
            ["user_id", "is_starred", "starred_at", "updated_at"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _index_exists(inspector, "conversations", "idx_conversations_user_starred_updated"):
        op.drop_index("idx_conversations_user_starred_updated", table_name="conversations")
        inspector = sa.inspect(bind)
    if _column_exists(inspector, "conversations", "starred_at"):
        op.drop_column("conversations", "starred_at")
        inspector = sa.inspect(bind)
    if _column_exists(inspector, "conversations", "is_starred"):
        op.drop_column("conversations", "is_starred")
