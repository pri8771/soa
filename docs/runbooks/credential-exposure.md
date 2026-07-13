# Runbook: credential / secret exposure

**Owner:** security-on-call · **Trigger:** a leaked API key, integration
credential, or platform secret is reported (gitleaks finding, customer
report, log leak)

## Detection

- A secret appears where it should not: a gitleaks CI finding (SEC-011),
  a value surfacing in logs (SEC-006 canary suite should prevent this), a
  customer report, or a third-party disclosure.

## Containment

- **Revoke first, investigate second.** Rotate or revoke the exposed
  secret immediately:
  - service API keys are hashed and revocable (TEN-009);
  - platform/integration secrets are stored as *references* (SEC-005), so
    rotate the value in the secret manager — the database holds no secret
    value to scrub.
- Invalidate any sessions/tokens derived from the secret.

## Recovery

- Issue a replacement credential and update the consumer (the integration
  or connector). Because storage is reference-based, no code or data
  migration is needed — only the backing secret changes.
- Scan history for other copies of the same secret; rotate anything that
  shared it.

## Verification

- The old secret no longer authenticates (test it fails); the new one
  works; no residual value remains in logs/telemetry (SEC-006 canary
  sweep) or in the repo (gitleaks clean).

## Communication

- If a customer's credential leaked, notify them and confirm rotation;
  drive external comms through
  [`security-communication`](security-communication.md).

## Follow-up

- Root-cause the exposure path and close it (a log statement, a
  misconfigured export, a committed secret). If committed, confirm
  `.dockerignore`/`.gitignore` and the secret-scan gate would now catch
  it. Review the audit trail for use of the secret during its exposure
  window.
