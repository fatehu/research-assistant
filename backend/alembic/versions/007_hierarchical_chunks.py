"""
007_hierarchical_chunks - 支持层级分块

添加层级分块支持：
1. document_chunks 表增加层级相关字段
2. 添加 chunk_hierarchy 关联表
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

# revision identifiers
revision = '007_hierarchical_chunks'
down_revision = '006_multi_role'
branch_labels = None
depends_on = None


def upgrade():
    # 1. 为 document_chunks 添加层级相关字段
    op.add_column('document_chunks', sa.Column(
        'chunk_level',
        sa.String(20),
        nullable=True,
        server_default='paragraph',
        comment='分块层级: paragraph/section/document'
    ))
    
    op.add_column('document_chunks', sa.Column(
        'section_type',
        sa.String(50),
        nullable=True,
        comment='学术章节类型: abstract/introduction/methodology等'
    ))
    
    op.add_column('document_chunks', sa.Column(
        'section_title',
        sa.String(500),
        nullable=True,
        comment='章节标题'
    ))
    
    op.add_column('document_chunks', sa.Column(
        'parent_chunk_id',
        sa.Integer,
        sa.ForeignKey('document_chunks.id', ondelete='SET NULL'),
        nullable=True,
        comment='父块ID（用于层级关系）'
    ))
    
    op.add_column('document_chunks', sa.Column(
        'has_citations',
        sa.Boolean,
        nullable=True,
        server_default='false',
        comment='是否包含引用'
    ))
    
    op.add_column('document_chunks', sa.Column(
        'semantic_score',
        sa.Float,
        nullable=True,
        comment='语义连贯性得分'
    ))
    
    # 2. 创建层级关系索引
    op.create_index(
        'idx_chunk_level',
        'document_chunks',
        ['chunk_level']
    )
    
    op.create_index(
        'idx_chunk_parent',
        'document_chunks',
        ['parent_chunk_id']
    )
    
    op.create_index(
        'idx_chunk_section_type',
        'document_chunks',
        ['section_type']
    )
    
    # 3. 为知识库添加分块策略配置
    # （已通过 metadata 字段存储，无需新增字段）
    
    print("✓ 层级分块支持已添加")


def downgrade():
    # 删除索引
    op.drop_index('idx_chunk_section_type', table_name='document_chunks')
    op.drop_index('idx_chunk_parent', table_name='document_chunks')
    op.drop_index('idx_chunk_level', table_name='document_chunks')
    
    # 删除字段
    op.drop_column('document_chunks', 'semantic_score')
    op.drop_column('document_chunks', 'has_citations')
    op.drop_column('document_chunks', 'parent_chunk_id')
    op.drop_column('document_chunks', 'section_title')
    op.drop_column('document_chunks', 'section_type')
    op.drop_column('document_chunks', 'chunk_level')
    
    print("✓ 层级分块支持已移除")
