"""Let an evaluation run score only a subset of a gold dataset's splits.

Training-set evaluations must score the HELD-OUT slice (validation/test),
never the train split whose documents were compiled verbatim into the live
few-shot examples — scoring memorised documents would inflate the metrics the
promotion gate relies on. ``scored_splits`` is the (nullable) allow-list of
splits a run scores; NULL keeps the existing behaviour of scoring every
document (org-level evaluations are unaffected).

Revision ID: 0055
Revises: 0054
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evaluation_runs",
        sa.Column("scored_splits", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("evaluation_runs", "scored_splits")
