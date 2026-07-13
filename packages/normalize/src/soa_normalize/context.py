"""Normalization context and error (PRC-008).

The context carries the ONLY facts normalizers may use beyond the raw
string itself. Locale conventions are applied when — and only when — the
caller states them (they come from stream configuration, a trading
partner profile, or document classification, never from a default).
"""

from dataclasses import dataclass


class NormalizationError(ValueError):
    """The raw value could not be canonicalized. The message explains
    what was invalid or ambiguous and is safe to display; the raw value
    itself stays wherever the caller keeps it (PRC-007 raw_value)."""


@dataclass(frozen=True)
class NormalizationContext:
    #: BCP-47-style tag, e.g. "en-US", "de-DE", "fr". Drives date field
    #: order and decimal-separator conventions for otherwise-ambiguous
    #: input. None = no locale is known; ambiguity becomes an error.
    locale: str | None = None
    #: ISO 4217 code applied to bare amounts and ambiguous symbols
    #: ("$", "kr"). None = currency stays unknown rather than guessed.
    currency: str | None = None


DEFAULT_CONTEXT = NormalizationContext()

#: Languages whose decimal separator is the comma ("1.234,56").
_COMMA_DECIMAL_LANGUAGES = frozenset(
    {
        "de",
        "fr",
        "es",
        "it",
        "pt",
        "nl",
        "da",
        "sv",
        "nb",
        "nn",
        "no",
        "fi",
        "pl",
        "cs",
        "sk",
        "hu",
        "ro",
        "bg",
        "el",
        "tr",
        "ru",
        "uk",
        "id",
        "vi",
    }
)

#: Locales that write dates month-first (everything else numeric is
#: day-first; year-first is detected from the string shape itself).
_MONTH_FIRST_LOCALES = frozenset({"en-us", "en-ph"})


def _language(locale: str) -> str:
    return locale.split("-", 1)[0].strip().lower()


def decimal_separator(context: NormalizationContext) -> str | None:
    """ "," or "." when the locale states a convention; None otherwise."""
    if context.locale is None:
        return None
    return "," if _language(context.locale) in _COMMA_DECIMAL_LANGUAGES else "."


def date_order(context: NormalizationContext) -> str | None:
    """ "MDY" or "DMY" when the locale states one; None otherwise."""
    if context.locale is None:
        return None
    return "MDY" if context.locale.strip().lower() in _MONTH_FIRST_LOCALES else "DMY"
