"""Customer and ship-to validation rules (CAT-011).

Business validation over the order's PARTIES once the extracted values
have been resolved to catalog records (CAT-009 matching / CAT-010
review picks). Pure and deterministic: records in, findings out —
every finding carries a stable code, a written message, and the field
it belongs to, shaped like the PRC-011 route reasons so the existing
review flow can surface them unchanged.

Checks:

- **ship-to belongs to customer** — a ship-to record must name its
  owning customer (the ``belongs_to`` attribute, compared as normalized
  identifiers). A contradiction is an ERROR; an absent link is a
  WARNING (``unverifiable``), never a silent pass and never a false
  block on catalogs that don't carry the relationship.
- **sold-to / bill-to / ship-to validity** — a record used in a role
  must allow that role (the ``roles`` attribute); records without a
  ``roles`` attribute allow every role, with a note. A party out of its
  effective window on the order date is an ERROR.
- **country / postcode consistency** — the document's address must
  agree with the picked ship-to record's ``country``/``postcode``
  attributes (ERROR on contradiction), and the postcode must fit the
  country's format where the format is known (WARNING; unknown
  countries are skipped with a note, not guessed).

Attribute contract for ``customers`` catalog records (all optional —
absence degrades to warnings/notes, contradiction to errors):
``roles`` (list of "sold_to" / "bill_to" / "ship_to"), ``belongs_to``
(owning customer's source id, ship-to records), ``country`` (ISO-3166
alpha-2 or a common English name), ``postcode``.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from soa_db.catalog_matching import normalize_identifier, normalize_text

PARTY_ROLES = ("sold_to", "bill_to", "ship_to")

#: Common English country names -> ISO-3166 alpha-2, so "Germany" on the
#: document agrees with "DE" in the catalog. Deliberately small; unknown
#: names compare as normalized text.
_COUNTRY_ALIASES: dict[str, str] = {
    "germany": "DE",
    "deutschland": "DE",
    "austria": "AT",
    "switzerland": "CH",
    "france": "FR",
    "netherlands": "NL",
    "belgium": "BE",
    "spain": "ES",
    "italy": "IT",
    "poland": "PL",
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "united kingdom": "GB",
    "great britain": "GB",
    "england": "GB",
}

#: Postcode shapes for countries where the format is unambiguous.
#: Matched against the postcode uppercased with internal spaces removed.
_POSTCODE_FORMATS: dict[str, re.Pattern[str]] = {
    "DE": re.compile(r"^\d{5}$"),
    "AT": re.compile(r"^\d{4}$"),
    "CH": re.compile(r"^\d{4}$"),
    "FR": re.compile(r"^\d{5}$"),
    "BE": re.compile(r"^\d{4}$"),
    "ES": re.compile(r"^\d{5}$"),
    "IT": re.compile(r"^\d{5}$"),
    "PL": re.compile(r"^\d{2}-?\d{3}$"),
    "NL": re.compile(r"^\d{4}[A-Z]{2}$"),
    "US": re.compile(r"^\d{5}(-\d{4})?$"),
    "GB": re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}$"),
}


def normalize_country(value: str) -> str:
    """ISO-3166 alpha-2 where recognisable; otherwise the trimmed
    uppercase input (compared as-is, never guessed)."""
    trimmed = value.strip()
    if len(trimmed) == 2 and trimmed.isalpha():
        return trimmed.upper()
    return _COUNTRY_ALIASES.get(normalize_text(trimmed), trimmed.upper())


def _normalize_postcode(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().upper())


@dataclass(frozen=True)
class PartyFacts:
    """The validation-relevant facts of one catalog party record."""

    source_id: str
    display_name: str
    attributes: Mapping[str, Any] = field(default_factory=dict)
    effective_from: date | None = None
    effective_to: date | None = None

    def effective_on(self, as_of: date) -> bool:
        if self.effective_from and as_of < self.effective_from:
            return False
        if self.effective_to and as_of > self.effective_to:
            return False
        return True


def party_of(record: object) -> PartyFacts:
    """Adapt a CatalogRecord row (or anything record-shaped)."""
    return PartyFacts(
        source_id=record.source_id,  # type: ignore[attr-defined]
        display_name=record.display_name,  # type: ignore[attr-defined]
        attributes=dict(record.attributes),  # type: ignore[attr-defined]
        effective_from=record.effective_from,  # type: ignore[attr-defined]
        effective_to=record.effective_to,  # type: ignore[attr-defined]
    )


@dataclass(frozen=True)
class OrderParties:
    """The order's parties, resolved to catalog records. ``None`` means
    the document names no such party (or nothing matched) — validation
    states what it therefore cannot check instead of failing."""

    sold_to: PartyFacts | None = None
    bill_to: PartyFacts | None = None
    ship_to: PartyFacts | None = None


@dataclass(frozen=True)
class DocumentAddress:
    """The ship-to address as EXTRACTED from the document, for
    consistency checks against the picked catalog record."""

    country: str | None = None
    postcode: str | None = None


@dataclass(frozen=True)
class ValidationFinding:
    code: str
    severity: str  # error | warning
    message: str
    field_key: str | None = None
    #: The line the finding belongs to (CAT-012); None = header-level.
    row_index: int | None = None

    def to_reason(self) -> dict[str, Any]:
        """The PRC-011 route-reason shape, so findings surface through
        the existing review flow unchanged."""
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "field_key": self.field_key,
            "row_index": self.row_index,
            "rule_key": f"catalog.{self.code}",
        }


@dataclass(frozen=True)
class PartyValidation:
    findings: tuple[ValidationFinding, ...]
    #: What was checked, assumed, or skipped — the honest record.
    notes: tuple[str, ...]

    @property
    def errors(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "warning")


def _roles_of(party: PartyFacts) -> tuple[list[str] | None, str | None]:
    """The record's declared roles, or (None, note) when it declares
    none and therefore allows every role."""
    raw = party.attributes.get("roles")
    if raw is None:
        return None, (
            f"record {party.source_id!r} declares no roles attribute — "
            "it is allowed in every party role"
        )
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return [item.strip().lower() for item in raw], None
    return [], None  # malformed roles allow nothing: fail closed on bad data


def validate_parties(
    parties: OrderParties,
    *,
    document_address: DocumentAddress | None = None,
    as_of: date | None = None,
) -> PartyValidation:
    """Run every CAT-011 check that the available data supports."""
    findings: list[ValidationFinding] = []
    notes: list[str] = []

    field_keys = {
        "sold_to": "customer_name",
        "bill_to": "bill_to",
        "ship_to": "ship_to",
    }
    for role in PARTY_ROLES:
        party: PartyFacts | None = getattr(parties, role)
        if party is None:
            notes.append(f"no {role} party resolved — its checks were skipped")
            continue

        roles, note = _roles_of(party)
        if note is not None:
            notes.append(note)
        elif roles is not None and role not in roles:
            declared = ", ".join(sorted(roles)) if roles else "none"
            findings.append(
                ValidationFinding(
                    code="party_role_invalid",
                    severity="error",
                    message=(
                        f"{party.display_name!r} ({party.source_id}) is not valid as a "
                        f"{role.replace('_', '-')} party — its declared roles: {declared}"
                    ),
                    field_key=field_keys[role],
                )
            )

        if as_of is not None and not party.effective_on(as_of):
            findings.append(
                ValidationFinding(
                    code="party_out_of_effect",
                    severity="error",
                    message=(
                        f"{party.display_name!r} ({party.source_id}) is out of effect "
                        f"on {as_of.isoformat()}"
                    ),
                    field_key=field_keys[role],
                )
            )

    if parties.ship_to is not None:
        _check_ship_to_link(parties, findings, notes)
        if document_address is not None:
            _check_address_consistency(parties.ship_to, document_address, findings, notes)

    return PartyValidation(findings=tuple(findings), notes=tuple(notes))


def _check_ship_to_link(
    parties: OrderParties, findings: list[ValidationFinding], notes: list[str]
) -> None:
    ship_to = parties.ship_to
    assert ship_to is not None
    belongs_to = ship_to.attributes.get("belongs_to")
    if not isinstance(belongs_to, str) or not belongs_to.strip():
        findings.append(
            ValidationFinding(
                code="ship_to_link_unverifiable",
                severity="warning",
                message=(
                    f"ship-to {ship_to.display_name!r} ({ship_to.source_id}) names no "
                    "owning customer (belongs_to) — the customer link cannot be verified"
                ),
                field_key="ship_to",
            )
        )
        return
    if parties.sold_to is None:
        notes.append(
            "the ship-to names its owning customer but no sold-to party resolved — "
            "the link was not checked"
        )
        return
    if normalize_identifier(belongs_to) != normalize_identifier(parties.sold_to.source_id):
        findings.append(
            ValidationFinding(
                code="ship_to_not_linked_to_customer",
                severity="error",
                message=(
                    f"ship-to {ship_to.display_name!r} ({ship_to.source_id}) belongs to "
                    f"customer {belongs_to!r}, not to the order's sold-to "
                    f"{parties.sold_to.source_id!r}"
                ),
                field_key="ship_to",
            )
        )
    else:
        notes.append(
            f"ship-to {ship_to.source_id!r} belongs to sold-to "
            f"{parties.sold_to.source_id!r} — verified"
        )


def _check_address_consistency(
    ship_to: PartyFacts,
    address: DocumentAddress,
    findings: list[ValidationFinding],
    notes: list[str],
) -> None:
    catalog_country = ship_to.attributes.get("country")
    catalog_postcode = ship_to.attributes.get("postcode")

    effective_country: str | None = None
    if address.country and isinstance(catalog_country, str) and catalog_country.strip():
        document_norm = normalize_country(address.country)
        catalog_norm = normalize_country(catalog_country)
        effective_country = catalog_norm
        if document_norm != catalog_norm:
            findings.append(
                ValidationFinding(
                    code="address_country_mismatch",
                    severity="error",
                    message=(
                        f"the document ships to {address.country!r} but the picked "
                        f"ship-to record {ship_to.source_id!r} is in {catalog_country!r}"
                    ),
                    field_key="ship_to",
                )
            )
    elif address.country:
        effective_country = normalize_country(address.country)
        notes.append(
            f"ship-to record {ship_to.source_id!r} carries no country — the document's "
            "country was not cross-checked"
        )
    elif isinstance(catalog_country, str) and catalog_country.strip():
        effective_country = normalize_country(catalog_country)

    if address.postcode and isinstance(catalog_postcode, str) and catalog_postcode.strip():
        if _normalize_postcode(address.postcode) != _normalize_postcode(catalog_postcode):
            findings.append(
                ValidationFinding(
                    code="address_postcode_mismatch",
                    severity="error",
                    message=(
                        f"the document's postcode {address.postcode!r} disagrees with the "
                        f"picked ship-to record's {catalog_postcode!r}"
                    ),
                    field_key="ship_to",
                )
            )

    postcode = address.postcode or (catalog_postcode if isinstance(catalog_postcode, str) else None)
    if postcode and effective_country:
        pattern = _POSTCODE_FORMATS.get(effective_country)
        if pattern is None:
            notes.append(
                f"no postcode format is known for {effective_country!r} — "
                "the format was not checked"
            )
        elif not pattern.match(_normalize_postcode(postcode)):
            findings.append(
                ValidationFinding(
                    code="postcode_format_invalid",
                    severity="warning",
                    message=(f"postcode {postcode!r} does not fit the {effective_country} format"),
                    field_key="ship_to",
                )
            )


__all__ = [
    "PARTY_ROLES",
    "DocumentAddress",
    "OrderParties",
    "PartyFacts",
    "PartyValidation",
    "ValidationFinding",
    "normalize_country",
    "party_of",
    "validate_parties",
]
