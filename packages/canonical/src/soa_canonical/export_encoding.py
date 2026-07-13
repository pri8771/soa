"""Deterministic export encodings (EXP-006).

Pure functions from a mapped payload to the exact bytes an export
artifact contains. Same input, same bytes — no timestamps or randomness
inside the encoders (the caller supplies ``exported_at`` in the
metadata, so even that is an input). Decimals and dates stay the exact
strings the mapping produced; nothing is ever parsed into floats.

Every export carries its identifying metadata: the canonical schema
version, the mapping version that produced it, and the business key a
receiver deduplicates on — in the JSON envelope, and as leading columns
on every CSV row.
"""

import csv
import io
import json
from dataclasses import dataclass
from typing import Any


class ExportEncodingError(Exception):
    pass


@dataclass(frozen=True)
class ExportMetadata:
    """What every export names about itself."""

    schema_version: str
    mapping_version_number: int
    integration_slug: str
    business_key: str
    document_id: str
    run_id: str
    #: Supplied by the caller so encoders stay pure.
    exported_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mapping_version_number": self.mapping_version_number,
            "integration_slug": self.integration_slug,
            "business_key": self.business_key,
            "document_id": self.document_id,
            "run_id": self.run_id,
            "exported_at": self.exported_at,
        }


def encode_json_export(payload: dict[str, Any], metadata: ExportMetadata) -> bytes:
    """The JSON artifact: a metadata envelope around the mapped payload.
    Keys sorted, two-space indent, UTF-8, trailing newline — byte-stable."""
    document = {"metadata": metadata.to_json(), "payload": payload}
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _defuse_formula(text: str) -> str:
    """Neutralize spreadsheet formula injection: a cell starting with
    ``=``, ``@``, tab, or CR — or ``+``/``-`` that is not simply a
    number — gets a leading apostrophe so Excel/Sheets render it as
    text instead of executing it. Legitimate negative amounts stay
    untouched."""
    if not text:
        return text
    first = text[0]
    if first in "=@\t\r":
        return "'" + text
    if first in "+-":
        try:
            float(text)
            return text
        except ValueError:
            return "'" + text
    return text


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _defuse_formula(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    # Nested structures become compact deterministic JSON.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


#: Metadata travels ON EVERY ROW so a split or sampled CSV still names
#: its schema and mapping version.
_METADATA_COLUMNS = ("export_business_key", "export_schema_version", "export_mapping_version")


def encode_csv_export(
    payload: dict[str, Any], metadata: ExportMetadata, *, lines_key: str
) -> bytes:
    """The CSV artifact: one row per line item. Columns are the metadata
    columns, then the payload's scalar header fields (definition order),
    then the line columns in first-appearance order across rows — all
    deterministic for a given payload."""
    rows = payload.get(lines_key)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ExportEncodingError(
            f"CSV export needs a list of line objects at {lines_key!r}; the mapped payload has none"
        )
    header_fields = [key for key, value in payload.items() if key != lines_key]
    line_columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in line_columns:
                line_columns.append(key)

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([*_METADATA_COLUMNS, *header_fields, *line_columns])
    metadata_cells = [
        metadata.business_key,
        metadata.schema_version,
        str(metadata.mapping_version_number),
    ]
    header_cells = [_cell(payload[key]) for key in header_fields]
    if not rows:
        writer.writerow([*metadata_cells, *header_cells])
    for row in rows:
        writer.writerow(
            [*metadata_cells, *header_cells, *[_cell(row.get(key)) for key in line_columns]]
        )
    return buffer.getvalue().encode("utf-8")
