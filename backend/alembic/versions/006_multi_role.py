"""Multi-role user system

Revision ID: 006_multi_role
Revises: 005_notebook
Create Date: 2025-01-28

使用 VARCHAR 代替 PostgreSQL ENUM 类型，避免类型创建冲突问题。
枚举值验证在应用层（Pydantic/SQLAlchemy模型）进行。
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from datetime import datetime

revision: str = '006_multi_role'
down_revision: Union[str, None] = '005_notebook'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ========== 扩展用户表 ==========
    # role: 'admin', 'mentor', 'student'
    op.add_column('users', sa.Column('role', sa.String(20), server_default='student', nullable=False))
    op.add_column('users', sa.Column('mentor_id', sa.Integer(), nullable=True))
    op.add_column('users', sa.Column('department', sa.String(200), nullable=True))
    op.add_column('users', sa.Column('research_direction', sa.String(500), nullable=True))
    op.add_column('users', sa.Column('joined_at', sa.DateTime(), nullable=True))
    
    # 添加外键约束和索引
    op.create_foreign_key(
        'fk_users_mentor_id',
        'users', 'users',
        ['mentor_id'], ['id'],
        ondelete='SET NULL'
    )
    op.create_index('ix_users_mentor_id', 'users', ['mentor_id'])
    op.create_index('ix_users_role', 'users', ['role'])
    
    # ========== 创建研究组表 ==========
    op.create_table(
        'research_groups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('mentor_id', sa.Integer(), nullable=False),
        sa.Column('avatar', sa.String(500), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true'),
        sa.Column('max_members', sa.Integer(), server_default='20'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['mentor_id'], ['users.id'], ondelete='CASCADE')
    )
    op.create_index('ix_research_groups_mentor_id', 'research_groups', ['mentor_id'])
    
    # ========== 创建组成员表 ==========
    op.create_table(
        'group_members',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(50), server_default='member'),  # 'admin', 'member'
        sa.Column('joined_at', sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['group_id'], ['research_groups.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('group_id', 'user_id', name='uq_group_members_group_user')
    )
    op.create_index('ix_group_members_group_id', 'group_members', ['group_id'])
    op.create_index('ix_group_members_user_id', 'group_members', ['user_id'])
    
    # ========== 创建邀请表 ==========
    # type: 'invite', 'apply'
    # status: 'pending', 'accepted', 'rejected', 'cancelled'
    op.create_table(
        'invitations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(20), nullable=False),
        sa.Column('from_user_id', sa.Integer(), nullable=False),
        sa.Column('to_user_id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('status', sa.String(20), server_default='pending', nullable=False),
        sa.Column('responded_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['from_user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['to_user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['research_groups.id'], ondelete='CASCADE')
    )
    op.create_index('ix_invitations_from_user', 'invitations', ['from_user_id'])
    op.create_index('ix_invitations_to_user', 'invitations', ['to_user_id'])
    op.create_index('ix_invitations_status', 'invitations', ['status'])
    
    # ========== 创建资源共享表 ==========
    # resource_type: 'knowledge_base', 'paper_collection', 'notebook'
    # shared_with_type: 'user', 'group', 'all_students'
    # permission: 'read', 'write', 'admin'
    op.create_table(
        'shared_resources',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('resource_type', sa.String(30), nullable=False),
        sa.Column('resource_id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('shared_with_type', sa.String(20), nullable=False),
        sa.Column('shared_with_id', sa.Integer(), nullable=True),
        sa.Column('permission', sa.String(20), server_default='read', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE')
    )
    op.create_index('ix_shared_resources_owner', 'shared_resources', ['owner_id'])
    op.create_index('ix_shared_resources_type', 'shared_resources', ['resource_type'])
    
    # ========== 创建公告表 ==========
    op.create_table(
        'announcements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('mentor_id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('is_pinned', sa.Boolean(), server_default='false'),
        sa.Column('is_active', sa.Boolean(), server_default='true'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['mentor_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['research_groups.id'], ondelete='SET NULL')
    )
    op.create_index('ix_announcements_mentor', 'announcements', ['mentor_id'])
    
    # ========== 创建公告已读表 ==========
    op.create_table(
        'announcement_reads',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('announcement_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('read_at', sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['announcement_id'], ['announcements.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('announcement_id', 'user_id', name='uq_announcement_reads')
    )


def downgrade() -> None:
    # 删除表（按依赖顺序）
    op.drop_table('announcement_reads')
    op.drop_table('announcements')
    op.drop_table('shared_resources')
    op.drop_table('invitations')
    op.drop_table('group_members')
    op.drop_table('research_groups')
    
    # 删除用户表的新列
    op.drop_constraint('fk_users_mentor_id', 'users', type_='foreignkey')
    op.drop_index('ix_users_mentor_id', 'users')
    op.drop_index('ix_users_role', 'users')
    op.drop_column('users', 'joined_at')
    op.drop_column('users', 'research_direction')
    op.drop_column('users', 'department')
    op.drop_column('users', 'mentor_id')
    op.drop_column('users', 'role')
