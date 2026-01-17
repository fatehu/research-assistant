"""add react_steps to messages

Revision ID: 003_react_steps
Revises: 002_knowledge
Create Date: 2024-01-17

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

# revision identifiers, used by Alembic.
revision = '003_react_steps'
down_revision = '002_knowledge'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 添加 react_steps 列到 messages 表
    op.add_column('messages', sa.Column('react_steps', JSON, nullable=True))


def downgrade() -> None:
    # 删除 react_steps 列
    op.drop_column('messages', 'react_steps')
