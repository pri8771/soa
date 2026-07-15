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
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRun


class RunConfigError(ValueError):
    """The run's immutable stream configuration cannot be trusted."""


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


__all__ = ["RunConfigError", "snapshot_fingerprint", "verify_run_config"]
