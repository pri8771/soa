# Runbook: extraction/OCR provider outage

**Owner:** platform-on-call · **Alert:** `provider.extraction_unavailable`

An extraction or OCR provider is failing or timing out across the board,
stalling the core processing pipeline.

## Detection

- All attempted configured providers are degraded/unreachable with no recent
  success in the tenant-scoped `provider_runtime_metrics` facts. Check
  consecutive failures, last attempt time, and fallback count in the provider
  admin API.
- Documents pile up in the processing/OCR stages; the backlog alert may
  fire alongside this one.

## Containment

- Identify whether one provider or all are affected. The provider router
  (AIO-013) fails over between configured providers automatically; if a
  healthy provider exists, confirm traffic shifted to it.
- If all configured providers are down, stop or scale down the affected worker
  path through the deployment change procedure and, if necessary, disable the
  corresponding ingress upstream. There is no generic per-stream pause button
  to assume here. Leave durable jobs intact so bounded retry/replay can recover
  without data loss.
- Native PDF text and Tesseract recognition do not depend on a hosted
  extraction provider, but they are not substitutes for structured
  extraction. A full stream keeps running only when its published policy also
  contains a healthy local/mock extraction candidate; otherwise recognized
  inputs remain durably queued for recovery/replay.

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
- If a single provider is a recurring single point of failure, evaluate a
  second allowed candidate against the same corpus and contract. Publish it as
  a bounded fallback only when quality, region, retention, cost, and outage
  behavior pass their gates.
