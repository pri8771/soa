# Runbook: queue backlog / review SLA breach

**Owner:** platform-on-call · **Alerts:** `queue.backlog_growing`,
`queue.dead_letter_spike`, `worker.absent`, `sla.review_overdue`

The job queue is falling behind intake, jobs are dead-lettering, no worker
is draining the queue at all, or review tasks are breaching SLA. Left
alone this cascades into customer-visible delays.

## Detection

- `soa.jobs.oldest_pending_age_seconds` climbing, `soa.jobs.queue_depth`
  (per status) rising, or `soa.jobs.dead_lettered` incrementing.
- `soa.jobs.seconds_since_last_claim` above 300s while pending queue
  depth is nonzero — no worker has claimed or heartbeated any job
  recently (`worker.absent`). This is a more urgent, more specific signal
  than `queue.backlog_growing`: it fires in 5 minutes instead of waiting
  ~10 for the backlog age threshold, and it means zero jobs are being
  attempted, not merely falling behind. The Jobs screen and Operations
  dashboard show a "Worker stalled" badge under the same condition
  (`last_claim_at` null or stale while jobs are pending).
- The Queue Health UI (JOB-007) and the operations dashboard (ANA-004)
  show the same backlog and the SLA overdue count.

## Containment

- Confirm workers are alive (container liveness, `soa_worker.
  healthcheck`); restart or scale out worker replicas — the claim loop
  uses `SKIP LOCKED` (JOB-003), so added workers pick up pending jobs
  safely with no double-processing.
- If one job type is flooding, check for a poison job or an upstream
  provider stall (see [`provider-outage`](provider-outage.md)).
- If dead-lettering, inspect the dead-letter reason via the job admin
  API (JOB-006); a systemic handler bug is a rollback candidate (see
  [`bad-release`](bad-release.md)).

## Recovery

- Drain the backlog with added capacity; requeue dead-lettered jobs from
  the admin API once the root cause is fixed.
- For review-SLA breaches, rebalance reviewer assignment (REV routing)
  and prioritise the oldest blocking tasks.

## Verification

- `oldest_pending_age_seconds` returns below the alert threshold and
  queue depth trends down; the dead-letter rate is zero.
- The operations dashboard shows overdue review counts recovering.

## Communication

- If customer-facing latency exceeded the support SLA, follow
  [`security-communication`](security-communication.md) for the status
  update path (the same mechanism carries operational incidents).

## Follow-up

- File the root cause (capacity, poison job, provider, bad release).
- If capacity-driven, revisit worker autoscaling thresholds; if a job
  type is chronically slow, open a performance item (REL-009).
