"""Global users and organization memberships (TEN-005, ADR-010).

A user is one human identity across the platform (keyed by the identity
provider's issuer+subject). Access to an organization exists only through a
membership in ``active`` status — invited, suspended, and removed
memberships grant nothing. Transitions follow an explicit matrix; anything
else raises.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import String, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.repository import OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow


class MembershipStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REMOVED = "removed"


_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    MembershipStatus.INVITED: {MembershipStatus.ACTIVE, MembershipStatus.REMOVED},
    MembershipStatus.ACTIVE: {MembershipStatus.SUSPENDED, MembershipStatus.REMOVED},
    MembershipStatus.SUSPENDED: {MembershipStatus.ACTIVE, MembershipStatus.REMOVED},
    MembershipStatus.REMOVED: set(),  # terminal
}


class InvalidMembershipTransitionError(Exception):
    def __init__(self, *, current: str, requested: str) -> None:
        super().__init__(f"membership cannot move from {current!r} to {requested!r}")


class User(UuidPrimaryKeyMixin, TimestampMixin, VersionedMixin, Base):
    """Global identity — deliberately NOT organization-scoped. One identity
    may hold memberships in many organizations."""

    __tablename__ = "users"

    identity_key: Mapped[str] = mapped_column(String(400), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)


class Membership(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "memberships"

    # Nullable while an invitation is pending; set on acceptance.
    user_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    invited_email: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MembershipStatus.INVITED
    )
    invited_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "user_id"),
        UniqueConstraint("organization_id", "invited_email"),
    )

    @property
    def grants_access(self) -> bool:
        return self.status == MembershipStatus.ACTIVE

    def transition_to(self, requested: MembershipStatus) -> None:
        allowed = _ALLOWED_TRANSITIONS[MembershipStatus(self.status)]
        if requested not in allowed:
            raise InvalidMembershipTransitionError(current=self.status, requested=requested)
        self.status = requested

    def accept(self, user_id: uuid.UUID) -> None:
        """Invitation acceptance: binds the invited email to a real user."""
        self.transition_to(MembershipStatus.ACTIVE)
        self.user_id = user_id
        self.accepted_at = utcnow()


class UserRepository:
    """Users are global identities (the join to a tenant happens through
    memberships), so this repository is intentionally unscoped."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_identity_key(self, identity_key: str) -> User | None:
        stmt = select(User).where(User.identity_key == identity_key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    def add(self, user: User) -> User:
        self._session.add(user)
        return user


class MembershipRepository(ScopedRepository[Membership]):
    model = Membership

    async def get_for_user(self, user_id: uuid.UUID) -> Membership | None:
        stmt = self._scoped_select().where(Membership.user_id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_invited_email(self, email: str) -> Membership | None:
        stmt = self._scoped_select().where(Membership.invited_email == email)
        return (await self._session.execute(stmt)).scalar_one_or_none()
