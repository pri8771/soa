"""Integration credentials become secret-store references (SEC-005).

The ``secret`` column held credential VALUES in the tenant database.
SEC-005 moves values into the configured secret store; the row keeps an
opaque ``secretref://<provider>/<name>`` reference instead.

Existing plaintext values CANNOT be converted in place — a migration
has no secret-store access, and copying values around is exactly what
this task removes. This is a pre-pilot schema with development/demo
credentials only, so the migration deliberately deletes credential rows
and clears integration credential pointers: affected integrations
honestly report "no live credential" until an operator re-sets the
credential through the API, which writes the value to the secret store.

Revision ID: 0036
Revises: 0035
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plaintext values are removed, not migrated (see module docstring).
    op.execute("UPDATE integrations SET credential_id = NULL")
    op.execute("DELETE FROM integration_credentials")
    op.drop_column("integration_credentials", "secret")
    op.add_column(
        "integration_credentials",
        sa.Column("secret_reference", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    # References are useless without the store; the plaintext column
    # returns empty, mirroring the destructive-by-design upgrade.
    op.execute("UPDATE integrations SET credential_id = NULL")
    op.execute("DELETE FROM integration_credentials")
    op.drop_column("integration_credentials", "secret_reference")
    op.add_column("integration_credentials", sa.Column("secret", sa.Text(), nullable=False))
