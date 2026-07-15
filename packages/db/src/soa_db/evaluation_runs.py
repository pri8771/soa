"""Durable, tenant-scoped evaluation runs and promotion evidence."""

import hashlib
import json
import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow

CALLER_SUBMITTED_EVIDENCE_SOURCE = "caller_submitted_predictions"
PROMOTABLE_EVIDENCE_SOURCE = "server_executed_candidate"
SERVER_ATTESTATION_SCHEMA_VERSION = 1
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class EvaluationExecutionMode(StrEnum):
    SIMULATION = "simulation"
    SERVER = "server"


class EvaluationRunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EvaluationRun(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "evaluation_runs"

    stream_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    candidate_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    baseline_run_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    execution_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EvaluationExecutionMode.SIMULATION.value
    )
    stream_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    candidate_snapshot: Mapped[dict[str, Any] | None] = mapped_column(PORTABLE_JSON, nullable=True)
    runtime_pins: Mapped[dict[str, Any] | None] = mapped_column(PORTABLE_JSON, nullable=True)
    execution_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    allow_external_provider: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    predictions: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    report: Mapped[dict[str, Any] | None] = mapped_column(PORTABLE_JSON, nullable=True)
    gate_result: Mapped[dict[str, Any] | None] = mapped_column(PORTABLE_JSON, nullable=True)
    attestation: Mapped[dict[str, Any] | None] = mapped_column(PORTABLE_JSON, nullable=True)
    safe_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class EvaluationRunRepository(ScopedRepository[EvaluationRun]):
    model = EvaluationRun

    async def latest_for_stream(self, stream_id: uuid.UUID) -> list[EvaluationRun]:
        stmt = (
            self._scoped_select()
            .where(EvaluationRun.stream_id == stream_id)
            .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def passed_for_candidate(
        self, stream_id: uuid.UUID, candidate_fingerprint: str
    ) -> EvaluationRun | None:
        stmt = (
            self._scoped_select()
            .where(
                EvaluationRun.stream_id == stream_id,
                EvaluationRun.candidate_fingerprint == candidate_fingerprint,
                EvaluationRun.state == EvaluationRunState.SUCCEEDED,
            )
            .order_by(EvaluationRun.finished_at.desc(), EvaluationRun.id.desc())
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        # Fail closed for legacy or forged gate JSON.  Only an attested
        # server-executed candidate may satisfy the publication gate;
        # caller-supplied simulations are useful diagnostics, never evidence.
        return next((row for row in rows if is_promotable_server_evidence(row)), None)


def canonical_json_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    return canonical_json_digest(
        {key: value for key, value in snapshot.items() if key != "fingerprint"}
    )


def runtime_pin_digest(pins: dict[str, Any]) -> str:
    material = {
        "stream_version_id": pins.get("stream_version_id"),
        "config_fingerprint": pins.get("config_fingerprint"),
        "instruction_version_id": pins.get("instruction_version_id"),
        "confidence_policy_version_id": pins.get("confidence_policy_version_id"),
        "provider_policy_version_id": pins.get("provider_policy_version_id"),
        "provider_credential_ref": pins.get("provider_credential_ref"),
    }
    return canonical_json_digest(material)


def attestation_digest(attestation: dict[str, Any]) -> str:
    return canonical_json_digest(
        {key: value for key, value in attestation.items() if key != "manifest_fingerprint"}
    )


def _uuid_string(value: object) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return False
    return isinstance(value, str)


def is_promotable_server_evidence(run: EvaluationRun) -> bool:
    """Validate every durable link in server evidence before publication may use it.

    Gate JSON is not trusted on its own: it is useful presentation data and can
    pre-date this contract.  Promotion requires an authenticated candidate
    snapshot, matching runtime pins, a complete source/runtime manifest, and a
    successful report with no skipped or failed documents.
    """

    gate = run.gate_result or {}
    report = run.report or {}
    snapshot = run.candidate_snapshot
    pins = run.runtime_pins
    attestation = run.attestation
    if (
        run.execution_mode != EvaluationExecutionMode.SERVER.value
        or run.predictions
        or not isinstance(snapshot, dict)
        or not isinstance(pins, dict)
        or not isinstance(attestation, dict)
        or run.stream_version_id is None
        or not isinstance(run.execution_fingerprint, str)
        or not _SHA256_HEX.fullmatch(run.execution_fingerprint)
    ):
        return False
    candidate = run.candidate_fingerprint
    config = snapshot.get("config")
    if (
        not _SHA256_HEX.fullmatch(candidate)
        or not isinstance(config, dict)
        or snapshot.get("fingerprint") != candidate
        or snapshot_digest(snapshot) != candidate
    ):
        return False
    provider_policy_id = pins.get("provider_policy_version_id")
    confidence_policy_id = pins.get("confidence_policy_version_id")
    instruction_version_id = pins.get("instruction_version_id")
    credential_reference = pins.get("provider_credential_ref")
    if (
        pins.get("stream_version_id") != str(run.stream_version_id)
        or pins.get("config_fingerprint") != candidate
        or provider_policy_id != config.get("provider_policy_version_id")
        or not _uuid_string(provider_policy_id)
        or confidence_policy_id
        != (
            str(config.get("confidence_policy_version_id"))
            if config.get("confidence_policy_version_id") is not None
            else None
        )
        or (confidence_policy_id is not None and not _uuid_string(confidence_policy_id))
        or (instruction_version_id is not None and not _uuid_string(instruction_version_id))
        or (
            credential_reference is not None
            and (
                not isinstance(credential_reference, str)
                or not credential_reference.startswith("secretref://")
            )
        )
        or pins.get("execution_fingerprint") != run.execution_fingerprint
        or runtime_pin_digest(pins) != run.execution_fingerprint
    ):
        return False
    documents = attestation.get("documents")
    manifest_fingerprint = attestation.get("manifest_fingerprint")
    if (
        attestation.get("schema_version") != SERVER_ATTESTATION_SCHEMA_VERSION
        or attestation.get("execution_mode") != EvaluationExecutionMode.SERVER.value
        or attestation.get("candidate_fingerprint") != candidate
        or attestation.get("execution_fingerprint") != run.execution_fingerprint
        or attestation.get("dataset_version_id") != str(run.dataset_version_id)
        or not isinstance(documents, dict)
        or not documents
        or not isinstance(manifest_fingerprint, str)
        or not _SHA256_HEX.fullmatch(manifest_fingerprint)
        or attestation_digest(attestation) != manifest_fingerprint
    ):
        return False
    catalog_snapshots: set[str] = set()
    for sha, evidence in documents.items():
        runtime_provenance = (
            evidence.get("runtime_provenance") if isinstance(evidence, dict) else None
        )
        if (
            not isinstance(sha, str)
            or not _SHA256_HEX.fullmatch(sha)
            or not isinstance(evidence, dict)
            or evidence.get("document_sha256") != sha
            or not _uuid_string(evidence.get("source_document_id"))
            or not _uuid_string(evidence.get("source_artifact_id"))
            or not isinstance(evidence.get("runtime_fingerprint"), str)
            or not _SHA256_HEX.fullmatch(str(evidence.get("runtime_fingerprint")))
            or not isinstance(runtime_provenance, dict)
            or canonical_json_digest(runtime_provenance) != evidence.get("runtime_fingerprint")
        ):
            return False
        catalog_snapshots.add(
            canonical_json_digest(runtime_provenance.get("business_catalog_versions", {}))
        )
    if len(catalog_snapshots) > 1:
        return False
    scored = report.get("documents_scored")
    if (
        not isinstance(scored, int)
        or scored <= 0
        or scored != len(documents)
        or report.get("documents_failed") != 0
    ):
        return False
    return (
        run.state == EvaluationRunState.SUCCEEDED
        and gate.get("passed") is True
        and gate.get("promotion_eligible") is True
        and gate.get("evidence_source") == PROMOTABLE_EVIDENCE_SOURCE
        and gate.get("attestation_fingerprint") == manifest_fingerprint
        and gate.get("execution_fingerprint") == run.execution_fingerprint
    )


async def create_evaluation_run(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_id: uuid.UUID,
    candidate_fingerprint: str,
    dataset_version_id: uuid.UUID,
    predictions: dict[str, Any],
    baseline_run_id: uuid.UUID | None,
    actor_id: str,
    execution_mode: EvaluationExecutionMode = EvaluationExecutionMode.SIMULATION,
    stream_version_id: uuid.UUID | None = None,
    candidate_snapshot: dict[str, Any] | None = None,
    runtime_pins: dict[str, Any] | None = None,
    execution_fingerprint: str | None = None,
    allow_external_provider: bool = False,
) -> EvaluationRun:
    if not _SHA256_HEX.fullmatch(candidate_fingerprint):
        raise ValueError("candidate fingerprint must be a SHA-256 digest")
    if execution_mode is EvaluationExecutionMode.SERVER:
        if predictions:
            raise ValueError("server evaluation cannot accept caller predictions")
        if stream_version_id is None or candidate_snapshot is None or runtime_pins is None:
            raise ValueError("server evaluation requires an immutable candidate execution contract")
        if (
            candidate_snapshot.get("fingerprint") != candidate_fingerprint
            or snapshot_digest(candidate_snapshot) != candidate_fingerprint
        ):
            raise ValueError("candidate snapshot fingerprint is invalid")
        if (
            execution_fingerprint is None
            or runtime_pins.get("stream_version_id") != str(stream_version_id)
            or runtime_pins.get("config_fingerprint") != candidate_fingerprint
            or runtime_pins.get("execution_fingerprint") != execution_fingerprint
            or runtime_pin_digest(runtime_pins) != execution_fingerprint
        ):
            raise ValueError("candidate runtime pin fingerprint is invalid")
    elif allow_external_provider:
        raise ValueError("simulation evaluation cannot consent to external execution")
    run = EvaluationRunRepository(session, context).add(
        EvaluationRun(
            stream_id=stream_id,
            candidate_fingerprint=candidate_fingerprint,
            dataset_version_id=dataset_version_id,
            predictions=predictions,
            baseline_run_id=baseline_run_id,
            created_by=actor_id,
            execution_mode=execution_mode.value,
            stream_version_id=stream_version_id,
            candidate_snapshot=dict(candidate_snapshot) if candidate_snapshot is not None else None,
            runtime_pins=dict(runtime_pins) if runtime_pins is not None else None,
            execution_fingerprint=execution_fingerprint,
            allow_external_provider=allow_external_provider,
        )
    )
    await session.flush()
    return run


async def start_evaluation_run(run: EvaluationRun) -> None:
    if run.state not in (EvaluationRunState.PENDING, EvaluationRunState.RUNNING):
        raise ValueError(f"evaluation run cannot start from {run.state!r}")
    run.state = EvaluationRunState.RUNNING
    run.started_at = run.started_at or utcnow()


async def finish_evaluation_run(
    run: EvaluationRun,
    *,
    report: dict[str, Any],
    checkpoint: dict[str, Any],
    gate_result: dict[str, Any],
) -> None:
    run.report = report
    run.checkpoint = checkpoint
    run.gate_result = gate_result
    run.state = EvaluationRunState.SUCCEEDED
    run.finished_at = utcnow()


__all__ = [
    "CALLER_SUBMITTED_EVIDENCE_SOURCE",
    "PROMOTABLE_EVIDENCE_SOURCE",
    "SERVER_ATTESTATION_SCHEMA_VERSION",
    "EvaluationExecutionMode",
    "EvaluationRun",
    "EvaluationRunRepository",
    "EvaluationRunState",
    "attestation_digest",
    "canonical_json_digest",
    "create_evaluation_run",
    "finish_evaluation_run",
    "is_promotable_server_evidence",
    "runtime_pin_digest",
    "snapshot_digest",
    "start_evaluation_run",
]
