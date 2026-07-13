"""Catalog CSV import pipeline (CAT-002).

Turns an uploaded CSV into a validated catalog-version draft in three
explicit, separately auditable steps — parse, preview, apply — with
activation deliberately left to CAT-001's ``activate_catalog_version``:
an import can NEVER become live as a side effect.

Parsing is defensive about real-world files and hostile cells:

- **encoding** — UTF-8 (with or without BOM) first, then Latin-1 with a
  recorded warning; NUL bytes mean the file is not text and are refused;
- **dialect** — the delimiter is sniffed from the header line among
  comma/semicolon/tab;
- **row validation** — missing identifiers or names, malformed ISO
  dates, backwards effective ranges, over-long cells, and duplicate
  source IDs are reported as per-row issues with their row numbers,
  never silently dropped;
- **formula injection** — cells that would execute in a spreadsheet
  (leading ``=``, ``@``, or non-numeric ``+``/``-``) are kept VERBATIM
  as text (matching needs the true value) but flagged with a warning;
  the CSV *export* encoder defuses such values with a leading
  apostrophe, so round-tripping through the platform cannot arm them;
- **bounded** — row and cell budgets are enforced with explicit errors.

``apply_import`` refuses a file with row issues unless the caller
passes ``allow_partial=True`` explicitly — a partial import is a human
decision, not a default.
"""

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.catalogs import (
    Catalog,
    CatalogRecordRepository,
    CatalogVersion,
    add_catalog_record,
    create_catalog_version,
)
from soa_db.repository import OrganizationContext

MAX_ROWS = 100_000
MAX_CELL_CHARS = 2_000
_DELIMITERS = (",", ";", "\t")


class CatalogImportError(ValueError):
    pass


@dataclass(frozen=True)
class ColumnMapping:
    """Which CSV columns feed which record fields. ``aliases`` names a
    column of ``|``-separated alternative spellings; ``attributes`` maps
    attribute names to columns."""

    source_id: str
    display_name: str
    aliases: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)

    def required_columns(self) -> tuple[str, ...]:
        columns = [self.source_id, self.display_name]
        for optional in (self.aliases, self.effective_from, self.effective_to):
            if optional:
                columns.append(optional)
        columns.extend(self.attributes.values())
        return tuple(columns)


@dataclass(frozen=True)
class RowIssue:
    row_number: int  # 1-based data row (header not counted)
    message: str


@dataclass(frozen=True)
class ParsedRecord:
    source_id: str
    display_name: str
    aliases: tuple[str, ...]
    attributes: dict[str, str]
    effective_from: date | None
    effective_to: date | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ParseResult:
    records: tuple[ParsedRecord, ...]
    issues: tuple[RowIssue, ...]
    warnings: tuple[str, ...]
    encoding: str
    delimiter: str

    @property
    def ok(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class ImportPreview:
    added: tuple[str, ...]
    changed: tuple[str, ...]
    deactivated: tuple[str, ...]
    unchanged: int


def _decode(data: bytes) -> tuple[str, str, list[str]]:
    if b"\x00" in data:
        raise CatalogImportError("the file contains NUL bytes — not a text CSV")
    warnings: list[str] = []
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig", warnings
    try:
        return data.decode("utf-8"), "utf-8", warnings
    except UnicodeDecodeError:
        warnings.append("the file is not UTF-8; decoded as Latin-1 — verify special characters")
        return data.decode("latin-1"), "latin-1", warnings


def _sniff_delimiter(header_line: str) -> str:
    counts = {delimiter: header_line.count(delimiter) for delimiter in _DELIMITERS}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def _formula_like(value: str) -> bool:
    if not value:
        return False
    if value[0] in "=@":
        return True
    if value[0] in "+-":
        try:
            float(value)
            return False
        except ValueError:
            return True
    return False


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def validate_import_rows(
    rows: "Iterable[dict[str, str]]",
    header: list[str],
    mapping: ColumnMapping,
    *,
    warnings: list[str],
    encoding: str,
    delimiter: str,
) -> ParseResult:
    """The shared validation core: CSV (CAT-002) and XLSX (CAT-003) both
    feed their rows through this, so every format gets identical issue
    reporting, formula flagging, and bounds."""
    missing = [column for column in mapping.required_columns() if column not in header]
    if missing:
        raise CatalogImportError(
            f"the mapping names columns the file does not have: {', '.join(sorted(missing))} "
            f"(file columns: {', '.join(header) or '(none)'})"
        )

    records: list[ParsedRecord] = []
    issues: list[RowIssue] = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=1):
        if row_number > MAX_ROWS:
            raise CatalogImportError(f"the file exceeds the {MAX_ROWS} row budget")
        cells = {key: (value or "").strip() for key, value in row.items() if key is not None}
        if any(len(value) > MAX_CELL_CHARS for value in cells.values()):
            issues.append(RowIssue(row_number, f"a cell exceeds {MAX_CELL_CHARS} characters"))
            continue

        source_id = cells.get(mapping.source_id, "")
        display_name = cells.get(mapping.display_name, "")
        if not source_id:
            issues.append(RowIssue(row_number, f"missing {mapping.source_id!r}"))
            continue
        if not display_name:
            issues.append(RowIssue(row_number, f"missing {mapping.display_name!r}"))
            continue
        if source_id in seen:
            issues.append(RowIssue(row_number, f"duplicate source id {source_id!r}"))
            continue

        row_warnings: list[str] = []
        for column, value in cells.items():
            if _formula_like(value):
                row_warnings.append(
                    f"{column!r} looks like a spreadsheet formula; it is stored as "
                    "text and defused on any CSV export"
                )

        effective: dict[str, date | None] = {"from": None, "to": None}
        bad_date = False
        for bound, date_column in (
            ("from", mapping.effective_from),
            ("to", mapping.effective_to),
        ):
            if date_column and cells.get(date_column):
                try:
                    effective[bound] = _parse_date(cells[date_column])
                except ValueError:
                    issues.append(
                        RowIssue(
                            row_number,
                            f"{date_column!r} is not an ISO date (YYYY-MM-DD): "
                            f"{cells[date_column]!r}",
                        )
                    )
                    bad_date = True
        if bad_date:
            continue
        if effective["from"] and effective["to"] and effective["to"] < effective["from"]:
            issues.append(RowIssue(row_number, "effective_to precedes effective_from"))
            continue

        aliases: tuple[str, ...] = ()
        if mapping.aliases and cells.get(mapping.aliases):
            aliases = tuple(
                part.strip() for part in cells[mapping.aliases].split("|") if part.strip()
            )
        attributes = {name: cells.get(column, "") for name, column in mapping.attributes.items()}

        seen.add(source_id)
        records.append(
            ParsedRecord(
                source_id=source_id,
                display_name=display_name,
                aliases=aliases,
                attributes=attributes,
                effective_from=effective["from"],
                effective_to=effective["to"],
                warnings=tuple(row_warnings),
            )
        )

    return ParseResult(
        records=tuple(records),
        issues=tuple(issues),
        warnings=tuple(warnings),
        encoding=encoding,
        delimiter=delimiter,
    )


def parse_catalog_csv(data: bytes, mapping: ColumnMapping) -> ParseResult:
    """Parse and validate a CSV upload. Issues are per-row facts, not
    exceptions — the caller shows them all at once."""
    text, encoding, warnings = _decode(data)
    stripped = text.lstrip("\r\n")
    if not stripped.strip():
        raise CatalogImportError("the file is empty")
    delimiter = _sniff_delimiter(stripped.splitlines()[0])
    reader = csv.DictReader(io.StringIO(stripped), delimiter=delimiter)
    header = list(reader.fieldnames or [])
    return validate_import_rows(
        reader, header, mapping, warnings=warnings, encoding=encoding, delimiter=delimiter
    )


async def preview_import(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    catalog: Catalog,
    parsed: ParseResult,
) -> ImportPreview:
    """What activating this file would change, against the ACTIVE
    version (everything is 'added' when none exists)."""
    current: dict[str, tuple[str, tuple[str, ...], tuple[tuple[str, str], ...], str, str]] = {}
    if catalog.active_version_id is not None:
        for row in await CatalogRecordRepository(session, context).list_for_version(
            catalog.active_version_id
        ):
            current[row.source_id] = (
                row.display_name,
                tuple(row.aliases),
                tuple(sorted(row.attributes.items())),
                str(row.effective_from or ""),
                str(row.effective_to or ""),
            )
    added: list[str] = []
    changed: list[str] = []
    unchanged = 0
    incoming_ids = set()
    for record in parsed.records:
        incoming_ids.add(record.source_id)
        fingerprint = (
            record.display_name,
            record.aliases,
            tuple(sorted(record.attributes.items())),
            str(record.effective_from or ""),
            str(record.effective_to or ""),
        )
        if record.source_id not in current:
            added.append(record.source_id)
        elif current[record.source_id] != fingerprint:
            changed.append(record.source_id)
        else:
            unchanged += 1
    deactivated = sorted(set(current) - incoming_ids)
    return ImportPreview(
        added=tuple(sorted(added)),
        changed=tuple(sorted(changed)),
        deactivated=tuple(deactivated),
        unchanged=unchanged,
    )


async def apply_import(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    catalog: Catalog,
    parsed: ParseResult,
    change_summary: str | None = None,
    actor_id: str,
    allow_partial: bool = False,
) -> CatalogVersion:
    """Create a catalog-version DRAFT from the parsed file. Activation
    is a separate explicit step — a partial or unreviewed import can
    never go live accidentally."""
    if parsed.issues and not allow_partial:
        raise CatalogImportError(
            f"{len(parsed.issues)} rows failed validation (first: row "
            f"{parsed.issues[0].row_number} — {parsed.issues[0].message}); fix the file "
            "or pass allow_partial explicitly to import only the valid rows"
        )
    if not parsed.records:
        raise CatalogImportError("no valid rows to import")
    draft = await create_catalog_version(
        session,
        context,
        catalog=catalog,
        change_summary=change_summary
        or f"CSV import: {len(parsed.records)} rows ({len(parsed.issues)} skipped)",
        actor_id=actor_id,
    )
    for record in parsed.records:
        await add_catalog_record(
            session,
            context,
            version=draft,
            source_id=record.source_id,
            display_name=record.display_name,
            attributes=record.attributes,
            aliases=list(record.aliases),
            effective_from=record.effective_from,
            effective_to=record.effective_to,
        )
    return draft


def import_identity(data: bytes) -> str:
    """Stable identity of an upload (dedupe/audit)."""
    import hashlib

    return hashlib.sha256(data).hexdigest()


__all__ = [
    "MAX_CELL_CHARS",
    "MAX_ROWS",
    "CatalogImportError",
    "ColumnMapping",
    "ImportPreview",
    "ParseResult",
    "ParsedRecord",
    "RowIssue",
    "apply_import",
    "import_identity",
    "parse_catalog_csv",
    "preview_import",
    "validate_import_rows",
]
