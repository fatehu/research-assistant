"""
004: 添加文献管理相关表

Revision ID: 004_literature
Revises: 003_react_steps
Create Date: 2024-01-20

Tables:
- papers: 论文表
- paper_collections: 收藏夹表
- paper_collection: 论文-收藏夹关联表
- paper_citations: 论文引用关系表
- paper_search_history: 搜索历史表
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = '004_literature'
down_revision = '003_react_steps'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 创建收藏夹表
    op.create_table(
        'paper_collections',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('color', sa.String(20), default='#3b82f6'),
        sa.Column('icon', sa.String(50), default='folder'),
        sa.Column('collection_type', sa.String(50), default='custom'),
        sa.Column('is_default', sa.Boolean(), default=False),
        sa.Column('paper_count', sa.Integer(), default=0),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_paper_collections_user_id', 'paper_collections', ['user_id'])
    
    # 创建论文表
    op.create_table(
        'papers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        
        # 外部标识符
        sa.Column('semantic_scholar_id', sa.String(100), nullable=True),
        sa.Column('arxiv_id', sa.String(50), nullable=True),
        sa.Column('doi', sa.String(200), nullable=True),
        sa.Column('pubmed_id', sa.String(50), nullable=True),
        
        # 基本信息
        sa.Column('title', sa.String(1000), nullable=False),
        sa.Column('abstract', sa.Text(), nullable=True),
        sa.Column('authors', postgresql.JSON(), default=[]),
        
        # 发表信息
        sa.Column('year', sa.Integer(), nullable=True),
        sa.Column('venue', sa.String(500), nullable=True),
        sa.Column('journal', sa.String(500), nullable=True),
        sa.Column('volume', sa.String(50), nullable=True),
        sa.Column('pages', sa.String(50), nullable=True),
        sa.Column('publisher', sa.String(200), nullable=True),
        
        # URL 链接
        sa.Column('url', sa.String(1000), nullable=True),
        sa.Column('pdf_url', sa.String(1000), nullable=True),
        sa.Column('arxiv_url', sa.String(500), nullable=True),
        
        # 本地存储
        sa.Column('pdf_path', sa.String(1000), nullable=True),
        sa.Column('pdf_downloaded', sa.Boolean(), default=False),
        
        # 知识库关联
        sa.Column('knowledge_base_id', sa.Integer(), nullable=True),
        sa.Column('document_id', sa.Integer(), nullable=True),
        
        # 统计信息
        sa.Column('citation_count', sa.Integer(), default=0),
        sa.Column('reference_count', sa.Integer(), default=0),
        sa.Column('influential_citation_count', sa.Integer(), default=0),
        
        # 分类和标签
        sa.Column('fields_of_study', postgresql.JSON(), default=[]),
        sa.Column('tags', postgresql.JSON(), default=[]),
        
        # 阅读状态
        sa.Column('is_read', sa.Boolean(), default=False),
        sa.Column('read_at', sa.DateTime(), nullable=True),
        
        # 用户笔记
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('rating', sa.Integer(), nullable=True),
        
        # 元数据
        sa.Column('source', sa.String(50), default='semantic_scholar'),
        sa.Column('raw_data', postgresql.JSON(), default={}),
        
        # 时间戳
        sa.Column('published_date', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
        
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'semantic_scholar_id', name='uq_user_s2_id'),
        sa.UniqueConstraint('user_id', 'arxiv_id', name='uq_user_arxiv_id')
    )
    op.create_index('ix_papers_user_id', 'papers', ['user_id'])
    op.create_index('ix_papers_semantic_scholar_id', 'papers', ['semantic_scholar_id'])
    op.create_index('ix_papers_arxiv_id', 'papers', ['arxiv_id'])
    op.create_index('ix_papers_doi', 'papers', ['doi'])
    
    # 创建论文-收藏夹关联表
    op.create_table(
        'paper_collection',
        sa.Column('paper_id', sa.Integer(), nullable=False),
        sa.Column('collection_id', sa.Integer(), nullable=False),
        sa.Column('added_at', sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['collection_id'], ['paper_collections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('paper_id', 'collection_id')
    )
    
    # 创建论文引用关系表
    op.create_table(
        'paper_citations',
        sa.Column('citing_paper_id', sa.Integer(), nullable=False),
        sa.Column('cited_paper_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['citing_paper_id'], ['papers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['cited_paper_id'], ['papers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('citing_paper_id', 'cited_paper_id')
    )
    
    # 创建搜索历史表
    op.create_table(
        'paper_search_history',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('query', sa.String(500), nullable=False),
        sa.Column('source', sa.String(50), default='semantic_scholar'),
        sa.Column('result_count', sa.Integer(), default=0),
        sa.Column('filters', postgresql.JSON(), default={}),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_paper_search_history_user_id', 'paper_search_history', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_paper_search_history_user_id', table_name='paper_search_history')
    op.drop_table('paper_search_history')
    op.drop_table('paper_citations')
    op.drop_table('paper_collection')
    op.drop_index('ix_papers_doi', table_name='papers')
    op.drop_index('ix_papers_arxiv_id', table_name='papers')
    op.drop_index('ix_papers_semantic_scholar_id', table_name='papers')
    op.drop_index('ix_papers_user_id', table_name='papers')
    op.drop_table('papers')
    op.drop_index('ix_paper_collections_user_id', table_name='paper_collections')
    op.drop_table('paper_collections')
