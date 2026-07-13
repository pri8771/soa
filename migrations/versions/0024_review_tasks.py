"""Review-task table with RLS (REV-001).

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


def _guid() -> sa.types.TypeEngine[object]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "review_tasks",
        sa.Column("id", _guid(), primary_key=True),
        sa.Column("organization_id", _guid(), nullable=False),
        sa.Column("document_id", _guid(), nullable=False),
        sa.Column("run_id", _guid(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("blocking", sa.Boolean(), nullable=False),
        sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assigned_to", sa.String(200), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", sa.String(200), nullable=True),
        sa.Column("outcome", sa.String(200), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_review_tasks_organization_id", "review_tasks", ["organization_id"])
    op.create_index("ix_review_tasks_document_id", "review_tasks", ["document_id"])
    op.create_index("ix_review_tasks_run_id", "review_tasks", ["run_id"])
    op.create_index(
        "uq_review_tasks_single_active",
        "review_tasks",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('open', 'in_progress')"),
        sqlite_where=sa.text("state IN ('open', 'in_progress')"),
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE review_tasks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE review_tasks FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON review_tasks
        USING (organization_id = {TENANT_MATCH})
        WITH CHECK (organization_id = {TENANT_MATCH})
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON review_tasks")
    op.drop_index("uq_review_tasks_single_active", table_name="review_tasks")
    op.drop_index("ix_review_tasks_run_id", table_name="review_tasks")
    op.drop_index("ix_review_tasks_document_id", table_name="review_tasks")
    op.drop_index("ix_review_tasks_organization_id", table_name="review_tasks")
    op.drop_table("review_tasks")
