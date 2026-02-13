"""
010_add_content_segmented_and_fts_index

Add segmented content column for Chinese-friendly FTS and create GIN expression index.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "010_add_content_segmented_and_fts_index"
down_revision: Union[str, None] = "009_hnsw_index_concurrently"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("document_chunks", sa.Column("content_segmented", sa.Text(), nullable=True))
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chunks_content_segmented_fts
            ON document_chunks
            USING gin (to_tsvector('simple', coalesce(content_segmented, '')))
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chunks_content_segmented_fts")
    op.drop_column("document_chunks", "content_segmented")

