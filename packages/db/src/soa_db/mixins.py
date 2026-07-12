"""Model mixins: identity, timestamps, optimistic concurrency."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Mapped, declarative_mixin, declared_attr, mapped_column

from soa_db.types import GUID, UTCDateTime, utcnow, uuid7


class VersionConflictError(Exception):
    """Raised when an expected record version does not match the current one."""

    def __init__(self, *, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"version conflict: expected {expected}, found {actual}")


@declarative_mixin
class UuidPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid7)


@declarative_mixin
class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False
    )


@declarative_mixin
class VersionedMixin:
    """Optimistic concurrency: SQLAlchemy increments ``version`` on every
    UPDATE and raises ``StaleDataError`` when a concurrent writer got there
    first. ``expect_version`` supports API If-Match style preconditions."""

    version: Mapped[int] = mapped_column(nullable=False, default=1)

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:
        return {"version_id_col": cls.version}

    def expect_version(self, expected: int) -> None:
        actual = self.version
        if actual != expected:
            raise VersionConflictError(expected=expected, actual=actual)
