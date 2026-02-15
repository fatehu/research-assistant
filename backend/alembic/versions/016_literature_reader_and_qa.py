"""
016_literature_reader_and_qa

Add literature reader/social/qa tables:
- paper_entities
- paper_read_sessions
- paper_annotations
- paper_comments
- paper_ratings
- paper_knowledge_links
- literature_qa_sessions
- literature_qa_messages

And extend papers:
- papers.paper_entity_id
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "016_literature_reader_and_qa"
down_revision: Union[str, None] = "015_agent_core_runtime"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _index_exists(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return any(item.get("name") == index_name for item in inspector.get_indexes(table_name))


def _column_exists(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return any(col.get("name") == column_name for col in inspector.get_columns(table_name))


def _fk_exists(inspector: sa.Inspector, table_name: str, fk_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return any(item.get("name") == fk_name for item in inspector.get_foreign_keys(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "paper_entities"):
        op.create_table(
            "paper_entities",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("canonical_key", sa.String(length=300), nullable=False),
            sa.Column("doi_norm", sa.String(length=200), nullable=True),
            sa.Column("arxiv_norm", sa.String(length=80), nullable=True),
            sa.Column("title_norm", sa.String(length=1200), nullable=True),
            sa.Column("year", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("canonical_key", name="uq_paper_entities_canonical_key"),
        )

    inspector = sa.inspect(bind)
    if not _index_exists(inspector, "paper_entities", "ix_paper_entities_id"):
        op.create_index("ix_paper_entities_id", "paper_entities", ["id"], unique=False)
    if not _index_exists(inspector, "paper_entities", "ix_paper_entities_canonical_key"):
        op.create_index("ix_paper_entities_canonical_key", "paper_entities", ["canonical_key"], unique=False)
    if not _index_exists(inspector, "paper_entities", "ix_paper_entities_doi_norm"):
        op.create_index("ix_paper_entities_doi_norm", "paper_entities", ["doi_norm"], unique=False)
    if not _index_exists(inspector, "paper_entities", "ix_paper_entities_arxiv_norm"):
        op.create_index("ix_paper_entities_arxiv_norm", "paper_entities", ["arxiv_norm"], unique=False)
    if not _index_exists(inspector, "paper_entities", "ix_paper_entities_title_norm"):
        op.create_index("ix_paper_entities_title_norm", "paper_entities", ["title_norm"], unique=False)

    if _table_exists(inspector, "papers") and not _column_exists(inspector, "papers", "paper_entity_id"):
        op.add_column("papers", sa.Column("paper_entity_id", sa.Integer(), nullable=True))
        inspector = sa.inspect(bind)
        if not _fk_exists(inspector, "papers", "fk_papers_paper_entity_id_paper_entities"):
            op.create_foreign_key(
                "fk_papers_paper_entity_id_paper_entities",
                "papers",
                "paper_entities",
                ["paper_entity_id"],
                ["id"],
                ondelete="SET NULL",
            )
        if not _index_exists(inspector, "papers", "ix_papers_paper_entity_id"):
            op.create_index("ix_papers_paper_entity_id", "papers", ["paper_entity_id"], unique=False)

    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "paper_read_sessions"):
        op.create_table(
            "paper_read_sessions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("paper_id", sa.Integer(), nullable=False),
            sa.Column("page", sa.Integer(), nullable=True, server_default="1"),
            sa.Column("zoom", sa.String(length=20), nullable=True, server_default="100%"),
            sa.Column("scroll_y", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("selected_kb_id", sa.Integer(), nullable=True),
            sa.Column("last_anchor", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["selected_kb_id"], ["knowledge_bases.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "paper_id", name="uq_read_session_user_paper"),
        )
    inspector = sa.inspect(bind)
    if not _index_exists(inspector, "paper_read_sessions", "ix_paper_read_sessions_id"):
        op.create_index("ix_paper_read_sessions_id", "paper_read_sessions", ["id"], unique=False)
    if not _index_exists(inspector, "paper_read_sessions", "ix_paper_read_sessions_user_id"):
        op.create_index("ix_paper_read_sessions_user_id", "paper_read_sessions", ["user_id"], unique=False)
    if not _index_exists(inspector, "paper_read_sessions", "ix_paper_read_sessions_paper_id"):
        op.create_index("ix_paper_read_sessions_paper_id", "paper_read_sessions", ["paper_id"], unique=False)

    if not _table_exists(inspector, "paper_annotations"):
        op.create_table(
            "paper_annotations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("paper_id", sa.Integer(), nullable=False),
            sa.Column("annotation_type", sa.String(length=20), nullable=False, server_default="highlight"),
            sa.Column("page", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("quote_text", sa.Text(), nullable=True),
            sa.Column("anchor_json", sa.JSON(), nullable=True),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("color", sa.String(length=20), nullable=True, server_default="#f59e0b"),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(bind)
    if not _index_exists(inspector, "paper_annotations", "ix_paper_annotations_id"):
        op.create_index("ix_paper_annotations_id", "paper_annotations", ["id"], unique=False)
    if not _index_exists(inspector, "paper_annotations", "ix_paper_annotations_user_id"):
        op.create_index("ix_paper_annotations_user_id", "paper_annotations", ["user_id"], unique=False)
    if not _index_exists(inspector, "paper_annotations", "ix_paper_annotations_paper_id"):
        op.create_index("ix_paper_annotations_paper_id", "paper_annotations", ["paper_id"], unique=False)
    if not _index_exists(inspector, "paper_annotations", "ix_paper_annotations_page"):
        op.create_index("ix_paper_annotations_page", "paper_annotations", ["page"], unique=False)
    if not _index_exists(inspector, "paper_annotations", "idx_paper_annotations_user_paper_page"):
        op.create_index(
            "idx_paper_annotations_user_paper_page",
            "paper_annotations",
            ["user_id", "paper_id", "page"],
            unique=False,
        )

    if not _table_exists(inspector, "paper_comments"):
        op.create_table(
            "paper_comments",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("paper_entity_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("parent_id", sa.Integer(), nullable=True),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["paper_entity_id"], ["paper_entities.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["parent_id"], ["paper_comments.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(bind)
    if not _index_exists(inspector, "paper_comments", "ix_paper_comments_id"):
        op.create_index("ix_paper_comments_id", "paper_comments", ["id"], unique=False)
    if not _index_exists(inspector, "paper_comments", "ix_paper_comments_paper_entity_id"):
        op.create_index("ix_paper_comments_paper_entity_id", "paper_comments", ["paper_entity_id"], unique=False)
    if not _index_exists(inspector, "paper_comments", "ix_paper_comments_user_id"):
        op.create_index("ix_paper_comments_user_id", "paper_comments", ["user_id"], unique=False)
    if not _index_exists(inspector, "paper_comments", "ix_paper_comments_parent_id"):
        op.create_index("ix_paper_comments_parent_id", "paper_comments", ["parent_id"], unique=False)
    if not _index_exists(inspector, "paper_comments", "ix_paper_comments_deleted_at"):
        op.create_index("ix_paper_comments_deleted_at", "paper_comments", ["deleted_at"], unique=False)
    if not _index_exists(inspector, "paper_comments", "ix_paper_comments_created_at"):
        op.create_index("ix_paper_comments_created_at", "paper_comments", ["created_at"], unique=False)

    if not _table_exists(inspector, "paper_ratings"):
        op.create_table(
            "paper_ratings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("paper_entity_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("rating", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["paper_entity_id"], ["paper_entities.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("paper_entity_id", "user_id", name="uq_paper_rating_entity_user"),
            sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_paper_ratings_rating_range"),
        )
    inspector = sa.inspect(bind)
    if not _index_exists(inspector, "paper_ratings", "ix_paper_ratings_id"):
        op.create_index("ix_paper_ratings_id", "paper_ratings", ["id"], unique=False)
    if not _index_exists(inspector, "paper_ratings", "ix_paper_ratings_paper_entity_id"):
        op.create_index("ix_paper_ratings_paper_entity_id", "paper_ratings", ["paper_entity_id"], unique=False)
    if not _index_exists(inspector, "paper_ratings", "ix_paper_ratings_user_id"):
        op.create_index("ix_paper_ratings_user_id", "paper_ratings", ["user_id"], unique=False)

    if not _table_exists(inspector, "paper_knowledge_links"):
        op.create_table(
            "paper_knowledge_links",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("paper_id", sa.Integer(), nullable=False),
            sa.Column("knowledge_base_id", sa.Integer(), nullable=False),
            sa.Column("document_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "paper_id", "knowledge_base_id", name="uq_paper_kb_link_user_paper_kb"),
        )
    inspector = sa.inspect(bind)
    if not _index_exists(inspector, "paper_knowledge_links", "ix_paper_knowledge_links_id"):
        op.create_index("ix_paper_knowledge_links_id", "paper_knowledge_links", ["id"], unique=False)
    if not _index_exists(inspector, "paper_knowledge_links", "ix_paper_knowledge_links_user_id"):
        op.create_index("ix_paper_knowledge_links_user_id", "paper_knowledge_links", ["user_id"], unique=False)
    if not _index_exists(inspector, "paper_knowledge_links", "ix_paper_knowledge_links_paper_id"):
        op.create_index("ix_paper_knowledge_links_paper_id", "paper_knowledge_links", ["paper_id"], unique=False)
    if not _index_exists(inspector, "paper_knowledge_links", "ix_paper_knowledge_links_knowledge_base_id"):
        op.create_index("ix_paper_knowledge_links_knowledge_base_id", "paper_knowledge_links", ["knowledge_base_id"], unique=False)
    if not _index_exists(inspector, "paper_knowledge_links", "ix_paper_knowledge_links_document_id"):
        op.create_index("ix_paper_knowledge_links_document_id", "paper_knowledge_links", ["document_id"], unique=False)
    if not _index_exists(inspector, "paper_knowledge_links", "ix_paper_knowledge_links_status"):
        op.create_index("ix_paper_knowledge_links_status", "paper_knowledge_links", ["status"], unique=False)

    if not _table_exists(inspector, "literature_qa_sessions"):
        op.create_table(
            "literature_qa_sessions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("scope", sa.String(length=20), nullable=False, server_default="paper"),
            sa.Column("paper_id", sa.Integer(), nullable=True),
            sa.Column("collection_id", sa.Integer(), nullable=True),
            sa.Column("knowledge_base_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=300), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["collection_id"], ["paper_collections.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(bind)
    if not _index_exists(inspector, "literature_qa_sessions", "ix_literature_qa_sessions_id"):
        op.create_index("ix_literature_qa_sessions_id", "literature_qa_sessions", ["id"], unique=False)
    if not _index_exists(inspector, "literature_qa_sessions", "ix_literature_qa_sessions_user_id"):
        op.create_index("ix_literature_qa_sessions_user_id", "literature_qa_sessions", ["user_id"], unique=False)
    if not _index_exists(inspector, "literature_qa_sessions", "ix_literature_qa_sessions_paper_id"):
        op.create_index("ix_literature_qa_sessions_paper_id", "literature_qa_sessions", ["paper_id"], unique=False)
    if not _index_exists(inspector, "literature_qa_sessions", "ix_literature_qa_sessions_collection_id"):
        op.create_index("ix_literature_qa_sessions_collection_id", "literature_qa_sessions", ["collection_id"], unique=False)
    if not _index_exists(inspector, "literature_qa_sessions", "ix_literature_qa_sessions_knowledge_base_id"):
        op.create_index("ix_literature_qa_sessions_knowledge_base_id", "literature_qa_sessions", ["knowledge_base_id"], unique=False)

    if not _table_exists(inspector, "literature_qa_messages"):
        op.create_table(
            "literature_qa_messages",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("session_id", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(length=20), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("sources", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["session_id"], ["literature_qa_sessions.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(bind)
    if not _index_exists(inspector, "literature_qa_messages", "ix_literature_qa_messages_id"):
        op.create_index("ix_literature_qa_messages_id", "literature_qa_messages", ["id"], unique=False)
    if not _index_exists(inspector, "literature_qa_messages", "ix_literature_qa_messages_session_id"):
        op.create_index("ix_literature_qa_messages_session_id", "literature_qa_messages", ["session_id"], unique=False)
    if not _index_exists(inspector, "literature_qa_messages", "ix_literature_qa_messages_created_at"):
        op.create_index("ix_literature_qa_messages_created_at", "literature_qa_messages", ["created_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "literature_qa_messages"):
        for idx in (
            "ix_literature_qa_messages_created_at",
            "ix_literature_qa_messages_session_id",
            "ix_literature_qa_messages_id",
        ):
            if _index_exists(inspector, "literature_qa_messages", idx):
                op.drop_index(idx, table_name="literature_qa_messages")
                inspector = sa.inspect(bind)
        op.drop_table("literature_qa_messages")
        inspector = sa.inspect(bind)

    if _table_exists(inspector, "literature_qa_sessions"):
        for idx in (
            "ix_literature_qa_sessions_knowledge_base_id",
            "ix_literature_qa_sessions_collection_id",
            "ix_literature_qa_sessions_paper_id",
            "ix_literature_qa_sessions_user_id",
            "ix_literature_qa_sessions_id",
        ):
            if _index_exists(inspector, "literature_qa_sessions", idx):
                op.drop_index(idx, table_name="literature_qa_sessions")
                inspector = sa.inspect(bind)
        op.drop_table("literature_qa_sessions")
        inspector = sa.inspect(bind)

    if _table_exists(inspector, "paper_knowledge_links"):
        for idx in (
            "ix_paper_knowledge_links_status",
            "ix_paper_knowledge_links_document_id",
            "ix_paper_knowledge_links_knowledge_base_id",
            "ix_paper_knowledge_links_paper_id",
            "ix_paper_knowledge_links_user_id",
            "ix_paper_knowledge_links_id",
        ):
            if _index_exists(inspector, "paper_knowledge_links", idx):
                op.drop_index(idx, table_name="paper_knowledge_links")
                inspector = sa.inspect(bind)
        op.drop_table("paper_knowledge_links")
        inspector = sa.inspect(bind)

    if _table_exists(inspector, "paper_ratings"):
        for idx in (
            "ix_paper_ratings_user_id",
            "ix_paper_ratings_paper_entity_id",
            "ix_paper_ratings_id",
        ):
            if _index_exists(inspector, "paper_ratings", idx):
                op.drop_index(idx, table_name="paper_ratings")
                inspector = sa.inspect(bind)
        op.drop_table("paper_ratings")
        inspector = sa.inspect(bind)

    if _table_exists(inspector, "paper_comments"):
        for idx in (
            "ix_paper_comments_created_at",
            "ix_paper_comments_deleted_at",
            "ix_paper_comments_parent_id",
            "ix_paper_comments_user_id",
            "ix_paper_comments_paper_entity_id",
            "ix_paper_comments_id",
        ):
            if _index_exists(inspector, "paper_comments", idx):
                op.drop_index(idx, table_name="paper_comments")
                inspector = sa.inspect(bind)
        op.drop_table("paper_comments")
        inspector = sa.inspect(bind)

    if _table_exists(inspector, "paper_annotations"):
        for idx in (
            "idx_paper_annotations_user_paper_page",
            "ix_paper_annotations_page",
            "ix_paper_annotations_paper_id",
            "ix_paper_annotations_user_id",
            "ix_paper_annotations_id",
        ):
            if _index_exists(inspector, "paper_annotations", idx):
                op.drop_index(idx, table_name="paper_annotations")
                inspector = sa.inspect(bind)
        op.drop_table("paper_annotations")
        inspector = sa.inspect(bind)

    if _table_exists(inspector, "paper_read_sessions"):
        for idx in (
            "ix_paper_read_sessions_paper_id",
            "ix_paper_read_sessions_user_id",
            "ix_paper_read_sessions_id",
        ):
            if _index_exists(inspector, "paper_read_sessions", idx):
                op.drop_index(idx, table_name="paper_read_sessions")
                inspector = sa.inspect(bind)
        op.drop_table("paper_read_sessions")
        inspector = sa.inspect(bind)

    if _table_exists(inspector, "papers") and _column_exists(inspector, "papers", "paper_entity_id"):
        if _index_exists(inspector, "papers", "ix_papers_paper_entity_id"):
            op.drop_index("ix_papers_paper_entity_id", table_name="papers")
        inspector = sa.inspect(bind)
        if _fk_exists(inspector, "papers", "fk_papers_paper_entity_id_paper_entities"):
            op.drop_constraint("fk_papers_paper_entity_id_paper_entities", "papers", type_="foreignkey")
        op.drop_column("papers", "paper_entity_id")
        inspector = sa.inspect(bind)

    if _table_exists(inspector, "paper_entities"):
        for idx in (
            "ix_paper_entities_title_norm",
            "ix_paper_entities_arxiv_norm",
            "ix_paper_entities_doi_norm",
            "ix_paper_entities_canonical_key",
            "ix_paper_entities_id",
        ):
            if _index_exists(inspector, "paper_entities", idx):
                op.drop_index(idx, table_name="paper_entities")
                inspector = sa.inspect(bind)
        op.drop_table("paper_entities")

