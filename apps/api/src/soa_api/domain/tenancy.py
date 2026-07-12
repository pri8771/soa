"""Organization and workspace models (TEN-004, ADR-002).

The organization is the contractual tenant and security boundary; the
workspace is an optional grouping inside it. Organizations are the tenant
ROOT — they are looked up by identity/slug at the authorization boundary.
Workspaces are tenant-owned and only reachable through the scoped
repository.
"""

import uuid
from enum import StrEnum

from sqlalchemy import String, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.repository import OrganizationScopedMixin, ScopedRepository


class OrganizationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class Organization(UuidPrimaryKeyMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=OrganizationStatus.ACTIVE
    )
    locale: Mapped[str] = mapped_column(String(20), nullable=False, default="en-GB")
    region: Mapped[str | None] = mapped_column(String(50), nullable=True)

    @property
    def is_operational(self) -> bool:
        """Suspended and closed organizations cannot use the platform;
        the authorization service (TEN-007) enforces this on every request."""
        return self.status == OrganizationStatus.ACTIVE


class Workspace(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)

    __table_args__ = (UniqueConstraint("organization_id", "slug"),)


class OrganizationRepository:
    """Organizations are the tenant root, not tenant-owned data — lookups
    here happen at the authorization boundary (resolving the tenant),
    which is why this repository is not organization-scoped."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, organization_id: uuid.UUID) -> Organization | None:
        return await self._session.get(Organization, organization_id)

    async def get_by_slug(self, slug: str) -> Organization | None:
        stmt = select(Organization).where(Organization.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    def add(self, organization: Organization) -> Organization:
        self._session.add(organization)
        return organization


class WorkspaceRepository(ScopedRepository[Workspace]):
    model = Workspace

    async def get_by_slug(self, slug: str) -> Workspace | None:
        stmt = self._scoped_select().where(Workspace.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()
