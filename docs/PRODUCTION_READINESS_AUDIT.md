# Production Readiness Audit

**Last verified:** 2026-07-15
**Baseline:** `codex/production-remediation` through the 2026-07-15 ten-task
capability checkpoint
**Decision:** **not production-ready yet**

This is the corrective source of truth for release readiness. A backlog item
being implemented in isolation is not equivalent to a working production
capability. Release claims require runtime wiring and evidence.

## 2026-07-15 ten-task capability checkpoint

The ten code and integration gaps from the prior audit are implemented and
verified locally:

1. Intake pins instruction, confidence, provider/data policy, credential
   reference, and execution fingerprint alongside schema and rules.
2. Workers authenticate and execute those immutable pins, pass the published
   instruction and tenant credential into model calls, and persist field-level
   call provenance.
3. Catalog/customer/product/UOM, line, duplicate-PO, and business validation
   execute in the validating stage and participate in review routing.
4. P0 enforces a single-sales-order input contract at publication and runtime;
   mixed packets are deliberately unsupported rather than silently mishandled.
5. Evaluation runs, checkpoints, reports, candidate/baseline comparisons, and
   simulation views are durable and tenant-isolated.
6. Repeat stream publication is gated by a successful evaluation for the exact
   candidate fingerprint when the evaluation gate is enabled.
7. A versioned ten-cohort synthetic gold-set manifest covers representative
   formats, currencies, ambiguities, boundary values, and negative cases.
8. Domain events are transactionally scheduled for an idempotent HTTP outbox
   publisher with retry, dead-letter, inspection, and replay controls.
9. Organization-scale exports are durable, batched, cancellable, resumable,
   progress-reporting jobs with manifests and expiring downloads.
10. Hosted-provider bootstrap accepts tenant-resolved secret references without
    requiring a deployment-global API key; explicit empty credentials still
    fail closed.

This is a **code-complete checkpoint, not production approval**. The gold set
currently contains the cohort/expected-result manifest rather than redistributable
source document bytes, and evaluation execution consumes candidate predictions
submitted by the inference workflow. Deployment configuration and the external
evidence gates below remain mandatory.

## What works now

- The API, web app, worker, migrations, tenant-scoped repositories, audit
  trail, durable jobs, object-store adapters, review flow, canonical payload,
  catalog APIs, ERP delivery adapters, and infrastructure definitions exist.
- The worker now claims PostgreSQL-backed jobs, recovers expired locks,
  heartbeats active jobs, acknowledges success, retries transient failures,
  and dead-letters permanent/exhausted failures. Before this remediation the
  production entry point registered no handlers and supplied no job fetcher.
- Every stage now authenticates its run's tenant-scoped immutable stream
  version and recomputes the stored snapshot fingerprint. Missing, mutable,
  cross-tenant, altered, or intake-mismatched configuration fails terminally
  before document processing code executes.
- The authenticated snapshot now selects the run's pinned schema fields,
  criticality, field normalizers, authored rules and rule version, languages,
  locale/currency, and field-extraction provider. Executors are cached only by
  tenant + stream version + verified fingerprint; deployment-wide extraction
  defaults are no longer used by the production entry point.
- Real extraction providers now receive actual per-page document text. Native
  PDF text is preferred; low-coverage/image pages use bounded local OCR. The
  recognized input is retained as a page artifact. Before this remediation,
  non-mock LLM requests contained no document text.
- `make seed` migrates into a deterministic, idempotent 56-row Northstar demo
  baseline, including five users, four active catalogs, two published streams,
  schema/rules/provider policy, and five sample documents.
- `make dev` supervises API, worker, and web processes and shuts the group down
  when signaled or when a child fails.
- Functional/accessibility browser tests and pinned-renderer visual regression
  are distinct gates. Platform font rasterization no longer makes functional
  E2E fail on macOS.
- The web bundle is route-split. The former 937 kB monolithic JavaScript chunk
  is replaced by a 35 kB app entry, bounded vendor chunks, and per-screen
  chunks. The build has no large-chunk warning.
- SQLite uses non-retained connections, eliminating event-loop/aiosqlite worker
  thread leaks in the test suite. The supply-chain gate now fails if a scanner
  crashes instead of treating a missing report as zero vulnerabilities.
- macOS test execution skips only the unsupported `RLIMIT_AS` control. Linux
  production retains address-space limits; the container/cgroup profile is
  still a deployment gate.

## Verified evidence

| Gate | Result |
|---|---|
| Python suite | full suite passed at 100%; only deliberate platform/security-test warnings remain |
| Web unit/component | 200 passed |
| Design system | 108 passed |
| Browser accessibility E2E | 3 passed |
| Migration + seed | clean SQLite upgrade; first seed 56 created; second seed 0 created/56 existing |
| Lint/format | passed; the previous eight frontend warnings were removed |
| Python/TypeScript type checks | passed |
| Production web build | passed; no chunk over 500 kB |
| Performance microbenchmarks | 12 passed |
| Supply-chain policy | passed; no blocking findings; scanner execution is now fail-closed |
| Docs command/link check | passed before this document; rerun in the final gate |

## Release blockers

### Remaining product and operational gaps

1. Replace the synthetic gold manifest's placeholder source hashes with a
   rights-cleared corpus and run inference-to-evaluation end to end in staging.
2. Configure the production outbox destination, tenant secret references,
   hosted provider endpoints/models, and an expired-export object cleanup job.
3. Complete settings/integration administration UX and production OIDC
   administration/support-access UX.
4. Keep the single-sales-order constraint visible in product copy. Mixed-packet
   classification/splitting is a separately evaluated post-P0 capability.

### Environment, owner, and external-evidence blockers

1. Select and configure the production identity provider, apply Firebase/OIDC
   settings, and execute role/session/revocation tests in staging.
2. Decide the managed OCR posture and data terms, or formally constrain launch
   to the evaluated local OCR profile.
3. Apply Terraform to a billed GCP staging project; configure WIF, domains,
   certificates, secrets, Cloud SQL, GCS, Cloud Run, and Firebase Hosting/Auth.
4. Run PostgreSQL/RLS, MinIO/GCS, container, Trivy, migration, rollback,
   backup/PITR, restore, provider-failover, and load tests in that environment.
5. Complete an independent penetration test and close all release-blocking
   findings.
6. Build the representative pilot gold set, import reconciled master data, run
   parallel operations, complete reviewer usability/accessibility studies, and
   obtain customer acceptance before controlled auto-release.

## Ordered next work

1. Assemble the rights-cleared source-byte gold corpus and run the complete
   inference/evaluation/promotion path against it.
2. Deploy with Node 22, PostgreSQL/RLS, GCS, Cloud Run, production secrets, and
   the outbox destination; execute migration, rollback, restore, and failover.
3. Add expired-export lifecycle cleanup and operator dashboards/alerts for
   evaluations, outbox delivery, and export throughput.
4. Finish settings/integration/identity administration UX.
5. Execute load, security, accessibility/usability, and pilot-acceptance gates.
   Production approval follows evidence; it does not precede it.

## Local verification limitations for this audit

The machine had Node 24 while the repository requires Node 22. JavaScript
checks passed with an engine warning and must be repeated on Node 22. The Docker
CLI was installed, but the daemon was stopped and the Compose plugin was
absent, so local PostgreSQL/MinIO/container builds could not be executed. These
are recorded as unverified—not passed. Visual snapshots are intentionally
verified only on the pinned Linux CI renderer.
