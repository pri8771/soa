# Billing statement and reconciliation (GTM-002)

How a tenant's monthly bill is produced, and the exact rules that decide
which consumption is billable. The statement is built by
[`soa_db.billing_statement`](../packages/db/src/soa_db/billing_statement.py)
from two existing sources and couples to no billing vendor:

- the **ANA-003 usage ledger** — what was consumed and what the platform
  spent (append-only, immutable rows; corrections are appended
  adjustments, never edits);
- the **GTM-001 plan** — what the tenant is entitled to and how overage
  is priced.

Because both inputs are immutable facts and the statement function is
pure, a statement is always **recomputable** — a restatement after a
correction is just a re-run over the corrected ledger rows.

## What the customer is billed vs what the platform spent

These are reported separately and never merged:

- **Customer charge** — subscription entitlements plus priced overage,
  from the plan. This is what the tenant owes.
- **Provider cost** — the reconciled amount the platform actually paid a
  provider (OCR, extraction, etc.). Reported alongside for margin
  visibility, never added to the customer's bill in this statement.

Merging them would hide margin and misstate the customer's charge.

## Billable metering rules

Applied by `meter_usage`; each is explicit and covered by a test.

| Dimension | Rule |
| --- | --- |
| **documents** | Each distinct document that entered processing in the period counts **once**, however many times it was reprocessed. |
| **pages** | Governed by the **reprocess policy** (below). |
| **exports** | Each successful export delivery counts once; a retried delivery of the same export is not double-counted (export orchestration already collapses retries to one delivery). |
| **provider costs** | The actual reconciled spend is summed **as-is** and reported separately. The reprocess policy never changes it — the platform really did spend that money each run. A net credit (negative) is allowed after reconciling a provider overcharge. |

### Reprocess policy

Reprocessing a document (e.g. a reviewer re-runs extraction after fixing
a bad reading) re-consumes compute. Who pays for the re-run is a
documented, per-deployment policy:

- **`BILL_FIRST_RUN_ONLY`** (default) — a document's pages are billed
  **once** across the period no matter how often it is reprocessed. A
  reviewer correcting a mistake does not cost the customer more.
- **`BILL_EACH_RUN`** — every run's pages are billed; the tenant pays for
  the compute each run consumes.

Under either policy the **provider cost** reflects every run — the choice
only affects what the customer is charged for pages, never what the
platform records as spent.

## Adjustments and restatement

A statement is never edited after the fact. A correction to a stated
period is a **ledger adjustment entry** (`record_adjustment` in ANA-003):
a signed delta against the entry it corrects, with a written reason,
audited. Re-running `monthly_statement` over the corrected rows produces
the restated bill deterministically.

## Enforcement and reporting share one source

The plan is the single source of both the hard quotas product enforcement
provisions (`plan_quotas` → ANA-009) and the entitlements this statement
reconciles against (`reconcile`). A BLOCK dimension that somehow exceeded
its cap is surfaced on the statement (`over_limit_dimensions`) but never
billed for overage — enforcement should have stopped it, and reporting
flags any that slipped through for finance to follow up. See
[`DECISIONS.md`](DECISIONS.md) ADRs on the plan model and
[`ALERTS.md`](ALERTS.md) for quota alerting.
