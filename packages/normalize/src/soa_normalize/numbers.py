"""Decimal, money, and currency normalization (PRC-008).

Canonical decimals are exact STRINGS ("1234.50"), never floats: no
grouping, '.' as the decimal point, sign only when negative, written
fractional digits preserved (a stated "…,50" stays ".50"). Separator
roles are taken from the string when it proves them (both separators
present, or a shape only one reading allows); a genuinely two-way string
like "1,234" needs the locale's convention or it is an error.

Money never guesses a currency: unambiguous symbols (€, £, ₹) map to
their code, ambiguous ones ($, ¥, kr) resolve only through explicit
context.currency, and a bare amount takes context.currency or honestly
carries None.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import TypedDict

from soa_normalize.context import (
    DEFAULT_CONTEXT,
    NormalizationContext,
    NormalizationError,
    decimal_separator,
)
from soa_normalize.text import normalize_whitespace

_MAX_LENGTH = 50
#: Group separators that are never decimal points: spaces and apostrophes.
_SPACE_GROUPS = re.compile(r"(?<=\d)[\s'\u2019](?=\d)")
_NUMERIC_BODY = re.compile(r"^[0-9.,]+$")


def _validate_groups(parts: list[str], separator: str) -> None:
    if any(not part.isdigit() for part in parts):
        raise NormalizationError("malformed number")
    if not 1 <= len(parts[0]) <= 3 or any(len(part) != 3 for part in parts[1:]):
        raise NormalizationError(
            f"{separator!r} does not group digits in threes; the number is malformed"
        )


def _split_number(text: str, context: NormalizationContext) -> tuple[str, str]:
    """Return (integer_digits, fraction_digits) with separators resolved."""
    commas, periods = text.count(","), text.count(".")

    if commas and periods:
        decimal_sep, group_sep = (",", ".") if text.rfind(",") > text.rfind(".") else (".", ",")
        if text.count(decimal_sep) > 1:
            raise NormalizationError("more than one decimal separator")
        integer, _, fraction = text.rpartition(decimal_sep)
        if not fraction or group_sep in fraction:
            raise NormalizationError("malformed number")
        _validate_groups(integer.split(group_sep), group_sep)
        return integer.replace(group_sep, ""), fraction

    if commas or periods:
        separator = "," if commas else "."
        parts = text.split(separator)
        if len(parts) > 2:
            _validate_groups(parts, separator)  # repeated separator: grouping only
            return "".join(parts), ""
        before, after = parts
        if not after:
            raise NormalizationError("number ends in a separator")
        if not before:
            return "0", after  # ".5" style: decimal, unambiguous
        if not before.isdigit() or not after.isdigit():
            raise NormalizationError("malformed number")
        # "1,234": valid as grouping AND as a 3-digit fraction — two-way.
        two_way = len(after) == 3 and 1 <= len(before) <= 3 and before != "0" and before[0] != "0"
        if two_way:
            convention = decimal_separator(context)
            if convention is None:
                raise NormalizationError(
                    f"{separator!r} is ambiguous here without a locale "
                    "(thousands grouping or decimal separator)"
                )
            if convention == separator:
                return before, after
            _validate_groups(parts, separator)
            return before + after, ""
        return before, after  # only the decimal reading is well-formed

    if not text.isdigit():
        raise NormalizationError("malformed number")
    return text, ""


def normalize_decimal(raw: str, context: NormalizationContext = DEFAULT_CONTEXT) -> str:
    text = normalize_whitespace(raw)
    if not text:
        raise NormalizationError("number is empty")
    if len(text) > _MAX_LENGTH:
        raise NormalizationError("number is implausibly long")

    negative = False
    if text.startswith("(") and text.endswith(")"):  # accounting negative
        negative, text = True, text[1:-1].strip()
    if text[:1] in "+-\u2212":  # includes the unicode minus sign
        negative, text = negative or text[0] in "-\u2212", text[1:].strip()
    text = _SPACE_GROUPS.sub("", text.replace(" ", ""))
    if not text or not _NUMERIC_BODY.match(text):
        raise NormalizationError("number contains unexpected characters")

    integer, fraction = _split_number(text, context)
    canonical = (integer.lstrip("0") or "0") + (f".{fraction}" if fraction else "")
    try:
        value = Decimal(canonical)
    except InvalidOperation:  # pragma: no cover - shapes above preclude this
        raise NormalizationError("malformed number") from None
    return f"-{canonical}" if negative and value != 0 else canonical


class Money(TypedDict):
    amount: str
    currency: str | None


#: Symbols with exactly one ISO 4217 meaning.
_UNAMBIGUOUS_SYMBOLS = {
    "€": "EUR",
    "£": "GBP",
    "₹": "INR",
    "₩": "KRW",
    "₺": "TRY",
    "₴": "UAH",
    "₪": "ILS",
    "฿": "THB",
    "₫": "VND",
    "zł": "PLN",
    "us$": "USD",
    "u$s": "USD",
    "c$": "CAD",
    "a$": "AUD",
    "nz$": "NZD",
    "hk$": "HKD",
    "s$": "SGD",
    "r$": "BRL",
}

#: Symbols several currencies share: resolved ONLY via context.currency.
_AMBIGUOUS_SYMBOLS = frozenset({"$", "¥", "kr", "kr.", "₨", "rs", "rs."})

_ISO_CODES = frozenset(
    {
        "usd", "eur", "gbp", "jpy", "cny", "chf", "cad", "aud", "nzd", "sek",
        "nok", "dkk", "pln", "czk", "huf", "ron", "bgn", "try", "rub", "uah",
        "inr", "idr", "myr", "php", "sgd", "thb", "vnd", "krw", "hkd", "twd",
        "mxn", "brl", "ars", "clp", "cop", "pen", "zar", "ils", "aed", "sar",
        "qar", "kwd", "egp", "ngn", "kes", "ghs", "mad", "isk", "rsd",
    }
)  # fmt: skip

_CURRENCY_NAMES = {
    "euro": "EUR",
    "euros": "EUR",
    "pound": "GBP",
    "pounds": "GBP",
    "pound sterling": "GBP",
    "pounds sterling": "GBP",
    "yen": "JPY",
    "us dollar": "USD",
    "us dollars": "USD",
    "u.s. dollar": "USD",
    "u.s. dollars": "USD",
    "swiss franc": "CHF",
    "swiss francs": "CHF",
}

_MONEY_SHAPE = re.compile(
    r"^(?P<pre>[^\d(+\-\u2212]{1,12})??\s*(?P<num>[(+\-\u2212]?[\d.,\s'\u2019]+\)?)\s*"
    r"(?P<post>[^\d)]{1,12})?$"
)


def _classify_currency_token(token: str, context: NormalizationContext) -> str | None:
    """Resolve one currency marker; None means 'ambiguous and no context'."""
    folded = token.strip().casefold()
    if folded in _UNAMBIGUOUS_SYMBOLS:
        return _UNAMBIGUOUS_SYMBOLS[folded]
    if folded in _AMBIGUOUS_SYMBOLS:
        return context.currency  # explicit context or honestly unknown
    if folded.rstrip(".") in _ISO_CODES:
        return folded.rstrip(".").upper()
    if folded in _CURRENCY_NAMES:
        return _CURRENCY_NAMES[folded]
    raise NormalizationError(f"{token[:12]!r} is not a recognized currency marker")


def normalize_money(raw: str, context: NormalizationContext = DEFAULT_CONTEXT) -> Money:
    text = normalize_whitespace(raw)
    if not text:
        raise NormalizationError("amount is empty")
    match = _MONEY_SHAPE.match(text)
    if match is None:
        raise NormalizationError("unrecognized amount format")
    pre, number, post = match.group("pre"), match.group("num"), match.group("post")

    currency: str | None = None
    marked = False
    for token in (pre, post):
        if token is None or not token.strip():
            continue
        if marked:
            raise NormalizationError("amount carries two currency markers")
        currency, marked = _classify_currency_token(token, context), True
    if not marked:
        currency = context.currency

    return Money(amount=normalize_decimal(number, context), currency=currency)


def normalize_currency_code(raw: str, context: NormalizationContext = DEFAULT_CONTEXT) -> str:
    text = normalize_whitespace(raw)
    if not text:
        raise NormalizationError("currency is empty")
    resolved = _classify_currency_token(text, context)
    if resolved is None:
        raise NormalizationError(f"{text[:12]!r} is ambiguous and no currency context was provided")
    return resolved
