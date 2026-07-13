# Alerts and ownership (REL-007)

The production alert catalog. The source of truth is
`packages/config/src/soa_config/alerts.py` (validated by
`packages/config/tests/test_alerts.py`); this page renders it. Alerts are
defined independently of the monitoring backend — REL-002 infrastructure
wires these definitions into whatever provider the deployment uses.

Every alert carries a **severity**, an **owner** rotation, a
**threshold with a rationale**, a **runbook** (one per slug under
[`runbooks/`](runbooks/README.md), REL-008), and a **test signal**
describing how a firing is exercised. On-call rotations map to concrete people and pagers
in the deployment, not in code.

## Severity

- **critical** — page immediately; a customer-visible failure or a
  data-safety risk.
- **high** — urgent but not paging-at-3am; degradation or an approaching
  limit.
- **warning** — ticket / next business day; a trend or soft threshold.

## Catalog

| Key | Category | Severity | Owner | Signal | Condition | Runbook |
| --- | --- | --- | --- | --- | --- | --- |
| `queue.backlog_growing` | queue | high | platform-on-call | `soa.jobs.oldest_pending_age_seconds` | oldest pending job age > 900s for 10m | `backlog` |
| `queue.dead_letter_spike` | queue | high | platform-on-call | `soa.jobs.dead_lettered` | dead-letter rate > 0 sustained 5m | `backlog` |
| `provider.extraction_unavailable` | provider | critical | platform-on-call | pending AIO-013 | provider error+timeout ratio > 25% over 5m | `provider-outage` |
| `schema.repair_fallback_rate` | schema_error | warning | platform-on-call | pending AIO-012 | repair fallback ratio > 10% over 1h | `bad-release` |
| `cost.budget_burn` | cost | high | finops | usage ledger (ANA-003) | projected monthly cost > budget (ANA-009) | `quota-exhaustion` |
| `sla.review_overdue` | sla | high | platform-on-call | operational snapshot (ANA-001) | overdue/open review > 20% for 30m | `backlog` |
| `export.delivery_failures` | export | critical | platform-on-call | delivery attempts (EXP-005/008) | delivery failure ratio > 20% over 15m | `failed-export` |
| `backup.stale_or_failed` | backup | critical | platform-on-call | pending REL-003 | no successful backup within the RPO window | `restore` |
| `auth.anomalous_denials` | auth_anomaly | high | security-on-call | audit events (DB-005) | auth-failure / cross-tenant-404 spike from one principal or IP | `tenant-access-incident` |
| `quota.exhaustion_imminent` | quota | warning | platform-on-call | feature flags / quotas (ANA-009) | tenant usage > 90% of a resolved quota | `quota-exhaustion` |

## Signals that are not live yet

Three alerts name a **pending** signal rather than a live `soa.*`
metric, because the code that emits the signal is not built yet — this is
stated honestly in the catalog rather than papered over:

- `provider.extraction_unavailable` → the provider router's failover
  telemetry (AIO-013 is wired per-stream during the pipeline build-out).
- `schema.repair_fallback_rate` → the schema-repair counter (AIO-012).
- `backup.stale_or_failed` → backup freshness (REL-003).

Each becomes armable when its task lands; the alert definition, owner,
and runbook are already fixed so nothing is forgotten.
