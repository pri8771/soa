# Marketing Operations Platform

> **Working name:** Marketing Ops
>
> **Implementation state:** `FND-001` runnable application foundation
>
> **Product type:** multi-tenant B2B SaaS

Marketing Ops combines campaign project management, content operations, approvals, social scheduling, publishing, and performance in one connected campaign graph.

> Plan the campaign, produce the work, approve the content, publish every channel, and understand the result without stitching together five disconnected tools.

## What is implemented in this foundation

- Product-local `pnpm` workspace with a pinned package-manager version
- Turborepo task orchestration
- Strict TypeScript configuration shared through `packages/config`
- Server-capable React application using Next.js
- Fastify API with liveness and readiness endpoints
- Separately runnable Fastify worker health service
- Shared contracts, domain, UI, social-provider, AI, configuration, and fixture packages
- Deterministic mock AI and mock social connector primitives
- Deliberate operational command-center bootstrap screen
- ESLint, Prettier, Vitest, typechecking, builds, and product-boundary checks

This milestone intentionally does **not** implement persistence, authentication, campaign CRUD, real social publishing, or production AI. Those capabilities follow the dependency order in `docs/BUILD_BACKLOG.md`.

## Prerequisites

- Node.js 22.14 or newer
- Corepack enabled

```bash
corepack enable
```

The repository pins `pnpm@11.11.0`; Corepack will use that version automatically.

## Install and verify

From `products/marketing-ops/`:

```bash
pnpm install --frozen-lockfile
pnpm format:check
pnpm lint
pnpm typecheck
pnpm test
pnpm build
```

Or run the complete local quality gate:

```bash
pnpm check
```

## Run locally

```bash
cp .env.example .env
pnpm dev
```

Services:

- Web: `http://localhost:3000`
- API liveness: `http://localhost:4100/health/live`
- API readiness: `http://localhost:4100/health/ready`
- Worker liveness: `http://localhost:4101/health/live`
- Worker readiness: `http://localhost:4101/health/ready`

The current worker is a separately runnable foundation process with durable-job behavior scheduled for later backlog tasks.

## Workspace structure

```text
products/marketing-ops/
├── apps/
│   ├── web/                 # Next.js operational web application
│   ├── api/                 # Fastify control-plane API
│   └── worker/              # separate background-worker runtime
├── packages/
│   ├── ai/                  # provider-neutral AI contracts and mock provider
│   ├── config/              # shared strict TypeScript and runtime primitives
│   ├── contracts/           # shared transport contracts
│   ├── domain/              # provider-independent domain rules
│   ├── social-connectors/   # social provider contracts and local mock metadata
│   ├── test-fixtures/       # realistic deterministic demo fixtures
│   └── ui/                  # semantic tokens and reusable UI primitives
├── scripts/
├── docs/
├── package.json
├── pnpm-lock.yaml
├── pnpm-workspace.yaml
└── turbo.json
```

## Architecture boundary

All application code for this product stays under `products/marketing-ops/`. The boundary check rejects imports from the Sales Order Automation domain or from sibling application internals.

The accepted hierarchy is:

```text
Organization
└── Workspace / Client
    └── Brand
        └── Campaign
            ├── Brief and objectives
            ├── Work and dependencies
            ├── Assets and content
            ├── Channel variants
            ├── Approvals
            ├── Publications
            └── Performance
```

## Documentation

- [`docs/PROJECT_DOCUMENTATION.md`](docs/PROJECT_DOCUMENTATION.md) — canonical index
- [`docs/PRODUCT.md`](docs/PRODUCT.md) — product requirements
- [`docs/DELIVERY_PLAN.md`](docs/DELIVERY_PLAN.md) — start-to-finish delivery sequence
- [`docs/BUILD_BACKLOG.md`](docs/BUILD_BACKLOG.md) — executable dependency-ordered tasks
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system and data architecture
- [`docs/UI_UX_BLUEPRINT.md`](docs/UI_UX_BLUEPRINT.md) — premium UI/UX contract
- [`docs/SOCIAL_AUTOMATION.md`](docs/SOCIAL_AUTOMATION.md) — provider and publication contract
- [`docs/AI_STRATEGY.md`](docs/AI_STRATEGY.md) — local/Gemini/future provider strategy
- [`docs/SECURITY_OPERATIONS.md`](docs/SECURITY_OPERATIONS.md) — release and security gates
- [`AGENTS.md`](AGENTS.md) — implementation rules for agents and engineers

## Next task

`FND-002 — Add environment schema and configuration composition`.

Production readiness is not claimed at this stage.
