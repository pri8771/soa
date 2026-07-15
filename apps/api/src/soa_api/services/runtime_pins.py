"""Resolve the complete immutable execution contract at intake time."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from soa_api.domain.policies import PolicyVersionRepository
from soa_api.domain.streams import StreamVersionRepository
from soa_config import SecretReference
from soa_db.instructions import InstructionVersionRepository
from soa_db.repository import OrganizationContext


class RuntimePinError(ValueError):
    """A stream cannot produce a complete immutable runtime contract."""


@dataclass(frozen=True)
class RuntimePins:
    stream_version_id: uuid.UUID
    config: dict[str, Any]
    config_fingerprint: str
    instruction_version_id: uuid.UUID | None
    confidence_policy_version_id: uuid.UUID | None
    provider_policy_version_id: uuid.UUID
    provider_credential_ref: str | None
    execution_fingerprint: str

    def job_payload(self) -> dict[str, str | None]:
        return {
            "stream_version_id": str(self.stream_version_id),
            "config_fingerprint": self.config_fingerprint,
            "instruction_version_id": (
                str(self.instruction_version_id) if self.instruction_version_id else None
            ),
            "confidence_policy_version_id": (
                str(self.confidence_policy_version_id)
                if self.confidence_policy_version_id
                else None
            ),
            "provider_policy_version_id": str(self.provider_policy_version_id),
            "provider_credential_ref": self.provider_credential_ref,
            "execution_fingerprint": self.execution_fingerprint,
        }


def _uuid(config: dict[str, Any], key: str, *, required: bool) -> uuid.UUID | None:
    raw = config.get(key)
    if raw is None and not required:
        return None
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError, AttributeError) as error:
        requirement = "required" if required else "optional"
        raise RuntimePinError(f"stream config has an invalid {requirement} {key}") from error


def execution_fingerprint(material: dict[str, Any]) -> str:
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    material = {key: value for key, value in snapshot.items() if key != "fingerprint"}
    return execution_fingerprint(material)


async def _resolve_snapshot_pins(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_version_id: uuid.UUID,
    snapshot: dict[str, Any],
) -> RuntimePins:
    config = snapshot.get("config")
    fingerprint = snapshot.get("fingerprint")
    if not isinstance(config, dict) or not isinstance(fingerprint, str):
        raise RuntimePinError("the stream snapshot is incomplete")
    if snapshot_fingerprint(snapshot) != fingerprint:
        raise RuntimePinError("the stream snapshot fingerprint is invalid")

    provider_policy_id = _uuid(config, "provider_policy_version_id", required=True)
    if provider_policy_id is None:  # narrowed by required=True; keeps -O behavior identical
        raise RuntimePinError("stream config has no provider policy pin")
    provider_policy = await PolicyVersionRepository(session, context).get(provider_policy_id)
    if provider_policy is None or provider_policy.policy_type != "provider":
        raise RuntimePinError("the pinned provider policy is missing or has the wrong type")
    if provider_policy.state not in ("published", "superseded"):
        raise RuntimePinError("the pinned provider policy is mutable")
    credential_ref_raw = provider_policy.definition.get("credential_ref")
    credential_ref = str(credential_ref_raw) if credential_ref_raw else None
    if credential_ref is not None:
        try:
            SecretReference.parse(credential_ref)
        except ValueError as error:
            raise RuntimePinError(
                "provider credential_ref must be an immutable secretref:// reference"
            ) from error

    instruction = await InstructionVersionRepository(session, context).get_published(
        stream_version_id
    )
    confidence_policy_id = _uuid(config, "confidence_policy_version_id", required=False)
    if confidence_policy_id is not None:
        confidence = await PolicyVersionRepository(session, context).get(confidence_policy_id)
        if confidence is None or confidence.policy_type != "confidence":
            raise RuntimePinError("the pinned confidence policy is missing or has the wrong type")
        if confidence.state not in ("published", "superseded"):
            raise RuntimePinError("the pinned confidence policy is mutable")

    material = {
        "stream_version_id": str(stream_version_id),
        "config_fingerprint": fingerprint,
        "instruction_version_id": str(instruction.id) if instruction else None,
        "confidence_policy_version_id": (
            str(confidence_policy_id) if confidence_policy_id else None
        ),
        "provider_policy_version_id": str(provider_policy_id),
        "provider_credential_ref": credential_ref,
    }
    return RuntimePins(
        stream_version_id=stream_version_id,
        config=dict(config),
        config_fingerprint=fingerprint,
        instruction_version_id=instruction.id if instruction else None,
        confidence_policy_version_id=confidence_policy_id,
        provider_policy_version_id=provider_policy_id,
        provider_credential_ref=credential_ref,
        execution_fingerprint=execution_fingerprint(material),
    )


async def resolve_runtime_pins(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_version_id: uuid.UUID | None,
) -> RuntimePins:
    if stream_version_id is None:
        raise RuntimePinError("the stream has no active published configuration")
    version = await StreamVersionRepository(session, context).get(stream_version_id)
    if version is None or version.state not in ("published", "superseded"):
        raise RuntimePinError("the active stream version is missing or mutable")
    snapshot = version.resolved_snapshot
    if not isinstance(snapshot, dict):
        raise RuntimePinError("the active stream version has no resolved snapshot")
    return await _resolve_snapshot_pins(
        session,
        context,
        stream_version_id=stream_version_id,
        snapshot=dict(snapshot),
    )


async def resolve_evaluation_runtime_pins(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_version_id: uuid.UUID,
    candidate_snapshot: dict[str, Any],
) -> RuntimePins:
    """Pin a candidate draft for a server evaluation without publishing it.

    The stream row may still be a draft, but every executable dependency
    (schema, rules, provider/confidence policy, and optional instructions)
    remains independently immutable.  The resolved snapshot itself is copied
    into the evaluation run and authenticated here, so later draft edits
    cannot change a queued or retried evaluation.
    """

    version = await StreamVersionRepository(session, context).get(stream_version_id)
    if version is None:
        raise RuntimePinError("the candidate stream version is missing")
    return await _resolve_snapshot_pins(
        session,
        context,
        stream_version_id=stream_version_id,
        snapshot=dict(candidate_snapshot),
    )


__all__ = [
    "RuntimePinError",
    "RuntimePins",
    "execution_fingerprint",
    "resolve_evaluation_runtime_pins",
    "resolve_runtime_pins",
    "snapshot_fingerprint",
]
