"""
026_project_primary_workspace

Promote research projects from paper collections to orchestration roots:
- add primary_paper_id / primary_workspace_id to research_projects
- add role / notes to research_project_papers
- add research_project_workspaces association table
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "026_project_primary_workspace"
down_revision: Union[str, None] = "025_research_projects"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_exists(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return any(item.get("name") == column_name for item in inspector.get_columns(table_name))


def _index_exists(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return any(item.get("name") == index_name for item in inspector.get_indexes(table_name))


def _fk_exists(inspector: sa.Inspector, table_name: str, fk_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return any(item.get("name") == fk_name for item in inspector.get_foreign_keys(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "research_projects"):
        if not _column_exists(inspector, "research_projects", "primary_paper_id"):
            op.add_column("research_projects", sa.Column("primary_paper_id", sa.Integer(), nullable=True))
            inspector = sa.inspect(bind)
        if not _column_exists(inspector, "research_projects", "primary_workspace_id"):
            op.add_column("research_projects", sa.Column("primary_workspace_id", sa.Integer(), nullable=True))
            inspector = sa.inspect(bind)

        if not _fk_exists(inspector, "research_projects", "fk_research_projects_primary_paper"):
            op.create_foreign_key(
                "fk_research_projects_primary_paper",
                "research_projects",
                "papers",
                ["primary_paper_id"],
                ["id"],
                ondelete="SET NULL",
            )
            inspector = sa.inspect(bind)
        if not _fk_exists(inspector, "research_projects", "fk_research_projects_primary_workspace"):
            op.create_foreign_key(
                "fk_research_projects_primary_workspace",
                "research_projects",
                "paper_experiment_workspaces",
                ["primary_workspace_id"],
                ["id"],
                ondelete="SET NULL",
            )
            inspector = sa.inspect(bind)

        for index_name, columns in (
            ("ix_research_projects_primary_paper_id", ["primary_paper_id"]),
            ("ix_research_projects_primary_workspace_id", ["primary_workspace_id"]),
        ):
            if not _index_exists(inspector, "research_projects", index_name):
                op.create_index(index_name, "research_projects", columns, unique=False)
                inspector = sa.inspect(bind)

    if _table_exists(inspector, "research_project_papers"):
        if not _column_exists(inspector, "research_project_papers", "role"):
            op.add_column(
                "research_project_papers",
                sa.Column("role", sa.String(length=32), nullable=False, server_default="related"),
            )
            inspector = sa.inspect(bind)
        if not _column_exists(inspector, "research_project_papers", "notes"):
            op.add_column(
                "research_project_papers",
                sa.Column("notes", sa.Text(), nullable=True),
            )
            inspector = sa.inspect(bind)

    if not _table_exists(inspector, "research_project_workspaces"):
        op.create_table(
            "research_project_workspaces",
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("workspace_id", sa.Integer(), nullable=False),
            sa.Column("paper_id", sa.Integer(), nullable=True),
            sa.Column("role", sa.String(length=40), nullable=False, server_default="related_reproduction"),
            sa.Column("added_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["project_id"], ["research_projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["workspace_id"], ["paper_experiment_workspaces.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("project_id", "workspace_id"),
        )
        inspector = sa.inspect(bind)

    for index_name, columns in (
        ("idx_research_project_workspaces_project", ["project_id"]),
        ("idx_research_project_workspaces_workspace", ["workspace_id"]),
        ("idx_research_project_workspaces_paper", ["paper_id"]),
    ):
        if not _index_exists(inspector, "research_project_workspaces", index_name):
            op.create_index(index_name, "research_project_workspaces", columns, unique=False)
            inspector = sa.inspect(bind)

    if _table_exists(inspector, "research_project_papers") and _table_exists(inspector, "research_projects"):
        op.execute(
            sa.text(
                """
                UPDATE research_projects AS rp
                SET primary_paper_id = sub.paper_id
                FROM (
                    SELECT project_id, MIN(paper_id) AS paper_id
                    FROM research_project_papers
                    GROUP BY project_id
                ) AS sub
                WHERE rp.id = sub.project_id
                  AND rp.primary_paper_id IS NULL
                """
            )
        )
        op.execute(sa.text("UPDATE research_project_papers SET role = 'related' WHERE role IS NULL OR role = ''"))
        op.execute(
            sa.text(
                """
                UPDATE research_project_papers AS rpp
                SET role = 'primary'
                FROM research_projects AS rp
                WHERE rp.id = rpp.project_id
                  AND rp.primary_paper_id = rpp.paper_id
                """
            )
        )

    if (
        _table_exists(inspector, "research_projects")
        and _table_exists(inspector, "paper_experiment_workspaces")
        and _column_exists(inspector, "research_projects", "primary_workspace_id")
    ):
        op.execute(
            sa.text(
                """
                UPDATE research_projects AS rp
                SET primary_workspace_id = pew.id
                FROM paper_experiment_workspaces AS pew
                WHERE rp.primary_paper_id IS NOT NULL
                  AND rp.primary_workspace_id IS NULL
                  AND pew.paper_id = rp.primary_paper_id
                  AND pew.user_id = rp.user_id
                """
            )
        )

    if _table_exists(inspector, "research_project_workspaces") and _table_exists(inspector, "research_projects"):
        op.execute(
            sa.text(
                """
                INSERT INTO research_project_workspaces (project_id, workspace_id, paper_id, role, added_at)
                SELECT rp.id, rp.primary_workspace_id, rp.primary_paper_id, 'primary_reproduction', NOW()
                FROM research_projects AS rp
                WHERE rp.primary_workspace_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM research_project_workspaces AS rw
                      WHERE rw.project_id = rp.id
                        AND rw.workspace_id = rp.primary_workspace_id
                  )
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "research_project_workspaces"):
        for index_name in (
            "idx_research_project_workspaces_paper",
            "idx_research_project_workspaces_workspace",
            "idx_research_project_workspaces_project",
        ):
            if _index_exists(inspector, "research_project_workspaces", index_name):
                op.drop_index(index_name, table_name="research_project_workspaces")
                inspector = sa.inspect(bind)
        op.drop_table("research_project_workspaces")
        inspector = sa.inspect(bind)

    if _table_exists(inspector, "research_project_papers"):
        if _column_exists(inspector, "research_project_papers", "notes"):
            op.drop_column("research_project_papers", "notes")
            inspector = sa.inspect(bind)
        if _column_exists(inspector, "research_project_papers", "role"):
            op.drop_column("research_project_papers", "role")
            inspector = sa.inspect(bind)

    if _table_exists(inspector, "research_projects"):
        for index_name in (
            "ix_research_projects_primary_workspace_id",
            "ix_research_projects_primary_paper_id",
        ):
            if _index_exists(inspector, "research_projects", index_name):
                op.drop_index(index_name, table_name="research_projects")
                inspector = sa.inspect(bind)
        for fk_name in (
            "fk_research_projects_primary_workspace",
            "fk_research_projects_primary_paper",
        ):
            if _fk_exists(inspector, "research_projects", fk_name):
                op.drop_constraint(fk_name, "research_projects", type_="foreignkey")
                inspector = sa.inspect(bind)
        if _column_exists(inspector, "research_projects", "primary_workspace_id"):
            op.drop_column("research_projects", "primary_workspace_id")
            inspector = sa.inspect(bind)
        if _column_exists(inspector, "research_projects", "primary_paper_id"):
            op.drop_column("research_projects", "primary_paper_id")
