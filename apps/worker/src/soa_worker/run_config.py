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

from soa_config import SecretReference, SecretStore
from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRun
from soa_normalize import NormalizationContext
from soa_rules import ConfidencePolicy
from soa_storage import ObjectStore
from soa_worker.extraction.provider import ExtractionProvider, FieldSpec
from soa_worker.orchestrator import StageExecutor
from soa_worker.pipeline import PipelineConfig, build_executors
from soa_worker.providers import Capability, create_provider, provider_info


class RunConfigError(ValueError):
    """The run's immutable stream configuration cannot be trusted."""


@dataclass(frozen=True)
class ResolvedRunConfig:
    fingerprint: str
    provider_name: str
    pipeline: PipelineConfig
    instructions: dict[str, Any] | None
    instruction_reference: str | None
    credential_reference: SecretReference | None


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


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise RunConfigError("the pinned stream snapshot is invalid JSON") from error
    if not isinstance(value, dict):
        raise RunConfigError("the pinned stream version has no resolved snapshot")
    return dict(value)


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
    provider_name = provider_policy.get("provider_name")
    capabilities = provider_policy.get("capabilities")
    if not isinstance(provider_name, str) or not provider_name:
        raise RunConfigError("the pinned provider policy has no provider name")
    if not isinstance(capabilities, list) or "field_extraction" not in capabilities:
        raise RunConfigError("the pinned provider policy cannot perform field extraction")
    info = provider_info(Capability.FIELD_EXTRACTION, provider_name)
    allow_third_party = bool(provider_policy.get("allow_third_party_processing", False))
    allow_retention = bool(provider_policy.get("allow_content_retention", False))
    allow_training = bool(provider_policy.get("allow_training_on_content", False))
    if info.data_policy.sends_content_to_third_party and not allow_third_party:
        raise RunConfigError("the pinned policy does not allow third-party processing")
    if info.data_policy.retains_content and not allow_retention:
        raise RunConfigError("the pinned policy does not allow provider retention")
    if info.data_policy.uses_content_for_training and not allow_training:
        raise RunConfigError("the pinned policy does not allow training on content")
    credential_reference: SecretReference | None = None
    policy_credential = provider_policy.get("credential_ref")
    if policy_credential:
        if run.provider_credential_ref != str(policy_credential):
            raise RunConfigError("the run's credential pin does not match its provider policy")
        try:
            credential_reference = SecretReference.parse(str(policy_credential))
        except ValueError as error:
            raise RunConfigError("the pinned provider credential reference is invalid") from error
    elif run.provider_credential_ref is not None:
        raise RunConfigError("the run pins a credential absent from its provider policy")
    if info.data_policy.sends_content_to_third_party and credential_reference is None:
        raise RunConfigError("the hosted provider policy has no credential reference")
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
        languages=tuple(language.lower() for language in languages),
        stream_config=config,
        input_contract=input_contract,
    )
    return ResolvedRunConfig(
        fingerprint=str(effective_snapshot["fingerprint"]),
        provider_name=provider_name,
        pipeline=pipeline,
        instructions=instructions,
        instruction_reference=instruction_reference,
        credential_reference=credential_reference,
    )


class ResolvedExecutorFactory:
    """Verify every stage and cache executors only for authenticated snapshots."""

    def __init__(self, store: ObjectStore, secret_store: SecretStore) -> None:
        self._store = store
        self._secret_store = secret_store
        self._cache: dict[tuple[uuid.UUID, uuid.UUID, str], dict[str, StageExecutor]] = {}

    async def __call__(
        self,
        session: AsyncSession,
        context: OrganizationContext,
        run: ProcessingRun,
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
        resolved = await load_resolved_run_config(
            session,
            context,
            run,
            snapshot=snapshot,
        )
        credential_value = None
        if resolved.credential_reference is not None:
            try:
                credential_value = await self._secret_store.resolve(resolved.credential_reference)
            except Exception as error:
                raise RunConfigError("the pinned provider credential cannot be resolved") from error
        runtime: dict[str, Any] = {}
        if resolved.provider_name != "mock":
            runtime = {
                "instructions": resolved.instructions,
                "instruction_reference": resolved.instruction_reference,
                "credential_value": credential_value,
            }
        provider = create_provider(
            Capability.FIELD_EXTRACTION,
            resolved.provider_name,
            **runtime,
        )
        if not isinstance(provider, ExtractionProvider):
            raise RunConfigError(
                f"provider {resolved.provider_name!r} does not implement field extraction"
            )
        executors = build_executors(self._store, provider, resolved.pipeline)
        self._cache[key] = executors
        return executors


__all__ = [
    "ResolvedExecutorFactory",
    "ResolvedRunConfig",
    "RunConfigError",
    "execution_fingerprint",
    "load_resolved_run_config",
    "snapshot_fingerprint",
    "verify_run_config",
]
