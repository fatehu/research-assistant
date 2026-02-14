"""
014_drop_legacy_embedding_hnsw

Drop legacy single-index HNSW on `document_chunks.embedding`.
That index is incompatible with mixed vector dimensions (256/512/1024).
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "014_drop_legacy_hnsw"
down_revision: Union[str, None] = "013_dynamic_vector_dimension"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chunks_embedding_hnsw")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_chunks_embedding_hnsw")


def downgrade() -> None:
    # Legacy global index is intentionally not restored.
    # Mixed dimensions should use dimension-scoped expression indexes.
    pass
