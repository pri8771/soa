"""Bind a gold dataset to a stream so it can act as a per-stream training set.

A "training set" (the extraction-training feature) is a gold dataset scoped to
one stream: its labelled sample documents become both few-shot exemplars for
that stream's extraction and the held-out slice the evaluation runner scores.
Org-level evaluation datasets keep ``stream_id`` NULL, so the column is
nullable and additive — no existing row changes meaning.

Revision ID: 0054
Revises: 0053
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gold_datasets",
        sa.Column("stream_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_gold_datasets_org_stream",
        "gold_datasets",
        ["organization_id", "stream_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_gold_datasets_org_stream", table_name="gold_datasets")
    op.drop_column("gold_datasets", "stream_id")
