"""File resource limit policies (ING-005).

Two layers, one rule: streams may configure limits BELOW the platform
maximum, never above it. Platform maxima come from API settings; stream
values come from the resolved stream configuration (CFG-006 keys:
``max_upload_bytes``, ``max_pages``, ``max_total_pixels``,
``max_decompressed_bytes``, ``max_conversion_seconds``) and are clamped
by ``resolve_limits``.

Enforcement points:

- Upload size is enforced at declaration time (before signing) using
  the stream's effective limit.
- Page-count, pixel, decompression (archive/stream-bomb), and
  conversion-time budgets are enforced by the preprocessing stages that
  actually open the file (PRC epic); the checks live here so the policy
  and its tests exist once.

Every violation is auditable via ``record_limit_violation`` — safe
messages that name limits and numbers, never content.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.audit import ActorType, record_audit_event
from soa_db.repository import OrganizationContext


class LimitViolation(Exception):
    """A file exceeds an effective resource limit. Message is UI-safe."""

    def __init__(self, limit_name: str, message: str) -> None:
        self.limit_name = limit_name
        super().__init__(message)


@dataclass(frozen=True)
class FileLimits:
    max_size_bytes: int
    max_pages: int
    max_total_pixels: int
    max_decompressed_bytes: int
    max_conversion_seconds: int


#: Configuration keys a stream may use to lower its limits.
_LIMIT_KEYS: dict[str, str] = {
    "max_size_bytes": "max_upload_bytes",
    "max_pages": "max_pages",
    "max_total_pixels": "max_total_pixels",
    "max_decompressed_bytes": "max_decompressed_bytes",
    "max_conversion_seconds": "max_conversion_seconds",
}


def resolve_limits(platform: FileLimits, stream_config: Mapping[str, Any]) -> FileLimits:
    """Apply stream configuration, clamped to the platform maximum: a
    stream can tighten its intake, never widen the platform's."""
    effective = platform
    for field_name, config_key in _LIMIT_KEYS.items():
        raw = stream_config.get(config_key)
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue  # malformed config never RAISES a limit; ignore it
        if value <= 0:
            continue
        platform_value = int(getattr(platform, field_name))
        effective = replace(effective, **{field_name: min(value, platform_value)})
    return effective


def check_size(size_bytes: int, limits: FileLimits) -> None:
    if size_bytes > limits.max_size_bytes:
        raise LimitViolation(
            "max_size_bytes",
            f"file size {size_bytes} exceeds the {limits.max_size_bytes}-byte limit",
        )


def check_page_count(pages: int, limits: FileLimits) -> None:
    if pages > limits.max_pages:
        raise LimitViolation(
            "max_pages", f"document has {pages} pages; the limit is {limits.max_pages}"
        )


def check_pixels(total_pixels: int, limits: FileLimits) -> None:
    """Guards decompression-style image bombs: a tiny file can declare an
    enormous raster."""
    if total_pixels > limits.max_total_pixels:
        raise LimitViolation(
            "max_total_pixels",
            f"image raster of {total_pixels} pixels exceeds the "
            f"{limits.max_total_pixels}-pixel limit",
        )


def check_decompressed_size(declared_decompressed_bytes: int, limits: FileLimits) -> None:
    """Archive/stream-bomb guard: refuse before inflating, based on the
    declared or estimated decompressed size."""
    if declared_decompressed_bytes > limits.max_decompressed_bytes:
        raise LimitViolation(
            "max_decompressed_bytes",
            f"decompressed content of {declared_decompressed_bytes} bytes exceeds the "
            f"{limits.max_decompressed_bytes}-byte limit",
        )


async def record_limit_violation(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    violation: LimitViolation,
    target_type: str,
    target_id: UUID | str,
    actor_id: str,
    actor_type: ActorType = ActorType.SYSTEM,
) -> None:
    await record_audit_event(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="document.limit_violated",
        target_type=target_type,
        target_id=str(target_id),
        organization_id=context.organization_id,
        summary={"limit": violation.limit_name, "message": str(violation)},
    )
