"""Server-executed evaluation contracts and attestations.

Revision ID: 0046
Revises: 0045
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evaluation_runs",
        sa.Column("execution_mode", sa.String(20), nullable=False, server_default="simulation"),
    )
    op.add_column("evaluation_runs", sa.Column("stream_version_id", sa.Uuid(), nullable=True))
    op.add_column("evaluation_runs", sa.Column("candidate_snapshot", sa.JSON(), nullable=True))
    op.add_column("evaluation_runs", sa.Column("runtime_pins", sa.JSON(), nullable=True))
    op.add_column(
        "evaluation_runs", sa.Column("execution_fingerprint", sa.String(64), nullable=True)
    )
    op.create_index(
        "ix_evaluation_runs_execution_fingerprint",
        "evaluation_runs",
        ["execution_fingerprint"],
    )
    op.add_column(
        "evaluation_runs",
        sa.Column(
            "allow_external_provider", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column("evaluation_runs", sa.Column("attestation", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("evaluation_runs", "attestation")
    op.drop_column("evaluation_runs", "allow_external_provider")
    op.drop_index("ix_evaluation_runs_execution_fingerprint", table_name="evaluation_runs")
    op.drop_column("evaluation_runs", "execution_fingerprint")
    op.drop_column("evaluation_runs", "runtime_pins")
    op.drop_column("evaluation_runs", "candidate_snapshot")
    op.drop_column("evaluation_runs", "stream_version_id")
    op.drop_column("evaluation_runs", "execution_mode")
