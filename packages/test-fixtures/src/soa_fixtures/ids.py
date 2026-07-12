"""Stable, deterministic fixture identifiers."""

import uuid

# Fixed namespace for SOA fixture aliases. Never change this value: doing so
# silently changes every fixture ID and breaks tests that reference them.
FIXTURE_NAMESPACE = uuid.UUID("6f3a4f6e-7d1c-4c1a-9d3e-2b8a5c9e0f42")


def stable_id(alias: str) -> uuid.UUID:
    """Derive the UUID for a fixture alias (idempotent across runs)."""
    return uuid.uuid5(FIXTURE_NAMESPACE, alias)
