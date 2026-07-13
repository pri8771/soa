"""Quality analytics model (ANA-002).

Tenant-scoped quality metrics over the review trail. The central
honesty rule: production corrections are a PROXY for accuracy, not
accuracy — a field nobody corrected is not necessarily right, only
uncorrected. Every metric is therefore named for what it measures
(``correction_rate``, not "accuracy"), every rate carries its sample
size, unmeasurable rates are ``None`` (never a flattering 0), and the
snapshot states plainly what would take gold data (AIO-015/016) to
claim:

- **field correction rates** — per field key, corrected reviewed runs
  over reviewed runs where the field appeared;
- **line correction rates** — the same for table cells (row-indexed);
- **STP** — settled documents that never needed a human;
- **false auto-approval** — NOT measurable from production data (an
  auto-approved document has no reviewer to catch the error); reported
  as unavailable with the pointer to the AIO-016 evaluation runner;
- **calibration cohorts** — reviewed fields bucketed by extraction
  confidence with each bucket's correction rate, so over- or
  under-confidence is visible.

Windows are half-open ``[since, until)`` UTC, anchored on the review
task's ``completed_at`` (STP anchors on ``received_at``). Fetches are
capped, and the snapshot says when a cap truncated.
"""

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.analytics import MetricDefinition
from soa_db.corrections import FieldCorrection
from soa_db.documents import Document, DocumentState
from soa_db.extracted_fields import ExtractedField
from soa_db.gold_datasets import GoldDocument
from soa_db.repository import OrganizationContext
from soa_db.review_tasks import ReviewTask, ReviewTaskState

#: Rows fetched per query before capping (noted on the snapshot).
QUALITY_SAMPLE_CAP = 100_000

#: Confidence buckets for calibration, as [low, high) except the last.
CALIBRATION_BUCKETS = (
    (0.0, 0.5),
    (0.5, 0.8),
    (0.8, 0.9),
    (0.9, 0.95),
    (0.95, 1.01),  # inclusive top
)

#: Document states that count as settled for STP.
_SETTLED_STATES = (
    DocumentState.APPROVED.value,
    DocumentState.EXPORTING.value,
    DocumentState.COMPLETED.value,
)

QUALITY_DEFINITIONS: dict[str, MetricDefinition] = {
    definition.key: definition
    for definition in (
        MetricDefinition(
            key="quality.field_correction_rate",
            description=(
                "How often reviewers corrected a field — a PROXY for accuracy, "
                "not accuracy; uncorrected is not verified-correct."
            ),
            numerator="reviewed runs in which the field was corrected at least once",
            denominator="reviewed runs in which the field appeared (extracted or corrected)",
        ),
        MetricDefinition(
            key="quality.line_correction_rate",
            description="Correction rate over table cells (row-indexed fields).",
            numerator="reviewed (run, field, row) cells corrected at least once",
            denominator="reviewed cells that appeared (extracted or corrected)",
        ),
        MetricDefinition(
            key="quality.stp_rate",
            description="Straight-through processing: settled without a human.",
            numerator=(
                "documents received in the window, now settled "
                "(approved/exporting/completed), with NO review task"
            ),
            denominator="documents received in the window, now settled",
        ),
        MetricDefinition(
            key="quality.false_auto_approval",
            description=(
                "Auto-approved documents that were wrong. NOT measurable from "
                "production alone — nobody reviewed them; measured against gold "
                "data by the AIO-016 evaluation runner."
            ),
            numerator="unavailable in production",
            denominator="unavailable in production",
        ),
        MetricDefinition(
            key="quality.calibration",
            description=(
                "Correction rate per extraction-confidence bucket over reviewed "
                "runs — a well-calibrated extractor is corrected less as "
                "confidence rises."
            ),
            numerator="reviewed fields in the bucket that were corrected",
            denominator="reviewed fields in the bucket",
        ),
    )
}


def _rate(numerator: int, denominator: int) -> float | None:
    """None when unmeasurable — never a flattering 0/0 = 0."""
    return round(numerator / denominator, 4) if denominator else None


async def quality_snapshot(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    since: datetime,
    until: datetime,
) -> dict[str, Any]:
    """The ANA-002 read model. See module docstring for semantics."""
    org = context.organization_id
    notes: list[str] = []

    # --- reviewed runs: tasks completed in the window ---
    task_rows = list(
        await session.execute(
            select(ReviewTask.run_id, ReviewTask.document_id)
            .where(
                ReviewTask.organization_id == org,
                ReviewTask.state == ReviewTaskState.COMPLETED.value,
                ReviewTask.completed_at.is_not(None),
                ReviewTask.completed_at >= since,
                ReviewTask.completed_at < until,
            )
            .limit(QUALITY_SAMPLE_CAP + 1)
        )
    )
    if len(task_rows) > QUALITY_SAMPLE_CAP:
        task_rows = task_rows[:QUALITY_SAMPLE_CAP]
        notes.append(f"reviewed-task sample capped at {QUALITY_SAMPLE_CAP}")
    reviewed_run_ids = {row.run_id for row in task_rows}

    fields_present: dict[tuple[uuid.UUID, str, int | None], float] = {}
    corrected_keys: set[tuple[uuid.UUID, str, int | None]] = set()
    if reviewed_run_ids:
        field_rows = list(
            await session.execute(
                select(
                    ExtractedField.run_id,
                    ExtractedField.field_key,
                    ExtractedField.row_index,
                    ExtractedField.confidence,
                )
                .where(
                    ExtractedField.organization_id == org,
                    ExtractedField.run_id.in_(reviewed_run_ids),
                )
                .limit(QUALITY_SAMPLE_CAP + 1)
            )
        )
        if len(field_rows) > QUALITY_SAMPLE_CAP:
            field_rows = field_rows[:QUALITY_SAMPLE_CAP]
            notes.append(f"extracted-field sample capped at {QUALITY_SAMPLE_CAP}")
        for row in field_rows:
            fields_present[(row.run_id, row.field_key, row.row_index)] = row.confidence

        correction_rows = list(
            await session.execute(
                select(
                    FieldCorrection.run_id,
                    FieldCorrection.field_key,
                    FieldCorrection.row_index,
                )
                .where(
                    FieldCorrection.organization_id == org,
                    FieldCorrection.run_id.in_(reviewed_run_ids),
                )
                .limit(QUALITY_SAMPLE_CAP + 1)
            )
        )
        if len(correction_rows) > QUALITY_SAMPLE_CAP:
            correction_rows = correction_rows[:QUALITY_SAMPLE_CAP]
            notes.append(f"correction sample capped at {QUALITY_SAMPLE_CAP}")
        for correction_row in correction_rows:
            key = (correction_row.run_id, correction_row.field_key, correction_row.row_index)
            corrected_keys.add(key)
            # A correction on a never-extracted cell (added row) still
            # counts as an appearance the reviewer had to author.
            fields_present.setdefault(key, 0.0)

    # --- per-field correction rates (header fields: row_index None) ---
    per_field: dict[str, dict[str, int]] = {}
    line_cells_present = 0
    line_cells_corrected = 0
    for (run_id, field_key, row_index), _confidence in fields_present.items():
        corrected = (run_id, field_key, row_index) in corrected_keys
        if row_index is None:
            bucket = per_field.setdefault(field_key, {"present_runs": 0, "corrected_runs": 0})
            bucket["present_runs"] += 1
            if corrected:
                bucket["corrected_runs"] += 1
        else:
            line_cells_present += 1
            if corrected:
                line_cells_corrected += 1
    field_rates = [
        {
            "field_key": field_key,
            "present_runs": counts["present_runs"],
            "corrected_runs": counts["corrected_runs"],
            "correction_rate": _rate(counts["corrected_runs"], counts["present_runs"]),
        }
        for field_key, counts in sorted(per_field.items())
    ]

    # --- calibration cohorts over reviewed fields ---
    cohorts = []
    for low, high in CALIBRATION_BUCKETS:
        in_bucket = [key for key, confidence in fields_present.items() if low <= confidence < high]
        corrected_count = sum(1 for key in in_bucket if key in corrected_keys)
        cohorts.append(
            {
                "confidence_range": f"[{low}, {min(high, 1.0)}{']' if high > 1 else ')'}",
                "fields_reviewed": len(in_bucket),
                "corrected": corrected_count,
                "correction_rate": _rate(corrected_count, len(in_bucket)),
            }
        )

    # --- STP over documents received in the window, settled now ---
    settled_rows = list(
        await session.execute(
            select(Document.id)
            .where(
                Document.organization_id == org,
                Document.received_at >= since,
                Document.received_at < until,
                Document.state.in_(_SETTLED_STATES),
            )
            .limit(QUALITY_SAMPLE_CAP + 1)
        )
    )
    if len(settled_rows) > QUALITY_SAMPLE_CAP:
        settled_rows = settled_rows[:QUALITY_SAMPLE_CAP]
        notes.append(f"settled-document sample capped at {QUALITY_SAMPLE_CAP}")
    settled_ids = {row.id for row in settled_rows}
    reviewed_document_ids: set[uuid.UUID] = set()
    if settled_ids:
        reviewed_document_ids = {
            row.document_id
            for row in await session.execute(
                select(ReviewTask.document_id).where(
                    ReviewTask.organization_id == org,
                    ReviewTask.document_id.in_(settled_ids),
                )
            )
        }
    straight_through = len(settled_ids - reviewed_document_ids)

    # --- ground truth availability ---
    gold_documents = (
        await session.execute(select(func.count()).where(GoldDocument.organization_id == org))
    ).scalar_one()

    return {
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "timezone": "UTC",
        },
        "reviewed": {"tasks_completed": len(task_rows), "runs": len(reviewed_run_ids)},
        "field_corrections": field_rates,
        "line_corrections": {
            "cells_present": line_cells_present,
            "cells_corrected": line_cells_corrected,
            "correction_rate": _rate(line_cells_corrected, line_cells_present),
        },
        "stp": {
            "settled_documents": len(settled_ids),
            "straight_through": straight_through,
            "stp_rate": _rate(straight_through, len(settled_ids)),
        },
        "false_auto_approval": {
            "available": False,
            "reason": (
                "auto-approved documents have no reviewer to catch errors, so "
                "production data cannot measure this; run the AIO-016 evaluation "
                "against a gold dataset (AIO-015) for a defensible number"
            ),
        },
        "calibration": {"cohorts": cohorts},
        "ground_truth": {
            "gold_documents": gold_documents,
            "note": (
                "all rates above are correction-based PROXIES; accuracy claims "
                "require evaluation against gold data"
            ),
        },
        "notes": notes,
        "definitions": {key: asdict(value) for key, value in QUALITY_DEFINITIONS.items()},
    }


__all__ = [
    "CALIBRATION_BUCKETS",
    "QUALITY_DEFINITIONS",
    "QUALITY_SAMPLE_CAP",
    "quality_snapshot",
]
