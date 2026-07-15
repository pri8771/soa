# Status and incident communication (REL-011)

How the platform coordinates an incident internally and communicates it
to customers: a shared severity model, a single-owner process, the
status path, and message templates — agreed in advance so nobody
improvises disclosure under pressure.

This is the communication layer above the operational response. The
per-incident mechanics of *acting* on a failure live in
[`runbooks/`](runbooks/README.md) (REL-008); the runbooks defer here for
*how to communicate*, and [`runbooks/security-communication.md`](runbooks/security-communication.md)
is the entry point they call.

> **Implementation status:** the severity model, templates, alert owners, and
> runbooks exist. A status-page vendor, incident channel/bridge, concrete
> contacts/pagers, support-plan deadlines, and completed game-day record do not
> live in this repository and must be configured/proved before production.

## Severity model

| Severity | Definition | Internal | Customer |
| --- | --- | --- | --- |
| **SEV1** | Customer data at risk (loss/exposure) or platform-wide outage | Page immediately, incident bridge opened | Proactive notice; status page; regulatory clock if data exposed |
| **SEV2** | Significant degradation — a core flow (intake, review, export) impaired for many tenants, or one tenant hard-down | Page on-call | Status page update; direct notice to affected tenants |
| **SEV3** | Minor or contained — single-tenant, degraded-not-down, or a workaround exists | Ticket, next business hours | Status update only if customer-visible |

Severity drives who is notified and how fast; it is set by the incident
lead and can be raised (never quietly lowered) as understanding changes.
The alert catalog ([`ALERTS.md`](ALERTS.md), REL-007) assigns each alert
a severity that seeds the incident's starting level.

## Roles

- **Incident lead** — owns the response and the timeline; the single
  decision-maker.
- **Communications owner** — owns all outbound messages (internal and
  external); the single voice, so customers never get conflicting
  updates.
- **Responders** — the on-call rotations that own the affected surface
  (platform-on-call, security-on-call, finops — the same owners named in
  the alert catalog). Concrete people/pagers are wired in the deployment
  (REL-002), not in this repo.

## Internal process

1. **Declare** — anyone can declare an incident; declaring is cheap,
   under-reacting is not. Open the incident channel/bridge and name the
   lead and comms owner.
2. **Assess** — set severity, identify blast radius, start the timeline
   (every state change and decision timestamped).
3. **Respond** — work the relevant runbook(s); the lead coordinates, the
   comms owner keeps stakeholders updated on the severity's cadence.
4. **Resolve** — confirm recovery via the runbook's verification step;
   downgrade/close only when verified.
5. **Review** — a blameless post-incident review within the committed
   window (below), feeding prevention items back into the backlog and,
   for security incidents, the threat model and
   [`SECURITY_OPERATIONS.md`](SECURITY_OPERATIONS.md).

## Customer status path

- A **status page** communicates platform-wide state (SEV1/SEV2) —
  investigating → identified → monitoring → resolved.
- **Direct notice** (email to affected tenants' admins) for
  tenant-specific impact and for any data-exposure event.
- Updates flow on a **cadence set by severity** (SEV1: frequent until
  mitigated; SEV2: at each state change; SEV3: on resolution) until the
  incident is resolved.

Commitments made in any message must match what the support plan
actually offers — response/resolution targets and notification timelines
are quoted from the plan, never invented in the moment. (The concrete
support-plan numbers land with GTM; this document is the process they
plug into.)

## Message templates

**Status-page update (SEV1/SEV2)**

> **[Investigating|Identified|Monitoring|Resolved] — <short title>**
> <timestamp UTC>. We are <current action>. Impact: <who/what is
> affected>. Next update by <time>.

**Customer notice (tenant-specific / data event)**

> Subject: <Service> incident affecting your account — <status>
> What happened: <plain-language summary>. What we did: <containment /
> recovery>. What you should do: <action or "no action needed">. We will
> follow up with <post-incident review / next update> by <time>.

**Post-incident review (internal, shared with affected customers on
request)**

> Timeline · Root cause · Impact (who/how long) · What went well · What
> we are changing (with owners and dates).

## Validation

The process is exercised, not just written: a **game-day / tabletop
drill** runs each severity through declaration → response → comms →
review against a simulated failure, confirming the roles, channels, and
templates work and that commitments match the support plan. The first
full drill runs during pilot preparation against the applied staging
environment. Run it before customer data is accepted; record participants,
timestamps, notifications, observed gaps, and remediation owners and fold the
results back here.
