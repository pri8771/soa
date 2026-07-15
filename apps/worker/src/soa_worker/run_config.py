"""Fail-closed verification of the immutable configuration pinned to a run.

The control-plane configuration tables intentionally live outside ``soa_db``.
The worker therefore reads the published snapshot through a narrow SQL boundary
instead of importing API service models.  This module does not pretend to turn
that snapshot into runtime executors yet; it guarantees that a stage cannot run
when its stream version is missing, belongs to another tenant, is mutable, or
does not match the fingerprint captured at intake.
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

from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRun
from soa_normalize import NormalizationContext
from soa_storage import ObjectStore
from soa_worker.extraction.provider import ExtractionProvider, FieldSpec
from soa_worker.orchestrator import StageExecutor
from soa_worker.pipeline import PipelineConfig, build_executors
from soa_worker.providers import Capability, create_provider


class RunConfigError(ValueError):
    """The run's immutable stream configuration cannot be trusted."""


@dataclass(frozen=True)
class ResolvedRunConfig:
    fingerprint: str
    provider_name: str
    pipeline: PipelineConfig


def snapshot_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """Return the API resolver's canonical SHA-256 fingerprint."""
    material = {key: value for key, value in snapshot.items() if key != "fingerprint"}
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
    pipeline = PipelineConfig(
        field_specs=specs,
        criticality=criticality,
        rules=[dict(rule) for rule in rules],
        rules_version=str(rule_set.get("version", rule_version)),
        normalization=NormalizationContext(locale=locale, currency=currency),
        normalizer_overrides=normalizers,
        languages=tuple(language.lower() for language in languages),
    )
    return ResolvedRunConfig(
        fingerprint=str(effective_snapshot["fingerprint"]),
        provider_name=provider_name,
        pipeline=pipeline,
    )


class ResolvedExecutorFactory:
    """Verify every stage and cache executors only for authenticated snapshots."""

    def __init__(self, store: ObjectStore) -> None:
        self._store = store
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
        key = (context.organization_id, run.stream_version_id, run.config_fingerprint)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        resolved = await load_resolved_run_config(
            session,
            context,
            run,
            snapshot=snapshot,
        )
        provider = create_provider(Capability.FIELD_EXTRACTION, resolved.provider_name)
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
    "load_resolved_run_config",
    "snapshot_fingerprint",
    "verify_run_config",
]
