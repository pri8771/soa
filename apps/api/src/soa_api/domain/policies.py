"""Provider, confidence, retention, and mapping policy versions (CFG-005).

Policies are org-level versioned records resolved into stream snapshots by
the configuration resolver (CFG-006). Provider policies REFERENCE secrets
(``credential_ref`` naming a managed credential) — raw secret material in a
policy definition is rejected at validation, and a provider policy missing
a required capability cannot publish.
"""

import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Index, String, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_api.domain.versioning import (
    ImmutablePublishedVersionMixin,
    InvalidVersionStateError,
    VersionState,
)
from soa_config import SecretReference
from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.advisory import transaction_advisory_lock
from soa_db.audit import ActorType, record_audit_event
from soa_db.outbox import PORTABLE_JSON
from soa_db.provider_credentials import credential_reference_is_live
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
MAX_PROVIDER_CHAIN_LENGTH = 8

_SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|secret|password|token|credential(?!_(?:ref|id)))", re.IGNORECASE
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


def _validate_secret_reference(value: object, *, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise PolicyValidationError(f"{field} must be an immutable secretref:// reference")
    try:
        SecretReference.parse(value)
    except ValueError as error:
        raise PolicyValidationError(
            f"{field} must be an immutable secretref:// reference"
        ) from error


def _validate_credential_binding(node: dict[str, Any], *, field: str) -> None:
    """Validate the persisted server-side credential binding pair."""

    raw_id = node.get("credential_id")
    raw_reference = node.get("credential_ref")
    if raw_id is None and raw_reference is None:
        return
    if raw_id is None or raw_reference is None:
        raise PolicyValidationError(
            f"{field} must contain both server-bound credential_id and credential_ref"
        )
    if not isinstance(raw_id, str):
        raise PolicyValidationError(f"{field}.credential_id must be a UUID")
    try:
        uuid.UUID(raw_id)
    except ValueError as error:
        raise PolicyValidationError(f"{field}.credential_id must be a UUID") from error
    _validate_secret_reference(raw_reference, field=f"{field}.credential_ref")


def _validate_nonnegative_integer(value: object, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PolicyValidationError(f"{field} must be a non-negative integer")


def _validate_quality_score(value: object, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= float(value) <= 1:
        raise PolicyValidationError(f"{field} must be between 0 and 1")


def _validate_provider_policy(definition: dict[str, Any]) -> None:
    provider_name = definition.get("provider_name")
    if (
        not isinstance(provider_name, str)
        or not provider_name.strip()
        or provider_name != provider_name.strip()
        or len(provider_name) > 100
    ):
        raise PolicyValidationError("provider policy needs a valid provider_name")
    capabilities = definition.get("capabilities")
    if not isinstance(capabilities, list) or not all(
        isinstance(capability, str) and capability for capability in capabilities
    ):
        raise PolicyValidationError("provider policy needs a capabilities list")
    if len(set(capabilities)) != len(capabilities):
        raise PolicyValidationError("provider policy capabilities cannot contain duplicates")

    if "credential_id" in definition:
        _validate_credential_binding(definition, field="provider")
    else:
        # Legacy/bootstrap policies may hold a validated immutable reference
        # without managed metadata. The admin API never creates this shape.
        _validate_secret_reference(definition.get("credential_ref"), field="credential_ref")
    if "estimated_cost_cents" in definition:
        _validate_nonnegative_integer(
            definition["estimated_cost_cents"], field="estimated_cost_cents"
        )
    evaluated_scores: list[float] = []
    if "evaluated_quality_score" in definition:
        _validate_quality_score(
            definition["evaluated_quality_score"], field="evaluated_quality_score"
        )
        evaluated_scores.append(float(definition["evaluated_quality_score"]))

    raw_fallbacks = definition.get("fallback_providers", [])
    if not isinstance(raw_fallbacks, list):
        raise PolicyValidationError("fallback_providers must be an ordered list")
    if len(raw_fallbacks) + 1 > MAX_PROVIDER_CHAIN_LENGTH:
        raise PolicyValidationError(
            f"provider chain cannot contain more than {MAX_PROVIDER_CHAIN_LENGTH} providers"
        )
    names = {provider_name}
    for index, raw in enumerate(raw_fallbacks):
        field = f"fallback_providers[{index}]"
        if not isinstance(raw, dict):
            raise PolicyValidationError(f"{field} must be an object")
        name = raw.get("provider_name")
        if not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 100:
            raise PolicyValidationError(f"{field}.provider_name is invalid")
        if name in names:
            raise PolicyValidationError(f"provider chain contains duplicate provider {name!r}")
        names.add(name)
        if "credential_id" in raw:
            _validate_credential_binding(raw, field=field)
        else:
            _validate_secret_reference(raw.get("credential_ref"), field=f"{field}.credential_ref")
        if "estimated_cost_cents" in raw:
            _validate_nonnegative_integer(
                raw["estimated_cost_cents"], field=f"{field}.estimated_cost_cents"
            )
        if "evaluated_quality_score" in raw:
            _validate_quality_score(
                raw["evaluated_quality_score"],
                field=f"{field}.evaluated_quality_score",
            )
            evaluated_scores.append(float(raw["evaluated_quality_score"]))

    for flag in (
        "local_only",
        "allow_third_party_processing",
        "allow_content_retention",
        "allow_training_on_content",
    ):
        if flag in definition and not isinstance(definition[flag], bool):
            raise PolicyValidationError(f"{flag} must be a boolean")

    allowed_regions = definition.get("allowed_regions")
    if allowed_regions is not None:
        if (
            not isinstance(allowed_regions, list)
            or not allowed_regions
            or not all(
                isinstance(region, str) and region and region == region.strip().lower()
                for region in allowed_regions
            )
            or len(set(allowed_regions)) != len(allowed_regions)
        ):
            raise PolicyValidationError(
                "allowed_regions must be a non-empty unique list of lowercase regions"
            )
    if "budget_cents" in definition:
        _validate_nonnegative_integer(definition["budget_cents"], field="budget_cents")
    if "min_quality" in definition:
        _validate_quality_score(definition["min_quality"], field="min_quality")
        quality_floor = float(definition["min_quality"])
        if quality_floor > 0 and not any(score >= quality_floor for score in evaluated_scores):
            raise PolicyValidationError(
                "min_quality requires at least one provider with a passing evaluated_quality_score"
            )


def validate_policy(policy_type: PolicyType, definition: dict[str, Any]) -> None:
    _reject_embedded_secrets(definition, "$")
    if policy_type == PolicyType.PROVIDER:
        _validate_provider_policy(definition)
    elif policy_type == PolicyType.CONFIDENCE:
        allowed_keys = {
            "floor",
            "critical_floor",
            "field_overrides",
            "critical_requires_evidence",
            "critical_candidate_margin",
            "standard_candidate_margin",
            "review_on_indeterminate_error_rules",
        }
        unknown = sorted(set(definition) - allowed_keys)
        if unknown:
            raise PolicyValidationError(f"confidence policy has unknown fields: {unknown}")
        floor = definition.get("floor")
        if (
            isinstance(floor, bool)
            or not isinstance(floor, int | float)
            or not 0 <= float(floor) <= 1
        ):
            raise PolicyValidationError("confidence floor must be between 0 and 1")
        overrides = definition.get("field_overrides", {})
        if not isinstance(overrides, dict):
            raise PolicyValidationError("confidence field_overrides must be an object")
        for key, value in overrides.items():
            if (
                not isinstance(key, str)
                or not key.strip()
                or isinstance(value, bool)
                or not isinstance(value, int | float)
                or not 0 <= float(value) <= 1
            ):
                raise PolicyValidationError(f"field override {key!r} must be between 0 and 1")
        critical_floor = definition.get("critical_floor", max(0.98, float(floor)))
        _validate_quality_score(critical_floor, field="critical_floor")
        if float(critical_floor) < float(floor):
            raise PolicyValidationError("critical_floor cannot be lower than floor")
        for key in ("critical_candidate_margin", "standard_candidate_margin"):
            if key in definition:
                _validate_quality_score(definition[key], field=key)
        for key in ("critical_requires_evidence", "review_on_indeterminate_error_rules"):
            if key in definition and not isinstance(definition[key], bool):
                raise PolicyValidationError(f"{key} must be a boolean")
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

    __table_args__ = (
        UniqueConstraint("organization_id", "policy_type", "version_number"),
        # One published policy per (org, type): backstops concurrent
        # first publishes at the database.
        Index(
            "uq_policy_versions_single_published",
            "organization_id",
            "policy_type",
            unique=True,
            postgresql_where=text("state = 'published'"),
            sqlite_where=text("state = 'published'"),
        ),
    )


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
    # Version allocation protects a logical sequence, including an empty
    # sequence where SELECT FOR UPDATE cannot lock anything.
    await transaction_advisory_lock(
        session,
        "policy-version-sequence",
        context.organization_id,
        policy_type,
    )
    repo = PolicyVersionRepository(session, context)
    versions = await repo.list_for_type(policy_type)
    next_number = (versions[-1].version_number + 1) if versions else 1
    draft = repo.add(
        PolicyVersion(
            policy_type=policy_type,
            version_number=next_number,
            definition=dict(definition),
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
    # Serialize the single-published slot across replicas before inspecting
    # and superseding its current occupant.
    await transaction_advisory_lock(
        session,
        "policy-published-slot",
        context.organization_id,
        policy_type,
    )
    validate_policy(policy_type, draft.definition)
    if policy_type == PolicyType.PROVIDER:
        validate_provider_capabilities(draft.definition)
        await ensure_managed_provider_credentials_live(
            session,
            context,
            definition=draft.definition,
            require_current=True,
        )
    repo = PolicyVersionRepository(session, context)
    previous = await repo.get_published(policy_type)
    if previous is not None:
        previous.state = VersionState.SUPERSEDED
        # Flush the supersede before publishing: the single-published
        # unique index must never see two published rows mid-flush.
        await session.flush()
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


def _provider_nodes(definition: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [definition]
    raw_fallbacks = definition.get("fallback_providers", [])
    if isinstance(raw_fallbacks, list):
        nodes.extend(item for item in raw_fallbacks if isinstance(item, dict))
    return nodes


async def ensure_managed_provider_credentials_live(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    definition: dict[str, Any],
    require_current: bool,
) -> None:
    """Authenticate every managed credential id/reference pair in a policy.

    The row lock serializes policy publication against credential revocation.
    Legacy policies with only a secret reference remain executable, but the
    production admin API always creates managed bindings.
    """

    for index, node in enumerate(_provider_nodes(definition)):
        raw_id = node.get("credential_id")
        if raw_id is None:
            continue
        provider_name = node.get("provider_name")
        raw_reference = node.get("credential_ref")
        field = "provider" if index == 0 else f"fallback_providers[{index - 1}]"
        if not isinstance(provider_name, str) or not isinstance(raw_reference, str):
            raise PolicyValidationError(f"{field} has an incomplete credential binding")
        try:
            credential_id = uuid.UUID(str(raw_id))
        except ValueError as error:
            raise PolicyValidationError(f"{field}.credential_id must be a UUID") from error
        credential = await credential_reference_is_live(
            session,
            context,
            credential_id=credential_id,
            provider_name=provider_name,
            require_current=require_current,
        )
        if credential is None or credential.secret_reference != raw_reference:
            state = "current and live" if require_current else "live"
            raise PolicyValidationError(
                f"{field} does not reference a {state} credential for {provider_name!r}"
            )


def policy_references_credential(
    policy: PolicyVersion,
    *,
    credential_id: uuid.UUID,
    secret_reference: str,
) -> bool:
    """Return whether a policy definition names a credential binding."""

    for node in _provider_nodes(policy.definition):
        if node.get("credential_id") == str(credential_id):
            return True
        if node.get("credential_ref") == secret_reference:
            return True
    return False


def policy_reference(policy: PolicyVersion) -> dict[str, Any]:
    """Stable reference stored inside stream snapshots (CFG-006)."""
    return {
        "policy_version_id": str(policy.id),
        "policy_type": policy.policy_type,
        "version_number": policy.version_number,
    }
