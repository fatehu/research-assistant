"""
025_research_projects

Add research project entry tables:
- research_projects
- research_project_papers
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "025_research_projects"
down_revision: Union[str, None] = "024_paper_experiment_workspace"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _index_exists(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return any(item.get("name") == index_name for item in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "research_projects"):
        op.create_table(
            "research_projects",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column("goal", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
            sa.Column("summary_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(bind)
    for index_name, columns in (
        ("ix_research_projects_id", ["id"]),
        ("ix_research_projects_user_id", ["user_id"]),
        ("ix_research_projects_status", ["status"]),
        ("idx_research_project_user_updated_at", ["user_id", "updated_at"]),
        ("idx_research_project_user_status", ["user_id", "status"]),
    ):
        if not _index_exists(inspector, "research_projects", index_name):
            op.create_index(index_name, "research_projects", columns, unique=False)
            inspector = sa.inspect(bind)

    if not _table_exists(inspector, "research_project_papers"):
        op.create_table(
            "research_project_papers",
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("paper_id", sa.Integer(), nullable=False),
            sa.Column("added_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["research_projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("project_id", "paper_id"),
        )

    inspector = sa.inspect(bind)
    for index_name, columns in (
        ("idx_research_project_papers_project", ["project_id"]),
        ("idx_research_project_papers_paper", ["paper_id"]),
    ):
        if not _index_exists(inspector, "research_project_papers", index_name):
            op.create_index(index_name, "research_project_papers", columns, unique=False)
            inspector = sa.inspect(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "research_project_papers"):
        for index_name in (
            "idx_research_project_papers_paper",
            "idx_research_project_papers_project",
        ):
            if _index_exists(inspector, "research_project_papers", index_name):
                op.drop_index(index_name, table_name="research_project_papers")
                inspector = sa.inspect(bind)
        op.drop_table("research_project_papers")
        inspector = sa.inspect(bind)

    if _table_exists(inspector, "research_projects"):
        for index_name in (
            "idx_research_project_user_status",
            "idx_research_project_user_updated_at",
            "ix_research_projects_status",
            "ix_research_projects_user_id",
            "ix_research_projects_id",
        ):
            if _index_exists(inspector, "research_projects", index_name):
                op.drop_index(index_name, table_name="research_projects")
                inspector = sa.inspect(bind)
        op.drop_table("research_projects")
