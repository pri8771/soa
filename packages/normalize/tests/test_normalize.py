"""Canonical normalization tests (PRC-008): table-driven matrices for
every normalizer plus property-based determinism/idempotence checks."""

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from soa_normalize import (
    NormalizationContext,
    NormalizationError,
    normalize,
    normalize_boolean,
    normalize_currency_code,
    normalize_date_iso,
    normalize_decimal,
    normalize_enum,
    normalize_identifier,
    normalize_money,
    normalize_uom,
    normalize_whitespace,
)

EN_US = NormalizationContext(locale="en-US")
DE_DE = NormalizationContext(locale="de-DE")
FR = NormalizationContext(locale="fr")
USD_CTX = NormalizationContext(currency="USD")


# -- dates ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-03-14", "2026-03-14"),
        ("2026/3/4", "2026-03-04"),
        ("2026.03.14", "2026-03-14"),
        ("14 Mar 2026", "2026-03-14"),
        ("14-Mar-2026", "2026-03-14"),
        ("1st March 2026", "2026-03-01"),
        ("March 14, 2026", "2026-03-14"),
        ("Sept 2, 2026", "2026-09-02"),
        ("  14   march   2026 ", "2026-03-14"),
        ("14/3/2026", "2026-03-14"),  # 14 > 12: day-first is forced
        ("3/14/2026", "2026-03-14"),  # 14 > 12: month-first is forced
        ("4/4/2026", "2026-04-04"),  # same either way
        ("31.12.2026", "2026-12-31"),
    ],
)
def test_dates_that_need_no_context(raw: str, expected: str) -> None:
    assert normalize_date_iso(raw) == expected


def test_ambiguous_dates_require_a_locale_and_are_never_guessed() -> None:
    with pytest.raises(NormalizationError, match="ambiguous without a locale"):
        normalize_date_iso("03/04/2026")
    assert normalize_date_iso("03/04/2026", EN_US) == "2026-03-04"  # month first
    assert normalize_date_iso("03/04/2026", DE_DE) == "2026-04-03"  # day first
    assert normalize_date_iso("03/04/2026", FR) == "2026-04-03"


@pytest.mark.parametrize(
    "raw",
    [
        "03/04/26",  # two-digit year
        "2026-02-30",  # not a real date
        "31/04/2026",  # April has 30 days
        "14 März 2026",  # non-English month: not guessed
        "next tuesday",
        "20260314",
        "",
    ],
)
def test_bad_dates_are_errors(raw: str) -> None:
    with pytest.raises(NormalizationError):
        normalize_date_iso(raw, EN_US)


@given(st.dates(min_value=date(1900, 1, 1), max_value=date(2199, 12, 31)))
def test_any_real_date_roundtrips_from_unambiguous_renderings(value: date) -> None:
    iso = value.isoformat()
    assert normalize_date_iso(iso) == iso
    assert normalize_date_iso(value.strftime("%d %B %Y")) == iso
    assert normalize_date_iso(value.strftime("%B %d, %Y")) == iso


# -- decimals ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234", "1234"),
        ("007", "7"),
        ("0", "0"),
        ("1,234.50", "1234.50"),
        ("1.234,50", "1234.50"),
        ("1,234,567.89", "1234567.89"),
        ("1 234 567,89", "1234567.89"),
        ("1'234.50", "1234.50"),
        ("12,34", "12.34"),  # 2 digits after: decimal, unambiguous
        ("1234,5", "1234.5"),
        ("1234,567", "1234.567"),  # grouping reading is malformed, so decimal
        ("0,123", "0.123"),  # a leading 0 group is not grouping
        (".5", "0.5"),
        ("-12.5", "-12.5"),
        ("\u221212.5", "-12.5"),  # unicode minus
        ("+12.5", "12.5"),
        ("(1,234.50)", "-1234.50"),  # accounting negative
        ("-0.00", "0.00"),  # negative zero is zero
        ("1,234,567", "1234567"),  # repeated separator: grouping only
    ],
)
def test_decimal_matrix(raw: str, expected: str) -> None:
    assert normalize_decimal(raw) == expected


def test_two_way_decimals_require_a_locale() -> None:
    with pytest.raises(NormalizationError, match="ambiguous"):
        normalize_decimal("1,234")
    with pytest.raises(NormalizationError, match="ambiguous"):
        normalize_decimal("1.234")
    assert normalize_decimal("1,234", EN_US) == "1234"  # comma groups in en
    assert normalize_decimal("1,234", DE_DE) == "1.234"  # comma is decimal in de
    assert normalize_decimal("1.234", EN_US) == "1.234"
    assert normalize_decimal("1.234", DE_DE) == "1234"


@pytest.mark.parametrize(
    "raw",
    ["", "abc", "12..5", "1,23,45", "1.234.5", "12.", "1,2345.6", "12-34"],
)
def test_malformed_numbers_are_errors(raw: str) -> None:
    with pytest.raises(NormalizationError):
        normalize_decimal(raw, EN_US)


@given(
    st.decimals(
        min_value=Decimal("-999999999"),
        max_value=Decimal("999999999"),
        allow_nan=False,
        allow_infinity=False,
        places=2,
    )
)
def test_grouped_renderings_roundtrip_exactly(value: Decimal) -> None:
    en = f"{value:,.2f}"  # e.g. -1,234,567.89
    de = en.translate(str.maketrans({",": ".", ".": ","}))
    assert Decimal(normalize_decimal(en, EN_US)) == value
    assert Decimal(normalize_decimal(de, DE_DE)) == value
    # Deterministic and idempotent: canonical output re-normalizes to itself.
    canonical = normalize_decimal(en, EN_US)
    assert normalize_decimal(canonical) == canonical


# -- money and currency --------------------------------------------------------


def test_money_matrix() -> None:
    assert normalize_money("€1.234,50") == {"amount": "1234.50", "currency": "EUR"}
    assert normalize_money("1,234.50 USD") == {"amount": "1234.50", "currency": "USD"}
    assert normalize_money("£99") == {"amount": "99", "currency": "GBP"}
    assert normalize_money("US$ 45.00") == {"amount": "45.00", "currency": "USD"}
    # Ambiguous symbol: context resolves it, absence stays honest.
    assert normalize_money("$1,234.50", USD_CTX) == {"amount": "1234.50", "currency": "USD"}
    assert normalize_money("$1,234.50") == {"amount": "1234.50", "currency": None}
    # Bare amounts take the context currency or none at all.
    assert normalize_money("1234.50", USD_CTX) == {"amount": "1234.50", "currency": "USD"}
    assert normalize_money("1234.50") == {"amount": "1234.50", "currency": None}
    assert normalize_money("(45.00) EUR") == {"amount": "-45.00", "currency": "EUR"}


def test_money_rejects_nonsense() -> None:
    with pytest.raises(NormalizationError, match="not a recognized currency"):
        normalize_money("1234.50 QQQ")
    with pytest.raises(NormalizationError, match="two currency markers"):
        normalize_money("€ 12.50 USD")
    with pytest.raises(NormalizationError):
        normalize_money("")


@pytest.mark.parametrize(
    ("raw", "context", "expected"),
    [
        ("usd", None, "USD"),
        ("EUR", None, "EUR"),
        ("€", None, "EUR"),
        ("euros", None, "EUR"),
        ("pounds sterling", None, "GBP"),
        ("$", USD_CTX, "USD"),
    ],
)
def test_currency_code_matrix(
    raw: str, context: NormalizationContext | None, expected: str
) -> None:
    assert normalize_currency_code(raw, context or NormalizationContext()) == expected


def test_currency_codes_are_never_guessed() -> None:
    with pytest.raises(NormalizationError, match="ambiguous"):
        normalize_currency_code("$")
    with pytest.raises(NormalizationError, match="not a recognized currency"):
        normalize_currency_code("QQQ")


# -- units ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ea", "EA"),
        ("Each", "EA"),
        ("PCS", "EA"),
        ("pcs.", "EA"),
        ("Stk", "EA"),
        ("kg", "KG"),
        ("Kilograms", "KG"),
        ("lbs", "LB"),
        ("metres", "M"),
        ("m²", "M2"),
        ("sq m", "M2"),
        ("cbm", "M3"),
        ("Litre", "L"),
        ("boxes", "BX"),
        ("Case", "CS"),
        ("pallets", "PLT"),
        ("hours", "HR"),
        ("dozen", "DZ"),
    ],
)
def test_uom_matrix(raw: str, expected: str) -> None:
    assert normalize_uom(raw) == expected


def test_unknown_units_are_never_guessed() -> None:
    with pytest.raises(NormalizationError, match="never guessed"):
        normalize_uom("blorps")
    with pytest.raises(NormalizationError):
        normalize_uom("")


# -- text, boolean, enum -------------------------------------------------------


def test_whitespace_and_identifier() -> None:
    assert normalize_whitespace("  Acme\u00a0 Industrial \n Supply ") == "Acme Industrial Supply"
    assert normalize_whitespace(" \t ") == ""
    assert normalize_identifier(" po​-100042 ") == "PO-100042"
    full_width = "\uff50\uff4f\uff0d\uff11\uff10\uff12"  # fullwidth po-102
    assert normalize_identifier(full_width) == "PO-102"  # NFKC folds full-width
    with pytest.raises(NormalizationError, match="empty"):
        normalize_identifier("  ")


@given(st.text(max_size=200))
def test_whitespace_normalization_is_idempotent_and_trimmed(raw: str) -> None:
    once = normalize_whitespace(raw)
    assert normalize_whitespace(once) == once
    assert once == once.strip()
    assert "  " not in once


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Yes", True),
        ("TRUE", True),
        ("x", True),
        ("✓", True),
        ("1", True),
        ("No", False),
        ("false", False),
        ("0", False),
        ("—", False),
    ],
)
def test_boolean_matrix(raw: str, expected: bool) -> None:
    assert normalize_boolean(raw) is expected


def test_unknown_boolean_markers_are_errors() -> None:
    with pytest.raises(NormalizationError):
        normalize_boolean("maybe")


def test_enum_matches_schema_spelling_without_fuzz() -> None:
    allowed = ["Net 30", "Net 60", "Due on Receipt"]
    assert normalize_enum("net  30", allowed) == "Net 30"
    assert normalize_enum(" DUE ON RECEIPT ", allowed) == "Due on Receipt"
    with pytest.raises(NormalizationError, match="does not match"):
        normalize_enum("Net 45", allowed)
    with pytest.raises(NormalizationError, match="no allowed values"):
        normalize_enum("x", [])


# -- registry ------------------------------------------------------------------


def test_registry_dispatch_and_unknown_names() -> None:
    assert normalize("date_iso", "14 Mar 2026") == "2026-03-14"
    assert normalize("decimal", "1.234,50", context=DE_DE) == "1234.50"
    assert normalize("uom", "pcs") == "EA"
    assert normalize("enum", "net 30", enum_values=["Net 30"]) == "Net 30"
    with pytest.raises(NormalizationError, match="unknown normalizer"):
        normalize("shout", "x")
    with pytest.raises(NormalizationError, match="requires the allowed values"):
        normalize("enum", "x")


def test_raw_values_are_never_mutated() -> None:
    raw = "  1,234.50  "
    normalize_decimal(raw, EN_US)
    assert raw == "  1,234.50  "  # strings are immutable, but the promise is explicit
