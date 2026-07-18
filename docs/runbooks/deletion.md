# Runbook: customer data deletion

**Owner:** platform-on-call (with security-on-call for verification) ·
**Trigger:** a customer data-deletion request (contractual / regulatory)

## Detection

- A deletion request arrives for one or more documents with the authority to
  make it (verify the requester). A set- or organization-wide request must be
  expanded into a persisted lifecycle for each document; the durable approval
  and erasure unit is one document.

## Containment

- Confirm scope and that no **legal hold** blocks deletion (SEC-008
  retention engine treats a hold as an absolute block).
- Place a hold with
  `POST /orgs/{org}/documents/{document_id}/legal-holds` when preservation
  is required. Placing one revokes any unexecuted approval. Releasing a hold
  never resumes deletion automatically; a different authorized principal
  must approve again.
- Record the verified request with
  `POST /orgs/{org}/documents/{document_id}/deletion-requests`. Only settled
  terminal documents are accepted. Use a case/reference reason, not copied
  customer content.
- A request recorded in error may be cancelled before execution with
  `POST /orgs/{org}/deletion-requests/{request_id}/cancel`. Cancellation is
  audited, makes document export available again, and the same attributable
  lifecycle record can later be reopened by a corrected request. Running or
  completed erasure cannot be cancelled.
- Deletion is two-person approval-gated. The requester cannot call
  `POST /orgs/{org}/deletion-requests/{request_id}/approve` successfully;
  an independent principal with `data.delete.approve` must do so. Approval
  commits the `document.delete` job in the same transaction.
- Repeating a request is idempotent: an existing non-cancelled lifecycle is
  returned instead of creating a competing approval, while a cancelled
  lifecycle is reopened as `pending_approval`. Repeating cancellation, hold
  placement, or hold release also returns the already-reached state.

### Holds, cancellation, and races

- Request, approval, cancellation, legal-hold mutation, and worker execution
  serialize on the same tenant/document lifecycle lock and lock the relevant
  rows. Whichever safety decision commits first governs the next transition;
  there is no approval/hold check-then-act window.
- An active hold blocks approval and execution. A hold placed before erasure
  commits clears any approval, including a `running` transition between worker
  transactions. If erasure commits first, the terminal deleted document cannot
  subsequently be placed on hold.
- A queued delivery made stale before execution by cancellation or a hold
  safely returns without erasing when it sees `cancelled` or
  `pending_approval`. If a hold lands after the worker records `running` but
  before the erasure transaction, that transaction rejects the cleared
  approval. Releasing a hold does not revive the stale delivery or restore its
  approval; a different authorized principal must explicitly approve the
  pending request again.
- Cancellation can win while a request is `pending_approval`, `approved`, or
  `failed`. While the current worker attempt is `running`, and after it is
  `completed`, cancellation is rejected; retries resume the same durable
  request rather than opening a new lifecycle.

## Recovery

- The worker executes the approved durable request (`soa_db.deletion_requests`,
  `soa_worker.document_deletion`, migrations 0037 and 0051). It erases
  artifact/upload objects and rows, derived runs/review/canonical data,
  linked gold examples, dependent evaluation evidence, copied document-export
  and organization-export bundles, and their manifests. It sanitizes queued
  jobs/outbox payloads, unlinks document/run identifiers
  from the immutable financial usage ledger without changing billed facts,
  clears duplicate links, and reduces the retained document row to an
  anonymized non-content shell in the terminal `deleted` state. `deleted` is
  not an alias for `archived`: the shell cannot resume, reprocess, export, or
  transition again. Every external key is tenant-fenced and its absence is
  reconciled before completion.
- Deleted shells are hidden from the default document list. An authorized
  operator must use the explicit `?document_state=deleted` filter to list
  them; normal active-work queues must not surface them.
- The explicit deleted listing and document-detail endpoint return sanitized
  shells and counts-only tombstone evidence. Stream association, original
  intake time, actors, reasons, artifacts, and historical timeline entries are
  not retransmitted through the general document API after erasure; privileged
  evidence remains in its dedicated audit/deletion ledgers.
- A retained **request**, legal-hold history, **tombstone**, anonymized
  document shell, financial totals, and counts-only completion audit remain
  for proof. Historical append-only audit evidence has its own access and
  retention policy; it is not rewritten by document erasure. It must never be
  used to store raw file contents. CI's document-reference completeness guard
  fails when a new typed document/run column has no erase/anonymize/retain
  policy.
- The application does not issue provider-side purge requests. For a tenant
  that enabled hosted extraction, follow the provider contract/API and record
  that purge or retention outcome separately. Backup retention is a documented
  exception: data ages out with the backup window rather than being removed
  from a point-in-time image (see [`restore`](restore.md)).

## Verification

- The objects and derived rows are gone (reconcile with STO-005 — deleted
  artifacts and invalidated export bundles must have no orphan objects); the
  request is `completed`; the tombstone and completion audit exist; the
  document is `deleted`; its filename/hash/source metadata/client reference
  are anonymized; it is absent from the default list and present only when the
  explicit deleted-state filter is used; usage rows have no document/run link;
  a redelivery is a safe no-op. Inspect requests with
  `GET /orgs/{org}/deletion-requests`.
- A failed attempt remains on the same request with a safe error. Retrying
  reconciles every external key again, treats already-absent objects and
  already-erased rows as success, and completes the same unique tombstone.
  Never report completion while an object survives reconciliation.

## Communication

- Confirm completion to the requester, including the documented backup-
  retention exception window and provider-purge status.

## Follow-up

- If the request was regulatory, record the completion evidence (counts
  and tombstone id) for the compliance file. Verify the retention policy
  would have eventually deleted this data anyway (SEC-008) — a manual
  request should be the exception, not the mechanism.

## The automatic retention sweep

Until this shipped, "the retention policy would have eventually deleted this
data anyway" was aspirational — nothing aged data automatically; every
deletion above was operator-triggered. `RetentionCoordinator`
(`apps/worker/src/soa_worker/retention_scheduler.py`) closes that gap the same
conservative way the rest of this runbook works: **it only ever creates a
deletion request**, landing a settled document in `pending_approval` once its
organization's published retention policy window elapses. It never approves
and never deletes — the two-person approval gate above (a distinct approver,
`POST .../deletion-requests/{id}/approve`) still stands between an automatic
request and actual erasure. A legal hold or a non-standard artifact retention
class (`extended`/`legal_hold`) is a human decision the sweep never overrides;
it skips those documents for a person to handle via the flow above.

It runs on the worker's existing periodic reconcile tick (`main.py`, the same
mechanism `ExternalCleanupCoordinator` uses) — no new job type, queue payload,
or migration. Organizations with no published `retention` policy are skipped
entirely (nothing to enforce). Two documented simplifications versus the
engine's ideal (`soa_db.retention`'s docstring calls policy-pinning "load
bearing"): settlement time is approximated as the document's `updated_at`
(nothing pins a `settled_at` today), and the policy applied is the org's
CURRENTLY published retention policy rather than one pinned at settlement — a
shorter policy published later can make older documents eligible sooner than a
true pin would. Both are safe because the worst case is an EARLIER deletion
**request**, still gated by the same human approval as everything else in this
runbook.

If a document shows up in the review queue with `requested_by:
system:retention-scheduler`, that's this sweep — treat it exactly like any
other pending request above.
