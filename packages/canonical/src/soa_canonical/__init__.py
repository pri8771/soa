"""Versioned canonical sales-order schema (CAN-001).

The canonical order is the ERP-neutral contract between review and
export: decimals are exact strings, dates are ISO 8601, money always
carries its currency, nullability is explicit, and every object is
closed except the namespaced ``extensions`` block. The JSON Schema
files under ``schemas/`` are the single source of truth — CAN-002
generates the Python and TypeScript models from them.

Version discipline: schema files are immutable once shipped. A change
means a NEW version file, and ``soa_canonical.compatibility`` enforces
that newer versions only ever widen (add optional fields), never break
existing payloads.
"""

import json
from functools import lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from soa_canonical.compatibility import breaking_changes

CURRENT_VERSION = "1.0.0"


class UnknownSchemaVersionError(Exception):
    def __init__(self, version: str) -> None:
        super().__init__(
            f"unknown canonical schema version {version!r}; shipped versions: "
            f"{', '.join(list_versions())}"
        )


class CanonicalValidationError(Exception):
    """The payload does not conform to the canonical schema. ``errors``
    lists every violation with its JSON path — callers surface all of
    them, not just the first."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        preview = "; ".join(errors[:5])
        more = f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""
        super().__init__(f"canonical payload invalid: {preview}{more}")


def list_versions() -> list[str]:
    """All shipped schema versions, oldest first."""
    versions: list[str] = []
    for entry in resources.files("soa_canonical.schemas").iterdir():
        name = entry.name
        if name.startswith("canonical_order_") and name.endswith(".json"):
            versions.append(
                name.removeprefix("canonical_order_").removesuffix(".json").replace("_", ".")
            )
    return sorted(versions, key=lambda v: tuple(int(part) for part in v.split(".")))


@lru_cache(maxsize=8)
def load_schema(version: str = CURRENT_VERSION) -> dict[str, Any]:
    filename = f"canonical_order_{version.replace('.', '_')}.json"
    try:
        raw = resources.files("soa_canonical.schemas").joinpath(filename).read_text("utf-8")
    except FileNotFoundError:
        raise UnknownSchemaVersionError(version) from None
    schema: dict[str, Any] = json.loads(raw)
    return schema


def _json_path(parts: tuple[Any, ...]) -> str:
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def validate_order(payload: dict[str, Any], *, version: str = CURRENT_VERSION) -> None:
    """Validate a payload against a shipped schema version. Raises
    CanonicalValidationError listing EVERY violation; returns None on
    success."""
    validator = Draft202012Validator(load_schema(version), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.absolute_path))
    if errors:
        raise CanonicalValidationError(
            [f"{_json_path(tuple(error.absolute_path))}: {error.message}" for error in errors]
        )


__all__ = [
    "CURRENT_VERSION",
    "CanonicalValidationError",
    "UnknownSchemaVersionError",
    "breaking_changes",
    "list_versions",
    "load_schema",
    "validate_order",
]
