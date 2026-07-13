"""Deterministic mock extraction provider (PRC-006).

The mock recognises documents by the SHA-256 of their ORIGINAL bytes and
returns pre-registered values for them — no network, no model, no
randomness. Everything else about it is honest:

- an unrecognised document yields every requested field ABSENT plus a
  warning, never plausible-looking fabrications;
- evidence polygons are computed deterministically from the field key
  and placed inside the request's real page bounds (PRC-005 coordinate
  system), so downstream evidence rendering can be exercised for real;
- configured failure modes (`MockMode.ERROR_RETRYABLE` /
  `ERROR_TERMINAL`) raise :class:`ExtractionProviderError` exactly the
  way a real adapter would, and `LOW_CONFIDENCE` scales every
  confidence down so PRC-011 routing paths can be tested end to end.
"""

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from soa_worker.extraction.provider import (
    EvidenceSpan,
    ExtractedField,
    ExtractionProviderError,
    ExtractionRequest,
    ExtractionResult,
    FieldCandidate,
    FieldSpec,
    PageInput,
)

PROVIDER_NAME = "mock"
MODEL_NAME = "mock-v1"


class MockMode(StrEnum):
    NORMAL = "normal"
    #: Every confidence is multiplied by ``low_confidence_factor`` so the
    #: result lands under any sane review gate.
    LOW_CONFIDENCE = "low_confidence"
    ERROR_RETRYABLE = "error_retryable"
    ERROR_TERMINAL = "error_terminal"


@dataclass(frozen=True)
class FixtureField:
    """One known value a registered fixture document contains."""

    field_key: str
    value: str
    confidence: float = 0.98
    page_number: int = 1
    quote: str | None = None
    row_index: int | None = None
    candidates: tuple[FieldCandidate, ...] = ()


def _evidence_box(
    field_key: str, row_index: int | None, page: PageInput
) -> tuple[tuple[float, float], ...]:
    """A deterministic rectangle inside the page for this field/row —
    derived from a hash so distinct fields land in distinct places, and
    identical inputs always land in the same place."""
    digest = hashlib.sha256(f"{field_key}:{row_index}".encode()).digest()
    x0 = round(digest[0] / 255 * 0.60 * page.width_px, 1)
    y0 = round(digest[1] / 255 * 0.90 * page.height_px, 1)
    x1 = min(round(x0 + 0.30 * page.width_px, 1), float(page.width_px))
    y1 = min(round(y0 + 0.04 * page.height_px, 1), float(page.height_px))
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def _scaled(confidence: float, factor: float) -> float:
    return round(min(max(confidence * factor, 0.0), 1.0), 6)


class MockExtractionProvider:
    """See module docstring. ``fixtures`` maps document SHA-256 (hex) to
    the values that document is known to contain."""

    def __init__(
        self,
        fixtures: Mapping[str, Sequence[FixtureField]] | None = None,
        *,
        mode: MockMode = MockMode.NORMAL,
        low_confidence_factor: float = 0.35,
    ) -> None:
        self._fixtures = dict(DEFAULT_FIXTURES if fixtures is None else fixtures)
        self._mode = mode
        self._factor = low_confidence_factor

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        if self._mode is MockMode.ERROR_RETRYABLE:
            raise ExtractionProviderError(
                "mock provider configured to fail (retryable)", retryable=True
            )
        if self._mode is MockMode.ERROR_TERMINAL:
            raise ExtractionProviderError(
                "mock provider configured to fail (terminal)", retryable=False
            )

        pages = {page.page_number: page for page in request.pages}
        warnings: list[str] = []
        known = self._fixtures.get(request.document_sha256)
        if known is None:
            warnings.append("document is not a registered mock fixture; every field is absent")
            known = ()
        by_key: dict[str, list[FixtureField]] = {}
        for fixture in known:
            by_key.setdefault(fixture.field_key, []).append(fixture)

        factor = self._factor if self._mode is MockMode.LOW_CONFIDENCE else 1.0
        fields: list[ExtractedField] = []
        for spec in request.fields:
            if spec.field_type == "table":
                continue  # the table's columns carry the data, not the container
            fixtures_for_key = by_key.get(spec.key)
            if not fixtures_for_key:
                fields.append(ExtractedField(field_key=spec.key, raw_value=None, confidence=0.0))
                continue
            for fixture in fixtures_for_key:
                page = pages.get(fixture.page_number)
                if page is None:
                    warnings.append(
                        f"fixture evidence for {spec.key!r} names page "
                        f"{fixture.page_number}, which the request does not contain"
                    )
                    fields.append(
                        ExtractedField(field_key=spec.key, raw_value=None, confidence=0.0)
                    )
                    continue
                fields.append(
                    ExtractedField(
                        field_key=spec.key,
                        raw_value=fixture.value,
                        confidence=_scaled(fixture.confidence, factor),
                        row_index=fixture.row_index,
                        evidence=(
                            EvidenceSpan(
                                page_number=fixture.page_number,
                                polygon=_evidence_box(spec.key, fixture.row_index, page),
                                quote=fixture.quote if fixture.quote is not None else fixture.value,
                            ),
                        ),
                        candidates=tuple(
                            FieldCandidate(c.raw_value, _scaled(c.confidence, factor))
                            for c in fixture.candidates
                        ),
                    )
                )
        return ExtractionResult(
            provider=PROVIDER_NAME,
            model=MODEL_NAME,
            fields=tuple(fields),
            cost_cents=0,
            warnings=tuple(warnings),
        )


# -- Synthetic fixtures --------------------------------------------------------

#: A structurally valid one-page PDF whose comment line makes its hash
#: unique to this fixture. It can flow through the PRC-004 renderer, and
#: the mock recognises its original bytes.
SYNTHETIC_SALES_ORDER = b"""%PDF-1.4
% synthetic sales-order fixture SO-FIXTURE-001 (PRC-006)
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj
trailer << /Root 1 0 R >>
"""

SYNTHETIC_SALES_ORDER_SHA256 = hashlib.sha256(SYNTHETIC_SALES_ORDER).hexdigest()

#: The values the synthetic sales order "contains" — raw strings exactly
#: as a document would show them (normalization is PRC-008's job).
SYNTHETIC_SALES_ORDER_FIELDS: tuple[FixtureField, ...] = (
    FixtureField(
        "po_number",
        "PO-100042",
        confidence=0.99,
        candidates=(FieldCandidate("PO-1000A2", 0.12),),
    ),
    FixtureField("order_date", "03/14/2026", quote="Order Date: 03/14/2026"),
    FixtureField("requested_delivery_date", "04/01/2026"),
    FixtureField("customer_name", "Acme Industrial Supply"),
    FixtureField("currency", "USD", confidence=0.97),
    FixtureField("total_amount", "1,234.50", confidence=0.96, quote="TOTAL: $1,234.50"),
    FixtureField("lines.sku", "WID-100", row_index=0),
    FixtureField("lines.description", "Widget, 10mm galvanized", row_index=0),
    FixtureField("lines.quantity", "10", row_index=0),
    FixtureField("lines.unit_price", "45.00", row_index=0),
    FixtureField("lines.sku", "GAD-205", row_index=1),
    FixtureField("lines.description", "Gadget, large", row_index=1),
    FixtureField("lines.quantity", "3", row_index=1),
    FixtureField("lines.unit_price", "261.50", row_index=1, confidence=0.88),
)

DEFAULT_FIXTURES: Mapping[str, tuple[FixtureField, ...]] = {
    SYNTHETIC_SALES_ORDER_SHA256: SYNTHETIC_SALES_ORDER_FIELDS,
}

#: Stable identity for the fixture document in tests and PRC-012.
SYNTHETIC_SALES_ORDER_DOCUMENT_ID = uuid.uuid5(
    uuid.NAMESPACE_URL, "soa:fixture:synthetic-sales-order"
)

SYNTHETIC_SALES_ORDER_FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("po_number", "text"),
    FieldSpec("order_date", "date"),
    FieldSpec("requested_delivery_date", "date"),
    FieldSpec("customer_name", "text"),
    FieldSpec("currency", "enum", enum_values=("USD", "EUR", "GBP")),
    FieldSpec("total_amount", "money"),
    #: A field the fixture deliberately does NOT contain — exercises the
    #: honest-absence path.
    FieldSpec("delivery_terms", "text"),
    FieldSpec("lines", "table"),
    FieldSpec("lines.sku", "text"),
    FieldSpec("lines.description", "text"),
    FieldSpec("lines.quantity", "number"),
    FieldSpec("lines.unit_price", "money"),
)


def synthetic_sales_order_request() -> ExtractionRequest:
    """The canonical request over the synthetic fixture: one US-letter
    page rendered at 200 dpi, asking for every schema field above."""
    return ExtractionRequest(
        document_id=SYNTHETIC_SALES_ORDER_DOCUMENT_ID,
        document_sha256=SYNTHETIC_SALES_ORDER_SHA256,
        content_type="application/pdf",
        pages=(PageInput(page_number=1, width_px=1700, height_px=2200),),
        fields=SYNTHETIC_SALES_ORDER_FIELD_SPECS,
    )
