"""Portable column types enforcing repository conventions.

- ``GUID``: PostgreSQL ``uuid``; CHAR(32) elsewhere (SQLite tests).
- ``UTCDateTime``: timezone-aware only; naive datetimes are rejected at bind
  time and values always come back UTC-aware.
- Money: ``money_column()`` builds NUMERIC(18, 6) — floats are forbidden for
  monetary values — paired with an ISO-4217 ``currency_column()``.
- ``uuid7()``: application-generated, time-sortable UUIDs (RFC 9562).
"""

import os
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CHAR, DateTime, Numeric, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm.properties import MappedColumn
from sqlalchemy.types import TypeDecorator


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562 v7): 48-bit ms timestamp + randomness."""
    timestamp_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (timestamp_ms & 0xFFFFFFFFFFFF) << 80
    value |= 0x7 << 76  # version 7
    value |= (rand >> 6) & (0xFFF << 64)
    value |= 0b10 << 62  # RFC 4122 variant
    value |= rand & 0x3FFFFFFFFFFFFFFF
    return uuid.UUID(int=value)


class GUID(TypeDecorator[uuid.UUID]):
    impl = CHAR(32)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(32))

    def process_bind_param(self, value: uuid.UUID | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return value.hex

    def process_result_value(self, value: Any, dialect: Dialect) -> uuid.UUID | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected: all timestamps must be timezone-aware")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


def money_column(*, nullable: bool = False) -> MappedColumn[Decimal]:
    """NUMERIC(18, 6) monetary column. Never use Float for money."""
    return mapped_column(Numeric(18, 6), nullable=nullable)


def currency_column(*, nullable: bool = False) -> MappedColumn[str]:
    """ISO-4217 currency code column paired with every monetary value."""
    return mapped_column(String(3), nullable=nullable)
