import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from soa_api.domain.identity import (
    InvalidMembershipTransitionError,
    Membership,
    MembershipRepository,
    MembershipStatus,
    User,
    UserRepository,
)
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext

ORG_A = OrganizationContext(organization_id=uuid.UUID(int=0xA1))
ORG_B = OrganizationContext(organization_id=uuid.UUID(int=0xB1))


@pytest.fixture
async def sessions(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/identity.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def make_user(sessions: DatabaseSessions) -> uuid.UUID:
    async with sessions.session_scope() as session:
        user = UserRepository(session).add(
            User(
                identity_key="https://id.example.com/|auth0|riley",
                email="riley@northstar.example",
                display_name="Riley",
            )
        )
    return user.id


async def test_one_identity_joins_multiple_organizations(sessions: DatabaseSessions) -> None:
    user_id = await make_user(sessions)
    async with sessions.session_scope() as session:
        for ctx in (ORG_A, ORG_B):
            membership = Membership(
                user_id=user_id,
                invited_email="riley@northstar.example",
                status=MembershipStatus.ACTIVE,
            )
            MembershipRepository(session, ctx).add(membership)
    async with sessions.session_scope() as session:
        in_a = await MembershipRepository(session, ORG_A).get_for_user(user_id)
        in_b = await MembershipRepository(session, ORG_B).get_for_user(user_id)
    assert in_a is not None and in_b is not None
    assert in_a.grants_access and in_b.grants_access
    assert in_a.organization_id != in_b.organization_id
    await sessions.dispose()


@pytest.mark.parametrize(
    ("status", "expected_access"),
    [
        (MembershipStatus.INVITED, False),
        (MembershipStatus.ACTIVE, True),
        (MembershipStatus.SUSPENDED, False),
        (MembershipStatus.REMOVED, False),
    ],
)
def test_only_active_membership_grants_access(
    status: MembershipStatus, expected_access: bool
) -> None:
    membership = Membership(invited_email="x@y.example", status=status)
    assert membership.grants_access is expected_access


@pytest.mark.parametrize(
    ("current", "requested", "allowed"),
    [
        # Direct INVITED->ACTIVE without a bound user is forbidden —
        # activation only happens through accept() (see test below).
        (MembershipStatus.INVITED, MembershipStatus.ACTIVE, False),
        (MembershipStatus.INVITED, MembershipStatus.REMOVED, True),
        (MembershipStatus.INVITED, MembershipStatus.SUSPENDED, False),
        (MembershipStatus.ACTIVE, MembershipStatus.SUSPENDED, True),
        (MembershipStatus.ACTIVE, MembershipStatus.REMOVED, True),
        (MembershipStatus.ACTIVE, MembershipStatus.INVITED, False),
        (MembershipStatus.SUSPENDED, MembershipStatus.ACTIVE, True),
        (MembershipStatus.SUSPENDED, MembershipStatus.REMOVED, True),
        (MembershipStatus.SUSPENDED, MembershipStatus.INVITED, False),
        (MembershipStatus.REMOVED, MembershipStatus.ACTIVE, False),
        (MembershipStatus.REMOVED, MembershipStatus.INVITED, False),
        (MembershipStatus.REMOVED, MembershipStatus.SUSPENDED, False),
    ],
)
def test_membership_transition_matrix(
    current: MembershipStatus, requested: MembershipStatus, allowed: bool
) -> None:
    membership = Membership(invited_email="x@y.example", status=current)
    if allowed:
        membership.transition_to(requested)
        assert membership.status == requested
    else:
        with pytest.raises(InvalidMembershipTransitionError):
            membership.transition_to(requested)
        assert membership.status == current


async def test_invitation_acceptance_binds_user(sessions: DatabaseSessions) -> None:
    user_id = await make_user(sessions)
    async with sessions.session_scope() as session:
        repo = MembershipRepository(session, ORG_A)
        repo.add(Membership(invited_email="riley@northstar.example"))
    async with sessions.session_scope() as session:
        repo = MembershipRepository(session, ORG_A)
        pending = await repo.get_by_invited_email("riley@northstar.example")
        assert pending is not None and pending.status == MembershipStatus.INVITED
        pending.accept(user_id)
    async with sessions.session_scope() as session:
        accepted = await MembershipRepository(session, ORG_A).get_for_user(user_id)
    assert accepted is not None
    assert accepted.grants_access
    assert accepted.accepted_at is not None
    await sessions.dispose()


async def test_duplicate_membership_per_org_is_rejected(sessions: DatabaseSessions) -> None:
    user_id = await make_user(sessions)
    async with sessions.session_scope() as session:
        MembershipRepository(session, ORG_A).add(
            Membership(
                user_id=user_id,
                invited_email="riley@northstar.example",
                status=MembershipStatus.ACTIVE,
            )
        )
    with pytest.raises(IntegrityError):
        async with sessions.session_scope() as session:
            MembershipRepository(session, ORG_A).add(
                Membership(
                    user_id=user_id,
                    invited_email="riley-again@northstar.example",
                    status=MembershipStatus.ACTIVE,
                )
            )
    await sessions.dispose()


async def test_duplicate_identity_key_is_rejected(sessions: DatabaseSessions) -> None:
    await make_user(sessions)
    with pytest.raises(IntegrityError):
        async with sessions.session_scope() as session:
            UserRepository(session).add(
                User(
                    identity_key="https://id.example.com/|auth0|riley",
                    email="impostor@evil.example",
                )
            )
    await sessions.dispose()


def test_admin_cannot_activate_an_invitation_without_a_user() -> None:
    """A members.manage PATCH must never mint an active membership with no
    identity behind it; accept() (which binds user_id first) is the only
    path from INVITED to ACTIVE."""
    membership = Membership(invited_email="x@y.example", status=MembershipStatus.INVITED)
    with pytest.raises(InvalidMembershipTransitionError):
        membership.transition_to(MembershipStatus.ACTIVE)
    membership.accept(uuid.uuid4())
    assert membership.status == MembershipStatus.ACTIVE
    assert membership.user_id is not None


def test_removed_member_can_be_reinvited() -> None:
    membership = Membership(invited_email="x@y.example", status=MembershipStatus.INVITED)
    membership.accept(uuid.uuid4())
    membership.transition_to(MembershipStatus.REMOVED)
    membership.reinvite()
    assert membership.status == MembershipStatus.INVITED
    assert membership.user_id is None
    assert membership.accepted_at is None
