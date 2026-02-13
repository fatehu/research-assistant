"""
012_document_chunks_embedding_dimension

Add embedding_dimension column to document_chunks and backfill values.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "012_chunk_embedding_dim"
down_revision: Union[str, None] = "011_context_summary"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

try:
    from app.models.knowledge import EMBEDDING_DIMENSION
    DEFAULT_DIMENSION = int(EMBEDDING_DIMENSION)
except Exception:
    DEFAULT_DIMENSION = 1024


def upgrade() -> None:
    op.execute("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding_dimension INTEGER")
    op.execute(
        f"""
        UPDATE document_chunks dc
        SET embedding_dimension = COALESCE(kb.embedding_dimension, {DEFAULT_DIMENSION})
        FROM knowledge_bases kb
        WHERE dc.knowledge_base_id = kb.id
          AND dc.embedding_dimension IS NULL
        """
    )
    op.execute(
        f"""
        UPDATE document_chunks
        SET embedding_dimension = {DEFAULT_DIMENSION}
        WHERE embedding_dimension IS NULL
        """
    )
    op.execute(
        f"""
        ALTER TABLE document_chunks
        ALTER COLUMN embedding_dimension SET DEFAULT {DEFAULT_DIMENSION}
        """
    )
    op.execute("ALTER TABLE document_chunks ALTER COLUMN embedding_dimension SET NOT NULL")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding_dimension "
        "ON document_chunks (embedding_dimension)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_dimension")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS embedding_dimension")
