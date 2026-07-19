"""Alert catalog and ownership (REL-007).

A declarative, reviewable catalog of the production alerts the platform
ships with. Each alert names the SIGNAL it watches (a metric the code
already emits, or the task that will emit it), a threshold with a stated
RATIONALE, a SEVERITY, an OWNER role, the RUNBOOK slug that handles it
(REL-008), and a TEST SIGNAL describing how a firing is exercised.

This module is provider-agnostic on purpose: it is the source of truth
for *what* we alert on and *who owns it*, independent of the monitoring
backend (Grafana/Prometheus/hosted) that REL-002 infrastructure wires
these definitions into. Keeping it as validated Python means the catalog
is tested — no alert may ship without an owner, a rationale, and a
runbook.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "CATALOG",
    "AlertCategory",
    "AlertDefinition",
    "AlertSeverity",
    "Owner",
    "runbook_slugs",
]


class AlertSeverity(StrEnum):
    #: Page immediately; customer-visible failure or data-safety risk.
    CRITICAL = "critical"
    #: Urgent but not paging-at-3am; degradation or approaching a limit.
    HIGH = "high"
    #: Ticket / next-business-day; trend or soft threshold.
    WARNING = "warning"


class AlertCategory(StrEnum):
    QUEUE = "queue"
    PROVIDER = "provider"
    SCHEMA_ERROR = "schema_error"
    COST = "cost"
    SLA = "sla"
    EXPORT = "export"
    BACKUP = "backup"
    AUTH_ANOMALY = "auth_anomaly"
    QUOTA = "quota"


class Owner(StrEnum):
    """On-call rotations that own response. Concrete people/pager
    mappings live in the deployment (REL-002), not in code."""

    PLATFORM_ONCALL = "platform-on-call"
    SECURITY_ONCALL = "security-on-call"
    FINOPS = "finops"


@dataclass(frozen=True)
class AlertDefinition:
    key: str
    title: str
    category: AlertCategory
    severity: AlertSeverity
    owner: Owner
    #: The observable this alert reads. A ``soa.*`` name is a metric the
    #: code emits today; ``pending:<TASK>`` marks a signal a not-yet-built
    #: task must emit before the alert can arm (honest, not faked).
    signal: str
    condition: str
    rationale: str
    runbook: str  # slug under docs/runbooks/ (REL-008)
    test_signal: str

    @property
    def signal_is_live(self) -> bool:
        return not self.signal.startswith("pending:")


CATALOG: tuple[AlertDefinition, ...] = (
    AlertDefinition(
        key="queue.backlog_growing",
        title="Job queue backlog is growing",
        category=AlertCategory.QUEUE,
        severity=AlertSeverity.HIGH,
        owner=Owner.PLATFORM_ONCALL,
        signal="soa.jobs.oldest_pending_age_seconds",
        condition="oldest pending job age > 900s for 10m",
        rationale=(
            "Documents finish within minutes when workers keep up; a 15-minute-old "
            "pending job means throughput has fallen behind intake and review SLAs "
            "are at risk before any single job fails."
        ),
        runbook="backlog",
        test_signal="enqueue jobs with no worker draining; assert the gauge crosses 900s",
    ),
    AlertDefinition(
        key="queue.dead_letter_spike",
        title="Jobs are dead-lettering",
        category=AlertCategory.QUEUE,
        severity=AlertSeverity.HIGH,
        owner=Owner.PLATFORM_ONCALL,
        signal="soa.jobs.dead_lettered",
        condition="rate(soa.jobs.dead_lettered) > 0 sustained over 5m",
        rationale=(
            "A job reaching the dead-letter state has exhausted retries (JOB-005); "
            "a sustained rate is a systemic handler or dependency failure, not a "
            "one-off, and each dead letter is stalled customer work."
        ),
        runbook="backlog",
        test_signal="force a handler to always fail; assert the dead_lettered counter rises",
    ),
    AlertDefinition(
        key="worker.absent",
        title="No worker is draining the job queue",
        category=AlertCategory.QUEUE,
        severity=AlertSeverity.HIGH,
        owner=Owner.PLATFORM_ONCALL,
        signal="soa.jobs.seconds_since_last_claim",
        condition=(
            "soa.jobs.seconds_since_last_claim > 300s while "
            "soa.jobs.queue_depth{soa.status=pending} > 0 for 5m"
        ),
        rationale=(
            "Zero workers draining the queue is a distinct, more urgent failure "
            "than a merely-growing backlog: nothing is being attempted at all, so "
            "queue.backlog_growing would not fire for another ~10 minutes (900s "
            "threshold). Catching worker absence directly gets an operator paged "
            "before customer SLAs are at risk."
        ),
        runbook="backlog",
        test_signal="enqueue pending jobs with no worker running; assert the gauge crosses 300s",
    ),
    AlertDefinition(
        key="provider.extraction_unavailable",
        title="Extraction/OCR provider is failing or timing out",
        category=AlertCategory.PROVIDER,
        severity=AlertSeverity.CRITICAL,
        owner=Owner.PLATFORM_ONCALL,
        signal="soa_db.provider_runtime_metrics",
        condition=(
            "all attempted configured extraction providers are degraded/unreachable and "
            "none has succeeded for 5m"
        ),
        rationale=(
            "The provider router (AIO-013) fails over between providers, but having "
            "no recently successful configured provider halts the pipeline; extraction "
            "is the core product path, so this pages."
        ),
        runbook="provider-outage",
        test_signal="stub all providers to error; assert the router surfaces exhaustion",
    ),
    AlertDefinition(
        key="schema.repair_fallback_rate",
        title="Extraction schema-repair/fallback rate is elevated",
        category=AlertCategory.SCHEMA_ERROR,
        severity=AlertSeverity.WARNING,
        owner=Owner.PLATFORM_ONCALL,
        signal="pending:AIO-012",
        condition="schema-repair fallback ratio > 10% over 1h",
        rationale=(
            "AIO-012 repairs malformed model output; a rising fallback rate signals "
            "a prompt or model regression degrading extraction quality before it "
            "shows up as customer-visible errors."
        ),
        runbook="bad-release",
        test_signal="feed malformed model output; assert the repair path is counted",
    ),
    AlertDefinition(
        key="cost.budget_burn",
        title="Provider cost is tracking over budget",
        category=AlertCategory.COST,
        severity=AlertSeverity.HIGH,
        owner=Owner.FINOPS,
        signal="soa_db.usage_ledger",
        condition="projected monthly cost > quota.monthly_cost_cents (ANA-009)",
        rationale=(
            "The usage ledger (ANA-003) records real provider spend; projecting it "
            "against the configured monthly budget (ANA-009) catches runaway cost "
            "while there is still time to intervene, not at the invoice."
        ),
        runbook="quota-exhaustion",
        test_signal="record ledger entries exceeding a low test budget; assert projection breach",
    ),
    AlertDefinition(
        key="sla.review_overdue",
        title="Review SLA breaches are accumulating",
        category=AlertCategory.SLA,
        severity=AlertSeverity.HIGH,
        owner=Owner.PLATFORM_ONCALL,
        signal="soa_db.analytics.operational_snapshot",
        condition="overdue_now / open review tasks > 20% for 30m",
        rationale=(
            "The operations snapshot (ANA-001) reports SLA overdue/breached counts; "
            "a rising overdue fraction means reviewers or routing cannot keep pace, "
            "which is the contractual promise to the customer."
        ),
        runbook="backlog",
        test_signal="seed overdue review tasks; assert the snapshot's overdue_now rises",
    ),
    AlertDefinition(
        key="export.delivery_failures",
        title="ERP export deliveries are failing",
        category=AlertCategory.EXPORT,
        severity=AlertSeverity.CRITICAL,
        owner=Owner.PLATFORM_ONCALL,
        signal="soa_db.export.delivery_attempts",
        condition="delivery failure ratio > 20% over 15m",
        rationale=(
            "A validated order that cannot reach the customer's ERP (EXP-008) is "
            "undelivered business value; a sustained failure ratio implies a "
            "receiver outage or a broken mapping, both customer-visible."
        ),
        runbook="failed-export",
        test_signal="stub the delivery adapter to fail; assert failed attempts are recorded",
    ),
    AlertDefinition(
        key="backup.stale_or_failed",
        title="Database backup is stale or failed",
        category=AlertCategory.BACKUP,
        severity=AlertSeverity.CRITICAL,
        owner=Owner.PLATFORM_ONCALL,
        signal="pending:REL-003",
        condition="no successful backup within the RPO window",
        rationale=(
            "A missed backup silently erodes the recovery point; because the loss "
            "is invisible until a restore is needed, a stale backup pages "
            "immediately. Backups themselves land with REL-003."
        ),
        runbook="restore",
        test_signal="REL-003 restore rehearsal verifies the freshness signal fires when stale",
    ),
    AlertDefinition(
        key="auth.anomalous_denials",
        title="Anomalous authentication/authorization denials",
        category=AlertCategory.AUTH_ANOMALY,
        severity=AlertSeverity.HIGH,
        owner=Owner.SECURITY_ONCALL,
        signal="soa_db.audit_events",
        condition="spike in auth failures or cross-tenant 404s from one principal/IP",
        rationale=(
            "A burst of auth failures or repeated cross-tenant probes (which return "
            "404 by design) is a credential-stuffing or enumeration attempt; the "
            "audit trail carries the events, and security owns the response."
        ),
        runbook="tenant-access-incident",
        test_signal="drive repeated failed auth from one identity; assert audit events cluster",
    ),
    AlertDefinition(
        key="quota.exhaustion_imminent",
        title="A tenant is approaching a hard quota",
        category=AlertCategory.QUOTA,
        severity=AlertSeverity.WARNING,
        owner=Owner.PLATFORM_ONCALL,
        signal="soa_db.feature_flags",
        condition="tenant usage > 90% of a resolved quota (ANA-009)",
        rationale=(
            "Hitting a hard quota rejects the tenant's work; warning at 90% gives "
            "support time to raise the limit or reach out before the customer is "
            "blocked."
        ),
        runbook="quota-exhaustion",
        test_signal="resolve a low quota and drive usage past 90%; assert the threshold trips",
    ),
)


def runbook_slugs() -> frozenset[str]:
    """Every runbook slug referenced by the catalog (REL-008 provides
    a document for each)."""
    return frozenset(alert.runbook for alert in CATALOG)
