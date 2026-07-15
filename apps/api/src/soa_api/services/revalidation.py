"""Post-correction revalidation (REV-009).

After a reviewer corrects a field, the run's validation picture changes:
this service rebuilds the effective values (latest correction per field,
else the extraction's canonical value), re-runs the baseline rules, and
re-decides the route. Corrected values are human-verified — they carry
full confidence and count as evidenced for policy purposes.

Field validation statuses are refreshed in place (extraction VALUES stay
immutable; the status column is the run's living verdict), and the fresh
decision is returned to the caller so the workspace can show exactly
what the correction changed. Uses the canonical sales-order maps from
soa_rules.baseline — the same ones the worker pipeline runs — until
per-stream config resolution lands.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from soa_api.services.stream_config import stream_config_by_id
from soa_db.corrections import FieldCorrection, FieldCorrectionRepository, latest_corrections
from soa_db.documents import Document
from soa_db.duplicate_policy import DuplicatePolicy, get_duplicate_policy
from soa_db.extracted_fields import ExtractedField, ExtractedFieldRepository, ValidationStatus
from soa_db.repository import OrganizationContext
from soa_normalize import NormalizationContext, NormalizationError, normalize
from soa_rules import EvaluationInput, FieldSignal, decide_route, evaluate_rule_set
from soa_rules.baseline import (
    CANONICAL_CRITICALITY,
    CANONICAL_ENUM_VALUES,
    baseline_sales_order_rules,
    canonical_normalizer_for,
)

#: The canonical pipeline's normalization context (matches the worker).
CANONICAL_CONTEXT = NormalizationContext(locale="en-US", currency="USD")


def normalize_correction(field_key: str, raw: str | None) -> tuple[Any | None, str | None]:
    """Canonicalize a corrected raw value: (normalized, safe_error)."""
    if raw is None or raw.strip() == "":
        return None, None
    kind = canonical_normalizer_for(field_key)
    if kind is None:
        return None, f"field {field_key!r} is not part of the canonical schema"
    try:
        return (
            normalize(
                kind,
                raw,
                context=CANONICAL_CONTEXT,
                enum_values=CANONICAL_ENUM_VALUES.get(field_key),
            ),
            None,
        )
    except NormalizationError as error:
        return None, str(error)


def _effective(
    field: ExtractedField, correction: FieldCorrection | None
) -> tuple[Any | None, bool]:
    """(value for rule evaluation, was it corrected by a human)."""
    if correction is not None:
        value = (
            correction.corrected_normalized_value
            if correction.corrected_normalized_value is not None
            else correction.corrected_raw_value
        )
        return value, True
    if field.normalized_value is not None:
        return field.normalized_value, False
    return field.raw_value, False


async def revalidate_run(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document: Document,
    run_id: Any,
) -> dict[str, Any]:
    fields = await ExtractedFieldRepository(session, context).list_for_run(run_id)
    corrections = latest_corrections(
        await FieldCorrectionRepository(session, context).list_for_run(run_id)
    )

    header: dict[str, Any] = {}
    tables: dict[str, dict[int, dict[str, Any]]] = {}
    signals: list[FieldSignal] = []
    for field in fields:
        correction = corrections.get((field.field_key, field.row_index))
        value, corrected = _effective(field, correction)
        if field.row_index is None:
            header[field.field_key] = value
        else:
            table = field.field_key.partition(".")[0]
            tables.setdefault(table, {}).setdefault(field.row_index, {})[field.field_key] = value
        signals.append(
            FieldSignal(
                field_key=field.field_key,
                criticality=CANONICAL_CRITICALITY.get(field.field_key, "standard"),
                present=value is not None,
                # A human-corrected value IS the verified value: full
                # confidence, evidenced by the reviewer's own eyes.
                confidence=1.0 if corrected else field.confidence,
                has_evidence=True if corrected else bool(field.evidence_json),
                row_index=field.row_index,
                top_candidate_confidence=(
                    None
                    if corrected
                    else max((c.confidence for c in field.candidate_readings()), default=None)
                ),
            )
        )
    # Correction-only cells: rows the reviewer ADDED (REV-008) have no
    # extraction rows at all — their corrections are the cells.
    covered = {(field.field_key, field.row_index) for field in fields}
    for (key, row), correction in corrections.items():
        if (key, row) in covered or row is None:
            continue
        value = (
            correction.corrected_normalized_value
            if correction.corrected_normalized_value is not None
            else correction.corrected_raw_value
        )
        table = key.partition(".")[0]
        tables.setdefault(table, {}).setdefault(row, {})[key] = value
        signals.append(
            FieldSignal(
                field_key=key,
                criticality=CANONICAL_CRITICALITY.get(key, "standard"),
                present=value is not None,
                confidence=1.0,
                has_evidence=True,
                row_index=row,
            )
        )

    # A row whose every cell was cleared no longer exists for the rules.
    for table, rows in list(tables.items()):
        tables[table] = {
            row: cells
            for row, cells in rows.items()
            if any(value is not None for value in cells.values())
        }

    # ING-006 gate, mirroring the worker pipeline: an 'allow' duplicate
    # policy is processing-transparent, so duplicates.business_hook must
    # not see the flag and re-route the corrected run to review. The
    # policy comes from the stream's ACTIVE published config. Reconcile
    # this gate with the rule itself when per-stream rules land.
    stream_config = await stream_config_by_id(session, context, document.stream_id)
    if (
        document.duplicate_of is not None
        and get_duplicate_policy(stream_config) is not DuplicatePolicy.ALLOW
    ):
        header["meta.duplicate_of"] = str(document.duplicate_of)

    data = EvaluationInput(
        header=header,
        tables={
            table: [cells for _, cells in sorted(rows.items())] for table, rows in tables.items()
        },
    )
    evaluation = evaluate_rule_set(baseline_sales_order_rules(), data)
    decision = decide_route(signals, evaluation)

    flagged = {
        (reason.field_key, reason.row_index)
        for reason in decision.reasons
        if reason.field_key is not None
    }
    for field in fields:
        correction = corrections.get((field.field_key, field.row_index))
        value, _corrected = _effective(field, correction)
        if (field.field_key, field.row_index) in flagged:
            field.validation_status = ValidationStatus.REVIEW.value
        elif value is not None:
            field.validation_status = ValidationStatus.PASSED.value

    return {"evaluation": evaluation.summary(), "decision": decision.to_json()}
