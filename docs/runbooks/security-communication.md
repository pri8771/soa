# Runbook: incident & security communication

**Owner:** security-on-call (security incidents), platform-on-call
(operational) · **Role:** the shared communication procedure the other
runbooks defer to

Not every incident needs external comms, but when one does, the path
must be consistent and pre-agreed so no one improvises disclosure under
pressure.

## Detection

- An incident has crossed a communication threshold: customer-visible
  impact, a data-exposure or data-loss event, or a missed SLA
  commitment. The originating runbook decides; this runbook handles
  *how* to communicate.

## Containment (of the message, not the incident)

- Designate a single incident lead and a single comms owner — one voice.
- Classify severity (a shared model: SEV1 customer-data or platform-wide
  down; SEV2 significant degradation; SEV3 minor/contained). Severity
  sets who is notified and how fast.
- Do not disclose specifics that are still unconfirmed; state what is
  known, what is being done, and when the next update comes.

## Recovery (the communication itself)

- **Internal:** open the incident channel, record a timeline, page the
  owning rotation (mapping to people/pagers lives in the REL-002
  deployment, not here).
- **Customer / external:** use the customer status path with the
  prepared templates; for data-exposure events follow the contractual /
  regulatory notification timeline. Keep updates flowing on the cadence
  the severity demands until resolved.

## Verification

- Every affected party received the notification appropriate to their
  exposure; the status path reflects the current state; commitments made
  in-message match what the support plan allows.

## Communication

- This *is* the communication runbook — its output is the messages; the
  verification above is the check that they landed.

## Follow-up

- Publish a post-incident review (timeline, root cause, remediation,
  prevention) within the committed window; feed prevention items back
  into the backlog and, for security incidents, the threat model
  (`../THREAT_MODEL.md`) and `../SECURITY_OPERATIONS.md`.
