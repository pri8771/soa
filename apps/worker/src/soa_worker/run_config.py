"""Fail-closed verification of the immutable configuration pinned to a run.

The control-plane configuration tables intentionally live outside ``soa_db``.
The worker reads them through a narrow SQL boundary, authenticates every pin
captured at intake, and constructs per-run executors. Mutable/current defaults
are never substituted for historical IDs.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from soa_config import (
    SecretNotFoundError,
    SecretReference,
    SecretStore,
    SecretStoreError,
    SecretStoreUnavailableError,
)
from soa_db.catalogs import (
    CATALOG_VERSION_PINS_KEY,
    CatalogError,
    parse_catalog_version_pins,
    validate_catalog_version_pins,
)
from soa_db.documents import Document
from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRun, StageRun
from soa_db.tenant_guard import bind_tenant
from soa_normalize import NormalizationContext
from soa_rules import ConfidencePolicy
from soa_storage import ObjectStore
from soa_worker.extraction.provider import (
    ExtractionProvider,
    ExtractionProviderError,
    ExtractionRequest,
    ExtractionResult,
    FieldSpec,
)
from soa_worker.orchestrator import StageExecutionError, StageExecutor, StageOutcome
from soa_worker.pipeline import PipelineConfig, build_executors
from soa_worker.provider_router import RoutingPolicy
from soa_worker.providers import Capability, create_provider, provider_info
from soa_worker.rendering import RenderLimits
from soa_worker.routed_extraction import (
    ConfiguredExtractionProvider,
    RoutedExtractionProvider,
)


class RunConfigError(ValueError):
    """The run's immutable stream configuration cannot be trusted."""


@dataclass(frozen=True)
class ResolvedProviderCandidate:
    provider_name: str
    credential_reference: SecretReference | None
    estimated_cost_cents: int | None


@dataclass(frozen=True)
class ResolvedRunConfig:
    fingerprint: str
    provider_name: str
    provider_candidates: tuple[ResolvedProviderCandidate, ...]
    routing_policy: RoutingPolicy
    pipeline: PipelineConfig
    instructions: dict[str, Any] | None
    instruction_reference: str | None
    credential_reference: SecretReference | None


class _DeferredExtractionProvider:
    """Non-secret placeholder for stages that cannot invoke extraction.

    A hosted credential is deliberately resolved only when the orchestrator
    is about to execute the extracting stage.  The other five stages still
    need the pinned pipeline configuration, but constructing their executors
    with a real hosted adapter would read the tenant secret on every stage and
    retain it until that stage finished.
    """

    @property
    def name(self) -> str:
        return "deferred"

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        del request
        raise ExtractionProviderError(
            "the extraction provider was not resolved for this stage",
            retryable=False,
        )


def snapshot_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """Return the API resolver's canonical SHA-256 fingerprint."""
    material = {key: value for key, value in snapshot.items() if key != "fingerprint"}
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def execution_fingerprint(run: ProcessingRun) -> str:
    material = {
        "stream_version_id": str(run.stream_version_id) if run.stream_version_id else None,
        "config_fingerprint": run.config_fingerprint,
        "instruction_version_id": (
            str(run.instruction_version_id) if run.instruction_version_id else None
        ),
        "confidence_policy_version_id": (
            str(run.confidence_policy_version_id) if run.confidence_policy_version_id else None
        ),
        "provider_policy_version_id": (
            str(run.provider_policy_version_id) if run.provider_policy_version_id else None
        ),
        "provider_credential_ref": run.provider_credential_ref,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


async def resolve_stream_pins(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_id: uuid.UUID,
) -> dict[str, Any]:
    """Resolve the immutable runtime pins for a stream's ACTIVE version,
    worker-side — the routing counterpart of the API's intake-time
    ``resolve_runtime_pins``. Used when the classify stage re-routes a
    document to its target skill and a fresh run must start under that
    skill's pinned configuration. Fail-closed on anything mutable/missing.

    Returns a ``document.preprocess`` payload fragment with the exact keys
    ``handle_preprocess`` reads."""
    stream_row = (
        (
            await session.execute(
                text(
                    "SELECT active_version_id FROM streams "
                    "WHERE id = :stream_id AND organization_id = :organization_id"
                ),
                {
                    "stream_id": _database_identifier(session, stream_id),
                    "organization_id": _database_identifier(session, context.organization_id),
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if stream_row is None or not stream_row["active_version_id"]:
        raise RunConfigError("the routing target stream has no active published version")
    raw_version = stream_row["active_version_id"]
    stream_version_id = (
        raw_version if isinstance(raw_version, uuid.UUID) else uuid.UUID(str(raw_version))
    )

    version_row = (
        (
            await session.execute(
                text(
                    "SELECT resolved_snapshot, state FROM stream_versions "
                    "WHERE id = :version_id AND organization_id = :organization_id"
                ),
                {
                    "version_id": _database_identifier(session, stream_version_id),
                    "organization_id": _database_identifier(session, context.organization_id),
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if version_row is None or version_row["state"] not in ("published", "superseded"):
        raise RunConfigError("the routing target stream version is missing or mutable")
    snapshot = _json_object(version_row["resolved_snapshot"])
    fingerprint = snapshot.get("fingerprint")
    if not isinstance(fingerprint, str) or snapshot_fingerprint(snapshot) != fingerprint:
        raise RunConfigError("the routing target stream snapshot fingerprint is invalid")
    config = snapshot.get("config")
    if not isinstance(config, dict):
        raise RunConfigError("the routing target stream snapshot has no config")

    provider_policy_raw = config.get("provider_policy_version_id")
    if not provider_policy_raw:
        raise RunConfigError("the routing target stream has no provider policy pin")
    provider_policy_id = uuid.UUID(str(provider_policy_raw))
    provider_definition, _ = await _version_definition(
        session,
        context,
        table="policy_versions",
        version_id=provider_policy_id,
        policy_type="provider",
    )
    credential_raw = provider_definition.get("credential_ref")
    provider_credential_ref = str(credential_raw) if credential_raw else None

    confidence_raw = config.get("confidence_policy_version_id")
    confidence_policy_id = uuid.UUID(str(confidence_raw)) if confidence_raw else None

    instruction_row = (
        (
            await session.execute(
                text(
                    "SELECT id FROM instruction_versions "
                    "WHERE stream_version_id = :stream_version_id "
                    "AND organization_id = :organization_id AND state = 'published'"
                ),
                {
                    "stream_version_id": _database_identifier(session, stream_version_id),
                    "organization_id": _database_identifier(session, context.organization_id),
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    raw_instruction = instruction_row["id"] if instruction_row else None
    instruction_version_id = (
        raw_instruction
        if isinstance(raw_instruction, uuid.UUID)
        else (uuid.UUID(str(raw_instruction)) if raw_instruction else None)
    )

    material = {
        "stream_version_id": str(stream_version_id),
        "config_fingerprint": fingerprint,
        "instruction_version_id": str(instruction_version_id) if instruction_version_id else None,
        "confidence_policy_version_id": (
            str(confidence_policy_id) if confidence_policy_id else None
        ),
        "provider_policy_version_id": str(provider_policy_id),
        "provider_credential_ref": provider_credential_ref,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return {**material, "execution_fingerprint": hashlib.sha256(encoded.encode()).hexdigest()}


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise RunConfigError("the pinned stream snapshot is invalid JSON") from error
    if not isinstance(value, dict):
        raise RunConfigError("the pinned stream version has no resolved snapshot")
    return dict(value)


def _render_limits(config: Mapping[str, Any]) -> RenderLimits:
    """Resolve positive, fail-closed render budgets from the immutable snapshot."""

    platform = RenderLimits()

    def bounded_int(key: str, ceiling: int) -> int:
        raw = config.get(key, ceiling)
        try:
            value = int(raw)
        except (TypeError, ValueError) as error:
            raise RunConfigError(f"the pinned {key} render limit is invalid") from error
        if value <= 0:
            raise RunConfigError(f"the pinned {key} render limit must be positive")
        return min(value, ceiling)

    def bounded_float(key: str, ceiling: float) -> float:
        raw = config.get(key, ceiling)
        try:
            value = float(raw)
        except (TypeError, ValueError) as error:
            raise RunConfigError(f"the pinned {key} render limit is invalid") from error
        if value <= 0:
            raise RunConfigError(f"the pinned {key} render limit must be positive")
        return min(value, ceiling)

    total_pixels = bounded_int("max_total_pixels", platform.max_total_pixels)
    return RenderLimits(
        max_pages=bounded_int("max_pages", platform.max_pages),
        max_pixels_per_page=min(platform.max_pixels_per_page, total_pixels),
        max_total_pixels=total_pixels,
        max_decompressed_bytes=bounded_int(
            "max_decompressed_bytes", platform.max_decompressed_bytes
        ),
        dpi=platform.dpi,
        timeout_seconds=bounded_float("max_conversion_seconds", platform.timeout_seconds),
        cpu_seconds=platform.cpu_seconds,
        memory_bytes=platform.memory_bytes,
    )


async def verify_run_config(
    session: AsyncSession,
    context: OrganizationContext,
    run: ProcessingRun,
) -> dict[str, Any]:
    """Load and authenticate the exact immutable stream snapshot for ``run``."""
    if run.stream_version_id is None or run.config_fingerprint is None:
        raise RunConfigError("the processing run is missing its immutable configuration pin")
    if run.provider_policy_version_id is None or run.execution_fingerprint is None:
        raise RunConfigError("the processing run is missing its provider or execution pin")
    if execution_fingerprint(run) != run.execution_fingerprint:
        raise RunConfigError("the processing run execution fingerprint is invalid")
    bind = session.get_bind()
    identifier: object = (
        run.stream_version_id.hex if bind.dialect.name == "sqlite" else run.stream_version_id
    )
    organization_id: object = (
        context.organization_id.hex if bind.dialect.name == "sqlite" else context.organization_id
    )
    row = (
        (
            await session.execute(
                text(
                    "SELECT resolved_snapshot, state FROM stream_versions "
                    "WHERE id = :version_id AND organization_id = :organization_id"
                ),
                {"version_id": identifier, "organization_id": organization_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RunConfigError("the pinned stream version does not exist for this tenant")
    if row["state"] not in ("published", "superseded"):
        raise RunConfigError("the pinned stream version is mutable and cannot be executed")
    snapshot = _json_object(row["resolved_snapshot"])
    stored = snapshot.get("fingerprint")
    computed = snapshot_fingerprint(snapshot)
    if not isinstance(stored, str) or stored != computed:
        raise RunConfigError("the pinned stream snapshot fingerprint is invalid")
    if run.config_fingerprint != stored:
        raise RunConfigError("the processing run fingerprint does not match its stream snapshot")
    if not isinstance(snapshot.get("config"), dict):
        raise RunConfigError("the pinned stream snapshot has no executable config object")
    return snapshot


def _database_identifier(session: AsyncSession, value: object) -> object:
    if isinstance(value, uuid.UUID) and session.get_bind().dialect.name == "sqlite":
        return value.hex
    return value


async def _version_definition(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    table: str,
    version_id: uuid.UUID,
    policy_type: str | None = None,
) -> tuple[dict[str, Any], int]:
    if table not in {"schema_versions", "rule_set_versions", "policy_versions"}:
        raise RunConfigError("unsupported configuration version table")
    policy_clause = " AND policy_type = :policy_type" if policy_type else ""
    row = (
        (
            await session.execute(
                text(
                    f"SELECT definition, version_number, state FROM {table} "
                    "WHERE id = :version_id AND organization_id = :organization_id"
                    f"{policy_clause}"
                ),
                {
                    "version_id": _database_identifier(session, version_id),
                    "organization_id": _database_identifier(session, context.organization_id),
                    "policy_type": policy_type,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RunConfigError(f"the pinned {table.removesuffix('_versions')} version is missing")
    if row["state"] not in ("published", "superseded"):
        raise RunConfigError(f"the pinned {table.removesuffix('_versions')} version is mutable")
    return _json_object(row["definition"]), int(row["version_number"])


async def _instruction_definition(
    session: AsyncSession,
    context: OrganizationContext,
    run: ProcessingRun,
) -> tuple[dict[str, Any] | None, str | None]:
    if run.instruction_version_id is None:
        return None, None
    row = (
        (
            await session.execute(
                text(
                    "SELECT content, version_number, state, stream_version_id "
                    "FROM instruction_versions WHERE id = :version_id "
                    "AND organization_id = :organization_id"
                ),
                {
                    "version_id": _database_identifier(session, run.instruction_version_id),
                    "organization_id": _database_identifier(session, context.organization_id),
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["state"] not in ("published", "superseded"):
        raise RunConfigError("the pinned instruction version is missing or mutable")
    linked = uuid.UUID(str(row["stream_version_id"]))
    if linked != run.stream_version_id:
        raise RunConfigError("the pinned instruction belongs to another stream version")
    return (
        _json_object(row["content"]),
        f"instruction:{run.instruction_version_id}:v{int(row['version_number'])}",
    )


def _confidence_policy(
    definition: Mapping[str, Any] | None, version: int | None
) -> ConfidencePolicy:
    if definition is None:
        return ConfidencePolicy()
    floor = float(definition.get("floor", 0.85))
    critical_floor = float(definition.get("critical_floor", max(0.98, floor)))
    overrides = definition.get("field_overrides", {})
    if not isinstance(overrides, dict):
        raise RunConfigError("the pinned confidence policy has invalid field overrides")
    return ConfidencePolicy(
        version=f"policy:{version}",
        critical_min_confidence=critical_floor,
        standard_min_confidence=floor,
        field_min_confidence={str(key): float(value) for key, value in overrides.items()},
        critical_requires_evidence=bool(definition.get("critical_requires_evidence", True)),
        critical_candidate_margin=float(definition.get("critical_candidate_margin", 0.20)),
        standard_candidate_margin=float(definition.get("standard_candidate_margin", 0.05)),
        review_on_indeterminate_error_rules=bool(
            definition.get("review_on_indeterminate_error_rules", True)
        ),
    )


def _required_uuid(config: Mapping[str, Any], key: str) -> uuid.UUID:
    value = config.get(key)
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise RunConfigError(f"the pinned config is missing a valid {key}") from error


def _optional_nonnegative_int(definition: Mapping[str, Any], key: str) -> int | None:
    value = definition.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunConfigError(f"the pinned provider policy has an invalid {key}")
    return value


def _secret_reference(value: object, *, field: str) -> SecretReference | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RunConfigError(f"the pinned provider policy has an invalid {field}")
    try:
        return SecretReference.parse(value)
    except ValueError as error:
        raise RunConfigError(f"the pinned provider policy has an invalid {field}") from error


def _provider_candidates(
    provider_policy: Mapping[str, Any],
    run: ProcessingRun,
) -> tuple[ResolvedProviderCandidate, ...]:
    primary_name = provider_policy.get("provider_name")
    if not isinstance(primary_name, str) or not primary_name.strip():
        raise RunConfigError("the pinned provider policy has no provider name")
    primary_reference = _secret_reference(
        provider_policy.get("credential_ref"), field="credential_ref"
    )
    pinned_primary = str(primary_reference) if primary_reference is not None else None
    if run.provider_credential_ref != pinned_primary:
        raise RunConfigError("the run's credential pin does not match its provider policy")

    candidates = [
        ResolvedProviderCandidate(
            provider_name=primary_name,
            credential_reference=primary_reference,
            estimated_cost_cents=_optional_nonnegative_int(provider_policy, "estimated_cost_cents"),
        )
    ]
    raw_fallbacks = provider_policy.get("fallback_providers", [])
    if not isinstance(raw_fallbacks, list) or len(raw_fallbacks) > 7:
        raise RunConfigError("the pinned provider fallback chain is invalid or too long")
    names = {primary_name}
    for index, raw in enumerate(raw_fallbacks):
        if not isinstance(raw, dict):
            raise RunConfigError("the pinned provider fallback chain contains an invalid entry")
        name = raw.get("provider_name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise RunConfigError("the pinned provider fallback chain contains an invalid name")
        names.add(name)
        candidates.append(
            ResolvedProviderCandidate(
                provider_name=name,
                credential_reference=_secret_reference(
                    raw.get("credential_ref"),
                    field=f"fallback_providers[{index}].credential_ref",
                ),
                estimated_cost_cents=_optional_nonnegative_int(raw, "estimated_cost_cents"),
            )
        )
    return tuple(candidates)


def _routing_policy(
    provider_policy: Mapping[str, Any],
    candidates: tuple[ResolvedProviderCandidate, ...],
) -> RoutingPolicy:
    def boolean(key: str, default: bool = False) -> bool:
        value = provider_policy.get(key, default)
        if not isinstance(value, bool):
            raise RunConfigError(f"the pinned provider policy has an invalid {key}")
        return value

    raw_regions = provider_policy.get("allowed_regions")
    regions: tuple[str, ...] | None = None
    if raw_regions is not None:
        if (
            not isinstance(raw_regions, list)
            or not raw_regions
            or not all(
                isinstance(region, str) and region and region == region.strip().lower()
                for region in raw_regions
            )
            or len(set(raw_regions)) != len(raw_regions)
        ):
            raise RunConfigError("the pinned provider policy has invalid allowed regions")
        regions = tuple(raw_regions)
    raw_quality = provider_policy.get("min_quality", 0.0)
    if (
        isinstance(raw_quality, bool)
        or not isinstance(raw_quality, int | float)
        or not 0 <= float(raw_quality) <= 1
    ):
        raise RunConfigError("the pinned provider policy has an invalid quality floor")
    evaluated_scores: dict[str, float] = {}

    def add_evaluated_score(name: str, raw: object) -> None:
        if raw is None:
            return
        if isinstance(raw, bool) or not isinstance(raw, int | float) or not 0 <= float(raw) <= 1:
            raise RunConfigError(
                f"the pinned provider policy has invalid evaluated quality for {name!r}"
            )
        evaluated_scores[name] = float(raw)

    add_evaluated_score(
        candidates[0].provider_name,
        provider_policy.get("evaluated_quality_score"),
    )
    raw_fallbacks = provider_policy.get("fallback_providers", [])
    if not isinstance(raw_fallbacks, list):
        raise RunConfigError("the pinned provider fallback chain is invalid")
    for candidate, raw in zip(candidates[1:], raw_fallbacks, strict=True):
        if not isinstance(raw, dict):
            raise RunConfigError("the pinned provider fallback chain is invalid")
        add_evaluated_score(
            candidate.provider_name,
            raw.get("evaluated_quality_score"),
        )
    budget = _optional_nonnegative_int(provider_policy, "budget_cents")
    names = tuple(candidate.provider_name for candidate in candidates)
    return RoutingPolicy(
        local_only=boolean("local_only"),
        allowed_regions=regions,
        allow_third_party_processing=boolean("allow_third_party_processing"),
        allow_content_retention=boolean("allow_content_retention"),
        allow_training_on_content=boolean("allow_training_on_content"),
        allowed_providers=names,
        preferred_order=names,
        budget_cents=budget,
        evaluated_quality_scores=evaluated_scores,
        min_quality=float(raw_quality),
    )


def _field_specs(
    definition: Mapping[str, Any],
) -> tuple[tuple[FieldSpec, ...], dict[str, str], dict[str, str]]:
    raw_fields = definition.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise RunConfigError("the pinned schema has no fields")
    specs: list[FieldSpec] = []
    criticality: dict[str, str] = {}
    normalizers: dict[str, str] = {}

    def add(raw: object, *, prefix: str | None = None) -> None:
        if not isinstance(raw, dict):
            raise RunConfigError("the pinned schema contains an invalid field")
        key_value, type_value = raw.get("key"), raw.get("type")
        if not isinstance(key_value, str) or not isinstance(type_value, str):
            raise RunConfigError("the pinned schema field is missing key or type")
        key = f"{prefix}.{key_value}" if prefix else key_value
        enum_values = raw.get("enum_values")
        if enum_values is not None and not (
            isinstance(enum_values, list) and all(isinstance(item, str) for item in enum_values)
        ):
            raise RunConfigError(f"schema field {key!r} has invalid enum values")
        specs.append(
            FieldSpec(
                key=key,
                field_type=type_value,
                enum_values=tuple(enum_values) if isinstance(enum_values, list) else None,
            )
        )
        level = raw.get("criticality", "standard")
        if level not in ("critical", "standard", "informational"):
            raise RunConfigError(f"schema field {key!r} has invalid criticality")
        criticality[key] = str(level)
        normalizer = raw.get("normalization")
        if isinstance(normalizer, str) and normalizer:
            normalizers[key] = normalizer
        if type_value == "table":
            columns = raw.get("columns")
            if not isinstance(columns, list) or not columns:
                raise RunConfigError(f"table field {key!r} has no columns")
            for column in columns:
                add(column, prefix=key)

    for raw_field in raw_fields:
        add(raw_field)
    return tuple(specs), criticality, normalizers


async def load_resolved_run_config(
    session: AsyncSession,
    context: OrganizationContext,
    run: ProcessingRun,
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> ResolvedRunConfig:
    """Resolve the pinned control-plane versions into one runtime value."""
    effective_snapshot = dict(snapshot or await verify_run_config(session, context, run))
    config = _json_object(effective_snapshot.get("config"))
    if CATALOG_VERSION_PINS_KEY not in config:
        raise RunConfigError(
            "the pinned stream snapshot predates immutable catalog pins; republish it"
        )
    try:
        catalog_version_pins = parse_catalog_version_pins(config[CATALOG_VERSION_PINS_KEY])
        if catalog_version_pins is None:
            raise CatalogError("catalog version pins cannot be null")
        await validate_catalog_version_pins(session, context, catalog_version_pins)
    except CatalogError as error:
        raise RunConfigError(f"the pinned catalog contract is invalid: {error}") from error
    schema, _schema_version = await _version_definition(
        session,
        context,
        table="schema_versions",
        version_id=_required_uuid(config, "schema_version_id"),
    )
    rule_set, rule_version = await _version_definition(
        session,
        context,
        table="rule_set_versions",
        version_id=_required_uuid(config, "rule_set_version_id"),
    )
    provider_policy, _policy_version = await _version_definition(
        session,
        context,
        table="policy_versions",
        version_id=_required_uuid(config, "provider_policy_version_id"),
        policy_type="provider",
    )
    expected_provider_policy = _required_uuid(config, "provider_policy_version_id")
    if run.provider_policy_version_id != expected_provider_policy:
        raise RunConfigError("the run's provider policy pin does not match its stream snapshot")
    confidence_definition: dict[str, Any] | None = None
    confidence_version: int | None = None
    configured_confidence = config.get("confidence_policy_version_id")
    if configured_confidence is not None:
        expected_confidence = _required_uuid(config, "confidence_policy_version_id")
        if run.confidence_policy_version_id != expected_confidence:
            raise RunConfigError(
                "the run's confidence policy pin does not match its stream snapshot"
            )
        confidence_definition, confidence_version = await _version_definition(
            session,
            context,
            table="policy_versions",
            version_id=expected_confidence,
            policy_type="confidence",
        )
    elif run.confidence_policy_version_id is not None:
        raise RunConfigError("the run pins a confidence policy absent from its snapshot")
    instructions, instruction_reference = await _instruction_definition(session, context, run)
    specs, criticality, normalizers = _field_specs(schema)
    rules = rule_set.get("rules")
    if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
        raise RunConfigError("the pinned rule set has no rules")
    capabilities = provider_policy.get("capabilities")
    if not isinstance(capabilities, list) or "field_extraction" not in capabilities:
        raise RunConfigError("the pinned provider policy cannot perform field extraction")
    candidates = _provider_candidates(provider_policy, run)
    routing_policy = _routing_policy(provider_policy, candidates)
    for candidate in candidates:
        try:
            info = provider_info(Capability.FIELD_EXTRACTION, candidate.provider_name)
        except Exception as error:
            raise RunConfigError(
                f"pinned extraction provider {candidate.provider_name!r} is unavailable"
            ) from error
        if (
            info.data_policy.sends_content_to_third_party
            and not routing_policy.allow_third_party_processing
        ):
            raise RunConfigError(
                f"the pinned policy does not allow third-party provider {candidate.provider_name!r}"
            )
        if info.data_policy.retains_content and not routing_policy.allow_content_retention:
            raise RunConfigError(
                f"the pinned policy does not allow retention by {candidate.provider_name!r}"
            )
        if (
            info.data_policy.uses_content_for_training
            and not routing_policy.allow_training_on_content
        ):
            raise RunConfigError(
                f"the pinned policy does not allow training by {candidate.provider_name!r}"
            )
        if info.data_policy.sends_content_to_third_party and candidate.credential_reference is None:
            raise RunConfigError(
                f"hosted provider {candidate.provider_name!r} has no credential reference"
            )
    languages = config.get("languages", ["en"])
    if (
        not isinstance(languages, list)
        or not languages
        or not all(isinstance(language, str) and language.strip() for language in languages)
    ):
        raise RunConfigError("the pinned config has invalid languages")
    locale = config.get("locale", "en-US")
    currency = config.get("currency", "USD")
    if not isinstance(locale, str) or not isinstance(currency, str):
        raise RunConfigError("the pinned normalization locale or currency is invalid")
    input_contract = config.get("input_contract", "single_sales_order")
    if input_contract != "single_sales_order":
        raise RunConfigError("the pinned input contract is not supported by this worker")
    pipeline = PipelineConfig(
        field_specs=specs,
        criticality=criticality,
        rules=[dict(rule) for rule in rules],
        rules_version=str(rule_set.get("version", rule_version)),
        normalization=NormalizationContext(locale=locale, currency=currency),
        normalizer_overrides=normalizers,
        confidence_policy=_confidence_policy(confidence_definition, confidence_version),
        render_limits=_render_limits(config),
        languages=tuple(language.lower() for language in languages),
        stream_config=config,
        input_contract=input_contract,
    )
    return ResolvedRunConfig(
        fingerprint=str(effective_snapshot["fingerprint"]),
        provider_name=candidates[0].provider_name,
        provider_candidates=candidates,
        routing_policy=routing_policy,
        pipeline=pipeline,
        instructions=instructions,
        instruction_reference=instruction_reference,
        credential_reference=candidates[0].credential_reference,
    )


def _bind_routing_execution(
    executors: dict[str, StageExecutor],
    provider: RoutedExtractionProvider,
) -> dict[str, StageExecutor]:
    extraction = executors.get("extracting")
    if extraction is None:
        raise RunConfigError("the pipeline has no extraction executor")

    async def execute_with_routing_context(
        session: AsyncSession,
        context: OrganizationContext,
        run: ProcessingRun,
        document: Document,
        stage_run: StageRun,
    ) -> StageOutcome:
        with provider.bind_execution(session, context, run, stage_run):
            return await extraction(session, context, run, document, stage_run)

    return {**executors, "extracting": execute_with_routing_context}


class ResolvedExecutorFactory:
    """Verify every stage and cache executors only for authenticated snapshots."""

    def __init__(self, store: ObjectStore, secret_store: SecretStore) -> None:
        self._store = store
        self._secret_store = secret_store
        # Credential-free adapters are safe to reuse for every stage. Hosted
        # runs use a separate cache containing only the deferred provider.
        self._cache: dict[tuple[uuid.UUID, uuid.UUID, str], dict[str, StageExecutor]] = {}
        self._non_extract_cache: dict[
            tuple[uuid.UUID, uuid.UUID, str], dict[str, StageExecutor]
        ] = {}

    async def __call__(
        self,
        session: AsyncSession,
        context: OrganizationContext,
        run: ProcessingRun,
        stage: str,
    ) -> Mapping[str, StageExecutor]:
        snapshot = await verify_run_config(session, context, run)
        if run.stream_version_id is None or run.config_fingerprint is None:
            raise RunConfigError("the processing run is missing its immutable configuration pin")
        if run.execution_fingerprint is None:
            raise RunConfigError("the processing run is missing its execution fingerprint")
        key = (context.organization_id, run.stream_version_id, run.execution_fingerprint)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if stage != "extracting":
            cached = self._non_extract_cache.get(key)
            if cached is not None:
                return cached
        resolved = await load_resolved_run_config(
            session,
            context,
            run,
            snapshot=snapshot,
        )
        has_tenant_credentials = any(
            candidate.credential_reference is not None for candidate in resolved.provider_candidates
        )
        if stage != "extracting" and has_tenant_credentials:
            # These executors close over no secret material.  The deferred
            # adapter is unreachable because the orchestrator selects exactly
            # the executor for ``stage``; it remains fail closed if that
            # invariant is ever violated.
            executors = build_executors(
                self._store,
                _DeferredExtractionProvider(),
                resolved.pipeline,
            )
            self._non_extract_cache[key] = executors
            return executors
        credential_values: list[str | None] = []
        release_database = has_tenant_credentials and session is not None
        if release_database:
            # Control-plane pins are fully snapshotted. Release SQL before a
            # cloud Secret Manager round trip; restore the tenant binding for
            # either executor work or durable failure recording.
            await session.commit()
        try:
            for candidate in resolved.provider_candidates:
                credential_value = None
                if candidate.credential_reference is not None:
                    try:
                        credential_value = await self._secret_store.resolve(
                            candidate.credential_reference
                        )
                    except SecretNotFoundError as error:
                        raise RunConfigError(
                            f"credential for provider {candidate.provider_name!r} "
                            "is missing or revoked"
                        ) from error
                    except SecretStoreUnavailableError as error:
                        raise StageExecutionError(
                            "the provider credential service is temporarily unavailable",
                            retryable=True,
                        ) from error
                    except SecretStoreError as error:
                        raise RunConfigError(
                            f"credential for provider {candidate.provider_name!r} is invalid"
                        ) from error
                credential_values.append(credential_value)
        finally:
            if release_database:
                await bind_tenant(session, context.organization_id)

        configured: list[ConfiguredExtractionProvider] = []
        for candidate, credential_value in zip(
            resolved.provider_candidates,
            credential_values,
            strict=True,
        ):
            runtime: dict[str, Any] = {}
            if candidate.provider_name != "mock":
                runtime = {
                    "instructions": resolved.instructions,
                    "instruction_reference": resolved.instruction_reference,
                    "credential_value": credential_value,
                }
            try:
                provider = create_provider(
                    Capability.FIELD_EXTRACTION,
                    candidate.provider_name,
                    **runtime,
                )
            except Exception as error:
                raise RunConfigError(
                    f"provider {candidate.provider_name!r} could not be constructed"
                ) from error
            if not isinstance(provider, ExtractionProvider):
                raise RunConfigError(
                    f"provider {candidate.provider_name!r} does not implement field extraction"
                )
            configured.append(
                ConfiguredExtractionProvider(
                    provider=provider,
                    estimated_cost_cents=candidate.estimated_cost_cents,
                )
            )
        routed_provider = RoutedExtractionProvider(
            tuple(configured),
            policy=resolved.routing_policy,
            language=resolved.pipeline.languages[0],
        )
        executors = _bind_routing_execution(
            build_executors(self._store, routed_provider, resolved.pipeline),
            routed_provider,
        )
        # Never cache an adapter containing resolved tenant secret material.
        # Re-resolving on each stage call makes revocation effective without a
        # worker restart. Credential-free local/mock pipelines remain safe to
        # share across runs and keep the fast path.
        if not has_tenant_credentials:
            self._cache[key] = executors
        return executors


__all__ = [
    "ResolvedExecutorFactory",
    "ResolvedProviderCandidate",
    "ResolvedRunConfig",
    "RunConfigError",
    "execution_fingerprint",
    "load_resolved_run_config",
    "snapshot_fingerprint",
    "verify_run_config",
]
