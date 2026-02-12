"""
009_hnsw_index_concurrently

将 document_chunks.embedding 的 HNSW 索引改为并发创建，避免迁移期间长时间锁表。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "009_hnsw_index_concurrently"
down_revision: Union[str, None] = "008_embedding_dimension"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_chunks_embedding_hnsw")
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chunks_embedding_hnsw
            ON document_chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chunks_embedding_hnsw")
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunks_embedding_hnsw
            ON document_chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            """
        )
