# Runbook: database / object restore

**Owner:** platform-on-call · **Alert:** `backup.stale_or_failed`

Recover from data loss or corruption by restoring the database and
reconciling object storage. Also the procedure a stale-backup alert
points at.

> **Status:** the GCP Terraform configures Cloud SQL backups and PITR, but it
> has not been applied or restored in the target project. The restore rehearsal
> remains required, and the alert catalog's freshness signal is still
> `pending:REL-003` until monitoring reads the live backup state. The RPO/RTO
> values are targets until the rehearsal measures them.

## Detection

- No successful backup within the RPO window, a corruption report, or an
  operator error requiring point-in-time recovery.

## Containment

- Stop writes to the affected scope if corruption is spreading: disable the
  applicable ingress upstream and scale/stop workers through the deployment
  procedure. Do not assume an application-level pause control exists, and do
  not delete anything — restoration needs the current state for comparison.
- Identify the target recovery point (PITR timestamp) and confirm the
  backup covering it is intact.

## Recovery

- Restore the database to a fresh instance at the chosen recovery point
  (REL-003 mechanism), then restore/copy objects for the same window.
- Reconcile artifacts against the restored database with the manifest
  hash check (STO-005, `make verify-artifacts`): every artifact row must
  have a matching object and hash. Mismatches go to
  [`object-recovery`](object-recovery.md).

## Verification

- Run the gold-dataset evaluation and an end-to-end smoke test against
  the restored system; confirm the achieved RPO/RTO and record them
  (REL-004 acceptance).

## Communication

- Data-loss events are customer-affecting and often reportable; drive
  comms through [`security-communication`](security-communication.md)
  with the recovery point and any gap.

## Follow-up

- Record actual vs. target RPO/RTO and resolve discrepancies; if the
  backup was stale, fix the backup pipeline before closing.
