# Engineering handoff

**Updated:** 2026-07-15
**Integration branch:** `dev`
**Release decision:** not production-approved; see
[`PRODUCTION_READINESS_AUDIT.md`](PRODUCTION_READINESS_AUDIT.md).

This document is a resume guide, not a historical task diary. The Git history,
Jira, and Notion retain project history; the production-readiness audit states
what is currently proven and what still needs external evidence.

## Integration status

The cross-audit reconciliation described here was locally validated for
integration into `dev` on 2026-07-15. The remote `dev` head and its CI results
are the source of truth; confirm both before using this handoff as release
evidence. Local validation does not replace the staging and live-environment
evidence listed below.

## Validation checkpoint

- Python: Ruff, formatting, mypy, and the complete 1,850-test collection
  exited successfully.
- Web: formatting, lint, typecheck, production build, all 372 unit tests, and
  all three accessibility E2E tests passed; E2E passed twice consecutively.
- Docs, dependency/security scanning, and all 12 performance checks passed.
  The security subset recorded 61 passes and 15 PostgreSQL-only skips.
- PostgreSQL/RLS, container, and Terraform validation still require CI or a
  workstation with the corresponding services and Terraform 1.12.2. They are
  not represented as locally proven gates.

## Product and launch boundary

SOA is a multi-tenant purchase-order-to-sales-order platform. A published
stream resolves immutable schema, instruction, rule, catalog, confidence,
provider/data, credential, and mapping inputs. The durable worker processes one
sales order per input, routes risk to an evidence-first review workbench, and
delivers an approved canonical order with idempotency and audit history.

Mixed-packet classification/splitting is not part of the launch path. Do not
weaken the one-order contract until a representative evaluation proves the new
behavior.

## Current implementation anchors

- `apps/api/src/soa_api/app.py` composes the HTTP surface. Production uses
  OIDC/JWT, PostgreSQL-backed abuse limits, GCS, GCP Secret Manager, ClamAV,
  exact-host outbound controls, and OTLP through validated settings.
- `apps/worker/src/soa_worker/main.py` is the only production worker entry
  point. It registers durable preprocess, stage, export, evaluation, outbox,
  organization-export/cleanup, upload-cleanup, and secret-revocation handlers.
- `apps/worker/src/soa_worker/database_queue.py` implements claims,
  heartbeats, recovery, retry/dead-letter transitions, and queue metrics.
- `apps/worker/src/soa_worker/run_config.py` authenticates the exact published
  run snapshot before creating an executor; `pipeline.py` performs the stage
  work; `routed_extraction.py` runs the pinned provider chain and records every
  attempt.
- `packages/db/src/soa_db/tenant_guard.py` is the RLS registry. Migration
  `0045` also strips provider-created elevated role membership from `soa_app`
  and grants only runtime DML/sequence privileges.
- `apps/api/src/soa_api/ops/seed.py` is the deterministic development seed.
- `apps/api/src/soa_api/routers/providers.py` owns write-only provider key
  create/rotate, guarded revoke, provider/confidence policy
  draft/validate/publish, routing preview, and durable health reads. Migration
  `0049` stores only RLS-scoped credential metadata; API responses omit both
  values and secret-store references.
- `packages/integrations/` owns shared connection-test capability and
  destination validation. QuickBooks Online is the only executable ERP
  delivery adapter; NetSuite, Dynamics, and SAP intentionally fail delivery
  closed pending vendor-specific idempotent upserts and sandbox certification.
- `infra/terraform/` defines the GCP reference deployment; the release
  workflows promote independently scanned immutable API, migrator, worker, and
  web artifacts.

## Significant remediation now in the tree

- Worker execution, immutable run configuration, real page text, runtime
  catalog/business validation, server-executed evaluations, promotion gates,
  outbox delivery, and complete organization export are wired end to end.
- Provider routing/fallback is policy-bound and tenant-scoped. Hosted empty
  credentials fail closed. Token counts, rate-card estimates, malformed-output
  repair calls, failed attempts, and fallback attempts retain separate
  provider/model/cost attribution; unknown usage remains explicitly unknown.
- Provider health is persisted across worker replicas and exposed through the
  administration API. Production OTLP export is batched and flushed on
  shutdown.
- Browser Firebase/generic-OIDC authentication and core organization,
  membership, role, export, stream, integration, and credential-management
  paths call real APIs instead of placeholder state.
- Service credentials are managed through Settings and authenticated
  create/list/rotate/revoke endpoints. Raw keys are returned only on create or
  rotate with `no-store`, every mutation is version-checked, and migration
  `0050` makes pre-scope keys fail closed until replaced with an explicit
  stream allowlist and expiry.
- Outbound tests and delivery share HTTPS/exact-host/public-address checks;
  paused integrations cannot export; last-admin removal is blocked; credential
  rotation schedules old integration-secret revocation durably. Provider
  rotations retain superseded keys for immutable policy/run pins and guard
  explicit revocation against references.
- Multipart and email intake are bounded for the reference Cloud Run request
  ceiling; staging/production rate limits are atomic across replicas.
- Every signed upload schedules primary and delayed-sweep expiry cleanup, and
  its signed URL cannot outlive the database session. Cleanup, completion, and
  abort lock the same tenant row; the sweep removes late PUT bytes after
  abort/expiry without deleting completed uploads.

## Resume rules

1. Preserve the one-order launch boundary, immutable published configuration,
   tenant/RLS binding, append-oriented history, and fail-closed provider and
   connector behavior.
2. Never restore deployment-global provider selection, caller-supplied
   evaluation predictions, process-local production rate limits, or
   duplicate-prone generic ERP POSTs.
3. A connection test proves reachability and authorization only; it does not
   prove mapping correctness, idempotent delivery, OAuth refresh, or production
   readiness.
4. Model confidence is uncalibrated. Provider/candidate promotion requires the
   corresponding server-executed gold evaluation; consequential automation
   remains conservatively reviewed until false-auto-approval is measured.
5. Keep code, migration, documentation, Terraform, and release-contract tests
   in the same change. Do not mark a live-environment gate passed from a mock or
   unit test.

## Local setup

Use [`LOCAL_DEV.md`](LOCAL_DEV.md). The current paths are:

```bash
make bootstrap
make local-up        # optional: PostgreSQL + MinIO + Mailpit; profiles add scanner/telemetry
make migrate
make seed
make dev
```

`make dev` supervises API, worker, and web and defaults both services to the
same development filesystem object store. PostgreSQL is still required. Node
22 is the supported JavaScript runtime.

## Verification before handoff or commit

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
volta run --node 22 pnpm run lint
volta run --node 22 pnpm run format:check
volta run --node 22 pnpm run typecheck
volta run --node 22 pnpm run test
volta run --node 22 pnpm run build
python3 scripts/check_docs.py
git diff --check
```

Also run the real PostgreSQL/RLS and migration cycle whenever schema, tenancy,
jobs, rate limiting, provider metrics, or database privileges change. Run the
browser, container, Terraform, security, performance, and resilience gates in
proportion to the affected surface.

The migration-head assertion currently expects `0053`. Confirm the directory
and `packages/db/tests/test_migrations.py` before changing this number.

## Remaining release work

The remaining work is dominated by evidence and external state, not missing
local scaffolding:

1. apply and validate the GCP staging environment and Workload Identity
   Federation;
2. configure and test live identity, scanner, email ingress, outbox, telemetry,
   alerts, and on-call ownership;
3. build a rights-cleared representative corpus, calibrate quality/risk gates,
   and reconcile provider usage estimates to invoices;
4. certify the selected ERP sandbox, mapping, OAuth refresh, retry, and
   idempotency behavior;
5. complete backup/PITR and object restore, release rollback, fault/load/soak,
   penetration, accessibility/usability, incident, legal, and pilot-acceptance
   gates.

Do not call the branch production-ready until all items in
[`PRODUCTION_READINESS_AUDIT.md`](PRODUCTION_READINESS_AUDIT.md) have attached
environment evidence and an explicit go/no-go approval.
