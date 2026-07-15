"""Tenancy use cases (TEN-008): user provisioning, organization creation,
invitations, and membership state changes. Routers parse/authorize/dispatch;
the transaction boundaries and audit trail live here."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_api.auth.principal import Principal
from soa_api.domain.identity import (
    Membership,
    MembershipRepository,
    MembershipStatus,
    User,
    UserRepository,
)
from soa_api.domain.rbac import (
    RoleRepository,
    assign_role,
    ensure_can_remove_active_org_admin,
    seed_system_roles,
)
from soa_api.domain.tenancy import Organization, OrganizationRepository
from soa_db.audit import ActorType, record_audit_event
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant


class SlugTakenError(Exception):
    pass


class NoInvitationError(Exception):
    pass


@dataclass(frozen=True)
class CreatedOrganization:
    organization: Organization
    membership: Membership


async def ensure_user(session: AsyncSession, principal: Principal) -> User:
    """Just-in-time provisioning: the first authenticated request creates
    the global user record for a new identity."""
    repo = UserRepository(session)
    existing = await repo.get_by_identity_key(principal.identity_key)
    if existing is not None:
        return existing
    normalized_email = (
        principal.email.strip().casefold()
        if principal.email is not None
        else f"{principal.subject}@unknown.invalid"
    )
    user = repo.add(
        User(
            identity_key=principal.identity_key,
            email=normalized_email,
            display_name=principal.display_name,
        )
    )
    await session.flush()
    return user


async def list_memberships_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[Membership]:
    # Global-by-design: a user listing THEIR OWN memberships across
    # organizations. Rows are filtered by user identity, never exposed to
    # other tenants.
    stmt = select(Membership).where(Membership.user_id == user_id)
    return list((await session.execute(stmt)).scalars().all())


async def create_organization(
    session: AsyncSession,
    principal: Principal,
    *,
    name: str,
    slug: str,
) -> CreatedOrganization:
    org_repo = OrganizationRepository(session)
    if await org_repo.get_by_slug(slug) is not None:
        raise SlugTakenError(slug)
    user = await ensure_user(session, principal)
    organization = org_repo.add(Organization(name=name, slug=slug))
    await session.flush()
    # Bind the new tenant so RLS-protected inserts below pass WITH CHECK.
    await bind_tenant(session, organization.id)

    context = OrganizationContext(organization_id=organization.id)
    roles = await seed_system_roles(session, context, actor_id=f"user:{user.id}")
    membership = MembershipRepository(session, context).add(
        Membership(
            user_id=user.id,
            invited_email=user.email,
            status=MembershipStatus.ACTIVE,
        )
    )
    await session.flush()
    admin_role = next(role for role in roles if role.slug == "org-admin")
    await assign_role(
        session,
        context,
        membership_id=membership.id,
        role_id=admin_role.id,
        actor_id=f"user:{user.id}",
    )
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=f"user:{user.id}",
        action="organization.created",
        target_type="organization",
        target_id=str(organization.id),
        organization_id=organization.id,
        summary={"slug": slug, "name": name},
    )
    return CreatedOrganization(organization=organization, membership=membership)


async def invite_member(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    email: str,
    actor_id: str,
) -> tuple[Membership, bool]:
    """Invite by email. Duplicate invitations are safe: the existing
    membership is returned with ``created=False`` instead of erroring."""
    normalized_email = email.strip().casefold()
    repo = MembershipRepository(session, context)
    existing = await repo.get_by_invited_email(normalized_email)
    if existing is not None:
        if existing.status != MembershipStatus.REMOVED:
            return existing, False
        # Removal is terminal for that tenure, not for the person: a fresh
        # invitation reopens the same row awaiting a new acceptance.
        existing.reinvite()
        await record_audit_event(
            session,
            actor_type=ActorType.USER,
            actor_id=actor_id,
            action="member.reinvited",
            target_type="membership",
            target_id=str(existing.id),
            organization_id=context.organization_id,
            summary={"email": normalized_email},
        )
        return existing, True
    membership = repo.add(Membership(invited_email=normalized_email))
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="member.invited",
        target_type="membership",
        target_id=str(membership.id),
        organization_id=context.organization_id,
        summary={"email": normalized_email},
    )
    return membership, True


async def accept_invitation(
    session: AsyncSession,
    principal: Principal,
    *,
    organization_slug: str,
) -> Membership:
    organization = await OrganizationRepository(session).get_by_slug(organization_slug)
    if organization is None:
        raise NoInvitationError(organization_slug)
    # Invitation matching trusts the email claim, so the claim must be
    # verified by the IdP — otherwise anyone who can type the invitee's
    # address into a lax IdP could take over the invitation.
    if principal.email is None or principal.email_verified is not True:
        raise NoInvitationError(organization_slug)
    normalized_email = principal.email.strip().casefold()
    user = await ensure_user(session, principal)
    # A stable IdP subject can outlive an email-address change. Invitation
    # ownership must be checked against the current explicitly verified claim,
    # not a stale email cached on the existing user row.
    if user.email != normalized_email:
        user.email = normalized_email
    # The invited membership row has no user_id yet, so tenant binding (not
    # user binding) is what makes it visible under RLS.
    await bind_tenant(session, organization.id)
    context = OrganizationContext(organization_id=organization.id)
    membership = await MembershipRepository(session, context).get_by_invited_email(normalized_email)
    if membership is None or membership.status != MembershipStatus.INVITED:
        raise NoInvitationError(organization_slug)
    membership.accept(user.id)
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=f"user:{user.id}",
        action="member.joined",
        target_type="membership",
        target_id=str(membership.id),
        organization_id=context.organization_id,
        summary={"email": normalized_email},
    )
    return membership


async def change_membership_status(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    membership: Membership,
    new_status: MembershipStatus,
    actor_id: str,
) -> Membership:
    if membership.status == MembershipStatus.ACTIVE and new_status != MembershipStatus.ACTIVE:
        await ensure_can_remove_active_org_admin(
            session,
            context,
            membership_id=membership.id,
        )
    membership.transition_to(new_status)
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action=f"member.{new_status.value}",
        target_type="membership",
        target_id=str(membership.id),
        organization_id=context.organization_id,
        summary={"status": new_status.value},
    )
    return membership


async def get_role_by_slug_or_none(
    session: AsyncSession, context: OrganizationContext, slug: str
) -> "object | None":
    return await RoleRepository(session, context).get_by_slug(slug)
