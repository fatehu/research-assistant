"""
010_content_segmented_fts

Add segmented content column for Chinese-friendly FTS and create GIN expression index.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "010_content_segmented_fts"
down_revision: Union[str, None] = "009_hnsw_index_concurrently"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS content_segmented TEXT")
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
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS content_segmented")
