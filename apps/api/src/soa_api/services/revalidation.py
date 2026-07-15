"""Post-correction revalidation (REV-009).

After a reviewer corrects a field, the run's validation picture changes:
this service rebuilds the effective values (latest correction per field,
else the extraction's canonical value), re-runs the exact rules pinned to
that run, and re-decides the route. Corrected values are human-verified —
they carry full confidence and count as evidenced for policy purposes.

Field validation statuses are refreshed in place (extraction VALUES stay
immutable; the status column is the run's living verdict), and the fresh
decision is returned to the caller so the workspace can show exactly
what the correction changed. Schema, normalizers, rules, confidence gates,
and stream policy are read from the exact immutable version pinned to the
run, never from the stream's current active version. The canonical baseline
is retained only for legacy/demo runs that predate immutable pins.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from soa_api.domain.policies import PolicyVersionRepository
from soa_api.domain.rules import RuleSetVersionRepository
from soa_api.domain.schemas import SchemaVersionRepository
from soa_api.domain.streams import StreamVersionRepository
from soa_api.services.runtime_pins import snapshot_fingerprint
from soa_api.settings import load_settings
from soa_db.catalog_business import merge_business_validation, validate_order_business_data
from soa_db.catalog_selections import CatalogSelectionError, resolve_catalog_identities
from soa_db.catalogs import (
    CATALOG_VERSION_PINS_KEY,
    CatalogError,
    CatalogVersionPin,
    parse_catalog_version_pins,
    validate_catalog_version_pins,
)
from soa_db.corrections import FieldCorrection, FieldCorrectionRepository, latest_corrections
from soa_db.documents import Document
from soa_db.duplicate_po import DuplicateOverride
from soa_db.duplicate_policy import (
    EXACT_DUPLICATE_RULE_KEY,
    DuplicatePolicy,
    exact_duplicate_review_reason,
    get_duplicate_policy,
)
from soa_db.extracted_fields import ExtractedField, ExtractedFieldRepository, ValidationStatus
from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRunRepository
from soa_normalize import NormalizationContext, NormalizationError, normalize
from soa_rules import (
    ConfidencePolicy,
    EvaluationInput,
    FieldSignal,
    decide_route,
    evaluate_rule_set,
)
from soa_rules.baseline import (
    CANONICAL_CRITICALITY,
    CANONICAL_ENUM_VALUES,
    CANONICAL_FIELD_TYPES,
    CANONICAL_NORMALIZER_OVERRIDES,
    TYPE_DEFAULT_NORMALIZERS,
    baseline_sales_order_rules,
)

#: The canonical pipeline's normalization context (matches the worker).
CANONICAL_CONTEXT = NormalizationContext(locale="en-US", currency="USD")


class RevalidationConfigError(ValueError):
    """The run's authenticated validation contract is missing or corrupt."""


@dataclass(frozen=True)
class RevalidationConfig:
    """The exact schema, rules, normalization, and policy pinned to a run."""

    stream_config: Mapping[str, Any]
    field_types: Mapping[str, str]
    criticality: Mapping[str, str]
    normalizer_overrides: Mapping[str, str]
    enum_values: Mapping[str, tuple[str, ...]]
    rules: tuple[dict[str, Any], ...]
    confidence_policy: ConfidencePolicy
    normalization: NormalizationContext
    catalog_version_pins: tuple[CatalogVersionPin, ...] | None

    def normalizer_for(self, field_key: str) -> str | None:
        override = self.normalizer_overrides.get(field_key)
        return override or TYPE_DEFAULT_NORMALIZERS.get(self.field_types.get(field_key, ""))


def _baseline_config() -> RevalidationConfig:
    return RevalidationConfig(
        stream_config={},
        field_types=CANONICAL_FIELD_TYPES,
        criticality=CANONICAL_CRITICALITY,
        normalizer_overrides=CANONICAL_NORMALIZER_OVERRIDES,
        enum_values=CANONICAL_ENUM_VALUES,
        rules=tuple(baseline_sales_order_rules()),
        confidence_policy=ConfidencePolicy(),
        normalization=CANONICAL_CONTEXT,
        catalog_version_pins=None,
    )


def normalize_correction(
    field_key: str,
    raw: str | None,
    *,
    config: RevalidationConfig | None = None,
) -> tuple[Any | None, str | None]:
    """Canonicalize a corrected raw value: (normalized, safe_error)."""
    if raw is None or raw.strip() == "":
        return None, None
    effective = config or _baseline_config()
    kind = effective.normalizer_for(field_key)
    if kind is None:
        return None, f"field {field_key!r} has no configured normalizer"
    try:
        return (
            normalize(
                kind,
                raw,
                context=effective.normalization,
                enum_values=effective.enum_values.get(field_key),
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


def _configured_uuid(config: Mapping[str, Any], key: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(config[key]))
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise RevalidationConfigError(f"revalidation stream snapshot has no valid {key}") from error


def _schema_maps(
    definition: Mapping[str, Any],
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, tuple[str, ...]],
]:
    raw_fields = definition.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise RevalidationConfigError("revalidation pinned schema has no fields")
    field_types: dict[str, str] = {}
    criticality: dict[str, str] = {}
    normalizers: dict[str, str] = {}
    enum_values: dict[str, tuple[str, ...]] = {}

    def add(raw: object, *, prefix: str | None = None) -> None:
        if not isinstance(raw, dict):
            raise RevalidationConfigError("revalidation pinned schema contains an invalid field")
        key_value, type_value = raw.get("key"), raw.get("type")
        if not isinstance(key_value, str) or not isinstance(type_value, str):
            raise RevalidationConfigError("revalidation pinned schema field is incomplete")
        key = f"{prefix}.{key_value}" if prefix else key_value
        field_types[key] = type_value
        level = raw.get("criticality", "standard")
        if level not in ("critical", "standard", "informational"):
            raise RevalidationConfigError(
                f"revalidation schema field {key!r} has invalid criticality"
            )
        criticality[key] = str(level)
        normalizer = raw.get("normalization")
        if isinstance(normalizer, str) and normalizer:
            normalizers[key] = normalizer
        enums = raw.get("enum_values")
        if enums is not None:
            if not isinstance(enums, list) or not all(isinstance(item, str) for item in enums):
                raise RevalidationConfigError(
                    f"revalidation schema field {key!r} has invalid enum values"
                )
            enum_values[key] = tuple(enums)
        if type_value == "table":
            columns = raw.get("columns")
            if not isinstance(columns, list) or not columns:
                raise RevalidationConfigError(f"revalidation table field {key!r} has no columns")
            for column in columns:
                add(column, prefix=key)

    for raw_field in raw_fields:
        add(raw_field)
    return field_types, criticality, normalizers, enum_values


def _confidence_policy(
    definition: Mapping[str, Any] | None, version: int | None
) -> ConfidencePolicy:
    if definition is None:
        return ConfidencePolicy()
    try:
        floor = float(definition.get("floor", 0.85))
        critical_floor = float(definition.get("critical_floor", max(0.98, floor)))
        overrides = definition.get("field_overrides", {})
        if not isinstance(overrides, dict):
            raise RevalidationConfigError("field overrides are not an object")
        return ConfidencePolicy(
            version=f"policy:{version}",
            critical_min_confidence=critical_floor,
            standard_min_confidence=floor,
            field_min_confidence={str(key): float(value) for key, value in overrides.items()},
            critical_requires_evidence=bool(definition.get("critical_requires_evidence", True)),
            critical_candidate_margin=float(definition.get("critical_candidate_margin", 0.20)),
            standard_candidate_margin=float(definition.get("standard_candidate_margin", 0.05)),
            review_on_indeterminate_error_rules=bool(
                definition.get("review_on_indeterminate_error_rules", True)
            ),
        )
    except (TypeError, ValueError) as error:
        raise RevalidationConfigError("revalidation pinned confidence policy is invalid") from error


async def load_revalidation_config(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document: Document,
    run_id: Any,
) -> RevalidationConfig:
    """Authenticate and resolve the complete configuration frozen for ``run_id``.

    Legacy test/demo runs without a version pin keep the platform defaults.
    Once a pin exists, a missing version, cross-document run, mutable snapshot,
    or fingerprint mismatch fails closed instead of silently reading today's
    active stream configuration.
    """

    run = await ProcessingRunRepository(session, context).get(run_id)
    if run is None:
        raise RevalidationConfigError("revalidation run does not exist")
    if run.document_id != document.id:
        raise RevalidationConfigError("revalidation run does not belong to the document")
    if run.stream_version_id is None and run.config_fingerprint is None:
        if load_settings().is_development_like:
            return _baseline_config()
        raise RevalidationConfigError(
            "revalidation run predates immutable configuration pins; reprocess the document"
        )
    if run.stream_version_id is None or run.config_fingerprint is None:
        raise RevalidationConfigError(
            "revalidation run has an incomplete immutable configuration pin"
        )
    version = await StreamVersionRepository(session, context).get(run.stream_version_id)
    if version is None or version.state not in ("published", "superseded"):
        raise RevalidationConfigError(
            "revalidation run references a missing or mutable stream version"
        )
    snapshot = version.resolved_snapshot
    if not isinstance(snapshot, dict):
        raise RevalidationConfigError("revalidation run references an incomplete stream snapshot")
    stored_fingerprint = snapshot.get("fingerprint")
    if (
        not isinstance(stored_fingerprint, str)
        or stored_fingerprint != run.config_fingerprint
        or snapshot_fingerprint(dict(snapshot)) != stored_fingerprint
    ):
        raise RevalidationConfigError(
            "revalidation stream snapshot fingerprint does not match the run"
        )
    config = snapshot.get("config")
    if not isinstance(config, dict):
        raise RevalidationConfigError("revalidation stream snapshot has no configuration")

    if CATALOG_VERSION_PINS_KEY not in config:
        raise RevalidationConfigError(
            "revalidation immutable snapshot has no catalog version pins; republish the stream"
        )
    try:
        catalog_version_pins = parse_catalog_version_pins(config[CATALOG_VERSION_PINS_KEY])
        if catalog_version_pins is None:
            raise CatalogError("catalog version pins cannot be null")
        await validate_catalog_version_pins(session, context, catalog_version_pins)
    except CatalogError as error:
        raise RevalidationConfigError(
            f"revalidation catalog version pins are invalid: {error}"
        ) from error

    schema_id = _configured_uuid(config, "schema_version_id")
    rule_set_id = _configured_uuid(config, "rule_set_version_id")
    schema = await SchemaVersionRepository(session, context).get(schema_id)
    rule_set = await RuleSetVersionRepository(session, context).get(rule_set_id)
    if schema is None or schema.state not in ("published", "superseded"):
        raise RevalidationConfigError("revalidation pinned schema is missing or mutable")
    if rule_set is None or rule_set.state not in ("published", "superseded"):
        raise RevalidationConfigError("revalidation pinned rule set is missing or mutable")
    if schema.process_id != rule_set.process_id:
        raise RevalidationConfigError(
            "revalidation pinned schema and rule set belong to different processes"
        )
    field_types, criticality, normalizers, enum_values = _schema_maps(schema.definition)
    raw_rules = rule_set.definition.get("rules")
    if not isinstance(raw_rules, list) or not all(isinstance(rule, dict) for rule in raw_rules):
        raise RevalidationConfigError("revalidation pinned rule set has no rules")

    confidence_definition: Mapping[str, Any] | None = None
    confidence_version: int | None = None
    configured_confidence = config.get("confidence_policy_version_id")
    if configured_confidence is not None:
        confidence_id = _configured_uuid(config, "confidence_policy_version_id")
        if run.confidence_policy_version_id != confidence_id:
            raise RevalidationConfigError(
                "revalidation run confidence pin does not match its snapshot"
            )
        confidence = await PolicyVersionRepository(session, context).get(confidence_id)
        if (
            confidence is None
            or confidence.policy_type != "confidence"
            or confidence.state not in ("published", "superseded")
        ):
            raise RevalidationConfigError(
                "revalidation pinned confidence policy is missing or mutable"
            )
        confidence_definition = confidence.definition
        confidence_version = confidence.version_number
    elif run.confidence_policy_version_id is not None:
        raise RevalidationConfigError(
            "revalidation run pins a confidence policy absent from its snapshot"
        )

    locale = config.get("locale", "en-US")
    currency = config.get("currency", "USD")
    if not isinstance(locale, str) or not isinstance(currency, str):
        raise RevalidationConfigError("revalidation snapshot has invalid normalization settings")
    return RevalidationConfig(
        stream_config=dict(config),
        field_types=field_types,
        criticality=criticality,
        normalizer_overrides=normalizers,
        enum_values=enum_values,
        rules=tuple(dict(rule) for rule in raw_rules),
        confidence_policy=_confidence_policy(confidence_definition, confidence_version),
        normalization=NormalizationContext(locale=locale, currency=currency),
        catalog_version_pins=catalog_version_pins,
    )


async def revalidate_run(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document: Document,
    run_id: Any,
    config: RevalidationConfig | None = None,
    duplicate_override: DuplicateOverride | None = None,
) -> dict[str, Any]:
    effective_config = config or await load_revalidation_config(
        session,
        context,
        document=document,
        run_id=run_id,
    )
    fields = await ExtractedFieldRepository(session, context).list_for_run(run_id)
    corrections = latest_corrections(
        await FieldCorrectionRepository(session, context).list_for_run(run_id)
    )

    header: dict[str, Any] = {}
    tables: dict[str, dict[int, dict[str, Any]]] = {}
    signals: list[FieldSignal] = []
    effective_values: dict[tuple[str, int | None], Any | None] = {}
    for field in fields:
        correction = corrections.get((field.field_key, field.row_index))
        value, corrected = _effective(field, correction)
        effective_values[(field.field_key, field.row_index)] = value
        if field.row_index is None:
            header[field.field_key] = value
        else:
            table = field.field_key.partition(".")[0]
            tables.setdefault(table, {}).setdefault(field.row_index, {})[field.field_key] = value
        signals.append(
            FieldSignal(
                field_key=field.field_key,
                criticality=effective_config.criticality.get(field.field_key, "standard"),
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
        effective_values[(key, row)] = value
        table = key.partition(".")[0]
        tables.setdefault(table, {}).setdefault(row, {})[key] = value
        signals.append(
            FieldSignal(
                field_key=key,
                criticality=effective_config.criticality.get(key, "standard"),
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
    # not see the flag and re-route the corrected run to review.
    duplicate_policy = get_duplicate_policy(effective_config.stream_config)
    if document.duplicate_of is not None and duplicate_policy is not DuplicatePolicy.ALLOW:
        header["meta.duplicate_of"] = str(document.duplicate_of)

    data = EvaluationInput(
        header=header,
        tables={
            table: [cells for _, cells in sorted(rows.items())] for table, rows in tables.items()
        },
    )
    evaluation = evaluate_rule_set(list(effective_config.rules), data)
    decision = decide_route(signals, evaluation, effective_config.confidence_policy)
    order_date = header.get("order_date")
    try:
        as_of = date.fromisoformat(str(order_date)) if order_date else document.received_at.date()
    except ValueError:
        as_of = document.received_at.date()
    try:
        catalog_resolution = await resolve_catalog_identities(
            session,
            context,
            stream_id=document.stream_id,
            run_id=run_id,
            values=effective_values,
            as_of=as_of,
            catalog_version_pins=effective_config.catalog_version_pins,
        )
    except (CatalogError, CatalogSelectionError, ValueError) as error:
        raise RevalidationConfigError(
            f"revalidation catalog configuration is invalid: {error}"
        ) from error
    catalog_reasons = [
        {**issue.to_reason(), "severity": "error"} for issue in catalog_resolution.issues
    ]

    evaluation_summary = evaluation.summary()
    decision_json = decision.to_json()
    if (
        document.duplicate_of is not None
        and duplicate_policy is DuplicatePolicy.FLAG
        and not any(
            reason.get("rule_key") == EXACT_DUPLICATE_RULE_KEY
            for reason in decision_json["reasons"]
        )
    ):
        # Exact-duplicate policy is a platform invariant, not an optional
        # tenant-authored rule. Custom rule sets may omit the baseline hook,
        # but ``flag`` must still route to a human.
        evaluation_summary["review_required"] = True
        decision_json["route"] = "review_required"
        decision_json["reasons"].append(exact_duplicate_review_reason())
    if catalog_reasons:
        evaluation_summary["blocking"] = True
        evaluation_summary["review_required"] = True
        evaluation_summary["catalog_identity_issues"] = len(catalog_reasons)
        evaluation_summary["triggered_by_severity"]["error"] += len(catalog_reasons)
        decision_json["route"] = "review_required"
        decision_json["reasons"].extend(catalog_reasons)
    else:
        evaluation_summary["catalog_identity_issues"] = 0
    run = await ProcessingRunRepository(session, context).get(run_id)
    if run is None:
        raise RevalidationConfigError("revalidation run does not exist")
    try:
        business = await validate_order_business_data(
            session,
            context,
            run,
            document,
            fields,
            stream_config=effective_config.stream_config,
            record_selections=False,
            effective_values=effective_values,
            catalog_version_pins=effective_config.catalog_version_pins,
            duplicate_override=duplicate_override,
        )
    except (CatalogError, CatalogSelectionError) as error:
        raise RevalidationConfigError(
            f"revalidation catalog configuration is invalid: {error}"
        ) from error
    evaluation_summary, decision_json = merge_business_validation(
        evaluation_summary,
        decision_json,
        business,
    )

    flagged = {
        (reason.get("field_key"), reason.get("row_index"))
        for reason in decision_json["reasons"]
        if reason.get("field_key") is not None
    }
    for field in fields:
        correction = corrections.get((field.field_key, field.row_index))
        value, _corrected = _effective(field, correction)
        if (field.field_key, field.row_index) in flagged:
            field.validation_status = ValidationStatus.REVIEW.value
        elif value is not None:
            field.validation_status = ValidationStatus.PASSED.value
    return {
        "evaluation": evaluation_summary,
        "decision": decision_json,
        "catalog_identity": {
            "selected": len(catalog_resolution.identities),
            "confirmed_no_match": len(catalog_resolution.confirmed_no_match),
            "issues": catalog_reasons,
        },
        "business_validation": {
            "findings": list(business.findings),
            "notes": list(business.notes),
            "catalog_versions": dict(business.catalog_versions),
            "matches": business.matches,
            "override": dict(business.override_record) if business.override_record else None,
        },
    }
