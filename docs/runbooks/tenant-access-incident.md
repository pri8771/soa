# Runbook: tenant access incident

**Owner:** security-on-call · **Alert:** `auth.anomalous_denials`

Anomalous authentication/authorization activity: a spike in auth
failures, or repeated cross-tenant probing from one principal or IP —
credential stuffing or enumeration.

## Detection

- A burst of authentication failures or repeated cross-tenant `404`s from one
  identity/IP in IdP, ingress, or structured request logs. Cross-tenant access
  returns `404` by design (existence never leaks), so a cluster is a probe, not
  proof of a missing resource.
- The application audit API exposes recorded security/administrative events,
  but the application does not persist every authentication or authorization
  denial as an audit row. Preserve and correlate IdP/ingress/request logs with
  the rate-limit and application audit facts; the complete live anomaly rule
  and log retention are deployment controls.

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
  [`credential-exposure`](credential-exposure.md)); confirm resource requests
  were denied in the correlated request/ingress evidence and use database/RLS
  evidence plus tenant data inspection to rule out successful cross-tenant
  reads.

## Verification

- The denial spike subsides; investigation finds no successful cross-tenant
  read; the offending credential/identity is disabled.

## Communication

- If any tenant data was exposed, this is a security incident — engage
  [`security-communication`](security-communication.md) and the
  customer-notification path without delay.

## Follow-up

- Preserve the audit evidence; review whether the rate-limit thresholds
  and anomaly detection should tighten; confirm the threat model
  (`../THREAT_MODEL.md`) covers the observed vector.
