"""Deletion tombstones with RLS (SEC-010).

A tombstone is the RETAINED PROOF that a document's data was deleted:
who, when, why, how much was removed per category, and which object
keys. The document's content is gone; the tombstone (and the audit
trail) is what remains, so a deletion is always attributable and
reconcilable. Tombstones are themselves never deleted by the retention
engine.

Revision ID: 0037
Revises: 0036
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"
PORTABLE_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "deletion_tombstones",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("requested_by", sa.String(200), nullable=False),
        sa.Column("object_keys_deleted", sa.Integer(), nullable=False, default=0),
        sa.Column("category_counts", PORTABLE_JSON, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # One tombstone per document: deletion is idempotent, re-running
        # completes the same tombstone instead of creating a second.
        sa.UniqueConstraint("organization_id", "document_id"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE deletion_tombstones ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE deletion_tombstones FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON deletion_tombstones
            USING (organization_id = {TENANT_MATCH})
            WITH CHECK (organization_id = {TENANT_MATCH})
            """
        )


def downgrade() -> None:
    op.drop_table("deletion_tombstones")
