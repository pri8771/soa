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

- **Missing object, source recoverable:** re-derive it. Most artifacts
  are outputs of a processing stage — reprocess the document (PRC-013) to
  regenerate the page rasters/text/canonical payload; the manifest hash
  then matches again.
- **Original upload missing:** the source document object cannot be
  re-derived. If it existed in a backup window, restore it (see
  [`restore`](restore.md)); otherwise the document is unrecoverable and
  must be re-ingested by the customer.
- **Mismatch:** quarantine the bad object, re-derive or restore the
  correct bytes, and confirm the hash.

## Verification

- `make verify-artifacts` reports zero discrepancies for the affected
  documents; a download URL issues successfully and the bytes hash-match.

## Communication

- If a source document is unrecoverable, inform the tenant and request
  re-ingestion; coordinate via
  [`security-communication`](security-communication.md) if it was part of
  a larger data-loss event.

## Follow-up

- Root-cause the loss/corruption (a deletion bug, a storage lifecycle
  misconfig, a bad write). Confirm the deletion workflow (SEC-010) and
  lifecycle rules cannot orphan or clobber live objects.
