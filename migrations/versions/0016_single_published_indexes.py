"""One published version per aggregate, enforced by the database.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-12

Two concurrent FIRST publishes have no prior published row for optimistic
locking to trip on, so both could commit and every subsequent
``get_published`` would raise MultipleResultsFound. Partial unique indexes
make the invariant structural.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES = (
    ("uq_process_versions_single_published", "process_versions", ["process_id"]),
    ("uq_stream_versions_single_published", "stream_versions", ["stream_id"]),
    ("uq_schema_versions_single_published", "schema_versions", ["process_id"]),
    ("uq_rule_set_versions_single_published", "rule_set_versions", ["process_id"]),
    (
        "uq_policy_versions_single_published",
        "policy_versions",
        ["organization_id", "policy_type"],
    ),
)


def upgrade() -> None:
    for name, table, columns in INDEXES:
        op.create_index(
            name,
            table,
            columns,
            unique=True,
            postgresql_where=sa.text("state = 'published'"),
            sqlite_where=sa.text("state = 'published'"),
        )


def downgrade() -> None:
    for name, table, _columns in INDEXES:
        op.drop_index(name, table_name=table)
