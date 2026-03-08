"""
011_add_context_summary

Add context_summary column for contextual retrieval.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "011_context_summary"
down_revision: Union[str, None] = "010_content_segmented_fts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS context_summary TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS context_summary")
