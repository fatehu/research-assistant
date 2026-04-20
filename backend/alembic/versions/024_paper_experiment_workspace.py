"""
024_paper_experiment_workspace

Add paper-backed experiment workspace persistence:
- paper_experiment_workspaces
- paper_experiment_runs
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "024_paper_experiment_workspace"
down_revision: Union[str, None] = "023_drop_chat_legacy_fields"
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

    if not _table_exists(inspector, "paper_experiment_workspaces"):
        op.create_table(
            "paper_experiment_workspaces",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("paper_id", sa.Integer(), nullable=False),
            sa.Column("notebook_id", sa.String(length=36), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column("summary_json", sa.JSON(), nullable=True),
            sa.Column("experiment_spec_json", sa.JSON(), nullable=True),
            sa.Column("compare_report_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["notebook_id"], ["notebooks.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "paper_id", name="uq_paper_experiment_workspace_user_paper"),
        )

    inspector = sa.inspect(bind)
    for index_name, columns in (
        ("ix_paper_experiment_workspaces_id", ["id"]),
        ("ix_paper_experiment_workspaces_user_id", ["user_id"]),
        ("ix_paper_experiment_workspaces_paper_id", ["paper_id"]),
        ("ix_paper_experiment_workspaces_notebook_id", ["notebook_id"]),
        ("ix_paper_experiment_workspaces_status", ["status"]),
        ("idx_paper_experiment_workspace_user_paper", ["user_id", "paper_id"]),
        ("idx_paper_experiment_workspace_updated_at", ["updated_at"]),
    ):
        if not _index_exists(inspector, "paper_experiment_workspaces", index_name):
            op.create_index(index_name, "paper_experiment_workspaces", columns, unique=False)
            inspector = sa.inspect(bind)

    if not _table_exists(inspector, "paper_experiment_runs"):
        op.create_table(
            "paper_experiment_runs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("workspace_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("notebook_id", sa.String(length=36), nullable=True),
            sa.Column("notebook_cell_id", sa.String(length=36), nullable=True),
            sa.Column("base_run_id", sa.Integer(), nullable=True),
            sa.Column("run_kind", sa.String(length=24), nullable=False, server_default="variant"),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="draft"),
            sa.Column("label", sa.String(length=200), nullable=False),
            sa.Column("model_name", sa.String(length=255), nullable=True),
            sa.Column("hypothesis", sa.Text(), nullable=True),
            sa.Column("variant_spec_json", sa.JSON(), nullable=True),
            sa.Column("params_json", sa.JSON(), nullable=True),
            sa.Column("metrics_json", sa.JSON(), nullable=True),
            sa.Column("artifacts_json", sa.JSON(), nullable=True),
            sa.Column("summary_json", sa.JSON(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["base_run_id"], ["paper_experiment_runs.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["notebook_id"], ["notebooks.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["workspace_id"], ["paper_experiment_workspaces.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(bind)
    for index_name, columns in (
        ("ix_paper_experiment_runs_id", ["id"]),
        ("ix_paper_experiment_runs_workspace_id", ["workspace_id"]),
        ("ix_paper_experiment_runs_user_id", ["user_id"]),
        ("ix_paper_experiment_runs_notebook_id", ["notebook_id"]),
        ("ix_paper_experiment_runs_notebook_cell_id", ["notebook_cell_id"]),
        ("ix_paper_experiment_runs_base_run_id", ["base_run_id"]),
        ("ix_paper_experiment_runs_run_kind", ["run_kind"]),
        ("ix_paper_experiment_runs_status", ["status"]),
        ("idx_paper_experiment_run_workspace_created", ["workspace_id", "created_at"]),
        ("idx_paper_experiment_run_workspace_status", ["workspace_id", "status"]),
    ):
        if not _index_exists(inspector, "paper_experiment_runs", index_name):
            op.create_index(index_name, "paper_experiment_runs", columns, unique=False)
            inspector = sa.inspect(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "paper_experiment_runs"):
        for index_name in (
            "idx_paper_experiment_run_workspace_status",
            "idx_paper_experiment_run_workspace_created",
            "ix_paper_experiment_runs_status",
            "ix_paper_experiment_runs_run_kind",
            "ix_paper_experiment_runs_base_run_id",
            "ix_paper_experiment_runs_notebook_cell_id",
            "ix_paper_experiment_runs_notebook_id",
            "ix_paper_experiment_runs_user_id",
            "ix_paper_experiment_runs_workspace_id",
            "ix_paper_experiment_runs_id",
        ):
            if _index_exists(inspector, "paper_experiment_runs", index_name):
                op.drop_index(index_name, table_name="paper_experiment_runs")
                inspector = sa.inspect(bind)
        op.drop_table("paper_experiment_runs")
        inspector = sa.inspect(bind)

    if _table_exists(inspector, "paper_experiment_workspaces"):
        for index_name in (
            "idx_paper_experiment_workspace_updated_at",
            "idx_paper_experiment_workspace_user_paper",
            "ix_paper_experiment_workspaces_status",
            "ix_paper_experiment_workspaces_notebook_id",
            "ix_paper_experiment_workspaces_paper_id",
            "ix_paper_experiment_workspaces_user_id",
            "ix_paper_experiment_workspaces_id",
        ):
            if _index_exists(inspector, "paper_experiment_workspaces", index_name):
                op.drop_index(index_name, table_name="paper_experiment_workspaces")
                inspector = sa.inspect(bind)
        op.drop_table("paper_experiment_workspaces")

