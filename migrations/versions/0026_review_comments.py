"""Review comments + task escalation columns with RLS (REV-011).

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


def _guid() -> sa.types.TypeEngine[object]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "review_comments",
        sa.Column("id", _guid(), primary_key=True),
        sa.Column("organization_id", _guid(), nullable=False),
        sa.Column("task_id", _guid(), nullable=False),
        sa.Column("document_id", _guid(), nullable=False),
        sa.Column("author", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("mentions", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_review_comments_organization_id", "review_comments", ["organization_id"])
    op.create_index("ix_review_comments_task_id", "review_comments", ["task_id"])
    op.create_index("ix_review_comments_document_id", "review_comments", ["document_id"])

    op.add_column("review_tasks", sa.Column("escalated_at", sa.DateTime(timezone=True)))
    op.add_column("review_tasks", sa.Column("escalated_by", sa.String(200)))
    op.add_column("review_tasks", sa.Column("escalation_reason", sa.String(500)))

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE review_comments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE review_comments FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON review_comments
        USING (organization_id = {TENANT_MATCH})
        WITH CHECK (organization_id = {TENANT_MATCH})
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON review_comments")
    op.drop_column("review_tasks", "escalation_reason")
    op.drop_column("review_tasks", "escalated_by")
    op.drop_column("review_tasks", "escalated_at")
    op.drop_index("ix_review_comments_document_id", table_name="review_comments")
    op.drop_index("ix_review_comments_task_id", table_name="review_comments")
    op.drop_index("ix_review_comments_organization_id", table_name="review_comments")
    op.drop_table("review_comments")
