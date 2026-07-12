"""Provider, confidence, retention, and mapping policy versions (CFG-005).

Policies are org-level versioned records resolved into stream snapshots by
the configuration resolver (CFG-006). Provider policies REFERENCE secrets
(``credential_ref`` naming a managed credential) — raw secret material in a
policy definition is rejected at validation, and a provider policy missing
a required capability cannot publish.
"""

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_api.domain.versioning import (
    ImmutablePublishedVersionMixin,
    InvalidVersionStateError,
    VersionState,
)
from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.audit import ActorType, record_audit_event
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import UTCDateTime, utcnow


class PolicyType(StrEnum):
    PROVIDER = "provider"
    CONFIDENCE = "confidence"
    RETENTION = "retention"
    MAPPING = "mapping"


class PolicyValidationError(ValueError):
    pass


#: Capabilities every published provider policy must cover.
REQUIRED_PROVIDER_CAPABILITIES = frozenset({"ocr", "field_extraction"})

_SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|secret|password|token|credential(?!_ref))", re.IGNORECASE
)


def _reject_embedded_secrets(node: Any, path: str) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and _SECRET_KEY_PATTERN.search(key):
                raise PolicyValidationError(
                    f"{path}.{key}: secrets are referenced via credential_ref, never embedded"
                )
            _reject_embedded_secrets(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _reject_embedded_secrets(item, f"{path}[{index}]")


def validate_policy(policy_type: PolicyType, definition: dict[str, Any]) -> None:
    _reject_embedded_secrets(definition, "$")
    if policy_type == PolicyType.PROVIDER:
        if not definition.get("provider_name"):
            raise PolicyValidationError("provider policy needs provider_name")
        capabilities = definition.get("capabilities")
        if not isinstance(capabilities, list):
            raise PolicyValidationError("provider policy needs a capabilities list")
        credential_ref = definition.get("credential_ref")
        if credential_ref is not None and not str(credential_ref).startswith("credential:"):
            raise PolicyValidationError("credential_ref must reference a managed credential")
    elif policy_type == PolicyType.CONFIDENCE:
        floor = definition.get("floor")
        if not isinstance(floor, int | float) or not 0 <= float(floor) <= 1:
            raise PolicyValidationError("confidence floor must be between 0 and 1")
        for key, value in (definition.get("field_overrides") or {}).items():
            if not isinstance(value, int | float) or not 0 <= float(value) <= 1:
                raise PolicyValidationError(f"field override {key!r} must be between 0 and 1")
    elif policy_type == PolicyType.RETENTION:
        days = definition.get("document_days")
        if not isinstance(days, int) or days < 1:
            raise PolicyValidationError("document_days must be a positive integer")
    elif policy_type == PolicyType.MAPPING and not isinstance(definition.get("mappings"), dict):
        raise PolicyValidationError("mapping policy needs a mappings object")


def validate_provider_capabilities(definition: dict[str, Any]) -> None:
    capabilities = set(definition.get("capabilities") or [])
    missing = REQUIRED_PROVIDER_CAPABILITIES - capabilities
    if missing:
        raise PolicyValidationError(
            f"provider policy is missing required capabilities: {sorted(missing)} — publish blocked"
        )


class PolicyVersion(
    UuidPrimaryKeyMixin,
    OrganizationScopedMixin,
    ImmutablePublishedVersionMixin,
    TimestampMixin,
    VersionedMixin,
    Base,
):
    __tablename__ = "policy_versions"

    policy_type: Mapped[str] = mapped_column(String(20), nullable=False)
    version_number: Mapped[int] = mapped_column(nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    change_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (UniqueConstraint("organization_id", "policy_type", "version_number"),)


class PolicyVersionRepository(ScopedRepository[PolicyVersion]):
    model = PolicyVersion

    async def list_for_type(self, policy_type: PolicyType) -> list[PolicyVersion]:
        stmt = (
            self._scoped_select()
            .where(PolicyVersion.policy_type == policy_type)
            .order_by(PolicyVersion.version_number)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_published(self, policy_type: PolicyType) -> PolicyVersion | None:
        stmt = self._scoped_select().where(
            PolicyVersion.policy_type == policy_type,
            PolicyVersion.state == VersionState.PUBLISHED,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


async def create_policy_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    policy_type: PolicyType,
    definition: dict[str, Any],
    change_summary: str | None = None,
    actor_id: str,
) -> PolicyVersion:
    validate_policy(policy_type, definition)
    repo = PolicyVersionRepository(session, context)
    versions = await repo.list_for_type(policy_type)
    next_number = (versions[-1].version_number + 1) if versions else 1
    draft = repo.add(
        PolicyVersion(
            policy_type=policy_type,
            version_number=next_number,
            definition=definition,
            change_summary=change_summary,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="policy.draft_created",
        target_type="policy_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={"policy_type": str(policy_type), "version_number": next_number},
    )
    return draft


async def publish_policy_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    draft: PolicyVersion,
    actor_id: str,
    now: datetime | None = None,
) -> PolicyVersion:
    if draft.state != VersionState.DRAFT:
        raise InvalidVersionStateError(f"only drafts publish; this version is {draft.state!r}")
    policy_type = PolicyType(draft.policy_type)
    validate_policy(policy_type, draft.definition)
    if policy_type == PolicyType.PROVIDER:
        validate_provider_capabilities(draft.definition)
    repo = PolicyVersionRepository(session, context)
    previous = await repo.get_published(policy_type)
    if previous is not None:
        previous.state = VersionState.SUPERSEDED
    draft.state = VersionState.PUBLISHED
    draft.published_at = now or utcnow()
    draft.published_by = actor_id
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="policy.version_published",
        target_type="policy_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={
            "policy_type": str(policy_type),
            "version_number": draft.version_number,
            "superseded_version_id": str(previous.id) if previous else None,
        },
    )
    return draft


def policy_reference(policy: PolicyVersion) -> dict[str, Any]:
    """Stable reference stored inside stream snapshots (CFG-006)."""
    return {
        "policy_version_id": str(policy.id),
        "policy_type": policy.policy_type,
        "version_number": policy.version_number,
    }
