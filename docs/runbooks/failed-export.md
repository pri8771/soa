# Runbook: failed ERP export deliveries

**Owner:** platform-on-call · **Alert:** `export.delivery_failures`

Validated orders cannot reach the customer's ERP — a receiver outage, a
broken mapping, or bad credentials. This is undelivered business value.

## Detection

- Delivery failure ratio elevated over the delivery-attempt records
  (EXP-005/008); the delivery history UI (EXP-009) shows failing
  attempts and their error outcomes.

## Containment

- Determine scope: one integration/tenant or many. A single failing
  receiver is that tenant's outage; broad failure suggests our export
  orchestration or a shared dependency.
- Export delivery is retried with backoff and is idempotent per order
  (EXP-008), so transient receiver errors self-heal; do not manually
  re-fire deliveries that will retry on their own.
- For an auth failure to a receiver, check the integration's credentials
  (the secret is a reference, SEC-005 — rotate at the source if leaked;
  see [`credential-exposure`](credential-exposure.md)).

## Recovery

- Once the receiver or mapping is fixed, let the retry schedule drain, or
  trigger a bounded replay of the failed exports (EXP replay).
- For a broken mapping profile, correct it (EXP-003/004) and re-export
  the affected orders; the mapping engine is deterministic (EXP-002) so a
  re-run reproduces exactly.

## Verification

- Delivery success ratio recovers; each previously-failed order shows a
  delivered attempt; no duplicate business delivery occurred (idempotency
  held).

## Communication

- If orders were delayed past commitments, notify the affected tenant(s)
  via [`security-communication`](security-communication.md).

## Follow-up

- If a mapping bug caused it, add a validation case; if a receiver is
  chronically flaky, review timeout/retry tuning and alert thresholds.
