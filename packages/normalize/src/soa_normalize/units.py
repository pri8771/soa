"""Unit-of-measure normalization (PRC-008).

Aliases map onto a fixed canonical code set (UN/ECE-flavoured, the codes
ERP mappings key on). Matching is case-insensitive over trimmed tokens
with a trailing period tolerated ("pcs." → PCS token). Anything not in
the table is an error — units are never guessed, because "M" meaning
metres when the vendor meant thousands corrupts orders silently.
"""

from soa_normalize.context import DEFAULT_CONTEXT, NormalizationContext, NormalizationError
from soa_normalize.text import normalize_whitespace

_CANONICAL_ALIASES: dict[str, tuple[str, ...]] = {
    "EA": ("ea", "each", "eaches", "pc", "pcs", "piece", "pieces", "unit", "units", "stk"),
    "DZ": ("dz", "doz", "dozen", "dozens"),
    "PR": ("pr", "pair", "pairs"),
    "SET": ("set", "sets"),
    "KG": ("kg", "kgs", "kilo", "kilos", "kilogram", "kilograms"),
    "G": ("g", "gram", "grams"),
    "MG": ("mg", "milligram", "milligrams"),
    "T": ("t", "ton", "tons", "tonne", "tonnes", "mt", "metric ton", "metric tons"),
    "LB": ("lb", "lbs", "pound", "pounds"),
    "OZ": ("oz", "ounce", "ounces"),
    "M": ("m", "meter", "meters", "metre", "metres"),
    "CM": ("cm", "centimeter", "centimeters", "centimetre", "centimetres"),
    "MM": ("mm", "millimeter", "millimeters", "millimetre", "millimetres"),
    "KM": ("km", "kilometer", "kilometers", "kilometre", "kilometres"),
    "FT": ("ft", "foot", "feet"),
    "IN": ("in", "inch", "inches"),
    "YD": ("yd", "yard", "yards"),
    "M2": (
        "m2",
        "m²",
        "sqm",
        "sq m",
        "square meter",
        "square meters",
        "square metre",
        "square metres",
    ),
    "M3": ("m3", "m³", "cbm", "cubic meter", "cubic meters", "cubic metre", "cubic metres"),
    "L": ("l", "ltr", "liter", "liters", "litre", "litres"),
    "ML": ("ml", "milliliter", "milliliters", "millilitre", "millilitres"),
    "GAL": ("gal", "gallon", "gallons"),
    "BX": ("bx", "box", "boxes"),
    "CS": ("cs", "case", "cases"),
    "CT": ("ct", "carton", "cartons"),
    "PK": ("pk", "pack", "packs", "package", "packages"),
    "PLT": ("plt", "pal", "pallet", "pallets"),
    "RL": ("rl", "roll", "rolls"),
    "BG": ("bg", "bag", "bags"),
    "DR": ("dr", "drum", "drums"),
    "BTL": ("btl", "bottle", "bottles"),
    "CAN": ("can", "cans"),
    "HR": ("hr", "hrs", "hour", "hours"),
    "DAY": ("day", "days"),
    "WK": ("wk", "week", "weeks"),
    "MO": ("mo", "month", "months"),
}

_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _code, _aliases in _CANONICAL_ALIASES.items():
    for _alias in (_code.casefold(), *_aliases):
        _claimed = _ALIAS_TO_CANONICAL.setdefault(_alias, _code)
        if _claimed != _code:
            raise RuntimeError(f"UOM alias {_alias!r} is claimed by {_claimed} and {_code}")


def normalize_uom(raw: str, context: NormalizationContext = DEFAULT_CONTEXT) -> str:
    token = normalize_whitespace(raw).casefold().rstrip(".")
    if not token:
        raise NormalizationError("unit of measure is empty")
    canonical = _ALIAS_TO_CANONICAL.get(token)
    if canonical is None:
        raise NormalizationError(
            f"{token[:20]!r} is not a recognized unit of measure; units are never guessed"
        )
    return canonical
