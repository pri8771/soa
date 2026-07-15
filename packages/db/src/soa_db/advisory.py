"""Portable helpers for PostgreSQL transaction-scoped serialization.

Some invariants protect the absence of a row (for example, one current
credential or a bounded count). A row lock cannot serialize that case, so the
production database uses a stable, namespaced advisory lock. SQLite remains a
lightweight single-process development/test backend and intentionally no-ops.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def transaction_advisory_lock(
    session: AsyncSession,
    namespace: str,
    *parts: object,
) -> None:
    """Hold one deterministic signed 64-bit lock until transaction end."""

    if session.get_bind().dialect.name != "postgresql":
        return
    material = "\x1f".join((namespace, *(str(part) for part in parts))).encode()
    unsigned = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    key = unsigned if unsigned < 2**63 else unsigned - 2**64
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


__all__ = ["transaction_advisory_lock"]
