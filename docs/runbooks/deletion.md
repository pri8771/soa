# Runbook: customer data deletion

**Owner:** platform-on-call (with security-on-call for verification) ·
**Trigger:** a customer data-deletion request (contractual / regulatory)

## Detection

- A deletion request arrives for a document, a set, or a whole
  organization, with the authority to make it (verify the requester).

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

## Recovery

- The worker executes the approved durable request (`soa_db.deletion_requests`,
  `soa_worker.document_deletion`, migrations 0037 and 0051). It erases
  artifact/upload objects and rows, derived runs/review/canonical data,
  linked gold examples, dependent evaluation evidence, copied document-export
  and organization-export bundles, and their manifests. It sanitizes queued
  jobs/outbox payloads, unlinks document/run identifiers
  from the immutable financial usage ledger without changing billed facts,
  clears duplicate links, and reduces the retained document row to an
  archived non-content shell. Every external key is tenant-fenced and its
  absence is reconciled before completion.
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
  document filename/hash/source metadata/client reference are anonymized;
  usage rows have no document/run link; a redelivery is a safe no-op. Inspect
  requests with `GET /orgs/{org}/deletion-requests`.

## Communication

- Confirm completion to the requester, including the documented backup-
  retention exception window and provider-purge status.

## Follow-up

- If the request was regulatory, record the completion evidence (counts
  and tombstone id) for the compliance file. Verify the retention policy
  would have eventually deleted this data anyway (SEC-008) — a manual
  request should be the exception, not the mechanism.
