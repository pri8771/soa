"""Date normalization to ISO 8601 (PRC-008).

Accepted without any context, because they are self-evident:

- year-first numeric: ``2026-03-14``, ``2026/3/14``, ``2026.03.14``;
- English textual months: ``14 Mar 2026``, ``March 14, 2026``,
  ``14-Mar-2026`` (textual months in other languages are NOT guessed);
- day/month numeric where one part exceeds 12, so the order is forced.

Ambiguous numeric dates (``03/04/2026``) require a locale in the
context; without one they are an error, never a guess. Two-digit years
are always refused — no century is ever invented.
"""

import re
from datetime import date

from soa_normalize.context import (
    DEFAULT_CONTEXT,
    NormalizationContext,
    NormalizationError,
    date_order,
)
from soa_normalize.text import normalize_whitespace

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_YEAR_FIRST = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_NUMERIC = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$")
_DAY_MONTH_TEXT = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?[\s.-]+([a-z]+)[\s.,-]+(\d{2,4})$")
_MONTH_DAY_TEXT = re.compile(r"^([a-z]+)[\s.-]+(\d{1,2})(?:st|nd|rd|th)?[\s.,-]+(\d{2,4})$")


def _build(year: int, month: int, day: int) -> str:
    if year < 100:
        raise NormalizationError("two-digit years are ambiguous; a century is never invented")
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        raise NormalizationError(
            f"{year:04d}-{month:02d}-{day:02d} is not a real calendar date"
        ) from None


def normalize_date_iso(raw: str, context: NormalizationContext = DEFAULT_CONTEXT) -> str:
    text = normalize_whitespace(raw).casefold()
    if not text:
        raise NormalizationError("date is empty")

    if match := _YEAR_FIRST.match(text):
        year, month, day = (int(part) for part in match.groups())
        return _build(year, month, day)

    for pattern, month_group in ((_DAY_MONTH_TEXT, 2), (_MONTH_DAY_TEXT, 1)):
        if match := pattern.match(text):
            month_name = match.group(month_group)
            month_number = _MONTHS.get(month_name)
            if month_number is None:
                raise NormalizationError(
                    f"{month_name[:20]!r} is not an English month name; "
                    "other languages are not guessed"
                )
            day_group = 1 if month_group == 2 else 2
            return _build(int(match.group(3)), month_number, int(match.group(day_group)))

    if match := _NUMERIC.match(text):
        first, second, year = (int(part) for part in match.groups())
        if year < 100:
            raise NormalizationError("two-digit years are ambiguous; a century is never invented")
        if first > 12 and second <= 12:
            return _build(year, second, first)  # day first, forced
        if second > 12 and first <= 12:
            return _build(year, first, second)  # month first, forced
        if first == second:
            return _build(year, first, second)  # same date either way
        order = date_order(context)
        if order == "MDY":
            return _build(year, first, second)
        if order == "DMY":
            return _build(year, second, first)
        raise NormalizationError(
            f"{first:02d}/{second:02d} is ambiguous without a locale "
            "(could be day/month or month/day)"
        )

    raise NormalizationError("unrecognized date format")
