# Runbook: customer data deletion

**Owner:** platform-on-call (with security-on-call for verification) ·
**Trigger:** a customer data-deletion request (contractual / regulatory)

## Detection

- A deletion request arrives for a document, a set, or a whole
  organization, with the authority to make it (verify the requester).

## Containment

- Confirm scope and that no **legal hold** blocks deletion (SEC-008
  retention engine treats a hold as an absolute block).
- Deletion is approval-gated: it moves through
  `RETAINED → ELIGIBLE → PENDING_APPROVAL → APPROVED → DELETED`
  (SEC-008); do not shortcut the approval.

## Recovery

- Execute the deletion workflow (SEC-010, `soa_db.data_deletion` +
  migration 0037): it erases the document's objects and derived rows,
  reconciles, and is idempotent/retry-safe. A retained **tombstone** and
  a counts-only audit event remain for attributability — the document row
  and audit trail are deliberately kept, not the content.
- Provider-side purge (hosted extraction caches) is a documented no-op
  until hosted adapters exist (OPEN-003/004); backup retention is a
  documented exception (the data ages out of backups per the retention
  window, see [`restore`](restore.md)).

## Verification

- The objects and derived rows are gone (reconcile with STO-005 —
  deleted artifacts must have no orphan objects); the tombstone and audit
  event exist; a re-run of the deletion is a safe no-op.

## Communication

- Confirm completion to the requester, including the documented backup-
  retention exception window and provider-purge status.

## Follow-up

- If the request was regulatory, record the completion evidence (counts
  and tombstone id) for the compliance file. Verify the retention policy
  would have eventually deleted this data anyway (SEC-008) — a manual
  request should be the exception, not the mechanism.
