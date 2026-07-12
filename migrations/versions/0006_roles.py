"""Roles and role assignments.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column(
            "permissions",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("organization_id", "slug", name="uq_roles_organization_id"),
    )
    op.create_index("ix_roles_organization_id", "roles", ["organization_id"])

    op.create_table(
        "role_assignments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("membership_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("role_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("membership_id", "role_id", name="uq_role_assignments_membership_id"),
    )
    op.create_index("ix_role_assignments_organization_id", "role_assignments", ["organization_id"])
    op.create_index("ix_role_assignments_membership_id", "role_assignments", ["membership_id"])
    op.create_index("ix_role_assignments_role_id", "role_assignments", ["role_id"])


def downgrade() -> None:
    op.drop_index("ix_role_assignments_role_id", table_name="role_assignments")
    op.drop_index("ix_role_assignments_membership_id", table_name="role_assignments")
    op.drop_index("ix_role_assignments_organization_id", table_name="role_assignments")
    op.drop_table("role_assignments")
    op.drop_index("ix_roles_organization_id", table_name="roles")
    op.drop_table("roles")
