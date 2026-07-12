import uuid
from dataclasses import dataclass

import pytest

from soa_db import (
    CursorRequest,
    InvalidCursorError,
    build_page,
    decode_cursor,
    encode_cursor,
)


@dataclass
class Row:
    id: uuid.UUID


def make_rows(count: int) -> list[Row]:
    return [Row(id=uuid.UUID(int=i + 1)) for i in range(count)]


def test_cursor_round_trip() -> None:
    original = uuid.uuid4()
    assert decode_cursor(encode_cursor(original)) == original


def test_malformed_cursor_raises() -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor("not-a-cursor!!")


def test_limit_bounds_are_enforced() -> None:
    with pytest.raises(ValueError):
        CursorRequest(limit=0)
    with pytest.raises(ValueError):
        CursorRequest(limit=201)
    assert CursorRequest(limit=200).limit == 200


def test_page_without_more_rows() -> None:
    rows = make_rows(2)
    page = build_page(rows, limit=5, id_of=lambda r: r.id)
    assert page.items == rows
    assert page.has_more is False
    assert page.next_cursor is None


def test_page_with_more_rows_chains_via_cursor() -> None:
    rows = make_rows(6)  # limit + 1 fetched
    page = build_page(rows, limit=5, id_of=lambda r: r.id)
    assert len(page.items) == 5
    assert page.has_more is True
    assert page.next_cursor is not None
    assert decode_cursor(page.next_cursor) == rows[4].id
