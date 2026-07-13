/** Deterministic API mocks (MSW) for web tests. */

import { HttpResponse, http } from "msw";
import { setupServer } from "msw/node";

import { DEFAULT_ME } from "./me-payload";

export { DEFAULT_ME };

export const DEFAULT_JOBS = {
  items: [
    {
      id: "11111111-1111-4111-8111-111111111111",
      job_type: "document.extract",
      status: "pending",
      priority: 100,
      attempts: 0,
      max_attempts: 5,
      run_after: "2026-07-12T10:00:00+00:00",
      created_at: "2026-07-12T10:00:00+00:00",
      finished_at: null,
      last_error: null,
      correlation_id: "corr-1",
    },
    {
      id: "22222222-2222-4222-8222-222222222222",
      job_type: "export.webhook",
      status: "dead_letter",
      priority: 100,
      attempts: 5,
      max_attempts: 5,
      run_after: "2026-07-12T09:00:00+00:00",
      created_at: "2026-07-12T09:00:00+00:00",
      finished_at: "2026-07-12T09:40:00+00:00",
      last_error: "delivery endpoint returned 503",
      correlation_id: "corr-2",
    },
  ],
  has_more: false,
  next_cursor: null,
};

export const DEFAULT_JOB_STATS = {
  by_status: { pending: 4, running: 1, succeeded: 20, dead_letter: 2, cancelled: 0 },
  oldest_pending_run_after: "2026-07-12T08:00:00+00:00",
};

export const DEFAULT_PROCESSES = [
  {
    id: "31111111-1111-4111-8111-111111111111",
    name: "Purchase orders",
    slug: "purchase-orders",
    status: "active",
    active_version_id: "32222222-2222-4222-8222-222222222222",
    active_version_number: 3,
    streams_count: 2,
    draft_count: 1,
    version: 4,
  },
  {
    id: "33333333-3333-4333-8333-333333333333",
    name: "Order confirmations",
    slug: "order-confirmations",
    status: "archived",
    active_version_id: null,
    active_version_number: null,
    streams_count: 0,
    draft_count: 0,
    version: 1,
  },
];

export const DEFAULT_STREAMS = [
  {
    id: "41111111-1111-4111-8111-111111111111",
    process_id: "31111111-1111-4111-8111-111111111111",
    name: "Email intake",
    slug: "email",
    status: "active",
    active_version_id: "42222222-2222-4222-8222-222222222222",
    process_name: "Purchase orders",
    process_slug: "purchase-orders",
    active_version_number: 2,
  },
];

export const DEFAULT_STREAM_DRAFT = {
  id: "43333333-3333-4333-8333-333333333333",
  version_number: 3,
  state: "draft",
  overrides: { confidence_floor: 0.9 },
  resolved_snapshot: null,
  pinned_process_version_id: null,
  version: 1,
};

export const DEFAULT_STREAM_DETAIL = {
  stream: {
    id: "41111111-1111-4111-8111-111111111111",
    process_id: "31111111-1111-4111-8111-111111111111",
    name: "Email intake",
    slug: "email",
    status: "active",
    active_version_id: "42222222-2222-4222-8222-222222222222",
  },
  versions: [
    {
      id: "40000000-0000-4000-8000-000000000001",
      version_number: 1,
      state: "superseded",
      overrides: {},
      resolved_snapshot: {
        process_version_id: "32222222-2222-4222-8222-222222222222",
        process_version_number: 3,
        config: { language: "en" },
      },
      pinned_process_version_id: "32222222-2222-4222-8222-222222222222",
      version: 2,
    },
    {
      id: "42222222-2222-4222-8222-222222222222",
      version_number: 2,
      state: "published",
      overrides: { confidence_floor: 0.95 },
      resolved_snapshot: {
        process_version_id: "32222222-2222-4222-8222-222222222222",
        process_version_number: 3,
        config: { language: "en", confidence_floor: 0.95 },
      },
      pinned_process_version_id: "32222222-2222-4222-8222-222222222222",
      version: 3,
    },
    DEFAULT_STREAM_DRAFT,
  ],
};

export const RESOLVE_ENVIRONMENT = { language: "en", confidence_floor: 0.85, max_pages: 50 };
export const RESOLVE_PROCESS = { language: "de" };

/** Mirrors the server's resolver shape: layers + provenance-tagged values,
 * recomputed from the overrides the client actually sent. */
export function buildResolvePreview(overrides: Record<string, unknown>) {
  const values: Record<string, { value: unknown; source: string }> = {};
  for (const [key, value] of Object.entries(RESOLVE_ENVIRONMENT)) {
    values[key] = { value, source: "environment" };
  }
  for (const [key, value] of Object.entries(RESOLVE_PROCESS)) {
    values[key] = { value, source: "process" };
  }
  for (const [key, value] of Object.entries(overrides)) {
    values[key] = { value, source: "stream" };
  }
  return {
    layers: { environment: RESOLVE_ENVIRONMENT, process: RESOLVE_PROCESS, stream: overrides },
    resolved: {
      process_version_id: "32222222-2222-4222-8222-222222222222",
      process_version_number: 3,
      policies: [],
      values,
      fingerprint: "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    },
  };
}

export const DEFAULT_SCHEMA_DRAFT = {
  id: "51111111-1111-4111-8111-111111111111",
  version_number: 2,
  state: "draft",
  definition: {
    fields: [
      {
        key: "po_number",
        label: "PO number",
        type: "text",
        required: true,
        criticality: "critical",
      },
      { key: "total", label: "Total", type: "money", criticality: "standard" },
    ],
  },
  change_summary: null,
  version: 1,
};

export const DEFAULT_SCHEMA_LISTING = {
  versions: [
    {
      id: "50000000-0000-4000-8000-000000000001",
      version_number: 1,
      state: "published",
      definition: { fields: [{ key: "po_number", label: "PO number", type: "text" }] },
      change_summary: null,
      version: 2,
    },
    DEFAULT_SCHEMA_DRAFT,
  ],
  published_json_schema: { type: "object" },
};

export const DEFAULT_RULES_DRAFT = {
  id: "61111111-1111-4111-8111-111111111111",
  version_number: 1,
  state: "draft",
  definition: {
    rules: [
      {
        key: "high-value",
        severity: "warning",
        action: "route_to_review",
        condition: {
          op: "gt",
          left: { op: "field", key: "total" },
          right: { op: "const", value: 5000 },
        },
        test_cases: [{ values: { total: 9000 }, expect_triggered: true }],
      },
    ],
  },
  change_summary: null,
  version: 1,
};

export const DEFAULT_RULES_LISTING = {
  versions: [DEFAULT_RULES_DRAFT],
  field_types: { po_number: "text", total: "money" },
};

export const handlers = [
  http.get("/api/orgs/:slug/processes/:processSlug/schema", () =>
    HttpResponse.json(DEFAULT_SCHEMA_LISTING),
  ),
  http.get("/api/orgs/:slug/processes/:processSlug/rules", () =>
    HttpResponse.json(DEFAULT_RULES_LISTING),
  ),
  http.post("/api/orgs/:slug/processes/:processSlug/rules/validate", () =>
    HttpResponse.json({ valid: true, message: null }),
  ),
  http.get("/api/orgs/:slug/streams", () => HttpResponse.json(DEFAULT_STREAMS)),
  http.post("/api/orgs/:slug/streams/:streamSlug/resolve", async ({ request }) => {
    const body = (await request.json()) as { overrides: Record<string, unknown> };
    return HttpResponse.json(buildResolvePreview(body.overrides ?? {}));
  }),
  http.get("/api/orgs/:slug/streams/:streamSlug", () => HttpResponse.json(DEFAULT_STREAM_DETAIL)),
  http.get("/api/orgs/:slug/processes", () => HttpResponse.json(DEFAULT_PROCESSES)),
  http.get("/api/me", () => HttpResponse.json(DEFAULT_ME)),
  http.get("/api/orgs/:slug/jobs/stats", () => HttpResponse.json(DEFAULT_JOB_STATS)),
  http.get("/api/orgs/:slug/jobs", ({ request }) => {
    const url = new URL(request.url);
    const status = url.searchParams.get("job_status");
    if (!status) {
      return HttpResponse.json(DEFAULT_JOBS);
    }
    return HttpResponse.json({
      ...DEFAULT_JOBS,
      items: DEFAULT_JOBS.items.filter((job) => job.status === status),
    });
  }),
  // Mutations return a valid JobSummary shape so tests exercising the
  // happy path without overriding these stay pinned to the real contract.
  http.post("/api/orgs/:slug/jobs/:jobId/replay", ({ params }) =>
    HttpResponse.json({
      ...DEFAULT_JOBS.items[1],
      id: String(params["jobId"]),
      status: "pending",
      attempts: 0,
    }),
  ),
  http.post("/api/orgs/:slug/jobs/:jobId/cancel", ({ params }) =>
    HttpResponse.json({
      ...DEFAULT_JOBS.items[0],
      id: String(params["jobId"]),
      status: "cancelled",
    }),
  ),
];

export const server = setupServer(...handlers);
