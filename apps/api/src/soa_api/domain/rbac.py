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
        # Approving past unresolved CRITICAL blockers — deliberately a
        # separate grant from documents.approve (REV-012).
        "documents.approve.override",
        "documents.reject",
        "documents.reprocess",
        "processes.read",
        "processes.manage",
        "streams.read",
        "streams.manage",
        "catalogs.read",
        "catalogs.manage",
        # Activating a catalog version changes what production matches
        # against — deliberately a separate grant from catalogs.manage
        # (CAT-004 read/import/activate split).
        "catalogs.activate",
        "integrations.read",
        "integrations.manage",
        "integrations.replay",
        # Extraction-instruction (prompt) content is sensitive config —
        # its own grants, deliberately separate from streams.* (AIO-010).
        "instructions.read",
        "instructions.manage",
        "organization.read",
        "organization.manage",
        "members.read",
        "members.manage",
        "roles.read",
        "roles.manage",
        "credentials.manage",
        "audit.read",
        "analytics.read",
        "jobs.read",
        "jobs.manage",
        "jobs.admin",
    }
)

#: Permissions reserved for platform operators (JOB-006 "internal controls
#: are separately permissioned"). Tenants can neither hold them via system
#: role templates nor grant them through custom roles — only platform-level
#: provisioning may assign them.
INTERNAL_PERMISSIONS: frozenset[str] = frozenset({"jobs.admin"})

#: Everything a tenant organization may grant its own members.
TENANT_GRANTABLE_PERMISSIONS: frozenset[str] = PERMISSION_REGISTRY - INTERNAL_PERMISSIONS


class UnknownPermissionError(Exception):
    def __init__(self, permission: str, *, reason: str = "permissions fail closed") -> None:
        self.permission = permission
        super().__init__(f"unknown permission {permission!r} — {reason}")


def validate_permissions(permissions: Iterable[str], *, allow_internal: bool = False) -> list[str]:
    validated: list[str] = []
    for permission in permissions:
        if permission not in PERMISSION_REGISTRY:
            raise UnknownPermissionError(permission)
        if not allow_internal and permission in INTERNAL_PERMISSIONS:
            raise UnknownPermissionError(
                permission, reason="internal permissions are not tenant-grantable"
            )
        validated.append(permission)
    return sorted(set(validated))


# System role templates (docs/PRODUCT.md §2 user roles).
SYSTEM_ROLE_TEMPLATES: dict[str, frozenset[str]] = {
    "org-admin": TENANT_GRANTABLE_PERMISSIONS,  # full tenant access template
    "supervisor": frozenset(
        {
            "organization.read",
            "members.read",
            "documents.read",
            "documents.review",
            "documents.approve",
            "documents.approve.override",
            "documents.reject",
            "documents.reprocess",
            "processes.read",
            "streams.read",
            "instructions.read",
            "catalogs.read",
            "analytics.read",
            "audit.read",
            "jobs.read",
        }
    ),
    "reviewer": frozenset(
        {
            "organization.read",
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
            "organization.read",
            "integrations.read",
            "integrations.manage",
            "integrations.replay",
            "credentials.manage",
            "documents.read",
            "streams.read",
            "jobs.read",
            "jobs.manage",
        }
    ),
    "auditor": frozenset(
        {
            "organization.read",
            "members.read",
            "roles.read",
            "audit.read",
            "documents.read",
            "processes.read",
            "streams.read",
            "catalogs.read",
            "integrations.read",
            "analytics.read",
            "jobs.read",
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
        # allow_internal: platform-provisioned roles may hold internal
        # permissions; tenants still cannot grant them (create_custom_role).
        combined.update(validate_permissions(role.permissions, allow_internal=True))
    return frozenset(combined)
