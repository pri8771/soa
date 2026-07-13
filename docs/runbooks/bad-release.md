# Runbook: bad release

**Owner:** platform-on-call · **Alert:** `schema.repair_fallback_rate`
(and any regression a deploy introduces)

A deployment degraded the system — rising schema-repair fallback,
elevated errors, latency, or a functional regression.

## Detection

- A metric regresses shortly after a deploy: schema-repair fallback ratio
  (`pending:AIO-012`), error rates, queue backlog, or latency.
- Correlate the regression's start with the deploy timeline (REL-005
  provides immutable build + deploy evidence).

## Containment

- Roll back to the previous immutable artifact (REL-006 rollback
  strategy; the deploy workflow REL-005 promotes the same artifact, so
  rollback is redeploying the prior image tag/digest).
- Migrations follow expand/contract (REL-006), so a code rollback is
  safe against the current schema without a down-migration in the hot
  path.

## Recovery

- After rollback, confirm the regressed metric returns to baseline.
- Reprocess documents that produced bad output during the bad-release
  window (PRC-013), especially any that auto-approved on degraded
  extraction — check the quality dashboard (ANA-005) correction proxies.

## Verification

- The regressed signal is back to baseline; the gold-dataset evaluation
  (AIO-016/017) passes on the restored version; a smoke test of the
  end-to-end pipeline succeeds.

## Communication

- If bad output reached customers (exports delivered from degraded
  extraction), coordinate via
  [`security-communication`](security-communication.md) and consider
  re-exporting corrected orders.

## Follow-up

- Root-cause why the pre-deploy checks (CI, evaluation gate AIO-017) did
  not catch it; add the missing gate or test.
- The evaluation promotion gate should block a version that regresses the
  gold set — tighten its threshold if it let this through.
