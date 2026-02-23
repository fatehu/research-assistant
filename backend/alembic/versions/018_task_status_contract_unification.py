"""
018_task_status_contract_unification

Unify task status contract to:
pending | running | completed | failed | timeout | cancelled

Targets:
- documents.status
- paper_knowledge_links.status
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "018_task_status_contract"
down_revision: Union[str, None] = "017_status_normalization"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TARGET_SET = "'pending', 'running', 'completed', 'failed', 'timeout', 'cancelled'"


def upgrade() -> None:
    bind = op.get_bind()

    bind.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS status_migration_audit_018 (
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
            f"""
            WITH updated AS (
                UPDATE documents
                SET
                    status = CASE
                        WHEN status IS NULL THEN 'failed'
                        WHEN lower(status) = 'processing' THEN 'running'
                        WHEN lower(status) = 'queued' THEN 'pending'
                        WHEN lower(status) IN ('ready', 'success', 'done') THEN 'completed'
                        WHEN lower(status) = 'canceled' THEN 'cancelled'
                        WHEN lower(status) IN ({_TARGET_SET}) THEN lower(status)
                        ELSE 'failed'
                    END,
                    error_message = CASE
                        WHEN (
                            status IS NULL
                            OR lower(status) NOT IN ({_TARGET_SET}, 'processing', 'queued', 'ready', 'success', 'done', 'canceled')
                        ) AND (error_message IS NULL OR btrim(error_message) = '')
                        THEN '状态迁移归一化(018): 原状态=' || COALESCE(status, 'NULL')
                        ELSE error_message
                    END,
                    updated_at = NOW()
                WHERE status IS NULL OR lower(status) NOT IN ({_TARGET_SET})
                RETURNING 1
            )
            SELECT count(*) FROM updated
            """
        )
    ).scalar_one()

    normalized_links = bind.execute(
        sa.text(
            f"""
            WITH updated AS (
                UPDATE paper_knowledge_links
                SET
                    status = CASE
                        WHEN status IS NULL THEN 'failed'
                        WHEN lower(status) = 'processing' THEN 'running'
                        WHEN lower(status) = 'ready' THEN 'completed'
                        WHEN lower(status) = 'queued' THEN 'pending'
                        WHEN lower(status) IN ('success', 'done') THEN 'completed'
                        WHEN lower(status) = 'canceled' THEN 'cancelled'
                        WHEN lower(status) IN ({_TARGET_SET}) THEN lower(status)
                        ELSE 'failed'
                    END,
                    error_message = CASE
                        WHEN (
                            status IS NULL
                            OR lower(status) NOT IN ({_TARGET_SET}, 'processing', 'ready', 'queued', 'success', 'done', 'canceled')
                        ) AND (error_message IS NULL OR btrim(error_message) = '')
                        THEN '状态迁移归一化(018): 原状态=' || COALESCE(status, 'NULL')
                        ELSE error_message
                    END,
                    updated_at = NOW()
                WHERE status IS NULL OR lower(status) NOT IN ({_TARGET_SET})
                RETURNING 1
            )
            SELECT count(*) FROM updated
            """
        )
    ).scalar_one()

    bind.execute(
        sa.text(
            """
            INSERT INTO status_migration_audit_018(entity_name, normalized_count, notes)
            VALUES
              ('documents', :doc_count, 'legacy -> pending|running|completed|failed|timeout|cancelled'),
              ('paper_knowledge_links', :link_count, 'legacy -> pending|running|completed|failed|timeout|cancelled')
            """
        ),
        {"doc_count": int(normalized_documents or 0), "link_count": int(normalized_links or 0)},
    )


def downgrade() -> None:
    bind = op.get_bind()

    bind.execute(
        sa.text(
            """
            UPDATE documents
            SET
                status = CASE
                    WHEN lower(status) = 'running' THEN 'processing'
                    WHEN lower(status) = 'cancelled' THEN 'failed'
                    WHEN lower(status) = 'timeout' THEN 'failed'
                    WHEN lower(status) IN ('pending', 'completed', 'failed') THEN lower(status)
                    ELSE 'failed'
                END,
                updated_at = NOW()
            """
        )
    )

    bind.execute(
        sa.text(
            """
            UPDATE paper_knowledge_links
            SET
                status = CASE
                    WHEN lower(status) = 'running' THEN 'processing'
                    WHEN lower(status) = 'completed' THEN 'ready'
                    WHEN lower(status) = 'cancelled' THEN 'failed'
                    WHEN lower(status) = 'timeout' THEN 'failed'
                    WHEN lower(status) IN ('pending', 'failed') THEN lower(status)
                    ELSE 'failed'
                END,
                updated_at = NOW()
            """
        )
    )

    bind.execute(sa.text("DROP TABLE IF EXISTS status_migration_audit_018"))
