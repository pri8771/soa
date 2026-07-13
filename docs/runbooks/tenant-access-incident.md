# Runbook: tenant access incident

**Owner:** security-on-call · **Alert:** `auth.anomalous_denials`

Anomalous authentication/authorization activity: a spike in auth
failures, or repeated cross-tenant probing from one principal or IP —
credential stuffing or enumeration.

## Detection

- A burst of auth failures or repeated cross-tenant `404`s from one
  identity/IP. Cross-tenant access returns `404` by design (existence
  never leaks), so a cluster of them is a probe, not a bug.
- The audit trail (DB-005) carries every auth and authorization event;
  query it via the audit API (ANA-007).

## Containment

- Rate limiting (SEC-003) already throttles abusive callers per
  principal/IP; confirm it engaged (`/health/rate-limits` counters).
- If a specific API credential is implicated, revoke it (TEN-009 service
  credentials are hashed and revocable); if a user principal, coordinate
  with the tenant to disable the account at the IdP.
- If cross-tenant data was actually reached (it should be impossible —
  RLS + repository scoping, TEN-010/011), treat as a confirmed breach and
  escalate immediately.

## Recovery

- Rotate any exposed credential (see
  [`credential-exposure`](credential-exposure.md)); confirm RLS denied
  the probes (they should show as 404/denied in the audit trail, never
  successful cross-tenant reads).

## Verification

- The denial spike subsides; the audit trail shows no successful
  cross-tenant access; the offending credential/identity is disabled.

## Communication

- If any tenant data was exposed, this is a security incident — engage
  [`security-communication`](security-communication.md) and the
  customer-notification path without delay.

## Follow-up

- Preserve the audit evidence; review whether the rate-limit thresholds
  and anomaly detection should tighten; confirm the threat model
  (`../THREAT_MODEL.md`) covers the observed vector.
