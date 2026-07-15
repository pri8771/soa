"""Extracted-field and evidence models (PRC-007).

One row per (run, field, row): header fields have ``row_index=None``,
table cells carry their 0-based row — both anchor evidence the same way.
Values are kept in three honest layers that never overwrite each other:

- ``raw_value`` — verbatim text as extracted (None = the provider did
  not find it; never a placeholder);
- ``normalized_value`` — the canonical typed value PRC-008 derives, as
  portable JSON, with ``normalization_error`` holding the safe reason
  when derivation failed;
- ``candidates`` — alternative READINGS of the same evidence region the
  provider also considered; promoting one swaps values, it does not
  invent new coordinates.

Evidence certainty is explicit — ``region`` spans carry a real polygon
in the PRC-005 coordinate system (top-left origin, pixels of the stored
raster), ``page`` spans deliberately carry none: page-level evidence is
stated as page-level, coordinates are never fabricated. An absent value
carries no evidence at all.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import Float, Index, String, Text, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID


class EvidenceCertainty(StrEnum):
    #: The provider located the value: the polygon is real coordinates.
    REGION = "region"
    #: The provider only knows the page. No polygon exists — and none is
    #: fabricated.
    PAGE = "page"


@dataclass(frozen=True)
class Evidence:
    page_number: int
    certainty: EvidenceCertainty
    polygon: tuple[tuple[float, float], ...] | None = None
    quote: str | None = None

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page numbering is 1-based")
        if self.certainty is EvidenceCertainty.REGION:
            if self.polygon is None or len(self.polygon) < 3:
                raise ValueError("region evidence requires a polygon with 3+ vertices")
        elif self.polygon is not None:
            raise ValueError("page-level evidence must not carry a polygon")

    def to_json(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "certainty": self.certainty.value,
            "polygon": [[x, y] for x, y in self.polygon] if self.polygon is not None else None,
            "quote": self.quote,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "Evidence":
        polygon = raw.get("polygon")
        return cls(
            page_number=int(raw["page_number"]),
            certainty=EvidenceCertainty(str(raw["certainty"])),
            polygon=(
                tuple((float(x), float(y)) for x, y in polygon) if polygon is not None else None
            ),
            quote=raw.get("quote"),
        )


@dataclass(frozen=True)
class Candidate:
    """An alternative reading of the SAME evidence region."""

    raw_value: str
    confidence: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")

    def to_json(self) -> dict[str, Any]:
        return {"raw_value": self.raw_value, "confidence": self.confidence}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "Candidate":
        return cls(raw_value=str(raw["raw_value"]), confidence=float(raw["confidence"]))


class ValidationStatus(StrEnum):
    PENDING = "pending"  # PRC-009 has not evaluated this field yet
    PASSED = "passed"
    REVIEW = "review"
    BLOCKED = "blocked"


class ExtractedField(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    __tablename__ = "extracted_fields"

    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    #: Flat schema path: header fields are bare keys, cells are
    #: ``table.column`` (matches SchemaDefinition.field_types).
    field_key: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 0-based table row; None for header fields.
    row_index: Mapped[int | None] = mapped_column(nullable=True)
    raw_value: Mapped[str | None] = mapped_column(Text(), nullable=True)
    normalized_value: Mapped[Any | None] = mapped_column(PORTABLE_JSON, nullable=True)
    #: Safe, display-ready reason normalization failed (never raw input).
    normalization_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    confidence: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    instruction_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Serialized Evidence list (see evidence_spans()).
    evidence_json: Mapped[list[Any]] = mapped_column(
        "evidence", PORTABLE_JSON, nullable=False, default=list
    )
    #: Serialized Candidate list (see candidate_readings()).
    candidates_json: Mapped[list[Any]] = mapped_column(
        "candidates", PORTABLE_JSON, nullable=False, default=list
    )
    validation_status: Mapped[str] = mapped_column(
        String(50), nullable=False, default=ValidationStatus.PENDING.value
    )

    __table_args__ = (
        # One row per (run, field, row); header rows (NULL row_index)
        # collapse onto -1 so they are unique too.
        Index(
            "uq_extracted_fields_run_key_row",
            "run_id",
            "field_key",
            text("coalesce(row_index, -1)"),
            unique=True,
        ),
    )

    def evidence_spans(self) -> tuple[Evidence, ...]:
        return tuple(Evidence.from_json(entry) for entry in self.evidence_json)

    def candidate_readings(self) -> tuple[Candidate, ...]:
        return tuple(Candidate.from_json(entry) for entry in self.candidates_json)


class ExtractedFieldRepository(ScopedRepository[ExtractedField]):
    model = ExtractedField

    async def list_for_run(self, run_id: uuid.UUID) -> list[ExtractedField]:
        stmt = (
            self._scoped_select()
            .where(ExtractedField.run_id == run_id)
            .order_by(
                ExtractedField.field_key,
                text("coalesce(row_index, -1)"),
            )
        )
        return list((await self._session.execute(stmt)).scalars().all())


async def create_extracted_field(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document_id: uuid.UUID,
    run_id: uuid.UUID,
    field_key: str,
    raw_value: str | None,
    confidence: float,
    provider: str,
    provider_model: str | None = None,
    instruction_reference: str | None = None,
    config_fingerprint: str | None = None,
    execution_fingerprint: str | None = None,
    row_index: int | None = None,
    evidence: Sequence[Evidence] = (),
    candidates: Sequence[Candidate] = (),
    normalized_value: Any | None = None,
    page_dimensions: Mapping[int, tuple[int, int]] | None = None,
) -> ExtractedField:
    """Persist one extracted field.

    ``page_dimensions`` (page_number -> (width_px, height_px)) is the
    run's real page set; when provided, every evidence span must name a
    known page and every polygon vertex must be inside it — fabricated
    coordinates are refused at the door.
    """
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be within [0, 1]")
    if row_index is not None and row_index < 0:
        raise ValueError("row_index is 0-based; negative rows are reserved")
    if raw_value is None and evidence:
        raise ValueError("an absent value cannot carry evidence")
    if page_dimensions is not None:
        for span in evidence:
            dimensions = page_dimensions.get(span.page_number)
            if dimensions is None:
                raise ValueError(
                    f"evidence names page {span.page_number}, which the run does not have"
                )
            width, height = dimensions
            for x, y in span.polygon or ():
                if not (0 <= x <= width and 0 <= y <= height):
                    raise ValueError(
                        f"evidence polygon leaves page {span.page_number} bounds at ({x}, {y})"
                    )
    record = ExtractedFieldRepository(session, context).add(
        ExtractedField(
            document_id=document_id,
            run_id=run_id,
            field_key=field_key,
            row_index=row_index,
            raw_value=raw_value,
            normalized_value=normalized_value,
            confidence=confidence,
            provider=provider,
            provider_model=provider_model,
            instruction_reference=instruction_reference,
            config_fingerprint=config_fingerprint,
            execution_fingerprint=execution_fingerprint,
            evidence_json=[span.to_json() for span in evidence],
            candidates_json=[candidate.to_json() for candidate in candidates],
        )
    )
    await session.flush()
    return record


def promote_candidate(field: ExtractedField, candidate_index: int) -> None:
    """Adopt an alternative reading as the value. Candidates are readings
    of the SAME evidence region, so evidence stays put; the previous
    value becomes a candidate (nothing the provider said is lost)."""
    candidates = list(field.candidate_readings())
    try:
        chosen = candidates.pop(candidate_index)
    except IndexError:
        raise ValueError(f"no candidate at index {candidate_index}") from None
    if field.raw_value is not None:
        candidates.append(Candidate(raw_value=field.raw_value, confidence=field.confidence))
    field.raw_value = chosen.raw_value
    field.confidence = chosen.confidence
    # A different reading invalidates any earlier normalization/validation.
    field.normalized_value = None
    field.normalization_error = None
    field.validation_status = ValidationStatus.PENDING.value
    field.candidates_json = [candidate.to_json() for candidate in candidates]


def confidence_summary(fields: Sequence[ExtractedField]) -> dict[str, Any]:
    """Run-level rollup for StageRun.output_summary and routing (PRC-011
    consumes richer inputs; this is the honest headline)."""
    extracted = [f for f in fields if f.raw_value is not None]
    confidences = [f.confidence for f in extracted]
    return {
        "total_fields": len(fields),
        "extracted": len(extracted),
        "absent": len(fields) - len(extracted),
        "min_confidence": round(min(confidences), 6) if confidences else None,
        "mean_confidence": (round(sum(confidences) / len(confidences), 6) if confidences else None),
    }


def validation_summary(fields: Sequence[ExtractedField]) -> dict[str, int]:
    """Counts by validation status; every status appears, even at zero."""
    counts = {status.value: 0 for status in ValidationStatus}
    for field in fields:
        counts[field.validation_status] += 1
    return counts
