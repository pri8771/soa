# Runbook: missing / mismatched stored object

**Owner:** platform-on-call · **Trigger:** artifact-manifest reconciliation
finds an object missing or its hash mismatched (STO-005)

## Detection

- `make verify-artifacts` (STO-005 reconciliation) reports an artifact row
  whose object is absent from storage, or whose stored bytes do not match
  the recorded sha256.
- A download-URL request returns `409` ("stored object is missing",
  STO-004) — the API refuses to hand out a URL for a phantom object.

## Containment

- Do not delete the artifact metadata — it is the record of what should
  exist and the key to recovery.
- Classify: a *missing* object (gone) vs. a *mismatch* (corrupted or
  overwritten). A mismatch is more serious — investigate whether a write
  path or a bad deploy corrupted it.

## Recovery

- **Missing derived object:** restore the exact object/version from backup and
  verify it against the existing artifact hash. A normal reprocess creates a
  new immutable run and new run-scoped artifacts; it does **not** repair the
  missing historical key. No generic in-place derived-artifact rebuild tool is
  implemented today.
- **Original upload missing:** the source document object cannot be
  re-derived. If it existed in a backup window, restore it (see
  [`restore`](restore.md)); otherwise the document is unrecoverable and
  must be re-ingested by the customer.
- **Mismatch:** preserve/quarantine the bad bytes for investigation, restore
  the exact recorded version from backup, and confirm the hash. If no matching
  backup exists, do not alter the immutable artifact row: keep the discrepancy
  open and downloads fail-closed, then create a new processing run for a new
  current artifact rather than overwriting history.

## Verification

- For a restored artifact, `make verify-artifacts` reports no discrepancy for
  its key and a download URL issues successfully with matching bytes. If exact
  recovery is impossible, the historical discrepancy remains explicitly open,
  its download stays unavailable, and the replacement run/artifact is verified
  separately; a new run must not be reported as repair of the old key.

## Communication

- If a source document is unrecoverable, inform the tenant and request
  re-ingestion; coordinate via
  [`security-communication`](security-communication.md) if it was part of
  a larger data-loss event.

## Follow-up

- Root-cause the loss/corruption (a deletion bug, a storage lifecycle
  misconfig, a bad write). Confirm the deletion workflow (SEC-010) and
  lifecycle rules cannot orphan or clobber live objects.
