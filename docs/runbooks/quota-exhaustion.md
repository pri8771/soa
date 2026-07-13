# Runbook: quota exhaustion / cost burn

**Owner:** platform-on-call (quota), finops (cost) · **Alerts:**
`quota.exhaustion_imminent`, `cost.budget_burn`

A tenant is approaching a hard quota (about to be blocked) or projected
provider cost is tracking over budget.

## Detection

- Tenant usage > 90% of a resolved quota (ANA-009 feature-flags/quota
  policy), or projected monthly cost > `quota.monthly_cost_cents` from
  the usage ledger (ANA-003).
- The cost dashboard (ANA-006) shows the burn and the responsible
  streams/tenants.

## Containment

- **Quota:** decide whether to raise the tenant's quota (ANA-009 flag
  update, audited) or let the limit hold. Raising is a deliberate,
  attributed change; do not silently uncap.
- **Cost:** identify the driver — a reprocessing loop, a costly provider,
  or genuine volume growth. Reprocessing storms are the usual culprit;
  check for a stuck retry or an operator re-running exports.

## Recovery

- If a runaway loop, stop it at the source (pause the stream / cancel the
  offending jobs via the job admin API JOB-006).
- If genuine growth, raise the quota/budget with the tenant's plan owner
  and record the decision.

## Verification

- Usage trends back under threshold, or the new quota/budget is in effect
  and the alert clears; the ledger reconciles (ANA-003) with no
  unexplained spend.

## Communication

- For a customer approaching a plan limit, proactive outreach beats a
  hard block; loop in the account owner. Use
  [`security-communication`](security-communication.md) only if the burn
  was caused by an incident.

## Follow-up

- If a reprocessing loop caused the burn, add a guard (ING-006 duplicate
  detection / idempotency) so it cannot recur silently.
- Revisit the quota/budget defaults if this tenant's real usage exceeds
  the plan assumptions (GTM-001).
