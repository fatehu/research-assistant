"""
017_status_normalization_and_conflict_cleanup

Normalize legacy/invalid status values for:
- documents.status
- paper_knowledge_links.status

This migration is intended for test-stage data cleanup where backward compatibility
is not required and conflicting legacy states should be migrated forward.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "017_status_normalization"
down_revision: Union[str, None] = "016_literature_reader_and_qa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # 审计表：记录本次归一化处理的影响行数，便于追溯。
    bind.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS status_migration_audit_017 (
                id SERIAL PRIMARY KEY,
                entity_name VARCHAR(64) NOT NULL,
                normalized_count INTEGER NOT NULL DEFAULT 0,
                normalized_at TIMESTAMP NOT NULL DEFAULT NOW(),
                notes TEXT NULL
            )
            """
        )
    )

    normalized_documents = bind.execute(
        sa.text(
            """
            WITH updated AS (
                UPDATE documents
                SET
                    status = CASE
                        WHEN status IS NULL THEN 'failed'
                        WHEN lower(status) = 'running' THEN 'processing'
                        WHEN lower(status) = 'queued' THEN 'pending'
                        WHEN lower(status) IN ('ready', 'success', 'done') THEN 'completed'
                        WHEN lower(status) IN ('timeout', 'cancelled', 'canceled', 'error') THEN 'failed'
                        WHEN lower(status) IN ('pending', 'processing', 'completed', 'failed') THEN lower(status)
                        ELSE 'failed'
                    END,
                    error_message = CASE
                        WHEN (
                            status IS NULL
                            OR lower(status) IN ('timeout', 'cancelled', 'canceled', 'error')
                            OR lower(status) NOT IN ('pending', 'processing', 'completed', 'failed', 'running', 'queued', 'ready', 'success', 'done')
                        )
                        AND (error_message IS NULL OR btrim(error_message) = '')
                        THEN '状态迁移归一化: 原状态=' || COALESCE(status, 'NULL')
                        ELSE error_message
                    END,
                    updated_at = NOW()
                WHERE status IS NULL OR lower(status) NOT IN ('pending', 'processing', 'completed', 'failed')
                RETURNING 1
            )
            SELECT count(*) FROM updated
            """
        )
    ).scalar_one()

    normalized_links = bind.execute(
        sa.text(
            """
            WITH updated AS (
                UPDATE paper_knowledge_links
                SET
                    status = CASE
                        WHEN status IS NULL THEN 'failed'
                        WHEN lower(status) = 'running' THEN 'processing'
                        WHEN lower(status) = 'queued' THEN 'pending'
                        WHEN lower(status) IN ('completed', 'success', 'done') THEN 'ready'
                        WHEN lower(status) IN ('timeout', 'cancelled', 'canceled', 'error') THEN 'failed'
                        WHEN lower(status) IN ('pending', 'processing', 'ready', 'failed') THEN lower(status)
                        ELSE 'failed'
                    END,
                    error_message = CASE
                        WHEN (
                            status IS NULL
                            OR lower(status) IN ('timeout', 'cancelled', 'canceled', 'error')
                            OR lower(status) NOT IN ('pending', 'processing', 'ready', 'failed', 'running', 'queued', 'completed', 'success', 'done')
                        )
                        AND (error_message IS NULL OR btrim(error_message) = '')
                        THEN '状态迁移归一化: 原状态=' || COALESCE(status, 'NULL')
                        ELSE error_message
                    END,
                    updated_at = NOW()
                WHERE status IS NULL OR lower(status) NOT IN ('pending', 'processing', 'ready', 'failed')
                RETURNING 1
            )
            SELECT count(*) FROM updated
            """
        )
    ).scalar_one()

    bind.execute(
        sa.text(
            """
            INSERT INTO status_migration_audit_017(entity_name, normalized_count, notes)
            VALUES
              ('documents', :doc_count, 'invalid/legacy -> pending|processing|completed|failed'),
              ('paper_knowledge_links', :link_count, 'invalid/legacy -> pending|processing|ready|failed')
            """
        ),
        {"doc_count": int(normalized_documents or 0), "link_count": int(normalized_links or 0)},
    )


def downgrade() -> None:
    # 数据归一化是前向迁移，不尝试自动还原原状态值。
    # downgrade 仅移除本次审计表，避免破坏当前可用状态。
    bind = op.get_bind()
    bind.execute(sa.text("DROP TABLE IF EXISTS status_migration_audit_017"))
