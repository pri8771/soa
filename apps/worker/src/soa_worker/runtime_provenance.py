"""Safe, deterministic provenance for the code and services that executed a run.

The control-plane execution fingerprint proves which immutable configuration
was requested.  This module supplies the complementary runtime fingerprint:
the renderer, recognizers, adapter, model, and endpoint identity that actually
handled the bytes.  Endpoint URLs are canonicalized and hashed so credentials,
query strings, and internal topology never enter durable records.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.parse import urlsplit, urlunsplit

RUNTIME_PROVENANCE_SCHEMA = 1

_SAFE_TEXT_FIELDS = ("adapter", "api_version", "model")
_SAFE_NUMBER_FIELDS = ("timeout_seconds", "temperature")
_SAFE_INTEGER_FIELDS = ("max_tokens", "seed")
_SAFE_PRICING_FIELDS = (
    "reference",
    "input_cents_per_million",
    "output_cents_per_million",
)


def package_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "unknown"


def endpoint_fingerprint(endpoint: str) -> str:
    """Hash a normalized URL after dropping credentials, query, and fragment."""

    parsed = urlsplit(endpoint.strip())
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    canonical = urlunsplit(
        (
            parsed.scheme.lower(),
            f"{hostname.lower()}{port}",
            parsed.path.rstrip("/") or "/",
            "",
            "",
        )
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def safe_adapter_provenance(provider: object) -> dict[str, Any]:
    """Return the allowlisted, JSON-safe provenance declared by an adapter.

    Runtime provenance is durable audit material, so an extension adapter
    cannot add an arbitrary key such as ``api_key`` and accidentally persist a
    secret.  New safe runtime knobs must be deliberately added to this
    allowlist.  Endpoint identity is accepted only as a SHA-256 digest.
    """

    declared = getattr(provider, "runtime_provenance", {})
    if not isinstance(declared, Mapping):
        return {}

    safe: dict[str, Any] = {}
    for key in _SAFE_TEXT_FIELDS:
        value = declared.get(key)
        if isinstance(value, str):
            safe[key] = value

    endpoint_sha256 = declared.get("endpoint_sha256")
    if (
        isinstance(endpoint_sha256, str)
        and len(endpoint_sha256) == 64
        and all(character in "0123456789abcdef" for character in endpoint_sha256.lower())
    ):
        safe["endpoint_sha256"] = endpoint_sha256.lower()

    for key in _SAFE_NUMBER_FIELDS:
        value = declared.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            safe[key] = value

    for key in _SAFE_INTEGER_FIELDS:
        value = declared.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            safe[key] = value

    pricing = declared.get("pricing")
    if isinstance(pricing, Mapping):
        safe_pricing = {
            key: value
            for key in _SAFE_PRICING_FIELDS
            if (value := pricing.get(key)) is not None
            and (
                isinstance(value, str)
                or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)
            )
        }
        if safe_pricing:
            safe["pricing"] = safe_pricing
    return safe


def renderer_provenance(content_type: str) -> dict[str, Any]:
    engine = (
        {"name": "pypdfium2", "version": package_version("pypdfium2")}
        if content_type == "application/pdf"
        else {"name": "Pillow", "version": package_version("Pillow")}
    )
    return {
        "adapter": "soa_worker.render_sandbox",
        "adapter_contract_version": RUNTIME_PROVENANCE_SCHEMA,
        "engine": engine,
    }


def extraction_provenance(provider: object, *, name: str, model: str | None) -> dict[str, Any]:
    """Return declared adapter identity plus the result's actual provider/model."""

    declared = getattr(provider, "runtime_provenance", {})
    safe_declared = dict(declared) if isinstance(declared, dict) else {}
    return {
        "adapter": f"{type(provider).__module__}.{type(provider).__qualname__}",
        "adapter_contract_version": RUNTIME_PROVENANCE_SCHEMA,
        **safe_declared,
        "provider": name,
        "model": model,
    }


def runtime_fingerprint(provenance: dict[str, Any]) -> str:
    encoded = json.dumps(provenance, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


__all__ = [
    "RUNTIME_PROVENANCE_SCHEMA",
    "endpoint_fingerprint",
    "extraction_provenance",
    "package_version",
    "renderer_provenance",
    "runtime_fingerprint",
    "safe_adapter_provenance",
]
