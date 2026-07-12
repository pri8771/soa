"""Cursor pagination primitives.

Cursor pagination (not offset) per docs/ARCHITECTURE.md §12: stable under
concurrent inserts and cheap on large queues. The cursor encodes the last
row's sort key; pages fetch ``limit + 1`` rows to compute ``has_more``
without a count query.
"""

import base64
import binascii
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


class InvalidCursorError(Exception):
    pass


@dataclass(frozen=True)
class CursorRequest:
    limit: int = DEFAULT_PAGE_SIZE
    after: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    has_more: bool
    next_cursor: str | None


def encode_cursor(last_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(last_id.bytes).decode("ascii")


def decode_cursor(cursor: str) -> uuid.UUID:
    try:
        return uuid.UUID(bytes=base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (ValueError, binascii.Error) as exc:
        raise InvalidCursorError(f"malformed cursor: {cursor!r}") from exc


def build_page(rows: list[T], limit: int, *, id_of: Callable[[T], uuid.UUID]) -> Page[T]:
    """Assemble a page from ``limit + 1`` fetched rows."""
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = encode_cursor(id_of(items[-1])) if has_more and items else None
    return Page(items=items, has_more=has_more, next_cursor=next_cursor)
