# Resilience tests (REL-010)

The failure modes the platform must survive, and the invariants each one
must not violate: **no data loss or duplicate business delivery**, and
**user-visible state stays recoverable**. Exercised against the real
components in `tests/resilience/`.

| Failure mode | What is asserted | Backed by |
| --- | --- | --- |
| Worker kill | A crashed worker's in-flight job returns to the queue when its lock expires, its attempt count preserved; a crash on the final attempt dead-letters instead of resurrecting forever | JOB-004 `recover_expired_locks` |
| Provider timeout / outage | A timeout or crash is classified **retryable** (the document is reprocessed, not lost); a malformed file or limit breach is **terminal** (no infinite loop) | PRC-004 render classification, JOB-005 |
| Database transient error | A transient failure reschedules with backoff; a permanent error dead-letters immediately; exhausted retries dead-letter — never a silent drop | JOB-005 `mark_failed` |
| Storage failure | A missing object read, a delete of a missing key, and a checksum-mismatched write all **raise** rather than silently succeeding, so reconciliation catches them | STO storage contract, STO-005 |
| Duplicate event | The same triggering event twice collapses to one job / one export intent — duplicate delivery cannot fan out | JOB-002 dedupe key, EXP-008 business key |
| Receiver timeout | A failed delivery is retried, the export intent stays single, and exactly one successful business delivery is recorded despite the retry | EXP-005/008 append-only attempts |

These run in the normal Python CI job (SQLite / in-memory, deterministic
with injected clocks) — a fast regression guard on the recovery and
idempotency paths. Full fault-injection under real infrastructure (network
partitions, a killed Postgres, a storage-provider outage) is not automated in
this repository. It remains a staging release gate that needs executable
scenarios and captured evidence; the unit suite proves only the code-level
invariants those exercises depend on.

The corresponding operator response for each mode is in
[`runbooks/`](runbooks/README.md) (REL-008).
