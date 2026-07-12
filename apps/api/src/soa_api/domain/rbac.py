"""Roles and permissions (TEN-006).

The permission registry is code — explicit, action-oriented strings. A
permission that is not in the registry does not exist: role creation,
updates, and permission checks all fail closed on unknown values.

System role templates are seeded per organization; organizations may also
define custom roles. Every role mutation writes an audit event.
"""

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import JSON, Boolean, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.audit import ActorType, record_audit_event
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID

PORTABLE_JSON = JSON().with_variant(JSONB(), "postgresql")

# ---------------------------------------------------------------------------
# Permission registry — action-oriented, explicit, fail-closed.
# ---------------------------------------------------------------------------
PERMISSION_REGISTRY: frozenset[str] = frozenset(
    {
        "documents.read",
        "documents.upload",
        "documents.review",
        "documents.approve",
        "documents.reject",
        "documents.reprocess",
        "processes.read",
        "processes.manage",
        "streams.read",
        "streams.manage",
        "catalogs.read",
        "catalogs.manage",
        "integrations.read",
        "integrations.manage",
        "integrations.replay",
        "organization.manage",
        "members.manage",
        "roles.manage",
        "credentials.manage",
        "audit.read",
        "analytics.read",
    }
)


class UnknownPermissionError(Exception):
    def __init__(self, permission: str) -> None:
        self.permission = permission
        super().__init__(f"unknown permission {permission!r} — permissions fail closed")


def validate_permissions(permissions: Iterable[str]) -> list[str]:
    validated: list[str] = []
    for permission in permissions:
        if permission not in PERMISSION_REGISTRY:
            raise UnknownPermissionError(permission)
        validated.append(permission)
    return sorted(set(validated))


# System role templates (docs/PRODUCT.md §2 user roles).
SYSTEM_ROLE_TEMPLATES: dict[str, frozenset[str]] = {
    "org-admin": PERMISSION_REGISTRY,  # full access template
    "supervisor": frozenset(
        {
            "documents.read",
            "documents.review",
            "documents.approve",
            "documents.reject",
            "documents.reprocess",
            "processes.read",
            "streams.read",
            "catalogs.read",
            "analytics.read",
            "audit.read",
        }
    ),
    "reviewer": frozenset(
        {
            "documents.read",
            "documents.review",
            "documents.approve",
            "documents.upload",
            "processes.read",
            "streams.read",
            "catalogs.read",
        }
    ),
    "integration-admin": frozenset(
        {
            "integrations.read",
            "integrations.manage",
            "integrations.replay",
            "credentials.manage",
            "documents.read",
            "streams.read",
        }
    ),
    "auditor": frozenset(
        {
            "audit.read",
            "documents.read",
            "processes.read",
            "streams.read",
            "catalogs.read",
            "integrations.read",
            "analytics.read",
        }
    ),
}


class Role(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    permissions: Mapped[list[str]] = mapped_column(PORTABLE_JSON, nullable=False, default=list)

    __table_args__ = (UniqueConstraint("organization_id", "slug"),)


class RoleAssignment(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "role_assignments"

    membership_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    role_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)

    __table_args__ = (UniqueConstraint("membership_id", "role_id"),)


class RoleRepository(ScopedRepository[Role]):
    model = Role

    async def get_by_slug(self, slug: str) -> Role | None:
        stmt = self._scoped_select().where(Role.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()


class RoleAssignmentRepository(ScopedRepository[RoleAssignment]):
    model = RoleAssignment

    async def list_for_membership(self, membership_id: uuid.UUID) -> Sequence[RoleAssignment]:
        stmt = self._scoped_select().where(RoleAssignment.membership_id == membership_id)
        return (await self._session.execute(stmt)).scalars().all()


# ---------------------------------------------------------------------------
# Application services — every mutation is audited.
# ---------------------------------------------------------------------------


async def seed_system_roles(
    session: AsyncSession, context: OrganizationContext, *, actor_id: str
) -> list[Role]:
    """Create the system role templates for a new organization (idempotent)."""
    repo = RoleRepository(session, context)
    created: list[Role] = []
    for slug, permissions in SYSTEM_ROLE_TEMPLATES.items():
        if await repo.get_by_slug(slug) is not None:
            continue
        role = repo.add(
            Role(
                name=slug.replace("-", " ").title(),
                slug=slug,
                is_system=True,
                permissions=validate_permissions(permissions),
            )
        )
        created.append(role)
    if created:
        await session.flush()
        for role in created:
            await record_audit_event(
                session,
                actor_type=ActorType.SYSTEM,
                actor_id=actor_id,
                action="role.created",
                target_type="role",
                target_id=str(role.id),
                organization_id=context.organization_id,
                summary={"slug": role.slug, "system": True},
            )
    return created


async def create_custom_role(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    name: str,
    slug: str,
    permissions: Iterable[str],
    actor_id: str,
) -> Role:
    role = RoleRepository(session, context).add(
        Role(name=name, slug=slug, is_system=False, permissions=validate_permissions(permissions))
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="role.created",
        target_type="role",
        target_id=str(role.id),
        organization_id=context.organization_id,
        summary={"slug": slug, "permissions": sorted(set(permissions))},
    )
    return role


async def assign_role(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    membership_id: uuid.UUID,
    role_id: uuid.UUID,
    actor_id: str,
) -> RoleAssignment:
    assignment = RoleAssignmentRepository(session, context).add(
        RoleAssignment(membership_id=membership_id, role_id=role_id)
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="role.assigned",
        target_type="membership",
        target_id=str(membership_id),
        organization_id=context.organization_id,
        summary={"role_id": str(role_id)},
    )
    return assignment


async def revoke_role(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    assignment: RoleAssignment,
    actor_id: str,
) -> None:
    await session.delete(assignment)
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="role.revoked",
        target_type="membership",
        target_id=str(assignment.membership_id),
        organization_id=context.organization_id,
        summary={"role_id": str(assignment.role_id)},
    )


async def permissions_for_membership(
    session: AsyncSession, context: OrganizationContext, membership_id: uuid.UUID
) -> frozenset[str]:
    """Union of permissions across all roles assigned to a membership.
    Unknown stored values fail closed (raise) rather than being ignored."""
    assignments = await RoleAssignmentRepository(session, context).list_for_membership(
        membership_id
    )
    if not assignments:
        return frozenset()
    role_ids = [assignment.role_id for assignment in assignments]
    stmt = (
        select(Role)
        .where(Role.organization_id == context.organization_id)
        .where(Role.id.in_(role_ids))
    )
    roles = (await session.execute(stmt)).scalars().all()
    combined: set[str] = set()
    for role in roles:
        combined.update(validate_permissions(role.permissions))
    return frozenset(combined)
