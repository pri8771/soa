"""Service credentials / API keys (TEN-009).

Key shape: ``soa_<prefix>_<secret>``. The prefix is stored for indexed
lookup; only a SHA-256 hash of the full key is persisted (the secret is
256-bit random, so a salted KDF adds nothing here). The raw key exists
exactly once — in the create/rotate response — and is never stored or
logged. All authentication failures raise the same safe error, so callers
cannot distinguish unknown prefix from wrong secret, revoked, or expired.
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_api.auth.errors import AuthenticationError
from soa_api.auth.principal import AuthMethod, Principal
from soa_api.domain.rbac import validate_permissions
from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.audit import ActorType, record_audit_event
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import UTCDateTime, utcnow

KEY_NAMESPACE = "soa"
PREFIX_LENGTH = 8
API_KEY_ISSUER = "soa-api-key"

_SAFE_FAILURE = "Invalid API key."


class CredentialStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class ServiceCredential(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "service_credentials"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    key_prefix: Mapped[str] = mapped_column(
        String(PREFIX_LENGTH), nullable=False, unique=True, index=True
    )
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(PORTABLE_JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=CredentialStatus.ACTIVE)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)

    def __repr__(self) -> str:  # never expose the hash either
        return f"<ServiceCredential {self.name!r} prefix={self.key_prefix!r}>"


class ScopeError(Exception):
    """Required scope missing (or unregistered — fails closed)."""


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("ascii")).hexdigest()


def _generate_key() -> tuple[str, str]:
    prefix = secrets.token_hex(PREFIX_LENGTH // 2)
    secret = secrets.token_urlsafe(32)
    return prefix, f"{KEY_NAMESPACE}_{prefix}_{secret}"


class ServiceCredentialRepository(ScopedRepository[ServiceCredential]):
    model = ServiceCredential


async def create_credential(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    name: str,
    scopes: list[str],
    actor_id: str,
    expires_in: timedelta | None = None,
) -> tuple[ServiceCredential, str]:
    """Create a credential. The returned raw key is shown ONCE."""
    prefix, raw_key = _generate_key()
    credential = ServiceCredentialRepository(session, context).add(
        ServiceCredential(
            name=name,
            key_prefix=prefix,
            key_hash=_hash_key(raw_key),
            scopes=validate_permissions(scopes),
            expires_at=(utcnow() + expires_in) if expires_in else None,
            created_by=actor_id,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="credential.created",
        target_type="service_credential",
        target_id=str(credential.id),
        organization_id=context.organization_id,
        summary={"name": name, "scopes": credential.scopes, "prefix": prefix},
    )
    return credential, raw_key


async def rotate_credential(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    credential: ServiceCredential,
    actor_id: str,
) -> str:
    """Replace the secret in place: the old key stops working immediately."""
    prefix, raw_key = _generate_key()
    credential.key_prefix = prefix
    credential.key_hash = _hash_key(raw_key)
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="credential.rotated",
        target_type="service_credential",
        target_id=str(credential.id),
        organization_id=context.organization_id,
        summary={"prefix": prefix},
    )
    return raw_key


async def revoke_credential(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    credential: ServiceCredential,
    actor_id: str,
) -> None:
    credential.status = CredentialStatus.REVOKED
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="credential.revoked",
        target_type="service_credential",
        target_id=str(credential.id),
        organization_id=context.organization_id,
        summary={"name": credential.name},
    )


async def authenticate_api_key(session: AsyncSession, raw_key: str) -> Principal:
    """Resolve a raw API key to a principal, failing closed with one
    indistinguishable error for every failure mode."""
    parts = raw_key.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_NAMESPACE or len(parts[1]) != PREFIX_LENGTH:
        raise AuthenticationError(_SAFE_FAILURE)
    prefix = parts[1]

    stmt = select(ServiceCredential).where(ServiceCredential.key_prefix == prefix)
    credential = (await session.execute(stmt)).scalar_one_or_none()
    if credential is None:
        # Constant-time compare against a dummy hash to equalize timing.
        hmac.compare_digest(_hash_key(raw_key), _hash_key("soa_dummy_dummy"))
        raise AuthenticationError(_SAFE_FAILURE)

    if not hmac.compare_digest(_hash_key(raw_key), credential.key_hash):
        raise AuthenticationError(_SAFE_FAILURE)
    if credential.status != CredentialStatus.ACTIVE:
        raise AuthenticationError(_SAFE_FAILURE)
    if credential.expires_at is not None and credential.expires_at <= utcnow():
        raise AuthenticationError(_SAFE_FAILURE)

    credential.last_used_at = utcnow()
    return Principal(
        subject=str(credential.id),
        issuer=API_KEY_ISSUER,
        auth_method=AuthMethod.API_KEY,
        display_name=credential.name,
        claims={
            "scopes": list(credential.scopes),
            "organization_id": str(credential.organization_id),
        },
    )


def require_scope(principal: Principal, required_scope: str) -> None:
    """Scope check for API-key principals — unknown scopes fail closed."""
    validate_permissions([required_scope])
    scopes = principal.claims.get("scopes")
    if not isinstance(scopes, list) or required_scope not in scopes:
        raise ScopeError(f"API key lacks required scope {required_scope!r}")


def credential_organization_id(principal: Principal) -> uuid.UUID:
    raw = principal.claims.get("organization_id")
    if not isinstance(raw, str):
        raise ScopeError("API key carries no organization context")
    return uuid.UUID(raw)
