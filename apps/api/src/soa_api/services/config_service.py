"""Draft validation and checked publish (CFG-007).

The per-artifact publish helpers (CFG-001..005) enforce their own local
invariants. This service is the operator-facing layer above them: it
validates a process draft against everything it will run with — published
schema, rule set, and policies — and produces a CLEAR validation report
(machine-readable findings with level/path/message). Publish refuses on
any error-level finding; warnings publish but stay in the report.

Transactionality comes from the session: the caller's unit of work either
commits the supersede + publish + audit trail together or rolls all of it
back. Concurrent publishes are backstopped by the single-published unique
indexes (0016) and surface as conflicts, never as double-published state.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from soa_api.domain.policies import (
    PolicyType,
    PolicyValidationError,
    PolicyVersionRepository,
    validate_provider_capabilities,
)
from soa_api.domain.processes import (
    Process,
    ProcessVersion,
    publish_draft,
    set_active_version,
)
from soa_api.domain.rules import RuleExpressionError, RuleSetVersionRepository, validate_rule_set
from soa_api.domain.schemas import (
    SchemaValidationError,
    SchemaVersionRepository,
    validate_schema,
)
from soa_db.repository import OrganizationContext


@dataclass(frozen=True)
class ValidationFinding:
    level: str  # "error" | "warning"
    path: str  # what was inspected, e.g. "schema", "rules.high-value"
    message: str


@dataclass(frozen=True)
class ValidationReport:
    findings: list[ValidationFinding] = field(default_factory=list)

    @property
    def is_publishable(self) -> bool:
        return all(finding.level != "error" for finding in self.findings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "publishable": self.is_publishable,
            "findings": [
                {"level": f.level, "path": f.path, "message": f.message} for f in self.findings
            ],
        }


class DraftNotPublishableError(Exception):
    """Raised when a checked publish is attempted on a failing draft. The
    attached report tells the operator exactly what to fix."""

    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        errors = [f.message for f in report.findings if f.level == "error"]
        super().__init__(f"draft is not publishable: {'; '.join(errors)}")


async def validate_process_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    process: Process,
    draft: ProcessVersion,
) -> ValidationReport:
    """Cross-artifact validation for a process draft.

    Checks the draft against the published schema, rule set, and policies
    it would run with. Every problem becomes a finding — the report is the
    contract the publish UI (CFG-014) renders, so messages must stand on
    their own.
    """
    findings: list[ValidationFinding] = []

    input_contract = draft.definition.get("input_contract", "single_sales_order")
    if input_contract != "single_sales_order":
        findings.append(
            ValidationFinding(
                level="error",
                path="input_contract",
                message="production currently supports exactly one sales order per input; "
                "packet classification and splitting are not available",
            )
        )

    schema_version = await SchemaVersionRepository(session, context).get_published(process.id)
    field_types: dict[str, str] = {}
    if schema_version is None:
        findings.append(
            ValidationFinding(
                level="error",
                path="schema",
                message="no published extraction schema — publishing would create "
                "an unexecutable stream",
            )
        )
    else:
        try:
            field_types = validate_schema(schema_version.definition).field_types()
        except SchemaValidationError as exc:
            findings.append(
                ValidationFinding(
                    level="error",
                    path="schema",
                    message=f"published schema no longer validates: {exc}",
                )
            )

    rule_set = await RuleSetVersionRepository(session, context).get_published(process.id)
    if rule_set is None:
        findings.append(
            ValidationFinding(
                level="error",
                path="rules",
                message="no published rule set — publishing would create an unvalidated stream",
            )
        )
    elif field_types:
        try:
            validate_rule_set(rule_set.definition, field_types)
        except RuleExpressionError as exc:
            findings.append(
                ValidationFinding(
                    level="error",
                    path="rules",
                    message=f"published rule set conflicts with the schema: {exc}",
                )
            )

    provider = await PolicyVersionRepository(session, context).get_published(PolicyType.PROVIDER)
    if provider is None:
        findings.append(
            ValidationFinding(
                level="error",
                path="policies.provider",
                message="no published provider policy — the pipeline has no "
                "extraction capability to run with",
            )
        )
    else:
        try:
            validate_provider_capabilities(provider.definition)
        except PolicyValidationError as exc:
            findings.append(
                ValidationFinding(level="error", path="policies.provider", message=str(exc))
            )

    return ValidationReport(findings=findings)


async def publish_process_draft_checked(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    process: Process,
    draft: ProcessVersion,
    actor_id: str,
) -> tuple[ProcessVersion, ValidationReport]:
    """Validate, then publish in the caller's transaction.

    Error findings refuse the publish with the full report attached;
    warnings ride along on success. Supersede + publish + active-pointer
    move + audit events commit or roll back as one unit.
    """
    report = await validate_process_draft(session, context, process=process, draft=draft)
    if not report.is_publishable:
        raise DraftNotPublishableError(report)
    schema = await SchemaVersionRepository(session, context).get_published(process.id)
    rules = await RuleSetVersionRepository(session, context).get_published(process.id)
    provider = await PolicyVersionRepository(session, context).get_published(PolicyType.PROVIDER)
    confidence = await PolicyVersionRepository(session, context).get_published(
        PolicyType.CONFIDENCE
    )
    if schema is None or rules is None or provider is None:
        raise DraftNotPublishableError(
            await validate_process_draft(session, context, process=process, draft=draft)
        )
    # Materialize immutable cross-artifact references into the process
    # definition before it is sealed. Stream snapshots can then execute
    # without consulting whichever versions happen to be current later.
    draft.definition = {
        **draft.definition,
        "input_contract": "single_sales_order",
        "evaluation_gate_required": bool(draft.definition.get("evaluation_gate_required", True)),
        "schema_version_id": str(schema.id),
        "rule_set_version_id": str(rules.id),
        "provider_policy_version_id": str(provider.id),
        **({"confidence_policy_version_id": str(confidence.id)} if confidence is not None else {}),
    }
    published = await publish_draft(
        session, context, process=process, draft=draft, actor_id=actor_id
    )
    return published, report


async def rollback_active_version(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    process: Process,
    target: ProcessVersion,
    reason: str,
    actor_id: str,
) -> Process:
    """Audited rollback pointer (delegates to the CFG-001 helper; the
    reason is mandatory here because operators use this under pressure)."""
    if not reason.strip():
        raise ValueError("rollback requires a reason for the audit trail")
    return await set_active_version(
        session, context, process=process, version=target, actor_id=actor_id, reason=reason
    )
