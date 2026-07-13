"""Version-lifecycle machinery — moved to soa_db.versioning (EXP-008)
so the worker can share it; this shim keeps existing imports stable."""

from soa_db.versioning import (
    ImmutablePublishedVersionMixin,
    ImmutableVersionError,
    InvalidVersionStateError,
    VersionState,
)

__all__ = [
    "ImmutablePublishedVersionMixin",
    "ImmutableVersionError",
    "InvalidVersionStateError",
    "VersionState",
]
