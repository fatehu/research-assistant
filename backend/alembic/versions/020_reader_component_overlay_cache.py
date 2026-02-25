"""
020_reader_component_overlay_cache

Add user-level component overlay cache table:
- paper_reader_component_overlays
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "020_reader_comp_overlay"
down_revision: Union[str, None] = "019_reader_gpage_cache"
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

    if not _table_exists(inspector, "paper_reader_component_overlays"):
        op.create_table(
            "paper_reader_component_overlays",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("paper_id", sa.Integer(), nullable=False),
            sa.Column("page", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source_signature", sa.String(length=255), nullable=False),
            sa.Column("node_id", sa.String(length=96), nullable=False),
            sa.Column("action_type", sa.String(length=32), nullable=False, server_default="patch"),
            sa.Column("overlay_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id",
                "paper_id",
                "page",
                "source_signature",
                "node_id",
                name="uq_reader_overlay_user_paper_page_sig_node",
            ),
        )

    inspector = sa.inspect(bind)
    if not _index_exists(inspector, "paper_reader_component_overlays", "ix_paper_reader_component_overlays_id"):
        op.create_index(
            "ix_paper_reader_component_overlays_id",
            "paper_reader_component_overlays",
            ["id"],
            unique=False,
        )
    if not _index_exists(inspector, "paper_reader_component_overlays", "ix_paper_reader_component_overlays_user_id"):
        op.create_index(
            "ix_paper_reader_component_overlays_user_id",
            "paper_reader_component_overlays",
            ["user_id"],
            unique=False,
        )
    if not _index_exists(inspector, "paper_reader_component_overlays", "ix_paper_reader_component_overlays_paper_id"):
        op.create_index(
            "ix_paper_reader_component_overlays_paper_id",
            "paper_reader_component_overlays",
            ["paper_id"],
            unique=False,
        )
    if not _index_exists(inspector, "paper_reader_component_overlays", "ix_paper_reader_component_overlays_page"):
        op.create_index(
            "ix_paper_reader_component_overlays_page",
            "paper_reader_component_overlays",
            ["page"],
            unique=False,
        )
    if not _index_exists(inspector, "paper_reader_component_overlays", "idx_reader_overlay_user_paper_page"):
        op.create_index(
            "idx_reader_overlay_user_paper_page",
            "paper_reader_component_overlays",
            ["user_id", "paper_id", "page"],
            unique=False,
        )
    if not _index_exists(inspector, "paper_reader_component_overlays", "idx_reader_overlay_updated_at"):
        op.create_index(
            "idx_reader_overlay_updated_at",
            "paper_reader_component_overlays",
            ["updated_at"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "paper_reader_component_overlays"):
        return

    for idx in (
        "idx_reader_overlay_updated_at",
        "idx_reader_overlay_user_paper_page",
        "ix_paper_reader_component_overlays_page",
        "ix_paper_reader_component_overlays_paper_id",
        "ix_paper_reader_component_overlays_user_id",
        "ix_paper_reader_component_overlays_id",
    ):
        if _index_exists(inspector, "paper_reader_component_overlays", idx):
            op.drop_index(idx, table_name="paper_reader_component_overlays")
            inspector = sa.inspect(bind)

    op.drop_table("paper_reader_component_overlays")
