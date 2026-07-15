# Production Readiness Audit

**Last verified:** 2026-07-15
**Baseline:** `38a7a2736390e0a53f6a7c487a1030a4e9a98eed` plus the unmerged
`codex/production-remediation` changes described below
**Decision:** **not production-ready yet**

This is the corrective source of truth for release readiness. A backlog item
being implemented in isolation is not equivalent to a working production
capability. Release claims require runtime wiring and evidence.

## What works now

- The API, web app, worker, migrations, tenant-scoped repositories, audit
  trail, durable jobs, object-store adapters, review flow, canonical payload,
  catalog APIs, ERP delivery adapters, and infrastructure definitions exist.
- The worker now claims PostgreSQL-backed jobs, recovers expired locks,
  heartbeats active jobs, acknowledges success, retries transient failures,
  and dead-letters permanent/exhausted failures. Before this remediation the
  production entry point registered no handlers and supplied no job fetcher.
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
| Python suite | 1,496 passed, 35 skipped, 2 warnings in 64.75 seconds |
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

### Code and integration blockers

1. **Pinned stream configuration is not executed by the worker.** Intake pins a
   stream version and fingerprint, but the running pipeline still uses one
   deployment-wide `PipelineConfig` and extraction provider. Schema, rules,
   confidence, locale/language, instruction version, provider routing, and
   tenant credential references must resolve from the run's immutable snapshot.
2. **Classification and packet splitting are explicit placeholders.** The
   current P0 stream assumes one sales-order document per upload. Either ship a
   validated single-document product constraint or implement and evaluate the
   configured classifier/splitter before advertising mixed-packet support.
3. **Evaluation is not a production workflow.** The offline runner and
   promotion-gate library exist, but evaluation runs/checkpoints/reports are not
   persisted, the simulation endpoint has no runs to display, and publishing is
   not transactionally gated on an approved comparison.
4. **Catalog/business validation libraries are not wired into the validating
   stage.** Customer/ship-to/material/UOM matching, line validation, and
   duplicate-PO checks exist as libraries/UI behavior but do not yet run from
   each pinned stream configuration.
5. **Outbox delivery has no running publisher.** Durable domain events are
   stored, but notification/integration consumers beyond directly scheduled
   export jobs are not drained by a production process.
6. **Large audit/customer exports remain synchronous and capped.** Add durable
   export jobs, progress, cancellation, retention, and worker execution for
   organization-scale requests.
7. **The settings route is still a placeholder**, integration creation is API
   only, and production OIDC administration/support-access UX is incomplete.

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

1. Build a worker-owned, immutable `ResolvedRunConfig` loader and provider
   factory; reject missing/mismatched fingerprints and add historical-version,
   tenant-isolation, secret-reference, and restart tests.
2. Wire published instructions and safe request construction into every model
   call; persist provider/model/instruction/config provenance on results.
3. Wire catalog matching, line validation, duplicate-PO validation, confidence,
   and review routing into the validating transaction with deterministic gold
   cases.
4. Add evaluation-run persistence and migration, durable execution/checkpoint
   jobs, comparison API, simulation data, and a publish-time promotion gate.
5. Make the P0 input contract explicit: enforce one PO per input now, then add
   classifier/splitter providers and packet gold tests as a separately gated
   expansion.
6. Add a claimed/leased outbox publisher and durable large-export handlers with
   retry, dead-letter, metrics, and operator controls.
7. Finish settings/integration administration UX and production identity
   integration.
8. Execute the external staging/security/pilot gates above. Production approval
   follows evidence; it does not precede it.

## Local verification limitations for this audit

The machine had Node 24 while the repository requires Node 22. JavaScript
checks passed with an engine warning and must be repeated on Node 22. The Docker
CLI was installed, but the daemon was stopped and the Compose plugin was
absent, so local PostgreSQL/MinIO/container builds could not be executed. These
are recorded as unverified—not passed. Visual snapshots are intentionally
verified only on the pinned Linux CI renderer.
