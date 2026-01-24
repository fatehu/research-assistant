"""add notebook tables

Revision ID: 005_notebook
Revises: 004_literature
Create Date: 2026-01-24

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '005_notebook'
down_revision = '004_literature'
branch_labels = None
depends_on = None


def upgrade():
    # 创建 notebooks 表
    op.create_table(
        'notebooks',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False, index=True),
        sa.Column('title', sa.String(255), default='Untitled Notebook'),
        sa.Column('description', sa.Text, nullable=True),
        sa.Column('execution_count', sa.Integer, default=0),
        sa.Column('metadata', sa.JSON, default=dict),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    
    # 创建 notebook_cells 表
    op.create_table(
        'notebook_cells',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('notebook_id', sa.String(36), sa.ForeignKey('notebooks.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('cell_type', sa.String(20), default='code'),
        sa.Column('source', sa.Text, default=''),
        sa.Column('execution_count', sa.Integer, nullable=True),
        sa.Column('outputs', sa.JSON, default=list),
        sa.Column('metadata', sa.JSON, default=dict),
        sa.Column('position', sa.Integer, default=0, index=True),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def downgrade():
    op.drop_table('notebook_cells')
    op.drop_table('notebooks')
