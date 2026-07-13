# Runbooks (REL-008)

Operational response procedures for the incident classes the platform
must handle. Each runbook follows the same shape — **detection,
containment, recovery, verification, communication, follow-up** — so a
responder always knows where to look under pressure.

The alert catalog ([`../ALERTS.md`](../ALERTS.md), REL-007) points at
these by slug; every alert's `runbook` field names one of the files
below, and `packages/config/tests/test_alerts.py` fails if a referenced
runbook is missing.

| Runbook | Handles | Alert-driven |
| --- | --- | --- |
| [`backlog`](backlog.md) | Queue backlog / review SLA breach | yes |
| [`provider-outage`](provider-outage.md) | Extraction/OCR provider failure | yes |
| [`failed-export`](failed-export.md) | ERP export delivery failures | yes |
| [`bad-release`](bad-release.md) | A deploy degraded extraction/quality | yes |
| [`quota-exhaustion`](quota-exhaustion.md) | Tenant hitting a quota; cost burn | yes |
| [`restore`](restore.md) | Database/object restore after loss | yes |
| [`tenant-access-incident`](tenant-access-incident.md) | Anomalous auth / cross-tenant probing | yes |
| [`credential-exposure`](credential-exposure.md) | Leaked API key or secret | on report |
| [`deletion`](deletion.md) | Customer data-deletion request | on request |
| [`object-recovery`](object-recovery.md) | Missing/mismatched stored object | on reconcile |
| [`security-communication`](security-communication.md) | Coordinating an incident's comms | supporting |

Several runbooks reference capabilities that are still owner-blocked or
unbuilt (production backups REL-003, hosted providers OPEN-003/004);
those steps say so plainly rather than promising a button that does not
exist yet.

Concrete escalation contacts and pager rotations live in the deployment
(REL-002), not in this repository; the runbooks name **roles**
(platform-on-call, security-on-call, finops) consistent with the alert
catalog's owners.
