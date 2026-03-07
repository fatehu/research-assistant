"""
021_admin_audit_logs

Add admin audit log table for administrator operations.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "021_admin_audit_logs"
down_revision: Union[str, None] = "020_reader_comp_overlay"
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

    if not _table_exists(inspector, "admin_audit_logs"):
        op.create_table(
            "admin_audit_logs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("admin_user_id", sa.Integer(), nullable=False),
            sa.Column("action", sa.String(length=64), nullable=False),
            sa.Column("target_type", sa.String(length=64), nullable=True),
            sa.Column("target_id", sa.String(length=64), nullable=True),
            sa.Column("summary", sa.String(length=500), nullable=False),
            sa.Column("details", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["admin_user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(bind)
    for index_name, columns in (
        ("ix_admin_audit_logs_id", ["id"]),
        ("ix_admin_audit_logs_admin_user_id", ["admin_user_id"]),
        ("ix_admin_audit_logs_action", ["action"]),
        ("ix_admin_audit_logs_target_type", ["target_type"]),
        ("ix_admin_audit_logs_target_id", ["target_id"]),
        ("ix_admin_audit_logs_created_at", ["created_at"]),
    ):
        if not _index_exists(inspector, "admin_audit_logs", index_name):
            op.create_index(index_name, "admin_audit_logs", columns, unique=False)
            inspector = sa.inspect(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "admin_audit_logs"):
        return

    for index_name in (
        "ix_admin_audit_logs_created_at",
        "ix_admin_audit_logs_target_id",
        "ix_admin_audit_logs_target_type",
        "ix_admin_audit_logs_action",
        "ix_admin_audit_logs_admin_user_id",
        "ix_admin_audit_logs_id",
    ):
        if _index_exists(inspector, "admin_audit_logs", index_name):
            op.drop_index(index_name, table_name="admin_audit_logs")
            inspector = sa.inspect(bind)

    op.drop_table("admin_audit_logs")
