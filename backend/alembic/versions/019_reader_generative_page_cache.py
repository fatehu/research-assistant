"""
019_reader_generative_page_cache

Add shared generative-reader page cache table:
- paper_reader_page_caches
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "019_reader_gpage_cache"
down_revision: Union[str, None] = "018_task_status_contract"
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

    if not _table_exists(inspector, "paper_reader_page_caches"):
        op.create_table(
            "paper_reader_page_caches",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("paper_id", sa.Integer(), nullable=False),
            sa.Column("page", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source_signature", sa.String(length=255), nullable=False),
            sa.Column("parser_version", sa.String(length=64), nullable=False),
            sa.Column("build_mode", sa.String(length=32), nullable=False, server_default="parser"),
            sa.Column("structure_confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("payload_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("paper_id", "page", "source_signature", name="uq_reader_page_cache_sig"),
        )

    inspector = sa.inspect(bind)
    if not _index_exists(inspector, "paper_reader_page_caches", "ix_paper_reader_page_caches_id"):
        op.create_index("ix_paper_reader_page_caches_id", "paper_reader_page_caches", ["id"], unique=False)
    if not _index_exists(inspector, "paper_reader_page_caches", "ix_paper_reader_page_caches_paper_id"):
        op.create_index(
            "ix_paper_reader_page_caches_paper_id",
            "paper_reader_page_caches",
            ["paper_id"],
            unique=False,
        )
    if not _index_exists(inspector, "paper_reader_page_caches", "ix_paper_reader_page_caches_page"):
        op.create_index("ix_paper_reader_page_caches_page", "paper_reader_page_caches", ["page"], unique=False)
    if not _index_exists(inspector, "paper_reader_page_caches", "idx_reader_page_cache_paper_page"):
        op.create_index(
            "idx_reader_page_cache_paper_page",
            "paper_reader_page_caches",
            ["paper_id", "page"],
            unique=False,
        )
    if not _index_exists(inspector, "paper_reader_page_caches", "idx_reader_page_cache_updated_at"):
        op.create_index(
            "idx_reader_page_cache_updated_at",
            "paper_reader_page_caches",
            ["updated_at"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "paper_reader_page_caches"):
        return

    for idx in (
        "idx_reader_page_cache_updated_at",
        "idx_reader_page_cache_paper_page",
        "ix_paper_reader_page_caches_page",
        "ix_paper_reader_page_caches_paper_id",
        "ix_paper_reader_page_caches_id",
    ):
        if _index_exists(inspector, "paper_reader_page_caches", idx):
            op.drop_index(idx, table_name="paper_reader_page_caches")
            inspector = sa.inspect(bind)

    op.drop_table("paper_reader_page_caches")

