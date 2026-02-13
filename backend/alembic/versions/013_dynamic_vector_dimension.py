"""
013_dynamic_vector_dimension

Switch document_chunks.embedding from fixed-length vector to dynamic vector and
add dimension-specific HNSW expression indexes (256/512/1024).
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "013_dynamic_vector_dimension"
down_revision: Union[str, None] = "012_chunk_embedding_dim"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

try:
    from app.models.knowledge import EMBEDDING_DIMENSION

    DEFAULT_DIMENSION = int(EMBEDDING_DIMENSION)
except Exception:
    DEFAULT_DIMENSION = 1024


def _create_hnsw_expression_indexes() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chunks_embedding_hnsw_256
            ON document_chunks
            USING hnsw ((embedding::vector(256)) vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE embedding IS NOT NULL AND embedding_dimension = 256
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chunks_embedding_hnsw_512
            ON document_chunks
            USING hnsw ((embedding::vector(512)) vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE embedding IS NOT NULL AND embedding_dimension = 512
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chunks_embedding_hnsw_1024
            ON document_chunks
            USING hnsw ((embedding::vector(1024)) vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE embedding IS NOT NULL AND embedding_dimension = 1024
            """
        )


def _drop_hnsw_expression_indexes() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chunks_embedding_hnsw_256")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chunks_embedding_hnsw_512")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chunks_embedding_hnsw_1024")


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE document_chunks
        ALTER COLUMN embedding TYPE vector
        USING embedding::vector
        """
    )
    _create_hnsw_expression_indexes()


def downgrade() -> None:
    _drop_hnsw_expression_indexes()
    op.execute(
        f"""
        UPDATE document_chunks
        SET embedding = NULL
        WHERE embedding IS NOT NULL
          AND embedding_dimension <> {DEFAULT_DIMENSION}
        """
    )
    op.execute(
        f"""
        UPDATE document_chunks
        SET embedding_dimension = {DEFAULT_DIMENSION}
        WHERE embedding_dimension <> {DEFAULT_DIMENSION}
        """
    )
    op.execute(
        f"""
        ALTER TABLE document_chunks
        ALTER COLUMN embedding TYPE vector({DEFAULT_DIMENSION})
        USING embedding::vector({DEFAULT_DIMENSION})
        """
    )
