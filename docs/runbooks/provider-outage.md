# Runbook: extraction/OCR provider outage

**Owner:** platform-on-call · **Alert:** `provider.extraction_unavailable`

An extraction or OCR provider is failing or timing out across the board,
stalling the core processing pipeline.

## Detection

- Provider error+timeout ratio elevated (the AIO-013 router's failover
  telemetry; this signal is `pending:AIO-013` until per-stream routing
  is wired).
- Documents pile up in the processing/OCR stages; the backlog alert may
  fire alongside this one.

## Containment

- Identify whether one provider or all are affected. The provider router
  (AIO-013) fails over between configured providers automatically; if a
  healthy provider exists, confirm traffic shifted to it.
- If all configured providers are down, pause intake for affected
  streams if possible so documents queue rather than fail terminally —
  processing runs are resumable (PRC-003), so a paused pipeline recovers
  without data loss.
- Local adapters (native PDF text AIO-002, Tesseract OCR AIO-004) do not
  depend on a hosted provider; streams that can use them keep running.

## Recovery

- When the provider recovers, unpause intake and let the queue drain
  (see [`backlog`](backlog.md)).
- Reprocess any documents that failed terminally during the outage via
  the reprocess API (PRC-013).

## Verification

- Provider error ratio back to baseline; a sample document completes the
  full pipeline; no documents stuck in the extraction stage.

## Communication

- Hosted-provider outages are usually not customer-visible if failover
  or queueing held; if throughput SLAs were missed, use
  [`security-communication`](security-communication.md).

## Follow-up

- Record the outage window and whether failover worked as designed.
- If a single provider is a recurring single point of failure, prioritise
  a second production adapter (blocked on OPEN-003/004 — owner provider
  decisions).
