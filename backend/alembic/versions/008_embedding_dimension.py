"""
008_embedding_dimension_update - 更新嵌入向量维度

将嵌入向量维度从固定的 1536 (阿里云 text-embedding-v2) 改为可配置维度，
以支持本地科研嵌入模型 (如 BAAI/bge-m3 的 1024 维)。

⚠️ 注意: 此迁移会清空所有已有的 embedding 向量数据，
   需要在迁移完成后重新为所有文档生成嵌入向量。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '008_embedding_dimension'
down_revision = '007_hierarchical_chunks'
branch_labels = None
depends_on = None

# 新维度 - 从环境变量/配置获取
# 默认 1024 (BAAI/bge-m3), 可根据实际模型修改
try:
    from app.models.knowledge import EMBEDDING_DIMENSION
    NEW_DIMENSION = EMBEDDING_DIMENSION
except Exception:
    NEW_DIMENSION = 1024

OLD_DIMENSION = 1536


def upgrade():
    """升级: 修改向量维度"""
    if NEW_DIMENSION == OLD_DIMENSION:
        # 维度没变，跳过
        return

    # 1. 删除可能存在的 HNSW 索引
    op.execute("DROP INDEX IF EXISTS idx_chunks_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS idx_document_chunks_embedding")

    # 2. 清空旧的嵌入数据 (维度不同无法保留)
    op.execute("UPDATE document_chunks SET embedding = NULL")

    # 3. 修改向量列维度
    op.execute(
        f"ALTER TABLE document_chunks "
        f"ALTER COLUMN embedding TYPE vector({NEW_DIMENSION}) "
        f"USING embedding::vector({NEW_DIMENSION})"
    )

    # 4. 更新知识库表的默认维度和模型名
    op.execute(
        f"UPDATE knowledge_bases SET "
        f"embedding_dimension = {NEW_DIMENSION}, "
        f"embedding_model = 'BAAI/bge-m3' "
        f"WHERE embedding_model = 'text-embedding-v2'"
    )

    # 5. 重建 HNSW 索引 (余弦距离)
    op.execute(
        f"CREATE INDEX idx_chunks_embedding_hnsw ON document_chunks "
        f"USING hnsw (embedding vector_cosine_ops) "
        f"WITH (m = 16, ef_construction = 64)"
    )

    print(f"✅ 向量维度已从 {OLD_DIMENSION} 更新为 {NEW_DIMENSION}")
    print("⚠️  请重新为所有文档生成嵌入向量 (旧向量已清空)")


def downgrade():
    """降级: 恢复为 1536 维"""
    if NEW_DIMENSION == OLD_DIMENSION:
        return

    op.execute("DROP INDEX IF EXISTS idx_chunks_embedding_hnsw")

    op.execute("UPDATE document_chunks SET embedding = NULL")

    op.execute(
        f"ALTER TABLE document_chunks "
        f"ALTER COLUMN embedding TYPE vector({OLD_DIMENSION}) "
        f"USING embedding::vector({OLD_DIMENSION})"
    )

    op.execute(
        f"UPDATE knowledge_bases SET "
        f"embedding_dimension = {OLD_DIMENSION}, "
        f"embedding_model = 'text-embedding-v2'"
    )

    op.execute(
        f"CREATE INDEX idx_chunks_embedding_hnsw ON document_chunks "
        f"USING hnsw (embedding vector_cosine_ops) "
        f"WITH (m = 16, ef_construction = 64)"
    )
