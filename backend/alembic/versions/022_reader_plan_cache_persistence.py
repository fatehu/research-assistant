"""
022_reader_plan_cache_persistence

Add persistent plan cache table for reader generative/experience plans:
- paper_reader_plan_caches
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "022_reader_plan_cache"
down_revision: Union[str, None] = "021_admin_audit_logs"
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

    if not _table_exists(inspector, "paper_reader_plan_caches"):
        op.create_table(
            "paper_reader_plan_caches",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("plan_kind", sa.String(length=32), nullable=False),
            sa.Column("cache_key", sa.String(length=255), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("paper_id", sa.Integer(), nullable=False),
            sa.Column("page", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("compose_source_signature", sa.String(length=255), nullable=False),
            sa.Column("payload_json", sa.JSON(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("cache_key", name="uq_reader_plan_cache_key"),
        )

    inspector = sa.inspect(bind)
    for index_name, columns in (
        ("ix_paper_reader_plan_caches_id", ["id"]),
        ("ix_paper_reader_plan_caches_user_id", ["user_id"]),
        ("ix_paper_reader_plan_caches_paper_id", ["paper_id"]),
        ("ix_paper_reader_plan_caches_page", ["page"]),
        ("ix_paper_reader_plan_caches_expires_at", ["expires_at"]),
        (
            "idx_reader_plan_cache_user_paper_page_kind",
            ["user_id", "paper_id", "page", "plan_kind"],
        ),
        ("idx_reader_plan_cache_expires_at", ["expires_at"]),
        ("idx_reader_plan_cache_updated_at", ["updated_at"]),
    ):
        if not _index_exists(inspector, "paper_reader_plan_caches", index_name):
            op.create_index(index_name, "paper_reader_plan_caches", columns, unique=False)
            inspector = sa.inspect(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "paper_reader_plan_caches"):
        return

    for index_name in (
        "idx_reader_plan_cache_updated_at",
        "idx_reader_plan_cache_expires_at",
        "idx_reader_plan_cache_user_paper_page_kind",
        "ix_paper_reader_plan_caches_expires_at",
        "ix_paper_reader_plan_caches_page",
        "ix_paper_reader_plan_caches_paper_id",
        "ix_paper_reader_plan_caches_user_id",
        "ix_paper_reader_plan_caches_id",
    ):
        if _index_exists(inspector, "paper_reader_plan_caches", index_name):
            op.drop_index(index_name, table_name="paper_reader_plan_caches")
            inspector = sa.inspect(bind)

    op.drop_table("paper_reader_plan_caches")
