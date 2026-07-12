"""Configuration resolver (CFG-006).

Merges, in precedence order (lowest to highest):

1. platform environment defaults,
2. the pinned process version's definition,
3. the stream's explicit overrides,

and attaches published policy references (CFG-005). The output is
deterministic — same inputs, same bytes, same SHA-256 fingerprint — and
every resolved value carries explicit provenance naming the layer that
supplied it, which is what the inheritance editor (CFG-013) renders.
"""

import hashlib
import json
from typing import Any

from soa_api.domain.policies import PolicyVersion, policy_reference
from soa_api.domain.processes import ProcessVersion
from soa_api.domain.versioning import InvalidVersionStateError, VersionState

#: Values every tenant starts from before any process/stream configuration.
ENVIRONMENT_DEFAULTS: dict[str, Any] = {
    "language": "en",
    "confidence_floor": 0.85,
    "max_pages": 50,
}

PROVENANCE_ENVIRONMENT = "environment"
PROVENANCE_PROCESS = "process"
PROVENANCE_STREAM = "stream"


class ResolvedValue(dict[str, Any]):  # dict for JSON-serializability
    """{"value": ..., "source": "environment"|"process"|"stream"}"""


def resolve_configuration(
    *,
    process_version: ProcessVersion,
    stream_overrides: dict[str, Any],
    policies: list[PolicyVersion] | None = None,
) -> dict[str, Any]:
    """Produce the resolved configuration document with provenance and a
    deterministic fingerprint. Only immutable (published/superseded)
    process versions and published policies may be pinned."""
    if process_version.state == VersionState.DRAFT:
        raise InvalidVersionStateError("resolver only pins immutable process versions")
    for policy in policies or []:
        if policy.state != VersionState.PUBLISHED:
            raise InvalidVersionStateError(
                f"policy {policy.policy_type!r} v{policy.version_number} is not published"
            )

    values: dict[str, ResolvedValue] = {}
    for key, value in sorted(ENVIRONMENT_DEFAULTS.items()):
        values[key] = ResolvedValue(value=value, source=PROVENANCE_ENVIRONMENT)
    for key, value in sorted(process_version.definition.items()):
        values[key] = ResolvedValue(value=value, source=PROVENANCE_PROCESS)
    for key, value in sorted(stream_overrides.items()):
        values[key] = ResolvedValue(value=value, source=PROVENANCE_STREAM)

    resolved = {
        "process_version_id": str(process_version.id),
        "process_version_number": process_version.version_number,
        "policies": sorted(
            (policy_reference(policy) for policy in policies or []),
            key=lambda ref: str(ref["policy_type"]),
        ),
        "values": {key: dict(values[key]) for key in sorted(values)},
    }
    resolved["fingerprint"] = fingerprint(resolved)
    return resolved


def fingerprint(resolved: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON encoding, excluding the fingerprint
    field itself. Stable across key insertion order."""
    material = {key: value for key, value in resolved.items() if key != "fingerprint"}
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def effective_config(resolved: dict[str, Any]) -> dict[str, Any]:
    """Plain key -> value view for the processing pipeline."""
    return {key: entry["value"] for key, entry in resolved["values"].items()}
