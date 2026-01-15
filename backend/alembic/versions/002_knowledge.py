"""阶段2: 添加知识库相关表 (使用 pgvector)

Revision ID: 002_knowledge
Revises: 001_initial
Create Date: 2024-01-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = '002_knowledge'
down_revision: Union[str, None] = '001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 阿里云 text-embedding-v2 向量维度
EMBEDDING_DIMENSION = 1536


def upgrade() -> None:
    # 启用 pgvector 扩展
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    
    # 创建知识库表
    op.create_table(
        'knowledge_bases',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('embedding_model', sa.String(100), server_default='text-embedding-v2'),
        sa.Column('embedding_dimension', sa.Integer(), server_default=str(EMBEDDING_DIMENSION)),
        sa.Column('chunk_size', sa.Integer(), server_default='500'),
        sa.Column('chunk_overlap', sa.Integer(), server_default='50'),
        sa.Column('document_count', sa.Integer(), server_default='0'),
        sa.Column('total_chunks', sa.Integer(), server_default='0'),
        sa.Column('total_tokens', sa.BigInteger(), server_default='0'),
        sa.Column('metadata', sa.JSON(), server_default='{}'),
        sa.Column('is_public', sa.Boolean(), server_default='false'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_knowledge_bases_user_id', 'knowledge_bases', ['user_id'])
    
    # 创建文档表
    op.create_table(
        'documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('knowledge_base_id', sa.Integer(), nullable=False),
        sa.Column('filename', sa.String(500), nullable=False),
        sa.Column('original_filename', sa.String(500), nullable=False),
        sa.Column('file_path', sa.String(1000), nullable=True),
        sa.Column('file_size', sa.BigInteger(), server_default='0'),
        sa.Column('file_type', sa.String(50), nullable=False),
        sa.Column('mime_type', sa.String(100), nullable=True),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.String(64), nullable=True),
        sa.Column('status', sa.String(20), server_default='pending'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('chunk_count', sa.Integer(), server_default='0'),
        sa.Column('token_count', sa.Integer(), server_default='0'),
        sa.Column('char_count', sa.Integer(), server_default='0'),
        sa.Column('metadata', sa.JSON(), server_default='{}'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_documents_knowledge_base_id', 'documents', ['knowledge_base_id'])
    op.create_index('ix_documents_content_hash', 'documents', ['content_hash'])
    
    # 创建文档分片表 (使用 pgvector 的 vector 类型)
    op.create_table(
        'document_chunks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('knowledge_base_id', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('start_char', sa.Integer(), server_default='0'),
        sa.Column('end_char', sa.Integer(), server_default='0'),
        # pgvector 向量列 - 1536 维 (阿里云 text-embedding-v2)
        sa.Column('embedding', Vector(EMBEDDING_DIMENSION), nullable=True),
        sa.Column('embedding_model', sa.String(100), nullable=True),
        sa.Column('token_count', sa.Integer(), server_default='0'),
        sa.Column('char_count', sa.Integer(), server_default='0'),
        sa.Column('metadata', sa.JSON(), server_default='{}'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_document_chunks_document_id', 'document_chunks', ['document_id'])
    op.create_index('ix_document_chunks_knowledge_base_id', 'document_chunks', ['knowledge_base_id'])
    op.create_index('idx_chunk_kb_doc', 'document_chunks', ['knowledge_base_id', 'document_id'])
    
    # 创建 HNSW 向量索引 (用于高效相似度搜索)
    # HNSW (Hierarchical Navigable Small World) 是一种近似最近邻搜索算法
    # 使用余弦距离 (vector_cosine_ops)
    op.execute('''
        CREATE INDEX idx_chunks_embedding_hnsw 
        ON document_chunks 
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    ''')


def downgrade() -> None:
    op.drop_table('document_chunks')
    op.drop_table('documents')
    op.drop_table('knowledge_bases')
    # 不删除 vector 扩展，因为其他表可能也在用
