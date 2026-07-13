# Session Handoff — 2026-07-12

> **Purpose:** capture decisions and state from the planning session so any new Claude Code session (or engineer) can resume without re-litigating. Read this together with `AGENTS.md` and `docs/BUILD_BACKLOG.md` before writing code.

## 1. Current state

- **Progress (updated as epics complete):** FND (12), DB (5), TEN (12), DSN (9), JOB (8), CFG (14), STO (6), ING (14), PRC (14), REV (16), CAN (4), EXP (10 of 11), AIO (15 of 19), CAT (12 of 13), and ANA (8 of 9) are DONE — implemented, tested, committed to `dev`, CI green. (REV-016's human study runs during PIL. EXP-011 is P1, blocked on the pilot-ERP-choice ADR — an OWNER decision. AIO deferred items: AIO-005 local layout OCR is P1 with a heavy isolated dependency and a benchmark gate; AIO-006 hosted OCR is blocked on OPEN-003 (managed-OCR provider decision — OWNER); AIO-008/009 hosted extraction adapters are blocked on OPEN-004 (production provider decision — OWNER) — the AIO-001 contracts, AIO-011 request builder, and AIO-012 repair policy mean each lands as an adapter + registration, no pipeline change. CAT-008 semantic/embedding matching is P1, deferred: the CAT-007 fuzzy scorer covers the pilot need and CAT-008 adds a heavy model dependency behind the same MatchPolicySet seam. Known wiring gaps stated in commits: the provider router and evaluation gate are libraries awaiting per-stream config resolution and evaluation-run persistence; the simulation endpoint and provider health honestly report those gaps; the CAT-011/012/013 validation libraries and the ING-006 business-duplicate hook await the same per-stream config resolution to run inside the validating stage; audit export bundles build synchronously under a 100k-event cap until the worker claim loop is wired. ANA-008 internal support console is DEFERRED: it needs a platform-staff/support-access identity model that TEN never delivered and that hinges on OPEN-002 (production identity provider — OWNER decision); the SECURITY_OPERATIONS principles for it are explicit/time-limited/audited grants, so it lands as a grant table + console surface once the identity question is answered.) SEC is underway: SEC-001 threat model (`docs/THREAT_MODEL.md`, DRAFT pending the owner's pre-pilot review), SEC-002 headers/CSP/CORS, SEC-003 rate limiting, SEC-004 sandbox hardening (`docs/SANDBOX_PROFILE.md`; the container-layer profile is a deployment requirement verified in REL), SEC-005 secret store (values behind `soa_config.SecretStore` references — memory/file dev backends, AWS Secrets Manager production adapter in `soa_storage.secrets_aws`), SEC-006 sensitive-logging canary suite (`tests/security/test_sensitive_logging.py`, required CI), SEC-007 prompt-injection corpus (`soa_fixtures.injection_corpus` driving `tests/security/test_prompt_injection.py`), and SEC-008 retention-policy engine (`packages/db/src/soa_db/retention.py` — a pure engine: pinned windows, legal-hold absolute block, scheduled eligibility, gated `RETAINED->ELIGIBLE->PENDING_APPROVAL->APPROVED->DELETED` state machine; SEC-010 wires persistence + object deletion), and SEC-009 customer data export (`soa_db/data_export.py` collector + `POST /orgs/{slug}/documents/{id}/data-exports` gated on the new `data.export` permission — a signed, expiring bundle documenting all 13 data categories with counts, audited counts-only; org-wide async export awaits the worker claim loop) are DONE. SEC-010 data deletion workflow (`soa_db/data_deletion.py` + migration 0037 `deletion_tombstones` — approval-gated erasure of a document's objects and derived rows, reconciled, idempotent/retry-safe, leaving a retained tombstone + counts-only audit; document row and audit trail kept for attributability; provider-purge is a documented no-op until hosted adapters, backup retention a documented exception) is DONE. Next action: `SEC-011` in `docs/BUILD_BACKLOG.md` (dependency/container/secret/SBOM pipeline), then the rest of SEC in order.
- **CI conventions learned the hard way:**
  - The migrations job runs all `-m postgres` tests as the non-superuser `soa_app` role (superusers bypass RLS; the bootstrap image user is a superuser).
  - RLS policies must use `NULLIF(current_setting(...), '')::uuid` — a reverted `SET LOCAL` GUC reads as `''` on pooled connections.
  - Playwright baselines are CI-rendered and canonical. To regenerate: push a commit whose message contains `[update-baselines]`; a ci.yml job rewrites `apps/web/e2e/__screenshots__/**` on the runner and pushes a bot commit (pull/rebase afterwards). Local runs of the app-shell shot differ by ~293px (unicode nav-icon font fallback) — expected. Comparison budget is a strict `maxDiffPixels: 64`.
  - Each new migration must bump the head assertion in `packages/db/tests/test_migrations.py`; new tenant tables get FORCED RLS policies in their migration and an entry in `soa_db.tenant_guard.RLS_PROTECTED_TABLES`.
- **Branch model (owner decision):** `main` (stable) / `qa` (staging) / `dev` (active development). All three exist on origin. **All implementation work happens on `dev`.** Do not create per-session throwaway branches; if the platform auto-creates one, merge to `dev` and continue there. Stray legacy branches (`agent/*`, `marketing-ops-foundation`, `phase-0-foundation`, `claude/app-setup-run-29yqln`) are historical and unused.
- **Prototype:** `prototype/soa-prototype.html` is a fully self-contained clickable UI prototype (no backend, mock data). Open directly in a browser. `prototype/claude-design-prompt.md` is the design prompt used to specify it. These are design references, not production code.

## 2. Decisions made in the 2026-07-12 planning session

These extend or refine the canonical docs; they are owner-approved product decisions.

### 2.1 Skills-first information architecture (refines UI_UX_BLUEPRINT §3)

The user-facing navigation model is **skills-first** (ABBYY Vantage-style), not the admin hierarchy:

```text
Login → Skills list (cards: name, volume, backlog, straight-through rate, status)
  → Skill workspace with tabs:
      1. Documents      (queue scoped to the skill)
      2. Analytics      (operations / quality / cost scoped to the skill)
      3. Senders & templates   (per-sender learned field hints — see 2.2)
      4. Training documents    (ONLY visible when the skill has a gold/evaluation set)
      5. Configuration  (schema, rules, routing — lighter weight)
```

A "Skill" is the user-facing name for a configured Process/Stream. Admin surfaces (Catalogs, Integrations, Org analytics, Settings) move to a secondary "Manage" area. Review Studio, evidence overlays, keyboard model, and design tokens in `UI_UX_BLUEPRINT.md` are unchanged.

### 2.2 Sender templates / "teach this field" (new capability — needs backlog epic)

Owner explicitly rejected silent model retraining (Option A) and approved the explicit mechanism (Option B), consistent with ADR-017:

- When a reviewer corrects a field, they may propose the correction as a **layout/anchor hint** for that sender (e.g., "PO date — top-right, near label 'Order Date:'").
- Proposals are stored per `(organization, stream/skill, sender)` as **versioned, inspectable configuration** — statuses `Proposed → Published | Rejected`.
- A supervisor must explicitly **publish** a proposal; nothing changes live behavior silently. Publishing follows the standard draft→review→publish pattern (ADR-012) and is tenant-isolated (ADR-011).
- Implementation candidates: deterministic anchor/region hints and/or few-shot corrected examples included in extraction requests (in-context, never training). Evidence records already track `prompt_or_instruction_version`, which accommodates this.
- **TODO for a future session:** add this as a proper epic (suggested ID `LRN`) to `docs/BUILD_BACKLOG.md` with tasks for the model, proposal capture in Review Studio, supervisor publish UI, extraction-request integration, and match-rate telemetry. It depends on REV + AIO epics.

### 2.3 ERP integration posture

No ERP adapter for the first pilot. Clients consume our **API / canonical JSON / signed webhook** and connect their own ERP. This matches ADR-023; `EXP-011` (first production ERP adapter) stays P1/deferred. Keep `EXP-010` (generic adapter contract) so a real adapter can be added without reworking export orchestration.

### 2.4 Execution mode

Owner wants autonomous task-by-task execution: start at `FND-001`, work down the backlog in dependency order, commit per task (or tightly coupled group) to `dev`, push regularly, and do not stop to ask between tasks unless a genuine product decision is required.

## 3. Environment facts (verified in the cloud container, 2026-07-12)

| Tool | Version | Note |
|---|---|---|
| node | v22.22.2 | pin Node 22 |
| pnpm | 10.33.0 | workspace manager for web/packages |
| python3 | 3.11.15 | prefer uv-managed 3.12+ if pinning higher |
| uv | 0.8.17 | Python dependency manager |
| docker | 29.3.1 | available in-session; `docker-compose` binary absent — use `docker compose` plugin or verify availability |
| make | GNU Make 4.3 | command runner (`just` not installed) |

GitHub access in cloud sessions goes through the GitHub MCP tools (no `gh` CLI). Outbound HTTPS uses a preconfigured proxy — do not disable TLS verification.

## 4. Resume instructions for a new session

1. `git checkout dev` (create from `origin/dev` if needed).
2. Read `AGENTS.md`, then `docs/BUILD_BACKLOG.md` §FND, then this file.
3. Begin at `FND-001 — Establish monorepo and toolchain`; proceed in backlog order. The backlog has **214 tasks across 17 epics**; epic order: FND → DB → TEN → DSN → JOB → CFG → STO → ING → PRC → REV → CAN → EXP → AIO → CAT → ANA → SEC → REL → GTM → PIL (ENT is post-pilot P1).
4. Honor every AGENTS.md rule (tests, tenant analysis, telemetry, docs per task). Commit per task with the task ID in the message; push to `dev` regularly.
5. Integration tests that need PostgreSQL/MinIO should run against local Docker services when available and skip cleanly (with a visible marker) when not — never fake a pass.

## 5. Open items / deferred choices (unchanged from DECISIONS.md)

- OPEN-001 hosting provider — decide before staging infra work.
- OPEN-002 production identity provider; OPEN-003 managed OCR; OPEN-004 hosted extraction provider/model — all deferred behind stable contracts.
- Artifact publishing of the prototype to claude.ai failed with a tool-permission error in the 2026-07-12 session; retry from a future session if a shareable link is wanted.
