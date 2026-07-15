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
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.parse import urlsplit, urlunsplit

RUNTIME_PROVENANCE_SCHEMA = 1


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
]
