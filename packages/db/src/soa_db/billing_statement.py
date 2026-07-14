"""Monthly billing statement and reconciliation (GTM-002).

Turns a period's raw consumption into the ONE statement a tenant is
billed from — by metering billable usage under documented rules, then
reconciling it against the GTM-001 plan. It sits between two existing
pieces and couples to no billing vendor:

- the ANA-003 usage ledger (:mod:`soa_db.usage_ledger`) is the source of
  what was consumed and what the platform spent;
- the GTM-001 plan (:mod:`soa_db.plans`) is the single source of what a
  tenant is entitled to and how overage is priced.

Two honesty separations carry through, mirroring the ledger's own:

- **what the customer is billed vs what the platform spent.** The plan
  statement (subscription entitlements + overage) is what the tenant
  owes. The provider cost is what WE paid a provider — reported
  alongside for margin visibility, never added to the customer's bill
  here. Merging them would hide margin and mislead the customer.
- **metered facts vs applied rules.** Raw per-document consumption is a
  fact; which of it is *billable* is a documented, versioned rule
  (below). The rule is explicit and testable, never buried in a query.

## Documented billing rules (the GTM-002 acceptance)

- **documents** — each distinct document that entered processing in the
  period counts once, regardless of how many times it was reprocessed.
- **pages** — pages are billed per the reprocess policy: under
  :attr:`ReprocessPolicy.BILL_FIRST_RUN_ONLY` a document's pages count
  once no matter how often it is reprocessed (reprocessing to fix a bad
  extraction is not re-charged); under
  :attr:`ReprocessPolicy.BILL_EACH_RUN` every run's pages count (the
  tenant pays for the compute each run consumes). The default is
  first-run-only — a reviewer correcting a mistake should not cost the
  customer more.
- **provider costs** — the actual reconciled provider spend
  (estimate + ledger adjustments) is summed as-is and reported
  separately. The reprocess policy NEVER changes it: the platform really
  did spend that money each run.
- **exports** — each successful export delivery counts once; a retried
  delivery of the same export is not double-counted (the export
  orchestration already collapses retries to one delivery).

Corrections to a period after it is stated do not edit the statement:
they are ledger adjustment entries (``record_adjustment`` in ANA-003),
and a restatement re-runs this function over the corrected rows. So the
statement is always recomputable from immutable ledger facts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from soa_db.plans import Plan, Statement, reconcile

__all__ = [
    "BillableUsage",
    "DocumentUsage",
    "MonthlyStatement",
    "ReprocessPolicy",
    "meter_usage",
    "monthly_statement",
]


class ReprocessPolicy(StrEnum):
    #: A document's pages are billed ONCE across the period however many
    #: times it is reprocessed — a reviewer fixing an extraction is not a
    #: new charge. The default.
    BILL_FIRST_RUN_ONLY = "bill_first_run_only"
    #: Every processing run's pages are billed — the tenant pays for the
    #: compute each run consumes.
    BILL_EACH_RUN = "bill_each_run"


@dataclass(frozen=True)
class DocumentUsage:
    """One document's consumption over the billing period, folded from
    the ANA-003 ledger rows for that document. ``run_count`` is the
    number of processing runs (>= 1; the first plus any reprocesses);
    ``pages`` is the document's page count; ``exports`` is the number of
    successful export deliveries; ``provider_cost_cents`` is the summed
    reconciled provider spend across every run of this document."""

    document_id: str
    run_count: int
    pages: int
    exports: int
    provider_cost_cents: int

    def __post_init__(self) -> None:
        if not self.document_id.strip():
            raise ValueError("document_id must be non-empty")
        if self.run_count < 1:
            raise ValueError(f"{self.document_id}: run_count is at least 1 (the first run)")
        if self.pages < 0 or self.exports < 0:
            raise ValueError(f"{self.document_id}: pages and exports cannot be negative")
        # provider_cost_cents may be negative: a net credit after a
        # reconciliation adjustment against a provider overcharge.


@dataclass(frozen=True)
class BillableUsage:
    """The billable quantities for the period after the documented rules
    are applied — the numbers that feed the plan reconciliation, plus the
    provider spend reported alongside."""

    documents: int
    pages: int
    exports: int
    #: Actual provider spend (reconciled). Reported, not billed to the
    #: customer here.
    provider_cost_cents: int
    #: How many runs were reprocesses (run_count - 1 summed) — surfaced so
    #: a statement can show what the reprocess policy waived or charged.
    reprocess_runs: int

    def as_plan_dimensions(self) -> dict[str, Decimal]:
        """The usage mapping :func:`soa_db.plans.reconcile` consumes —
        keyed to the plan's metered dimensions."""
        return {
            "documents": Decimal(self.documents),
            "pages": Decimal(self.pages),
            "exports": Decimal(self.exports),
        }


def meter_usage(
    documents: Sequence[DocumentUsage],
    *,
    reprocess: ReprocessPolicy = ReprocessPolicy.BILL_FIRST_RUN_ONLY,
) -> BillableUsage:
    """Apply the documented billing rules to a period's per-document
    consumption. Deterministic and pure — no ledger or plan access."""
    billed_documents = len(documents)
    billed_pages = 0
    billed_exports = 0
    provider_cost = 0
    reprocess_runs = 0
    for usage in documents:
        reprocess_runs += usage.run_count - 1
        billed_exports += usage.exports
        provider_cost += usage.provider_cost_cents
        if reprocess is ReprocessPolicy.BILL_EACH_RUN:
            billed_pages += usage.pages * usage.run_count
        else:  # BILL_FIRST_RUN_ONLY
            billed_pages += usage.pages
    return BillableUsage(
        documents=billed_documents,
        pages=billed_pages,
        exports=billed_exports,
        provider_cost_cents=provider_cost,
        reprocess_runs=reprocess_runs,
    )


@dataclass(frozen=True)
class MonthlyStatement:
    """A tenant's statement for one billing period. ``plan_statement`` is
    what the tenant owes (subscription entitlements + overage, from the
    GTM-001 plan); ``provider_cost_cents`` is what the platform spent
    (reported for margin, not part of the customer charge)."""

    period_label: str
    plan_key: str
    usage: BillableUsage
    plan_statement: Statement
    reprocess_policy: ReprocessPolicy

    @property
    def customer_charge_cents(self) -> int:
        """What the tenant owes for the period: plan overage charges.
        (Subscription base fees, if any, are a plan attribute GTM adds
        when a priced base tier exists; today the statement bills metered
        overage, which is the pilot model.)"""
        return self.plan_statement.total_overage_cents

    @property
    def provider_cost_cents(self) -> int:
        return self.usage.provider_cost_cents

    @property
    def over_limit_dimensions(self) -> tuple[str, ...]:
        """BLOCK dimensions whose usage exceeded the cap — enforcement
        should have prevented it; the statement surfaces any that slipped
        through so finance can follow up."""
        return self.plan_statement.over_limit_dimensions


def monthly_statement(
    plan: Plan,
    documents: Sequence[DocumentUsage],
    *,
    period_label: str,
    reprocess: ReprocessPolicy = ReprocessPolicy.BILL_FIRST_RUN_ONLY,
) -> MonthlyStatement:
    """Build the period's statement: meter billable usage under the
    documented rules, then reconcile it against the plan. Pure and
    deterministic, so a restatement over corrected ledger rows is exact."""
    if not period_label.strip():
        raise ValueError("a statement needs a period label (e.g. '2026-07')")
    usage = meter_usage(documents, reprocess=reprocess)
    plan_statement = reconcile(plan, usage.as_plan_dimensions())
    return MonthlyStatement(
        period_label=period_label,
        plan_key=plan.key,
        usage=usage,
        plan_statement=plan_statement,
        reprocess_policy=reprocess,
    )
